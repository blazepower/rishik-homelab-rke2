#!/usr/bin/env python3
"""Operator-authorized terminal cancellation of ONE never-started historical run."""

import argparse
import base64
from contextlib import contextmanager
import hashlib
import importlib.util
import json
import math
import os
from pathlib import Path
import platform
import re
import socket
import stat
import subprocess
import time


RUN = "53a3a0a510054a5dbca6bccb677d3148"
STEP = "627bb24ba72a4e17accbbf4a012b16b5"
LOCK_UID = "98379fb1-bf6d-4536-9a1a-d630a0333a30"
START = 1795588445.0377986
DEADLINE = 1795631645.0377986
APPROVAL = "024a064a9d9d59ee95ec70d787df80fe7336b33dc13436ef48e97931a2e7666f"
CONTRACT_SHA = "1d8b75715fbdd176da8b307478ebb17d4523ab7f827e5eb2b29eb3a469a83cf3"
ORIGINAL_COMMIT = "66a10ed8c8a959009ff2f39549d8637d383ff79c"
SETUP = "582d351db706417aab8492d945598f2d"
SETUP_START, SETUP_EXPIRY = 1795586345, 1795672745
SERVER, WORKER = "rishik-controller", "rishik-worker2"
IPS = {SERVER: "192.168.1.10", WORKER: "192.168.1.12"}
HASHES = {
    ".github/scripts/node-maintenance.py": "eb0a5c4e2e7592b234d2f66a8506ff70fcc3fca0e78e5b883f33188f1202e724",
    ".github/scripts/node-maintenance-host.py": "1675a4f4915ccd6fbeb53c1fc88b091b765f00cbd6e0028692cca7c2ea00b511",
    ".github/scripts/node-maintenance-cold.py": "66f82606c195dfbe7b3fde76a67ecf586a772ff4b69c4e6e11c67e3831d86448",
    ".github/scripts/node-maintenance-peer.py": "35f71ef263af3c883062cf6de6d27cc9f0c89277473ff4582eeb2e62ed0457cf",
    "node-config/maintenance/policy.json": "682dc06167c95a399d52d29728fb0de390d16fb9e934afdd08f5a626b58d2758",
    "apps/_shared/http-addon-owned/source.json": "ab64729edfef1f6af76e2cf681ef7b54da4232b65251dda042525404d5d547e5",
}
BASE = Path("/var/lib/node-maintenance-operator-recovery") / RUN
PUBLIC = Path("/var/lib/node-maintenance-operator-recovery-status") / RUN
PEER = Path("/etc/node-maintenance-peer") / SETUP
SYSTEM = Path("/etc/systemd/system")
RUNTIME_SYSTEM = Path("/run/systemd/system")
CGROUP = Path("/sys/fs/cgroup")
MOUNTINFO = Path("/proc/self/mountinfo")
BOOT = Path("/proc/sys/kernel/random/boot_id")
OS_RELEASE = Path("/etc/os-release")
PROC_LOCKS = Path("/proc/locks")
PACKAGE_LOCKS = tuple(Path(p) for p in (
    "/var/lib/dpkg/lock", "/var/lib/dpkg/lock-frontend",
    "/var/lib/apt/lists/lock", "/var/cache/apt/archives/lock"))
LOCK_URI = "/api/v1/namespaces/kube-system/configmaps/operator-node-maintenance"
LOCK_LIST_URI = "/api/v1/namespaces/kube-system/configmaps?fieldSelector=metadata.name%3Doperator-node-maintenance"
SIGN_NAMESPACE = "node-maintenance-cancel-unstarted-" + RUN


def require(value, message):
    if not value:
        raise RuntimeError(message)


def canonical(value):
    return json.dumps(value, sort_keys=True, separators=(",", ":")).encode()


def sha(value):
    return hashlib.sha256(value).hexdigest()


def checked(path, modes=(0o600,), directory=False):
    meta = path.lstat()
    require(meta.st_uid == 0 and stat.S_IMODE(meta.st_mode) in modes
            and (stat.S_ISDIR(meta.st_mode) if directory else stat.S_ISREG(meta.st_mode)),
            "Root artifact ownership/type/mode drift")
    return path


def sync_directory(path):
    fd = os.open(path, os.O_RDONLY | os.O_DIRECTORY)
    try:
        os.fsync(fd)
    finally:
        os.close(fd)


def create(path, data, mode=0o600):
    pending = path.with_name(path.name + ".creating")
    if pending.exists():
        checked(pending, (0o600, 0o644, 0o444)).unlink()
    with pending.open("xb") as stream:
        os.fchmod(stream.fileno(), mode)
        stream.write(data)
        stream.flush()
        os.fsync(stream.fileno())
    os.link(pending, path)
    pending.unlink()
    sync_directory(path.parent)


def atomic(path, data, mode=0o600):
    if path.exists():
        checked(path, (0o600, 0o644, 0o444))
    pending = path.with_name(path.name + ".updating")
    if pending.exists():
        checked(pending, (0o600, 0o444)).unlink()
    create(pending, data, mode)
    os.replace(pending, path)
    sync_directory(path.parent)


