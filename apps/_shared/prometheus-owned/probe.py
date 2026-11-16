"""Synthetic loopback-only server/client inside a disposable network namespace."""
import argparse
import hashlib
import http.server
import json
from pathlib import Path
import ssl
import threading
import time
import urllib.error
import urllib.parse
import urllib.request

BASE = "http://127.0.0.1:9090"


def get(path, data=None):
    with urllib.request.urlopen(BASE + path, data=data, timeout=5) as response:
        return response.read()


def api(path):
    value = json.loads(get("/api/v1/" + path))
    assert value["status"] == "success", value
    return value["data"]


def query(expression):
    result = api("query?" + urllib.parse.urlencode({"query": expression}))
    assert result["resultType"] == "vector", result
    return result["result"]


def wait(description, check):
    deadline = time.monotonic() + 90
    last = None
    while time.monotonic() < deadline:
        try:
            last = check()
            if last:
                return last
        except (urllib.error.URLError, TimeoutError) as error:
            last = str(error)
        time.sleep(1)
    raise TimeoutError((description, last))


def main():
    parser = argparse.ArgumentParser()
    parser.add_argument("--phase", required=True)
    parser.add_argument("--value", type=int, required=True)
    parser.add_argument("--previous", type=int)
    parser.add_argument("--compiler", required=True)
    parser.add_argument("--revision", required=True)
    args = parser.parse_args()
    wait("Native Prometheus readiness", lambda: get("/-/ready"))
    info = api("status/buildinfo")
    assert info["version"] == "3.16.0" and info["revision"] == args.revision, info
    assert info["goVersion"] == args.compiler, info
    history = 'max_over_time(homelab_owned_probe{job="plain"}[10m])'
    before = query(history)
    if args.previous is None:
        assert not before, ("Synthetic volume was not empty", before)
    else:
        assert len(before) == 1 and float(before[0]["value"][1]) == args.previous, (
            "Prior native TSDB/WAL data did not reopen", before)

    class Metrics(http.server.BaseHTTPRequestHandler):
        def do_GET(self):
            if self.path != "/metrics":
                self.send_error(404)
                return
            payload = ('# TYPE homelab_owned_probe gauge\n'
                       'homelab_owned_probe{scope="synthetic"} ' + str(args.value) + '\n').encode()
            self.send_response(200)
            self.send_header("Content-Type", "text/plain; version=0.0.4")
            self.send_header("Content-Length", str(len(payload)))
            self.end_headers()
            self.wfile.write(payload)

        def log_message(self, *_):
            pass

    servers = []
    try:
        for port, tls in ((18080, True), (18081, False)):
            server = http.server.ThreadingHTTPServer(("127.0.0.1", port), Metrics)
            if tls:
                context = ssl.SSLContext(ssl.PROTOCOL_TLS_SERVER)
                context.load_cert_chain("/fixture/server.crt", "/fixture/server.key")
                server.socket = context.wrap_socket(server.socket, server_side=True)
            servers.append(server)
            threading.Thread(target=server.serve_forever, daemon=True).start()

        def scraped():
            values = {row["metric"]["job"]: float(row["value"][1]) for row in query("up")}
            return values if values == {"plain": 1, "trusted": 1, "untrusted": 0, "wrong-host": 0} else None

        observed = wait("Verified positive/negative native TLS scrapes", scraped)

        def written():
            values = query('homelab_owned_probe{job="plain"}')
            count = query('count_over_time(homelab_owned_probe{job="plain"}[10m])')
            return values if (len(values) == len(count) == 1
                              and float(values[0]["value"][1]) == args.value
                              and float(count[0]["value"][1]) >= 2) else None

        wait("Actual native TSDB writes", written)
        targets = {target["labels"]["job"]: target for target in api("targets")["activeTargets"]}
        assert "unknown authority" in targets["untrusted"]["lastError"], targets["untrusted"]["lastError"]
        assert "not-localhost.invalid" in targets["wrong-host"]["lastError"], targets["wrong-host"]["lastError"]
        assert not targets["trusted"]["lastError"] and not targets["plain"]["lastError"]
        files = json.loads(Path("/fixture/ui-files.json").read_text())
        checked = {}
        for name, checksum in files.items():
            prefix = "static/mantine-ui/"
            if not name.startswith(prefix) or name == prefix + "index.html":
                continue
            path = "/" + name.removeprefix(prefix)
            actual = hashlib.sha256(get(path)).hexdigest()
            assert actual == checksum, (path, "Native UI bytes differ from locked official release")
            checked[path] = actual
        assert checked and any(path.endswith(".js") for path in checked), "No native UI application checked"
        index = get("/query")
        assert b"<html" in index and b"<script" in index, "Native UI did not render"
        config_path = Path("/fixture/prometheus.json")
        config = json.loads(config_path.read_text())
        config["global"]["external_labels"] = {"native_reload": args.phase + "-reloaded"}
        temporary = config_path.with_suffix(".tmp")
        temporary.write_text(json.dumps(config))
        temporary.chmod(0o644)
        temporary.replace(config_path)
        get("/-/reload", data=b"")
        assert args.phase + "-reloaded" in api("status/config")["yaml"], "Changed native configuration not reloaded"
        print(json.dumps({
            "phase": args.phase, "api_buildinfo": info, "prior_disk_max": before,
            "written_value": args.value, "up": observed,
            "negative_tls_errors": {name: targets[name]["lastError"] for name in ("untrusted", "wrong-host")},
            "ui_assets": checked, "rendered_index_sha256": hashlib.sha256(index).hexdigest(),
            "changed_config_reloaded": True,
        }))
    finally:
        for server in servers:
            server.shutdown()
            server.server_close()


if __name__ == "__main__":
    main()
