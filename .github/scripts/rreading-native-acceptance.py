import hashlib
import http.client
import http.server
import json
import os
import pathlib
import re
import ssl
import subprocess
import sys
import tempfile
import threading
import time


BINARY = pathlib.Path(sys.argv[1])
POSTGRES = sys.argv[2]
AUTH = "synthetic-source-vet-not-a-real-token"
AUTHOR = {"id": 300, "name": "Synthetic acceptance author", "slug": "synthetic"}
CONTRIBUTION = {"contribution": "author", "author": AUTHOR}
EDITION = {
    "id": 100, "title": "Synthetic acceptance edition", "book_id": 200,
    "language": {"code3": "eng"}, "publisher": {"name": "Synthetic"},
    "release_date": "2026-01-01", "edition_format": "ebook", "pages": 10,
    "contributions": [CONTRIBUTION],
}
WORK = {
    "id": 200, "title": "Synthetic acceptance work", "slug": "synthetic",
    "description": "Disposable fixture, not a real library record.",
    "release_date": "2026-01-01", "cached_tags": [{"tag": "Synthetic"}],
    "cached_image": "", "contributions": [CONTRIBUTION],
    "default_cover_edition": EDITION, "editions": [EDITION],
    "fallback": [{"id": 100}], "ratings_count": 1, "rating": 4,
}
DATA = {
    "editions_by_pk": dict(EDITION, book=WORK),
    "books_by_pk": WORK,
    "authors_by_pk": dict(AUTHOR, contributions=[dict(CONTRIBUTION, book=WORK)]),
}


def pg(*args):
    return subprocess.run(
        ["docker", "exec", POSTGRES, *args],
        check=True, capture_output=True, text=True,
    ).stdout.strip()


def sql(database, query):
    return pg("psql", "-X", "-U", "postgres", "-d", database,
              "-v", "ON_ERROR_STOP=1", "-At", "-c", query)


class Fixture(http.server.BaseHTTPRequestHandler):
    def log_message(self, *args):
        pass

    def do_POST(self):
        assert self.path == "/v1/graphql"
        assert self.headers.get("Authorization") == AUTH
        length = int(self.headers["Content-Length"])
        assert 0 < length < 100000
        query = json.loads(self.rfile.read(length))["query"]
        aliases = re.findall(
            r"\b(\w+)\s*:\s*(editions_by_pk|books_by_pk|authors_by_pk)\b",
            query,
        )
        assert aliases, "Native batch did not contain the expected API fields"
        body = json.dumps({"data": {alias: DATA[field] for alias, field in aliases}}).encode()
        self.server.requests += 1
        self.send_response(200)
        self.send_header("Content-Type", "application/json")
        self.send_header("Content-Length", str(len(body)))
        self.end_headers()
        self.wfile.write(body)


def openssl(*args):
    subprocess.run(["openssl", *map(str, args)], check=True,
                   stdout=subprocess.DEVNULL, stderr=subprocess.PIPE)


def certificate(directory, name, issuer=None, ca=False, constrained=False):
    key, csr, cert = (directory / (name + suffix) for suffix in (".key", ".csr", ".crt"))
    openssl("req", "-new", "-newkey", "ec", "-pkeyopt", "ec_paramgen_curve:P-256",
            "-nodes", "-subj", "/CN=" + name, "-keyout", key, "-out", csr)
    extensions = directory / (name + ".ext")
    lines = [
        "basicConstraints=critical,CA:" + ("TRUE,pathlen:2" if ca else "FALSE"),
        "keyUsage=critical," + ("keyCertSign,cRLSign" if ca else "digitalSignature"),
    ]
    if not ca:
        lines += ["extendedKeyUsage=serverAuth", "subjectAltName=DNS:localhost"]
    if constrained:
        lines += ["nameConstraints=critical,permitted;DNS:.allowed.invalid"]
    extensions.write_text("\n".join(lines) + "\n")
    signing = ["-signkey", key] if issuer is None else [
        "-CA", issuer[1], "-CAkey", issuer[0], "-CAcreateserial",
    ]
    openssl("x509", "-req", "-in", csr, "-days", "2", "-sha256",
            *signing, "-extfile", extensions, "-out", cert)
    return key, cert


def upstream(key, cert, issuer=None):
    chain = cert
    if issuer is not None:
        chain = cert.with_suffix(".chain")
        chain.write_bytes(cert.read_bytes() + issuer[1].read_bytes())
    server = http.server.ThreadingHTTPServer(("127.0.0.1", 0), Fixture)
    server.requests = 0
    context = ssl.SSLContext(ssl.PROTOCOL_TLS_SERVER)
    context.minimum_version = ssl.TLSVersion.TLSv1_2
    context.load_cert_chain(chain, key)
    server.socket = context.wrap_socket(server.socket, server_side=True)
    thread = threading.Thread(target=server.serve_forever, daemon=True)
    thread.start()
    return server


