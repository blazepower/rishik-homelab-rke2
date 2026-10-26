"""Render the local Paperless chart and protect its v3 migration invariants."""

from pathlib import Path
import subprocess
import unittest

import yaml


ROOT = Path(__file__).resolve().parents[2]
CHART = ROOT / "charts/paperless-ngx"
VALUES = yaml.safe_load((CHART / "values.yaml").read_text())
METADATA = yaml.safe_load((CHART / "Chart.yaml").read_text())
RELEASE = yaml.safe_load((ROOT / "apps/paperless-ngx/helmrelease.yaml").read_text())
RENDERED = [
    item for item in yaml.safe_load_all(subprocess.run(
        ["helm", "template", "paperless", str(CHART), "--namespace", "paperless"],
        check=True, capture_output=True, encoding="utf-8",
    ).stdout) if item
]
DEPLOYMENT = next(item for item in RENDERED if item["kind"] == "Deployment")
CONTAINER = DEPLOYMENT["spec"]["template"]["spec"]["containers"][0]
ENV = {item["name"]: item for item in CONTAINER["env"]}


class PaperlessUpgradeTests(unittest.TestCase):
    def test_target_version_matches_chart_metadata_and_immutable_image(self):
        version, digest = VALUES["image"]["tag"].split("@")
        self.assertEqual(METADATA["appVersion"], version)
        self.assertRegex(digest, r"^sha256:[0-9a-f]{64}$")
        self.assertEqual(CONTAINER["image"],
                         VALUES["image"]["repository"] + ":" + VALUES["image"]["tag"])

    def test_original_secret_key_and_database_engine_are_preserved(self):
        self.assertEqual(ENV["PAPERLESS_DBENGINE"]["value"], "postgresql")
        for name, key in (
            ("PAPERLESS_SECRET_KEY", "paperless-secret-key"),
            ("PAPERLESS_DBPASS", "postgresql-password"),
            ("PAPERLESS_ADMIN_PASSWORD", "paperless-admin-password"),
        ):
            self.assertEqual(ENV[name], {
                "name": name,
                "valueFrom": {"secretKeyRef": {
                    "name": "paperless-credentials", "key": key,
                }},
            })
        self.assertFalse(any(item["kind"] in ("Secret", "SealedSecret")
                             for item in RENDERED))
        self.assertEqual(len(ENV), len(CONTAINER["env"]))

    def test_v2_ocr_and_archive_defaults_are_preserved_explicitly(self):
        self.assertEqual(ENV["PAPERLESS_OCR_MODE"]["value"], "auto")
        self.assertEqual(ENV["PAPERLESS_ARCHIVE_FILE_GENERATION"]["value"], "always")
        self.assertEqual(ENV["PAPERLESS_CONSUMER_DELETE_DUPLICATES"]["value"], "true")

    def test_startup_probe_protects_migrations_and_index_rebuild(self):
        probe = CONTAINER["startupProbe"]
        self.assertEqual(probe["httpGet"], {"path": "/api/", "port": 8000})
        self.assertGreaterEqual(probe["periodSeconds"] * probe["failureThreshold"],
                                600)
        self.assertEqual(RELEASE["spec"]["upgrade"]["timeout"], "15m")

    def test_schema_changes_cannot_automatically_roll_back_the_image(self):
        remediation = RELEASE["spec"]["upgrade"]["remediation"]
        self.assertEqual(remediation["retries"], 0)
        self.assertFalse(remediation["remediateLastFailure"])
        self.assertNotIn("suspend", RELEASE["spec"])

    def test_existing_single_writer_and_document_volumes_are_preserved(self):
        self.assertEqual(DEPLOYMENT["spec"]["replicas"], 1)
        self.assertEqual(DEPLOYMENT["spec"]["strategy"]["type"], "Recreate")
        volumes = {
            item["name"]: item["persistentVolumeClaim"]["claimName"]
            for item in DEPLOYMENT["spec"]["template"]["spec"]["volumes"]
            if "persistentVolumeClaim" in item
        }
        self.assertEqual(volumes, {
            name: "paperless-paperless-ngx-" + name
            for name in ("data", "media", "consume", "export")
        })

    def test_postgresql_major_and_scale_to_zero_are_not_changed(self):
        self.assertRegex(VALUES["postgresql"]["image"]["tag"],
                         r"^18-alpine@sha256:[0-9a-f]{64}$")
        scaler = yaml.safe_load(
            (ROOT / "apps/paperless-ngx/scaledobject.yaml").read_text()
        )
        self.assertEqual(scaler["spec"]["minReplicaCount"], 0)
        self.assertEqual(scaler["spec"]["maxReplicaCount"], 1)
        self.assertEqual(scaler["spec"]["scaleTargetRef"]["name"],
                         DEPLOYMENT["metadata"]["name"])


if __name__ == "__main__":
    unittest.main()
