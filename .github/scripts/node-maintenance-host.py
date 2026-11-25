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
    command(["apt-get", "update"], timeout=None)
    refreshed = audit()
    require(refreshed["pending_packages"] == payload["pending_packages"],
            "APT candidates changed after refresh; generate and approve a new plan")
    require(not command(["dpkg", "--audit"]), "dpkg needs repair before maintenance")
    require(not command(["apt-mark", "showhold"]), "Held packages need individual review")
    reserve(payload)
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
        require(payload["name"] == "rishik-worker2" and payload["operation"] in ("os", "reboot"),
                "Cold host authorization is worker2 OS/reboot ONLY")
        status = cold_peer_status(payload["cold_run_id"])
        require(status["mode"] == "disrupting" and status["phase"] == "awaiting-host"
                and status["host_started"] is True and status["host_step_id"] == payload["run_id"]
                and status["deadline_epoch"] == payload["deadline_epoch"]
                and status.get("host_reboot_authorized") is True
                and not status.get("host_slot_cancelled"), "Independent guard revoked worker2 host disruption")
        if payload.get("contract_sha256"):
            require(status["worker_contract_sha256"] == payload["contract_sha256"],
                    "Exact standby source/approval slot drift")
            require(standby_peer(status["worker_contract"], status, time.time(), launching=True) == "claim",
                    "Standby launch revoked")
            directory, contract = worker_runtime(payload["cold_run_id"])
            require(payload == {**contract["os_payload"], "operation": payload["operation"],
                                "contract_sha256": payload["contract_sha256"]}, "Standby OS/reboot payload drift")
            def clock(state):
                now = time.time()
                require(now >= state["last_epoch"] and state["phase"] in ("claimed", "packages-complete"),
                        "Standby local clock/terminal authorization revoked")
                state["last_epoch"] = now
            worker_transaction(directory, clock)


def cold_public(path):
    meta = path.lstat()
    require(stat.S_ISREG(meta.st_mode) and meta.st_uid == 0 and stat.S_IMODE(meta.st_mode) == 0o444,
            "Cold receipt ownership/mode mismatch")
    return json.loads(path.read_text())


def schedule_reboot(payload):
    reserve(payload)
    reboot = {**payload, "operation": "reboot"}
    source = Path(__file__).read_bytes().decode("utf-8") if "__file__" in globals() else globals().get("__maintenance_host_source__")
    require(source, "Deferred reboot requires the exact bound host helper source")
    command(["systemd-run", "--unit=node-maintenance-reboot-" + payload["run_id"],
             "--on-active=10s", "--timer-property=RemainAfterElapse=no", "--property=SendSIGKILL=no",
             "/usr/bin/python3", "-c",
             "import base64,zlib;__maintenance_host_source__=zlib.decompress(base64.b64decode(" +
             repr(base64.b64encode(zlib.compress(source.encode())).decode()) +
             ")).decode();exec(__maintenance_host_source__)",
             base64.b64encode(json.dumps(reboot).encode()).decode()])


def cold_peer_status(run_id):
    require(re.fullmatch("[a-f0-9]{32}", run_id), "Invalid cold run identifier")
    return load_peer().request("controller-status", run_id)


def load_peer():
    import importlib.util
    if "__file__" in globals():
        script = Path(__file__).with_name("node-maintenance-peer.py")
    else:
        setup_id = cold_public(Path("/etc/node-maintenance-peer/current.json"))["setup_id"]
        require(re.fullmatch("[a-f0-9]{32}", setup_id), "Invalid maintenance peer setup")
        script = Path("/etc/node-maintenance-peer") / setup_id / "node-maintenance-peer.py"
        meta = script.lstat()
        require(stat.S_ISREG(meta.st_mode) and meta.st_uid == 0 and stat.S_IMODE(meta.st_mode) == 0o444,
                "Installed maintenance peer source is not trusted")
    spec = importlib.util.spec_from_file_location("maintenance_peer", script)
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


