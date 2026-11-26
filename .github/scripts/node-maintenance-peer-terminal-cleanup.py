#!/usr/bin/env python3
"""Reviewed SSH-only retirement of the exact cancelled worker2 peer setup."""

import argparse
import hashlib
import importlib.util
import json
import math
import os
from pathlib import Path
import re
import socket
import stat
import subprocess
import sys
import time


SETUP = "582d351db706417aab8492d945598f2d"
RUN = "53a3a0a510054a5dbca6bccb677d3148"
RECOVERY_SHA = "320751e5bca62a283352b312f4ede5752d6f6b8f54367b8feec61731da1734aa"
PEER_SHA = "35f71ef263af3c883062cf6de6d27cc9f0c89277473ff4582eeb2e62ed0457cf"
RECOVERY = Path("/var/lib/node-maintenance-operator-recovery") / RUN
BASE = Path("/var/lib/node-maintenance-operator-peer-cleanup") / SETUP
PUBLIC = Path("/var/lib/node-maintenance-operator-peer-cleanup-status") / SETUP
PEER = Path("/etc/node-maintenance-peer") / SETUP
SERVER, WORKER = "rishik-controller", "rishik-worker2"
NAMESPACE = "node-maintenance-peer-terminal-cleanup-" + SETUP
TERMINALS = {
    "controller-terminal": "36c825d75c04cdebcfab0faefea9fb4ea5b64707e26463d2b74c3bb55f9005cd",
    "worker-cancel": "3b2153ef93848df1f3de28dc9695c9c6ae97a5b16252b8832af643ae911d875c",
}
PHASES = ("intent", "authorization-removed", "identity-removed",
          "pointer-retire-intent", "pointer-retired")


def require(value, message):
    if not value:
        raise RuntimeError(message)


def digest(data):
    return hashlib.sha256(data).hexdigest()


def canonical(value):
    return json.dumps(value, sort_keys=True, separators=(",", ":")).encode()


def checked(path, mode=None, directory=False):
    if mode is None:
        mode = 0o700 if directory else 0o600
    info = path.lstat()
    require(info.st_uid == info.st_gid == 0 and stat.S_IMODE(info.st_mode) == mode
            and (stat.S_ISDIR(info.st_mode) if directory else stat.S_ISREG(info.st_mode))
            and (directory or info.st_nlink == 1), "Owned cleanup artifact type/owner/mode/link drift")
    return path


def read(path, mode=0o600):
    return json.loads(checked(path, mode).read_bytes())


def native(args, data=None):
    result = subprocess.run(args, input=data, stdout=subprocess.PIPE, stderr=subprocess.PIPE,
                            timeout=60, env={"PATH": "/usr/sbin:/usr/bin:/sbin:/bin", "LC_ALL": "C"})
    require(result.returncode == 0, "Native cleanup command refused; preserve remaining artifacts")
    return result.stdout


def directory(path):
    if not path.exists():
        path.mkdir(mode=0o700)
    checked(path, directory=True)


def sync(path):
    fd = os.open(path, os.O_RDONLY | os.O_DIRECTORY | os.O_NOFOLLOW)
    try:
        os.fsync(fd)
    finally:
        os.close(fd)


def promote(pending, path, data):
    require(not path.exists(), "Foreign committed cleanup record; preserve")
    fd = os.open(pending, os.O_RDONLY | os.O_NOFOLLOW)
    try:
        before = identity(checked(pending))
        meta = os.fstat(fd)
        require(before[:2] == [meta.st_dev, meta.st_ino]
                and pending.read_bytes() == data, "Pending record inode/payload changed")
        os.fsync(fd)
        require(identity(checked(pending)) == before and pending.read_bytes() == data
                and not path.exists(), "Pending record changed during durability proof")
        os.replace(pending, path)
        sync(path.parent)
    finally:
        os.close(fd)


def load(path, expected, name):
    require(digest(checked(path, 0o444).read_bytes()) == expected, "Pinned cleanup dependency source drift")
    spec = importlib.util.spec_from_file_location(name, path)
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


