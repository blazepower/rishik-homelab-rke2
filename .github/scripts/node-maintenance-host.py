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
import shlex
import zlib


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


def recovery_directory(path):
    recovery = Path(path).resolve()
    require(recovery.is_dir(), "Protected recovery directory must already exist")
    mount = json.loads(command(["findmnt", "-J", "-T", str(recovery), "-o", "TARGET,FSTYPE"]))["filesystems"][0]
    require(mount["target"] != "/" and mount["fstype"] in ("nfs", "nfs4", "cifs"),
            "Recovery directory must be on an explicitly provisioned off-node NFS/CIFS mount")
    metadata = recovery.stat()
    require(metadata.st_uid == 0 and stat.S_IMODE(metadata.st_mode) == 0o700,
            "Recovery directory must be root-owned mode 0700")
    return recovery


def backup(payload, work):
    recovery = recovery_directory(payload["recovery_directory"])
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
    reserve(payload)
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
    reserve(payload)
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


def reserve(payload):
    require(payload["deadline_epoch"] - time.time() > payload["recovery_reserve_seconds"],
            "Host reached restoration reserve; no new disruption/reboot")
    if payload.get("cold_run_id"):
        status = cold_peer_status(payload["cold_run_id"])
        require(status["mode"] == "disrupting" and status["phase"] == "awaiting-host"
                and status["host_started"] is True and status["host_step_id"] == payload["run_id"]
                and status["deadline_epoch"] == payload["deadline_epoch"]
                and not status.get("host_slot_cancelled"), "Independent guard revoked worker2 host disruption")


def cold_public(path):
    meta = path.lstat()
    require(stat.S_ISREG(meta.st_mode) and meta.st_uid == 0 and stat.S_IMODE(meta.st_mode) == 0o444,
            "Cold receipt ownership/mode mismatch")
    return json.loads(path.read_text())


def cold_peer_status(run_id):
    require(re.fullmatch("[a-f0-9]{32}", run_id), "Invalid cold run identifier")
    source = (
        "import json,pathlib,stat; "
        f"p=pathlib.Path('/var/lib/node-maintenance-cold-status/{run_id}.json'); "
        "s=p.lstat(); assert stat.S_ISREG(s.st_mode) and s.st_uid==0 and stat.S_IMODE(s.st_mode)==0o444; "
        "print(p.read_text())"
    )
    return json.loads(command(["ssh", "-T", "-o", "BatchMode=yes", "-o", "StrictHostKeyChecking=yes",
                               "-o", "ConnectTimeout=10", "rishik@192.168.1.10",
                               shlex.join(["python3", "-c", source])]))