def run(args, data=None, timeout=60):
    result = subprocess.run(args, input=data, stdout=subprocess.PIPE, stderr=subprocess.PIPE,
                            timeout=timeout, env={"PATH": "/usr/sbin:/usr/bin:/sbin:/bin", "LC_ALL": "C"})
    require(result.returncode == 0, "Native command refused; no inferred success")
    return result.stdout


@contextmanager
def mutex(path):
    import fcntl
    fd = os.open(path, os.O_CREAT | os.O_RDWR | os.O_NOFOLLOW, 0o600)
    with os.fdopen(fd, "a") as stream:
        checked(path)
        end = time.monotonic() + 90
        while True:
            try:
                fcntl.flock(stream, fcntl.LOCK_EX | fcntl.LOCK_NB)
                break
            except BlockingIOError:
                require(time.monotonic() < end, "Original mutex remains busy; no process kill")
                time.sleep(0.2)
        yield


def read_json(path, modes=(0o600,)):
    return json.loads(checked(path, modes).read_bytes())


def no_job(native):
    return "Job" in native and native["Job"] in ("", "0")


def pid(native, field):
    value = native[field]
    require(isinstance(value, str) and re.fullmatch(r"0|[1-9][0-9]*", value), "Missing/malformed native PID")
    return int(value)


