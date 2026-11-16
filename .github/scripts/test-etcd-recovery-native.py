#!/usr/bin/env python3
"""Rehearse policy-pinned etcd recovery with public synthetic data, never RKE2."""

import argparse
import base64
import contextlib
import importlib.util
import json
import os
from pathlib import Path
import platform
import socket
import subprocess
import time
import urllib.request


ROOT = Path(__file__).resolve().parents[2]
SPEC = importlib.util.spec_from_file_location(
    "maintenance_host", ROOT / ".github/scripts/node-maintenance-host.py")
HOST = importlib.util.module_from_spec(SPEC)
SPEC.loader.exec_module(HOST)
PREFIX = "/homelab-public-synthetic/"
BUMP = 1_000_000_000
SOURCE_FILES = (
    ".github/scripts/test-etcd-recovery-native.py",
    ".github/scripts/node-maintenance-host.py",
    ".github/workflows/etcd-recovery-acceptance.yaml",
    "node-config/maintenance/policy.json",
    "docs/node-maintenance.md",
)


def require(condition, message):
    if not condition:
        raise RuntimeError(message)


def write_json(path, value):
    with path.open("x", encoding="utf-8") as output:
        json.dump(value, output, indent=2)
        output.write("\n")


def source():
    return {name: HOST.digest(ROOT / name) for name in SOURCE_FILES}


def pins():
    policy = json.loads((ROOT / "node-config/maintenance/policy.json").read_text())
    selected = {minor: policy["etcdutl"][minor] for minor in ("3.6", "3.7")}
    for minor, pin in selected.items():
        require(pin["version"].startswith("v" + minor + "."),
                "etcd tool pin does not match its selected minor")
    return selected


def prepare(tools, evidence):
    tools.mkdir()
    evidence.mkdir()
    records = {}
    for minor, pin in pins().items():
        version = pin["version"]
        directory = tools / minor
        directory.mkdir()
        archive = directory / "etcd.tar.gz"
        url = (f"https://github.com/etcd-io/etcd/releases/download/{version}/"
               f"etcd-{version}-linux-amd64.tar.gz")
        HOST.download(url, pin["sha256"], archive)
        binaries = {}
        for name in ("etcd", "etcdctl", "etcdutl"):
            target = directory / name
            HOST.extract_file(archive, f"etcd-{version}-linux-amd64/{name}", target)
            binaries[name] = HOST.digest(target)
        records[minor] = {**pin, "url": url, "binaries": binaries}
    manifest = {"source_sha": os.environ.get("GITHUB_SHA"), "source_files": source(),
                "tools": records}
    write_json(tools / "manifest.json", manifest)
    write_json(evidence / "preparation.json", manifest)