def standby_peer_binding(contract, peer):
    require(peer["run_id"] == contract["run_id"] and peer["host_step_id"] == contract["host_step_id"]
            and peer["plan_sha256"] == contract["plan_sha256"]
            and peer["worker_contract"] == contract
            and peer["worker_contract_sha256"] == hashlib.sha256(
                json.dumps(contract, sort_keys=True, separators=(",", ":")).encode()).hexdigest()
            and peer["started_epoch"] == contract["started_epoch"]
            and peer["deadline_epoch"] == contract["deadline_epoch"],
            "Standby controller identity/source/approval/clock replay")
    require(peer["controller_name"] == "rishik-controller"
            and peer["controller_boot_id"] == contract["bindings"]["rishik-controller"]["boot_id"]
            and peer["controller_kernel"] == contract["bindings"]["rishik-controller"]["kernel"]
            and peer["live_source_bindings"] == contract["source_bindings"],
            "Standby original controller boot/kernel/source drift")


def standby_peer(contract, peer, now, launching=False):
    standby_peer_binding(contract, peer)
    require(now >= contract["started_epoch"] and now < contract["deadline_epoch"] - 7200,
            "Standby clock/reserve/expiry revoked")
    if peer.get("host_slot_cancelled") or peer["mode"] == "recovering" or peer["completed"]:
        return "cancelled"
    require(peer["guard_registered"] is True and peer["guard_active"] is True and peer["guard_enabled"] is True,
            "Standby independent guard unavailable")
    epoch = peer["guard_verified_epoch"] if peer["mode"] == "registered" else peer["last_epoch"]
    require(contract["started_epoch"] <= epoch <= now, "Standby stale/rolled-back controller clock")
    if peer["mode"] == "registered" or launching or peer["host_started"]:
        require(now - epoch < 60, "Standby stale controller launch slot")
    if peer["mode"] == "registered":
        require(not launching and not peer["host_started"] and not peer.get("host_reboot_authorized"),
                "Standby registered slot cannot launch")
        return "waiting"
    require(peer["mode"] == "disrupting", "Standby controller mode drift")
    if not peer["executor_active"] and not launching and peer["phase"] == "registered" and not peer["host_started"]:
        return "waiting"  # Inert cold-start -> systemd startup handoff, never launch authority.
    require(peer["executor_active"] is True, "Standby executor loss; launch revoked")
    if peer["phase"] == "awaiting-host" and peer["host_started"] is True and peer["host_reboot_authorized"] is True:
        require(0 <= now - peer["host_native_gate_epoch"] < 60, "Standby stale native storage/peer host slot")
        return "claim"
    require(not launching and not peer["host_started"], "Standby precise host slot unavailable")
    return "waiting"


def read_transport(payload):
    incoming = Path(payload["input_file"])
    require(incoming.parent == Path("/home/rishik/.local/state/node-maintenance-cold") / payload["run_id"]
            and incoming.name == "source.json" and not incoming.is_symlink(), "Unexpected bound standby input")
    data = incoming.read_bytes()
    require(hashlib.sha256(data).hexdigest() == payload["input_sha256"], "Standby input transport drift")
    transport = json.loads(data)
    require(set(transport) == {"plan", "approval", "host_step_id", "bundle"}, "Unexpected standby transport fields")
    require(hashlib.sha256(json.dumps(transport["plan"], sort_keys=True, separators=(",", ":")).encode()).hexdigest()
            == transport["approval"], "Standby exact plan approval mismatch")
    files = json.loads(zlib.decompress(base64.b64decode(transport["bundle"])))
    expected = {".github/scripts/node-maintenance.py", ".github/scripts/node-maintenance-host.py",
                ".github/scripts/node-maintenance-cold.py", ".github/scripts/node-maintenance-peer.py",
                "node-config/maintenance/policy.json",
                "apps/_shared/http-addon-owned/source.json"}
    require(set(files) == expected == set(transport["plan"]["source_bindings"]), "Unexpected standby source bundle")
    return transport, files


