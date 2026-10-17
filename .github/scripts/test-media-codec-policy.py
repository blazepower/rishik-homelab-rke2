"""Regression tests for the GitOps-managed media codec policy."""

import copy
import contextlib
import io
from pathlib import Path
import re
import types
import unittest
from unittest import mock
import urllib.parse

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
        self.series = [
            {"id": 15, "tvdbId": 371310, "tags": [1, 7], "seriesType": "anime", "useSceneNumbering": True},
            {"id": 61, "tvdbId": 377543, "tags": [1], "seriesType": "anime", "useSceneNumbering": True},
            {"id": 80, "tvdbId": 123, "tags": [7], "seriesType": "standard"},
        ]
        self.tags = [{"id": 1, "label": "existing"}, {"id": 7, "label": "another-existing"}]
        self.release_profiles = [
            {"id": 4, "name": "User preference", "enabled": True, "required": ["1080p"],
             "ignored": ["bad"], "indexerId": 2, "tags": [7]}
        ]
        self.naming = {
            "id": 1, "renameEpisodes": False, "replaceIllegalCharacters": True,
            "colonReplacementFormat": 4, "customColonReplacementFormat": "",
            "multiEpisodeStyle": 5, "seriesFolderFormat": "{Series Title}",
            "seasonFolderFormat": "Season {season}", "specialsFolderFormat": "Specials",
            "standardEpisodeFormat": "{Series Title} - S{season:00}E{episode:00} - {Episode Title}",
            "animeEpisodeFormat": "{Series Title} - S{season:00}E{episode:00} - {Episode Title}",
            "dailyEpisodeFormat": "{Series Title} - {Air-Date} - {Episode Title}",
        }
        self.examples = {
            key: "Series - S01E01 - Title [WEBDL-1080p] [AVC] [10bit] [DTS] [JA] -Group"
            for key in ("singleEpisodeExample", "multiEpisodeExample", "dailyEpisodeExample",
                        "animeEpisodeExample", "animeMultiEpisodeExample")
        }
        self.sample_queries = []
        self.clients = [
            {
                "id": 3, "name": "Renamed Usenet client", "implementation": "Sabnzbd",
                "configContract": "SabnzbdSettings", "enable": True,
                "removeFailedDownloads": True, "removeCompletedDownloads": True,
                "priority": 2, "tags": [7],
                "fields": [
                    {"name": "host", "value": "sabnzbd"},
                    {"name": "apiKey", "value": "fixture-only-not-a-secret"},
                    {"name": "tvCategory", "value": "tv"},
                ],
            },
            {
                "id": 4, "name": "SABnzbd", "implementation": "QBittorrent",
                "configContract": "QBittorrentSettings", "enable": True,
                "removeFailedDownloads": True, "removeCompletedDownloads": False,
                "fields": [{"name": "host", "value": "qbittorrent"}],
            },
        ]
        self.download_settings = {"id": 1, "autoRedownloadFailed": True, "enableCompletedDownloadHandling": True}

    def request(self, path, method="GET", body=None):
        if method == "GET":
            if path.startswith("config/naming/examples?"):
                self.sample_queries.append(urllib.parse.parse_qs(path.split("?", 1)[1], keep_blank_values=True))
                return copy.deepcopy(self.examples)
            resources = {
                "customformat": self.formats, "qualityprofile": self.profiles,
                "series": self.series, "tag": self.tags, "releaseprofile": self.release_profiles,
                "config/naming": self.naming,
                "downloadclient": self.clients, "config/downloadclient": self.download_settings,
            }
            if path not in resources:
                raise AssertionError(f"Unexpected GET: {path}")
            return copy.deepcopy(resources[path])
        self.writes.append((path, method, copy.deepcopy(body)))
        if path in ("tag", "releaseprofile"):
            collection = self.tags if path == "tag" else self.release_profiles
            created = {**copy.deepcopy(body), "id": max((item["id"] for item in collection), default=0) + 1}
            collection.append(created)
            return copy.deepcopy(created)
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
        elif path.startswith("series/"):
            self.series = [copy.deepcopy(body) if item["id"] == body["id"] else item for item in self.series]
        elif path.startswith("releaseprofile/"):
            self.release_profiles = [
                copy.deepcopy(body) if item["id"] == body["id"] else item for item in self.release_profiles
            ]
        elif path.startswith("config/naming/"):
            self.naming = copy.deepcopy(body)
        elif path.startswith("downloadclient/"):
            self.clients = [copy.deepcopy(body) if item["id"] == body["id"] else item for item in self.clients]
        else:
            raise AssertionError(f"Unexpected API path: {path}")