class Native:
    def __init__(self, tools, evidence):
        require(platform.system() == "Linux" and platform.machine() == "x86_64",
                "Native rehearsal requires isolated Linux amd64")
        require(os.environ.get("GITHUB_ACTIONS") == "true" and os.geteuid() != 0,
                "Run only as an unprivileged user in isolated hosted CI")
        require({name for _, name in socket.if_nameindex()} == {"lo"},
                "Native execution requires a private loopback-only network namespace")
        self.tools = tools
        self.evidence = evidence
        self.commands = evidence / "commands.jsonl"
        require(not self.commands.exists(), "Use a fresh native evidence directory")
        self.env = {"PATH": os.defpath, "LC_ALL": "C", "ETCDCTL_API": "3",
                    "HOME": str(evidence), "TMPDIR": str(evidence)}
        manifest = json.loads((tools / "manifest.json").read_text())
        require(manifest["source_files"] == source(), "Prepared source changed")
        require(manifest["source_sha"] == os.environ["GITHUB_SHA"], "Prepared commit changed")
        self.builds = {}
        for minor, pin in pins().items():
            record = manifest["tools"][minor]
            require(record["version"] == pin["version"] and record["sha256"] == pin["sha256"],
                    "Prepared policy pin changed")
            require(HOST.digest(tools / minor / "etcd.tar.gz") == pin["sha256"],
                    "Prepared release archive changed")
            self.builds[minor] = {}
            for name in ("etcd", "etcdctl", "etcdutl"):
                binary = self.binary(minor, name)
                require(HOST.digest(binary) == record["binaries"][name],
                        "Prepared native binary changed")
                version_args = ["--version"] if name == "etcd" else ["version"]
                output = self.command([binary, *version_args]).stdout
                label = "etcd Version: " if name == "etcd" else name + " version: "
                require(label + pin["version"].removeprefix("v") in output.splitlines(),
                        "Unexpected actual native binary version")
                self.builds[minor][name] = {
                    "sha256": HOST.digest(binary), "version_output": output}

    def binary(self, minor, name):
        return self.tools / minor / name

    def command(self, args, success=True):
        args = [str(arg) for arg in args]
        result = subprocess.run(args, capture_output=True, text=True,
                                timeout=30, env=self.env)
        with self.commands.open("a", encoding="utf-8") as output:
            output.write(json.dumps({"args": args, "returncode": result.returncode,
                                     "stdout": result.stdout, "stderr": result.stderr}) + "\n")
        if success:
            require(result.returncode == 0,
                    f"Native command failed: {args}; inspect commands.jsonl")
        return result

    def ctl(self, minor, server, *args, success=True):
        return self.command(
            [self.binary(minor, "etcdctl"), "--endpoints=" + server["client"],
             "--dial-timeout=2s", "--command-timeout=3s", "--write-out=json", *args],
            success=success)

    def server_spec(self, name, port):
        return {"name": name, "data": self.evidence / (name + "-data"),
                "client": f"http://127.0.0.1:{port}",
                "peer": f"http://127.0.0.1:{port + 1}"}

    @contextlib.contextmanager
    def server(self, minor, spec):
        args = [str(self.binary(minor, "etcd")), "--name=" + spec["name"],
                "--data-dir=" + str(spec["data"]),
                "--listen-client-urls=" + spec["client"],
                "--advertise-client-urls=" + spec["client"],
                "--listen-peer-urls=" + spec["peer"],
                "--initial-advertise-peer-urls=" + spec["peer"],
                "--initial-cluster=" + spec["name"] + "=" + spec["peer"],
                "--initial-cluster-token=public-synthetic-rehearsal",
                "--log-level=warn"]
        log_path = self.evidence / (spec["name"] + "-" + minor + ".log")
        with log_path.open("x") as log:
            process = subprocess.Popen(args, stdout=log, stderr=log, env=self.env)
            try:
                deadline = time.monotonic() + 40
                while True:
                    require(process.poll() is None, f"Native server exited; inspect {log_path.name}")
                    result = self.ctl(minor, spec, "endpoint", "health", success=False)
                    if result.returncode == 0:
                        health = json.loads(result.stdout)
                        require(len(health) == 1 and health[0]["health"] is True,
                                "Native endpoint did not report health")
                        break
                    require(time.monotonic() < deadline, "Native server did not become healthy")
                    time.sleep(0.2)
                yield spec
            finally:
                if process.poll() is None:
                    process.terminate()
                    try:
                        process.wait(timeout=15)
                    except subprocess.TimeoutExpired:
                        process.kill()
                        process.wait(timeout=5)
                        raise RuntimeError("Owned native server required forced cleanup")
                require(process.returncode == 0, f"Native server failed; inspect {log_path.name}")

    def state(self, minor, server, expected):
        result = json.loads(self.ctl(minor, server, "get", PREFIX, "--prefix").stdout)
        actual = {}
        for item in result.get("kvs", []):
            key = base64.b64decode(item["key"], validate=True).decode()
            require(key not in actual, "Duplicate synthetic key")
            actual[key] = base64.b64decode(item["value"], validate=True).decode()
        require(actual == expected, "Recovered full synthetic keyspace differs")
        return result["header"]

    def version(self, minor, server):
        opener = urllib.request.build_opener(urllib.request.ProxyHandler({}))
        deadline = time.monotonic() + 30
        while True:
            with opener.open(server["client"] + "/version", timeout=5) as response:
                result = json.load(response)
            require(result["etcdserver"] == pins()[minor]["version"].removeprefix("v"),
                    "Running server does not match approved native version")
            if result["etcdcluster"] == minor + ".0":
                return result
            require(time.monotonic() < deadline, "Single-member cluster minor did not advance")
            time.sleep(0.2)

    def snapshot(self, minor, server, name, expected):
        before = self.state(minor, server, expected)
        path = self.evidence / (name + ".db")
        self.ctl(minor, server, "snapshot", "save", path)
        checksum = HOST.validate_snapshot(path)
        status = json.loads(self.command(
            [self.binary(minor, "etcdutl"), "snapshot", "status", path,
             "--write-out=json"]).stdout)
        require(status["revision"] == before["revision"] and status["totalKey"] > 0,
                "Native snapshot revision/key count does not match source")
        return {"path": path.name, "sha256": checksum, "status": status, "header": before}

    def restore(self, minor, snapshot, spec):
        return self.command([
            self.binary(minor, "etcdutl"), "snapshot", "restore",
            self.evidence / snapshot["path"], "--data-dir=" + str(spec["data"]),
            "--name=" + spec["name"], "--initial-cluster=" + spec["name"] + "=" + spec["peer"],
            "--initial-advertise-peer-urls=" + spec["peer"],
            "--initial-cluster-token=public-synthetic-rehearsal",
            "--bump-revision=" + str(BUMP), "--mark-compacted"])

    def restored(self, minor, snapshot, spec, expected):
        self.restore(minor, snapshot, spec)
        with self.server(minor, spec):
            version = self.version(minor, spec)
            header = self.state(minor, spec, expected)
            require(header["revision"] >= snapshot["status"]["revision"] + BUMP,
                    "Restore did not bump the MVCC revision")
            require(header["cluster_id"] != snapshot["header"]["cluster_id"]
                    and header["member_id"] != snapshot["header"]["member_id"],
                    "Restore did not create new cluster/member identities")
            compacted = self.ctl(minor, spec, "get", PREFIX, "--prefix",
                                 "--rev=" + str(snapshot["status"]["revision"]), success=False)
            require(compacted.returncode != 0 and
                    "required revision has been compacted" in compacted.stderr,
                    "Native restored old-revision read was not rejected by compaction")
            key = PREFIX + "after-restore"
            self.ctl(minor, spec, "put", key, minor)
            after = self.state(minor, spec, {**expected, key: minor})
            require(after["revision"] > header["revision"], "Restored database cannot advance")
            return {"version": version, "restored_header": header, "writable_header": after,
                    "exact_keyspace": True, "old_revision_rejected": True}

    def invalid_snapshots(self, minor, snapshot):
        original = (self.evidence / snapshot["path"]).read_bytes()
        damaged = bytearray(original)
        damaged[-1] ^= 1
        cases = {"corrupt-hash": bytes(damaged), "truncated": original[:-17]}
        records = {}
        for name, content in cases.items():
            path = self.evidence / (minor + "-" + name + ".db")
            path.write_bytes(content)
            try:
                HOST.validate_snapshot(path)
            except RuntimeError as error:
                helper_error = str(error)
            else:
                raise RuntimeError("Production checksum helper accepted an invalid snapshot")
            result = self.command(
                [self.binary(minor, "etcdutl"), "snapshot", "restore", path,
                 "--data-dir=" + str(self.evidence / (minor + "-" + name + "-refused"))],
                success=False)
            require(result.returncode != 0 and "hash" in result.stderr.lower(),
                    "Native restore did not explicitly reject invalid snapshot integrity")
            records[name] = {"sha256": HOST.digest(path), "helper_error": helper_error,
                             "native_returncode": result.returncode, "native_error": result.stderr}
        return records