def worker_write(directory, state):
    for path, mode in ((directory / "state.json", 0o600),
                       (Path("/var/lib/node-maintenance-cold-worker-status") / (state["contract"]["run_id"] + ".json"), 0o444)):
        pending = path.with_suffix(".next")
        if pending.exists():
            meta = pending.lstat()
            require(stat.S_ISREG(meta.st_mode) and meta.st_uid == 0, "Untrusted worker pending record")
            pending.unlink()
        with pending.open("x") as stream:
            os.fchmod(stream.fileno(), mode)
            stream.write(json.dumps(state, sort_keys=True))
            stream.flush()
            os.fsync(stream.fileno())
        os.replace(pending, path)
        fd = os.open(path.parent, os.O_RDONLY | os.O_DIRECTORY)
        try:
            os.fsync(fd)
        finally:
            os.close(fd)


def worker_transaction(directory, callback):
    import fcntl
    with (directory / "mutex").open("a") as stream:
        fcntl.flock(stream, fcntl.LOCK_EX)
        state = json.loads((directory / "state.json").read_text())
        result = callback(state)
        worker_write(directory, state)
        return result


def worker_runtime(run_id):
    require(os.geteuid() == 0 and platform.machine() == "x86_64" and socket.gethostname() == "rishik-worker2"
            and re.fullmatch("[a-f0-9]{32}", run_id), "Exact root worker standby required")
    directory = Path("/var/lib/node-maintenance-cold-worker") / run_id
    for path in (directory.parent, directory, directory / "state.json", directory / "plan.json"):
        meta = path.lstat()
        require(meta.st_uid == 0 and not stat.S_ISLNK(meta.st_mode)
                and stat.S_IMODE(meta.st_mode) in (0o700, 0o600), "Worker root artifact drift")
    state = json.loads((directory / "state.json").read_text())
    plan = json.loads((directory / "plan.json").read_text())
    require(set(state["contract"]["source_bindings"]) == {
        ".github/scripts/node-maintenance.py", ".github/scripts/node-maintenance-host.py",
        ".github/scripts/node-maintenance-cold.py", ".github/scripts/node-maintenance-peer.py",
        "node-config/maintenance/policy.json",
        "apps/_shared/http-addon-owned/source.json"}, "Worker bound source set drift")
    for name, expected in state["contract"]["source_bindings"].items():
        path = directory / name
        meta = path.lstat()
        require(stat.S_ISREG(meta.st_mode) and meta.st_uid == 0 and stat.S_IMODE(meta.st_mode) == 0o600
                and digest(path) == expected, "Worker standby bound source drift")
    if "__file__" in globals():
        require(Path(__file__).resolve() == directory / ".github/scripts/node-maintenance-host.py",
                "Standby must execute staged exact helper, not a caller command")
    else:
        require(hashlib.sha256(globals().get("__maintenance_host_source__", "").encode()).hexdigest()
                == state["contract"]["source_bindings"][".github/scripts/node-maintenance-host.py"],
                "Deferred reboot must execute the exact staged helper")
    cold = load_cold(directory / ".github/scripts/node-maintenance-cold.py")
    policy = json.loads((directory / "node-config/maintenance/policy.json").read_text())
    contract = cold.worker_contract(plan, policy, run_id, state["contract"]["host_step_id"],
                                    state["contract"]["started_epoch"], state["contract"]["deadline_epoch"])
    cold.verify_worker(state, contract)
    require(cold.source_bindings() == contract["source_bindings"], "Worker source bundle drift")
    return directory, contract


def worker_unit_status(unit):
    value = command(["systemctl", "show", unit, "-p", "LoadState", "-p", "ActiveState",
                     "-p", "MainPID", "-p", "ControlPID", "-p", "Job", "-p", "User",
                     "-p", "FragmentPath", "-p", "ControlGroup", "-p", "Unit"])
    return dict(v.split("=", 1) for v in value.splitlines())


