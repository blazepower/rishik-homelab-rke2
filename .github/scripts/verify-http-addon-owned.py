"""Verify three real images and isolated current-minor HTTP autoscaling."""
import argparse
from concurrent.futures import ThreadPoolExecutor
import hashlib
import importlib.util
import json
import os
from pathlib import Path
import re
import shutil
import socket
import subprocess
import tarfile
import tempfile
import time
import urllib.request
import uuid

ROOT = Path(__file__).resolve().parents[2]
BUILD = ROOT / "apps" / "_shared" / "http-addon-owned"
spec = importlib.util.spec_from_file_location("maintained", Path(__file__).with_name("verify-maintained-go.py"))
shared = importlib.util.module_from_spec(spec)
spec.loader.exec_module(shared)
spec = importlib.util.spec_from_file_location("maintained_build", ROOT / "apps/_shared/maintained-go/build.py")
scanner = importlib.util.module_from_spec(spec)
spec.loader.exec_module(scanner)
docker, run = shared.docker, shared.run
COMPONENTS = ("operator", "scaler", "interceptor")


def fetch(url, digest, path):
    with urllib.request.urlopen(url, timeout=120) as response:
        data = response.read()
    assert hashlib.sha256(data).hexdigest() == digest, (url, "Checksum differs")
    path.write_bytes(data)
    return path


def validate_buildinfo(info, lock):
    assert info.splitlines()[0] == "/proof/application: " + lock["compiler"]
    assert all(value in info for value in ("CGO_ENABLED=0", "GOOS=linux", "GOARCH=amd64"))
    for module, version in lock["required_modules"].items():
        assert re.search(r"^\s+dep\s+" + re.escape(module) + r"\s+" + re.escape(version) + r"\s",
                         info, re.MULTILINE), (module, "Actual dependency differs")


def binary_proof(image, name, lock, scratch):
    container = docker("create", image).stdout.strip()
    directory = scratch / ("binary-" + name + "-" + uuid.uuid4().hex)
    directory.mkdir()
    try:
        docker("cp", container + ":/ko-app/" + name, str(directory / "application"))
        docker("cp", container + ":/usr/share/http-addon-owned/source.json", str(directory / "source.json"))
        source = json.loads((directory / "source.json").read_text())
        assert {k: source[k] for k in lock} == lock
        sha = hashlib.sha256((directory / "application").read_bytes()).hexdigest()
        assert source["actual_binaries"][name]["sha256"] == sha
        assert source["actual_binaries"][name]["symbol_table_verified"] is True
        command = ("run", "--rm", "--network", "none", "--read-only", "--cap-drop=ALL",
                   "--security-opt=no-new-privileges", "--mount",
                   f"type=bind,src={directory},dst=/proof,readonly", lock["builder"])
        info = docker(*command, "go", "version", "-m", "/proof/application").stdout
        validate_buildinfo(info, lock)
        symbols = docker(*command, "go", "tool", "nm", "/proof/application").stdout
        assert re.search(r"\bT main\.main$", symbols, re.MULTILINE), "Actual ELF symbols missing"
        scans = {}
        for label, mode in (("all-source", "source"), (name + "-binary", "binary")):
            path = directory / (label + ".jsonl")
            docker("cp", container + ":/usr/share/http-addon-owned/" + path.name, str(path))
            scans[label] = scanner.advisory_summary(path.read_text(), mode, lock["govulncheck"])
            assert scans[label] == source["advisory_scans"][label]
        return {"sha256": sha, "actual_elf_buildinfo": info, "advisory_scans": scans,
                "actual_main_symbol_verified": True}
    finally:
        docker("rm", container)


def filesystem(image, archive):
    container = docker("create", image).stdout.strip()
    try:
        docker("export", "--output", str(archive), container)
    finally:
        docker("rm", container)
    records = {}
    with tarfile.open(archive) as tar:
        for member in tar:
            path = member.name.removeprefix("./").rstrip("/")
            if path in ("etc/hostname", "etc/hosts", "etc/resolv.conf"):
                continue
            checksum = hashlib.sha256(tar.extractfile(member).read()).hexdigest() if member.isfile() else None
            records[path] = (member.type.decode(), member.mode, member.uid, member.gid, member.linkname, checksum)
    return records


