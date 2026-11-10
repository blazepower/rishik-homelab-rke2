#!/usr/bin/env python3
"""Host half of operator-approved maintenance; invoked through existing SSH/sudo."""

import base64
import hashlib
import json
import os
from pathlib import Path
import platform
import re
import shutil
import socket
import stat
import subprocess
import sys
import tarfile
import time
import urllib.request


HEADLESS_UNITS = (
    "display-manager.service", "gdm3.service", "gdm.service",
    "cups.service", "cups.socket", "cups.path", "cups-browsed.service",
    "bluetooth.service", "avahi-daemon.service", "avahi-daemon.socket",
    "ModemManager.service",
)


def require(condition, message):
    if not condition:
        raise RuntimeError(message)


def command(args, timeout=600):
    result = subprocess.run(args, capture_output=True, text=True, timeout=timeout,
                            env={**os.environ, "LC_ALL": "C", "DEBIAN_FRONTEND": "noninteractive",
                                 "NEEDRESTART_MODE": "l"})
    require(result.returncode == 0, f"{args[0]} failed (exit {result.returncode}); stop and inspect on host")
    return result.stdout.strip()


def digest(path):
    with open(path, "rb") as stream:
        return hashlib.file_digest(stream, "sha256").hexdigest()


def installed(package):
    result = subprocess.run(
        ["dpkg-query", "-W", "-f=${db:Status-Abbrev}", package],
        capture_output=True, text=True,
    )
    return result.returncode == 0 and result.stdout.startswith("ii")


def audit():
    release = {}
    for line in Path("/etc/os-release").read_text().splitlines():
        if "=" in line:
            key, value = line.split("=", 1)
            release[key] = value.strip('"')
    memory = {}
    for line in Path("/proc/meminfo").read_text().splitlines():
        key, value = line.split(":", 1)
        memory[key] = int(value.strip().split()[0]) * 1024
    boot = next(line.split()[1] for line in Path("/proc/stat").read_text().splitlines() if line.startswith("btime "))
    simulation = command(["apt-get", "-s", "--no-remove", "--with-new-pkgs", "upgrade"])
    updates = [line for line in simulation.splitlines() if line.startswith("Inst ")]
    return {
        "name": socket.gethostname(), "os": release.get("ID"),
        "os_version": release.get("VERSION_ID"), "architecture": platform.machine(),
        "kernel": platform.release(), "boot_epoch": int(boot),
        "boot_id": Path("/proc/sys/kernel/random/boot_id").read_text().strip(),
        "reboot_required": Path("/run/reboot-required").exists(),
        "kernel_installed": [p.name.removeprefix("vmlinuz-") for p in Path("/boot").glob("vmlinuz-*")],
        "root_free_bytes": shutil.disk_usage("/").free,
        "boot_free_bytes": shutil.disk_usage("/boot").free,
        "kernel_metapackage_installed": installed("linux-generic"),
        "memory_available_bytes": memory["MemAvailable"], "sunreclaim_bytes": memory["SUnreclaim"],
        "default_target": command(["systemctl", "get-default"]),
        "desktop_installed": installed("ubuntu-desktop-minimal") or installed("gnome-shell"),
        "pending_packages": updates,
        "apt_metadata_epoch": max(
            (p.stat().st_mtime for p in Path("/var/lib/apt/lists").glob("*InRelease")),
            default=0,
        ),
    }


def download(url, sha256, target):
    require(re.fullmatch(r"[a-f0-9]{64}", sha256) is not None, "Missing pinned publisher SHA256")
    with urllib.request.urlopen(url, timeout=120) as response, open(target, "xb") as output:
        shutil.copyfileobj(response, output)
    require(digest(target) == sha256, "Publisher asset SHA256 mismatch")


def extract_file(archive, suffix, destination):
    with tarfile.open(archive, "r:gz") as source:
        matches = [m for m in source.getmembers() if m.name.lstrip("./") == suffix]
        require(len(matches) == 1 and matches[0].isfile(), "Unexpected publisher archive layout")
        with source.extractfile(matches[0]) as input_file, open(destination, "xb") as output:
            shutil.copyfileobj(input_file, output)
    destination.chmod(0o755)


def validate_snapshot(path):
    size = path.stat().st_size
    require(size > 32, "Snapshot is empty/truncated")
    checksum = hashlib.sha256()
    remaining = size - 32
    with path.open("rb") as stream:
        while remaining:
            block = stream.read(min(1024 * 1024, remaining))
            require(bool(block), "Snapshot truncated while checking")
            checksum.update(block)
            remaining -= len(block)
        require(checksum.digest() == stream.read(32), "Snapshot's embedded checksum is invalid")
    return digest(path)