def worker_unit_digest(unit):
    path = Path("/run/systemd/system") / unit
    if path.exists():
        meta = path.lstat()
        require(stat.S_ISREG(meta.st_mode) and meta.st_uid == 0 and stat.S_IMODE(meta.st_mode) == 0o644,
                "Worker standby unit ownership drift")
        return digest(path)
    return None


def worker_status(run_id):
    require(re.fullmatch("[a-f0-9]{32}", run_id), "Invalid worker status identifier")
    record = cold_public(Path("/var/lib/node-maintenance-cold-worker-status") / (run_id + ".json"))
    unit = "node-maintenance-cold-worker-" + run_id
    return {"record": record, "unit": worker_unit_status(unit + ".service"),
            "unit_sha256": worker_unit_digest(unit + ".service"),
            "finalizer": {
                "service": worker_unit_status(unit + "-finalize.service"),
                "timer": worker_unit_status(unit + "-finalize.timer"),
                "service_sha256": worker_unit_digest(unit + "-finalize.service"),
                "timer_sha256": worker_unit_digest(unit + "-finalize.timer"),
            }}


def worker_cgroup_empty(unit, native):
    group = "/system.slice/" + unit
    require(native["ControlGroup"] in ("", group), "Finalizer worker cgroup identity drift")
    # Never infer emptiness from an empty systemctl ControlGroup property.
    # Linux cannot remove a cgroup containing tasks; populated covers descendants.
    path = Path("/sys/fs/cgroup") / group.lstrip("/")
    try:
        meta = path.lstat()
    except FileNotFoundError:
        require(not native["ControlGroup"], "Finalizer referenced worker cgroup is missing")
        return group
    require(stat.S_ISDIR(meta.st_mode) and meta.st_uid == 0,
            "Finalizer worker cgroup ownership drift")
    events = dict(line.split() for line in (path / "cgroup.events").read_text().splitlines())
    require(events.get("populated") == "0", "Finalizer worker/package descendants still active; no acknowledgment")
    return group


def worker_quiescent(contract, cold):
    unit = "node-maintenance-cold-worker-" + contract["run_id"] + ".service"
    native = worker_unit_status(unit)
    require(worker_unit_digest(unit) == cold.sha(cold.worker_unit(contract).encode())
            and native["LoadState"] == "loaded" and native["ActiveState"] in ("inactive", "failed")
            and native["MainPID"] == "0" and native["ControlPID"] == "0" and native["Job"] == "0"
            and native["User"] == "root" and native["FragmentPath"] == "/run/systemd/system/" + unit,
            "Finalizer requires exact loaded dead worker unit with no queued claim")
    group = worker_cgroup_empty(unit, native)
    # A claimed process could die after scheduling its separately scoped reboot.
    for suffix in (".timer", ".service"):
        name = "node-maintenance-reboot-" + contract["host_step_id"] + suffix
        reboot = worker_unit_status(name)
        require(reboot["LoadState"] in ("loaded", "not-found")
                and reboot["ActiveState"] in ("inactive", "failed") and reboot["Job"] == "0",
                "Finalizer reboot unit/children still active or unknown")
        if suffix == ".service":
            require(reboot["MainPID"] == "0" and reboot["ControlPID"] == "0",
                    "Finalizer reboot service children still active or unknown")
            worker_cgroup_empty(name, reboot)
    for unit in ("reboot.target", "shutdown.target"):
        shutdown = worker_unit_status(unit)
        require(shutdown["LoadState"] == "loaded" and shutdown["ActiveState"] == "inactive"
                and shutdown["Job"] == "0", "Finalizer native shutdown job pending or unknown")
    return group


