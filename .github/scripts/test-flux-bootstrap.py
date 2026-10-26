"""Keep Flux's complete, coordinated bootstrap bundle under protected GitOps."""

from pathlib import Path
import re
import unittest

import yaml


ROOT = Path(__file__).resolve().parents[2]
CLUSTER = ROOT / "clusters" / "production"
BOOTSTRAP = CLUSTER / "flux-system"
COMPONENTS_TEXT = (BOOTSTRAP / "gotk-components.yaml").read_text()
COMPONENTS = list(yaml.safe_load_all(COMPONENTS_TEXT))
SYNC = list(yaml.safe_load_all((BOOTSTRAP / "gotk-sync.yaml").read_text()))


class FluxBootstrapTests(unittest.TestCase):
    def test_production_build_includes_bootstrap_and_existing_resources(self):
        resources = yaml.safe_load((CLUSTER / "kustomization.yaml").read_text())["resources"]
        self.assertEqual(resources.count("flux-system"), 1)
        self.assertTrue(set((
            "../../infrastructure", "kyverno-install.yaml",
            "kyverno-policies.yaml", "apps.yaml",
        )).issubset(resources))
        child = yaml.safe_load((BOOTSTRAP / "kustomization.yaml").read_text())
        self.assertEqual(child["resources"], ["gotk-components.yaml", "gotk-sync.yaml"])

    def test_bootstrap_resources_are_protected_from_pruning(self):
        child = yaml.safe_load((BOOTSTRAP / "kustomization.yaml").read_text())
        self.assertEqual(child["commonAnnotations"].get(
            "kustomize.toolkit.fluxcd.io/prune"), "disabled")
        sync = next(d for d in SYNC if d["kind"] == "Kustomization")
        self.assertIs(sync["spec"]["prune"], True)

    def test_all_four_controllers_match_the_stable_bundle(self):
        version = re.search(r"^# Flux Version: (v\S+)$", COMPONENTS_TEXT, re.MULTILINE)
        self.assertIsNotNone(version)
        self.assertEqual(version.group(1), "v2.9.6")
        expected = {
            "source-controller": "v1.9.6",
            "kustomize-controller": "v1.9.6",
            "helm-controller": "v1.6.5",
            "notification-controller": "v1.9.4",
        }
        deployments = [d for d in COMPONENTS if d["kind"] == "Deployment"]
        self.assertEqual({d["metadata"]["name"] for d in deployments}, set(expected))
        for deployment in deployments:
            name = deployment["metadata"]["name"]
            self.assertEqual(deployment["spec"]["template"]["spec"]["containers"][0]["image"],
                             f"ghcr.io/fluxcd/{name}:{expected[name]}")
            self.assertEqual(deployment["metadata"]["labels"]["app.kubernetes.io/version"],
                             version.group(1))

    def test_bundle_includes_controller_crds_and_rbac(self):
        crds = {d["metadata"]["name"] for d in COMPONENTS
                if d["kind"] == "CustomResourceDefinition"}
        self.assertEqual(crds, {
            "buckets.source.toolkit.fluxcd.io",
            "externalartifacts.source.toolkit.fluxcd.io",
            "gitrepositories.source.toolkit.fluxcd.io",
            "helmcharts.source.toolkit.fluxcd.io",
            "helmrepositories.source.toolkit.fluxcd.io",
            "ocirepositories.source.toolkit.fluxcd.io",
            "kustomizations.kustomize.toolkit.fluxcd.io",
            "helmreleases.helm.toolkit.fluxcd.io",
            "alerts.notification.toolkit.fluxcd.io",
            "providers.notification.toolkit.fluxcd.io",
            "receivers.notification.toolkit.fluxcd.io",
        })
        accounts = {d["metadata"]["name"] for d in COMPONENTS
                    if d["kind"] == "ServiceAccount"}
        bound_accounts = {
            subject["name"] for d in COMPONENTS if d["kind"] == "ClusterRoleBinding"
            for subject in d.get("subjects", []) if subject["kind"] == "ServiceAccount"
        }
        self.assertTrue(accounts.issubset(bound_accounts))
        self.assertTrue(any(d["kind"] == "Namespace" and
                            d["metadata"]["name"] == "flux-system" for d in COMPONENTS))

    def test_self_sync_preserves_source_credentials_branch_and_path(self):
        source = next(d for d in SYNC if d["kind"] == "GitRepository")
        sync = next(d for d in SYNC if d["kind"] == "Kustomization")
        self.assertEqual(source["metadata"]["name"], "flux-system")
        self.assertEqual(source["spec"]["secretRef"], {"name": "flux-system"})
        self.assertEqual(source["spec"]["ref"], {"branch": "master"})
        self.assertEqual(sync["spec"]["path"], "./clusters/production")
        self.assertEqual(sync["spec"]["sourceRef"], {
            "kind": "GitRepository", "name": "flux-system",
        })
        self.assertFalse(any(d["kind"] in ("Secret", "SealedSecret")
                             for d in COMPONENTS + SYNC))

    def test_ci_runs_bootstrap_regression_tests(self):
        workflow = yaml.safe_load((ROOT / ".github" / "workflows" / "ci.yaml").read_text())
        commands = [s.get("run", "") for s in workflow["jobs"]["yaml-lint"]["steps"]]
        self.assertIn("python3 .github/scripts/test-flux-bootstrap.py", commands)


if __name__ == "__main__":
    unittest.main()
