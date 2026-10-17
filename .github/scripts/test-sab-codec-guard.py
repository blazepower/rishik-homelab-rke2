#!/usr/bin/env python3
"""Synthetic, offline tests: no real downloads, Kubernetes writes or SAB requests."""
import contextlib
import copy
import io
import json
from pathlib import Path
import shutil
import subprocess
import types
import unittest
from unittest.mock import patch
import uuid

import yaml


ROOT = Path(__file__).resolve().parents[2]
MANIFEST = yaml.safe_load((ROOT / "apps" / "sabnzbd" / "codec-guard.yaml").read_text())


def module(key):
    result = types.ModuleType(key.removesuffix(".py"))
    exec(compile(MANIFEST["data"][key], key, "exec"), result.__dict__)
    return result


guard = module("codec-guard.py")
config = module("reconcile.py")


def metadata(codec="h264", pixel="yuv420p", profile="High", bits="8"):
    return {"streams": [{"codec_name": codec, "pix_fmt": pixel,
                         "profile": profile, "bits_per_raw_sample": bits}]}


class Fixture(unittest.TestCase):
    def setUp(self):
        # Deliberately keep fixtures in the project, never an OS temporary directory.
        self.work = ROOT / (".test-sab-codec-guard-" + uuid.uuid4().hex)
        self.work.mkdir()
        self.addCleanup(shutil.rmtree, self.work)
        self.output = io.StringIO()
        self.capture = contextlib.redirect_stdout(self.output)
        self.capture.__enter__()
        self.addCleanup(self.capture.__exit__, None, None, None)

    def video(self, name):
        path = self.work / name
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_bytes(b"synthetic fixture: not media")
        return path