def backup(payload, work):
    recovery = Path(payload["recovery_directory"]).resolve()
    require(recovery.is_dir(), "Protected recovery directory must already exist")
    mount = json.loads(command(["findmnt", "-J", "-T", str(recovery), "-o", "TARGET,FSTYPE"]))["filesystems"][0]
    require(mount["target"] != "/" and mount["fstype"] in ("nfs", "nfs4", "cifs"),
            "Recovery directory must be on an explicitly provisioned off-node NFS/CIFS mount")
    metadata = recovery.stat()
    require(metadata.st_uid == 0 and stat.S_IMODE(metadata.st_mode) == 0o700,
            "Recovery directory must be root-owned mode 0700")
    prefix = "node-maintenance-" + payload["run_id"]
    started = time.time()
    command(["/usr/local/bin/rke2", "etcd-snapshot", "save", "--name", prefix,
             "--etcd-s3=false", "--etcd-snapshot-compress=false"])
    candidates = list(Path("/var/lib/rancher/rke2/server/db/snapshots").glob(prefix + "-*"))
    require(len(candidates) == 1, "Cannot uniquely identify new snapshot in default RKE2 data directory")
    snapshot = candidates[0]
    require(snapshot.is_file() and not snapshot.is_symlink() and snapshot.stat().st_mtime >= started - 2,
            "Snapshot is not a fresh regular file")
    checksum = validate_snapshot(snapshot)
    version = payload["etcdutl"]["version"]
    require(re.fullmatch(r"v3\.\d+\.\d+", version) is not None, "Invalid etcdutl version")
    archive = work / "etcd.tar.gz"
    download(f"https://github.com/etcd-io/etcd/releases/download/{version}/etcd-{version}-linux-amd64.tar.gz",
             payload["etcdutl"]["sha256"], archive)
    tool = work / "etcdutl"
    extract_file(archive, f"etcd-{version}-linux-amd64/etcdutl", tool)
    status = json.loads(command(["unshare", "--net", "--", str(tool), "snapshot", "status",
                                 str(snapshot), "--write-out=json"]))
    require(status.get("revision", 0) > 0 and status.get("totalKey", 0) > 0,
            "Native etcdutl snapshot status did not validate a populated database")
    destination = recovery / snapshot.name
    with snapshot.open("rb") as source, destination.open("xb") as output:
        os.fchmod(output.fileno(), 0o600)
        shutil.copyfileobj(source, output)
        output.flush()
        os.fsync(output.fileno())
    require(validate_snapshot(destination) == checksum and digest(snapshot) == checksum,
            "Off-node recovery copy verification failed")
    return {"snapshot_sha256": checksum, "native_revision": status["revision"],
            "off_node_verified": True, "created_epoch": time.time()}


def os_upgrade(payload):
    before = audit()
    require(before["kernel_metapackage_installed"], "Supported Ubuntu kernel metapackage is missing")
    command(["apt-get", "update"])
    refreshed = audit()
    require(refreshed["pending_packages"] == payload["pending_packages"],
            "APT candidates changed after refresh; generate and approve a new plan")
    require(not command(["dpkg", "--audit"]), "dpkg needs repair before maintenance")
    require(not command(["apt-mark", "showhold"]), "Held packages need individual review")
    # Never permit package removal, a release upgrade, autoremove or a foreign kernel.
    command(["apt-get", "-y", "--no-remove", "--with-new-pkgs",
             "-o", "Dpkg::Options::=--force-confold", "upgrade"], timeout=None)
    require(installed(payload["kernel_package"]), "Supported Ubuntu kernel metapackage is missing")
    require(Path("/boot/vmlinuz-" + payload["kernel"]).is_file(), "Approved Ubuntu kernel was not installed")
    command(["systemctl", "set-default", "multi-user.target"])
    for unit in HEADLESS_UNITS:
        result = subprocess.run(["systemctl", "show", unit, "-p", "LoadState", "--value"],
                                capture_output=True, text=True)
        if result.stdout.strip() not in ("", "not-found"):
            command(["systemctl", "mask", "--now", "--force", unit])
    return {"old_boot_id": before["boot_id"], "expected_kernel": payload["kernel"],
            "headless": True, "package_removals": False}


