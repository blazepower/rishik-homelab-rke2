"""Unprivileged disposable native acceptance; no production cluster or data."""
import argparse
import hashlib
import importlib.util
import json
import os
from pathlib import Path
import re
import shutil
import time

ROOT = Path(__file__).resolve().parents[2]
BUILD = ROOT / "apps" / "_shared" / "npd-owned"
spec = importlib.util.spec_from_file_location("maintained", Path(__file__).with_name("verify-maintained-go.py"))
shared = importlib.util.module_from_spec(spec)
spec.loader.exec_module(shared)
build_spec = importlib.util.spec_from_file_location(
    "maintained_build", ROOT / "apps" / "_shared" / "maintained-go" / "build.py")
scanner = importlib.util.module_from_spec(build_spec)
build_spec.loader.exec_module(scanner)
docker = shared.docker
run = shared.run


def binary_proof(image, lock, scratch, vendor=False):
    container = docker("create", image).stdout.strip()
    try:
        package_status = scratch / "package-status"
        docker("cp", container + ":/var/lib/dpkg/status", str(package_status))
        assert hashlib.sha256(package_status.read_bytes()).hexdigest() == \
            lock["runtime_package_status_sha256"], "Inherited runtime package metadata changed"
        for name, sha in lock["monitor_config_sha256"].items():
            path = scratch / name
            docker("cp", container + ":/config/" + name, str(path))
            assert hashlib.sha256(path.read_bytes()).hexdigest() == sha, (name, "Monitor configuration changed")
        source = None
        if not vendor:
            docker("cp", container + ":/usr/share/npd-owned/source.json", str(scratch / "source.json"))
            source = json.loads((scratch / "source.json").read_text())
            assert {k: source[k] for k in lock} == lock
        results = {}
        for name, binary in lock["binaries"].items():
            path = scratch / name
            docker("cp", container + ":" + binary["path"], str(path))
            sha = hashlib.sha256(path.read_bytes()).hexdigest()
            if vendor:
                assert sha == binary["vendor_sha256"], name
                results[name] = {"sha256": sha}
                continue
            evidence = source["actual_binaries"][name]
            assert sha == evidence["sha256"]
            assert evidence["symbol_table_verified"] is True
            info = docker("run", "--rm", "--network", "none", "--read-only",
                          "--cap-drop=ALL", "--security-opt=no-new-privileges", "--mount",
                          f"type=bind,src={scratch},dst=/proof,readonly", lock["builder"],
                          "go", "version", "-m", "/proof/" + name).stdout
            compiler = re.search(r"^/proof/" + re.escape(name) + r": (go\S+)$", info, re.MULTILINE)
            assert compiler and compiler[1] == lock["compiler"], (name, info)
            assert "CGO_ENABLED=1" in info and "-tags=journald" in info
            for mode in ("source", "binary"):
                filename = name + "-" + mode + ".jsonl"
                docker("cp", container + ":/usr/share/npd-owned/" + filename, str(scratch / filename))
                summary = scanner.advisory_summary(
                    (scratch / filename).read_text(), mode, lock["govulncheck"])
                assert summary == evidence["advisory_scans"][mode]
            results[name] = {**evidence, "actual_elf_buildinfo": info}
        return results
    finally:
        docker("rm", container)