def identity(path):
    info = path.lstat()
    return [info.st_dev, info.st_ino, info.st_uid, info.st_gid,
            stat.S_IMODE(info.st_mode), info.st_nlink]


class Cleanup:
    def __init__(self, reviewed):
        require(os.geteuid() == 0 and socket.gethostname() in (SERVER, WORKER),
                "Original authenticated root node required")
        require(Path(__file__).resolve() == BASE / "helper.py"
                and digest(checked(BASE / "helper.py", 0o444).read_bytes()) == reviewed,
                "Actual reviewed cleanup source/path required")
        checked(BASE, directory=True)
        checked(BASE / "inbox", directory=True)
        directory(BASE / "events")
        directory(BASE / "proofs")
        self.source = reviewed
        self.node = socket.gethostname()
        self.context = {"source_sha256": reviewed, "setup_id": SETUP, "run_id": RUN,
                        "node": self.node, "recovery_sha256": RECOVERY_SHA, "terminals": TERMINALS}
        self.core = load(RECOVERY / "helper.py", RECOVERY_SHA, "accepted_cancelled_recovery")
        self.peer = load(PEER / "node-maintenance-peer.py", PEER_SHA, "immutable_cleanup_peer")
        owner = self

        class TerminalView(self.core.Recovery):
            def verify_peer_setup(view):
                owner.peer_binding(view)

            def clock(view):
                return owner.clock()

        self.view = TerminalView(RECOVERY_SHA)
        self.value = self.core.read_json(PEER / "config.json", (0o444,))
        self.authorization = self.core.read_json(PEER / "authorization.json", (0o444,))
        self.verify_terminals()

    def event(self, phase):
        require(phase in PHASES, "Unknown cleanup phase")
        path = BASE / "events" / (phase + ".json")
        return self.validate_event(read(path), phase) if path.exists() else None

    def validate_event(self, body, phase):
        wanted = {"context", "phase", "observed_epoch"} | ({"private_files"} if phase == "intent" else set())
        require(set(body) == wanted and body["context"] == self.context and body["phase"] == phase
                and type(body["observed_epoch"]) in (int, float)
                and math.isfinite(body["observed_epoch"])
                and 1795658411.7102768 <= body["observed_epoch"] <= time.time(),
                "Cleanup phase/source/clock drift")
        if phase == "intent":
            require(set(body["private_files"]) == {"identity", "identity.pub", "known_hosts"}
                    and all(isinstance(v, list) and len(v) == 6
                            and all(type(n) is int for n in v) for v in body["private_files"].values()),
                    "Private identity intent shape drift")
        return body

    def clock(self):
        previous = read(RECOVERY / "operator-clock.json")
        require(set(previous) == {"source_sha256", "last_epoch"}
                and previous["source_sha256"] == RECOVERY_SHA
                and type(previous["last_epoch"]) in (int, float)
                and math.isfinite(previous["last_epoch"]) and previous["last_epoch"] >= self.core.START,
                "Accepted recovery clock drift")
        floor = max(1795658411.7102768, previous["last_epoch"])
        allowed = {p + ".json" + suffix for p in PHASES for suffix in ("", ".pending")}
        checked(BASE / "events", directory=True)
        for path in (BASE / "events").iterdir():
            require(path.name in allowed, "Unknown pending cleanup/actuation artifact")
            phase = path.name.split(".", 1)[0]
            floor = max(floor, self.validate_event(read(path), phase)["observed_epoch"])
        checked(BASE / "proofs", directory=True)
        for path in (BASE / "proofs").iterdir():
            require(re.fullmatch("[a-f0-9]{64}\\.(json|pending)", path.name),
                    "Unknown cleanup proof history artifact")
            body = self.validate_proof(read(path))
            require(digest(canonical(body)) == path.name.split(".", 1)[0], "Proof history payload drift")
            floor = max(floor, body["observed_epoch"])
        now = time.time()
        require(math.isfinite(now) and now >= floor, "Cleanup clock rollback; no renewed maintenance")
        return now

    def checkpoint(self, phase, private_files=None):
        path = BASE / "events" / (phase + ".json")
        pending = path.with_name(path.name + ".pending")
        now = self.clock()
        if path.exists():
            require(not pending.exists(), "Committed phase has unexpected pending replacement")
            return self.event(phase)
        if pending.exists():
            body = self.validate_event(read(pending), phase)
        else:
            body = {"context": self.context, "phase": phase, "observed_epoch": now}
            if phase == "intent":
                require(private_files is not None, "Initial private identity proof missing")
                body["private_files"] = private_files
            with pending.open("xb") as stream:
                os.fchmod(stream.fileno(), 0o600)
                stream.write(canonical(body))
                stream.flush()
                os.fsync(stream.fileno())
            sync(pending.parent)
        self.validate_event(body, phase)
        promote(pending, path, canonical(body))
        return body

    def validate_proof(self, body, node=None):
        require(set(body) == {"protocol", "setup_id", "run_id", "node", "source_sha256", "terminals",
                             "observed_epoch", "outcome", "original_records_preserved",
                             "maintenance_success", "ssh_validated_without_restart"}
                and body["protocol"] == "terminal-peer-cleanup/v1" and body["setup_id"] == SETUP
                and body["run_id"] == RUN and body["node"] == (self.node if node is None else node)
                and body["source_sha256"] == self.source and body["terminals"] == TERMINALS
                and body["outcome"] == "OWNED-PEER-SETUP-CLEANED"
                and body["original_records_preserved"] is True and body["maintenance_success"] is False
                and body["ssh_validated_without_restart"] is True
                and type(body["observed_epoch"]) in (int, float)
                and math.isfinite(body["observed_epoch"])
                and 1795658411.7102768 <= body["observed_epoch"] <= time.time(),
                "Cleanup proof scope/source/clock drift")
        return body

    def preserve_proof(self, body):
        data = canonical(self.validate_proof(body))
        path = BASE / "proofs" / (digest(data) + ".json")
        pending = path.with_suffix(".pending")
        if path.exists():
            require(checked(path).read_bytes() == data and not pending.exists(), "Proof publication drift")
            return
        if pending.exists():
            require(checked(pending).read_bytes() == data, "Foreign pending proof; preserve")
        else:
            with pending.open("xb") as stream:
                os.fchmod(stream.fileno(), 0o600)
                stream.write(data)
                stream.flush()
                os.fsync(stream.fileno())
            sync(pending.parent)
        promote(pending, path, data)

    def peer_binding(self, view):
        self.core.checked(PEER, (0o755,), directory=True)
        pointer = PEER.parent / "current.json"
        if pointer.exists():
            require(read(pointer, 0o444) == {"setup_id": SETUP}, "Foreign active SSH setup; preserve it")
        else:
            require(self.event("pointer-retire-intent") is not None
                    and self.event("identity-removed") is not None
                    and self.event("authorization-removed") is not None,
                    "Missing original setup pointer without durable owned retirement")
        value = read(PEER / "config.json", 0o444)
        require(value["setup_id"] == SETUP and value["node"] == view.node and value["peer"] == view.other
                and value["started_epoch"] == self.core.SETUP_START
                and value["expires_epoch"] == self.core.SETUP_EXPIRY
                and digest(canonical(value)) == view.plan["peer_scope"][view.node]["probe"]["config_sha256"],
                "Original peer identity/scope drift; no rebind")
        for name in self.peer.SOURCES:
            expected = self.core.HASHES[".github/scripts/" + name]
            require(value["source_sha256"][name] == expected
                    and digest(checked(PEER / name, 0o444).read_bytes()) == expected,
                    "Original installed peer source drift")
        grant = read(PEER / "run.json", 0o444)
        require(grant == {k: view.contract[k] for k in
                         ("run_id", "host_step_id", "plan_sha256", "source_bindings",
                          "started_epoch", "deadline_epoch")}, "Original peer grant drift")
        view.other_key = value["peer_host_key"]
        require(view.key_fingerprint(view.other_key) == value["peer_host_fingerprint"],
                "Original peer host key fingerprint drift")
        key = checked(Path("/etc/ssh/ssh_host_ed25519_key.pub"), 0o644).read_text().split()
        view.local_key = " ".join(key[:2])
        require(view.key_fingerprint(view.local_key)
                == view.plan["peer_scope"][view.other]["peer_host_fingerprint"],
                "Original local host identity drift")
        checked(Path("/etc/ssh/ssh_host_ed25519_key"), 0o600)

    def signers(self):
        keys = {self.node: self.view.local_key, self.view.other: self.view.other_key}
        data = "".join(n + " " + keys[n] + "\n" for n in (SERVER, WORKER)).encode()
        path = BASE / "allowed-signers"
        if not path.exists():
            self.core.create(path, data)
        require(checked(path).read_bytes() == data, "Cleanup signing trust changed")
        return path

    def verify_signed(self, path, node, namespace):
        data = checked(path, 0o444).read_bytes()
        signature = checked(Path(str(path) + ".sig"), 0o444)
        native(["/usr/bin/ssh-keygen", "-Y", "verify", "-f", str(self.signers()),
                "-I", node, "-n", namespace, "-s", str(signature)], data)
        return json.loads(data)

    def verify_terminals(self):
        for kind, expected in TERMINALS.items():
            path = BASE / "inbox" / (kind + ".json")
            require(digest(checked(path, 0o444).read_bytes()) == expected, "Genuine terminal witness changed")
            node = SERVER if kind == "controller-terminal" else WORKER
            body = self.verify_signed(path, node, self.core.SIGN_NAMESPACE)
            require(body["run_id"] == RUN and body["source_sha256"] == RECOVERY_SHA
                    and body["lock_uid"] == self.core.LOCK_UID
                    and body["started_epoch"] == self.core.START and body["deadline_epoch"] == self.core.DEADLINE
                    and body["outcome"] == "FAILED/CANCELLED-UNSTARTED", "Not the exact original terminal run")
            if kind == "controller-terminal":
                require(body["original_lock_absent"] is True and body["maintenance_success"] is False
                        and body["worker_evidence_sha256"] == TERMINALS["worker-cancel"],
                        "No accepted exact-lock retirement")
            else:
                require(body["idle"] is True and body["old_finalizer_ack"] is False,
                        "Worker terminal evidence is not genuine cancelled-unstarted")
        self.clock()

    def preflight(self, require_worker=True):
        self.peer_binding(self.view)
        self.verify_terminals()
        self.view.state()
        if self.node == SERVER:
            require((RECOVERY / "authority.json").exists(), "Original controller fence authority absent")
            self.view.verify_inhibition()
            self.view.actor("node-maintenance-cold-" + RUN + ".service")
            require(self.view.lock(allow_absent=True) is None, "Maintenance lock present; no SSH retirement")
            if require_worker:
                body = self.verify_signed(BASE / "inbox" / "worker-peer-cleanup.json", WORKER, NAMESPACE)
                self.validate_proof(body, node=WORKER)
                require(0 <= time.time() - body["observed_epoch"] < 120,
                        "Fresh genuine worker-first SSH cleanup proof required")
                self.worker_proof = body
        else:
            self.view.worker_idle()
            for unit in self.view.unit_bodies():
                if "-finalize." in unit:
                    self.view.actor(unit)
        record = self.authorization
        require(read(PEER / "authorization.json", 0o444) == record
                and record["activated"] is True and record["setup_id"] == SETUP
                and record["expires_epoch"] == self.core.SETUP_EXPIRY,
                "Original activated SSH authorization drift")
        revoked = {"setup_id": SETUP, "authorization_sha256": digest(canonical(record))}
        marker = PEER / "authorization.revoked.json"
        if marker.exists():
            require(self.event("intent") is not None and read(marker, 0o444) == revoked,
                    "Foreign/unowned authorization revocation")
        else:
            self.peer.activation_verified(self.value, PEER)
        self.peer.validate_policy(self.value, record, installed=self.peer.ssh_files(self.value)[1].exists())

    def worker_fresh(self):
        if self.node == SERVER:
            require(hasattr(self, "worker_proof"), "Verified worker cleanup proof missing")
            self.validate_proof(self.worker_proof, node=WORKER)
            require(0 <= time.time() - self.worker_proof["observed_epoch"] < 120,
                    "Worker cleanup proof expired before mutation; preserve remaining setup")

    def remove_authorization(self):
        # The immutable revoker has no callback after its blocking validation.
        # Keep its activated-only checks, with freshness at each mutation boundary.
        value, record = self.value, self.authorization
        keyfile, dropin = self.peer.ssh_files(value)
        marker, cleaned = PEER / "authorization.revoked.json", PEER / "authorization.cleaned.json"
        revoked = {"setup_id": SETUP, "authorization_sha256": digest(canonical(record))}
        require(record["activated"] is True and record["setup_id"] == value["setup_id"] == SETUP
                and record["expires_epoch"] == value["expires_epoch"]
                and record["entry"] == self.peer.authorized_entry(value, record["incoming_public_key"])
                and record["incoming_key_fingerprint"] == self.peer.public_key(record["incoming_public_key"]),
                "Foreign authorization cleanup record")
        for path in (marker, cleaned):
            if path.exists():
                require(read(path, 0o444) == revoked, "Foreign owned SSH retirement record; preserve")
        if not marker.exists():
            self.peer.activation_verified(value, PEER)
        self.peer.validate_policy(value, record, installed=dropin.exists())
        if not marker.exists():
            self.worker_fresh()
            self.peer.write_new(marker, canonical(revoked))
            sync(marker.parent)
        for path, mode, data in (
                (keyfile, 0o444, (record["entry"] + "\n").encode()),
                (dropin, 0o644, self.peer.dropin_text(
                    value, record["policy"]["profiles"][next(iter(self.peer.ssh_contexts(value)))]["authorized_keys"]))):
            if path.exists():
                require(checked(path, mode).read_bytes() == data, "Foreign owned SSH artifact; preserve")
                self.worker_fresh()
                path.unlink()
                sync(path.parent)
        pid = self.peer.validate_policy(value, record, installed=False)
        self.worker_fresh()
        self.peer.execute(["/usr/bin/systemctl", "reload", "ssh.service"])
        require(self.peer.validate_policy(value, record, installed=False) == pid,
                "SSH reload failed/ambiguous or daemon identity changed; no cleanup proof")
        if not cleaned.exists():
            self.worker_fresh()
            self.peer.write_new(cleaned, canonical(revoked))
            sync(cleaned.parent)

    def private_proof(self, intent=None):
        folder = self.peer.PRIVATE / SETUP
        names = {"identity": 0o600, "identity.pub": 0o644, "known_hosts": 0o600}
        if not folder.exists():
            require(intent is not None, "Private setup identity unexpectedly absent")
            return {}
        checked(folder, directory=True)
        require(set(p.name for p in folder.iterdir()) <= set(names), "Foreign private identity file; retain")
        result = {}
        for name, mode in names.items():
            path = folder / name
            if not path.exists():
                require(intent is not None, "Private setup incomplete without owned cleanup intent")
                continue
            checked(path, mode)
            result[name] = identity(path)
            if intent is not None:
                require(result[name] == intent["private_files"][name], "Private artifact inode replaced; retain")
        fingerprint = self.value["public_key_fingerprint"]
        if "identity" in result:
            key = " ".join(native(["/usr/bin/ssh-keygen", "-y", "-f",
                                  str(folder / "identity")]).decode().split()[:2])
            require(self.peer.public_key(key) == fingerprint, "Foreign private identity; retain")
        if "identity.pub" in result:
            key = " ".join((folder / "identity.pub").read_text().split()[:2])
            require(self.peer.public_key(key) == fingerprint, "Foreign temporary public identity; retain")
        if "known_hosts" in result:
            require((folder / "known_hosts").read_bytes() == self.peer.known_host(self.value),
                    "Original temporary peer trust changed; retain")
        return result

    def cleanup(self):
        with self.peer.setup_lock(), self.view.locked():
            self.preflight()
            existing = self.event("intent")
            proof = self.private_proof(existing)
            intent = self.checkpoint("intent", proof if existing is None else None)
            self.private_proof(intent)
            self.preflight()
            self.remove_authorization()
            self.checkpoint("authorization-removed")
            self.preflight()
            proof = self.private_proof(intent)
            folder = self.peer.PRIVATE / SETUP
            for name in ("identity", "identity.pub", "known_hosts"):
                if name in proof:
                    path = folder / name
                    require(identity(checked(path, 0o644 if name == "identity.pub" else 0o600))
                            == proof[name], "Private file changed before exact deletion")
                    self.worker_fresh()
                    path.unlink()
                    sync(folder)
            if folder.exists():
                require(not list(checked(folder, directory=True).iterdir()), "Private folder not empty; retain")
                self.worker_fresh()
                folder.rmdir()
                sync(folder.parent)
            self.checkpoint("identity-removed")
            self.preflight()
            self.checkpoint("pointer-retire-intent")
            pointer = self.peer.PUBLIC / "current.json"
            if pointer.exists():
                require(read(pointer, 0o444) == {"setup_id": SETUP}, "Replacement setup pointer; preserve")
                self.worker_fresh()
                pointer.unlink()
                sync(pointer.parent)
            self.checkpoint("pointer-retired")
            self.preflight()
            return self.publish()

    def publish(self):
        require(not (self.peer.PRIVATE / SETUP).exists()
                and not (self.peer.PUBLIC / "current.json").exists()
                and all(not p.exists() for p in self.peer.ssh_files(self.value)),
                "Owned SSH setup not fully retired")
        self.peer.validate_policy(self.value, self.authorization, installed=False)
        body = {"protocol": "terminal-peer-cleanup/v1", "setup_id": SETUP, "run_id": RUN,
                "node": self.node, "source_sha256": self.source, "terminals": TERMINALS,
                "observed_epoch": self.clock(), "outcome": "OWNED-PEER-SETUP-CLEANED",
                "original_records_preserved": True, "maintenance_success": False,
                "ssh_validated_without_restart": True}
        self.preserve_proof(body)
        signed = BASE / "publication.json"
        self.core.atomic(signed, canonical(body))
        signature = Path(str(signed) + ".sig")
        if signature.exists():
            checked(signature).unlink()
        native(["/usr/bin/ssh-keygen", "-Y", "sign", "-f", "/etc/ssh/ssh_host_ed25519_key",
                "-n", NAMESPACE, str(signed)])
        native(["/usr/bin/ssh-keygen", "-Y", "verify", "-f", str(self.signers()), "-I", self.node,
                "-n", NAMESPACE, "-s", str(checked(signature))], canonical(body))
        for path in (PUBLIC.parent, PUBLIC, PUBLIC / self.node):
            if not path.exists():
                path.mkdir(mode=0o755)
                path.chmod(0o755)
            checked(path, 0o755, directory=True)
        name = "worker-peer-cleanup" if self.node == WORKER else "controller-peer-cleanup"
        destination = PUBLIC / self.node / (name + ".json")
        self.core.atomic(Path(str(destination) + ".sig"), signature.read_bytes(), 0o444)
        self.core.atomic(destination, canonical(body), 0o444)
        return body


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("operation", choices=("audit", "cleanup"))
    parser.add_argument("--reviewed-source-sha256", required=True)
    args = parser.parse_args()
    os.umask(0o077)
    operation = Cleanup(args.reviewed_source_sha256)
    if args.operation == "audit":
        with operation.peer.setup_lock(), operation.view.locked():
            operation.preflight(require_worker=False)
            operation.private_proof(operation.event("intent"))
        result = {"setup_id": SETUP, "node": operation.node, "outcome": "SSH-CLEANUP-AUDIT-ACCEPTED",
                  "actuation": False, "maintenance_success": False}
    else:
        result = operation.cleanup()
    print(json.dumps(result))


if __name__ == "__main__":
    try:
        main()
    except (OSError, ValueError, KeyError, TypeError, RuntimeError, subprocess.SubprocessError) as error:
        print("STOP: peer cleanup rejected; no inferred cleanup or maintenance success", file=sys.stderr)
        print("Technical gate: " + str(error), file=sys.stderr)
        sys.exit(1)