def run(tools, evidence):
    native = Native(tools, evidence)
    original = native.server_spec("original", 23790)
    expected = {PREFIX + "alpha": '{"generation":2}', PREFIX + "beta": "preserved"}
    with native.server("3.6", original):
        before_version = native.version("3.6", original)
        native.ctl("3.6", original, "put", PREFIX + "alpha", '{"generation":1}')
        native.ctl("3.6", original, "put", PREFIX + "beta", "preserved")
        native.ctl("3.6", original, "put", PREFIX + "alpha", expected[PREFIX + "alpha"])
        native.ctl("3.6", original, "put", PREFIX + "deleted", "must-not-return")
        native.ctl("3.6", original, "del", PREFIX + "deleted")
        old_snapshot = native.snapshot("3.6", original, "before-upgrade", expected)
    old_restore = native.restored("3.6", old_snapshot,
                                  native.server_spec("restored36", 23800), expected)
    old_negatives = native.invalid_snapshots("3.6", old_snapshot)
    with native.server("3.7", original):
        after_version = native.version("3.7", original)
        upgraded = native.state("3.7", original, expected)
        require(upgraded["cluster_id"] == old_snapshot["header"]["cluster_id"]
                and upgraded["member_id"] == old_snapshot["header"]["member_id"]
                and upgraded["revision"] == old_snapshot["status"]["revision"],
                "Same-data upgrade changed member identity or synthetic MVCC revision")
        expected[PREFIX + "after-upgrade"] = "native-3.7-write"
        native.ctl("3.7", original, "put", PREFIX + "after-upgrade",
                   expected[PREFIX + "after-upgrade"])
        new_snapshot = native.snapshot("3.7", original, "after-upgrade", expected)
    new_restore = native.restored("3.7", new_snapshot,
                                  native.server_spec("restored37", 23810), expected)
    new_negatives = native.invalid_snapshots("3.7", new_snapshot)
    write_json(evidence / "acceptance.json", {
        "outcome": "accepted-public-synthetic-native-etcd-only",
        "source_sha": os.environ["GITHUB_SHA"], "source_files": source(),
        "pins": pins(), "builds": native.builds,
        "before_version": before_version, "after_version": after_version,
        "before_snapshot": old_snapshot, "after_snapshot": new_snapshot,
        "original_minor_restore": old_restore, "target_minor_restore": new_restore,
        "same_data_upgrade": {"header": upgraded, "exact_keyspace": True},
        "invalid_snapshots": {"3.6": old_negatives, "3.7": new_negatives},
        "limitations": [
            "No production access, data, credentials, snapshots or RKE2 tokens.",
            "No RKE2 bootstrap/token, Kubernetes informer or application recovery rehearsal.",
            "No cross-minor snapshot restore, binary downgrade or automatic recovery.",
            "No private off-node destination, maintenance authorization or live readiness proof.",
            "No vendor binary advisory/security clearance or full RKE2 compatibility claim.",
        ],
    })
    print("Accepted isolated native etcd upgrade and both same-minor restores.")


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("operation", choices=("prepare", "run"))
    parser.add_argument("--tools", required=True, type=Path)
    parser.add_argument("--evidence", required=True, type=Path)
    args = parser.parse_args()
    operation = prepare if args.operation == "prepare" else run
    operation(args.tools.resolve(), args.evidence.resolve())


if __name__ == "__main__":
    main()
