#!/usr/bin/env python3
"""Source-only simulations of the separately authorized historical cancellation."""

from contextlib import ExitStack, contextmanager
import copy
import importlib.util
import json
import os
from pathlib import Path
import stat
import tempfile
import unittest
from unittest.mock import patch


DIRECTORY = Path(__file__).resolve().parent


def load(name, filename):
    spec = importlib.util.spec_from_file_location(name, DIRECTORY / filename)
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


r = load("unstarted_recovery", "node-maintenance-cancel-unstarted.py")
c = load("unstarted_old_units", "node-maintenance-cold.py")


class Interrupted(Exception):
    pass


class RecoveryFixture:
    real_metadata = False

    def setUp(self):
        self.stack = ExitStack()
        self.addCleanup(self.stack.close)
        self.root = Path(self.stack.enter_context(tempfile.TemporaryDirectory(dir=Path.cwd())))
        self.now = r.START + 100
        self.nodes = {}
        self.calls = []
        self.lock_paths = []
        self.fail = None
        self.lock_value = {"metadata": {"name": "operator-node-maintenance", "namespace": "kube-system",
            "uid": r.LOCK_UID, "resourceVersion": "435298222", "creationTimestamp": "2026-11-25T06:34:05Z"},
            "data": {"run_id": r.RUN, "host_step_id": r.STEP, "plan_sha256": "",
                     "started_epoch": str(r.START), "deadline_epoch": str(r.DEADLINE)}}
        hashes = {"source.py": r.sha(b"original source")}
        plan = {"source_bindings": hashes, "bindings": {
            node: {"boot_id": node + "-original-boot", "kernel": "7.0.0-38"} for node in (r.SERVER, r.WORKER)}}
        approval = r.sha(r.canonical(plan))
        contract = {"run_id": r.RUN, "host_step_id": r.STEP, "plan_sha256": approval,
                    "source_bindings": hashes, "started_epoch": r.START, "deadline_epoch": r.DEADLINE}
        self.stack.enter_context(patch.multiple(r, HASHES=hashes, APPROVAL=approval,
                                               CONTRACT_SHA=r.sha(r.canonical(contract))))
        def exists(path, *args, **kwargs):
            Path(path).lstat()
            return Path(path)
        if not self.real_metadata:
            self.stack.enter_context(patch.object(r, "checked", side_effect=exists))
            self.stack.enter_context(patch.object(r, "sync_directory"))
            self.stack.enter_context(patch.object(r.os, "fchmod", create=True))
        self.stack.enter_context(patch.object(r.time, "time", side_effect=lambda: self.now))
        self.stack.enter_context(patch.object(r, "run", side_effect=self.native))
        if not self.real_metadata:
            self.stack.enter_context(patch.object(r, "mutex", side_effect=self.mutex))
        self.lock_value["data"]["plan_sha256"] = approval
        for node in (r.SERVER, r.WORKER):
            base = self.root / node
            for name in ("base/inbox", "original", "system", "cgroup/system.slice", "public"):
                (base / name).mkdir(parents=True)
            if self.real_metadata:
                for name in ("base", "base/inbox", "original"):
                    (base / name).chmod(0o700)
                for name in ("system", "cgroup", "cgroup/system.slice", "public"):
                    (base / name).chmod(0o755)
            (base / "mountinfo").write_text("29 23 0:26 / /sys/fs/cgroup rw - cgroup2 cgroup rw\n")
            (base / "proc-locks").write_text("")
            for index in range(4):
                (base / ("package-lock-" + str(index))).write_bytes(b"")
            obj = r.Recovery.__new__(r.Recovery)
            obj.node, obj.other = node, r.WORKER if node == r.SERVER else r.SERVER
            obj.source = "e" * 64
            obj.original, obj.pub, obj.local = base / "original", base / "public", base / "base/authority.json"
            obj.plan, obj.contract, obj.old, obj.client = plan, contract, c, "fixture-kubectl"
            obj.local_key = obj.other_key = "ssh-ed25519 fixture-public-key"
            obj.verify_host = lambda: None
            (obj.original / "source.py").write_bytes(b"original source")
            (obj.original / "plan.json").write_bytes(r.canonical(plan))
            state = {"last_epoch": r.START}
            if node == r.SERVER:
                state.update(run_id=r.RUN, host_step_id=r.STEP, plan_sha256=approval,
                    started_epoch=r.START, deadline_epoch=r.DEADLINE, worker_contract=contract,
                    worker_contract_sha256=r.CONTRACT_SHA, mode="recovering", host_slot_cancelled=True,
                    guard_registered=True, worker_ready_verified=False,
                    completed=False, mutations={}, host_started=False, host_complete=False,
                    host_packages_complete=False, host_reboot_authorized=False)
            else:
                state.update(contract=contract, contract_sha256=r.CONTRACT_SHA, phase="staged", pid=0)
            (obj.original / "state.json").write_bytes(r.canonical(state))
            if self.real_metadata:
                for path in obj.original.iterdir():
                    path.chmod(0o600)
            units = {}
            for name, body in obj.unit_bodies().items():
                path = base / "system" / name
                path.write_bytes(body.encode())
                if self.real_metadata:
                    path.chmod(0o644)
                units[name] = {"LoadState": "loaded", "ActiveState": "inactive", "Job": "",
                    "FragmentPath": str(path), "DropInPaths": ""}
                if name.endswith(".service"):
                    units[name].update(MainPID="0", ControlPID="0", ControlGroup="", User="root")
                else:
                    units[name]["Unit"] = name.removesuffix(".timer") + ".service"
            for target in ("shutdown.target", "reboot.target"):
                units[target] = {"LoadState": "loaded", "ActiveState": "inactive", "Job": ""}
            for name in ("apt-daily.service", "apt-daily-upgrade.service"):
                units[name] = {"LoadState": "loaded", "ActiveState": "inactive", "Job": "",
                               "MainPID": "0", "ControlPID": "0", "User": "", "ControlGroup": ""}
            self.nodes[node] = {"object": obj, "base": base, "units": units}
        self.original_controller = self.nodes[r.SERVER]["object"].original.joinpath("state.json").read_bytes()
        self.original_worker = self.nodes[r.WORKER]["object"].original.joinpath("state.json").read_bytes()

    @contextmanager
    def mutex(self, path):
        self.lock_paths.append(str(path))
        yield

    @contextmanager
    def on(self, node):
        previous = getattr(self, "current", None)
        self.current = node
        base = self.nodes[node]["base"]
        with patch.multiple(r, BASE=base / "base", PUBLIC=base / "public", SYSTEM=base / "system",
                            RUNTIME_SYSTEM=base / "system", CGROUP=base / "cgroup", MOUNTINFO=base / "mountinfo",
                            PROC_LOCKS=base / "proc-locks",
                            PACKAGE_LOCKS=tuple(base / ("package-lock-" + str(i)) for i in range(4))), \
                patch.object(r.os, "major", create=True, return_value=0), \
                patch.object(r.os, "minor", create=True, return_value=0):
            yield self.nodes[node]["object"]
        self.current = previous

    def native(self, args, data=None, timeout=60):
        self.calls.append((self.current, args, data))
        if self.fail:
            self.fail(args)
        units = self.nodes[self.current]["units"]
        if args[:2] == ["/usr/bin/systemctl", "show"]:
            value = units.get(args[2], {"LoadState": "not-found", "ActiveState": "inactive",
                "Job": "", "FragmentPath": "", "DropInPaths": ""})
            value = copy.deepcopy(value)
            if args[2].endswith(".service") and value["LoadState"] == "not-found":
                value.update(MainPID="0", ControlPID="0", ControlGroup="")
            return "\n".join(k + "=" + v for k, v in value.items()).encode()
        if args[:2] == ["/usr/bin/systemctl", "stop"]:
            for unit in args[2:]:
                units[unit]["ActiveState"] = "inactive"
                if unit.endswith(".service"):
                    units[unit]["MainPID"] = "0"
            return b""
        if args[:2] == ["/usr/bin/systemctl", "daemon-reload"]:
            guard = "node-maintenance-cold-" + r.RUN + "-guard.service"
            path = r.SYSTEM / (guard + ".d") / "90-reviewed-cancel-unstarted.conf"
            if path.exists():
                units[guard]["DropInPaths"] = str(path)
            return b""
        if args[:3] == ["/usr/bin/ssh-keygen", "-Y", "sign"]:
            path = Path(args[-1])
            Path(str(path) + ".sig").write_bytes((self.current + ":" + r.sha(path.read_bytes())).encode())
            return b""
        if args[:3] == ["/usr/bin/ssh-keygen", "-Y", "verify"]:
            identity = args[args.index("-I") + 1]
            self.assertEqual(args[args.index("-n") + 1], r.SIGN_NAMESPACE)
            signature = Path(args[args.index("-s") + 1]).read_bytes()
            r.require(signature == (identity + ":" + r.sha(data)).encode(), "Fixture signature refused")
            return b""
        if args[0] == "fixture-kubectl":
            if "delete" in args:
                body = json.loads(data)
                self.assertEqual(body, {"apiVersion": "v1", "kind": "DeleteOptions",
                    "preconditions": {"uid": r.LOCK_UID, "resourceVersion": "435298222"}})
                self.assertIn("--raw=" + r.LOCK_URI, args)
                self.assertEqual(args[-2:], ["-f", "-"])
                self.lock_value = None
                return b'{"kind":"Status","status":"Success"}'
            self.assertIn("--raw=" + r.LOCK_LIST_URI, args)
            return r.canonical({"metadata": {}, "items": [] if self.lock_value is None else [self.lock_value]})
        if args[:2] == ["/usr/bin/systemd-analyze", "verify"]:
            return b""
        raise AssertionError("Unexpected native command: " + repr(args))

    def transfer(self, sender, receiver, kind):
        src = self.nodes[sender]["object"].pub / (kind + ".json")
        dst = self.nodes[receiver]["base"] / "base/inbox" / src.name
        for suffix in ("", ".sig"):
            destination = Path(str(dst) + suffix)
            destination.write_bytes(Path(str(src) + suffix).read_bytes())
            if self.real_metadata:
                destination.chmod(0o444)

    def prepared(self):
        with self.on(r.WORKER) as worker:
            worker.worker_audit()
        self.transfer(r.WORKER, r.SERVER, "worker-audit")
        with self.on(r.SERVER) as controller:
            controller.controller_fence()
        self.transfer(r.SERVER, r.WORKER, "controller-fence")

    def cancelled(self):
        self.prepared()
        with self.on(r.WORKER) as worker:
            worker.worker_cancel()
        self.transfer(r.WORKER, r.SERVER, "worker-cancel")

    def modify_state(self, node, updates):
        path = self.nodes[node]["object"].original / "state.json"
        value = json.loads(path.read_bytes())
        value.update(updates)
        path.write_bytes(r.canonical(value))