class Recovery:
    def __init__(self, reviewed_sha):
        self.node = socket.gethostname()
        require(os.geteuid() == 0 and self.node in IPS and platform.system() == "Linux"
                and platform.machine() == "x86_64", "Original root console/node/platform required")
        require(Path(__file__).resolve() == BASE / "helper.py"
                and sha(checked(BASE / "helper.py", (0o444,)).read_bytes()) == reviewed_sha,
                "Actual reviewed helper bytes/path required")
        self.source = reviewed_sha
        self.other = WORKER if self.node == SERVER else SERVER
        self.original = Path("/var/lib/node-maintenance-cold" if self.node == SERVER
                             else "/var/lib/node-maintenance-cold-worker") / RUN
        checked(self.original, (0o700,), directory=True)
        for name, digest in HASHES.items():
            require(sha(checked(self.original / name).read_bytes()) == digest, "Original bundle drift")
        self.plan = read_json(self.original / "plan.json")
        require(sha(canonical(self.plan)) == APPROVAL and self.plan["source_bindings"] == HASHES,
                "Original approved plan drift")
        self.policy = read_json(self.original / "node-config/maintenance/policy.json")
        spec = importlib.util.spec_from_file_location("exact_old_cold", self.original / ".github/scripts/node-maintenance-cold.py")
        old = importlib.util.module_from_spec(spec)
        spec.loader.exec_module(old)
        self.contract = old.worker_contract(self.plan, self.policy, RUN, STEP, START, DEADLINE)
        require(sha(canonical(self.contract)) == CONTRACT_SHA, "Original worker contract drift")
        self.old = old
        self.verify_peer_setup()
        self.verify_host()
        self.pub = PUBLIC / self.node
        self.local = BASE / "authority.json"
        self.client = "/var/lib/rancher/rke2/bin/kubectl"
        if self.node == SERVER:
            require(sha(checked(Path(self.client), (0o755,)).read_bytes())
                    == self.plan["controller_client"]["sha256"], "Original native API client drift")

    def verify_peer_setup(self):
        checked(PEER, (0o755,), directory=True)
        require(read_json(PEER.parent / "current.json", (0o444,)) == {"setup_id": SETUP},
                "Original peer setup pointer drift")
        config = read_json(PEER / "config.json", (0o444,))
        require(config["node"] == self.node and config["peer"] == self.other and config["setup_id"] == SETUP
                and config["started_epoch"] == SETUP_START and config["expires_epoch"] == SETUP_EXPIRY
                and sha(canonical(config)) == self.plan["peer_scope"][self.node]["probe"]["config_sha256"],
                "Original peer config/scope drift; no rebinding")
        for name in ("node-maintenance-host.py", "node-maintenance-peer.py"):
            require(config["source_sha256"][name] == HASHES[".github/scripts/" + name]
                    and sha(checked(PEER / name, (0o444,)).read_bytes()) == HASHES[".github/scripts/" + name],
                    "Original installed peer sources drift")
        grant = read_json(PEER / "run.json", (0o444,))
        require(grant == {k: self.contract[k] for k in
                         ("run_id", "host_step_id", "plan_sha256", "source_bindings", "started_epoch", "deadline_epoch")},
                "Original immutable grant drift")
        self.other_key = config["peer_host_key"]
        require(self.key_fingerprint(self.other_key) == config["peer_host_fingerprint"],
                "Original trusted peer host fingerprint drift")
        local_key = checked(Path("/etc/ssh/ssh_host_ed25519_key.pub"), (0o644,)).read_text().split()
        self.local_key = " ".join(local_key[:2])
        require(self.key_fingerprint(self.local_key) == self.plan["peer_scope"][self.other]["peer_host_fingerprint"],
                "Original local host signing identity drift")
        checked(Path("/etc/ssh/ssh_host_ed25519_key"), (0o600,))

    @staticmethod
    def key_fingerprint(key):
        require(re.fullmatch(r"ssh-ed25519 [A-Za-z0-9+/]+={0,2}", key), "Exact Ed25519 host identity required")
        data = base64.b64decode(key.split()[1], validate=True)
        require(len(data) == 51 and data[:19] == b"\0\0\0\x0bssh-ed25519\0\0\0\x20",
                "Host public-key wire shape drift")
        return "SHA256:" + base64.b64encode(hashlib.sha256(data).digest()).decode().rstrip("=")

    def verify_host(self):
        require(socket.gethostname() == self.node and platform.system() == "Linux"
                and platform.machine() == "x86_64", "Original node/platform changed")
        original = self.plan["bindings"][self.node]
        require(BOOT.read_text().strip() == original["boot_id"]
                and platform.release() == original["kernel"], "Original boot/kernel drift")
        release = dict(row.split("=", 1) for row in OS_RELEASE.read_text().splitlines() if "=" in row)
        require(release.get("ID", "").strip('"') == "ubuntu"
                and release.get("VERSION_ID", "").strip('"') == "26.04", "Original Ubuntu identity drift")
        addresses = json.loads(run(["/usr/sbin/ip", "-j", "-4", "addr", "show"]))
        require(any(a.get("local") == IPS[self.node] for row in addresses for a in row["addr_info"]),
                "Original local node IP absent")

    def state(self):
        self.verify_host()
        require(sha(canonical(read_json(self.original / "plan.json"))) == APPROVAL, "Original plan changed")
        for name, digest in HASHES.items():
            require(sha(checked(self.original / name).read_bytes()) == digest, "Original source drift")
        state = read_json(self.original / "state.json")
        require(time.time() >= state["last_epoch"] >= START, "Original clock rollback")
        self.clock()
        if self.node == SERVER:
            require(state["run_id"] == RUN and state["host_step_id"] == STEP and state["plan_sha256"] == APPROVAL
                    and state["started_epoch"] == START and state["deadline_epoch"] == DEADLINE
                    and state["worker_contract"] == self.contract and state["worker_contract_sha256"] == CONTRACT_SHA,
                    "Original controller binding drift")
            require(state["mode"] == "recovering" and state["host_slot_cancelled"] is True
                    and state["completed"] is False and isinstance(state["mutations"], dict)
                    and len(state["mutations"]) == 0 and state["guard_registered"] is True
                    and state["worker_ready_verified"] is False, "Not cancelled-unstarted with zero mutations")
            for name in ("host_started", "host_complete", "host_packages_complete", "host_reboot_authorized"):
                require(state[name] is False, "Host action ever authorized/started")
            for name in ("host_started_epoch", "host_packages_complete_epoch", "host_receipt", "drain_pid",
                         "cordon_intent", "uncordoned", "drain_started", "drain_complete"):
                require(name not in state, "Unknown/ever-started drain or host action")
        else:
            require(state["contract"] == self.contract and state["contract_sha256"] == CONTRACT_SHA
                    and type(state["pid"]) is int and state["pid"] == 0, "Worker identity/ever-started PID drift")
            require(state["phase"] == "staged" or (state["phase"] == "cancelled"
                    and state.get("operator_cancellation") == self.cancellation_tag()),
                    "Claimed/started/foreign worker terminal state")
            for name in ("receipt", "finalizer_ack", "finalizer_verified_epoch", "ready_epoch", "peer_epoch", "original_host"):
                require(name not in state, "Worker has prior start/claim/finalizer evidence")
        return state

    def clock(self):
        path = BASE / "operator-clock.json"
        now = time.time()
        require(math.isfinite(now) and now >= START, "Operator clock invalid")
        if path.exists():
            previous = read_json(path)
            require(set(previous) == {"source_sha256", "last_epoch"}
                    and previous["source_sha256"] == self.source
                    and type(previous["last_epoch"]) in (float, int)
                    and START <= previous["last_epoch"] <= now, "Operator clock rollback/drift")
            atomic(path, canonical({"source_sha256": self.source, "last_epoch": now}))
        else:
            create(path, canonical({"source_sha256": self.source, "last_epoch": now}))

    def cancellation_tag(self):
        return {"authority": "reviewed-cancelled-unstarted-operator", "source_sha256": self.source,
                "original_commit": ORIGINAL_COMMIT, "run_id": RUN}

    @contextmanager
    def locked(self):
        with mutex(BASE / "operator-mutex"):
            if self.node == SERVER:
                with mutex(self.original / "recovery-mutex"), mutex(self.original / "mutex"):
                    yield
            else:
                with mutex(self.original / "mutex"):
                    yield

    def native(self, unit):
        value = run(["/usr/bin/systemctl", "show", unit, "-p", "LoadState", "-p", "ActiveState",
                     "-p", "Job", "-p", "MainPID", "-p", "ControlPID", "-p", "User",
                     "-p", "FragmentPath", "-p", "ControlGroup", "-p", "DropInPaths", "-p", "Unit"]).decode()
        result = dict(line.split("=", 1) for line in value.splitlines())
        require(no_job(result), "Missing/malformed/queued native job")
        return result

    def cgroup(self, unit, native):
        group = "/system.slice/" + unit
        require(native["ControlGroup"] in ("", group), "Foreign native cgroup")
        mounts = [row.split(" - ", 1) for row in MOUNTINFO.read_text().splitlines()]
        require(sum(parts[0].split()[3:5] == ["/", "/sys/fs/cgroup"] and parts[1].split()[0] == "cgroup2"
                    for parts in mounts) == 1
                and not any(parts[0].split()[4].startswith("/sys/fs/cgroup/")
                            for parts in mounts), "Untrusted/non-cgroup2 process namespace")
        checked(CGROUP / "system.slice", (0o755,), directory=True)
        path = CGROUP / group.lstrip("/")
        try:
            checked(path, (0o755,), directory=True)
        except FileNotFoundError:
            require(native["ControlGroup"] == "", "Referenced cgroup missing")
            return
        events = dict(line.split() for line in checked(path / "cgroup.events", (0o444, 0o644)).read_text().splitlines())
        require(events.get("populated") == "0", "Active package/process descendants; no cancellation")

    def unit_bodies(self):
        if self.node == WORKER:
            name = "node-maintenance-cold-worker-" + RUN
            return {name + ".service": self.old.worker_unit(self.contract),
                    **self.old.worker_finalizer_units(self.contract)}
        name = "node-maintenance-cold-" + RUN
        executable = f"/usr/bin/python3 -B {self.original}/.github/scripts/node-maintenance-cold.py"
        return {
            name + ".service": ("[Unit]\nDescription=Bound worker2 cold coordinator\n[Service]\nType=oneshot\n"
                               f"ExecStart={executable} execute --directory {self.original}\n"
                               "TimeoutStartSec=infinity\nSendSIGKILL=no\nKillMode=process\n"),
            name + "-guard.service": ("[Unit]\nDescription=Worker2 cold original-state recovery guard\n[Service]\nType=oneshot\n"
                                     f"ExecStart={executable} guard --directory {self.original}\n"
                                     "TimeoutStartSec=infinity\nSendSIGKILL=no\nKillMode=process\n"),
            name + "-guard.timer": ("[Unit]\nDescription=Independent worker2 reserve/interruption recovery\n[Timer]\n"
                                   "OnBootSec=30s\nOnUnitActiveSec=30s\n"
                                   f"Unit={name}-guard.service\n[Install]\nWantedBy=timers.target\n"),
        }

    def actor(self, unit, allow_running=False, inhibited=False):
        folder = RUNTIME_SYSTEM if self.node == WORKER else SYSTEM
        require(checked(folder / unit, (0o644,)).read_bytes() == self.unit_bodies()[unit].encode(),
                "Original exact unit source drift")
        native = self.native(unit)
        require(native["LoadState"] == "loaded" and native["FragmentPath"] == str(folder / unit),
                "Original loaded unit/fragment drift")
        expected_dropins = str(self.inhibit_path(unit)) if inhibited else ""
        require(native["DropInPaths"] == expected_dropins, "Foreign/unloaded actor inhibition")
        if unit.endswith(".timer"):
            require(native["Unit"] == unit.removesuffix(".timer") + ".service", "Timer target identity drift")
        require(native["ActiveState"] in (("inactive", "failed", "active", "activating") if allow_running
                                         else ("inactive", "failed")), "Native actor not idle")
        if unit.endswith(".service"):
            require(native["User"] in (("", "root") if self.node == SERVER else ("root",))
                    and pid(native, "ControlPID") == 0, "Native service identity/control PID drift")
            if allow_running and native["ActiveState"] in ("active", "activating"):
                require(pid(native, "MainPID") > 0, "Active actor has no genuine PID")
            else:
                require(pid(native, "MainPID") == 0, "Service still active")
                self.cgroup(unit, native)
        return native

    def worker_idle(self):
        self.actor("node-maintenance-cold-worker-" + RUN + ".service")
        for unit in (f"node-maintenance-{STEP}-os.service", f"node-maintenance-reboot-{STEP}.service",
                     f"node-maintenance-reboot-{STEP}.timer"):
            native = self.native(unit)
            require(native["LoadState"] == "not-found" and native["ActiveState"] == "inactive",
                    "Historical package/reboot unit exists; unstarted scope not proved")
            require(native["FragmentPath"] == "" and native["DropInPaths"] == "", "Unknown absent work unit identity")
            if unit.endswith(".service"):
                require(pid(native, "MainPID") == 0 and pid(native, "ControlPID") == 0, "Package/reboot PID active")
                self.cgroup(unit, native)
        for unit in ("reboot.target", "shutdown.target"):
            native = self.native(unit)
            require(native["LoadState"] == "loaded" and native["ActiveState"] == "inactive",
                    "Native shutdown/reboot pending")
        for unit in ("apt-daily.service", "apt-daily-upgrade.service"):
            native = self.native(unit)
            require(native["LoadState"] == "loaded" and native["ActiveState"] in ("inactive", "failed")
                    and pid(native, "MainPID") == 0 and pid(native, "ControlPID") == 0
                    and native["User"] in ("", "root"), "Native package service active/queued/unknown")
            self.cgroup(unit, native)
        inodes = set()
        for path in PACKAGE_LOCKS:
            meta = checked(path, (0o600, 0o640, 0o644)).stat()
            inodes.add((os.major(meta.st_dev), os.minor(meta.st_dev), meta.st_ino))
        for row in PROC_LOCKS.read_text().splitlines():
            match = re.findall(r"\b([0-9a-fA-F]+):([0-9a-fA-F]+):([0-9]+)\b", row)
            require(len(match) == 1, "Malformed native package-lock observation")
            major, minor, inode = match[0]
            require((int(major, 16), int(minor, 16), int(inode)) not in inodes,
                    "Native APT/dpkg lock held/queued; no package stop")

    def authority(self):
        if self.local.exists():
            value = read_json(self.local)
            require(set(value) == {"source_sha256", "run_id", "nonce"}
                    and value["source_sha256"] == self.source and value["run_id"] == RUN
                    and re.fullmatch("[a-f0-9]{32}", value["nonce"]),
                    "Foreign recovery authority artifact")
            return value
        value = {"source_sha256": self.source, "run_id": RUN, "nonce": os.urandom(16).hex()}
        create(self.local, canonical(value))
        return value

    def snapshot(self, name, path):
        destination = BASE / name
        if not destination.exists():
            create(destination, checked(path).read_bytes())
        else:
            checked(destination)

    def inhibit_path(self, unit):
        return SYSTEM / (unit + ".d") / "90-reviewed-cancel-unstarted.conf"

    def inhibit(self):
        unit = "node-maintenance-cold-" + RUN + "-guard.service"
        for name in self.unit_bodies():
            path = SYSTEM / name
            require(checked(path, (0o644,)).read_bytes() == self.unit_bodies()[name].encode(),
                    "Original controller unit changed before fencing")
            native = self.native(name)
            require(native["DropInPaths"] in (("", str(self.inhibit_path(unit))) if name == unit else ("",)),
                    "Foreign controller drop-in before fencing")
            dropins = SYSTEM / (name + ".d")
            if dropins.exists():
                checked(dropins, (0o755,), directory=True)
                require(set(dropins.iterdir()) <= ({self.inhibit_path(unit)} if name == unit else set()),
                        "Unknown unloaded controller drop-in; no metadata reload")
        fence = BASE / "guard-inhibited"
        value = canonical(self.authority())
        if not fence.exists():
            create(fence, value)
        require(checked(fence).read_bytes() == value, "Foreign persistent guard fence")
        path = self.inhibit_path(unit)
        if not path.parent.exists():
            path.parent.mkdir(mode=0o755)
            sync_directory(path.parent.parent)
        checked(path.parent, (0o755,), directory=True)
        data = f"[Unit]\nConditionPathExists=!{fence}\n".encode()
        if not path.exists():
            create(path, data, 0o644)
        require(checked(path, (0o644,)).read_bytes() == data, "Foreign guard drop-in; no overwrite")
        run(["/usr/bin/systemd-analyze", "verify", "--man=no", str(SYSTEM / unit)])
        run(["/usr/bin/systemctl", "daemon-reload"])
        run(["/usr/bin/systemctl", "stop", "node-maintenance-cold-" + RUN + "-guard.timer", unit])
        self.actor(unit, inhibited=True)
        self.actor("node-maintenance-cold-" + RUN + "-guard.timer")
        self.actor("node-maintenance-cold-" + RUN + ".service")

    def verify_inhibition(self):
        unit = "node-maintenance-cold-" + RUN + "-guard.service"
        require(checked(BASE / "guard-inhibited").read_bytes() == canonical(self.authority())
                and checked(self.inhibit_path(unit), (0o644,)).read_bytes()
                == f"[Unit]\nConditionPathExists=!{BASE / 'guard-inhibited'}\n".encode(),
                "Persistent guard inhibition drift")
        self.actor(unit, inhibited=True)
        self.actor("node-maintenance-cold-" + RUN + "-guard.timer")
        self.actor("node-maintenance-cold-" + RUN + ".service")

    def kube(self, method, uri, body=None):
        args = [self.client, "--kubeconfig", "/etc/rancher/rke2/rke2.yaml", "--request-timeout=30s"]
        args += ["get", "--raw=" + uri] if method == "GET" else ["delete", "--raw=" + uri, "-f", "-"]
        return json.loads(run(args, None if body is None else canonical(body), timeout=40))

    def lock(self, allow_absent=False):
        value = self.kube("GET", LOCK_LIST_URI)
        require(isinstance(value.get("items"), list) and not value["metadata"].get("continue"), "Unknown native API list")
        items = value["items"]
        require(len(items) <= 1, "Ambiguous lock list")
        if not items:
            require(allow_absent, "Original lock absent without retirement intent")
            return None
        lock = items[0]
        require(lock["metadata"]["name"] == "operator-node-maintenance"
                and lock["metadata"]["namespace"] == "kube-system", "Foreign lock identity")
        if lock["metadata"]["uid"] != LOCK_UID:
            require(allow_absent, "Replacement lock; no retirement authority")
            return lock
        require(lock["data"] == {"deadline_epoch": str(DEADLINE), "host_step_id": STEP, "plan_sha256": APPROVAL,
                                 "run_id": RUN, "started_epoch": str(START)}
                and isinstance(lock["metadata"]["resourceVersion"], str)
                and re.fullmatch("[0-9]+", lock["metadata"]["resourceVersion"])
                and lock["metadata"]["creationTimestamp"] == "2026-11-25T06:34:05Z",
                "Original lock data/version/creation drift")
        return lock

    def sign(self, kind, extras):
        body = {"protocol": "cancelled-unstarted-operator/v1", "kind": kind, "node": self.node,
                "run_id": RUN, "host_step_id": STEP, "approval": APPROVAL, "contract_sha256": CONTRACT_SHA,
                "lock_uid": LOCK_UID, "started_epoch": START, "deadline_epoch": DEADLINE,
                "original_commit": ORIGINAL_COMMIT, "source_sha256": self.source,
                "boot_id": self.plan["bindings"][self.node]["boot_id"],
                "kernel": self.plan["bindings"][self.node]["kernel"], "observed_epoch": time.time(), **extras}
        destination = self.pub / (kind + ".json")
        signed = BASE / "signing.json"
        atomic(signed, canonical(body))
        signature = Path(str(signed) + ".sig")
        if signature.exists():
            checked(signature, (0o600, 0o644)).unlink()
        run(["/usr/bin/ssh-keygen", "-Y", "sign", "-f", "/etc/ssh/ssh_host_ed25519_key",
             "-n", SIGN_NAMESPACE, str(signed)])
        own_signers = BASE / "local-signers"
        data = (self.node + " " + self.local_key + "\n").encode()
        if not own_signers.exists():
            create(own_signers, data)
        require(checked(own_signers).read_bytes() == data, "Local signing trust drift")
        run(["/usr/bin/ssh-keygen", "-Y", "verify", "-f", str(own_signers), "-I", self.node,
             "-n", SIGN_NAMESPACE, "-s", str(signature)], canonical(body))
        atomic(Path(str(destination) + ".sig"), checked(signature, (0o600, 0o644)).read_bytes(), 0o444)
        atomic(destination, canonical(body), 0o444)
        return body

    def evidence(self, kind, source=None, fresh=True):
        source = source or BASE / "inbox" / (kind + ".json")
        body_bytes = checked(source, (0o444,)).read_bytes()
        body = json.loads(body_bytes)
        require(body_bytes == canonical(body), "Noncanonical unchecked recovery assertion")
        signers = BASE / "allowed-signers"
        data = (self.other + " " + self.other_key + "\n").encode()
        if not signers.exists():
            create(signers, data)
        require(checked(signers).read_bytes() == data, "Foreign host trust; no key/config changes")
        run(["/usr/bin/ssh-keygen", "-Y", "verify", "-f", str(signers), "-I", self.other,
             "-n", SIGN_NAMESPACE, "-s", str(checked(Path(str(source) + ".sig"), (0o444,)))], body_bytes)
        expected = {"protocol": "cancelled-unstarted-operator/v1", "kind": kind, "node": self.other,
                    "run_id": RUN, "host_step_id": STEP, "approval": APPROVAL, "contract_sha256": CONTRACT_SHA,
                    "lock_uid": LOCK_UID, "started_epoch": START, "deadline_epoch": DEADLINE,
                    "original_commit": ORIGINAL_COMMIT, "source_sha256": self.source,
                    "boot_id": self.plan["bindings"][self.other]["boot_id"],
                    "kernel": self.plan["bindings"][self.other]["kernel"]}
        extras = {
            "worker-audit": {"unstarted", "idle"},
            "controller-fence": {"nonce", "lock_resource_version", "cancelled", "zero_mutations", "actors_inhibited_idle"},
            "worker-cancel": {"nonce", "controller_fence_sha256", "outcome", "idle",
                              "old_finalizer_ack", "cancellation_authority"},
        }[kind]
        age = time.time() - body["observed_epoch"]
        require(set(body) == set(expected) | extras | {"observed_epoch"}
                and all(body[k] == v for k, v in expected.items()) and age >= 0 and (not fresh or age < 120),
                "Signed evidence foreign/stale/source/clock drift")
        return body

    def worker_audit(self):
        require(self.node == WORKER, "Worker console only")
        with self.locked():
            self.state()
            self.worker_idle()
            return self.sign("worker-audit", {"unstarted": True, "idle": True})

    def controller_fence(self):
        require(self.node == SERVER, "Controller console only")
        worker = self.evidence("worker-audit")
        require(worker["unstarted"] is True and worker["idle"] is True, "Worker unstarted evidence absent")
        with self.locked():
            self.state()
            lock = self.lock()
            require(not lock["metadata"].get("deletionTimestamp"), "Original lock already terminating")
            self.snapshot("controller-state-original.json", self.original / "state.json")
            guard = "node-maintenance-cold-" + RUN + "-guard.service"
            if not (BASE / "guard-inhibited").exists():
                self.actor(guard, allow_running=True)
                self.actor("node-maintenance-cold-" + RUN + "-guard.timer", allow_running=True)
                self.actor("node-maintenance-cold-" + RUN + ".service")
            self.inhibit()
            self.state()
            lock = self.lock()
            return self.sign("controller-fence", {"nonce": self.authority()["nonce"],
                             "lock_resource_version": lock["metadata"]["resourceVersion"],
                             "cancelled": True, "zero_mutations": True, "actors_inhibited_idle": True})

    def worker_cancel(self):
        require(self.node == WORKER, "Worker console only")
        controller = self.evidence("controller-fence")
        require(controller["cancelled"] is True and controller["zero_mutations"] is True
                and controller["actors_inhibited_idle"] is True, "Actual controller fence absent")
        with self.locked():
            state = self.state()
            self.worker_idle()
            self.snapshot("worker-state-original.json", self.original / "state.json")
            self.authority()
            intent = BASE / "worker-cancel-intent.json"
            value = {"source_sha256": self.source, "nonce": controller["nonce"], "contract_sha256": CONTRACT_SHA}
            if not intent.exists():
                create(intent, canonical(value))
            require(read_json(intent) == value, "Foreign cancellation intent")
            units = ["node-maintenance-cold-worker-" + RUN + "-finalize" + suffix for suffix in (".timer", ".service")]
            for unit in units:
                self.actor(unit, allow_running=True)
            run(["/usr/bin/systemctl", "stop", *units])
            for unit in units:
                self.actor(unit)
            self.worker_idle()
            state = self.state()
            state.update(phase="cancelled", operator_cancellation=self.cancellation_tag())
            atomic(self.original / "state.json", canonical(state))
            original = read_json(BASE / "worker-state-original.json")
            require(original["phase"] == "staged" and original["pid"] == 0
                    and original["contract"] == self.contract and original["contract_sha256"] == CONTRACT_SHA,
                    "Original unstarted snapshot drift")
            self.state()
            self.worker_idle()
            return self.sign("worker-cancel", {"nonce": controller["nonce"],
                             "controller_fence_sha256": sha(canonical(controller)),
                             "outcome": "FAILED/CANCELLED-UNSTARTED", "idle": True,
                             "old_finalizer_ack": False, "cancellation_authority": self.cancellation_tag()})

    def terminal_worker(self, worker, fence):
        require(worker["nonce"] == self.authority()["nonce"]
                and worker["controller_fence_sha256"] == sha(canonical(fence))
                and worker["outcome"] == "FAILED/CANCELLED-UNSTARTED" and worker["idle"] is True
                and worker["old_finalizer_ack"] is False and worker["cancellation_authority"] == self.cancellation_tag(),
                "Independent worker terminal/idle evidence absent")

    def intents(self):
        result = []
        for path in sorted(BASE.glob("delete-intent-*.json")):
            require(re.fullmatch("delete-intent-[0-9]+[.]json", path.name), "Foreign deletion intent path")
            value = read_json(path)
            require(set(value) == {"uid", "resourceVersion", "worker_evidence_sha256", "source_sha256", "nonce"}
                    and value["uid"] == LOCK_UID and value["source_sha256"] == self.source
                    and value["nonce"] == self.authority()["nonce"]
                    and path.name == "delete-intent-" + value["resourceVersion"] + ".json",
                    "Foreign exact-UID deletion intent")
            saved = BASE / ("retirement-witness-" + value["resourceVersion"] + ".json")
            worker = self.evidence("worker-cancel", saved, fresh=False)
            require(sha(canonical(worker)) == value["worker_evidence_sha256"]
                    and worker["nonce"] == value["nonce"] and worker["outcome"] == "FAILED/CANCELLED-UNSTARTED"
                    and worker["idle"] is True and worker["old_finalizer_ack"] is False
                    and worker["cancellation_authority"] == self.cancellation_tag(), "Saved terminal witness drift")
            result.append(value)
        return result

    def controller_retire(self):
        require(self.node == SERVER, "Controller console only")
        with self.locked():
            self.state()
            self.verify_inhibition()
            intents = self.intents()
            lock = self.lock(allow_absent=bool(intents))
            if lock is not None and lock["metadata"]["uid"] == LOCK_UID:
                rv = lock["metadata"]["resourceVersion"]
                if lock["metadata"].get("deletionTimestamp"):
                    require(intents, "Foreign terminating lock")
                    return {"outcome": "FAILED/CANCELLED-UNSTARTED", "lock_retirement": "finalizers-pending"}
                worker = self.evidence("worker-cancel")
                fence = read_json(self.pub / "controller-fence.json", (0o444,))
                self.terminal_worker(worker, fence)
                require(rv == fence["lock_resource_version"], "ResourceVersion changed; fresh terminal proof required")
                intent = {"uid": LOCK_UID, "resourceVersion": rv, "worker_evidence_sha256": sha(canonical(worker)),
                          "source_sha256": self.source, "nonce": self.authority()["nonce"]}
                intent_path = BASE / ("delete-intent-" + rv + ".json")
                if not intent_path.exists():
                    witness = BASE / ("retirement-witness-" + rv + ".json")
                    if not witness.exists():
                        for suffix in (".sig", ""):
                            data = checked(BASE / "inbox" / ("worker-cancel.json" + suffix), (0o444,)).read_bytes()
                            destination = Path(str(witness) + suffix)
                            atomic(destination, data, 0o444)
                    saved = self.evidence("worker-cancel", witness, fresh=False)
                    require(saved["nonce"] == worker["nonce"] and saved["outcome"] == worker["outcome"]
                            and saved["idle"] is True and saved["old_finalizer_ack"] is False
                            and saved["cancellation_authority"] == self.cancellation_tag(),
                            "Interrupted saved cancellation witness differs")
                    intent["worker_evidence_sha256"] = sha(canonical(saved))
                    create(intent_path, canonical(intent))
                require(any(row["resourceVersion"] == rv for row in self.intents()), "Exact valid intent absent")
                require(self.evidence("worker-cancel") == worker, "Peer witness changed during local reproof")
                self.state()
                self.verify_inhibition()
                require(self.lock()["metadata"]["resourceVersion"] == rv, "Lock changed before conditioned DELETE")
                # Native kubectl sends this DeleteOptions body, not an invented delete flag.
                self.kube("DELETE", LOCK_URI, {"apiVersion": "v1", "kind": "DeleteOptions",
                                              "preconditions": {"uid": LOCK_UID, "resourceVersion": rv}})
                lock = self.lock(allow_absent=True)
                if lock is not None and lock["metadata"]["uid"] == LOCK_UID:
                    return {"outcome": "FAILED/CANCELLED-UNSTARTED", "lock_retirement": "finalizers-pending"}
            intents = self.intents()
            require(intents, "No exact preconditioned retirement intent")
            self.state()
            self.verify_inhibition()
            lock = self.lock(allow_absent=True)
            require(lock is None or lock["metadata"]["uid"] != LOCK_UID, "Original lock still present")
            return self.sign("controller-terminal", {"nonce": self.authority()["nonce"],
                             "outcome": "FAILED/CANCELLED-UNSTARTED", "original_lock_absent": True,
                             "replacement_lock_untouched": lock is not None,
                             "worker_evidence_sha256": intents[-1]["worker_evidence_sha256"],
                             "maintenance_success": False})


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("operation", choices=("worker-audit", "controller-fence", "worker-cancel", "controller-retire"))
    parser.add_argument("--reviewed-source-sha256", required=True)
    args = parser.parse_args()
    require(re.fullmatch("[a-f0-9]{64}", args.reviewed_source_sha256), "Reviewed source digest required")
    require(os.geteuid() == 0 and socket.gethostname() in IPS, "Exact authorized root console only")
    os.umask(0o077)
    for path in (BASE.parent, BASE, BASE / "inbox"):
        if not path.exists():
            path.mkdir(mode=0o700)
        checked(path, (0o700,), directory=True)
    for path in (PUBLIC.parent, PUBLIC, PUBLIC / socket.gethostname()):
        if not path.exists():
            path.mkdir(mode=0o755)
            path.chmod(0o755)
        checked(path, (0o755,), directory=True)
    recovery = Recovery(args.reviewed_source_sha256)
    result = getattr(recovery, args.operation.replace("-", "_"))()
    print(json.dumps({"run_id": RUN, "authority": "reviewed-operator-recovery-only",
                      "operation": args.operation, "maintenance_success": False, "result": result}))


if __name__ == "__main__":
    try:
        main()
    except (RuntimeError, OSError, ValueError, KeyError, TypeError, IndexError, subprocess.SubprocessError) as error:
        reason = str(error) if isinstance(error, RuntimeError) else type(error).__name__
        print("STOP: recovery proof/operation rejected; no inferred lock retirement or maintenance success",
              file=os.sys.stderr)
        print("Technical gate: " + reason, file=os.sys.stderr)
        raise SystemExit(1)