def cold_register(payload):
    require(socket.gethostname() == "rishik-controller", "Cold guard belongs only on the existing controller")
    incoming = Path(payload["input_file"])
    require(incoming.parent == Path("/home/rishik/.local/state/node-maintenance-cold") / payload["run_id"]
            and incoming.name == "source.json" and not incoming.is_symlink(),
            "Unexpected bound guard input file")
    data = incoming.read_bytes()
    require(hashlib.sha256(data).hexdigest() == payload["input_sha256"], "Guard input transport drift")
    transport = json.loads(data)
    require(set(transport) == {"plan", "approval", "host_step_id", "bundle"}, "Unexpected guard transport fields")
    payload = {**payload, **transport}
    files = json.loads(zlib.decompress(base64.b64decode(payload["bundle"])))
    expected = (".github/scripts/node-maintenance.py", ".github/scripts/node-maintenance-host.py",
                ".github/scripts/node-maintenance-cold.py", "node-config/maintenance/policy.json",
                "apps/_shared/http-addon-owned/source.json")
    require(set(files) == set(expected) and set(payload["plan"]["source_bindings"]) == set(expected),
            "Unexpected guard bundle members")
    directory = Path("/var/lib/node-maintenance-cold") / payload["run_id"]
    directory.parent.mkdir(mode=0o700, exist_ok=True)
    require(directory.parent.stat().st_uid == 0 and stat.S_IMODE(directory.parent.stat().st_mode) == 0o700,
            "Cold guard parent must be root-owned 0700")
    directory.mkdir(mode=0o700, exist_ok=False)
    for name, encoded in files.items():
        value = base64.b64decode(encoded)
        require(hashlib.sha256(value).hexdigest() == payload["plan"]["source_bindings"][name],
                "Guard source bundle hash mismatch")
        path = directory / name
        path.parent.mkdir(mode=0o700, parents=True, exist_ok=True)
        with path.open("xb") as output:
            os.fchmod(output.fileno(), 0o600)
            output.write(value)
            output.flush()
            os.fsync(output.fileno())
    with (directory / "plan.json").open("x") as output:
        os.fchmod(output.fileno(), 0o600)
        output.write(json.dumps(payload["plan"]))
        output.flush()
        os.fsync(output.fileno())
    require(hashlib.sha256(json.dumps(payload["plan"], sort_keys=True, separators=(",", ":")).encode()).hexdigest()
            == payload["approval"], "Guard plan approval mismatch")
    state = {
        "run_id": payload["run_id"], "plan_sha256": payload["approval"], "mode": "registered", "phase": "registered",
        "started_epoch": payload["started_epoch"], "deadline_epoch": payload["deadline_epoch"],
        "last_epoch": time.time(), "guard_registered": False, "mutations": {}, "completed": False,
        "host_step_id": payload["host_step_id"], "host_started": False, "host_complete": False,
    }
    require(state["deadline_epoch"] == state["started_epoch"] + 43200
            and state["deadline_epoch"] - time.time() > 7200, "Invalid or spent guard deadline")
    with (directory / "state.json").open("x") as output:
        os.fchmod(output.fileno(), 0o600)
        output.write(json.dumps(state))
        output.flush()
        os.fsync(output.fileno())
    script = directory / ".github/scripts/node-maintenance-cold.py"
    unit = "node-maintenance-cold-" + payload["run_id"]
    guard = unit + "-guard"
    executable = f"/usr/bin/python3 -B {script}"
    units = {
        unit + ".service": ("[Unit]\nDescription=Bound worker2 cold coordinator\n[Service]\nType=oneshot\n"
                           f"ExecStart={executable} execute --directory {directory}\n"
                           "TimeoutStartSec=infinity\nSendSIGKILL=no\nKillMode=process\n"),
        guard + ".service": ("[Unit]\nDescription=Worker2 cold original-state recovery guard\n[Service]\nType=oneshot\n"
                            f"ExecStart={executable} guard --directory {directory}\n"
                            "TimeoutStartSec=infinity\nSendSIGKILL=no\nKillMode=process\n"),
        guard + ".timer": ("[Unit]\nDescription=Independent worker2 reserve/interruption recovery\n[Timer]\n"
                          "OnBootSec=30s\nOnUnitActiveSec=30s\n"
                          f"Unit={guard}.service\n[Install]\nWantedBy=timers.target\n"),
    }
    for name, value in units.items():
        with (Path("/etc/systemd/system") / name).open("x") as output:
            os.fchmod(output.fileno(), 0o644)
            output.write(value)
            output.flush()
            os.fsync(output.fileno())
    command(["systemctl", "daemon-reload"])
    command(["systemctl", "enable", "--now", guard + ".timer"])
    command(["systemctl", "start", guard + ".service"])
    require(command(["systemctl", "is-enabled", guard + ".timer"]) == "enabled"
            and command(["systemctl", "is-active", guard + ".timer"]) == "active",
            "Independent recovery timer registration not verified")
    # Actual guard execution, not merely accepting a successful unit creation command.
    state = json.loads((directory / "state.json").read_text())
    require(0 <= time.time() - state.get("guard_verified_epoch", 0) < 60,
            "Registered guard did not run its native storage/context self-check")
    cold_module = load_cold(script)
    coordinator = cold_module.load_runtime(directory)
    coordinator.api.host_audit(coordinator.policy, "rishik-worker2")
    coordinator.journal.transaction(lambda s: s.update(guard_registered=True))
    print("Cold guard registered and verified; no application was stopped.")


def load_cold(script):
    import importlib.util
    spec = importlib.util.spec_from_file_location("host_cold", script)
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