def compare_filesystems(before, after, name, vendor_sha):
    binary = "ko-app/" + name
    assert binary in before and binary in after, "Actual ELF missing"
    assert before[binary][-1] == vendor_sha, "Factory ELF changed"
    assert before[binary][:-1] == after[binary][:-1], "ELF ownership or mode changed"
    for path in before:
        assert path in after, (path, "Factory path removed")
        if path != binary:
            assert after[path] == before[path], (path, "Undeclared factory filesystem change")
    additions = after.keys() - before.keys()
    assert additions
    assert all(path in ("usr", "usr/share", "usr/share/http-addon-owned")
               or path.startswith("usr/share/http-addon-owned/") for path in additions)
    return {"unchanged_factory_paths": len(before) - 1, "new_provenance_paths": len(additions),
            "runtime_generated_mount_files_excluded": ["etc/hostname", "etc/hosts", "etc/resolv.conf"]}


def wait(check, description, seconds=180):
    deadline = time.monotonic() + seconds
    while time.monotonic() < deadline:
        value = check()
        if value:
            return value
        time.sleep(2)
    raise TimeoutError(description)


def backend(lock, name):
    labels = {"app": name}
    return [
        {"apiVersion": "apps/v1", "kind": "Deployment", "metadata": {"name": name}, "spec": {
            "replicas": 0, "selector": {"matchLabels": labels}, "template": {
                "metadata": {"labels": labels}, "spec": {"automountServiceAccountToken": False,
                    "containers": [{"name": "echo", "image": lock["native"]["backend"],
                        "args": ["-listen=:8080", "-text=native-fixture"],
                        "ports": [{"containerPort": 8080}],
                        "resources": {"requests": {"cpu": "5m", "memory": "8Mi"},
                                      "limits": {"cpu": "100m", "memory": "32Mi"}},
                        "securityContext": {"runAsNonRoot": True, "runAsUser": 1000,
                            "readOnlyRootFilesystem": True, "allowPrivilegeEscalation": False,
                            "capabilities": {"drop": ["ALL"]}, "seccompProfile": {"type": "RuntimeDefault"}}}]}}}},
        {"apiVersion": "v1", "kind": "Service", "metadata": {"name": name},
         "spec": {"selector": labels, "ports": [{"port": 8080, "targetPort": 8080}]}}
    ]


