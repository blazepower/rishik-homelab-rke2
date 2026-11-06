"""Disposable loopback TLS endpoints and public CA preparation; never real application data."""

import ctypes
import hashlib
import http.server
import importlib.util
import json
from pathlib import Path
import re
import shutil
import ssl
import sys
import threading
import time
import urllib.error
import urllib.parse
import urllib.request


class Endpoint(http.server.ThreadingHTTPServer):
    def __init__(self, directory, label, responses=None):
        class Handler(http.server.BaseHTTPRequestHandler):
            def do_GET(handler):
                with handler.server.lock:
                    handler.server.requests += 1
                if responses is None:
                    status, content_type = 200, "application/x-bittorrent"
                    body = b"Isolated TLS fixture, deliberately not a torrent.\n"
                else:
                    response = responses.get(urllib.parse.urlsplit(handler.path).path)
                    status = 200 if response else 404
                    content_type, body = response or ("text/plain", b"")
                handler.send_response(status)
                handler.send_header("Content-Type", content_type)
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


def filter_fixture(directory):
    spec = importlib.util.spec_from_file_location("public_guard", "/ca-guard/ca-overlay.py")
    guard = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(guard)
    policy = guard.policy_keys(Path("/ca-guard/blocked-spkis.sha256"))
    synthetic = (directory / "synthetic-spki.sha256").read_text().strip()
    if not re.fullmatch("[0-9A-F]{64}", synthetic) or synthetic in policy:
        raise ValueError("Expected one separate synthetic-only key")
    return guard.harden(Path("/ca-store"), "qbittorrent", policy | {synthetic})