class CodecPolicyTests(unittest.TestCase):
    def test_sab_retention_preserves_all_other_client_settings_and_repairs_drift(self):
        for completed_removal in (True, False):
            for auto_redownload in (True, False):
                with self.subTest(completed=completed_removal, redownload=auto_redownload):
                    api = FakeApi()
                    api.clients[0]["removeCompletedDownloads"] = completed_removal
                    api.download_settings["autoRedownloadFailed"] = auto_redownload
                    before = copy.deepcopy(api.clients)
                    settings = copy.deepcopy(api.download_settings)
                    output = io.StringIO()
                    with contextlib.redirect_stdout(output):
                        POLICY.reconcile_sonarr_retention(api)
                    expected = copy.deepcopy(before)
                    expected[0]["removeFailedDownloads"] = False
                    self.assertEqual(api.clients, expected)
                    self.assertEqual(api.download_settings, settings)
                    self.assertEqual(api.writes, [("downloadclient/3", "PUT", expected[0])])
                    self.assertNotIn("fixture-only-not-a-secret", output.getvalue())
                    self.assertNotIn("Renamed Usenet client", output.getvalue())
                    api.writes.clear()
                    POLICY.reconcile_sonarr_retention(api)
                    self.assertEqual(api.writes, [])
                    api.clients[0]["removeFailedDownloads"] = True
                    POLICY.reconcile_sonarr_retention(api)
                    self.assertEqual(api.clients, expected)
                    self.assertEqual(len(api.writes), 1)

    def test_sab_retention_rejects_malformed_or_ambiguous_api_without_writes(self):
        for broken in (
            "not-list", "missing", "multiple", "duplicate-id", "identity", "contract",
            "failed-flag", "completed-flag", "enable", "fields", "field-entry", "duplicate-field",
            "global-settings",
        ):
            with self.subTest(broken=broken):
                api = FakeApi()
                if broken == "not-list":
                    api.clients = {"unexpected": "shape"}
                elif broken == "missing":
                    api.clients = api.clients[1:]
                elif broken == "multiple":
                    api.clients.append({**copy.deepcopy(api.clients[0]), "id": 5})
                elif broken == "duplicate-id":
                    api.clients[1]["id"] = 3
                elif broken == "identity":
                    del api.clients[1]["implementation"]
                elif broken == "contract":
                    api.clients[0]["configContract"] = "OtherSettings"
                elif broken == "failed-flag":
                    api.clients[0]["removeFailedDownloads"] = "true"
                elif broken == "completed-flag":
                    del api.clients[0]["removeCompletedDownloads"]
                elif broken == "enable":
                    api.clients[0]["enable"] = 1
                elif broken == "fields":
                    api.clients[0]["fields"] = None
                elif broken == "field-entry":
                    api.clients[0]["fields"] = [None]
                elif broken == "duplicate-field":
                    api.clients[0]["fields"].append(copy.deepcopy(api.clients[0]["fields"][0]))
                else:
                    api.download_settings["autoRedownloadFailed"] = None
                with self.assertRaisesRegex(RuntimeError, "Sonarr:"):
                    POLICY.reconcile_sonarr_retention(api)
                self.assertEqual(api.writes, [])

    def test_sab_retention_readback_detects_failed_write_or_unrelated_changes(self):
        for broken in ("write-lost", "other-client", "global-settings", "client-settings"):
            with self.subTest(broken=broken):
                api = FakeApi()
                original_request = api.request

                def request(path, method="GET", body=None):
                    if method == "PUT" and path == "downloadclient/3":
                        if broken == "write-lost":
                            return None
                        result = original_request(path, method, body)
                        if broken == "other-client":
                            api.clients[1]["removeCompletedDownloads"] = True
                        elif broken == "global-settings":
                            api.download_settings["autoRedownloadFailed"] = False
                        else:
                            api.clients[0]["fields"][0]["value"] = "changed"
                        return result
                    return original_request(path, method, body)

                api.request = request
                with self.assertRaisesRegex(RuntimeError, "preservation verification failed"):
                    POLICY.reconcile_sonarr_retention(api)

    def test_sonarr_ambiguous_titles_are_scoped_to_demonstrated_styles(self):
        sam = "[sam]_Mushoku_Tensei_Jobless_Reincarnation_S2_-_04_[WEB_1080p_EAC-3]_[9C6ECB8E]"
        almighty = "[Almighty] Jujutsu Kaisen S3 Vol.3 [BD 1920x1080 x264 FLAC][Multiple subs]"
        ember = (
            '[EMBER] Mushoku Tensei: Jobless Reincarnation (2021-2022) (Season 1 Part 02 + Special) '
            '[BDRip [1080p Dual Audio HEVC 10 bits DDP] (Mushoku Tensei: Isekai Ittara Honki Dasu Part 2) '
            '(Batch) [1/9] - "S01E15-Slow Life in the Doldia Village [AEDF24F9].mkv" yEnc'
        )
        cases = [
            (371310, sam, True), (371310, sam.replace("_04_", "_05_"), True),
            (371310, sam.lower().replace("_", "."), True),
            (371310, sam.replace("_", " "), True),
            (377543, almighty, True), (377543, almighty.upper().replace(" ", "_"), True),
            (377543, almighty.replace(" ", "."), True),
            (371310, ember, True), (371310, ember.lower().replace(" ", "_"), True),
            (371310, ember.replace(" ", "."), True),
            (371310, "[sam] Mushoku Tensei Jobless Reincarnation S02E04 [WEB 1080p]", False),
            (377543, "[Almighty] Jujutsu Kaisen S03E07 [BD 1920x1080 x264 FLAC]", False),
            (371310, "[EMBER] Mushoku Tensei S01E15 [1080p HEVC 10bit]", False),
            (371310, sam.replace("[sam]", "[Other]"), False),
            (377543, almighty.replace("[Almighty]", "[Other]"), False),
            (371310, ember.replace("[EMBER]", "[Other]"), False),
            (371310, sam.replace("Mushoku_Tensei", "Other_Series"), False),
            (377543, almighty.replace("Jujutsu Kaisen", "Other Series"), False),
            (371310, sam.replace("_S2_", "_S1_"), False),
            (377543, almighty.replace("S3 Vol.3", "S2 Vol.3"), False),
            (371310, ember.replace("Part 02", "Part 03"), False),
            (371310, ember.replace("(Batch)", "(Single)"), False),
            (371310, "[EMBER] Other Series (Season 1 Part 02) (Batch) HEVC", False),
            (371310, "[Other] Some.Series.S02E04.1080p.HEVC.10bit", False),
            (377543, sam, False), (371310, almighty, False),
        ]
        for tvdb_id, title, expected in cases:
            with self.subTest(title=title, tvdb_id=tvdb_id):
                patterns = next(item[2] for item in POLICY.SONARR_RELEASE_POLICIES if item[0] == tvdb_id)
                # Sonarr's PerlRegexFactory interprets /pattern/i using .NET regex.
                rejected = any(re.search(pattern[1:-2], title, re.I) for pattern in patterns)
                self.assertEqual(rejected, expected)

    def test_sonarr_preserves_preferences_tag_union_and_idempotence(self):
        api = FakeApi()
        original = copy.deepcopy(api)
        POLICY.reconcile_sonarr(api)
        for before, after in zip(original.series, api.series):
            self.assertTrue(set(before["tags"]) <= set(after["tags"]))
            self.assertEqual({k: v for k, v in before.items() if k != "tags"},
                             {k: v for k, v in after.items() if k != "tags"})
        self.assertEqual(api.series[2], original.series[2])
        self.assertEqual(api.release_profiles[0], original.release_profiles[0])
        self.assertEqual(api.profiles, original.profiles)
        for key, value in original.naming.items():
            if key in POLICY.EPISODE_FORMATS:
                self.assertTrue(api.naming[key].startswith(value + " "))
                for token, _ in POLICY.NAMING_METADATA:
                    self.assertEqual(api.naming[key].count("{" + token + "}"), 1)
            else:
                self.assertEqual(api.naming[key], value)
        for profile in api.release_profiles[1:]:
            self.assertEqual(profile["required"], [])
            self.assertTrue(profile["enabled"])
            self.assertEqual(profile["indexerId"], 0)
            self.assertEqual(len(profile["tags"]), 1)
        for field in POLICY.EPISODE_FORMATS:
            self.assertEqual(api.sample_queries[0][field], [api.naming[field]])
        api.writes.clear()
        POLICY.reconcile_sonarr(api)
        self.assertEqual(api.writes, [])

    def test_sonarr_repairs_managed_drift(self):
        api = FakeApi()
        POLICY.reconcile_sonarr(api)
        api.release_profiles[1].update(enabled=False, ignored=[], tags=[1], required=["unwanted"], indexerId=3)
        api.series[0]["tags"] = [1, 7]
        api.naming["animeEpisodeFormat"] = FakeApi().naming["animeEpisodeFormat"]
        POLICY.reconcile_sonarr(api)
        self.assertTrue(api.release_profiles[1]["enabled"])
        self.assertEqual(api.release_profiles[1]["required"], [])
        self.assertEqual(api.release_profiles[1]["indexerId"], 0)
        self.assertEqual(api.release_profiles[1]["ignored"], list(POLICY.SONARR_RELEASE_POLICIES[0][2]))
        self.assertTrue(set([1, 7]) < set(api.series[0]["tags"]))
        api.writes.clear()
        POLICY.reconcile_sonarr(api)
        self.assertEqual(api.writes, [])

    def test_sonarr_appends_only_missing_tokens(self):
        api = FakeApi()
        for field in POLICY.EPISODE_FORMATS:
            api.naming[field] += " [{quality.full}] -{Release-Group} [{MediaInfo AudioLanguages:EN+JA}]"
        before = copy.deepcopy(api.naming)
        POLICY.reconcile_sonarr(api)
        for field in POLICY.EPISODE_FORMATS:
            self.assertTrue(api.naming[field].startswith(before[field]))
            self.assertNotIn("{Quality Full}", api.naming[field])
            self.assertNotIn("{Release Group}", api.naming[field])
            self.assertNotIn("{MediaInfo AudioLanguages}", api.naming[field])

    def test_missing_series_is_logged_without_global_policy(self):
        api = FakeApi()
        api.series = [api.series[2]]
        output = io.StringIO()
        with contextlib.redirect_stdout(output):
            POLICY.reconcile_sonarr(api)
        self.assertIn("TVDB 371310) not present", output.getvalue())
        self.assertIn("TVDB 377543) not present", output.getvalue())
        self.assertEqual(len(api.release_profiles), 1)
        self.assertFalse(any(path in ("tag", "releaseprofile") for path, _, _ in api.writes))

    def test_invalid_preflight_fails_without_prevention_writes(self):
        for broken in (
            "series", "identity", "tags", "profiles", "naming", "naming-id",
            "samples", "sample-tokens", "duplicate", "shared-tag",
        ):
            with self.subTest(broken=broken):
                api = FakeApi()
                if broken == "series":
                    del api.series[0]["tvdbId"]
                elif broken == "identity":
                    api.series[0]["tvdbId"] = "371310"
                elif broken == "tags":
                    api.series[0]["tags"] = None
                elif broken == "profiles":
                    del api.release_profiles[0]["ignored"]
                elif broken == "naming":
                    api.naming["animeEpisodeFormat"] = ""
                elif broken == "naming-id":
                    api.naming["id"] = 0
                elif broken == "samples":
                    api.examples["animeMultiEpisodeExample"] = None
                elif broken == "sample-tokens":
                    api.examples["singleEpisodeExample"] = "Series {Unsupported Token}"
                elif broken == "duplicate":
                    api.series.append(copy.deepcopy(api.series[0]))
                else:
                    api.tags.append({"id": 8, "label": "gitops-import-371310"})
                    api.series[2]["tags"].append(8)
                with self.assertRaises(RuntimeError):
                    POLICY.reconcile_sonarr(api)
                self.assertEqual(api.writes, [])

    def test_api_lookup_errors_are_not_treated_as_absent_series(self):
        api = FakeApi()
        original_request = api.request

        def request(path, method="GET", body=None):
            if path == "series":
                raise OSError("lookup unavailable")
            return original_request(path, method, body)

        api.request = request
        with self.assertRaisesRegex(OSError, "lookup unavailable"):
            POLICY.reconcile_sonarr(api)
        self.assertEqual(api.writes, [])

    def test_failed_write_readback_is_not_reported_as_success(self):
        api = FakeApi()
        original_request = api.request

        def request(path, method="GET", body=None):
            if path.startswith("config/naming/") and method == "PUT":
                return None
            return original_request(path, method, body)

        api.request = request
        with self.assertRaisesRegex(RuntimeError, "naming verification failed"):
            POLICY.reconcile_sonarr(api)

    def test_only_sonarr_main_runs_import_prevention(self):
        for application in ("sonarr", "radarr"):
            with self.subTest(application=application):
                with mock.patch.object(POLICY.sys, "argv", ["reconcile.py", application]), \
                        mock.patch.object(POLICY, "Api", return_value=FakeApi()) as constructor, \
                        mock.patch.object(POLICY, "reconcile") as codec, \
                        mock.patch.object(POLICY, "reconcile_sonarr_retention") as retention, \
                        mock.patch.object(POLICY, "reconcile_sonarr") as prevention:
                    POLICY.main()
                codec.assert_called_once_with(constructor.return_value, application.capitalize())
                if application == "sonarr":
                    retention.assert_called_once_with(constructor.return_value)
                    prevention.assert_called_once_with(constructor.return_value)
                else:
                    retention.assert_not_called()
                    prevention.assert_not_called()

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
