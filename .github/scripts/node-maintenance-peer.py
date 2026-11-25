#!/usr/bin/env python3
"""Single-use controller/worker2 maintenance reads; operator-only SSH setup."""

import argparse
import base64
from contextlib import contextmanager
import datetime
import hashlib
import importlib.util
import json
import os
from pathlib import Path
import platform
import re
import socket
import stat
import struct
import subprocess
import time


PUBLIC = Path("/etc/node-maintenance-peer")
PRIVATE = Path("/var/lib/node-maintenance-peer")
HOME = Path("/home/rishik")
NODES = {"rishik-controller": "192.168.1.10", "rishik-worker2": "192.168.1.12"}
SOURCES = ("node-maintenance-peer.py", "node-maintenance-host.py")
PROTOCOL = "node-maintenance-peer-v1"
READ_ENV = {"PATH": "/usr/sbin:/usr/bin:/sbin:/bin", "LC_ALL": "C"}


def require(condition, message):
    if not condition:
        raise RuntimeError(message)


def sha(value):
    return hashlib.sha256(value).hexdigest()


def canonical(value):
    return json.dumps(value, sort_keys=True, separators=(",", ":")).encode()


def identifier(value):
    require(isinstance(value, str) and re.fullmatch("[a-f0-9]{32}", value), "Invalid maintenance identifier")
    return value


def execute(args):
    result = subprocess.run(args, capture_output=True, text=True, timeout=60,
                            env=READ_ENV)
    require(result.returncode == 0, "Maintenance read/setup failed; retain original scope and inspect locally")
    return result.stdout.strip()


def account():
    import pwd
    user = pwd.getpwnam("rishik")
    require(user.pw_uid >= 1000 and user.pw_dir == str(HOME)
            and user.pw_shell in ("/bin/bash", "/bin/sh"), "Unexpected existing rishik account")
    return user


def host_identity(node):
    require(node in NODES and socket.gethostname() == node and platform.system() == "Linux"
            and platform.machine() == "x86_64", "Exact approved maintenance node/platform required")
    release = dict(line.split("=", 1) for line in Path("/etc/os-release").read_text().splitlines() if "=" in line)
    require(release.get("ID", "").strip('"') == "ubuntu"
            and release.get("VERSION_ID", "").strip('"') == "26.04", "Exact Ubuntu LTS required")
    addresses = json.loads(execute(["/usr/sbin/ip", "-j", "-4", "addr", "show"]))
    require(any(a.get("local") == NODES[node] for interface in addresses for a in interface["addr_info"]),
            "Approved local node IP absent")
    account()


def checked(path, mode, directory=False):
    metadata = path.lstat()
    require(metadata.st_uid == 0 and stat.S_IMODE(metadata.st_mode) == mode
            and (stat.S_ISDIR(metadata.st_mode) if directory else stat.S_ISREG(metadata.st_mode)),
            "Root-owned maintenance source/config/identity permissions drift")
    return path


def public_json(path):
    return json.loads(checked(path, 0o444).read_bytes())


def write_new(path, value, mode=0o444):
    with path.open("xb") as output:
        os.fchmod(output.fileno(), mode)
        output.write(value)
        output.flush()
        os.fsync(output.fileno())


@contextmanager
def setup_lock():
    import fcntl
    PRIVATE.mkdir(mode=0o700, exist_ok=True)
    checked(PRIVATE, 0o700, directory=True)
    fd = os.open(PRIVATE / "setup.lock", os.O_CREAT | os.O_RDWR | os.O_NOFOLLOW, 0o600)
    with os.fdopen(fd, "a") as lock:
        metadata = os.fstat(lock.fileno())
        require(stat.S_ISREG(metadata.st_mode) and metadata.st_uid == 0 and stat.S_IMODE(metadata.st_mode) == 0o600,
                "Root setup lock ownership/type drift")
        fcntl.flock(lock, fcntl.LOCK_EX)
        yield