class CodecTests(Fixture):
    def test_normal_h264_and_hevc_10_are_allowed(self):
        for value in [metadata(), metadata("hevc", "yuv420p10le", "Main 10", "10")]:
            self.assertFalse(guard.incompatible(value))

    def test_each_high10_signal_independently_rejects(self):
        for value in [
            metadata(profile="High 10", bits="0"),
            metadata(profile="High 10 Intra", bits="0"),
            metadata(pixel="yuv420p10le", bits="0"),
            metadata(pixel="yuv422p10be", bits="0"),
            metadata(pixel="p010le", bits="0"),
            metadata(pixel="gray10le", bits="0"),
            metadata(bits="10"),
        ]:
            with self.subTest(value=value):
                self.assertTrue(guard.incompatible(value))

    def test_any_video_stream_rejects_but_attached_cover_is_ignored(self):
        mixed = metadata()
        mixed["streams"] += metadata(profile="High 10")["streams"]
        self.assertTrue(guard.incompatible(mixed))
        mixed["streams"][1]["disposition"] = {"attached_pic": 1}
        self.assertFalse(guard.incompatible(mixed))

    def test_unknown_and_malformed_metadata_fail_closed(self):
        for value in [
            None, [], {}, {"streams": None}, {"streams": []}, {"streams": ["bad"]},
            {"streams": [{}]}, metadata(codec="unknown"), metadata(pixel="unknown"),
            metadata(bits="not-a-number"), metadata(profile=None),
            {"streams": [{"disposition": "bad"}]},
        ]:
            with self.subTest(value=value), self.assertRaises(guard.GuardError):
                guard.incompatible(value)

    def test_multifile_rejects_one_incompatible_and_preserves_bytes(self):
        good, bad = self.video("episode01.mkv"), self.video("episode02.mkv")
        before = {p: p.read_bytes() for p in (good, bad)}
        def probe(path, timeout):
            self.assertGreater(timeout, 0)
            self.assertLessEqual(timeout, 30)
            return metadata(profile="High 10" if path == bad else "High")
        with self.assertRaises(guard.GuardError):
            guard.validate(self.work, probe_fn=probe)
        self.assertEqual(before, {p: p.read_bytes() for p in before})
        self.assertIn('REJECT "episode02.mkv"', self.output.getvalue())

    def test_explicit_samples_skipped_not_arbitrary_extras(self):
        main = self.video("episode.mkv")
        self.video("sample.mkv")
        self.video("Samples/preview.mkv")
        extra = self.video("Extras/interview.mkv")
        calls = []
        def probe(path, timeout):
            calls.append(path)
            return metadata()
        guard.validate(self.work, probe_fn=probe)
        self.assertEqual(set(calls), {main, extra})
        self.assertIn("2 sample(s) skipped", self.output.getvalue())

    def test_no_media_and_samples_only_fail(self):
        for create in (False, True):
            if create:
                self.video("sample.mkv")
            with self.assertRaisesRegex(guard.GuardError, "no main video"):
                guard.validate(self.work)

    def test_probe_errors_fail_without_printing_raw_errors(self):
        path = self.video("episode.mkv")
        cases = [
            FileNotFoundError("dummy-private-path"),
            subprocess.TimeoutExpired("ffprobe", 30, stderr=b"dummy-private-value"),
            subprocess.CompletedProcess([], 1, b"", b"dummy-private-value"),
            subprocess.CompletedProcess([], 0, b"not-json", b""),
            subprocess.CompletedProcess([], 0, b"x" * 524289, b""),
        ]
        for result in cases:
            kwargs = {"side_effect": result} if isinstance(result, Exception) else {"return_value": result}
            with patch.object(guard.subprocess, "run", **kwargs), self.assertRaises(guard.GuardError):
                guard.probe(path, 1)
        self.assertNotIn("dummy-private", self.output.getvalue())

    def test_probe_command_is_bounded_and_read_only(self):
        path = self.video("episode.mkv")
        response = subprocess.CompletedProcess([], 0, json.dumps(metadata()).encode(), b"")
        with patch.object(guard.subprocess, "run", return_value=response) as run:
            self.assertEqual(guard.probe(path, 12), metadata())
        args, kwargs = run.call_args
        self.assertEqual(args[0][0], "ffprobe")
        self.assertEqual(args[0][-1], str(path))
        self.assertIn("-max_streams", args[0])
        self.assertEqual(args[0][args[0].index("-max_alloc") + 1], "67108864")
        self.assertEqual(args[0][args[0].index("-protocol_whitelist") + 1], "file")
        self.assertEqual(kwargs["timeout"], 12)
        self.assertEqual(kwargs["stderr"], subprocess.DEVNULL)
        self.assertNotIn("shell", kwargs)

    def test_probe_failure_and_deadline_cannot_be_success(self):
        self.video("episode.mkv")
        with self.assertRaisesRegex(guard.GuardError, "unverifiable TV media"):
            guard.validate(self.work, probe_fn=lambda *_: (_ for _ in ()).throw(guard.GuardError("unreadable")))
        with self.assertRaises(guard.GuardError):
            guard.validate(self.work, clock=iter([0, 301]).__next__)

    def test_file_and_directory_limits(self):
        self.video("episode.mkv")
        with patch.object(guard, "MAX_FILES", 0), self.assertRaises(guard.GuardError):
            guard.validate(self.work)
        with patch.object(guard, "MAX_ENTRIES", 0), self.assertRaises(guard.GuardError):
            guard.validate(self.work)

    def test_symlink_is_not_followed(self):
        target = self.video("episode.mkv")
        try:
            (self.work / "link.mkv").symlink_to(target)
        except OSError:
            self.skipTest("symlink creation unavailable to this Windows account")
        with self.assertRaisesRegex(guard.GuardError, "symlink"):
            guard.validate(self.work)

    def test_main_category_and_status_gating(self):
        self.assertEqual(guard.main({"SAB_CAT": "movies", "SAB_PP_STATUS": "0", "SAB_STATUS": "Running"}), 0)
        for status in ("1", "2", "3", "-1", ""):
            self.assertEqual(guard.main({"SAB_CAT": "tv", "SAB_PP_STATUS": status, "SAB_STATUS": "Running"}), 1)
        self.assertEqual(guard.main({"SAB_CAT": "tv", "SAB_PP_STATUS": "0", "SAB_STATUS": "Failed"}), 1)
        self.assertEqual(guard.main({"SAB_CAT": "tv", "SAB_PP_STATUS": "0"}), 1)
        self.assertEqual(guard.main({"SAB_CAT": "tv", "SAB_PP_STATUS": "0", "SAB_STATUS": "Running"}), 1)

    def test_main_uses_only_job_under_tv_root(self):
        job = self.work / "job"
        job.mkdir()
        env = {"SAB_CAT": "tv", "SAB_PP_STATUS": "0", "SAB_STATUS": "Running",
               "SAB_COMPLETE_DIR": str(job)}
        with patch.object(guard, "TV_ROOT", self.work), patch.object(guard, "validate") as validate:
            self.assertEqual(guard.main(env), 0)
            validate.assert_called_once_with(job.resolve())
            env["SAB_COMPLETE_DIR"] = str(self.work)
            self.assertEqual(guard.main(env), 1)
            env["SAB_COMPLETE_DIR"] = str(self.work.parent)
            self.assertEqual(guard.main(env), 1)