def request(port, path):
    connection = http.client.HTTPConnection("127.0.0.1", port, timeout=15)
    try:
        connection.request("GET", path)
        response = connection.getresponse()
        return response.status, response.getheader("Location"), response.read()
    finally:
        connection.close()


def start(directory, database, roots, upstream_port):
    env = os.environ.copy()
    env.update({
        "POSTGRES_HOST": "localhost", "POSTGRES_USER": "postgres",
        "POSTGRES_PASSWORD": "synthetic-source-vet", "POSTGRES_DATABASE": database,
        "HARDCOVER_AUTH": AUTH, "UPSTREAM": f"localhost:{upstream_port}",
        "PORT": "8789", "SSL_CERT_FILE": str(roots),
        "SSL_CERT_DIR": str(directory / "empty-ca-dir"),
        "NO_PROXY": "*", "HTTP_PROXY": "", "HTTPS_PROXY": "", "ALL_PROXY": "",
    })
    log = directory / (database + ".log")
    with log.open("wb") as output:
        process = subprocess.Popen([str(BINARY), "serve"], env=env,
                                   stdout=output, stderr=subprocess.STDOUT)
    try:
        for _ in range(60):
            assert process.poll() is None, log.read_text()
            try:
                if request(8789, "/")[0] == 200:
                    return process, log
            except OSError:
                pass
            time.sleep(0.1)
        raise AssertionError("Native HTTP startup timed out")
    except BaseException:
        stop(process)
        raise


def stop(process):
    process.terminate()
    try:
        process.wait(timeout=10)
    except subprocess.TimeoutExpired:
        process.kill()
        process.wait(timeout=5)


def main():
    results = {"elf_sha256": hashlib.sha256(BINARY.read_bytes()).hexdigest()}
    with tempfile.TemporaryDirectory(prefix="rreading-native-") as temp:
        directory = pathlib.Path(temp)
        (directory / "empty-ca-dir").mkdir()
        root = certificate(directory, "trusted-root", ca=True)
        other = certificate(directory, "untrusted-root", ca=True)
        intermediate = certificate(directory, "constrained", root, ca=True, constrained=True)
        cases = [
            ("native_valid", certificate(directory, "valid", root), None),
            ("native_unknown", certificate(directory, "unknown", other), other),
            ("native_constrained", certificate(directory, "denied", intermediate), intermediate),
        ]
        for database, leaf, issuer in cases:
            pg("createdb", "-U", "postgres", database)
            server = upstream(*leaf, issuer)
            process = None
            try:
                process, log = start(directory, database, root[1], server.server_port)
                status, location, _ = request(8789, "/book/100")
                if database == "native_valid":
                    assert status == 303 and location == "/author/300?edition=100", log.read_text()
                    status, _, body = request(8789, "/work/200")
                    assert status == 200, log.read_text()
                    assert json.loads(body)["Title"] == WORK["title"], body
                    assert server.requests > 0
                    counts = sql(database, "SELECT count(*),count(*) FILTER (WHERE substring(value FROM 1 FOR 3)=decode('1f8b08','hex')) FROM cache")
                    total, gzip = map(int, counts.split("|"))
                    assert total > 0 and gzip == total
                    pg("pg_dump", "-U", "postgres", "-d", database,
                       "-Fc", "-f", "/tmp/rreading-synthetic-cache.dump")
                    results["trusted_chain_metadata_and_gzip_cache"] = "passed"
                else:
                    assert status == 500 and location is None, (status, location)
                    assert server.requests == 0, "Rejected chain reached authenticated API"
                    assert "x509:" in log.read_text()
                    if database == "native_unknown":
                        assert "unknown authority" in log.read_text()
                    else:
                        assert "not permitted" in log.read_text()
                    results[database] = "rejected_before_authenticated_API"
            finally:
                if process is not None:
                    stop(process)
                server.shutdown()
                server.server_close()
        pg("createdb", "-U", "postgres", "native_restored")
        pg("pg_restore", "-U", "postgres", "-d", "native_restored",
           "--exit-on-error", "/tmp/rreading-synthetic-cache.dump")
        process, log = start(directory, "native_restored", root[1], 65534)
        try:
            assert request(8789, "/book/100")[:2] == (303, "/author/300?edition=100"), log.read_text()
            status, _, body = request(8789, "/work/200")
            assert status == 200 and json.loads(body)["Title"] == WORK["title"], log.read_text()
            results["restored_persistent_cache_with_upstream_offline"] = "passed"
        finally:
            stop(process)
        pg("rm", "-f", "/tmp/rreading-synthetic-cache.dump")
    print(json.dumps(results, sort_keys=True))


if __name__ == "__main__":
    main()