def public_key(value):
    require(isinstance(value, str) and re.fullmatch(r"ssh-ed25519 [A-Za-z0-9+/]+={0,2}", value),
            "Exactly one Ed25519 public key, without options/comments, required")
    blob = base64.b64decode(value.split()[1], validate=True)
    require(len(blob) == 51 and struct.unpack(">I", blob[:4])[0] == 11
            and blob[4:15] == b"ssh-ed25519" and struct.unpack(">I", blob[15:19])[0] == 32,
            "Invalid Ed25519 public-key wire shape")
    return "SHA256:" + base64.b64encode(hashlib.sha256(blob).digest()).decode().rstrip("=")


def known_host(config):
    require(public_key(config["peer_host_key"]) == config["peer_host_fingerprint"],
            "Host key does not match authoritative fingerprint verified through trusted Windows SSH")
    return (NODES[config["peer"]] + " " + config["peer_host_key"] + "\n").encode()


def config():
    checked(PUBLIC, 0o755, directory=True)
    setup_id = identifier(public_json(PUBLIC / "current.json")["setup_id"])
    directory = checked(PUBLIC / setup_id, 0o755, directory=True)
    value = public_json(directory / "config.json")
    require(value["setup_id"] == setup_id and value["node"] in NODES
            and value["peer"] == next(n for n in NODES if n != value["node"])
            and value["expires_epoch"] == value["started_epoch"] + 86400
            and value["started_epoch"] <= time.time() < value["expires_epoch"], "Maintenance scope expired/clock/role drift")
    host_identity(value["node"])
    for name in SOURCES:
        require(sha(checked(directory / name, 0o444).read_bytes()) == value["source_sha256"][name],
                "Installed root-owned maintenance source drift")
    if (directory / "run.json").exists():
        grant = public_json(directory / "run.json")
        require(all(grant["source_bindings"][".github/scripts/" + name] == value["source_sha256"][name]
                    for name in SOURCES), "Immutable run/installed read-source binding drift")
    require(sha(Path(__file__).read_bytes()) == value["source_sha256"][SOURCES[0]], "Executing peer source drift")
    known_host(value)
    return value, directory


def helper(value, directory):
    path = checked(directory / SOURCES[1], 0o444)
    require(sha(path.read_bytes()) == value["source_sha256"][SOURCES[1]], "Host read helper source drift")
    spec = importlib.util.spec_from_file_location("bound_peer_host", path)
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


def local_proof(require_probe=True):
    value, directory = config()
    authorization = public_json(directory / "authorization.json")
    require(authorization["setup_id"] == value["setup_id"]
            and authorization["expires_epoch"] == value["expires_epoch"]
            and authorization["entry"] == authorized_entry(value, authorization["incoming_public_key"])
            and authorization["incoming_key_fingerprint"] == public_key(authorization["incoming_public_key"])
            and authorization["entry"] in (HOME / ".ssh/authorized_keys").read_text().splitlines(),
            "Exact restricted authorized-key entry absent/drifted")
    proof = {k: value[k] for k in ("setup_id", "node", "peer", "started_epoch", "expires_epoch",
                                  "source_sha256", "peer_host_fingerprint", "public_key_fingerprint",
                                  "original_boot_id", "original_kernel")}
    proof["incoming_key_fingerprint"] = authorization["incoming_key_fingerprint"]
    if require_probe:
        probe = public_json(directory / "probe.json")
        require(probe["config_sha256"] == sha(canonical(value))
                and value["started_epoch"] <= probe["verified_epoch"] <= time.time() < value["expires_epoch"],
                "Actual restricted read protocol probe missing/stale")
        proof["probe"] = probe
    return proof


def plan_scope(plan):
    proof = local_proof()
    expected = plan["peer_scope"][proof["node"]]
    require(proof == expected, "Approved peer identity/trust/source/expiry/probe drift")
    for name, digest in proof["source_sha256"].items():
        require(plan["source_bindings"][".github/scripts/" + name] == digest, "Approved peer source drift")
    for node, peer in plan["peer_scope"].items():
        require(node in NODES and peer["node"] == node and peer["peer"] == next(n for n in NODES if n != node)
                and peer["setup_id"] == proof["setup_id"] and peer["source_sha256"] == proof["source_sha256"]
                and peer["started_epoch"] == proof["started_epoch"] and peer["expires_epoch"] == proof["expires_epoch"]
                and peer["original_boot_id"] == plan["bindings"][node]["boot_id"]
                and peer["original_kernel"] == plan["bindings"][node]["kernel"],
                "Approved original node/source/setup binding drift")
    return proof


