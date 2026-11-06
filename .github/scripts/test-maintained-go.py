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


class ArtifactGateTest(unittest.TestCase):
    def setUp(self):
        self.directory = module.ROOT / ".maintained-go-unit"
        self.directory.mkdir(exist_ok=False)

    def tearDown(self):
        shutil.rmtree(self.directory)

    def execute(self, compiler="go1.27.3", claimed_hash=None):
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
        with self.assertRaises(AssertionError):
            self.execute(compiler="go1.25.11")

    def test_rejects_source_evidence_not_bound_to_actual_binary(self):
        with self.assertRaises(AssertionError):
            self.execute(claimed_hash="0" * 64)

    def test_accepts_actual_patched_binary_bound_to_source_evidence(self):
        self.assertIn("go1.27.3", self.execute()["actual_elf_buildinfo"])
        self.assertFalse((self.directory / "application.elf").exists())


if __name__ == "__main__":
    unittest.main()
