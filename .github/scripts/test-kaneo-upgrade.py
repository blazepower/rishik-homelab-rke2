"""Regression tests for Kaneo's bundled application and database upgrade wiring."""

from pathlib import Path
import unittest

import yaml


ROOT = Path(__file__).resolve().parents[2]
RELEASE = yaml.safe_load((ROOT / "apps/kaneo/helmrelease.yaml").read_text())
SOURCE = yaml.safe_load(
    (ROOT / "infrastructure/crds/kaneo-git-repo.yaml").read_text()
)
VALUES = RELEASE["spec"]["values"]
APP = VALUES["kaneo"]
PATCHES = RELEASE["spec"]["postRenderers"][0]["kustomize"]["patches"]


class KaneoUpgradeTests(unittest.TestCase):
    def test_chart_and_immutable_bundled_image_are_coordinated(self):
        self.assertEqual(APP["image"]["repository"], "ghcr.io/usekaneo/kaneo")
        version, digest = APP["image"]["tag"].split("@")
        self.assertEqual(SOURCE["spec"]["ref"]["tag"], "v" + version)
        self.assertRegex(digest, r"^sha256:[0-9a-f]{64}$")
        self.assertNotIn("api", VALUES)
        self.assertNotIn("web", VALUES)

    def test_existing_authentication_and_database_secrets_are_preserved(self):
        self.assertEqual(APP["env"]["existingSecret"], {
            "enabled": True, "name": "kaneo-credentials", "key": "jwt-secret",
        })
        database = APP["env"]["database"]["external"]
        self.assertTrue(database["enabled"])
        self.assertEqual(database["existingSecret"], {
            "enabled": True,
            "name": "kaneo-credentials",
            "passwordKey": "database-url",
        })
        self.assertNotIn("authSecret", APP["env"])
        self.assertNotIn("password", database)
        self.assertEqual(VALUES["postgresql"]["auth"]["existingSecret"],
                         "kaneo-credentials")
        self.assertEqual(
            VALUES["postgresql"]["auth"]["secretKeys"]["userPasswordKey"],
            "postgres-password",
        )

    def test_postgresql_major_and_persistent_storage_are_preserved(self):
        database = VALUES["postgresql"]
        self.assertTrue(database["enabled"])
        self.assertRegex(database["image"]["tag"],
                         r"^16-alpine@sha256:[0-9a-f]{64}$")
        self.assertEqual(database["auth"]["database"], "kaneo")
        self.assertEqual(database["auth"]["username"], "kaneo_user")
        self.assertEqual(database["persistence"], {
            "enabled": True, "size": "8Gi", "storageClass": "longhorn",
            "accessMode": "ReadWriteOnce",
        })

    def test_both_deployments_are_single_writer_recreate(self):
        self.assertEqual(VALUES["replicaCount"], 1)
        strategies = {}
        for patch in PATCHES:
            for operation in yaml.safe_load(patch["patch"]):
                if operation["path"] == "/spec/strategy":
                    self.assertEqual(patch["target"]["kind"], "Deployment")
                    strategies[patch["target"]["name"]] = operation["value"]
        self.assertEqual(strategies, {
            "kaneo": {"type": "Recreate", "rollingUpdate": None},
            "kaneo-postgresql": {"type": "Recreate", "rollingUpdate": None},
        })

    def test_migrations_do_not_trigger_automatic_application_rollback(self):
        remediation = RELEASE["spec"]["upgrade"]["remediation"]
        self.assertEqual(remediation["retries"], 0)
        self.assertFalse(remediation["remediateLastFailure"])

    def test_runtime_volume_is_writable_by_the_rootless_image(self):
        security = VALUES["podSecurityContext"]
        self.assertTrue(security["runAsNonRoot"])
        self.assertEqual(security["runAsUser"], 1001)
        self.assertEqual(security["fsGroup"], 1001)
        self.assertFalse(APP["securityContext"]["allowPrivilegeEscalation"])
        self.assertEqual(APP["securityContext"]["capabilities"]["drop"], ["ALL"])

    def test_same_origin_service_and_health_routes_are_preserved(self):
        self.assertEqual(APP["service"]["port"], 5173)
        self.assertEqual(APP["service"]["targetPort"], 5173)
        self.assertEqual(APP["env"]["clientUrl"], "https://kaneo.homelab")
        self.assertEqual(APP["env"]["corsOrigins"], "https://kaneo.homelab")
        self.assertFalse(VALUES["ingress"]["enabled"])
        for name in ("readinessProbe", "livenessProbe"):
            self.assertEqual(APP[name]["httpGet"],
                             {"path": "/api/health", "port": "kaneo"})

    def test_startup_probe_allows_database_migrations_to_finish(self):
        probes = [
            operation["value"]
            for patch in PATCHES
            if patch["target"]["name"] == "kaneo"
            for operation in yaml.safe_load(patch["patch"])
            if operation["path"].endswith("/startupProbe")
        ]
        self.assertEqual(len(probes), 1)
        self.assertEqual(probes[0]["httpGet"],
                         {"path": "/api/health", "port": "kaneo"})
        self.assertGreaterEqual(
            probes[0]["periodSeconds"] * probes[0]["failureThreshold"], 300
        )

    def test_resource_settings_apply_to_the_bundled_application(self):
        for component in (APP, VALUES["postgresql"]):
            for budget in ("requests", "limits"):
                self.assertEqual(set(component["resources"][budget]),
                                 {"cpu", "memory"})


if __name__ == "__main__":
    unittest.main()
