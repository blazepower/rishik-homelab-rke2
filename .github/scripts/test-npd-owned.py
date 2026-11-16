"""Verify helper substitutions and raw positive findings cannot hide in PID-1 proof."""
import hashlib
import importlib.util
import json
from pathlib import Path
import subprocess
import tempfile
import unittest
from unittest.mock import patch

spec = importlib.util.spec_from_file_location("npd", Path(__file__).with_name("verify-npd-owned.py"))
npd = importlib.util.module_from_spec(spec)
spec.loader.exec_module(npd)

BASE_PACKAGES = b"\n\n".join(
    ("Package: " + name + "\nStatus: install ok installed\nArchitecture: amd64\nVersion: " + version).encode()
    for name, version in (("apt", "2.6.1"), ("libapt-pkg6.0", "2.6.1"), ("libc6", "2.36"))
)
PATCHED_PACKAGES = BASE_PACKAGES.replace(b"Version: 2.6.1", b"Version: 2.6.2")
PACKAGE_LOCK = {
    "vendor_package_status_sha256": hashlib.sha256(BASE_PACKAGES).hexdigest(),
    "runtime_package_updates": {name: {"architecture": "amd64", "from_version": "2.6.1", "version": "2.6.2"}
                                for name in ("apt", "libapt-pkg6.0")},
}


class ThreeProgramProofTest(unittest.TestCase):
    def execute(self, wrong_compiler=None, changed_helper=None, positive_helper=None, changed_config=False):
        names = ("node-problem-detector", "health-checker", "log-counter")
        raw = {name: ("synthetic " + name).encode() for name in names}
        lock = {"compiler": "go1.27.4", "govulncheck": "v1.9.0", "builder": "fixture",
                **PACKAGE_LOCK,
                "monitor_config_sha256": {"kernel-monitor.json": hashlib.sha256(b"config").hexdigest()},
                "binaries": {name: {"path": "/" + name} for name in names}}
        source = json.loads(json.dumps(lock))
        source["actual_binaries"] = {}
        scans = {}
        for name in names:
            summaries = {}
            for mode in ("source", "binary"):
                config = {"scanner_name": "govulncheck", "scanner_version": "v1.9.0",
                          "scan_mode": mode, "scan_level": "symbol", "db": "https://vuln.go.dev",
                          "db_last_modified": "2026-11-11T02:51:11Z"}
                text = json.dumps({"config": config})
                summaries[mode] = npd.scanner.advisory_summary(text, mode, "v1.9.0")
                if name == positive_helper and mode == "binary":
                    text += json.dumps({"finding": {"osv": "GO-forbidden", "trace": [{"function": "main"}]}})
                scans[name + "-" + mode + ".jsonl"] = text.encode()
            source["actual_binaries"][name] = {
                "sha256": hashlib.sha256(raw[name]).hexdigest(), "symbol_table_verified": True,
                "advisory_scans": summaries,
            }

        def docker(*args):
            output = ""
            if args[0] == "create":
                output = "container"
            elif args[0] == "cp":
                name = args[1].rsplit("/", 1)[1]
                if name == "status":
                    data = PATCHED_PACKAGES
                elif name == "kernel-monitor.json":
                    data = b"tampered" if changed_config else b"config"
                elif name == "source.json":
                    data = json.dumps(source).encode()
                elif name in scans:
                    data = scans[name]
                else:
                    data = raw[name] + (b"tampered" if name == changed_helper else b"")
                Path(args[2]).write_bytes(data)
            elif args[0] == "run":
                compiler = "go1.27.2" if args[-1].endswith("/" + str(wrong_compiler)) else "go1.27.4"
                output = args[-1] + ": " + compiler + "\n\tbuild\tCGO_ENABLED=1\n\tbuild\t-tags=journald\n"
            return subprocess.CompletedProcess(args, 0, stdout=output)

        with tempfile.TemporaryDirectory(prefix=".npd-unit-", dir=npd.ROOT) as directory:
            (Path(directory) / "vendor-package-status").write_bytes(BASE_PACKAGES)
            with patch.object(npd, "docker", side_effect=docker):
                return npd.binary_proof("fixture", lock, Path(directory))[0]

    def test_accepts_all_three_actual_programs(self):
        self.assertEqual(set(self.execute()), {"node-problem-detector", "health-checker", "log-counter"})

    def test_rejects_an_old_compiler_in_a_helper_despite_patched_main(self):
        with self.assertRaises(AssertionError):
            self.execute(wrong_compiler="health-checker")

    def test_rejects_a_replaced_helper_despite_patched_main(self):
        with self.assertRaises(AssertionError):
            self.execute(changed_helper="log-counter")

    def test_rejects_raw_helper_finding_even_when_summary_claims_zero(self):
        with self.assertRaises(AssertionError):
            self.execute(positive_helper="health-checker")

    def test_rejects_silently_changed_monitor_coverage(self):
        with self.assertRaises(AssertionError):
            self.execute(changed_config=True)


class RuntimePatchProofTest(unittest.TestCase):
    def test_rejects_an_undeclared_dependency_upgrade(self):
        with self.assertRaises(AssertionError):
            npd.package_proof(PATCHED_PACKAGES.replace(b"Version: 2.36", b"Version: 2.37"),
                              PACKAGE_LOCK, BASE_PACKAGES)

    def test_rejects_a_silently_added_package(self):
        extra = b"\n\nPackage: unexpected\nStatus: install ok installed\nArchitecture: amd64\nVersion: 1"
        with self.assertRaises(AssertionError):
            npd.package_proof(PATCHED_PACKAGES + extra, PACKAGE_LOCK, BASE_PACKAGES)

    def test_rejects_an_incomplete_security_patch(self):
        with self.assertRaises(AssertionError):
            npd.package_proof(PATCHED_PACKAGES.replace(b"Version: 2.6.2", b"Version: 2.6.1", 1),
                              PACKAGE_LOCK, BASE_PACKAGES)


if __name__ == "__main__":
    unittest.main()
