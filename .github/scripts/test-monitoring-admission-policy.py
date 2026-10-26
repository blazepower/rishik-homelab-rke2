"""Keep API-server admission access narrowly scoped within monitoring."""

import ipaddress
from pathlib import Path
import unittest

import yaml


ROOT = Path(__file__).resolve().parents[2]
POLICIES = ROOT / "infrastructure" / "policies" / "network-policies"
FILENAME = "allow-prometheus-admission.yaml"


class MonitoringAdmissionPolicyTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.policy = yaml.safe_load((POLICIES / FILENAME).read_text())

    def test_only_selects_the_monitoring_operator(self):
        self.assertEqual(self.policy["apiVersion"], "networking.k8s.io/v1")
        self.assertEqual(self.policy["kind"], "NetworkPolicy")
        self.assertEqual(self.policy["metadata"]["namespace"], "monitoring")
        self.assertEqual(self.policy["spec"]["podSelector"], {
            "matchLabels": {
                "app": "kube-prometheus-stack-operator",
                "release": "kube-prometheus-stack",
            },
        })

    def test_only_adds_ingress_on_the_backend_https_port(self):
        spec = self.policy["spec"]
        self.assertEqual(set(spec), {"podSelector", "policyTypes", "ingress"})
        self.assertEqual(spec["policyTypes"], ["Ingress"])
        self.assertEqual(len(spec["ingress"]), 1)
        self.assertEqual(set(spec["ingress"][0]), {"from", "ports"})
        self.assertEqual(spec["ingress"][0]["ports"], [{"protocol": "TCP", "port": 10250}])

    def test_only_allows_the_controller_host_addresses(self):
        sources = self.policy["spec"]["ingress"][0]["from"]
        self.assertCountEqual(sources, [
            {"ipBlock": {"cidr": "192.168.1.10/32"}},
            {"ipBlock": {"cidr": "10.42.0.0/32"}},
        ])
        for source in sources:
            self.assertEqual(ipaddress.ip_network(source["ipBlock"]["cidr"]).prefixlen, 32)

    def test_policy_is_wired_into_the_existing_kustomizations(self):
        resources = yaml.safe_load((POLICIES / "kustomization.yaml").read_text())["resources"]
        self.assertEqual(resources.count(FILENAME), 1)
        parent = yaml.safe_load((POLICIES.parent / "kustomization.yaml").read_text())
        self.assertIn("network-policies", parent["resources"])

    def test_ci_runs_the_policy_guard(self):
        workflow = yaml.safe_load((ROOT / ".github" / "workflows" / "ci.yaml").read_text())
        commands = [step.get("run", "") for step in workflow["jobs"]["yaml-lint"]["steps"]]
        self.assertIn("python3 .github/scripts/test-monitoring-admission-policy.py", commands)


if __name__ == "__main__":
    unittest.main()