def probe(directory, mode, context="url-download"):
    if context not in ("url-download", "rss", "tracker"):
        raise ValueError("Unknown native context")
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
    if any(preferences.get(key) is not False for key in ("dht", "lsd", "pex")):
        raise ValueError("Disposable fixture must not discover peers")
    if context == "rss":
        api("app/setPreferences", {"json": json.dumps({
            "rss_processing_enabled": True, "rss_auto_downloading_enabled": False, "rss_fetch_delay": 0,
        })})
        preferences = json.loads(api("app/preferences"))
        assert preferences["rss_processing_enabled"] is True
        assert preferences["rss_auto_downloading_enabled"] is False
    elif context == "tracker":
        if preferences.get("validate_https_tracker_certificate") is not True:
            raise ValueError("Actual libtorrent tracker verification must be enabled")
        # Only the unpublished network-none fixture needs to reach a loopback tracker.
        api("app/setPreferences", {"json": json.dumps({"ssrf_mitigation": False})})
        preferences = json.loads(api("app/preferences"))
        assert preferences["ssrf_mitigation"] is False
    build = json.loads(api("app/buildInfo"))
    responses = {}
    for label in ("good", "bad"):
        if context == "rss":
            body = ('<?xml version="1.0"?><rss version="2.0"><channel>'
                    f'<title>Synthetic RSS {label}</title><link>https://127.0.0.1/</link>'
                    '<description>Empty synthetic TLS fixture</description></channel></rss>').encode()
            responses[label] = {f"/{label}.xml": ("application/rss+xml", body)}
        elif context == "tracker":
            responses[label] = {
                "/announce": ("text/plain", b"d8:intervali3600e12:min intervali3600e5:peers0:e"),
            }
    endpoints = {label: Endpoint(directory, label, responses.get(label)) for label in ("good", "bad")}
    try:
        suffixes = {"url-download": "{label}.torrent", "rss": "{label}.xml", "tracker": "announce"}
        urls = {label: f"https://127.0.0.1:{endpoint.server_port}/" + suffixes[context].format(label=label)
                for label, endpoint in endpoints.items()}
        native_errors = []
        application_ready = False
        state = {}
        infohashes = {}
        trust_messages = {
            "The root certificate of the certificate chain is self-signed, and untrusted":
                "SelfSignedCertificateInChain",
            "The issuer certificate of a locally looked up certificate could not be found":
                "UnableToGetLocalIssuerCertificate",
            "The issuer certificate could not be found": "UnableToGetIssuerCertificate",
        }
        for label, endpoint in endpoints.items():
            if context == "rss":
                api("rss/addFeed", {"url": urls[label], "path": "fixture-" + label})
            else:
                url = urls[label]
                if context == "tracker":
                    infohashes[label] = hashlib.sha1(("isolated-tls-tracker-" + label).encode()).hexdigest()
                    url = "magnet:?" + urllib.parse.urlencode({
                        "xt": "urn:btih:" + infohashes[label], "dn": "synthetic-" + label, "tr": url,
                    })
                api("torrents/add", {
                    "urls": url, "stopped": "false" if context == "tracker" else "true",
                    "savepath": "/config/downloads",
                })
        deadline = time.monotonic() + 30
        while time.monotonic() < deadline:
            if mode == "filtered" and context != "tracker":
                entries = json.loads(api("log/main?normal=false&info=false&warning=true&critical=false"))
                prefix = 'SSL error, URL: "' + urls["bad"] + '", errors: "'
                for entry in entries:
                    message = entry.get("message", "")
                    if message.startswith(prefix) and message.endswith('"'):
                        errors = message[len(prefix):-1].split(". ")
                        if all(error in trust_messages for error in errors):
                            native_errors = [trust_messages[error] for error in errors]
            if context == "rss":
                feeds = json.loads(api("rss/items?withData=true"))
                state = {
                    label: {
                        "parsed_empty_feed": feeds.get("fixture-" + label, {}).get("title") == f"Synthetic RSS {label}",
                        "has_error": feeds.get("fixture-" + label, {}).get("hasError"),
                    } for label in endpoints
                }
                application_ready = (
                    state["good"]["parsed_empty_feed"] and state["good"]["has_error"] is False and
                    (state["bad"]["parsed_empty_feed"] and state["bad"]["has_error"] is False
                     if mode == "factory-plus-fixtures" else state["bad"]["has_error"] is True)
                )
            elif context == "tracker":
                state = {}
                for label in endpoints:
                    try:
                        trackers = json.loads(api("torrents/trackers?hash=" + infohashes[label]))
                    except urllib.error.HTTPError as error:
                        if error.code == 404:
                            continue
                        raise
                    tracker = next((row for row in trackers if row["url"] == urls[label]), {})
                    state[label] = {
                        "status": tracker.get("status"),
                        "certificate_verify_failed": "certificate verify failed" in tracker.get("msg", "").lower(),
                    }
                if (state.get("bad", {}).get("certificate_verify_failed") and
                        "TLSV1_ALERT_UNKNOWN_CA" in endpoints["bad"].snapshot()["tls_alerts"]):
                    native_errors = ["LibtorrentCertificateVerifyFailedWithUnknownCA"]
                application_ready = (
                    state.get("good", {}).get("status") == 2 and
                    (state.get("bad", {}).get("status") == 2 if mode == "factory-plus-fixtures"
                     else state.get("bad", {}).get("status") == 4 and bool(native_errors))
                )
            else:
                application_ready = True
            good, bad = (endpoints[label].snapshot() for label in ("good", "bad"))
            if application_ready and good["http_requests"] and (bad["http_requests"] or native_errors):
                break
            time.sleep(0.1)
        time.sleep(1)
        result = {label: endpoint.snapshot() for label, endpoint in endpoints.items()}
        assert application_ready, {"context": context, "application_state": state, "endpoints": result}
        assert result["good"]["http_requests"] > 0 and not result["good"]["tls_alerts"], result
        if mode == "factory-plus-fixtures":
            assert result["bad"]["http_requests"] > 0 and not result["bad"]["tls_alerts"], result
        elif mode == "filtered":
            assert result["bad"]["http_requests"] == 0, result
            assert native_errors, result
        else:
            raise ValueError("Unknown fixture mode")
        return {
            "mode": mode, "context": context, "native_version": "v5.2.4",
            "build_info": {key: build[key] for key in ("qt", "openssl", "libtorrent", "bitness") if key in build},
            "ignore_ssl_errors": False, "endpoints": result, "native_trust_errors": native_errors,
            "application_state": state,
            "tracker_certificate_verification": preferences.get("validate_https_tracker_certificate"),
            "ssrf_disabled_only_for_isolated_loopback_tracker": context == "tracker",
        }
    finally:
        for endpoint in endpoints.values():
            endpoint.close()


if __name__ == "__main__":
    if sys.argv[1] == "prepare":
        prepare_store(Path(sys.argv[2]))
    elif sys.argv[1] == "filter-fixture":
        print(json.dumps(filter_fixture(Path(sys.argv[2]))))
    else:
        print(json.dumps(probe(Path(sys.argv[2]), sys.argv[1],
                               sys.argv[3] if len(sys.argv) > 3 else "url-download")))