class RecoveryTests(RecoveryFixture, unittest.TestCase):
    def test_native_empty_jobs_typed_timers_and_genuine_terminal_retirement(self):
        self.cancelled()
        with self.on(r.SERVER) as controller:
            proof = controller.controller_retire()
            self.assertFalse(proof["maintenance_success"])
            self.assertTrue(proof["original_lock_absent"])
            self.assertEqual((r.BASE / "controller-state-original.json").read_bytes(), self.original_controller)
            self.assertEqual((controller.original / "state.json").read_bytes(), self.original_controller)
        with self.on(r.WORKER) as worker:
            state = worker.state()
            self.assertEqual(state["phase"], "cancelled")
            self.assertNotIn("finalizer_ack", state)
            self.assertEqual((r.BASE / "worker-state-original.json").read_bytes(), self.original_worker)
        self.assertTrue(any(path.endswith("recovery-mutex") for path in self.lock_paths))
        stops = [args for _, args, _ in self.calls if args[:2] == ["/usr/bin/systemctl", "stop"]]
        self.assertEqual(len(stops), 2)
        self.assertFalse(any("-os.service" in unit or "reboot" in unit for args in stops for unit in args[2:]))

    def test_missing_malformed_and_queued_jobs_never_cancel(self):
        unit = "node-maintenance-cold-worker-" + r.RUN + ".service"
        for job in (None, " ", "00", "bad", "18", "18 /org/freedesktop/systemd1/job/18"):
            with self.subTest(job=job), self.on(r.WORKER) as worker:
                props = self.nodes[r.WORKER]["units"][unit]
                if job is None:
                    props.pop("Job", None)
                else:
                    props["Job"] = job
                with self.assertRaises(RuntimeError):
                    worker.worker_audit()
        self.assertIsNotNone(self.lock_value)

    def test_package_reboot_shutdown_children_and_unknown_units_fail_closed(self):
        with self.on(r.WORKER) as worker:
            for unit in (f"node-maintenance-{r.STEP}-os.service",
                         f"node-maintenance-reboot-{r.STEP}.timer", "shutdown.target"):
                with self.subTest(unit=unit):
                    old = self.nodes[r.WORKER]["units"].get(unit)
                    self.nodes[r.WORKER]["units"][unit] = {"LoadState": "loaded", "ActiveState": "active", "Job": ""}
                    with self.assertRaises((RuntimeError, KeyError)):
                        worker.worker_idle()
                    if old is None:
                        del self.nodes[r.WORKER]["units"][unit]
                    else:
                        self.nodes[r.WORKER]["units"][unit] = old
            unit = f"node-maintenance-{r.STEP}-os.service"
            group = r.CGROUP / "system.slice" / unit
            group.mkdir()
            (group / "cgroup.events").write_text("populated 1\nfrozen 0\n")
            with self.assertRaises(RuntimeError):
                worker.worker_idle()
            (group / "cgroup.events").write_text("frozen 0\n")
            with self.assertRaises(RuntimeError):
                worker.worker_idle()

    def test_started_claimed_mutated_and_foreign_cancellation_rejected(self):
        for node, updates in ((r.SERVER, {"mutations": {"app": {"scale": 0}}}),
                (r.SERVER, {"host_started": True}), (r.SERVER, {"host_reboot_authorized": True}),
                (r.SERVER, {"worker_ready_verified": True}),
                (r.SERVER, {"drain_pid": 0}), (r.SERVER, {"deadline_epoch": r.DEADLINE + 1}),
                (r.WORKER, {"phase": "claimed"}), (r.WORKER, {"pid": 9}),
                (r.WORKER, {"finalizer_ack": {}}), (r.WORKER, {"phase": "cancelled"})):
            with self.subTest(node=node, updates=updates), self.on(node) as obj:
                path = obj.original / "state.json"
                saved = path.read_bytes()
                self.modify_state(node, updates)
                with self.assertRaises((RuntimeError, KeyError)):
                    obj.state()
                path.write_bytes(saved)

    def test_original_source_plan_boot_and_operator_clock_drift(self):
        with self.on(r.WORKER) as worker:
            worker.state()
            for path in (worker.original / "source.py", worker.original / "plan.json"):
                saved = path.read_bytes()
                path.write_bytes(b"drift")
                with self.assertRaises((RuntimeError, ValueError)):
                    worker.state()
                path.write_bytes(saved)
            self.now -= 1
            with self.assertRaises(RuntimeError):
                worker.state()
            self.now += 1
            with patch.object(worker, "verify_host", side_effect=RuntimeError("Boot drift")), \
                    self.assertRaises(RuntimeError):
                worker.state()

    def test_signed_assertion_injection_role_source_replay_and_clock_rejected(self):
        with self.on(r.WORKER) as worker:
            worker.worker_audit()
        self.transfer(r.WORKER, r.SERVER, "worker-audit")
        with self.on(r.SERVER) as controller:
            path = r.BASE / "inbox/worker-audit.json"
            saved = path.read_bytes()
            for field, value in (("source_sha256", "f" * 64), ("node", r.SERVER), ("run_id", "foreign"),
                                 ("observed_epoch", self.now + 1), ("observed_epoch", self.now - 120),
                                 ("payload", "arbitrary-command")):
                body = json.loads(saved)
                body[field] = value
                path.write_bytes(r.canonical(body))
                Path(str(path) + ".sig").write_bytes((r.WORKER + ":" + r.sha(path.read_bytes())).encode())
                with self.subTest(field=field), self.assertRaises(RuntimeError):
                    controller.evidence("worker-audit")
            path.write_bytes(saved)
            Path(str(path) + ".sig").write_bytes(b"unchecked assertion")
            with self.assertRaises(RuntimeError):
                controller.controller_fence()

    def test_guard_and_finalizer_races_reprove_before_terminal_actions(self):
        self.prepared()
        def mutate(args):
            if args[:2] == ["/usr/bin/systemctl", "stop"]:
                self.modify_state(r.WORKER, {"phase": "claimed"})
        self.fail = mutate
        with self.on(r.WORKER) as worker, self.assertRaises(RuntimeError):
            worker.worker_cancel()
        self.assertIsNotNone(self.lock_value)
        self.assertFalse((self.nodes[r.WORKER]["object"].pub / "worker-cancel.json").exists())

    def test_guard_validation_or_stop_failure_retains_lock_and_resumes(self):
        with self.on(r.WORKER) as worker:
            worker.worker_audit()
        self.transfer(r.WORKER, r.SERVER, "worker-audit")
        def failure(args):
            if args[:2] == ["/usr/bin/systemd-analyze", "verify"]:
                raise Interrupted()
        self.fail = failure
        with self.on(r.SERVER) as controller, self.assertRaises(Interrupted):
            controller.controller_fence()
        self.assertIsNotNone(self.lock_value)
        self.fail = None
        with self.on(r.SERVER) as controller:
            controller.controller_fence()
            controller.verify_inhibition()
        self.assertEqual(self.nodes[r.SERVER]["object"].original.joinpath("state.json").read_bytes(),
                         self.original_controller)

    def test_worker_committed_cancellation_before_sign_interruption_is_restartable(self):
        self.prepared()
        def failure(args):
            if args[:3] == ["/usr/bin/ssh-keygen", "-Y", "sign"]:
                raise Interrupted()
        self.fail = failure
        with self.on(r.WORKER) as worker, self.assertRaises(Interrupted):
            worker.worker_cancel()
        self.fail = None
        with self.on(r.WORKER) as worker:
            self.assertEqual(worker.state()["phase"], "cancelled")
            proof = worker.worker_cancel()
            self.assertFalse(proof["old_finalizer_ack"])

    def test_precondition_conflict_and_replacement_lock_never_deleted(self):
        self.cancelled()
        def replace(args):
            if args[0] == "fixture-kubectl" and "delete" in args:
                self.lock_value["metadata"]["uid"] = "replacement-uid"
                raise RuntimeError("Native precondition conflict")
        self.fail = replace
        with self.on(r.SERVER) as controller, self.assertRaises(RuntimeError):
            controller.controller_retire()
        self.fail = None
        with self.on(r.SERVER) as controller:
            proof = controller.controller_retire()
            self.assertTrue(proof["replacement_lock_untouched"])
        self.assertEqual(self.lock_value["metadata"]["uid"], "replacement-uid")
        self.assertEqual(sum("delete" in args for _, args, _ in self.calls), 1)

    def test_original_lock_uid_rv_data_and_creation_drift_retains(self):
        with self.on(r.SERVER) as controller:
            original = copy.deepcopy(self.lock_value)
            for area, field, value in (("metadata", "uid", "other"),
                    ("metadata", "resourceVersion", "bad"), ("metadata", "creationTimestamp", "other"),
                    ("data", "deadline_epoch", str(r.DEADLINE + 1))):
                self.lock_value = copy.deepcopy(original)
                self.lock_value[area][field] = value
                with self.subTest(field=field), self.assertRaises(RuntimeError):
                    controller.lock()
            self.lock_value = original

    def test_changed_resource_version_requires_new_round_not_unconditional_retry(self):
        self.cancelled()
        self.lock_value["metadata"]["resourceVersion"] = "435298223"
        with self.on(r.SERVER) as controller, self.assertRaises(RuntimeError):
            controller.controller_retire()
        self.assertFalse(any("delete" in args for _, args, _ in self.calls))

    def test_finalizers_pending_is_not_terminal_and_retry_is_restartable(self):
        self.cancelled()
        def pending(args):
            if args[0] == "fixture-kubectl" and "delete" in args:
                self.lock_value["metadata"]["deletionTimestamp"] = "2026-11-25T07:00:00Z"
                raise Interrupted()
        self.fail = pending
        with self.on(r.SERVER) as controller, self.assertRaises(Interrupted):
            controller.controller_retire()
        self.fail = None
        with self.on(r.SERVER) as controller:
            result = controller.controller_retire()
            self.assertEqual(result["lock_retirement"], "finalizers-pending")
            self.assertFalse((controller.pub / "controller-terminal.json").exists())
            self.lock_value = None
            self.now += 500
            result = controller.controller_retire()
            self.assertFalse(result["maintenance_success"])

    def test_delete_accepted_response_lost_and_terminal_signing_interruption(self):
        self.cancelled()
        def lost(args):
            if args[0] == "fixture-kubectl" and "delete" in args:
                self.lock_value = None
                raise Interrupted()
        self.fail = lost
        with self.on(r.SERVER) as controller, self.assertRaises(Interrupted):
            controller.controller_retire()
        def signing(args):
            if args[:3] == ["/usr/bin/ssh-keygen", "-Y", "sign"]:
                raise Interrupted()
        self.fail = signing
        with self.on(r.SERVER) as controller, self.assertRaises(Interrupted):
            controller.controller_retire()
        self.fail = None
        with self.on(r.SERVER) as controller:
            controller.controller_retire()
            controller.controller_retire()
        self.assertEqual(sum("delete" in args for _, args, _ in self.calls), 1)

    def test_atomic_creation_and_publication_interruption_preserve_old_artifacts(self):
        with self.on(r.SERVER):
            path = r.BASE / "authority-fixture.json"
            with patch.object(r.os, "link", side_effect=Interrupted()), self.assertRaises(Interrupted):
                r.create(path, b"complete")
            self.assertFalse(path.exists())
            r.create(path, b"complete")
            with patch.object(r.os, "replace", side_effect=Interrupted()), self.assertRaises(Interrupted):
                r.atomic(path, b"replacement")
            self.assertEqual(path.read_bytes(), b"complete")
            r.atomic(path, b"replacement")
            self.assertEqual(path.read_bytes(), b"replacement")

    def test_native_service_identity_and_timer_target_missing_or_foreign_reject(self):
        with self.on(r.WORKER) as worker:
            service = "node-maintenance-cold-worker-" + r.RUN + ".service"
            timer = "node-maintenance-cold-worker-" + r.RUN + "-finalize.timer"
            units = self.nodes[r.WORKER]["units"]
            for name, field, value in ((service, "MainPID", None), (service, "ControlPID", "bad"),
                    (service, "ControlGroup", "/foreign"), (service, "FragmentPath", "/foreign"),
                    (service, "DropInPaths", "/foreign"), (timer, "Unit", None),
                    (timer, "Unit", "arbitrary.service")):
                saved = copy.deepcopy(units[name])
                if value is None:
                    units[name].pop(field)
                else:
                    units[name][field] = value
                with self.subTest(field=field), self.assertRaises((RuntimeError, KeyError)):
                    worker.actor(name)
                units[name] = saved

    def test_witness_before_intent_interruption_accepts_fresh_reproof_not_rewrite(self):
        self.cancelled()
        original_create = r.create
        def interrupted(path, data, mode=0o600):
            if path.name.startswith("delete-intent-"):
                raise Interrupted()
            return original_create(path, data, mode)
        with self.on(r.SERVER) as controller:
            with patch.object(r, "create", side_effect=interrupted), self.assertRaises(Interrupted):
                controller.controller_retire()
            witness = (r.BASE / "retirement-witness-435298222.json").read_bytes()
        self.now += 1
        with self.on(r.WORKER) as worker:
            worker.worker_cancel()
        self.transfer(r.WORKER, r.SERVER, "worker-cancel")
        with self.on(r.SERVER) as controller:
            controller.controller_retire()
            self.assertEqual((r.BASE / "retirement-witness-435298222.json").read_bytes(), witness)

    def test_old_expiry_does_not_reset_clock_or_grant_maintenance_authority(self):
        self.cancelled()
        self.now = r.SETUP_EXPIRY + 10
        with self.on(r.WORKER) as worker:
            worker.worker_audit()
        self.transfer(r.WORKER, r.SERVER, "worker-audit")
        with self.on(r.SERVER) as controller:
            controller.controller_fence()
        self.transfer(r.SERVER, r.WORKER, "controller-fence")
        with self.on(r.WORKER) as worker:
            worker.worker_cancel()
        self.transfer(r.WORKER, r.SERVER, "worker-cancel")
        with self.on(r.SERVER) as controller:
            proof = controller.controller_retire()
            self.assertEqual(proof["deadline_epoch"], r.DEADLINE)
            self.assertFalse(proof["maintenance_success"])

    def test_guard_cgroup_and_state_race_after_stopping_retains_lock(self):
        with self.on(r.WORKER) as worker:
            worker.worker_audit()
        self.transfer(r.WORKER, r.SERVER, "worker-audit")
        def mutation(args):
            if args[:2] == ["/usr/bin/systemctl", "stop"]:
                self.modify_state(r.SERVER, {"mutations": {"unexpected": {}}})
        self.fail = mutation
        with self.on(r.SERVER) as controller, self.assertRaises(RuntimeError):
            controller.controller_fence()
        self.assertIsNotNone(self.lock_value)
        self.assertFalse((self.nodes[r.SERVER]["object"].pub / "controller-fence.json").exists())

    def test_torn_public_receipt_and_intent_tamper_never_retire(self):
        self.cancelled()
        with self.on(r.SERVER) as controller:
            signature = r.BASE / "inbox/worker-cancel.json.sig"
            saved = signature.read_bytes()
            signature.write_bytes(b"torn-publication")
            with self.assertRaises(RuntimeError):
                controller.controller_retire()
            self.assertIsNotNone(self.lock_value)
            signature.write_bytes(saved)
            def interruption(args):
                if args[0] == "fixture-kubectl" and "delete" in args:
                    raise Interrupted()
            self.fail = interruption
            with self.assertRaises(Interrupted):
                controller.controller_retire()
            self.fail = None
            intent = r.BASE / "delete-intent-435298222.json"
            value = json.loads(intent.read_bytes())
            value["uid"] = "foreign"
            intent.write_bytes(r.canonical(value))
            with self.assertRaises(RuntimeError):
                controller.controller_retire()
            self.assertIsNotNone(self.lock_value)

    def test_cgroup_subtree_namespace_and_hidden_bind_mount_fail_closed(self):
        with self.on(r.WORKER) as worker:
            original = r.MOUNTINFO.read_text()
            for contents in (
                original.replace(" / /sys/fs/cgroup", " /subtree /sys/fs/cgroup"),
                original + "30 29 0:26 /empty /sys/fs/cgroup/system.slice rw - cgroup2 cgroup rw\n"):
                r.MOUNTINFO.write_text(contents)
                with self.assertRaises(RuntimeError):
                    worker.worker_idle()

    def test_unloaded_foreign_dropin_is_rejected_before_reload(self):
        with self.on(r.WORKER) as worker:
            worker.worker_audit()
        self.transfer(r.WORKER, r.SERVER, "worker-audit")
        with self.on(r.SERVER) as controller:
            unit = "node-maintenance-cold-" + r.RUN + "-guard.service"
            directory = r.SYSTEM / (unit + ".d")
            directory.mkdir()
            (directory / "99-foreign.conf").write_text("[Unit]\nConditionPathExists=\n")
            with self.assertRaises(RuntimeError):
                controller.controller_fence()
        self.assertFalse(any(args[:2] == ["/usr/bin/systemctl", "daemon-reload"]
                             for _, args, _ in self.calls))

    def test_other_package_service_and_kernel_lock_owners_never_stopped(self):
        with self.on(r.WORKER) as worker:
            service = self.nodes[r.WORKER]["units"]["apt-daily.service"]
            service["Job"] = "56"
            with self.assertRaises(RuntimeError):
                worker.worker_idle()
            service["Job"] = ""
            inode = r.PACKAGE_LOCKS[0].stat().st_ino
            r.PROC_LOCKS.write_text(f"1: POSIX ADVISORY WRITE 901 00:00:{inode} 0 EOF\n")
            with self.assertRaises(RuntimeError):
                worker.worker_idle()
            r.PROC_LOCKS.write_text("malformed\n")
            with self.assertRaises(RuntimeError):
                worker.worker_idle()
        self.assertFalse(any("stop" in args for _, args, _ in self.calls))

    def test_real_host_validator_rejects_original_boot_kernel_platform_ip_drift(self):
        with self.on(r.WORKER) as worker:
            boot = r.BASE / "fixture-boot"
            release = r.BASE / "fixture-os"
            boot.write_text(worker.plan["bindings"][r.WORKER]["boot_id"] + "\n")
            release.write_text('ID=ubuntu\nVERSION_ID="26.04"\n')
            with patch.multiple(r, BOOT=boot, OS_RELEASE=release), \
                    patch.object(r.socket, "gethostname", return_value=r.WORKER), \
                    patch.object(r.platform, "system", return_value="Linux"), \
                    patch.object(r.platform, "machine", return_value="x86_64"), \
                    patch.object(r.platform, "release", return_value="7.0.0-38"), \
                    patch.object(r, "run", return_value=r.canonical([{"addr_info": [{"local": r.IPS[r.WORKER]}]}])):
                r.Recovery.verify_host(worker)
                boot.write_text("new-boot\n")
                with self.assertRaises(RuntimeError):
                    r.Recovery.verify_host(worker)
                boot.write_text(worker.plan["bindings"][r.WORKER]["boot_id"])
                release.write_text('ID=other\nVERSION_ID="26.04"\n')
                with self.assertRaises(RuntimeError):
                    r.Recovery.verify_host(worker)
                release.write_text('ID=ubuntu\nVERSION_ID="26.04"\n')
                with patch.object(r, "run", return_value=b'[{"addr_info":[{"local":"192.168.1.11"}]}]'), \
                        self.assertRaises(RuntimeError):
                    r.Recovery.verify_host(worker)
                with patch.object(r.platform, "release", return_value="other"), self.assertRaises(RuntimeError):
                    r.Recovery.verify_host(worker)
                with patch.object(r.platform, "machine", return_value="other"), self.assertRaises(RuntimeError):
                    r.Recovery.verify_host(worker)

    def test_worker_receipt_expiring_inside_final_api_reproof_retains_lock(self):
        self.cancelled()
        self.now += 119
        reads = 0
        def delay(args):
            nonlocal reads
            if args[0] == "fixture-kubectl" and "get" in args:
                reads += 1
                if reads == 2:
                    self.now += 2
        self.fail = delay
        with self.on(r.SERVER) as controller, self.assertRaises(RuntimeError):
            controller.controller_retire()
        self.assertEqual(reads, 2)
        self.assertIsNotNone(self.lock_value)
        self.assertFalse(any("delete" in args for _, args, _ in self.calls))

    def test_worker_receipt_expiring_inside_systemd_reproof_retains_lock(self):
        self.cancelled()
        self.now += 119
        reads = 0
        guard = "node-maintenance-cold-" + r.RUN + "-guard.service"
        def delay(args):
            nonlocal reads
            if args[:3] == ["/usr/bin/systemctl", "show", guard]:
                reads += 1
                if reads == 2:
                    self.now += 2
        self.fail = delay
        with self.on(r.SERVER) as controller, self.assertRaises(RuntimeError):
            controller.controller_retire()
        self.assertIsNotNone(self.lock_value)
        self.assertFalse(any("delete" in args for _, args, _ in self.calls))

    def test_changed_authenticated_worker_receipt_after_reproof_retains_lock(self):
        self.cancelled()
        reads = 0
        def change(args):
            nonlocal reads
            if args[0] == "fixture-kubectl" and "get" in args:
                reads += 1
                if reads == 2:
                    path = r.BASE / "inbox/worker-cancel.json"
                    body = json.loads(path.read_bytes())
                    body["observed_epoch"] -= 1
                    path.write_bytes(r.canonical(body))
                    Path(str(path) + ".sig").write_bytes((r.WORKER + ":" + r.sha(path.read_bytes())).encode())
        self.fail = change
        with self.on(r.SERVER) as controller, self.assertRaises(RuntimeError):
            controller.controller_retire()
        self.assertIsNotNone(self.lock_value)
        self.assertFalse(any("delete" in args for _, args, _ in self.calls))

    def test_clock_rollback_during_final_authentication_retains_lock(self):
        self.cancelled()
        self.now += 119
        ready = False
        reads = 0
        def rollback(args):
            nonlocal ready, reads
            if args[0] == "fixture-kubectl" and "get" in args:
                reads += 1
                ready = reads == 2
            elif ready and args[:3] == ["/usr/bin/ssh-keygen", "-Y", "verify"]:
                self.now -= 1
                ready = False
        self.fail = rollback
        with self.on(r.SERVER) as controller, self.assertRaises(RuntimeError):
            controller.controller_retire()
        self.assertIsNotNone(self.lock_value)
        self.assertFalse(any("delete" in args for _, args, _ in self.calls))