def register_run(plan, run_id, step_id, started, deadline):
    require(os.geteuid() == 0, "Run authorization requires visible operator root setup")
    identifier(run_id)
    identifier(step_id)
    require(run_id != step_id and deadline == started + 43200, "Exact cold run/step/deadline required")
    proof = plan_scope(plan)
    require(time.time() >= started and deadline < proof["expires_epoch"],
            "Peer setup expires before immutable outage deadline; block BEFORE pause")
    _, directory = config()
    grant = {"run_id": run_id, "host_step_id": step_id, "plan_sha256": sha(canonical(plan)),
             "source_bindings": plan["source_bindings"], "started_epoch": started, "deadline_epoch": deadline}
    write_new(directory / "run.json", canonical(grant))


def parse_request(text, value):
    require(isinstance(text, str) and len(text) <= 180, "Invalid maintenance request")
    match = re.fullmatch(PROTOCOL + r" ([a-f0-9]{32}) (proof|audit|controller-status|worker-status|worker-idle)"
                         r"(?: ([a-f0-9]{32}) ([a-f0-9]{32}))?", text)
    require(match is not None and match[1] == value["setup_id"], "Unknown/shell/foreign maintenance request")
    operation, run_id, step_id = match[2], match[3], match[4]
    require((operation in ("proof", "audit")) == (run_id is None), "Unexpected maintenance request arguments")
    require(operation in ("proof", "audit") or
            (value["node"] == "rishik-controller") == (operation == "controller-status"), "Foreign peer role")
    return operation, run_id, step_id


def bound_grant(directory, run_id, step_id):
    grant = public_json(directory / "run.json")
    require(grant["run_id"] == run_id and grant["host_step_id"] == step_id
            and grant["started_epoch"] <= time.time()
            and grant["deadline_epoch"] == grant["started_epoch"] + 43200, "Foreign/replayed run/step/deadline")
    return grant


def idle(h, run_id, step_id):
    units = [f"node-maintenance-{step_id}-os.service", f"node-maintenance-reboot-{step_id}.timer",
             f"node-maintenance-reboot-{step_id}.service", "reboot.target", "shutdown.target",
             f"node-maintenance-cold-worker-{run_id}.service"]
    states = [h.worker_unit_status(unit) for unit in units]
    require(all(s["LoadState"] in ("loaded", "not-found") and s["ActiveState"] in ("inactive", "failed")
                and s["Job"] == "0" for s in states[:3] + states[5:]), "Bound host work still active/unknown")
    require(all(s["LoadState"] == "loaded" and s["ActiveState"] == "inactive" and s["Job"] == "0"
                for s in states[3:5]), "Bound native reboot/shutdown pending")
    for index in (0, 2, 5):
        native = states[index]
        if native["LoadState"] == "loaded":
            require(native["MainPID"] == "0" and native["ControlPID"] == "0", "Bound service children active")
            h.worker_cgroup_empty(units[index], native)
        else:
            require(native.get("MainPID", "0") == "0" and native.get("ControlPID", "0") == "0",
                    "Absent unit with unexpected processes")
            h.worker_cgroup_empty(units[index], {"ControlGroup": ""})
    return {"states": [{k: s[k] for k in ("LoadState", "ActiveState", "Job")} for s in states],
            "cgroups_empty": True}


