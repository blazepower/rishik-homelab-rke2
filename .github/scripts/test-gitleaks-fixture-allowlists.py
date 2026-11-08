#!/usr/bin/env python3
"""Keep historical synthetic fixture exceptions exact and reject other values."""
import argparse
import json
from pathlib import Path
import subprocess
import tempfile
import unittest
import uuid


class FixtureAllowlistTests(unittest.TestCase):
    def scan(self, text):
        with tempfile.TemporaryDirectory() as folder:
            root = Path(folder)
            fixture = root / "fixture.yaml"
            fixture.write_text(text, encoding="utf-8")
            report = root / "report.json"
            result = subprocess.run(
                [SCANNER, "detect", "--no-git", "--source", str(root),
                 "--config", str(CONFIG), "--redact", "--exit-code", "23",
                 "--report-format", "json", "--report-path", str(report)],
                capture_output=True,
            )
            self.assertIn(result.returncode, (0, 23), "Scanner execution failed")
            return json.loads(report.read_text()) if report.exists() else []

    def test_exact_disposable_password_is_allowed(self):
        value = "synthetic-" + "source-vet"
        self.assertEqual(self.scan("PASSWORD" + ": " + value + "\n"), [])

    def test_exact_disposable_bearer_is_allowed(self):
        value = "synthetic-" + "rehearsal-only"
        self.assertEqual(self.scan("Authorization: " + "Bearer" + " " + value + "\n"), [])

    def test_other_password_is_detected(self):
        value = "synthetic-" + "source-vet-" + uuid.uuid4().hex
        findings = self.scan("PASSWORD" + ": " + value + "\n")
        self.assertTrue(any(x["RuleID"] == "generic-password" for x in findings))

    def test_other_bearer_is_detected(self):
        value = "synthetic-" + "rehearsal-only-" + uuid.uuid4().hex
        findings = self.scan("Authorization: " + "Bearer" + " " + value + "\n")
        self.assertTrue(any(x["RuleID"] == "bearer-token" for x in findings))


if __name__ == "__main__":
    parser = argparse.ArgumentParser()
    parser.add_argument("--gitleaks", default="gitleaks")
    args = parser.parse_args()
    SCANNER = args.gitleaks
    CONFIG = Path(__file__).resolve().parents[2] / ".gitleaks.toml"
    unittest.main(argv=[__file__])
