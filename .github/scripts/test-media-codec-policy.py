"""Regression tests for the GitOps-managed media codec policy."""

import copy
from pathlib import Path
import re
import types
import unittest

import yaml


ROOT = Path(__file__).resolve().parents[2]
DOCUMENT = yaml.safe_load((ROOT / "apps/media-codec-policy/script.yaml").read_text())
POLICY = types.ModuleType("codec_policy")
exec(compile(DOCUMENT["data"]["reconcile.py"], "reconcile.py", "exec"), POLICY.__dict__)


class FakeApi:
    def __init__(self):
        self.formats = []
        self.profiles = [
            {
                "id": 1,
                "name": "Existing profile",
                "minFormatScore": 0,
                "upgradeAllowed": True,
                "formatItems": [{"format": 2, "score": 40000}, {"format": 3, "score": 20000}],
            }
        ]
        self.writes = []

    def request(self, path, method="GET", body=None):
        if method == "GET":
            return copy.deepcopy(self.formats if path == "customformat" else self.profiles)
        self.writes.append((path, method, copy.deepcopy(body)))
        if path == "customformat":
            created = {**copy.deepcopy(body), "id": 99}
            self.formats.append(created)
            for profile in self.profiles:
                profile["formatItems"].append({"format": 99, "score": 0})
            return copy.deepcopy(created)
        if path.startswith("customformat/"):
            self.formats = [copy.deepcopy(body)]
        elif path.startswith("qualityprofile/"):
            self.profiles = [
                copy.deepcopy(body) if item["id"] == body["id"] else item
                for item in self.profiles
            ]
        else:
            raise AssertionError(f"Unexpected API path: {path}")


class CodecPolicyTests(unittest.TestCase):
    def test_title_detection(self):
        cases = {
            "Release.1080p.Hi10P.AAC": True,
            "Release.1080p.Hi10.AAC": True,
            "Release.High-10.H264": True,
            "Release.x264.10bit": True,
            "Release.H.264.10-bit": True,
            "Release.10.bits.AVC": True,
            "Release.x264.8bit": False,
            "Release.x264": False,
            "Release.x265.10bit": False,
            "Release.HEVC.Main10": False,
            "Release.AV1.10bit": False,
            "Release.x265.Hi10P": False,
        }
        for title, expected in cases.items():
            with self.subTest(title=title):
                rejected = bool(re.search(POLICY.HIGH_10, title)) and not re.search(
                    POLICY.OTHER_CODECS, title
                )
                self.assertEqual(bool(rejected), expected)

    def test_rejection_cannot_be_outscored(self):
        for minimum in (0, 1, -20000):
            with self.subTest(minimum=minimum):
                profile = FakeApi().profiles[0]
                profile["minFormatScore"] = minimum
                score = POLICY.rejection_score(profile, 99)
                self.assertLess(60000 + score, minimum)

    def test_reconcile_preserves_other_preferences_and_is_idempotent(self):
        api = FakeApi()
        original = copy.deepcopy(api.profiles[0])
        POLICY.reconcile(api, "Test")
        result = api.profiles[0]
        self.assertEqual(result["formatItems"][:2], original["formatItems"])
        self.assertEqual(result["upgradeAllowed"], original["upgradeAllowed"])
        self.assertEqual(result["minFormatScore"], original["minFormatScore"])
        self.assertEqual(result["formatItems"][-1]["score"], -60001)
        api.writes.clear()
        POLICY.reconcile(api, "Test")
        self.assertEqual(api.writes, [])

    def test_reconcile_repairs_drift_and_new_profiles(self):
        api = FakeApi()
        POLICY.reconcile(api, "Test")
        api.formats[0]["specifications"][0]["fields"][0]["value"] = "incorrect"
        api.profiles[0]["formatItems"][-1]["score"] = 0
        api.profiles.append({**copy.deepcopy(api.profiles[0]), "id": 2})
        POLICY.reconcile(api, "Test")
        self.assertTrue(POLICY.same_format(api.formats[0], POLICY.custom_format()))
        for profile in api.profiles:
            self.assertEqual(profile["formatItems"][-1]["score"], -60001)

    def test_missing_format_in_profile_fails_explicitly(self):
        api = FakeApi()
        api.formats = [{**POLICY.custom_format(), "id": 99}]
        with self.assertRaisesRegex(RuntimeError, "missing or duplicated"):
            POLICY.reconcile(api, "Test")

    def test_each_job_uses_only_its_own_application_affinity_and_volume(self):
        jobs = list(yaml.safe_load_all((ROOT / "apps/media-codec-policy/cronjob.yaml").read_text()))
        self.assertEqual(len(jobs), 2)
        for job in jobs:
            spec = job["spec"]["jobTemplate"]["spec"]["template"]["spec"]
            application = spec["containers"][0]["command"][-1]
            self.assertIn(application, ("sonarr", "radarr"))
            terms = spec["affinity"]["podAffinity"]["requiredDuringSchedulingIgnoredDuringExecution"]
            self.assertEqual(len(terms), 1)
            self.assertEqual(
                terms[0]["labelSelector"]["matchLabels"],
                {"app.kubernetes.io/name": application},
            )
            claims = [
                volume["persistentVolumeClaim"]
                for volume in spec["volumes"]
                if "persistentVolumeClaim" in volume
            ]
            self.assertEqual(claims, [{"claimName": f"{application}-config", "readOnly": True}])

    def test_monitoring_queries_are_bounded_without_losing_core_alerts(self):
        document = yaml.safe_load(
            (ROOT / "infrastructure/monitoring/helmrelease-kube-prometheus-stack.yaml").read_text()
        )
        values = document["spec"]["values"]
        self.assertEqual(
            values["defaultRules"]["rules"],
            {
                "kubeApiserverAvailability": False,
                "kubeApiserverBurnrate": False,
                "kubeApiserverSlos": False,
            },
        )
        self.assertEqual(
            values["prometheus"]["prometheusSpec"]["query"],
            {"maxConcurrency": 4, "maxSamples": 10000000, "timeout": "30s"},
        )


if __name__ == "__main__":
    unittest.main()
