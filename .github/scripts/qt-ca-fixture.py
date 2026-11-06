"""Disposable loopback TLS endpoints and public CA preparation; never real application data."""

import ctypes
import http.server
import json
from pathlib import Path
import shutil
import ssl
import sys
import threading
import time
import urllib.error
import urllib.parse
import urllib.request


class Endpoint(http.server.ThreadingHTTPServer):
    def __init__(self, directory, label):
        class Handler(http.server.BaseHTTPRequestHandler):
            def do_GET(handler):
                with handler.server.lock:
                    handler.server.requests += 1
                body = b"Isolated TLS fixture, deliberately not a torrent.\n"
                handler.send_response(200)
                handler.send_header("Content-Type", "application/x-bittorrent")
                handler.send_header("Content-Length", str(len(body)))
                handler.end_headers()
                handler.wfile.write(body)

            def log_message(self, *_):
                pass

        super().__init__(("127.0.0.1", 0), Handler)
        self.lock = threading.Lock()
        self.requests = 0
        self.alerts = []
        self.context = ssl.SSLContext(ssl.PROTOCOL_TLS_SERVER)
        self.context.load_cert_chain(directory / (label + "-server.pem"), directory / (label + "-server.key"))
        self.thread = threading.Thread(target=self.serve_forever, daemon=True)
        self.thread.start()

    def get_request(self):
        connection, address = super().get_request()
        connection.settimeout(5)
        try:
            return self.context.wrap_socket(connection, server_side=True), address
        except ssl.SSLError as error:
            with self.lock:
                self.alerts.append(error.reason)
            connection.close()
            raise

    def snapshot(self):
        with self.lock:
            return {"http_requests": self.requests, "tls_alerts": list(self.alerts)}

    def close(self):
        self.shutdown()
        self.server_close()
        self.thread.join(timeout=5)


def prepare_store(directory):
    library = ctypes.CDLL("libcrypto.so.3")
    library.BIO_new_mem_buf.argtypes = [ctypes.c_void_p, ctypes.c_int]
    library.BIO_new_mem_buf.restype = ctypes.c_void_p
    library.PEM_read_bio_X509.argtypes = [ctypes.c_void_p] * 4
    library.PEM_read_bio_X509.restype = ctypes.c_void_p
    library.X509_subject_name_hash.argtypes = [ctypes.c_void_p]
    library.X509_subject_name_hash.restype = ctypes.c_ulong
    library.X509_free.argtypes = [ctypes.c_void_p]
    library.BIO_free.argtypes = [ctypes.c_void_p]
    root = Path("/ca-store")
    for label in ("good", "bad"):
        source = directory / (label + "-ca.pem")
        data = source.read_bytes()
        target = root / "usr/local/share/ca-certificates" / ("qt-fixture-" + label + ".crt")
        shutil.copyfile(source, target)
        with (root / "etc/ssl/certs/ca-certificates.crt").open("ab") as bundle:
            bundle.write(b"\n" + data)
        buffer = ctypes.create_string_buffer(data)
        bio = library.BIO_new_mem_buf(buffer, len(data))
        certificate = library.PEM_read_bio_X509(bio, None, None, None)
        if not certificate:
            library.BIO_free(bio)
            raise ValueError("Synthetic public root cannot be parsed")
        try:
            subject_hash = library.X509_subject_name_hash(certificate)
        finally:
            library.X509_free(certificate)
            library.BIO_free(bio)
        alias_target = "/" + target.relative_to(root).as_posix()
        (root / "etc/ssl/certs" / ("qt-fixture-" + label + ".pem")).symlink_to(alias_target)
        index = 0
        while ((root / "etc/ssl/certs" / f"{subject_hash:08x}.{index}").exists() or
               (root / "etc/ssl/certs" / f"{subject_hash:08x}.{index}").is_symlink()):
            index += 1
        (root / "etc/ssl/certs" / f"{subject_hash:08x}.{index}").symlink_to(alias_target)