def cold_worker_finalize(run_id):
    directory, contract = worker_runtime(run_id)
    cold = load_cold(directory / ".github/scripts/node-maintenance-cold.py")
    unit = "node-maintenance-cold-worker-" + run_id + "-finalize"
    def finalize(state):
        cold.verify_worker(state, contract)
        requested = time.time()
        require(requested >= state["last_epoch"] >= contract["started_epoch"], "Finalizer local clock rollback")
        peer = cold_peer_status(run_id)
        now = time.time()
        require(now >= requested, "Finalizer response clock rollback")
        standby_peer_binding(contract, peer)
        proof = worker_status(run_id)
        # This proof is written only by a real invocation of the separately
        # registered root finalizer, never by observing a failed standby.
        proof["record"]["finalizer_verified_epoch"] = now
        cold.worker_finalizer_ready(proof, contract, now)
        require(proof["finalizer"]["service"]["MainPID"] == str(os.getpid()),
                "Finalizer must execute in its exact independent root unit")
        if state["phase"] in ("staged", "waiting", "claimed"):
            if not (peer.get("host_slot_cancelled") is True
                    and peer["mode"] == "recovering" and peer.get("host_reboot_authorized") is False):
                state.update(finalizer_verified_epoch=now, last_epoch=now)
                return False
            require(contract["started_epoch"] <= peer["last_epoch"] <= now, "Finalizer controller clock drift")
            require(Path("/proc/sys/kernel/random/boot_id").read_text().strip()
                    == contract["bindings"]["rishik-worker2"]["boot_id"]
                    and platform.release() == contract["bindings"]["rishik-worker2"]["kernel"],
                    "Finalizer original worker boot/kernel drift")
            group = worker_quiescent(contract, cold)
            # The worker mutex remains held across proof and acknowledgment.
            # A late claimant cannot change phase after these native checks.
            state.update(phase="failed" if state["phase"] == "claimed" else "cancelled",
                         finalizer_ack={"contract_sha256": state["contract_sha256"],
                                        "worker_cgroup": group, "revoked_epoch": now})
        require(state["phase"] in ("cancelled", "failed", "packages-complete"), "Unknown finalizer worker phase")
        state.update(finalizer_verified_epoch=now, last_epoch=now)
        return True
    terminal = worker_transaction(directory, finalize)
    if terminal:
        command(["systemctl", "stop", unit + ".timer"])


def standby_clock(state, contract, previous):
    now = time.time()
    require(now >= previous >= state["last_epoch"] >= contract["started_epoch"], "Standby local clock rollback")
    require(now < contract["deadline_epoch"] - 7200, "Standby clock/reserve/expiry revoked")
    return now


def standby_tick(directory, contract):
    # Claim and cancellation acknowledgments share one durable local mutex.
    # Recovery never equates an inactive/missing unit with this acknowledgment.
    def tick(state):
        require(state["phase"] in ("staged", "waiting"), "Standby is single-shot/terminal; no replay")
        requested = standby_clock(state, contract, state["last_epoch"])
        require(Path("/proc/sys/kernel/random/boot_id").read_text().strip()
                == contract["bindings"]["rishik-worker2"]["boot_id"]
                and platform.release() == contract["bindings"]["rishik-worker2"]["kernel"],
                "Standby original worker boot/kernel drift")
        peer = cold_peer_status(contract["run_id"])
        now = standby_clock(state, contract, requested)
        decision = standby_peer(contract, peer, now)
        peer_epoch = now
        if state["phase"] == "staged" or decision == "claim":
            before = audit()
            require(before["boot_id"] == contract["bindings"]["rishik-worker2"]["boot_id"]
                    and before["kernel"] == contract["bindings"]["rishik-worker2"]["kernel"]
                    and before["pending_packages"] == contract["os_payload"]["pending_packages"],
                    "Worker standby package/boot/kernel binding drift")
            state["original_host"] = {k: before[k] for k in ("boot_id", "kernel", "pending_packages")}
        if decision == "claim":
            # Recheck after the potentially slow read-only package simulation.
            requested = standby_clock(state, contract, now)
            peer = cold_peer_status(contract["run_id"])
            now = standby_clock(state, contract, requested)
            require(standby_peer(contract, peer, now, launching=True) == "claim",
                    "Standby slot revoked during claim")
            peer_epoch = now
        now = standby_clock(state, contract, now)
        state.update(phase="claimed" if decision == "claim" else decision, last_epoch=now,
                     ready_epoch=now, peer_epoch=peer_epoch, pid=os.getpid())
        return decision
    return worker_transaction(directory, tick)