def rke2_upgrade(payload, work):
    release = payload["release"]
    version = release["version"]
    require(re.fullmatch(r"v1\.\d+\.\d+\+rke2r\d+", version) is not None, "Invalid RKE2 version")
    archive = work / "rke2.tar.gz"
    download(f"https://github.com/rancher/rke2/releases/download/{version.replace('+', '%2B')}/rke2.linux-amd64.tar.gz",
             release["sha256"], archive)
    candidate = work / "rke2"
    extract_file(archive, "bin/rke2", candidate)
    require(version in command([str(candidate), "--version"]).split(), "Candidate binary version mismatch")
    current = Path("/usr/local/bin/rke2")
    require(current.is_file() and not current.is_symlink() and current.stat().st_uid == 0,
            "Only the established root-owned tarball RKE2 installation is supported")
    require(payload["source_version"] in command([str(current), "--version"]).split(), "Installed RKE2 drifted")
    shutil.copy2(current, work / "rke2-previous")
    unit = "rke2-" + payload["role"] + ".service"
    hooks = command(["systemctl", "show", unit, "-p", "ExecStop", "-p", "ExecStopPost"]).lower()
    require(not any(value in hooks for value in ("sigkill", " -9", " -kill", "--signal=9", "--signal=kill", "killall")),
            "Existing service stop hooks require manual review; no forced-stop fallback is permitted")
    guard = Path("/run/systemd/system") / (unit + ".d") / "90-node-maintenance.conf"
    guard.parent.mkdir(parents=True, exist_ok=True)
    require(not guard.exists(), "A previous maintenance stop guard needs operator investigation")
    guard.write_text("[Service]\nTimeoutStopSec=5min\nSendSIGKILL=no\nKillMode=process\n")
    command(["systemctl", "daemon-reload"])
    effective = dict(line.split("=", 1) for line in command([
        "systemctl", "show", unit, "-p", "SendSIGKILL", "-p", "KillMode", "-p", "TimeoutStopUSec",
    ]).splitlines())
    require(effective.get("SendSIGKILL") == "no" and effective.get("KillMode") == "process"
            and effective.get("TimeoutStopUSec") == "5min",
            "Effective service guard was overridden; do not install/restart")
    temporary = current.with_name("rke2.node-maintenance")
    require(not temporary.exists(), "Previous candidate installation needs operator investigation")
    shutil.copy2(candidate, temporary)
    os.replace(temporary, current)
    command(["systemctl", "restart", unit], timeout=660)
    pid = command(["systemctl", "show", unit, "-p", "MainPID", "--value"])
    require(pid.isdigit() and int(pid) > 1, "RKE2 service has no running main process")
    require(digest(Path("/proc") / pid / "exe") == digest(current), "Running RKE2 executable mismatch")
    # The guard is deliberately retained until the workstation accepts postflight.
    return {"version": version, "binary_sha256": digest(current), "guard": str(guard)}


def main(payload):
    operation = payload["operation"]
    if operation == "audit":
        print(json.dumps(audit()))
        return
    require(os.geteuid() == 0 and platform.machine() == "x86_64", "Root on amd64 is required")
    require(re.fullmatch(r"[a-f0-9]{32}", payload["run_id"]) is not None, "Invalid run identifier")
    state = audit()
    require(state["name"] == payload["name"] and state["os"] == "ubuntu"
            and state["os_version"] == payload["ubuntu_lts"], "Host/Ubuntu LTS identity mismatch")
    require(state["boot_id"] == payload["boot_id"], "Host rebooted after plan approval")
    require(state["root_free_bytes"] >= 4 * 1024**3
            and state["boot_free_bytes"] >= 512 * 1024**2
            and state["memory_available_bytes"] >= 1536 * 1024**2
            and state["sunreclaim_bytes"] < 1024**3, "Host capacity/slab precondition failed")
    if operation == "accept":
        guard = Path("/run/systemd/system") / ("rke2-" + payload["role"] + ".service.d") / "90-node-maintenance.conf"
        guard.unlink()
        command(["systemctl", "daemon-reload"])
        return
    directory = Path("/var/lib/node-maintenance") / payload["run_id"]
    directory.mkdir(mode=0o700, parents=True, exist_ok=False)
    os.chmod(directory.parent, 0o700)
    journal = command(["journalctl", "-u", "rke2-" + payload["role"], "--since=-15min",
                       "--no-pager", "-o", "cat"], timeout=60).lower()
    (directory / "before.json").write_text(json.dumps({
        "host": state, "journal_classification_counts": {
            word: journal.count(word) for word in ("error", "panic", "oom", "x509", "timeout")
        },
    }))
    recovery = backup(payload, directory) if payload["role"] == "server" else None
    if operation == "os":
        result = os_upgrade(payload)
    else:
        require(operation == "rke2", "Unknown operation")
        result = rke2_upgrade(payload, directory)
    result.update({"run_id": payload["run_id"], "name": state["name"], "operation": operation,
                   "recovery": recovery, "completed_epoch": time.time()})
    receipt = Path("/var/tmp") / ("node-maintenance-" + payload["run_id"] + ".json")
    with receipt.open("x") as output:
        output.write(json.dumps(result))
        os.fchmod(output.fileno(), 0o444)
    if operation == "os":
        command(["systemd-run", "--unit=node-maintenance-reboot-" + payload["run_id"],
                 "--on-active=10s", "/usr/bin/systemctl", "reboot"])
    print("Host step complete; workstation must verify recovery before continuing.")


if __name__ == "__main__":
    try:
        main(json.loads(base64.b64decode(sys.argv[1])))
    except Exception as error:
        print(f"STOP: {error}", file=sys.stderr)
        sys.exit(1)
