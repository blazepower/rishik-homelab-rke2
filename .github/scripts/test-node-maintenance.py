#!/usr/bin/env python3
"""Focused maintenance regressions; never connects to a node or cluster."""

import copy
import hashlib
import importlib.util
import json
from pathlib import Path
import stat
import sys
import tempfile
from types import SimpleNamespace
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
    def plex_fixture(self):
        policy = json.loads(m.POLICY.read_text())
        labels = {"app": "plex", "controller-revision-hash": "plex-revision"}
        pod = {"kind": "Pod", "metadata": {
            "name": "plex-plex-media-server-0", "namespace": "media", "uid": "old-pod",
            "labels": labels, "ownerReferences": [{"controller": True, "kind": "StatefulSet",
                                                 "name": "plex-plex-media-server", "uid": "plex-controller"}]},
            "spec": {"nodeName": "rishik-worker1", "nodeSelector": {"kubernetes.io/hostname": "rishik-worker1"},
                     "containers": [{"name": "plex", "image": "public-synthetic:1", "resources": {
                         "requests": {"cpu": "500m", "memory": "1Gi", "gpu.intel.com/i915": "1"}},
                         "volumeMounts": [{"name": "kube-api-access-abc12", "mountPath": "/account",
                                           "readOnly": True}]}],
                     "volumes": [{"name": "dri", "hostPath": {"path": "/dev/dri", "type": "Directory"}},
                                 {"name": "config", "persistentVolumeClaim": {"claimName": "plex-config"}},
                                 {"name": "kube-api-access-abc12", "projected": {
                                     "sources": [{"serviceAccountToken": {"path": "token", "expirationSeconds": 3607}}]}}]},
            "status": {"phase": "Running", "conditions": [{"type": "Ready", "status": "True"}]}}
        template = {"metadata": {"labels": {"app": "plex"}}, "spec": copy.deepcopy(pod["spec"])}
        template["spec"].pop("nodeName")
        controller = {"kind": "StatefulSet", "metadata": {
            "name": "plex-plex-media-server", "namespace": "media", "uid": "plex-controller", "generation": 3},
            "spec": {"replicas": 1, "selector": {"matchLabels": {"app": "plex"}}, "template": template},
            "status": {"observedGeneration": 3, "currentRevision": "plex-revision",
                       "updateRevision": "plex-revision", "readyReplicas": 1}}
        policy["plex_downtime"] = {"template_sha256": m.sha(m.canonical(template)),
                                    "pod_spec_sha256": m.pending_spec_hash(pod)}
        with patch.object(m, "get", return_value=controller):
            review = m.reviewed_plex_downtime(policy, [pod])
        replacement = copy.deepcopy(pod)
        replacement["metadata"]["uid"] = "replacement-pod"
        replacement["spec"].pop("nodeName")
        replacement["spec"]["volumes"][-1]["name"] = "kube-api-access-def34"
        replacement["spec"]["containers"][0]["volumeMounts"][0]["name"] = "kube-api-access-def34"
        replacement["status"] = {"phase": "Pending", "conditions": [
            {"type": "PodScheduled", "status": "False", "reason": "Unschedulable"}]}
        return policy, controller, pod, replacement, review

    def test_plex_downtime_is_exact_and_not_peer_migration_capacity(self):
        policy, controller, pod, replacement, review = self.plex_fixture()
        peer = {"metadata": {"name": "rishik-worker2", "labels": {}},
                "spec": {}, "status": {"allocatable": {"cpu": "4", "memory": "4Gi", "pods": "100"}}}
        self.assertTrue(m.scheduling_blockers("rishik-worker1", peer, [pod]))
        self.assertFalse(m.scheduling_blockers("rishik-worker1", peer, [pod], pending_reviews=review))
        unrelated = copy.deepcopy(pod)
        unrelated["metadata"].update(name="another-app", uid="unrelated-pod")
        self.assertTrue(m.scheduling_blockers("rishik-worker1", peer, [pod, unrelated], pending_reviews=review))
        with patch.object(m, "get", return_value=controller):
            self.assertEqual(m.expected_pending_pods(policy, [replacement], review, "rishik-worker1"),
                             ({"replacement-pod"}, {"https://plex.homelab/identity"}))
            for node in ("rishik-worker2", "rishik-controller", None):
                with self.subTest(node=node), self.assertRaises(RuntimeError):
                    m.expected_pending_pods(policy, [replacement], review, node)

    def test_plex_pending_never_hides_crashes_missing_or_drifted_workloads(self):
        policy, controller, pod, replacement, review = self.plex_fixture()
        for case in ("missing", "terminating", "old-uid", "scheduled", "crash", "not-unschedulable",
                     "owner", "revision", "image", "init", "volume", "projection", "ephemeral"):
            changed = copy.deepcopy(replacement)
            if case == "terminating":
                changed["metadata"]["deletionTimestamp"] = "2026-11-22T00:00:00Z"
            elif case == "old-uid":
                changed["metadata"]["uid"] = pod["metadata"]["uid"]
            elif case == "scheduled":
                changed["spec"]["nodeName"] = "rishik-worker1"
            elif case == "crash":
                changed["status"]["phase"] = "Running"
            elif case == "not-unschedulable":
                changed["status"]["conditions"] = []
            elif case == "owner":
                changed["metadata"]["ownerReferences"][0]["uid"] = "lookalike-controller"
            elif case == "revision":
                changed["metadata"]["labels"]["controller-revision-hash"] = "new-revision"
            elif case == "image":
                changed["spec"]["containers"][0]["image"] = "different-image"
            elif case == "init":
                changed["spec"]["initContainers"] = [{"name": "added", "image": "another-image"}]
            elif case == "volume":
                changed["spec"]["volumes"][0]["hostPath"]["path"] = "/etc"
            elif case == "projection":
                changed["spec"]["volumes"][-1]["projected"]["sources"] = []
            elif case == "ephemeral":
                changed["spec"]["ephemeralContainers"] = [{"name": "debug", "image": "debug-image"}]
            with self.subTest(case=case), patch.object(m, "get", return_value=controller), \
                    self.assertRaises(RuntimeError):
                m.expected_pending_pods(policy, [] if case == "missing" else [changed], review, "rishik-worker1")
        for case in ("recreated", "rollout", "replicas", "template"):
            changed = copy.deepcopy(controller)
            if case == "recreated":
                changed["metadata"]["uid"] = "recreated-controller"
            elif case == "rollout":
                changed["status"]["updateRevision"] = "new-revision"
            elif case == "replicas":
                changed["spec"]["replicas"] = 2
            else:
                changed["spec"]["template"]["spec"]["containers"][0]["image"] = "new-image"
            with self.subTest(case=case), patch.object(m, "get", return_value=changed), \
                    self.assertRaises(RuntimeError):
                m.expected_pending_pods(policy, [replacement], review, "rishik-worker1")
        unhealthy = copy.deepcopy(pod)
        unhealthy["status"]["conditions"] = []
        with patch.object(m, "get", return_value=controller), self.assertRaises(RuntimeError):
            m.reviewed_plex_downtime(policy, [unhealthy])

    def test_plex_pending_health_preserves_storage_other_pods_and_all_recovery_probes(self):
        policy, controller, pod, replacement, review = self.plex_fixture()
        managers = [{"metadata": {"name": n, "uid": n, "labels": {"app": "longhorn-manager"}}, "spec": {"nodeName": n},
                     "status": {"conditions": [{"type": "Ready", "status": "True"}]}}
                    for n in ("rishik-worker1", "rishik-worker2")]
        expected = {n["name"]: n for n in policy["nodes"]}
        resources = {("nodes", None): [], ("pods", policy["storage_namespace"]): managers}

        def get(resource, namespace=None, name=None):
            if resource == "statefulset":
                return controller
            if resource == "helmrelease":
                return {"spec": {"suspend": True}}
            return {"value": "block-if-contains-last-replica"}

        with patch.object(m, "get", side_effect=get), patch.object(m, "kube") as kube, \
                patch.object(m, "items", side_effect=lambda r, ns=None: resources.get((r, ns), [])), \
                patch.object(m, "check_nodes", return_value=expected), \
                patch.object(m, "quarantine_state", return_value={}), \
                patch.object(m, "storage_check", return_value={}) as storage, \
                patch.object(m.urllib.request, "urlopen") as probe:
            probe.return_value.__enter__.return_value.status = 200
            result = m.health(policy, [replacement], draining_node="rishik-worker1", pending_reviews=review)
            self.assertEqual(result["expected_pending"], ["replacement-pod"])
            self.assertEqual([c.args[0] for c in probe.call_args_list], ["https://vault.homelab/"])
            storage.assert_called_once()
            self.assertTrue(any("healthcheck" in c.args for c in kube.call_args_list))
            unrelated = copy.deepcopy(replacement)
            unrelated["metadata"].update(name="unrelated", uid="unrelated")
            with self.assertRaisesRegex(RuntimeError, "Active pods not Ready"):
                m.health(policy, [replacement, unrelated], draining_node="rishik-worker1", pending_reviews=review)
            storage.side_effect = RuntimeError("storage drift")
            with self.assertRaisesRegex(RuntimeError, "storage drift"):
                m.health(policy, [replacement], draining_node="rishik-worker1", pending_reviews=review)
            storage.side_effect = None
            with self.assertRaisesRegex(RuntimeError, "Active pods not Ready"):
                m.health(policy, [replacement])
            probe.reset_mock()
            m.health(policy, [pod])
            self.assertEqual([c.args[0] for c in probe.call_args_list], policy["probe_urls"])

    def test_outage_budget_reserves_recovery_and_does_not_reset_with_clock_changes(self):
        with patch.object(m.time, "time", return_value=100), patch.object(m.time, "monotonic", return_value=200):
            budget = m.OutageBudget()
        with patch.object(m.time, "time", return_value=101), patch.object(m.time, "monotonic", return_value=201):
            budget.before_disruption()
        with patch.object(m.time, "time", return_value=101), \
                patch.object(m.time, "monotonic", return_value=200 + 10 * 3600), self.assertRaises(RuntimeError):
            budget.before_disruption()
        with patch.object(m.time, "time", return_value=100 + 12 * 3600), \
                patch.object(m.time, "monotonic", return_value=201), self.assertRaises(RuntimeError):
            budget.remaining()
        with patch.object(m.time, "time", return_value=99), self.assertRaises(RuntimeError):
            budget.remaining()

    def test_recovery_wait_cannot_report_success_after_outage_deadline(self):
        budget = m.OutageBudget()
        with patch.object(budget, "remaining", side_effect=[1, RuntimeError("deadline")]), \
                self.assertRaisesRegex(RuntimeError, "deadline"):
            m.wait_for(lambda: "recovered too late", budget=budget)

    def test_recovery_directory_requires_off_node_private_root_owned_mount(self):
        for mount, uid, mode, accepted in [
            ({"target": "/recovery", "fstype": "nfs4"}, 0, 0o700, True),
            ({"target": "/", "fstype": "ext4"}, 0, 0o700, False),
            ({"target": "/recovery", "fstype": "nfs4"}, 1000, 0o700, False),
            ({"target": "/recovery", "fstype": "nfs4"}, 0, 0o755, False),
        ]:
            with self.subTest(mount=mount, uid=uid, mode=mode), \
                    patch.object(h.Path, "is_dir", return_value=True), \
                    patch.object(h.Path, "stat", return_value=SimpleNamespace(st_uid=uid, st_mode=stat.S_IFDIR | mode)), \
                    patch.object(h, "command", return_value=json.dumps({"filesystems": [mount]})):
                if accepted:
                    h.recovery_directory("/recovery/private")
                else:
                    with self.assertRaises(RuntimeError):
                        h.recovery_directory("/recovery/private")

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

    def test_kubectl_must_cover_both_current_and_target_api(self):
        m.check_client_skew("v1.34.2", "v1.35.10+rke2r1")
        m.check_client_skew("v1.36.6", "v1.35.10+rke2r1", "v1.36.6+rke2r1")
        m.check_client_skew("v1.36.6", "v1.36.6+rke2r1", "v1.37.2+rke2r1")
        for client, current, target in [
            ("v1.34.2", "v1.35.10+rke2r1", "v1.36.6+rke2r1"),
            ("v1.35.10", "v1.36.6+rke2r1", "v1.37.2+rke2r1"),
            ("v1.37.2", "v1.35.10+rke2r1", "v1.36.6+rke2r1"),
            ("v2.35.0", "v1.35.10+rke2r1", None),
        ]:
            with self.subTest(client=client, target=target), self.assertRaisesRegex(RuntimeError, "one-minor skew"):
                m.check_client_skew(client, current, target)

    def test_unparseable_and_prerelease_clients_are_not_accepted(self):
        for value in ("", "unknown", "v1.36", "v1.36.0-rc.1"):
            with self.subTest(value=value), self.assertRaisesRegex(RuntimeError, "Unsupported"):
                m.check_client_skew(value, "v1.35.10+rke2r1")

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

    def test_post_drain_accepts_only_the_selected_node_additional_cordon(self):
        policy = json.loads(m.POLICY.read_text())
        nodes = [{"metadata": {"name": n["name"]},
                  "spec": {"unschedulable": n["role"] == "server"},
                  "status": {"conditions": [
                      {"type": "Ready", "status": "True"},
                      {"type": "MemoryPressure", "status": "False"},
                  ]}} for n in policy["nodes"]]
        target = next(n for n in nodes if n["metadata"]["name"] == "rishik-worker2")
        target["spec"]["unschedulable"] = True
        m.check_nodes(policy, nodes, "rishik-worker2")
        for failure in ("target-uncordoned", "peer-cordoned", "controller-uncordoned", "peer-not-ready",
                        "pressure", "unknown-target"):
            changed = copy.deepcopy(nodes)
            by_name = {n["metadata"]["name"]: n for n in changed}
            if failure == "target-uncordoned":
                by_name["rishik-worker2"]["spec"]["unschedulable"] = False
            elif failure == "peer-cordoned":
                by_name["rishik-worker1"]["spec"]["unschedulable"] = True
            elif failure == "controller-uncordoned":
                by_name["rishik-controller"]["spec"]["unschedulable"] = False
            elif failure == "peer-not-ready":
                by_name["rishik-worker1"]["status"]["conditions"][0]["status"] = "False"
            elif failure == "pressure":
                by_name["rishik-worker1"]["status"]["conditions"][1]["status"] = "True"
            with self.subTest(failure=failure), self.assertRaises(RuntimeError):
                m.check_nodes(policy, changed, "unknown" if failure == "unknown-target" else "rishik-worker2")

    def test_post_drain_rechecks_full_health_and_rejects_remaining_evictable_pods(self):
        node = {"name": "worker"}
        pod = {"metadata": {"name": "app", "namespace": "media"},
               "spec": {"nodeName": "worker"}, "status": {"phase": "Running"}}
        with patch.object(m, "items", return_value=[pod]), patch.object(m, "health") as health, \
                self.assertRaisesRegex(RuntimeError, "Drain left active evictable pods"):
            try:
                m.drained_health({}, node)
            finally:
                health.assert_not_called()
        daemon = copy.deepcopy(pod)
        daemon["metadata"]["ownerReferences"] = [{"kind": "DaemonSet"}]
        completed = copy.deepcopy(pod)
        completed["status"]["phase"] = "Succeeded"
        with patch.object(m, "items", return_value=[daemon, completed]), \
                patch.object(m, "health", return_value={"attached_two_rw": 1}) as health:
            self.assertEqual(m.drained_health({}, node), {"attached_two_rw": 1})
            health.assert_called_once_with({}, [daemon, completed], draining_node="worker")

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

    def socket_fixture(self):
        pod = {"metadata": {"namespace": "storage", "name": "csi-attacher-pod", "uid": "pod",
                           "labels": {"app": "csi-attacher"}, "ownerReferences": [
                               {"kind": "ReplicaSet", "name": "csi-attacher-rs", "uid": "rs", "controller": True}]},
               "status": {"phase": "Running"}, "spec": {
                   "nodeName": "old", "containers": [{
                       "name": "csi-attacher", "image": "reviewed-image",
                       "volumeMounts": [{"name": "socket-dir", "mountPath": "/csi/"}]}],
                   "volumes": [{"name": "socket-dir", "hostPath": {
                       "path": "/var/lib/kubelet/plugins/driver.longhorn.io", "type": "DirectoryOrCreate"}}]}}
        replica = {"metadata": {"namespace": "storage", "name": "csi-attacher-rs", "uid": "rs",
                               "ownerReferences": [{"kind": "Deployment", "name": "csi-attacher",
                                                    "uid": "deployment", "controller": True}]}}
        deployment = {"metadata": {"namespace": "storage", "name": "csi-attacher", "uid": "deployment"},
                      "spec": {"selector": {"matchLabels": {"app": "csi-attacher"}},
                               "template": {"spec": {"containers": [{
                                   "name": "csi-attacher", "image": "reviewed-image"}]}}}}
        return pod, replica, deployment

    def test_exact_csi_socket_review_preserves_other_drain_checks(self):
        pod, replica, deployment = self.socket_fixture()
        reviews = m.reviewed_csi_sockets([pod], [replica], [deployment], "storage")
        self.assertEqual(len(reviews), 1)
        self.assertEqual(reviews[0]["deployment_uid"], "deployment")
        node = {"metadata": {"name": "new", "labels": {}}, "spec": {},
                "status": {"allocatable": {"cpu": "6", "memory": "16Gi", "pods": "250"}}}
        self.assertTrue(m.scheduling_blockers("old", node, [pod]))
        self.assertFalse(m.scheduling_blockers("old", node, [pod], reviews))
        pod["spec"]["nodeSelector"] = {"kubernetes.io/hostname": "old"}
        self.assertTrue(m.scheduling_blockers("old", node, [pod], reviews))

    def test_csi_review_never_becomes_a_generic_hostpath_or_identity_exception(self):
        cases = {
            "stale-owner": lambda p, r, d: p["metadata"]["ownerReferences"][0].update(uid="missing"),
            "replaced-deployment": lambda p, r, d: d["metadata"].update(uid="replacement"),
            "other-controller": lambda p, r, d: d["metadata"].update(name="csi-nfs-controller"),
            "nfs-host-data": lambda p, r, d: p["spec"]["volumes"][0]["hostPath"].update(path="/var/lib/kubelet/pods"),
            "longhorn-data": lambda p, r, d: p["spec"]["volumes"][0]["hostPath"].update(path="/var/lib/longhorn"),
            "extra-hostpath": lambda p, r, d: p["spec"]["volumes"].append(
                {"name": "other", "hostPath": {"path": "/data", "type": "Directory"}}),
            "data-claim": lambda p, r, d: p["spec"]["volumes"].append(
                {"name": "data", "persistentVolumeClaim": {"claimName": "user-data"}}),
            "bidirectional": lambda p, r, d: p["spec"]["containers"][0]["volumeMounts"][0].update(
                mountPropagation="Bidirectional"),
            "subpath": lambda p, r, d: p["spec"]["containers"][0]["volumeMounts"][0].update(subPath="other"),
            "unreviewed-image": lambda p, r, d: p["spec"]["containers"][0].update(image="different"),
        }
        for label, change in cases.items():
            with self.subTest(case=label):
                pod, replica, deployment = self.socket_fixture()
                change(pod, replica, deployment)
                self.assertEqual(m.reviewed_csi_sockets([pod], [replica], [deployment], "storage"), [])

    def test_failure_retains_lock_and_never_maintains_second_node(self):
        policy = json.loads(m.POLICY.read_text())
        plan = {"action": "os", "release": None, "quarantine": {}, "kubectl": {}, "reviewed_csi_sockets": [],
                "nodes": policy["nodes"], "bindings": {
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
                patch.object(m, "remote", return_value={"boot_id": "boot", "pending_packages": [],
                                                       "off_node_directory_verified": True}) as remote, \
                patch.object(m, "kube", side_effect=fake_kube), \
                patch.object(m, "run", side_effect=RuntimeError("PDB prevents drain")):
            with self.assertRaises(RuntimeError):
                m.apply(plan, "approval", policy, "/protected/recovery", True, False)
            self.assertFalse(any("delete" in call or "uncordon" in call for call in calls))
            self.assertEqual(remote.call_count, 2)

    def test_successful_drain_with_new_storage_failure_never_changes_host_or_releases_lock(self):
        policy = json.loads(m.POLICY.read_text())
        node = policy["nodes"][1]
        plan = {"action": "os", "release": None, "quarantine": {}, "kubectl": {}, "reviewed_csi_sockets": [],
                "nodes": [node], "bindings": {node["name"]: {
                    "boot_id": "boot", "source_version": "v1.35.10+rke2r1", "pending_packages": []}}}

        def fake_remote(node, payload, privileged=False):
            if privileged:
                raise RuntimeError("privileged host step reached after unhealthy drain")
            return {"boot_id": "boot", "pending_packages": [], "off_node_directory_verified": True}

        with patch("builtins.print"), tempfile.TemporaryDirectory() as output, \
                patch.object(m.tempfile, "mkdtemp", return_value=output), patch.object(m, "check_plan"), \
                patch.object(m, "get", return_value={"metadata": {"uid": "cluster"}}), \
                patch.object(m, "audit_nodes", return_value={}), \
                patch.object(m, "build_plan", return_value={**plan, "blockers": []}), \
                patch.object(m, "stable_health"), patch.object(m, "items", return_value=[]), \
                patch.object(m, "health", side_effect=RuntimeError("post-drain storage drift")), \
                patch.object(m, "etcd_tool", return_value={}), patch.object(m, "capture_evidence", return_value={}), \
                patch.object(m, "remote", side_effect=fake_remote) as remote, \
                patch.object(m, "receipt") as receipt, patch.object(m, "kube") as kube, \
                patch.object(m, "run", return_value="") as run:
            with self.assertRaisesRegex(RuntimeError, "post-drain storage drift"):
                m.apply(plan, "approval", policy, "/protected/recovery", True, False)
            self.assertTrue(any("drain" in call.args[0] for call in run.call_args_list))
            self.assertFalse(any(call.kwargs.get("privileged") for call in remote.call_args_list))
            receipt.assert_not_called()
            self.assertFalse(any("delete" in call.args or "uncordon" in call.args for call in kube.call_args_list))

    def test_missing_recovery_protection_stops_before_lock_or_cordon(self):
        policy = json.loads(m.POLICY.read_text())
        plan = {"action": "os", "release": None, "bindings": {}, "nodes": [], "quarantine": {},
                "kubectl": {}, "reviewed_csi_sockets": []}
        with patch.object(m, "check_plan"), patch.object(m, "get", return_value={"metadata": {"uid": "cluster"}}), \
                patch.object(m, "audit_nodes", return_value={}), \
                patch.object(m, "build_plan", return_value={**plan, "blockers": []}), \
                patch.object(m, "remote", return_value={"off_node_directory_verified": False}), \
                patch.object(m, "kube") as kube, self.assertRaises(RuntimeError):
            try:
                m.apply(plan, "approval", policy, "/private/recovery", True, False)
            finally:
                kube.assert_not_called()

    def test_client_or_socket_drift_stops_before_lock_and_host_operations(self):
        policy = json.loads(m.POLICY.read_text())
        plan = {"action": "rke2", "release": policy["rke2_releases"]["1.36"], "bindings": {},
                "nodes": [], "quarantine": {}, "reviewed_csi_sockets": [],
                "kubectl": {"client": "v1.35.10", "server": "v1.35.10+rke2r1"}}
        for drift in ({"blockers": ["kubectl outside supported skew"]},
                      {"kubectl": {"client": "v1.36.6", "server": "v1.35.10+rke2r1"}},
                      {"reviewed_csi_sockets": [{"pod_uid": "replacement"}]},
                      {"reviewed_plex_downtime": [{"pod_uid": "replacement"}]}):
            with self.subTest(drift=drift), patch.object(m, "check_plan"), \
                    patch.object(m, "get", return_value={"metadata": {"uid": "cluster"}}), \
                    patch.object(m, "audit_nodes", return_value={}), \
                    patch.object(m, "build_plan", return_value={**plan, "blockers": [], **drift}), \
                    patch.object(m, "remote") as remote, patch.object(m, "kube") as kube, \
                    self.assertRaises(RuntimeError):
                try:
                    m.apply(plan, "approval", policy, "/private/recovery", True, True)
                finally:
                    remote.assert_not_called()
                    kube.assert_not_called()

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
        policy, plex_controller, plex_pod, _, plex_review = self.plex_fixture()
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
        plex_pdb = copy.deepcopy(pdb)
        plex_pdb["metadata"]["name"] = "plex-pdb"
        plex_pdb["spec"]["selector"] = {"matchLabels": {"app": "plex"}}
        resources = {"pods": [pod, plex_pod], "nodes": nodes, "poddisruptionbudgets": [pdb, plex_pdb]}
        versions = {"clientVersion": {"gitVersion": "v1.35.10"}, "serverVersion": {"gitVersion": "v1.35.10+rke2r1"}}
        with patch.object(m, "items", side_effect=lambda r: resources.get(r, [])), \
                patch.object(m, "get", side_effect=lambda r, *args, **kwargs: plex_controller if r == "statefulset"
                             else {"metadata": {"uid": "cluster"}}), patch.object(m, "health"), \
                patch.object(m, "kube", side_effect=lambda *args: json.dumps(versions)):
            os_plan = m.build_plan(policy, "os", None, inventory)
            rke_plan = m.build_plan(policy, "rke2", "1.36", inventory)
            versions["clientVersion"]["gitVersion"] = "v1.34.2"
            unsupported = m.build_plan(policy, "rke2", "1.36", inventory)
        self.assertEqual([n["name"] for n in os_plan["nodes"]],
                         ["rishik-worker2", "rishik-worker1", "rishik-controller"])
        self.assertEqual(rke_plan["nodes"][0]["role"], "server")
        self.assertTrue(any("app-pdb" in b for b in os_plan["blockers"]))
        self.assertTrue(any("plex-pdb" in b for b in os_plan["blockers"]))
        self.assertEqual(os_plan["reviewed_plex_downtime"], plex_review)
        self.assertFalse(any("plex-plex-media-server-0: hostPath" in b
                             or "plex-plex-media-server-0: nodeSelector" in b for b in os_plan["blockers"]))
        self.assertEqual(os_plan["kubectl"]["client"], "v1.35.10")
        self.assertTrue(any("one-minor skew" in b for b in unsupported["blockers"]))

    def test_successful_os_run_is_serial_and_keeps_controller_cordoned(self):
        policy, _, _, _, plex_review = self.plex_fixture()
        order = [policy["nodes"][1], policy["nodes"][2], policy["nodes"][0]]
        plan = {"action": "os", "release": None, "quarantine": {}, "kubectl": {}, "reviewed_csi_sockets": [],
                "reviewed_plex_downtime": plex_review,
                "nodes": order, "bindings": {
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
            if payload["operation"] == "recovery-audit":
                return {"off_node_directory_verified": True}
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
                patch.object(m, "health"), patch.object(m, "stable_health"), \
                patch.object(m, "items", return_value=[]), patch.object(m, "reviewed_plex_downtime", return_value=plex_review), \
                patch.object(m, "drained_health", return_value={}) as drained, patch.object(m.time, "sleep"), \
                patch.object(m, "etcd_tool", return_value={}), patch.object(m, "capture_evidence", return_value={}), \
                patch.object(m, "remote", side_effect=fake_remote), patch.object(m, "receipt", side_effect=fake_receipt), \
                patch.object(m, "wait_for", side_effect=lambda check, **kwargs: check()), \
                patch.object(m, "kube", side_effect=fake_kube), patch.object(m, "run", return_value=""):
            m.apply(plan, "approved", policy, "/recovery", True, False)
        self.assertEqual(privileged_steps, ["rishik-worker2", "rishik-worker1", "rishik-controller"])
        self.assertEqual([call.args[1]["name"] for call in drained.call_args_list],
                         ["rishik-worker2", "rishik-worker1", "rishik-controller"])
        self.assertEqual([call.kwargs for call in drained.call_args_list],
                         [{}, {"pending_reviews": plex_review}, {}])
        self.assertNotIn(("uncordon", "rishik-controller"), calls)
        self.assertEqual([c for c in calls if c[0] == "uncordon"],
                         [("uncordon", "rishik-worker2"), ("uncordon", "rishik-worker1")])
        self.assertIn(("-n", "kube-system", "delete", "configmap", m.LOCK), calls)
        acquired = next(c for c in calls if "create" in c and "configmap" in c)
        self.assertTrue(any(c.startswith("--from-literal=started_epoch=") for c in acquired))
        self.assertTrue(any(c.startswith("--from-literal=deadline_epoch=") for c in acquired))

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
