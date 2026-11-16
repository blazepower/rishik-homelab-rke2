#!/usr/bin/env python3
"""Isolated real Ubuntu APIs, systemd syntax and the production textfile parser."""
import argparse
import hashlib
import importlib.util
import json
import os
from pathlib import Path
import subprocess
import sys
import tempfile
import time

import yaml

ROOT = Path(__file__).resolve().parents[2]
SCRIPTS = ROOT / ".github/scripts"
EXPORTER = "quay.io/prometheus/node-exporter@sha256:8c9bac11973b94b59be88d6e11fee4429aa743c8846cdc75d65b18db33f6a106"
spec = importlib.util.spec_from_file_location("observer", SCRIPTS / "node-patch-observer.py")
observer = importlib.util.module_from_spec(spec)
spec.loader.exec_module(observer)


def run(command, **kwargs):
    result = subprocess.run(command, text=True, **kwargs)
    if result.returncode:
        for output in (result.stdout, result.stderr):
            if output:
                print(output, file=sys.stderr)
    result.check_returncode()
    return result


def image_identity(image):
    value, = json.loads(run(["docker", "image", "inspect", image], capture_output=True).stdout)
    observer.require(value["Os"] == "linux" and value["Architecture"] == "amd64", "Wrong native image platform")
    def canonical(subject):
        return subject.removeprefix("docker.io/").removeprefix("library/")
    observer.require(canonical(image) in {canonical(subject) for subject in value["RepoDigests"]},
                     "Pulled exact subject not retained: " + json.dumps(
                         {"expected": image, "actual_repo_digests": value["RepoDigests"]}))
    return {"subject": image, "engine_image_id": value["Id"], "repo_digests": value["RepoDigests"]}


