"""Exercise the actual CLI against a disposable loopback API, never a cluster."""

import base64
import hashlib
import http.server
import json
from pathlib import Path
import re
import subprocess
import threading
import urllib.parse

ROOT = Path(__file__).resolve().parents[2]


def command(*args, check=True, **kwargs):
    result = subprocess.run(args, capture_output=True, encoding="utf-8", timeout=120, **kwargs)
    if check and result.returncode:
        raise RuntimeError(f"Synthetic CLI proof failed ({result.returncode}): {result.stdout[-4000:]} {result.stderr[-4000:]}")
    return result


def verify(image, app, scratch, reference=False):
    name = "reference" if reference else "owned"
    if reference:
        container = command("docker", "create", image).stdout.strip()
        try:
            path = scratch / "reference-kubectl.elf"
            command("docker", "cp", container + ":" + app["binary"], str(path))
            assert hashlib.sha256(path.read_bytes()).hexdigest() == app["reference_binary_sha256"]
        finally:
            command("docker", "rm", container)

    def cli(*args, network="none", check=True):
        return command(
            "docker", "run", "--rm", "--network", network, "--cap-drop", "ALL",
            "--security-opt", "no-new-privileges", "--read-only",
            "--tmpfs", "/tmp:rw,noexec,nosuid,size=32m", "-e", "KUBECONFIG=/dev/null",
            "--entrypoint", app["binary"], image, *args, check=check,
        )

    version = json.loads(cli("version", "--client", "-o", "json").stdout)["clientVersion"]
    assert version["gitVersion"] == (app["version"] if reference else app["client_git_version"])
    assert version["gitCommit"] == app["revision"] and version["platform"] == "linux/amd64"
    if not reference:
        assert version["goVersion"] == "go1.27.4" and version["gitTreeState"] == "dirty"
    state = {"exec_success": 0, "exec_failure": 0, "restart_patches": [], "unexpected_requests": []}
    pod = {"apiVersion": "v1", "kind": "Pod", "metadata": {"name": "synthetic", "namespace": "proof"},
           "spec": {"containers": [{"name": "app", "image": "synthetic.invalid/unused"}]},
           "status": {"phase": "Running"}}
    deployment = {"apiVersion": "apps/v1", "kind": "Deployment",
                  "metadata": {"name": "synthetic", "namespace": "proof"},
                  "spec": {"template": {"metadata": {"annotations": {}}, "spec": pod["spec"]}}}

    class Handler(http.server.BaseHTTPRequestHandler):
        protocol_version = "HTTP/1.1"

        def log_message(self, *_):
            pass

        def respond(self, value, status=200):
            body = json.dumps(value).encode()
            self.send_response(status)
            self.send_header("Content-Type", "application/json")
            self.send_header("Content-Length", str(len(body)))
            self.end_headers()
            self.wfile.write(body)

        def do_GET(self):
            parsed = urllib.parse.urlsplit(self.path)
            path = parsed.path.rstrip("/")
            if path == "/api":
                return self.respond({"kind": "APIVersions", "apiVersion": "v1", "versions": ["v1"]})
            if path == "/apis":
                return self.respond({"kind": "APIGroupList", "apiVersion": "v1", "groups": [{
                    "name": "apps", "versions": [{"groupVersion": "apps/v1", "version": "v1"}],
                    "preferredVersion": {"groupVersion": "apps/v1", "version": "v1"},
                }]})
            if path in ("/api/v1", "/apis/apps/v1"):
                core = path == "/api/v1"
                return self.respond({"kind": "APIResourceList", "apiVersion": "v1",
                    "groupVersion": "v1" if core else "apps/v1", "resources": [{
                        "name": "pods" if core else "deployments", "singularName": "pod" if core else "deployment",
                        "namespaced": True, "kind": "Pod" if core else "Deployment",
                        "verbs": ["get", "list", "patch"],
                    }]})
            if path == "/api/v1/namespaces/proof/pods":
                return self.respond({"kind": "PodList", "apiVersion": "v1", "metadata": {}, "items": [pod]})
            if path == "/api/v1/namespaces/proof/pods/synthetic":
                return self.respond(pod)
            if path == "/apis/apps/v1/namespaces/proof/deployments/synthetic":
                return self.respond(deployment)
            if path.endswith("/pods/synthetic/exec"):
                query = urllib.parse.parse_qs(parsed.query)
                if query.get("command") == ["false"]:
                    state["exec_failure"] += 1
                    return self.respond({"kind": "Status", "apiVersion": "v1", "status": "Failure",
                                         "reason": "BadRequest", "message": "synthetic exec rejected", "code": 400}, 400)
                assert query.get("command") == ["ls", "/media"] and query.get("container") == ["app"]
                protocols = self.headers.get("Sec-WebSocket-Protocol", "").split(",")
                assert "v5.channel.k8s.io" in {p.strip() for p in protocols}
                key = self.headers["Sec-WebSocket-Key"] + "258EAFA5-E914-47DA-95CA-C5AB0DC85B11"
                self.send_response(101)
                self.send_header("Upgrade", "websocket")
                self.send_header("Connection", "Upgrade")
                self.send_header("Sec-WebSocket-Accept", base64.b64encode(
                    hashlib.sha1(key.encode(), usedforsecurity=False).digest()).decode())
                self.send_header("Sec-WebSocket-Protocol", "v5.channel.k8s.io")
                self.end_headers()
                for payload in (b"\x01synthetic-directory\n", b'\x03{"kind":"Status","apiVersion":"v1","status":"Success"}'):
                    assert len(payload) < 126
                    self.wfile.write(bytes([0x82, len(payload)]) + payload)
                self.wfile.write(b"\x88\x02\x03\xe8")
                self.wfile.flush()
                self.close_connection = True
                state["exec_success"] += 1
                return
            state["unexpected_requests"].append({"method": "GET", "path": path})
            self.respond({"kind": "Status", "status": "Failure", "message": "unexpected proof request", "code": 404}, 404)

        def do_PATCH(self):
            assert urllib.parse.urlsplit(self.path).path == "/apis/apps/v1/namespaces/proof/deployments/synthetic"
            length = int(self.headers["Content-Length"])
            assert 0 < length < 65536
            patch = json.loads(self.rfile.read(length))
            assert patch["spec"]["template"]["metadata"]["annotations"]["kubectl.kubernetes.io/restartedAt"]
            state["restart_patches"].append(patch)
            self.respond(deployment)

    with http.server.ThreadingHTTPServer(("127.0.0.1", 0), Handler) as server:
        thread = threading.Thread(target=server.serve_forever)
        thread.start()
        try:
            flags = ("--server=http://127.0.0.1:" + str(server.server_port), "--cache-dir=/tmp/proof-cache", "--request-timeout=15s")
            result = cli(*flags, "get", "pods", "-n", "proof", "-o", "json", network="host")
            assert json.loads(result.stdout)["items"][0]["metadata"]["name"] == "synthetic"
            result = cli(*flags, "exec", "-n", "proof", "synthetic", "-c", "app", "--", "ls", "/media", network="host")
            assert result.stdout == "synthetic-directory\n"
            failed = cli(*flags, "exec", "-n", "proof", "synthetic", "-c", "app", "--", "false", network="host", check=False)
            assert failed.returncode != 0 and not failed.stdout, "Exec transport failure must not become successful empty output"
            cli(*flags, "rollout", "restart", "deployment/synthetic", "-n", "proof", network="host")
            assert state["exec_success"] == 1 and state["exec_failure"] >= 1
            assert len(state["restart_patches"]) == 1 and not state["unexpected_requests"]
        finally:
            server.shutdown()
            thread.join(timeout=10)
            assert not thread.is_alive()
    stores = None
    if not reference:
        container = command("docker", "create", image).stdout.strip()
        try:
            path = scratch / ("kubectl-" + name + "-ca-bundle.pem")
            command("docker", "cp", container + ":/etc/ssl/certs/ca-certificates.crt", str(path))
        finally:
            command("docker", "rm", container)
        certificates = re.findall(b"-----BEGIN CERTIFICATE-----.*?-----END CERTIFICATE-----", path.read_bytes(), re.S)
        denied = {r["sha256"].upper() for r in json.loads(
            (ROOT / ".github" / "security" / "emudhra-2026.json").read_text(encoding="utf-8"))["blocked_spkis"]}
        assert len(certificates) >= 10
        identities = set()
        for certificate in certificates:
            key = subprocess.run(["openssl", "x509", "-pubkey", "-noout"], input=certificate,
                                 capture_output=True, check=True, timeout=10).stdout
            der = subprocess.run(["openssl", "pkey", "-pubin", "-outform", "DER"], input=key,
                                 capture_output=True, check=True, timeout=10).stdout
            identities.add(hashlib.sha256(der).hexdigest().upper())
        assert not identities & denied, "Minimal runtime still trusts a blocked public SPKI"
        stores = {"certificates": len(certificates), "spki_identities": len(identities), "blocked_spkis_present": 0}
    return {"client_version": version, "actual_get_pod_list": True, "actual_websocket_exec": True,
            "exec_error_stays_nonzero_not_empty_success": True, "actual_rollout_restart_patch": True,
            "public_ca_bundle": stores,
            "scope": "Actual native CLI against host-loopback synthetic API/WebSocket; no production API, kubeconfig, service-account token, media or private records. Public-CA inventory is not universal TLS/hostile-input or whole-rootfs clearance."}
