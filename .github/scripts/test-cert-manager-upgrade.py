"""Preserve certificate ownership, CRD management and resource bounds on upgrades."""

from pathlib import Path
import unittest

import yaml


ROOT = Path(__file__).resolve().parents[2]
RELEASE = yaml.safe_load(
    (ROOT / "infrastructure/cert-manager/helmrelease-cert-manager.yaml").read_text()
)


class CertManagerUpgradeTests(unittest.TestCase):
    def test_release_keeps_its_chart_source_and_namespace(self):
        self.assertEqual(RELEASE["metadata"], {
            "name": "cert-manager",
            "namespace": "cert-manager",
        })
        chart = RELEASE["spec"]["chart"]["spec"]
        self.assertEqual(chart["chart"], "cert-manager")
        self.assertRegex(chart["version"], r"^v\d+\.\d+\.\d+$")
        self.assertEqual(chart["sourceRef"], {
            "kind": "HelmRepository",
            "name": "jetstack",
            "namespace": "cert-manager",
        })

    def test_crds_and_private_key_ownership_remain_managed(self):
        values = RELEASE["spec"]["values"]
        self.assertIs(values["installCRDs"], True)
        self.assertEqual(values["extraArgs"], ["--enable-certificate-owner-ref=true"])
        self.assertEqual(
            values["global"]["priorityClassName"], "infrastructure-high"
        )

    def test_upgrade_preserves_controller_resource_bounds(self):
        values = RELEASE["spec"]["values"]
        for component, cpu, memory, limit_cpu, limit_memory in (
            (values, "25m", "64Mi", "100m", "128Mi"),
            (values["webhook"], "10m", "32Mi", "50m", "64Mi"),
            (values["cainjector"], "25m", "64Mi", "100m", "128Mi"),
        ):
            with self.subTest(component=component):
                self.assertEqual(component["resources"], {
                    "requests": {"cpu": cpu, "memory": memory},
                    "limits": {"cpu": limit_cpu, "memory": limit_memory},
                })

    def test_ci_runs_certificate_upgrade_guards(self):
        workflow = yaml.safe_load((ROOT / ".github/workflows/ci.yaml").read_text())
        commands = [
            step.get("run", "") for step in workflow["jobs"]["yaml-lint"]["steps"]
        ]
        self.assertIn(
            "python3 .github/scripts/test-cert-manager-upgrade.py", commands
        )


if __name__ == "__main__":
    unittest.main()