def dispatch(text):
    value, directory = config()
    operation, run_id, step_id = parse_request(text, value)
    if operation == "proof":
        return local_proof(require_probe=False)
    h = helper(value, directory)
    if operation == "audit":
        return h.audit()
    grant = bound_grant(directory, run_id, step_id)
    if operation == "worker-idle":
        return idle(h, run_id, step_id)
    if operation == "worker-status":
        proof = h.worker_status(run_id)
        contract = proof["record"]["contract"]
        require(all(contract[k] == grant[k] for k in grant)
                and proof["record"]["contract_sha256"] == sha(canonical(contract)), "Worker public receipt binding drift")
        return proof
    status = h.cold_public(Path("/var/lib/node-maintenance-cold-status") / (run_id + ".json"))
    require(all(status[k] == grant[k] for k in grant if k != "source_bindings")
            and status["worker_contract"]["source_bindings"] == grant["source_bindings"],
            "Controller public status binding drift")
    unit = "node-maintenance-cold-" + run_id
    executor = h.worker_unit_status(unit + ".service")
    timer = h.worker_unit_status(unit + "-guard.timer")
    status.update(controller_name=socket.gethostname(), controller_boot_id=Path("/proc/sys/kernel/random/boot_id").read_text().strip(),
                  controller_kernel=platform.release(),
                  executor_active=executor["LoadState"] == "loaded" and executor["ActiveState"] in ("active", "activating")
                  and int(executor["MainPID"]) > 0,
                  guard_active=timer["LoadState"] == "loaded" and timer["ActiveState"] == "active",
                  guard_enabled=execute(["/usr/bin/systemctl", "is-enabled", unit + "-guard.timer"]) == "enabled")
    return status


def request(operation, run_id=None, step_id=None):
    require(os.geteuid() == 0, "Outgoing maintenance identity is host-local root only")
    value, directory = config()
    if operation not in ("proof", "audit"):
        grant = public_json(directory / "run.json")
        run_id = run_id or grant["run_id"]
        step_id = step_id or grant["host_step_id"]
        bound_grant(directory, run_id, step_id)
    text = PROTOCOL + " " + value["setup_id"] + " " + operation
    if run_id is not None:
        text += " " + identifier(run_id) + " " + identifier(step_id)
    parse_request(text, {**value, "node": value["peer"]})
    private = checked(PRIVATE / value["setup_id"], 0o700, directory=True)
    checked(private / "identity", 0o600)
    require(checked(private / "known_hosts", 0o600).read_bytes() == known_host(value),
            "Scoped authoritative known_hosts drift")
    args = ["/usr/bin/ssh", "-F", "/dev/null", "-T", "-i", str(private / "identity"),
            "-o", "IdentitiesOnly=yes", "-o", "IdentityAgent=none", "-o", "ForwardAgent=no",
            "-o", "ClearAllForwardings=yes", "-o", "RequestTTY=no", "-o", "BatchMode=yes",
            "-o", "PasswordAuthentication=no", "-o", "KbdInteractiveAuthentication=no",
            "-o", "StrictHostKeyChecking=yes", "-o", "HostKeyAlgorithms=ssh-ed25519",
            "-o", "UpdateHostKeys=no", "-o", "VerifyHostKeyDNS=no", "-o", "PreferredAuthentications=publickey",
            "-o", "UserKnownHostsFile=" + str(private / "known_hosts"), "-o", "GlobalKnownHostsFile=/dev/null",
            "-o", "ControlMaster=no", "-o", "ControlPath=none", "-o", "ProxyCommand=none",
            "-o", "ProxyJump=none", "-o", "ConnectTimeout=10", "rishik@" + NODES[value["peer"]], text]
    result = json.loads(execute(args))
    require(time.time() < value["expires_epoch"], "Peer scope expired during response")
    return result


def authorized_entry(value, key):
    identifier(value["setup_id"])
    require(value["peer"] in NODES and value["node"] in NODES and value["peer"] != value["node"],
            "Exact authorized peer pair required")
    public_key(key)
    expiry = datetime.datetime.fromtimestamp(value["expires_epoch"], datetime.timezone.utc).strftime("%Y%m%d%H%M%SZ")
    command = f"/usr/bin/python3 -I -B {PUBLIC}/{value['setup_id']}/{SOURCES[0]} serve"
    return (f'from="{NODES[value["peer"]]}",restrict,command="{command}",expiry-time="{expiry}" '
            f'{key} node-maintenance-peer:{value["setup_id"]}:{value["peer"]}')