def native(images, lock, scratch, phase, evidence):
    assert os.environ.get("RUNNER_ENVIRONMENT") == "github-hosted", "Native fixtures require GitHub-hosted CI"
    directory = scratch / phase
    directory.mkdir()
    kind = scratch / "kind"
    cluster = "http-owned-" + uuid.uuid4().hex[:12]
    config = directory / "ephemeral-kubeconfig"
    prefix = (str(scratch / "kubectl"), "--kubeconfig", str(config), "--context", "kind-" + cluster)
    kube = lambda *args: run(*prefix, *args).stdout
    get = lambda kind: json.loads(kube("get", kind, "-n", "native", "-o", "json"))

    def apply(objects):
        path = directory / ("fixture-" + uuid.uuid4().hex + ".json")
        path.write_text(json.dumps({"apiVersion": "v1", "kind": "List", "items": objects}))
        kube("apply", "-n", "native", "-f", str(path))

    forward, log = None, None
    try:
        run(str(kind), "create", "cluster", "--name", cluster, "--image", lock["native"]["node"],
            "--kubeconfig", str(config), "--wait", "180s")
        nodes = json.loads(kube("get", "nodes", "-o", "json"))["items"]
        assert [n["metadata"]["name"] for n in nodes] == [cluster + "-control-plane"]
        versions = json.loads(kube("version", "-o", "json"))
        assert versions["clientVersion"]["gitVersion"] == "v1.36.6"
        assert versions["serverVersion"]["gitVersion"].startswith("v1.35.")
        evidence["actual_tool_versions"] = versions
        kube("create", "namespace", "native")
        core_values = directory / "core-values.json"
        core_values.write_text(json.dumps({"image": {
            name: {"tag": tag} for name, tag in lock["native"]["core_image_tags"].items()}}))
        run("helm", "install", "keda", str(scratch / "core.tgz"), "--kubeconfig", str(config),
            "--kube-context", "kind-" + cluster, "--namespace", "native", "-f", str(core_values),
            "--wait", "--timeout", "180s")
        if phase == "candidate":
            run(str(kind), "load", "docker-image", "--name", cluster, *images.values())
        resources = lambda cpu: {"requests": {"cpu": "10m", "memory": "20Mi"},
                                  "limits": {"cpu": cpu, "memory": "64Mi"}}
        policy = "Never" if phase == "candidate" else "Always"
        values = {"operator": {"resources": resources("100m"), "pullPolicy": policy}, "scaler": {
            "replicas": 1, "resources": resources("100m"), "pullPolicy": policy}, "interceptor": {
                "directPodRouting": False, "readinessTimeout": "180s", "requestTimeout": "180s",
                "replicas": {"min": 1, "max": 2}, "resources": resources("200m"), "pullPolicy": policy}}
        path = directory / "values.json"
        path.write_text(json.dumps(values))
        rendered = run("helm", "template", "keda-add-ons-http", str(scratch / "addon.tgz"),
                       "--namespace", "native", "--kube-version", "1.35.0",
                       "--include-crds", "-f", str(path)).stdout
        for name in COMPONENTS:
            original = "ghcr.io/kedacore/http-add-on-" + name + ":0.16.0"
            assert rendered.count('"' + original + '"') == 1, (name, "Exact chart image contract changed")
            rendered = rendered.replace('"' + original + '"', '"' + images[name] + '"')
        path = directory / "addon.yaml"
        path.write_text(rendered)
        kube("apply", "-f", str(path))
        kube("wait", "--for=condition=Established", "crd/interceptorroutes.http.keda.sh",
             "crd/httpscaledobjects.http.keda.sh", "--timeout=90s")
        kube("rollout", "status", "deployment", "-n", "native",
             "-l", "app.kubernetes.io/instance=keda-add-ons-http", "--timeout=180s")
        with socket.socket() as listener:
            listener.bind(("127.0.0.1", 0))
            port = listener.getsockname()[1]
        log = (directory / "port-forward.log").open("w")
        forward = subprocess.Popen([*prefix, "port-forward", "-n", "native", "--address", "127.0.0.1",
                                    "service/keda-add-ons-http-interceptor-proxy", str(port) + ":8080"],
                                   stdout=log, stderr=subprocess.STDOUT)

        def forwarding():
            assert forward.poll() is None, "Port forward exited"
            with socket.socket() as probe:
                probe.settimeout(1)
                return probe.connect_ex(("127.0.0.1", port)) == 0

        wait(forwarding, "Native proxy port forward unavailable", 30)
        evidence.update(state="fixtures_running", cases={},
                        native_only_pull_policy=policy, preloaded_candidate_images=phase == "candidate")
        for style in ("legacy", "direct"):
            name, host = "backend-" + style, style + ".native-fixture.invalid"
            objects = backend(lock, name)
            if style == "legacy":
                objects.append({"apiVersion": "http.keda.sh/v1alpha1", "kind": "HTTPScaledObject",
                    "metadata": {"name": name}, "spec": {"hosts": [host],
                        "scaleTargetRef": {"name": name, "service": name, "port": 8080},
                        "replicas": {"min": 0, "max": 1}, "scaledownPeriod": 5,
                        "scalingMetric": {"concurrency": {"targetValue": 1}}}})
            else:
                objects += [
                    {"apiVersion": "http.keda.sh/v1beta1", "kind": "InterceptorRoute",
                     "metadata": {"name": name}, "spec": {"rules": [{"hosts": [host],
                         "paths": [{"value": "/"}]}], "target": {"service": name, "port": 8080},
                         "scalingMetric": {"concurrency": {"targetValue": 1}}}},
                    {"apiVersion": "keda.sh/v1alpha1", "kind": "ScaledObject",
                     "metadata": {"name": name}, "spec": {"scaleTargetRef": {"name": name},
                         "minReplicaCount": 0, "maxReplicaCount": 1, "pollingInterval": 1, "cooldownPeriod": 5,
                         "triggers": [{"type": "external-push", "metadata": {
                             "interceptorRoute": name,
                             "scalerAddress": "keda-add-ons-http-external-scaler.native:9090"}}]}}
                ]
            apply(objects)
            wait(lambda: any(i["spec"]["scaleTargetRef"]["name"] == name and any(
                c["type"] == "Ready" and c["status"] == "True" for c in i.get("status", {}).get("conditions", []))
                for i in get("scaledobjects")["items"]), "Native ScaledObject not Ready")

            def scaled(expected):
                deployment = json.loads(kube("get", "deployment", name, "-n", "native", "-o", "json"))
                pods = json.loads(kube("get", "pods", "-n", "native", "-l", "app=" + name, "-o", "json"))
                state = {"desired": deployment["spec"]["replicas"],
                         "total": deployment.get("status", {}).get("replicas", 0),
                         "ready": deployment.get("status", {}).get("readyReplicas", 0),
                         "existing_pods": len(pods["items"])}
                return state if all(value == expected for value in state.values()) else None

            initial = wait(lambda: scaled(0), "Native workload did not start genuinely cold")

            def request():
                req = urllib.request.Request("http://127.0.0.1:" + str(port), headers={"Host": host})
                with urllib.request.urlopen(req, timeout=120) as response:
                    assert response.status == 200 and response.read().decode().strip() == "native-fixture"
                return True

            with ThreadPoolExecutor(max_workers=1) as pool:
                future = pool.submit(request)

                def ready():
                    if future.done():
                        future.result()
                    return scaled(1)

                active = wait(ready, "Real HTTP cold start did not scale to one")
                assert future.result(timeout=125)
            restored = wait(lambda: scaled(0), "Native workload failed to return genuinely to zero")
            evidence["cases"][style] = {"replica_transition": [0, 1, 0], "proxied_status": 200,
                "observed_states": {"initial": initial, "active": active, "restored": restored},
                "response_body": "native-fixture", "fallback_configured": False}
        pods = json.loads(kube("get", "pods", "-n", "native",
                              "-l", "app.kubernetes.io/instance=keda-add-ons-http", "-o", "json"))["items"]
        observed = set()
        for pod in pods:
            for container in pod["spec"]["containers"]:
                name, = [name for name, image in images.items() if image == container["image"]]
                assert container["resources"] == resources("200m" if name == "interceptor" else "100m")
                status, = [s for s in pod["status"]["containerStatuses"] if s["name"] == container["name"]]
                assert status["ready"] and status["restartCount"] == 0 and not status.get("lastState")
                observed.add(name)
        assert observed == set(COMPONENTS)
        evidence.update(state="native_accepted", exact_images=images, zero_restarts=True,
                        original_addon_limits_preserved=True, kubernetes_minor="1.35")
    finally:
        try:
            if forward:
                try:
                    forward.terminate()
                    forward.wait(timeout=15)
                finally:
                    log.close()
            elif log:
                log.close()
            if config.exists():
                result = run(*prefix, "--request-timeout=15s", "get", "pods", "-n", "native", "-o", "json",
                             check=False)
                evidence["final_pod_readback_exit"] = result.returncode
                if result.returncode == 0:
                    evidence["final_pods"] = json.loads(result.stdout)["items"]
                    evidence["addon_logs"] = {}
                    for pod in evidence["final_pods"]:
                        if pod["metadata"].get("labels", {}).get("app.kubernetes.io/instance") != "keda-add-ons-http":
                            continue
                        result = run(*prefix, "--request-timeout=15s", "logs", "-n", "native",
                                     pod["metadata"]["name"], "--all-containers=true", "--tail=100", check=False)
                        evidence["addon_logs"][pod["metadata"]["name"]] = {
                            "exit": result.returncode, "output": result.stdout[-24000:]}
                else:
                    evidence["final_pod_readback_error"] = result.stdout[-8000:]
        finally:
            run(str(kind), "delete", "cluster", "--name", cluster)


