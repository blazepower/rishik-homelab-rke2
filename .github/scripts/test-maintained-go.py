"""Focused fail-closed tests for actual ELF acceptance."""
import hashlib
import importlib.util
import json
from pathlib import Path
import shutil
import subprocess
import unittest
from unittest.mock import patch

spec = importlib.util.spec_from_file_location("maintained", Path(__file__).with_name("verify-maintained-go.py"))
module = importlib.util.module_from_spec(spec)
spec.loader.exec_module(module)


class PlatformDigestTest(unittest.TestCase):
    def test_selects_runtime_instead_of_index_or_attestation(self):
        digest = "sha256:" + "a" * 64
        manifest = {"schemaVersion": 2, "manifests": [
            {"digest": "sha256:" + "b" * 64, "platform": {"os": "unknown", "architecture": "unknown"}},
            {"digest": digest, "platform": {"os": "linux", "architecture": "amd64"}},
        ]}
        self.assertEqual(module.select_amd64_digest(manifest, "sha256:" + "c" * 64), digest)

    def test_accepts_a_direct_runtime_manifest(self):
        digest = "sha256:" + "a" * 64
        for media in ("application/vnd.oci.image.manifest.v1+json",
                      "application/vnd.docker.distribution.manifest.v2+json"):
            with self.subTest(media=media):
                self.assertEqual(module.select_amd64_digest(
                    {"schemaVersion": 2, "mediaType": media}, digest,
                ), digest)

    def test_rejects_missing_ambiguous_or_invalid_runtime(self):
        runtime = {"digest": "sha256:" + "a" * 64, "platform": {"os": "linux", "architecture": "amd64"}}
        for children in ([], [runtime, runtime], [{**runtime, "digest": "not-a-sha256"}]):
            with self.subTest(children=children), self.assertRaises(AssertionError):
                module.select_amd64_digest({"schemaVersion": 2, "manifests": children}, "sha256:" + "b" * 64)


class ArtifactGateTest(unittest.TestCase):
    def setUp(self):
        self.directory = module.ROOT / ".maintained-go-unit"
        self.directory.mkdir(exist_ok=False)

    def tearDown(self):
        shutil.rmtree(self.directory)

    def execute(self, compiler="go1.27.4", claimed_hash=None):
        binary = b"synthetic ELF fixture"
        source = {"revision": "fixture-revision", "app": "gluetun",
                  "binary_sha256": claimed_hash or hashlib.sha256(binary).hexdigest()}

        def docker(*args):
            output = ""
            if args[0] == "create":
                output = "fixture-container"
            elif args[0] == "cp":
                Path(args[2]).write_bytes(binary if args[1].endswith("/binary") else json.dumps(source).encode())
            elif args[0] == "run":
                output = f"/proof/application.elf: {compiler}\n\tbuild\tCGO_ENABLED=0\n"
            return subprocess.CompletedProcess(args, 0, stdout=output)

        with patch.object(module, "docker", side_effect=docker):
            return module.proof_binary("fixture", {"binary": "/binary", "revision": "fixture-revision"},
                                       self.directory, "fixture-builder")

    def test_rejects_old_actual_application_compiler(self):
        for compiler in ("go1.25.11", "go1.27.3"):
            with self.subTest(compiler=compiler), self.assertRaises(AssertionError):
                self.execute(compiler=compiler)

    def test_rejects_source_evidence_not_bound_to_actual_binary(self):
        with self.assertRaises(AssertionError):
            self.execute(claimed_hash="0" * 64)

    def test_accepts_actual_patched_binary_bound_to_source_evidence(self):
        self.assertIn("go1.27.4", self.execute()["actual_elf_buildinfo"])
        self.assertFalse((self.directory / "application.elf").exists())


if __name__ == "__main__":
    unittest.main()