def edit_authorized(entry, add=True, separator=False, original_sha256=None):
    user = account()
    directory = HOME / ".ssh"
    metadata = directory.lstat()
    require(stat.S_ISDIR(metadata.st_mode) and metadata.st_uid == user.pw_uid
            and stat.S_IMODE(metadata.st_mode) == 0o700, "Existing user SSH directory must remain unchanged")
    path = directory / "authorized_keys"
    metadata = path.lstat()
    require(stat.S_ISREG(metadata.st_mode) and metadata.st_uid == user.pw_uid
            and stat.S_IMODE(metadata.st_mode) == 0o600, "Existing authorized_keys must be regular user-owned 0600")
    original = path.read_bytes()
    line = entry.encode() + b"\n"
    if add:
        require(original_sha256 is None or sha(original) == original_sha256,
                "Authorized_keys changed after setup ownership receipt; stop")
        require(entry.encode() not in original and b"node-maintenance-peer:" not in original,
                "Existing maintenance authorization; no replacement/rotation")
        separator = bool(original and not original.endswith(b"\n"))
        changed = original + (b"\n" if separator else b"") + line
    else:
        require(original.splitlines().count(entry.encode()) == 1, "Exact owned entry absent/ambiguous; no cleanup")
        changed = b"".join(row for row in original.splitlines(keepends=True) if row.rstrip(b"\r\n") != entry.encode())
        if separator and original.endswith(line) and changed.endswith(b"\n") and (
                original_sha256 is None or sha(changed[:-1]) == original_sha256):
            changed = changed[:-1]
    staged = directory / ("node-maintenance-peer-" + sha(line)[:16] + ".next")
    write_new(staged, changed, 0o600)
    os.chown(staged, metadata.st_uid, metadata.st_gid)
    require(path.lstat() == metadata and path.read_bytes() == original, "Concurrent authorized_keys edit; stop")
    os.replace(staged, path)
    return separator


def prepare_input(setup_id):
    identifier(setup_id)
    incoming = HOME / ".local/state/node-maintenance-peer" / setup_id
    value = json.loads((incoming / "setup.json").read_bytes())
    require(set(value) == {"setup_id", "node", "peer", "started_epoch", "expires_epoch",
                           "peer_host_key", "peer_host_fingerprint", "source_sha256"}
            and value["setup_id"] == setup_id and value["node"] in NODES
            and value["peer"] == next(n for n in NODES if n != value["node"])
            and isinstance(value["started_epoch"], int) and isinstance(value["expires_epoch"], int)
            and value["expires_epoch"] == value["started_epoch"] + 86400
            and 0 <= time.time() - value["started_epoch"] < 900
            and set(value["source_sha256"]) == set(SOURCES), "Exact fresh immutable 24h setup scope required")
    host_identity(value["node"])
    known_host(value)
    sources = {name: (incoming / name).read_bytes() for name in SOURCES}
    require(all(sha(data) == value["source_sha256"][name] for name, data in sources.items())
            and sha(Path(__file__).read_bytes()) == value["source_sha256"][SOURCES[0]], "Approved setup source drift")
    # Refuse absent/unexpected original SSH settings before creating anything.
    user = account()
    ssh = (HOME / ".ssh").lstat()
    keys = (HOME / ".ssh/authorized_keys").lstat()
    require(stat.S_ISDIR(ssh.st_mode) and ssh.st_uid == user.pw_uid and stat.S_IMODE(ssh.st_mode) == 0o700
            and stat.S_ISREG(keys.st_mode) and keys.st_uid == user.pw_uid and stat.S_IMODE(keys.st_mode) == 0o600,
            "Provisioned existing user SSH settings required; no broad SSH configuration changes")
    return value, sources