def main():
    parser = argparse.ArgumentParser()
    parser.add_argument("--publish", action="store_true")
    args = parser.parse_args()
    if args.publish:
        assert os.environ.get("GITHUB_REF") == "refs/heads/master"
        assert os.environ.get("GITHUB_EVENT_NAME") == "workflow_dispatch"
    lock = json.loads((BUILD / "source.json").read_text())
    assert set(lock["components"]) == set(COMPONENTS)
    report = {"head": run("git", "rev-parse", "HEAD").stdout.strip(), "state": "incomplete",
              "lock": lock, "components": {}, "native": {}, "published": False}
    output = ROOT / "http-addon-owned-evidence.json"
    try:
        with tempfile.TemporaryDirectory(prefix="http-addon-owned-") as directory:
            scratch = Path(directory)
            run("git", "clone", "--quiet", "--depth", "1", "--branch", lock["version"],
                lock["repository"], str(scratch / "upstream"))
            assert run("git", "-C", str(scratch / "upstream"), "rev-parse", "HEAD").stdout.strip() == lock["revision"]
            for name in ("source.json", "security.patch", "build.py"):
                shutil.copyfile(BUILD / name, scratch / name)
            shutil.copyfile(ROOT / "apps/_shared/maintained-go/build.py", scratch / "maintained_build.py")
            tags = {}
            for name in COMPONENTS:
                vendor = lock["components"][name]["vendor"]
                docker("pull", vendor)
                original = json.loads(docker("image", "inspect", vendor).stdout)[0]
                tag = "ghcr.io/blazepower/http-addon-owned-" + name + ":" + report["head"]
                docker("build", "--platform", "linux/amd64", "--progress", "plain",
                       "--build-arg", "BUILDER=" + lock["builder"], "--build-arg", "VENDOR=" + vendor,
                       "--build-arg", "COMPONENT=" + name, "-f", str(BUILD / "Dockerfile"),
                       "-t", tag, str(scratch))
                actual = json.loads(docker("image", "inspect", tag).stdout)[0]
                before, after = dict(original["Config"]), dict(actual["Config"])
                before.pop("Labels", None)
                after.pop("Labels", None)
                assert before == after, (name, "Factory execution configuration changed")
                inherited = original["RootFS"]["Layers"]
                assert actual["RootFS"]["Layers"][:len(inherited)] == inherited
                changes = compare_filesystems(filesystem(vendor, scratch / "before.tar"),
                    filesystem(tag, scratch / "after.tar"), name, lock["components"][name]["vendor_binary_sha256"])
                proof = binary_proof(tag, name, lock, scratch)
                report["components"][name] = {"binary": proof, "filesystem": changes,
                                              "accepted_identity": shared.runtime_identity(actual)}
                tags[name] = tag
            n = lock["native"]
            fetch(n["kind_url"], n["kind_sha256"], scratch / "kind").chmod(0o755)
            fetch(n["kubectl_url"], n["kubectl_sha256"], scratch / "kubectl").chmod(0o755)
            for name in ("core", "addon"):
                chart = n[name + "_chart"]
                fetch(chart["url"], chart["sha256"], scratch / (name + ".tgz"))
            for phase, images in (("vendor", {name: lock["components"][name]["vendor"] for name in COMPONENTS}),
                                  ("candidate", tags)):
                report["native"][phase] = {"state": "incomplete"}
                native(images, lock, scratch, phase, report["native"][phase])
            report["state"] = "native_accepted_publication_not_requested"
            if args.publish:
                report["state"] = "native_accepted_publication_incomplete"
                for name, tag in tags.items():
                    result = docker("push", tag).stdout
                    match = re.search(r"digest: (sha256:[0-9a-f]{64})", result)
                    assert match, "Push returned no digest"
                    repository = tag.rsplit(":", 1)[0]
                    published = repository + "@" + match[1]
                    manifest = json.loads(docker("buildx", "imagetools", "inspect", "--raw", published).stdout)
                    immutable = repository + "@" + shared.select_amd64_digest(manifest, match[1])
                    record = report["components"][name]
                    record.update(pushed_image=published, selected_runtime_image=immutable)
                    record["publication_stage"] = "pushed_anonymous_pull_verification_pending"
                    with tempfile.TemporaryDirectory(prefix="http-owned-anonymous-registry-") as anonymous:
                        run("docker", "--config", anonymous, "pull", immutable)
                    pulled = json.loads(docker("image", "inspect", immutable).stdout)[0]
                    assert shared.runtime_identity(pulled) == record["accepted_identity"]
                    assert binary_proof(immutable, name, lock, scratch) == record["binary"]
                    record["accepted_image"] = immutable
                    record["publication_stage"] = "accepted"
                report.update(state="immutable_publication_accepted", published=True)
            report["scope"] = ("Owned unchanged application source with declared dependency/compiler updates; "
                "actual symbolled ELFs, real DB scope, preserved factory paths/configuration and bounded "
                "Linux current-minor legacy/direct HTTP scaling only. Not production activation, "
                "Kubernetes 1.36/1.37, whole-fleet vulnerability or forensic clearance.")
    finally:
        output.write_text(json.dumps(report, indent=2))


if __name__ == "__main__":
    main()