def cold_standby(run_id):
    directory, contract = worker_runtime(run_id)
    try:
        while True:
            # Revalidate exact source on every poll, including the final claim.
            worker_runtime(run_id)
            decision = standby_tick(directory, contract)
            if decision == "cancelled":
                return
            if decision == "claim":
                main({**contract["os_payload"], "contract_sha256": hashlib.sha256(
                    json.dumps(contract, sort_keys=True, separators=(",", ":")).encode()).hexdigest()})
                return
            time.sleep(5)
    except (RuntimeError, OSError, ValueError, subprocess.TimeoutExpired) as error:
        def failed(state):
            state.update(phase="failed" if state["phase"] in ("claimed", "packages-complete") else "cancelled",
                         failure_type=type(error).__name__)
        worker_transaction(directory, failed)
        raise


def cold_worker_register(payload):
    transport, files = read_transport(payload)
    run_id = payload["run_id"]
    directory = Path("/var/lib/node-maintenance-cold-worker") / run_id
    directory.parent.mkdir(mode=0o700, exist_ok=True)
    require(not directory.parent.is_symlink() and directory.parent.stat().st_uid == 0
            and stat.S_IMODE(directory.parent.stat().st_mode) == 0o700, "Worker standby parent is not trusted")
    directory.mkdir(mode=0o700, exist_ok=False)
    for name, encoded in files.items():
        value = base64.b64decode(encoded)
        require(hashlib.sha256(value).hexdigest() == transport["plan"]["source_bindings"][name],
                "Worker source transport hash drift")
        path = directory / name
        path.parent.mkdir(mode=0o700, parents=True, exist_ok=True)
        with path.open("xb") as output:
            os.fchmod(output.fileno(), 0o600)
            output.write(value)
            output.flush()
            os.fsync(output.fileno())
    with (directory / "plan.json").open("x") as output:
        os.fchmod(output.fileno(), 0o600)
        output.write(json.dumps(transport["plan"]))
        output.flush()
        os.fsync(output.fileno())
    cold = load_cold(directory / ".github/scripts/node-maintenance-cold.py")
    policy = json.loads((directory / "node-config/maintenance/policy.json").read_text())
    contract = cold.worker_contract(transport["plan"], policy, run_id, transport["host_step_id"],
                                    payload["started_epoch"], payload["deadline_epoch"])
    require(contract["plan_sha256"] == transport["approval"], "Worker approval drift")
    load_peer().register_run(transport["plan"], run_id, transport["host_step_id"],
                             payload["started_epoch"], payload["deadline_epoch"])
    require(standby_peer(contract, cold_peer_status(run_id), time.time()) == "waiting",
            "Controller must be registered/inert before worker setup")
    status_directory = Path("/var/lib/node-maintenance-cold-worker-status")
    status_directory.mkdir(mode=0o755, exist_ok=True)
    require(not status_directory.is_symlink() and status_directory.stat().st_uid == 0
            and stat.S_IMODE(status_directory.stat().st_mode) == 0o755, "Worker public record directory drift")
    state = {"contract": contract, "contract_sha256": cold.sha(cold.canonical(contract)),
             "phase": "staged", "last_epoch": time.time(), "pid": 0}
    worker_write(directory, state)
    start_worker_standby(directory, contract, cold)
    print("Exact worker2 standby is live and inert; no package or application was changed.")