def native(image, scratch, suffix):
    fixture = scratch / ("fixture-" + suffix)
    fixture.mkdir()
    (fixture / "journal").mkdir()
    (fixture / "check.sh").write_text(
        'if [ -e /fixture/unhealthy ]; then echo "synthetic failure"; exit 1; fi\n'
        'echo "synthetic healthy"\nexit 0\n')
    (fixture / "crictl").write_text("#!/bin/sh\nexit 0\n")
    (fixture / "crictl").chmod(0o755)
    config = {
        "plugin": "custom", "pluginConfig": {"invoke_interval": "1s", "timeout": "1s",
                                            "max_output_length": 80, "concurrency": 1},
        "source": "synthetic", "metricsReporting": True,
        "conditions": [{"type": "SyntheticProblem", "reason": "SyntheticHealthy", "message": "healthy"}],
        "rules": [{"type": "permanent", "condition": "SyntheticProblem", "reason": "SyntheticFailed",
                   "path": "/bin/sh", "args": ["/fixture/check.sh"]},
                  {"type": "temporary", "reason": "SyntheticObserved",
                   "path": "/bin/sh", "args": ["/fixture/check.sh"]}],
    }
    (fixture / "monitor.json").write_text(json.dumps(config))
    name = "npd-native-" + suffix
    restrictions = ["--network", "none", "--read-only", "--cap-drop=ALL",
                    "--security-opt=no-new-privileges", "--pids-limit=64", "--memory=128m",
                    "--cpus=0.1", "--tmpfs", "/tmp:rw,noexec,nosuid,size=32m",
                    "--mount", f"type=bind,src={fixture},dst=/fixture,readonly"]

    def metric(expected):
        deadline = time.monotonic() + 40
        while time.monotonic() < deadline:
            result = docker(
                "exec", name, "/bin/bash", "-c",
                "exec 3<>/dev/tcp/127.0.0.1/20257; "
                "printf 'GET /metrics HTTP/1.0\\r\\nHost: localhost\\r\\n\\r\\n' >&3; cat <&3",
                check=False,
            )
            samples = [line for line in result.stdout.splitlines()
                       if line.startswith("problem_gauge{") and 'type="SyntheticProblem"' in line]
            values = [float(line.rsplit(" ", 1)[1]) for line in samples]
            if values and all(value in (0, 1) for value in values) and max(values) == expected:
                return "\n".join(samples)
            if docker("inspect", "-f", "{{.State.Running}}", name).stdout.strip() != "true":
                break
            time.sleep(1)
        raise RuntimeError("Synthetic condition transition failed:\n" + docker("logs", name).stdout[-8000:])

    try:
        version = docker("run", "--rm", *restrictions, "--entrypoint", "/node-problem-detector",
                         image, "--version").stdout.strip()
        assert "v1.35.4" in version
        docker("run", "-d", "--name", name, *restrictions, "--entrypoint", "/node-problem-detector",
               image, "--hostname-override=synthetic", "--enable-k8s-exporter=false",
               "--port=0", "--prometheus-address=127.0.0.1", "--prometheus-port=20257",
               "--kube-api-qps=5", "--kube-api-burst=10",
               "--config.custom-plugin-monitor=/fixture/monitor.json")
        transitions = [metric(0)]
        (fixture / "unhealthy").touch()
        transitions.append(metric(1))
        (fixture / "unhealthy").unlink()
        transitions.append(metric(0))
        log_args = ["--log-path=/fixture/journal", "--journald-source=kernel", "--lookback=1m", "--delay=0s",
                    "--pattern=synthetic", "--count=1"]
        log = docker("run", "--rm", *restrictions, "--entrypoint", "/home/kubernetes/bin/log-counter",
                     image, *log_args, check=False)
        assert log.returncode == 0, log.stdout
        invalid = docker("run", "--rm", *restrictions, "--entrypoint", "/home/kubernetes/bin/log-counter",
                         image, *log_args, "--pattern=[", check=False)
        assert invalid.returncode == 2 and "error parsing regexp" in invalid.stdout, invalid.stdout
        health_args = ["--component=cri", "--enable-repair=false", "--crictl-path=/fixture/crictl",
                       "--cri-socket-path=unix:///fixture/synthetic.sock", "--health-check-timeout=2s"]
        health = docker("run", "--rm", *restrictions, "--entrypoint", "/home/kubernetes/bin/health-checker",
                        image, *health_args, check=False)
        assert health.returncode == 0, health.stdout
        (fixture / "crictl").write_text("#!/bin/sh\nexit 1\n")
        unhealthy = docker("run", "--rm", *restrictions, "--entrypoint", "/home/kubernetes/bin/health-checker",
                           image, *health_args, check=False)
        assert unhealthy.returncode in (1, 2), unhealthy.stdout
        state = json.loads(docker("inspect", name).stdout)[0]["State"]
        assert state["Running"] and not state["OOMKilled"], state
        return {"version": version, "condition_gauge_values": [0, 1, 0],
                "memory_limit_bytes": 134217728, "cpu_limit_cores": 0.1, "oom_killed": False,
                "condition_samples": transitions, "empty_journal_exit": log.returncode,
                "invalid_journal_regex_exit": invalid.returncode, "synthetic_cri_healthy_exit": health.returncode,
                "synthetic_cri_failed_exit": unhealthy.returncode,
                "scope": "Disposable custom-plugin transitions, empty journal/library ABI, invalid regex and "
                         "synthetic CRI command. Repair disabled; no host PID, devices, journal, socket, "
                         "kubeconfig, cluster API, public TLS or private data. Not a real-journal/API proof."}
    finally:
        docker("rm", "-f", name, check=False)


