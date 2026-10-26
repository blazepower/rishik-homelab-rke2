"""Offline checks for the scripts executed by the Python media containers."""

from contextlib import redirect_stdout
from datetime import datetime, timedelta, timezone
import io
import json
import os
from pathlib import Path
import types
import unittest
from unittest import mock
import urllib.parse
import xml.etree.ElementTree as ET

import yaml


ROOT = Path(__file__).resolve().parents[2]


def load_script(path, key):
    source = yaml.safe_load((ROOT / path).read_text())["data"][key]
    module = types.ModuleType(key.removesuffix(".py"))
    exec(compile(source, key, "exec"), module.__dict__)
    return module


CLEANUP = load_script("apps/sonarr/cleanup-script.yaml", "cleanup.py")
RELEASE = yaml.safe_load((ROOT / "apps/qbittorrent/helmrelease.yaml").read_text())
SEED_SOURCE = RELEASE["spec"]["values"]["controllers"]["qbittorrent"]["containers"][
    "seed-policy"
]["command"][2]


class PythonMediaRuntimeTests(unittest.TestCase):
    def test_native_stdlib_dependencies(self):
        import _elementtree
        import _json
        import hashlib
        import ssl
        import zlib

        self.assertTrue(ssl.create_default_context())
        self.assertEqual(ET.fromstring("<config><value>ok</value></config>").findtext("value"), "ok")
        self.assertEqual(zlib.decompress(zlib.compress(b"ok")), b"ok")
        self.assertEqual(len(hashlib.sha256(b"ok").digest()), 32)

    def test_cleanup_preserves_unrelated_and_recent_downloads(self):
        cutoff = datetime.now(timezone.utc) - timedelta(hours=24)
        old = {
            "id": 1, "title": "fixture", "added": "2020-01-01T00:00:00Z",
            "status": "completed", "trackedDownloadStatus": "warning",
            "statusMessages": [{"messages": [CLEANUP.SUPERSEDED_REASONS[0]]}],
        }
        self.assertTrue(CLEANUP.is_superseded(old, cutoff))
        for change in (
            {"added": datetime.now(timezone.utc).isoformat()},
            {"status": "downloading"},
            {"trackedDownloadStatus": "ok"},
            {"statusMessages": [{"messages": ["Unrelated warning"]}]},
            {"statusMessages": []},
        ):
            self.assertFalse(CLEANUP.is_superseded({**old, **change}, cutoff))

    def test_cleanup_delete_and_dry_run(self):
        record = {
            "id": 42, "title": "fixture", "added": "2020-01-01T00:00:00Z",
            "status": "completed", "trackedDownloadStatus": "warning",
            "statusMessages": [{"messages": [CLEANUP.SUPERSEDED_REASONS[0]]}],
        }
        config = ET.ElementTree(ET.fromstring("<Config><ApiKey>fixture</ApiKey></Config>"))
        for dry_run in (False, True):
            with self.subTest(dry_run=dry_run), mock.patch.object(
                CLEANUP.ET, "parse", return_value=config
            ), mock.patch.object(CLEANUP, "DRY_RUN", dry_run), mock.patch.object(
                CLEANUP, "request", return_value={"records": [record]}
            ) as request, redirect_stdout(io.StringIO()):
                CLEANUP.main()
                self.assertEqual(request.call_count, 1 if dry_run else 2)
                if not dry_run:
                    self.assertEqual(request.call_args.kwargs, {"method": "DELETE"})
                    path, query = request.call_args.args[1].split("?")
                    self.assertEqual(path, "/api/v3/queue/42")
                    self.assertEqual(urllib.parse.parse_qs(query), {
                        "removeFromClient": ["true"], "blocklist": ["false"],
                        "deleteFiles": ["true"],
                    })

    def test_seed_policy_starts_only_books_and_stops_only_other_seeders(self):
        torrents = [
            {"hash": "start", "category": "books", "progress": 1, "state": "stoppedUP"},
            {"hash": "stop", "category": "tv", "progress": 1, "state": "uploading"},
            {"hash": "keep", "category": "books", "progress": 1, "state": "uploading"},
            {"hash": "incomplete", "category": "tv", "progress": 0.5, "state": "downloading"},
        ]

        class EndIteration(Exception):
            pass

        calls = []

        def urlopen(request, timeout):
            calls.append(request)
            response = mock.MagicMock()
            response.__enter__.return_value = io.StringIO(json.dumps(torrents))
            return response

        with mock.patch.dict(os.environ, {"SEED_CATEGORY": "books", "POLL_INTERVAL_SECONDS": "15"}), mock.patch(
            "urllib.request.urlopen", side_effect=urlopen
        ), mock.patch("time.sleep", side_effect=EndIteration), redirect_stdout(io.StringIO()):
            with self.assertRaises(EndIteration):
                exec(compile(SEED_SOURCE, "seed-policy", "exec"), {})
        self.assertEqual(len(calls), 3)
        for request, action, torrent_hash in zip(calls[1:], ("start", "stop"), ("start", "stop")):
            self.assertTrue(request.full_url.endswith(f"/torrents/{action}"))
            self.assertEqual(urllib.parse.parse_qs(request.data.decode()), {"hashes": [torrent_hash]})


if __name__ == "__main__":
    unittest.main()