def start_worker_standby(directory, contract, cold):
    run_id = contract["run_id"]
    unit = "node-maintenance-cold-worker-" + run_id + ".service"
    units = {unit: cold.worker_unit(contract), **cold.worker_finalizer_units(contract)}
    for name, value in units.items():
        with (Path("/run/systemd/system") / name).open("x") as output:
            os.fchmod(output.fileno(), 0o644)
            output.write(value)
            output.flush()
            os.fsync(output.fileno())
    command(["systemctl", "daemon-reload"])
    command(["systemctl", "start", unit.removesuffix(".service") + "-finalize.timer"])
    command(["systemctl", "start", unit.removesuffix(".service") + "-finalize.service"])
    cold.worker_finalizer_ready(worker_status(run_id), contract, time.time())
    command(["systemctl", "start", "--no-block", unit])
    end = time.monotonic() + 60
    while True:
        proof = worker_status(run_id)
        if proof["record"]["phase"] == "waiting":
            cold.worker_ready(proof, contract, time.time())
            break
        require(proof["record"]["phase"] == "staged" and time.monotonic() < end,
                "Worker standby did not publish actual live readiness")
        time.sleep(1)


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
                ".github/scripts/node-maintenance-cold.py", ".github/scripts/node-maintenance-peer.py",
                "node-config/maintenance/policy.json",
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
        "host_packages_complete": False, "host_reboot_authorized": False,
    }
    cold_module = load_cold(directory / ".github/scripts/node-maintenance-cold.py")
    policy = json.loads((directory / "node-config/maintenance/policy.json").read_text())
    contract = cold_module.worker_contract(payload["plan"], policy, payload["run_id"], payload["host_step_id"],
                                          payload["started_epoch"], payload["deadline_epoch"])
    load_peer().register_run(payload["plan"], payload["run_id"], payload["host_step_id"],
                             payload["started_epoch"], payload["deadline_epoch"])
    state.update(worker_contract=contract, worker_contract_sha256=cold_module.sha(cold_module.canonical(contract)),
                 worker_ready_verified=False)
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


def cold_start(c, cold, payload):
    load_peer().plan_scope(c.plan)
    def ready(state):
        c.gate(state)
        require(state["mode"] == "registered" and time.time() < state["deadline_epoch"] - 7200,
                "Cold execution already started or reserve reached")
        timer = "node-maintenance-cold-" + payload["run_id"] + "-guard.timer"
        require(command(["systemctl", "is-active", timer]) == "active"
                and command(["systemctl", "is-enabled", timer]) == "enabled",
                "Independent recovery guard disappeared before first app stop")
        require(0 <= time.time() - state.get("guard_verified_epoch", 0) < 60,
                "Actual native guard readiness expired before first pause")
        cold.worker_ready(c.api.worker_status(c.policy, state["run_id"]), state["worker_contract"], time.time())
    c.journal.transaction(ready)
    # Read-only checks need not hold the mutation mutex and starve the
    # independent guard heartbeat. Final ready() rejects concurrent recovery.
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
            and host["kernel"] == c.plan["bindings"]["rishik-worker2"]["kernel"]
            and host["pending_packages"] == c.plan["bindings"]["rishik-worker2"]["pending_packages"],
            "Host/package drift before first pause")
    cold.cold_storage(c.plan, c.policy, c.api)
    cold.native_gate(c.plan, c.policy, c.api)
    c.autoscaling_restored()
    def start(state):
        ready(state)
        state.update(mode="disrupting", worker_ready_verified=True)
    c.journal.transaction(start)
    print("Manual authentication complete; autonomous bound worker2 cold maintenance and protected recovery "
          "are ready. No normal late sudo is required.", flush=True)
    command(["systemctl", "start", "--no-block", "node-maintenance-cold-" + payload["run_id"] + ".service"])