INI = """# Dummy secrets only. Never read live configuration in these tests.
[misc]
api_key = dummy-key-KEEP
password = dummy-password-KEEP
servers = unrelated-value
script_dir = ""
script_can_fail = 0
complete_dir = /media/torrents/complete/sabnzbd
cleanup_list = nfo, sfv, txt
history_retention = 1d
history_retention_option = failed
history_retention_number = 1
new_nzb_on_failure = 1
[servers]
[[dummy-provider]]
host = localhost
password = dummy-provider-secret-KEEP
[categories]
[[*]]
script = None
dir = ""
pp = 3
[[tv]]
script = Default
dir = TV Shows
pp = ""
[[movies]]
script = Default
dir = Movies
"""


class ConfigTests(Fixture):
    def setUp(self):
        super().setUp()
        self.ini = self.work / "sabnzbd.ini"
        self.ini.write_text(INI, encoding="ascii")

    def test_reconcile_allowlist_retention_and_credentials(self):
        before = config.load(self.ini).dict()
        config.reconcile(self.ini)
        after = config.load(self.ini).dict()
        expected = copy.deepcopy(before)
        expected["misc"].update(config.MISC)
        expected["categories"]["tv"]["script"] = config.SCRIPT
        self.assertEqual(after, expected)
        self.assertEqual(after["misc"]["script_can_fail"], "1")
        self.assertEqual(after["misc"]["history_retention"], "")
        self.assertEqual(after["misc"]["history_retention_option"], "all")
        self.assertEqual(after["misc"]["new_nzb_on_failure"], "0")
        config.reconcile(self.ini, check=True)
        current = self.ini.read_bytes()
        config.reconcile(self.ini)
        self.assertEqual(self.ini.read_bytes(), current)
        self.assertNotIn("dummy-key", self.output.getvalue())
        self.assertNotIn("dummy-password", self.output.getvalue())
        self.assertNotIn("dummy-provider-secret", self.output.getvalue())

    def test_check_is_read_only_and_detects_drift(self):
        before = self.ini.read_bytes()
        with self.assertRaisesRegex(config.ConfigError, "drift"):
            config.reconcile(self.ini, check=True)
        self.assertEqual(before, self.ini.read_bytes())

    def test_existing_scripts_or_changed_paths_block_without_writes(self):
        for old, new in [
            ('script_dir = ""', "script_dir = /existing-scripts"),
            ("script = Default", "script = existing.py"),
            ("script = None", "script = default-existing.py"),
            ("dir = TV Shows", "dir = Changed"),
            ("/media/torrents/complete/sabnzbd", "/another-root"),
            ("cleanup_list = nfo, sfv, txt", "cleanup_list = mkv"),
        ]:
            content = INI.replace(old, new)
            self.ini.write_text(content, encoding="ascii")
            with self.subTest(new=new), self.assertRaises(config.ConfigError):
                config.reconcile(self.ini)
            self.assertEqual(self.ini.read_text(), content)

    def test_malformed_configuration_is_not_rewritten(self):
        self.ini.write_text("[broken\n", encoding="ascii")
        before = self.ini.read_bytes()
        with self.assertRaises(config.ConfigError):
            config.reconcile(self.ini)
        self.assertEqual(self.ini.read_bytes(), before)

    def test_atomic_replace_failure_retains_original(self):
        before = self.ini.read_bytes()
        with patch.object(config.os, "replace", side_effect=OSError("synthetic failure")), self.assertRaises(OSError):
            config.reconcile(self.ini)
        self.assertEqual(self.ini.read_bytes(), before)
        self.assertFalse(self.ini.with_name("sabnzbd.ini.codec-guard-next").exists())

    def test_existing_staging_file_is_not_deleted(self):
        staging = self.ini.with_name("sabnzbd.ini.codec-guard-next")
        staging.write_bytes(b"another owner's staged file")
        with self.assertRaises(FileExistsError):
            config.reconcile(self.ini)
        self.assertEqual(staging.read_bytes(), b"another owner's staged file")

    def test_manifest_wiring(self):
        app = yaml.safe_load((ROOT / "apps" / "sabnzbd" / "helmrelease.yaml").read_text())["spec"]["values"]
        controller = app["controllers"]["sabnzbd"]
        self.assertEqual(controller["strategy"], "Recreate")
        self.assertEqual(controller["initContainers"]["codec-config"]["command"],
                         ["python3", "/codec-guard/reconcile.py"])
        env = {entry["name"]: entry for entry in controller["containers"]["app"]["env"]}
        self.assertEqual(env["INSTALL_PACKAGES"]["value"], "ffmpeg")
        self.assertIn("@sha256:", env["DOCKER_MODS"]["value"])
        probes = controller["containers"]["app"]["probes"]
        startup = probes["startup"]
        self.assertTrue(startup["enabled"])
        self.assertTrue(startup["custom"])
        self.assertEqual(startup["spec"]["httpGet"],
                         {"path": "/sabnzbd/api?mode=version", "port": 8080})
        self.assertEqual(startup["spec"]["periodSeconds"], 10)
        self.assertEqual(startup["spec"]["failureThreshold"], 60)
        self.assertEqual(startup["spec"]["timeoutSeconds"], 5)
        self.assertEqual(startup["spec"]["periodSeconds"] * startup["spec"]["failureThreshold"], 600)
        for name, delay, period, timeout in [
            ("liveness", 30, 30, 10), ("readiness", 10, 10, 5),
        ]:
            self.assertEqual(probes[name]["spec"], {
                "httpGet": startup["spec"]["httpGet"],
                "initialDelaySeconds": delay, "periodSeconds": period,
                "timeoutSeconds": timeout, "failureThreshold": 3,
            })
        mount = app["persistence"]["codec-guard"]
        self.assertEqual(mount["name"], MANIFEST["metadata"]["name"])
        self.assertEqual(mount["defaultMode"], 0o555)
        self.assertIn({"key": "codec-guard.py", "path": "scripts/codec-guard.py"}, mount["items"])
        self.assertEqual(config.SCRIPT_DIR, "/codec-guard/scripts")
        self.assertTrue(mount["globalMounts"][0]["readOnly"])
        resources = yaml.safe_load((ROOT / "apps" / "sabnzbd" / "kustomization.yaml").read_text())["resources"]
        self.assertIn("codec-guard.yaml", resources)

    def test_cleanup_extension_policy_matches_probed_extensions(self):
        value = config.load(self.ini)
        for extension in guard.VIDEO:
            value["misc"]["cleanup_list"] = [extension.lstrip(".")]
            with self.subTest(extension=extension), self.assertRaises(config.ConfigError):
                config.desired(value)


if __name__ == "__main__":
    unittest.main(verbosity=2)