@unittest.skipUnless(os.name == "posix" and getattr(os, "geteuid", lambda: -1)() == 0,
                     "Actual POSIX root ownership/umask filesystem required; no permission mocks")
class RecoveryPermissionTests(RecoveryFixture, unittest.TestCase):
    real_metadata = True

    def test_real_restrictive_umask_creates_exact_guard_directory_and_restores_mask(self):
        previous = os.umask(0o077)
        try:
            control = self.root / "umask-control"
            control.mkdir(mode=0o755)
            self.assertEqual(stat.S_IMODE(control.stat().st_mode), 0o700)
            self.prepared()
            with self.on(r.SERVER) as controller:
                unit = "node-maintenance-cold-" + r.RUN + "-guard.service"
                directory = controller.inhibit_path(unit).parent
                self.assertEqual(stat.S_IMODE(directory.lstat().st_mode), 0o755)
                self.assertEqual(directory.lstat().st_uid, 0)
                self.assertEqual(stat.S_IMODE(controller.inhibit_path(unit).stat().st_mode), 0o644)
                current = os.umask(0o077)
                self.assertEqual(current, 0o077)
                controller.verify_inhibition()
        finally:
            os.umask(previous)

    def test_real_created_directory_interruption_retries_without_chmod_or_clock_reset(self):
        previous = os.umask(0o077)
        try:
            with self.on(r.WORKER) as worker:
                worker.worker_audit()
            self.transfer(r.WORKER, r.SERVER, "worker-audit")
            with self.on(r.SERVER) as controller:
                original_sync = r.sync_directory
                def interruption(path):
                    original_sync(path)
                    if path == r.SYSTEM:
                        raise Interrupted()
                with patch.object(r, "sync_directory", side_effect=interruption), self.assertRaises(Interrupted):
                    controller.controller_fence()
                unit = "node-maintenance-cold-" + r.RUN + "-guard.service"
                path = controller.inhibit_path(unit)
                self.assertEqual(stat.S_IMODE(path.parent.lstat().st_mode), 0o755)
                self.assertFalse(path.exists())
                fence = (r.BASE / "guard-inhibited").read_bytes()
                identity = (path.parent.stat().st_dev, path.parent.stat().st_ino)
                controller.controller_fence()
                self.assertEqual((path.parent.stat().st_dev, path.parent.stat().st_ino), identity)
                self.assertEqual((r.BASE / "guard-inhibited").read_bytes(), fence)
                self.assertEqual((controller.original / "state.json").read_bytes(), self.original_controller)
                controller.verify_inhibition()
            self.assertIsNotNone(self.lock_value)
        finally:
            os.umask(previous)

    def test_real_foreign_directory_modes_contents_and_symlink_are_never_repaired(self):
        with self.on(r.WORKER) as worker:
            worker.worker_audit()
        self.transfer(r.WORKER, r.SERVER, "worker-audit")
        with self.on(r.SERVER) as controller:
            unit = "node-maintenance-cold-" + r.RUN + "-guard.service"
            directory = controller.inhibit_path(unit).parent
            directory.mkdir(mode=0o700)
            before = directory.lstat()
            with self.assertRaises(RuntimeError):
                controller.controller_fence()
            self.assertEqual(stat.S_IMODE(directory.lstat().st_mode), 0o700)
            self.assertEqual(directory.lstat().st_ino, before.st_ino)
            directory.rmdir()
            directory.mkdir(mode=0o755)
            directory.chmod(0o755)
            foreign = directory / "99-unrelated.conf"
            foreign.write_bytes(b"[Unit]\nDescription=Unrelated\n")
            foreign.chmod(0o644)
            with self.assertRaises(RuntimeError):
                controller.controller_fence()
            self.assertEqual(foreign.read_bytes(), b"[Unit]\nDescription=Unrelated\n")
            foreign.unlink()
            os.chown(directory, 65534, -1)
            with self.assertRaises(RuntimeError):
                controller.controller_fence()
            self.assertEqual(directory.lstat().st_uid, 65534)
            os.chown(directory, 0, -1)
            directory.rmdir()
            target = self.root / "unrelated-directory"
            target.mkdir(mode=0o700)
            directory.symlink_to(target, target_is_directory=True)
            with self.assertRaises(RuntimeError):
                controller.controller_fence()
            self.assertTrue(directory.is_symlink())
            self.assertEqual(stat.S_IMODE(target.lstat().st_mode), 0o700)
        self.assertIsNotNone(self.lock_value)


if __name__ == "__main__":
    unittest.main()