def probe(directory, mode):
    base = "http://127.0.0.1:8080"
    opener = urllib.request.build_opener(urllib.request.ProxyHandler({}))

    def api(path, values=None):
        request = urllib.request.Request(
            base + "/api/v2/" + path,
            data=None if values is None else urllib.parse.urlencode(values).encode(),
            headers={"Origin": base, "Referer": base + "/"},
        )
        with opener.open(request, timeout=20) as response:
            if response.status != 200 and not (path == "torrents/add" and response.status == 202):
                raise ValueError(f"Disposable API request not accepted: {path}, HTTP {response.status}")
            return response.read()

    ready = False
    for _ in range(60):
        try:
            if api("app/version").decode().strip() != "v5.2.4":
                raise ValueError("Unexpected immutable vendor binary")
            ready = True
            break
        except (OSError, urllib.error.URLError):
            time.sleep(1)
    if not ready:
        raise RuntimeError("Disposable loopback API did not become ready; no application logs dumped")
    preferences = json.loads(api("app/preferences"))
    if preferences.get("ignore_ssl_errors") is not False:
        raise ValueError("Actual daemon would ignore SSL errors")
    build = json.loads(api("app/buildInfo"))
    endpoints = {label: Endpoint(directory, label) for label in ("good", "bad")}
    try:
        urls = {label: f"https://127.0.0.1:{endpoint.server_port}/{label}.torrent"
                for label, endpoint in endpoints.items()}
        native_errors = []
        trust_messages = {
            "The root certificate of the certificate chain is self-signed, and untrusted":
                "SelfSignedCertificateInChain",
            "The issuer certificate of a locally looked up certificate could not be found":
                "UnableToGetLocalIssuerCertificate",
            "The issuer certificate could not be found": "UnableToGetIssuerCertificate",
        }
        for label, endpoint in endpoints.items():
            api("torrents/add", {
                "urls": urls[label],
                "stopped": "true", "savepath": "/config/downloads",
            })
        deadline = time.monotonic() + 20
        while time.monotonic() < deadline:
            if mode == "filtered":
                entries = json.loads(api("log/main?normal=false&info=false&warning=true&critical=false"))
                prefix = 'SSL error, URL: "' + urls["bad"] + '", errors: "'
                for entry in entries:
                    message = entry.get("message", "")
                    if message.startswith(prefix) and message.endswith('"'):
                        errors = message[len(prefix):-1].split(". ")
                        if all(error in trust_messages for error in errors):
                            native_errors = [trust_messages[error] for error in errors]
            good, bad = (endpoints[label].snapshot() for label in ("good", "bad"))
            if good["http_requests"] and (bad["http_requests"] or native_errors):
                break
            time.sleep(0.1)
        time.sleep(1)
        result = {label: endpoint.snapshot() for label, endpoint in endpoints.items()}
        assert result["good"]["http_requests"] > 0 and not result["good"]["tls_alerts"], result
        if mode == "factory-plus-fixtures":
            assert result["bad"]["http_requests"] > 0 and not result["bad"]["tls_alerts"], result
        elif mode == "filtered":
            assert result["bad"]["http_requests"] == 0, result
            assert native_errors, result
        else:
            raise ValueError("Unknown fixture mode")
        return {
            "mode": mode, "native_version": "v5.2.4",
            "build_info": {key: build[key] for key in ("qt", "openssl", "libtorrent", "bitness") if key in build},
            "ignore_ssl_errors": False, "endpoints": result, "native_trust_errors": native_errors,
        }
    finally:
        for endpoint in endpoints.values():
            endpoint.close()


if __name__ == "__main__":
    if sys.argv[1] == "prepare":
        prepare_store(Path(sys.argv[2]))
    else:
        print(json.dumps(probe(Path(sys.argv[2]), sys.argv[1])))