def cold_control(payload):
    directory = Path("/var/lib/node-maintenance-cold") / payload["run_id"]
    cold = load_cold(directory / ".github/scripts/node-maintenance-cold.py")
    c = cold.load_runtime(directory)
    operation = payload["operation"]
    if operation == "cold-start":
        def start(state):
            c.gate(state)
            require(state["mode"] == "registered" and time.time() < state["deadline_epoch"] - 7200,
                    "Cold execution already started or reserve reached")
            timer = "node-maintenance-cold-" + payload["run_id"] + "-guard.timer"
            require(command(["systemctl", "is-active", timer]) == "active"
                    and command(["systemctl", "is-enabled", timer]) == "enabled",
                    "Independent recovery guard disappeared before first app stop")
            proof = payload["worker2_access"]
            require(proof["name"] == "rishik-worker2" and proof["boot_id"] == c.plan["bindings"]["rishik-worker2"]["boot_id"]
                    and proof["controller_status_verified"] is True and 0 <= time.time() - proof["created_epoch"] < 60,
                    "Selected real worker2 sudo/recovery channel must be verified BEFORE first app stop")
            # Rebind exact metadata/packages/client immediately before the executor's first pause.
            versions = json.loads(c.api.kube("version", "-o", "json"))
            require(versions["serverVersion"]["gitVersion"] == c.plan["kubectl"]["server"], "API version drift")
            require(versions["clientVersion"]["gitVersion"] == c.plan["controller_client"]["version"],
                    "Controller native kubectl version drift")
            cold.require(cold.source_bindings() == c.plan["source_bindings"], "Guard source drift")
            cold.m.check_client_skew(versions["clientVersion"]["gitVersion"], versions["serverVersion"]["gitVersion"])
            cold.m.check_nodes(c.policy, c.api.items("nodes"))
            for node in c.api.items("nodes"):
                require(node["status"]["nodeInfo"]["kubeletVersion"]
                        == c.plan["bindings"][node["metadata"]["name"]]["source_version"], "RKE2 source drift")
            for row in c.plan["controllers"] + c.plan["reconciliation"] + c.plan["schedules"]:
                cold.assert_binding(c.api.get(cold.RESOURCES[row["kind"]], row["namespace"], row["name"]), row)
            host = c.api.host_audit(c.policy, "rishik-worker2")
            require(host["boot_id"] == c.plan["bindings"]["rishik-worker2"]["boot_id"]
                    and host["pending_packages"] == c.plan["bindings"]["rishik-worker2"]["pending_packages"],
                    "Host/package drift before first pause")
            cold.cold_storage(c.plan, c.policy, c.api)
            cold.native_gate(c.plan, c.policy, c.api)
            c.autoscaling_restored()
            state["mode"] = "disrupting"
        c.journal.transaction(start)
        command(["systemctl", "start", "--no-block", "node-maintenance-cold-" + payload["run_id"] + ".service"])
    elif operation == "cold-host-start":
        c.infrastructure(drained=True)
        cold.native_gate(c.plan, c.policy, c.api, released=True)
        def mark(state):
            c.gate(state, True)
            require(state["phase"] == "awaiting-host" and not state["host_started"], "Host slot not available")
            state.update(host_started=True, host_started_epoch=time.time())
        c.journal.transaction(mark)
    elif operation == "cold-host-complete":
        def finish(state):
            c.gate(state)
            require(state["host_started"] and payload["receipt"]["run_id"] == state["host_step_id"]
                    and payload["receipt"]["name"] == "rishik-worker2"
                    and payload["receipt"]["operation"] == "os"
                    and payload["receipt"]["old_boot_id"] == c.plan["bindings"]["rishik-worker2"]["boot_id"]
                    and payload["receipt"]["expected_kernel"] == c.policy["kernel"]
                    and 0 <= time.time() - payload["receipt"]["completed_epoch"] < 1800,
                    "Invalid worker2 host receipt")
            state["host_complete"] = True
        c.journal.transaction(finish)
    else:
        require(operation == "cold-recover", "Unsupported cold control")
        c.request_recovery("explicit-safe-recovery-command")
        command(["systemctl", "start", "--no-block", "node-maintenance-cold-" + payload["run_id"] + "-guard.service"])


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
    if operation == "cold-status":
        require(re.fullmatch("[a-f0-9]{32}", payload["run_id"]), "Invalid cold status identifier")
        print(json.dumps(cold_public(Path("/var/lib/node-maintenance-cold-status") / (payload["run_id"] + ".json"))))
        return
    if operation == "cold-access-receipt":
        require(re.fullmatch("[a-f0-9]{32}", payload["run_id"]), "Invalid cold access identifier")
        print(json.dumps(cold_public(Path("/var/lib/node-maintenance-cold-access") / (payload["run_id"] + ".json"))))
        return
    if operation == "cold-client-audit":
        require(socket.gethostname() == "rishik-controller", "Controller native kubectl binding required")
        kubectl = Path("/var/lib/rancher/rke2/bin/kubectl")
        version = json.loads(command([str(kubectl), "version", "--client", "-o", "json"]))
        print(json.dumps({"version": version["clientVersion"]["gitVersion"], "sha256": digest(kubectl)}))
        return
    if operation == "reboot":
        require(os.geteuid() == 0 and socket.gethostname() == payload["name"]
                and Path("/proc/sys/kernel/random/boot_id").read_text().strip() == payload["boot_id"],
                "Deferred reboot host/boot identity drift")
        reserve(payload)
        command(["systemctl", "reboot"])
        return
    if operation == "audit":
        print(json.dumps(audit()))
        return
    if operation == "recovery-audit":
        recovery_directory(payload["recovery_directory"])
        print(json.dumps({"off_node_directory_verified": True}))
        return
    require(os.geteuid() == 0 and platform.machine() == "x86_64", "Root on amd64 is required")
    require(re.fullmatch(r"[a-f0-9]{32}", payload["run_id"]) is not None, "Invalid run identifier")
    state = audit()
    require(state["name"] == payload["name"] and state["os"] == "ubuntu"
            and state["os_version"] == payload["ubuntu_lts"], "Host/Ubuntu LTS identity mismatch")
    require(state["boot_id"] == payload["boot_id"], "Host rebooted after plan approval")
    if operation.startswith("cold-"):
        reserve_payload = {**payload}
        reserve_payload.pop("cold_run_id", None)
        if operation not in ("cold-recover", "cold-host-complete"):
            reserve(reserve_payload)
        if operation == "cold-access":
            require(state["name"] == "rishik-worker2", "Only worker2 sudo access is selected")
            verified = bool(payload.get("controller_run_id"))
            if verified:
                status = cold_peer_status(payload["controller_run_id"])
                require(status["guard_registered"] is True and status["mode"] == "registered",
                        "Existing worker2-to-controller SSH recovery channel failed")
            directory = Path("/var/lib/node-maintenance-cold-access")
            directory.mkdir(mode=0o755, exist_ok=True)
            with (directory / (payload["run_id"] + ".json")).open("x") as output:
                os.fchmod(output.fileno(), 0o444)
                output.write(json.dumps({"name": state["name"], "boot_id": state["boot_id"],
                                         "created_epoch": time.time(), "controller_status_verified": verified}))
            return
        if operation == "cold-register":
            cold_register(payload)
        else:
            require(state["name"] == "rishik-controller", "Cold control belongs only on controller")
            cold_control(payload)
        return
    require(state["root_free_bytes"] >= 4 * 1024**3
            and state["boot_free_bytes"] >= 512 * 1024**2
            and state["memory_available_bytes"] >= 1536 * 1024**2
            and state["sunreclaim_bytes"] < 1024**3, "Host capacity/slab precondition failed")
    if operation == "accept":
        guard = Path("/run/systemd/system") / ("rke2-" + payload["role"] + ".service.d") / "90-node-maintenance.conf"
        guard.unlink()
        command(["systemctl", "daemon-reload"])
        return
    require(payload["deadline_epoch"] - time.time() > payload["recovery_reserve_seconds"],
            "Host reached restoration reserve; do not start a disruptive step")
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
    require(payload["deadline_epoch"] - time.time() > payload["recovery_reserve_seconds"],
            "Backup/preflight consumed the restoration reserve; do not change host services")
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
        # APT may run longer than the disruption window. Check again both now and
        # in the deferred unit; never kill dpkg or reboot into the recovery reserve.
        reserve(payload)
        reboot = {**payload, "operation": "reboot"}
        source = Path(__file__).read_text() if "__file__" in globals() else globals().get("__maintenance_host_source__")
        require(source, "Deferred reboot requires the exact bound host helper source")
        command(["systemd-run", "--unit=node-maintenance-reboot-" + payload["run_id"],
                 "--on-active=10s", "--property=SendSIGKILL=no",
                 "/usr/bin/python3", "-c",
                 "import base64,zlib;__maintenance_host_source__=zlib.decompress(base64.b64decode(" +
                 repr(base64.b64encode(zlib.compress(source.encode())).decode()) +
                 ")).decode();exec(__maintenance_host_source__)",
                 base64.b64encode(json.dumps(reboot).encode()).decode()])
    print("Host step complete; workstation must verify recovery before continuing.")


if __name__ == "__main__":
    try:
        main(json.loads(base64.b64decode(sys.argv[1])))
    except Exception as error:
        print(f"STOP: {error}", file=sys.stderr)
        sys.exit(1)
