"""Keep Longhorn manager recovery separate from data-plane upgrades."""

from pathlib import Path
import unittest

import yaml


ROOT = Path(__file__).resolve().parents[2]
RELEASE = yaml.safe_load(
    (ROOT / "infrastructure/storage/longhorn/helmrelease-longhorn.yaml").read_text()
)
VALUES = RELEASE["spec"]["values"]


class LonghornManagerResourceTests(unittest.TestCase):
    def test_manager_has_bounded_cpu_and_memory_headroom(self):
        self.assertEqual(VALUES["longhornManager"]["resources"], {
            "requests": {"cpu": "250m", "memory": "512Mi"},
            "limits": {"cpu": "2000m", "memory": "2Gi"},
        })

    def test_recovery_keeps_current_chart_and_sequential_rollout(self):
        self.assertEqual(RELEASE["spec"]["chart"]["spec"]["version"], "1.12.1")
        self.assertEqual(VALUES["longhornManager"]["updateStrategy"], {
            "rollingUpdate": {"maxUnavailable": 1},
        })
        self.assertNotIn("defaultEngineImage", VALUES["defaultSettings"])

    def test_replica_and_control_plane_protections_are_preserved(self):
        self.assertEqual(VALUES["persistence"]["defaultClassReplicaCount"], 2)
        self.assertIs(VALUES["defaultSettings"]["replicaSoftAntiAffinity"], False)
        self.assertEqual(VALUES["defaultSettings"]["concurrentReplicaRebuildPerNodeLimit"], 1)
        patches = RELEASE["spec"]["postRenderers"][0]["kustomize"]["patches"]
        manager = next(p for p in patches if p["target"]["name"] == "longhorn-manager")
        affinity = yaml.safe_load(manager["patch"])["spec"]["template"]["spec"]["affinity"]
        terms = affinity["nodeAffinity"]["requiredDuringSchedulingIgnoredDuringExecution"]
        self.assertEqual(terms["nodeSelectorTerms"], [{
            "matchExpressions": [{
                "key": "node-role.kubernetes.io/control-plane",
                "operator": "DoesNotExist",
            }],
        }])

    def test_manager_readiness_tolerates_bounded_control_plane_load(self):
        patches = RELEASE["spec"]["postRenderers"][0]["kustomize"]["patches"]
        manager = next(p for p in patches if p["target"]["name"] == "longhorn-manager")
        containers = yaml.safe_load(manager["patch"])["spec"]["template"]["spec"]["containers"]
        self.assertEqual(containers, [{
            "name": "longhorn-manager",
            "readinessProbe": {"timeoutSeconds": 5},
        }])

    def test_ci_runs_manager_regression_tests(self):
        workflow = yaml.safe_load((ROOT / ".github/workflows/ci.yaml").read_text())
        commands = [s.get("run", "") for s in workflow["jobs"]["yaml-lint"]["steps"]]
        self.assertIn("python3 .github/scripts/test-longhorn-manager-resources.py", commands)


if __name__ == "__main__":
    unittest.main()