def cold_control(payload):
    directory = Path("/var/lib/node-maintenance-cold") / payload["run_id"]
    cold = load_cold(directory / ".github/scripts/node-maintenance-cold.py")
    c = cold.load_runtime(directory)
    operation = payload["operation"]
    if operation == "cold-start":
        try:
            cold_start(c, cold, payload)
        except (RuntimeError, OSError, ValueError, subprocess.TimeoutExpired):
            c.request_recovery("cold-start-preflight-or-executor-failed")
            command(["systemctl", "start", "--no-block", "node-maintenance-cold-" + payload["run_id"] + "-guard.service"])
            raise
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
    if operation == "cold-peer-proof":
        require(set(payload) == {"operation"}, "Peer proof takes no caller path/source/payload")
        print(json.dumps(load_peer().local_proof()))
        return
    if operation == "cold-worker-status":
        print(json.dumps(worker_status(payload["run_id"])))
        return
    if operation == "cold-standby":
        require(set(payload) == {"operation", "run_id"}, "Standby accepts only its bound run, no command/payload")
        cold_standby(payload["run_id"])
        return
    if operation == "cold-worker-finalize":
        require(set(payload) == {"operation", "run_id"}, "Finalizer accepts only its bound run, no command/payload")
        cold_worker_finalize(payload["run_id"])
        return
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
        if payload.get("contract_sha256"):
            directory, contract = worker_runtime(payload["cold_run_id"])
            require(payload == {**contract["os_payload"], "operation": "reboot",
                                "contract_sha256": hashlib.sha256(json.dumps(
                                    contract, sort_keys=True, separators=(",", ":")).encode()).hexdigest()}
                    and json.loads((directory / "state.json").read_text())["phase"] == "packages-complete",
                    "Deferred reboot exact payload/terminal receipt drift")
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
        elif operation == "cold-worker-register":
            require(state["name"] == "rishik-worker2", "Standby belongs only on selected worker2")
            cold_worker_register(payload)
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
        if payload.get("contract_sha256"):
            worker_directory, contract = worker_runtime(payload["cold_run_id"])
            require(payload == {**contract["os_payload"], "contract_sha256": hashlib.sha256(json.dumps(
                        contract, sort_keys=True, separators=(",", ":")).encode()).hexdigest()}
                    and json.loads((worker_directory / "state.json").read_text())["phase"] == "claimed",
                    "Standby only executes its exact claimed OS payload")
        result = os_upgrade(payload)
    else:
        require(operation == "rke2", "Unknown operation")
        result = rke2_upgrade(payload, directory)
    result.update({"run_id": payload["run_id"], "name": state["name"], "operation": operation,
                   "recovery": recovery, "completed_epoch": time.time()})
    if payload.get("contract_sha256"):
        require(operation == "os" and payload["name"] == "rishik-worker2", "Exact standby OS receipt required")
        result["contract_sha256"] = payload["contract_sha256"]
        directory = Path("/var/lib/node-maintenance-cold-worker") / payload["cold_run_id"]
        def receipt(s):
            require(s["phase"] == "claimed" and s["contract_sha256"] == payload["contract_sha256"]
                    and not s.get("receipt"), "Worker package receipt is terminal/single-shot")
            s.update(phase="packages-complete", receipt=result)
        worker_transaction(directory, receipt)
    else:
        receipt = Path("/var/tmp") / ("node-maintenance-" + payload["run_id"] + ".json")
        with receipt.open("x") as output:
            output.write(json.dumps(result))
            os.fchmod(output.fileno(), 0o444)
    if operation == "os":
        # APT may run longer than the disruption window. Check again both now and
        # in the deferred unit; never kill dpkg or reboot into the recovery reserve.
        schedule_reboot(payload)
    print("Bound package step complete; controller must verify new boot and protected recovery."
          if payload.get("contract_sha256") else
          "Host step complete; workstation must verify recovery before continuing.")


if __name__ == "__main__":
    try:
        main(json.loads(base64.b64decode(sys.argv[1])))
    except Exception as error:
        print(f"STOP: {error}", file=sys.stderr)
        sys.exit(1)