def prepare(setup_id, validated=None):
    value, sources = validated or prepare_input(setup_id)
    require(0 <= time.time() - value["started_epoch"] < 900, "Setup expired while waiting for original root lock")
    host_identity(value["node"])
    if not PUBLIC.exists():
        PUBLIC.mkdir(mode=0o755)
        PUBLIC.chmod(0o755)
    checked(PUBLIC, 0o755, directory=True)
    require(not (PUBLIC / "current.json").exists(), "Existing single-use scope; never reset/replace expiry")
    directory = PUBLIC / setup_id
    directory.mkdir(mode=0o755)
    directory.chmod(0o755)
    private = PRIVATE / setup_id
    private.mkdir(mode=0o700)
    execute(["/usr/bin/ssh-keygen", "-q", "-t", "ed25519", "-N", "", "-C",
             "node-maintenance-peer:" + setup_id, "-f", str(private / "identity")])
    checked(private / "identity", 0o600)
    key = " ".join((private / "identity.pub").read_text().split()[:2])
    value.update(public_key_fingerprint=public_key(key),
                 original_boot_id=Path("/proc/sys/kernel/random/boot_id").read_text().strip(),
                 original_kernel=platform.release())
    write_new(private / "known_hosts", known_host(value), 0o600)
    for name, data in sources.items():
        write_new(directory / name, data)
    write_new(directory / "config.json", canonical(value))
    write_new(directory / "public-key.txt", (key + "\n").encode())
    write_new(PUBLIC / "current.json", canonical({"setup_id": setup_id}))
    return {"setup_id": setup_id, "node": value["node"], "public_key": key,
            "public_key_fingerprint": value["public_key_fingerprint"], "expires_epoch": value["expires_epoch"]}


def authorize():
    value, directory = config()
    incoming = HOME / ".local/state/node-maintenance-peer" / value["setup_id"] / "peer-public-key.txt"
    key = incoming.read_text().strip()
    entry = authorized_entry(value, key)
    original = (HOME / ".ssh/authorized_keys").read_bytes()
    record = {"setup_id": value["setup_id"], "expires_epoch": value["expires_epoch"], "entry": entry,
              "incoming_key_fingerprint": public_key(key), "incoming_public_key": key,
              "separator": bool(original and not original.endswith(b"\n")), "original_sha256": sha(original)}
    # Write ownership evidence first; an interrupted append can only fail closed.
    write_new(directory / "authorization.json", canonical(record))
    edit_authorized(entry, original_sha256=record["original_sha256"])
    return local_proof(require_probe=False)


def probe():
    value, directory = config()
    own = local_proof(require_probe=False)
    peer = request("proof")
    require(peer["node"] == value["peer"] and peer["peer"] == value["node"]
            and all(peer[k] == own[k] for k in ("setup_id", "started_epoch", "expires_epoch", "source_sha256"))
            and peer["incoming_key_fingerprint"] == own["public_key_fingerprint"]
            and own["incoming_key_fingerprint"] == peer["public_key_fingerprint"], "Actual restricted peer probe identity drift")
    audit = request("audit")
    require(audit["name"] == value["peer"] and audit["boot_id"] == peer["original_boot_id"]
            and audit["kernel"] == peer["original_kernel"], "Actual read-only peer audit binding drift")
    write_new(directory / "probe.json", canonical({"config_sha256": sha(canonical(value)),
                                                 "peer_proof_sha256": sha(canonical(peer)),
                                                 "verified_epoch": time.time()}))
    return local_proof()