def main():
    parser = argparse.ArgumentParser()
    parser.add_argument("--publish", action="store_true")
    args = parser.parse_args()
    lock = json.loads((BUILD / "source.json").read_text())
    scratch = ROOT / ".npd-owned"
    scratch.mkdir(exist_ok=False)
    report = {"head": run("git", "rev-parse", "HEAD").stdout.strip(),
              "workflow_head": os.environ.get("GITHUB_SHA"), "source": lock,
              "published": False, "publication_stage": "native_or_build_incomplete"}
    tag = "ghcr.io/blazepower/npd-owned:" + report["head"]
    output = ROOT / "npd-owned-evidence.json"
    try:
        run("git", "clone", "--quiet", "--depth=1", "--branch", lock["version"],
            lock["repository"], str(scratch / "upstream"))
        assert run("git", "-C", str(scratch / "upstream"), "rev-parse", "HEAD").stdout.strip() == lock["revision"]
        for filename in ("source.json", "build.py"):
            shutil.copyfile(BUILD / filename, scratch / filename)
        shutil.copyfile(ROOT / "apps" / "_shared" / "maintained-go" / "build.py",
                        scratch / "maintained_build.py")
        docker("pull", lock["vendor"])
        docker("pull", lock["builder"])
        original = json.loads(docker("image", "inspect", lock["vendor"]).stdout)[0]
        report["vendor_binaries"] = binary_proof(lock["vendor"], lock, scratch, vendor=True)
        docker("build", "--platform", "linux/amd64", "--progress", "plain",
               "--build-arg", "BUILDER=" + lock["builder"], "--build-arg", "VENDOR=" + lock["vendor"],
               "-f", str(BUILD / "Dockerfile"), "-t", tag, str(scratch))
        actual = json.loads(docker("image", "inspect", tag).stdout)[0]
        expected = json.loads(json.dumps(original["Config"]))
        expected["Labels"] = {
            **(expected.get("Labels") or {}),
            "org.opencontainers.image.source": "https://github.com/blazepower/rishik-homelab-rke2",
            "io.homelab.derivative": "Owned supported-minor NPD rebuild with Go 1.27.4; not an official Kubernetes image",
        }
        assert actual["Config"] == expected, "Undeclared execution configuration changed"
        inherited = original["RootFS"]["Layers"]
        assert actual["RootFS"]["Layers"][:len(inherited)] == inherited
        report["accepted_runtime_identity"] = shared.runtime_identity(actual)
        report["binaries"] = binary_proof(tag, lock, scratch)
        report["vendor_native"] = native(lock["vendor"], scratch, "vendor")
        report["native"] = native(tag, scratch, "owned")
        for key in ("version", "condition_gauge_values", "empty_journal_exit", "invalid_journal_regex_exit",
                    "synthetic_cri_healthy_exit", "synthetic_cri_failed_exit"):
            assert report["native"][key] == report["vendor_native"][key], key
        if args.publish:
            assert os.environ["GITHUB_EVENT_NAME"] == "workflow_dispatch"
            assert os.environ["GITHUB_REF"] == "refs/heads/master"
            assert report["head"] == os.environ["GITHUB_SHA"]
            report["publication_stage"] = "native_accepted_push_pending"
            pushed = docker("push", tag).stdout
            match = re.search(r"digest: (sha256:[0-9a-f]{64})", pushed)
            assert match, pushed
            parent = tag.split(":")[0] + "@" + match[1]
            manifest = json.loads(docker("buildx", "imagetools", "inspect", "--raw", parent).stdout)
            digest = shared.select_amd64_digest(manifest, match[1])
            immutable = tag.split(":")[0] + "@" + digest
            report.update(publication_stage="pushed_pull_verification_pending",
                          pushed_image=parent, selected_runtime_image=immutable)
            docker("pull", immutable)
            identity = shared.runtime_identity(json.loads(docker("image", "inspect", immutable).stdout)[0])
            assert identity == report["accepted_runtime_identity"]
            assert binary_proof(immutable, lock, scratch) == report["binaries"]
            report.update(published=True, image=immutable, publication_stage="accepted")
        else:
            report["publication_stage"] = "native_accepted_publication_not_requested"
        report["limitations"] = (
            "Supported-1.35 vendor source upgrade and all-three-program compiler patch, not a server upgrade. "
            "Inherited Debian libraries/configuration are unchanged; module-only matches and DB times are retained. "
            "No whole-rootfs, general API/journal, production, universal PKI or forensic clearance.")
        print(json.dumps(report, indent=2))
    finally:
        output.write_text(json.dumps(report, indent=2))
        shutil.rmtree(scratch)


if __name__ == "__main__":
    main()