def sample(text, key):
    matches = [line.rsplit(" ", 1)[1] for line in text.splitlines()
               if line and not line.startswith("#") and line.rsplit(" ", 1)[0] == key]
    observer.require(len(matches) == 1, "Expected exactly one native parsed sample: " + key)
    return float(matches[0])


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--docker", action="store_true", required=True)
    parser.add_argument("--evidence", type=Path, required=True)
    args = parser.parse_args()
    dockerfile = SCRIPTS / "node-patch-observer.Dockerfile"
    base = dockerfile.read_text().splitlines()[0].removeprefix("FROM ")
    sdk = "homelab-node-observer-test:" + os.environ.get("GITHUB_SHA", "held-source")[:12]
    for image in (base, EXPORTER):
        run(["docker", "pull", "--platform=linux/amd64", image])
    identities = [image_identity(image) for image in (base, EXPORTER)]
    run(["docker", "build", "--platform=linux/amd64", "--file", str(dockerfile), "--tag", sdk, str(SCRIPTS)])
    units = ROOT / "node-config/maintenance/observer"

    def sdk_python(code, network="none"):
        command = [
            "docker", "run", "--rm", "--network=" + network, "--read-only", "--cap-drop=ALL",
            "--security-opt=no-new-privileges", "--pids-limit=64", "--memory=256m", "--cpus=0.1",
            "--user=65534:65534", "--tmpfs", "/tmp:rw,noexec,nosuid,size=32m,mode=1777",
            "--mount", f"type=bind,source={SCRIPTS},target=/source,readonly",
            "--mount", f"type=bind,source={units},target=/units,readonly",
            "--entrypoint", "/usr/bin/python3", sdk, "-B", "-c", code,
        ]
        return run(command, capture_output=True).stdout

    control = json.loads(sdk_python("""
import apt_pkg,hashlib,json,pathlib,subprocess,resource,sys
subprocess.run(['/usr/bin/python3','-B','/source/test-node-patch-observer.py'],check=True)
result=subprocess.run(['/usr/bin/python3','-B','/source/node-patch-observer.py','--stdout'],
                      capture_output=True,text=True,timeout=85)
assert result.returncode==1,(result.returncode,result.stderr)
assert 'kernel:' in result.stderr and 'security:' not in result.stderr and 'reboot:' not in result.stderr,result.stderr
assert 'homelab_node_kernel_abi_pending{' not in result.stdout,result.stdout
assert 'check="kernel"' in result.stdout,result.stdout
assert apt_pkg.VERSION=='3.2.1',apt_pkg.VERSION
print(json.dumps({'apt_pkg_version':apt_pkg.VERSION,'metrics':result.stdout,
 'python':sys.version,'sdk_package_versions':subprocess.run(
 ['/usr/bin/dpkg-query','-W','apt','python3','python3-apt','systemd'],
 capture_output=True,text=True,check=True).stdout,
 'expected_partial_failure':result.stderr.strip(),'exit_code':result.returncode,
 'peak_child_rss_kib':resource.getrusage(resource.RUSAGE_CHILDREN).ru_maxrss,
 'script_sha256':hashlib.sha256(pathlib.Path('/source/node-patch-observer.py').read_bytes()).hexdigest()}))
"""))
    observer.require(control["script_sha256"] == hashlib.sha256(
        (SCRIPTS / "node-patch-observer.py").read_bytes()).hexdigest(), "SDK did not execute actual source")
    profile = json.loads(sdk_python("""
import json,subprocess,sys
results=[]
for command in [
 ['systemd-analyze','verify','--man=no','/units/homelab-node-observer.service','/units/homelab-node-observer.timer'],
 ['systemd-sysusers','--dry-run','/units/homelab-node-observer.sysusers']]:
 r=subprocess.run(command,capture_output=True,text=True)
 if r.returncode:print(r.stdout,r.stderr,file=sys.stderr)
 r.check_returncode()
 results.append({'command':command,'stdout':r.stdout,'stderr':r.stderr,'exit_code':r.returncode})
print(json.dumps(results))
"""))
    release = yaml.safe_load(
        (ROOT / "infrastructure/monitoring/helmrelease-kube-prometheus-stack.yaml").read_text())
    flag, = [value for value in release["spec"]["values"]["prometheus-node-exporter"]["extraArgs"]
             if value.startswith("--collector.textfile.directory=")]
    target = flag.split("=", 1)[1]
    observer.require(target == "/host/root/var/lib/homelab-node-observer", "Unexpected host textfile directory")
    phases = []
    with tempfile.TemporaryDirectory() as temporary:
        directory = Path(temporary)
        directory.chmod(0o755)
        path = directory / "node-maintenance.prom"
        observer.publish(path, control["metrics"])
        container = run([
            "docker", "run", "-d", "--network=none", "--read-only", "--cap-drop=ALL",
            "--security-opt=no-new-privileges", "--pids-limit=64", "--memory=128m", "--user=65534:65534",
            "--mount", f"type=bind,source={directory},target={target},readonly", EXPORTER,
            "--path.rootfs=/host/root", "--collector.disable-defaults", "--collector.textfile", flag,
            "--web.listen-address=127.0.0.1:9100",
        ], capture_output=True).stdout.strip()
        try:
            def scrape():
                return sdk_python(
                    "import urllib.request;print(urllib.request.urlopen('http://127.0.0.1:9100/metrics',timeout=5).read().decode(),end='')",
                    "container:" + container)

            first = scrape()
            observer.require(sample(first, "node_textfile_scrape_error") == 0, "Real SDK metrics did not parse")
            for check in ("reboot", "security"):
                observer.require(any('check="' + check + '"' in line and float(line.rsplit(" ", 1)[1]) == 1
                                     for line in first.splitlines() if line.startswith(
                                         "homelab_node_observer_check_success{")), "SDK real check was not exposed")
            phases.append({"phase": "actual Ubuntu nonroot API control", "native_textfile_error": 0})

            def failed_security():
                raise observer.ObservationError("Synthetic isolated stale security input")

            text, errors = observer.collect("fixture-node", time.time(), {
                "reboot": lambda: [("homelab_node_reboot_required", 1, {})],
                "kernel": lambda: [("homelab_node_kernel_abi_pending", 0, {})],
                "security": failed_security,
            })
            observer.require(len(errors) == 1, "Expected isolated partial failure")
            observer.publish(path, text)
            second = scrape()
            observer.require(sample(second, "node_textfile_scrape_error") == 0, "Failed-check exposition invalid")
            observer.require(sample(second, 'homelab_node_reboot_required{node="fixture-node"}') == 1,
                             "Independent known reboot lost")
            observer.require(sample(second, 'homelab_node_observer_check_success{check="security",node="fixture-node"}') == 0,
                             "Failed security check became healthy")
            observer.require(not any(line.startswith("homelab_node_apt_security_updates_available{")
                                     for line in second.splitlines()), "Old security value retained after failed check")
            phases.append({"phase": "atomic failure replaces old value and preserves independent positive",
                           "native_textfile_error": 0, "security_check": 0, "old_security_metric_absent": True})
            observer.publish(path, "not valid exposition\n")
            third = scrape()
            observer.require(sample(third, "node_textfile_scrape_error") == 1, "Actual parser failure was hidden")
            observer.require(not any(line.startswith("homelab_node_reboot_required{")
                                     for line in third.splitlines()), "Malformed file retained healthy observer data")
            phases.append({"phase": "malformed fixture is actual parser failure", "native_textfile_error": 1})
        finally:
            run(["docker", "rm", "-f", container], capture_output=True)
    evidence = {"source_sha": os.environ.get("GITHUB_SHA"), "images": identities, "ubuntu_control": control,
                "systemd_syntax_only": profile, "native_textfile_phases": phases, "production_changes": False,
                "limits": "Isolated public SDK/fixtures only; no real service provisioning, host sandbox activation, alert delivery or Grafana visual proof."}
    args.evidence.write_text(json.dumps(evidence, indent=2) + "\n")
    print(json.dumps(evidence, indent=2))


if __name__ == "__main__":
    main()
