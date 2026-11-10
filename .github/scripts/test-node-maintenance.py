#!/usr/bin/env python3
"""Focused maintenance regressions; never connects to a node or cluster."""

import copy
import hashlib
import importlib.util
import json
from pathlib import Path
import sys
import tempfile
import unittest
from unittest.mock import patch


sys.dont_write_bytecode = True
DIRECTORY = Path(__file__).resolve().parent


def load(name, filename):
    spec = importlib.util.spec_from_file_location(name, DIRECTORY / filename)
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


m = load("maintenance", "node-maintenance.py")
h = load("maintenance_host", "node-maintenance-host.py")


class MaintenanceTests(unittest.TestCase):
    def test_minor_order_and_downgrades(self):
        m.check_transition("v1.35.10+rke2r1", "v1.36.6+rke2r1")
        for current, target in [
            ("v1.35.10+rke2r1", "v1.37.2+rke2r1"),
            ("v1.36.6+rke2r1", "v1.35.10+rke2r1"),
            ("v1.35.10+rke2r1", "v1.35.9+rke2r1"),
        ]:
            with self.subTest(target=target), self.assertRaises(RuntimeError):
                m.check_transition(current, target)

    def test_pdb_selectors(self):
        labels = {"app": "plex", "tier": "media"}
        self.assertTrue(m.matches({}, labels))
        self.assertFalse(m.matches(None, labels))
        self.assertFalse(m.matches({"matchLabels": {"app": "other"}}, labels))
        self.assertTrue(m.matches({"matchExpressions": [
            {"key": "app", "operator": "In", "values": ["plex"]},
            {"key": "missing", "operator": "NotIn", "values": ["x"]},
            {"key": "tier", "operator": "Exists"},
            {"key": "absent", "operator": "DoesNotExist"},
        ]}, labels))
        self.assertFalse(m.matches({"matchExpressions": [
            {"key": "app", "operator": "NotIn", "values": ["plex"]},
        ]}, labels))

    def test_plan_cannot_be_replayed_or_tampered(self):
        plan = {
            "created_epoch": 100, "cluster_uid": "cluster", "action": "os",
            "policy_sha256": m.sha(m.POLICY.read_bytes()),
            "runner_sha256": m.sha(Path(m.__file__).read_bytes()),
            "host_sha256": m.sha(m.HOST.read_bytes()), "blockers": [],
        }
        approval = m.sha(m.canonical(plan))
        m.check_plan(plan, approval, "cluster", now=101)
        for field, value in [("blockers", ["unsafe drain"]), ("cluster_uid", "other")]:
            changed = {**plan, field: value}
            with self.subTest(field=field), self.assertRaises(RuntimeError):
                m.check_plan(changed, approval, "cluster", now=101)
        for now, context in [(15000, "cluster"), (101, "other"), (99, "cluster")]:
            with self.subTest(now=now), self.assertRaises(RuntimeError):
                m.check_plan(plan, approval, context, now=now)
        blocked = {**plan, "blockers": ["unsafe drain"]}
        with self.assertRaises(RuntimeError):
            m.check_plan(blocked, m.sha(m.canonical(blocked)), "cluster", now=101)
        with patch.object(m, "HOST_BYTES", b"different executable source"), self.assertRaises(RuntimeError):
            m.check_plan(plan, approval, "cluster", now=101)

    def storage(self, state="attached"):
        volume = {"metadata": {"name": "vol"}, "spec": {"numberOfReplicas": 2},
                  "status": {"state": state, "robustness": "healthy"}}
        engine = {"spec": {"volumeName": "vol"}, "status": {
            "currentState": "running", "replicaModeMap": {"r1": "RW", "r2": "RW"}}}
        replicas = [
            {"metadata": {"name": name}, "spec": {"volumeName": "vol", "active": True,
             "healthyAt": "now", "nodeID": node}, "status": {"currentState": "running" if state == "attached" else "stopped"}}
            for name, node in [("r1", "worker1"), ("r2", "worker2")]
        ]
        return volume, engine, replicas

    def test_storage_requires_two_clean_distinct_copies(self):
        for state in ("attached", "detached"):
            volume, engine, replicas = self.storage(state)
            self.assertEqual(sum(m.storage_check([volume], [engine], replicas, {"worker1", "worker2"}).values()), 1)
            for failure in ("failed", "same-node", "missing", "degraded"):
                v, e, r = copy.deepcopy((volume, engine, replicas))
                if failure == "failed":
                    r[0]["spec"]["failedAt"] = "now"
                elif failure == "same-node":
                    r[0]["spec"]["nodeID"] = "worker2"
                elif failure == "missing":
                    r.pop()
                elif state == "attached":
                    v["status"]["robustness"] = "degraded"
                else:
                    r[0]["status"]["currentState"] = "running"
                with self.subTest(state=state, failure=failure), self.assertRaises(RuntimeError):
                    m.storage_check([v], [e], r, {"worker1", "worker2"})

    def test_snapshot_embedded_checksum(self):
        with tempfile.TemporaryDirectory() as directory:
            path = Path(directory) / "snapshot"
            data = b"synthetic-database" * 100
            path.write_bytes(data + hashlib.sha256(data).digest())
            self.assertEqual(h.validate_snapshot(path), hashlib.sha256(path.read_bytes()).hexdigest())
            for invalid in [b"", data, data + b"x" * 32]:
                path.write_bytes(invalid)
                with self.assertRaises(RuntimeError):
                    h.validate_snapshot(path)

    def test_capacity_includes_init_and_extended_resources(self):
        pod = {"metadata": {"namespace": "media", "name": "plex", "ownerReferences": [{"kind": "ReplicaSet"}]},
               "status": {"phase": "Running"}, "spec": {
                   "nodeName": "old", "containers": [{"resources": {"requests": {"cpu": "500m", "memory": "1Gi", "gpu": "1"}}}],
                   "initContainers": [{"resources": {"requests": {"memory": "512Mi"}}}]}}
        target = {"metadata": {"name": "new", "labels": {}}, "spec": {}, "status": {
            "allocatable": {"cpu": "2", "memory": "2Gi", "pods": "10", "gpu": "0"}}}
        self.assertTrue(m.scheduling_blockers("old", target, [pod]))
        target["status"]["allocatable"]["gpu"] = "1"
        self.assertFalse(m.scheduling_blockers("old", target, [pod]))
        pod["spec"]["nodeSelector"] = {"kubernetes.io/hostname": "old"}
        self.assertTrue(m.scheduling_blockers("old", target, [pod]))

    def test_failure_retains_lock_and_never_maintains_second_node(self):
        policy = json.loads(m.POLICY.read_text())
        plan = {"action": "os", "release": None, "quarantine": {}, "nodes": policy["nodes"], "bindings": {
            node["name"]: {"boot_id": "boot", "source_version": "v1.35.10+rke2r1",
                           "pending_packages": []} for node in policy["nodes"]}}
        fresh = {**plan, "blockers": []}
        calls = []

        def fake_kube(*args):
            calls.append(args)
            if "drain" in args:
                raise RuntimeError("PDB prevents drain")
            return ""

        with patch("builtins.print"), tempfile.TemporaryDirectory() as output, patch.object(m.tempfile, "mkdtemp", return_value=output), \
                patch.object(m, "check_plan"), patch.object(m, "get", return_value={"metadata": {"uid": "cluster"}}), \
                patch.object(m, "audit_nodes", return_value={}), patch.object(m, "build_plan", return_value=fresh), \
                patch.object(m, "health"), patch.object(m, "stable_health"), patch.object(m.time, "sleep"), \
                patch.object(m, "etcd_tool", return_value={}), \
                patch.object(m, "capture_evidence", return_value={}), \
                patch.object(m, "remote", return_value={"boot_id": "boot", "pending_packages": []}) as remote, \
                patch.object(m, "kube", side_effect=fake_kube), \
                patch.object(m, "run", side_effect=RuntimeError("PDB prevents drain")):
            with self.assertRaises(RuntimeError):
                m.apply(plan, "approval", policy, "/protected/recovery", True, False)
            self.assertFalse(any("delete" in call or "uncordon" in call for call in calls))
            self.assertEqual(remote.call_count, 1)

    def test_headless_upgrade_never_removes_packages_or_stops_server_dependencies(self):
        state = {"boot_id": "boot", "pending_packages": ["Inst synthetic (1.0)"],
                 "kernel_metapackage_installed": True}
        calls = []

        def fake_command(args, **kwargs):
            calls.append(args)
            return ""

        with patch.object(h, "audit", return_value=state), patch.object(h, "command", side_effect=fake_command), \
                patch.object(h, "installed", return_value=True), patch.object(h.Path, "is_file", return_value=True), \
                patch.object(h.subprocess, "run") as process:
            process.return_value.stdout = "loaded"
            h.os_upgrade({"pending_packages": state["pending_packages"],
                          "kernel_package": "linux-generic", "kernel": "test"})
        apt = next(args for args in calls if args[:2] == ["apt-get", "-y"])
        self.assertIn("--no-remove", apt)
        masked = {args[-1] for args in calls if args[:3] == ["systemctl", "mask", "--now"]}
        self.assertIn("display-manager.service", masked)
        self.assertTrue(masked.isdisjoint({
            "NetworkManager.service", "systemd-networkd.service", "iscsid.service",
            "open-iscsi.service", "sshd.service", "rke2-server.service",
            "rke2-agent.service", "thermald.service",
        }))

    def test_refreshed_package_drift_stops_before_upgrade(self):
        states = [{"boot_id": "boot", "pending_packages": [], "kernel_metapackage_installed": True},
                  {"pending_packages": ["Inst changed (2.0)"]}]
        with patch.object(h, "audit", side_effect=states), patch.object(h, "command") as command:
            with self.assertRaises(RuntimeError):
                h.os_upgrade({"pending_packages": ["Inst approved (1.0)"]})
            command.assert_called_once_with(["apt-get", "update"])

    def test_preferred_scheduling_is_not_a_hard_blocker(self):
        pod = {"metadata": {"namespace": "system", "name": "app", "ownerReferences": [{"kind": "ReplicaSet"}]},
               "status": {"phase": "Running"}, "spec": {"nodeName": "old", "containers": [],
               "affinity": {"podAntiAffinity": {"preferredDuringSchedulingIgnoredDuringExecution": [{}]}},
               "topologySpreadConstraints": [{"whenUnsatisfiable": "ScheduleAnyway"}]}}
        node = {"metadata": {"name": "new", "labels": {}}, "spec": {},
                "status": {"allocatable": {"cpu": "6", "memory": "16Gi", "pods": "250"}}}
        self.assertFalse(m.scheduling_blockers("old", node, [pod]))
        pod["spec"]["topologySpreadConstraints"][0]["whenUnsatisfiable"] = "DoNotSchedule"
        self.assertTrue(m.scheduling_blockers("old", node, [pod]))

    def test_etcd_verifier_tracks_actual_server_minor(self):
        policy = json.loads(m.POLICY.read_text())
        for minor in ("3.6", "3.7"):
            pod = {"metadata": {"labels": {"component": "etcd"}}, "spec": {
                "containers": [{"image": f"rancher/hardened-etcd:v{minor}.1-k3s3-build"}]}}
            with patch.object(m, "items", return_value=[pod]):
                self.assertEqual(m.etcd_tool(policy), policy["etcdutl"][minor])
        with patch.object(m, "items", return_value=[]), self.assertRaises(RuntimeError):
            m.etcd_tool(policy)

    def test_quarantine_cannot_be_reattached_or_silently_replaced(self):
        volume = {"metadata": {"name": "legacy", "uid": "v"}, "spec": {},
                  "status": {"state": "detached"}}
        replica = {"metadata": {"uid": "r"}, "spec": {
            "volumeName": "legacy", "nodeID": "rishik-worker1", "desireState": "stopped",
            "healthyAt": "then", "dataDirectoryName": "original"}, "status": {"currentState": "stopped"}}
        engine = {"metadata": {"uid": "e"}, "spec": {"volumeName": "legacy", "salvageRequested": True},
                  "status": {"currentState": "stopped"}}
        policy = {"storage_namespace": "storage", "quarantined_volumes": ["legacy"]}
        with patch.object(m, "get", return_value={"spec": {"attachmentTickets": {}}}):
            baseline = m.quarantine_state(policy, [volume], [engine], [replica])
            with self.assertRaises(RuntimeError):
                m.quarantine_state({**policy, "_quarantine": baseline}, [volume], [engine],
                                   [{**replica, "metadata": {"uid": "replaced"}}])
        with patch.object(m, "get", return_value={"spec": {"attachmentTickets": {"new": {}}}}), \
                self.assertRaises(RuntimeError):
            m.quarantine_state(policy, [volume], [engine], [replica])

    def test_plan_orders_and_pdb_blockers(self):
        policy = json.loads(m.POLICY.read_text())
        nodes = [{"metadata": {"name": n["name"], "labels": {}}, "spec": {},
                  "status": {"nodeInfo": {"kubeletVersion": "v1.35.10+rke2r1"},
                             "allocatable": {"cpu": "6", "memory": "16Gi", "pods": "250"}}}
                 for n in policy["nodes"]]
        pod = {"metadata": {"name": "app", "namespace": "media", "labels": {"app": "test"},
                            "ownerReferences": [{"kind": "ReplicaSet"}]},
               "spec": {"nodeName": "rishik-worker1", "containers": []}, "status": {"phase": "Running"}}
        pdb = {"metadata": {"namespace": "media", "name": "app-pdb", "generation": 1},
               "spec": {"selector": {"matchLabels": {"app": "test"}}},
               "status": {"observedGeneration": 1, "disruptionsAllowed": 0}}
        inventory = {n["name"]: {"name": n["name"], "os": "ubuntu", "os_version": "26.04",
                     "architecture": "x86_64", "root_free_bytes": 10 * 1024**3,
                     "memory_available_bytes": 4 * 1024**3, "sunreclaim_bytes": 0,
                     "boot_free_bytes": 4 * 1024**3, "kernel_metapackage_installed": True,
                     "apt_metadata_epoch": m.time.time(), "boot_id": "boot",
                     "kernel": "test", "pending_packages": []} for n in policy["nodes"]}
        resources = {"pods": [pod], "nodes": nodes, "poddisruptionbudgets": [pdb]}
        with patch.object(m, "items", side_effect=lambda r: resources.get(r, [])), \
                patch.object(m, "get", return_value={"metadata": {"uid": "cluster"}}), patch.object(m, "health"):
            os_plan = m.build_plan(policy, "os", None, inventory)
            rke_plan = m.build_plan(policy, "rke2", "1.36", inventory)
        self.assertEqual([n["name"] for n in os_plan["nodes"]],
                         ["rishik-worker2", "rishik-worker1", "rishik-controller"])
        self.assertEqual(rke_plan["nodes"][0]["role"], "server")
        self.assertTrue(any("app-pdb" in b for b in os_plan["blockers"]))

    def test_successful_os_run_is_serial_and_keeps_controller_cordoned(self):
        policy = json.loads(m.POLICY.read_text())
        order = [policy["nodes"][1], policy["nodes"][2], policy["nodes"][0]]
        plan = {"action": "os", "release": None, "quarantine": {}, "nodes": order, "bindings": {
            n["name"]: {"boot_id": "old", "source_version": "v1.35.10+rke2r1", "pending_packages": []} for n in order}}
        states = {n["name"]: {"boot_id": "old", "pending_packages": []} for n in order}
        lock, calls, privileged_steps = {}, [], []

        def fake_kube(*args):
            calls.append(args)
            for arg in args:
                if arg.startswith("--from-literal=run_id="):
                    lock["run_id"] = arg.split("=")[-1]
            return ""

        def fake_get(resource, namespace=None, name=None):
            if resource == "configmap":
                return {"data": lock}
            return {"metadata": {"uid": "cluster"}, "status": {"conditions": [{"type": "Ready", "status": "True"}]}}

        def fake_remote(node, payload, privileged=False):
            if privileged:
                privileged_steps.append(node["name"])
                states[node["name"]] = {"boot_id": "new", "kernel": policy["kernel"],
                                        "default_target": "multi-user.target", "reboot_required": False}
                return None
            return states[node["name"]]

        def fake_receipt(node, run_id):
            return {"run_id": run_id, "name": node["name"], "operation": "os",
                    "completed_epoch": m.time.time(), "recovery": {"off_node_verified": True}}

        with patch("builtins.print"), tempfile.TemporaryDirectory() as output, patch.object(m.tempfile, "mkdtemp", return_value=output), \
                patch.object(m, "check_plan"), patch.object(m, "get", side_effect=fake_get), \
                patch.object(m, "audit_nodes", return_value={}), \
                patch.object(m, "build_plan", return_value={**plan, "blockers": []}), \
                patch.object(m, "health"), patch.object(m, "stable_health"), patch.object(m.time, "sleep"), \
                patch.object(m, "etcd_tool", return_value={}), patch.object(m, "capture_evidence", return_value={}), \
                patch.object(m, "remote", side_effect=fake_remote), patch.object(m, "receipt", side_effect=fake_receipt), \
                patch.object(m, "wait_for", side_effect=lambda check, **kwargs: check()), \
                patch.object(m, "kube", side_effect=fake_kube), patch.object(m, "run", return_value=""):
            m.apply(plan, "approved", policy, "/recovery", True, False)
        self.assertEqual(privileged_steps, ["rishik-worker2", "rishik-worker1", "rishik-controller"])
        self.assertNotIn(("uncordon", "rishik-controller"), calls)
        self.assertEqual([c for c in calls if c[0] == "uncordon"],
                         [("uncordon", "rishik-worker2"), ("uncordon", "rishik-worker1")])
        self.assertIn(("-n", "kube-system", "delete", "configmap", m.LOCK), calls)

    def test_manager_restart_invalidates_stability_window(self):
        samples = [{"managers": {"worker": {"uid": "first", "restarts": 0}}},
                   {"managers": {"worker": {"uid": "first", "restarts": 1}}}]
        with patch.object(m, "health", side_effect=samples), patch.object(m.time, "sleep"), \
                self.assertRaises(RuntimeError):
            m.stable_health({})

    def test_discovery_updates_only_pinned_minors_and_requires_digests(self):
        policy = {"rke2_releases": {"1.36": {"version": "v1.36.6+rke2r1", "sha256": "old"}}}
        releases = [
            {"tag_name": tag, "prerelease": False, "draft": False,
             "assets": [{"name": "rke2.linux-amd64.tar.gz", "digest": "sha256:" + "a" * 64}]}
            for tag in ("v1.36.7+rke2r1", "v1.37.2+rke2r1")
        ]
        with tempfile.TemporaryDirectory() as directory:
            path = Path(directory) / "policy.json"
            with patch("builtins.print"), patch.object(m, "POLICY", path), patch.object(m, "run", return_value=json.dumps(releases)):
                m.discover(copy.deepcopy(policy))
            proposed = json.loads(path.read_text())
            self.assertEqual(set(proposed["rke2_releases"]), {"1.36"})
            self.assertEqual(proposed["rke2_releases"]["1.36"]["version"], "v1.36.7+rke2r1")
            original = path.read_bytes()
            releases[0]["assets"][0]["digest"] = None
            with patch.object(m, "POLICY", path), patch.object(m, "run", return_value=json.dumps(releases)), \
                    self.assertRaises(RuntimeError):
                m.discover(copy.deepcopy(policy))
            self.assertEqual(path.read_bytes(), original)


if __name__ == "__main__":
    unittest.main(verbosity=2)