def cleanup(setup_id):
    identifier(setup_id)
    checked(PUBLIC, 0o755, directory=True)
    require(public_json(PUBLIC / "current.json") == {"setup_id": setup_id}, "Foreign cleanup scope")
    directory = checked(PUBLIC / setup_id, 0o755, directory=True)
    value = public_json(directory / "config.json")
    require(value["setup_id"] == setup_id
            and sha(checked(directory / SOURCES[0], 0o444).read_bytes()) == value["source_sha256"][SOURCES[0]]
            and sha(Path(__file__).read_bytes()) == value["source_sha256"][SOURCES[0]], "Exact cleanup source drift")
    host_identity(value["node"])
    h = helper(value, directory)
    if (directory / "run.json").exists():
        grant = public_json(directory / "run.json")
        if value["node"] == "rishik-controller":
            status = h.cold_public(Path("/var/lib/node-maintenance-cold-status") / (grant["run_id"] + ".json"))
            require(status["completed"] is True
                    and all(status["worker_contract"][k] == grant[k] for k in grant),
                    "Live/blocked/foreign controller run; retain channel")
            for suffix in (".service", "-guard.service"):
                unit = "node-maintenance-cold-" + grant["run_id"] + suffix
                native = h.worker_unit_status(unit)
                require(native["LoadState"] == "loaded" and native["ActiveState"] in ("inactive", "failed")
                        and native["Job"] == "0" and native["FragmentPath"] == "/etc/systemd/system/" + unit,
                        "Controller units still active; retain channel")
                require(native["MainPID"] == "0" and native["ControlPID"] == "0", "Controller service children active")
                h.worker_cgroup_empty(unit, native)
            timer = "node-maintenance-cold-" + grant["run_id"] + "-guard.timer"
            native = h.worker_unit_status(timer)
            require(native["LoadState"] == "loaded" and native["Job"] == "0"
                    and native["FragmentPath"] == "/etc/systemd/system/" + timer,
                    "Completed controller guard timer identity drift")
            execute(["/usr/bin/systemctl", "disable", "--now", timer])
        else:
            status = h.worker_status(grant["run_id"])["record"]
            require(status["phase"] in ("cancelled", "failed", "packages-complete")
                    and all(status["contract"][k] == grant[k] for k in grant), "Live/foreign worker run; retain channel")
            if time.time() < value["expires_epoch"]:
                controller = request("controller-status", grant["run_id"], grant["host_step_id"])
                require(controller["completed"] is True and controller["plan_sha256"] == grant["plan_sha256"],
                        "Controller recovery not terminal; retain still-valid peer channel")
            idle(h, grant["run_id"], grant["host_step_id"])
            for suffix in ("-finalize.service", "-finalize.timer"):
                native = h.worker_unit_status("node-maintenance-cold-worker-" + grant["run_id"] + suffix)
                require(native["ActiveState"] in ("inactive", "failed") and native["Job"] == "0",
                        "Worker finalizer still active; retain channel")
    record = public_json(directory / "authorization.json")
    edit_authorized(record["entry"], add=False, separator=record["separator"], original_sha256=record["original_sha256"])
    private = checked(PRIVATE / setup_id, 0o700, directory=True)
    for name, mode in (("identity", 0o600), ("identity.pub", 0o644), ("known_hosts", 0o600)):
        checked(private / name, mode).unlink()
    private.rmdir()
    (PUBLIC / "current.json").unlink()
    return {"setup_id": setup_id, "exact_entry_removed": True, "host_local_identity_removed": True}


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("operation", choices=("serve", "prepare", "authorize", "probe", "cleanup"))
    parser.add_argument("--setup-id")
    args = parser.parse_args()
    if args.operation == "serve":
        require(args.setup_id is None and os.geteuid() == account().pw_uid
                and "SSH_TTY" not in os.environ, "Incoming protocol is existing non-root rishik only; no PTY")
        value, _ = config()
        connection = os.environ.get("SSH_CONNECTION", "").split()
        require(len(connection) == 4 and connection[0] == NODES[value["peer"]]
                and connection[2] == NODES[value["node"]] and all(p.isdigit() for p in (connection[1], connection[3])),
                "Foreign SSH peer/node")
        text = os.environ.get("SSH_ORIGINAL_COMMAND", "")
        os.environ.clear()
        os.environ.update(READ_ENV)
        result = dispatch(text)
    else:
        require(os.geteuid() == 0 and args.setup_id is not None, "Visible operator root setup only")
        identifier(args.setup_id)
        validated = prepare_input(args.setup_id) if args.operation == "prepare" else None
        if args.operation == "cleanup":
            require(public_json(PUBLIC / "current.json") == {"setup_id": args.setup_id}, "Foreign cleanup scope")
            value = public_json(PUBLIC / args.setup_id / "config.json")
            host_identity(value["node"])
        elif validated is None:
            require(config()[0]["setup_id"] == args.setup_id, "Foreign setup invocation")
        with setup_lock():
            if args.operation == "prepare":
                result = prepare(args.setup_id, validated)
            elif args.operation == "cleanup":
                result = cleanup(args.setup_id)
            else:
                require(config()[0]["setup_id"] == args.setup_id, "Foreign setup invocation")
                result = authorize() if args.operation == "authorize" else probe()
    print(json.dumps(result))


if __name__ == "__main__":
    try:
        main()
    except (RuntimeError, OSError, ValueError, KeyError, subprocess.SubprocessError):
        print("STOP: restricted maintenance read/setup rejected; no pause permitted; inspect locally", file=os.sys.stderr)
        raise SystemExit(1)
