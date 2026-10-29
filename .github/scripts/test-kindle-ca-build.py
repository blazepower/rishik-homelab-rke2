#!/usr/bin/env python3
"""Kindle-only regression coverage for immutable, offline acceptance preparation."""

import base64
import hashlib
import importlib.util
import json
import re
import sys
import unittest
import uuid
from pathlib import Path
from unittest.mock import patch


sys.dont_write_bytecode = True
ROOT = Path(__file__).resolve().parents[2]
SOURCE = ROOT / "apps" / "kindle-sender" / "src"
SPEC = importlib.util.spec_from_file_location("kindle_ca", Path(__file__).with_name("verify-kindle-ca.py"))
VERIFY = importlib.util.module_from_spec(SPEC)
SPEC.loader.exec_module(VERIFY)


class KindleBuildTests(unittest.TestCase):
    def test_exact_published_bases_and_native_toolchain(self):
        dockerfile = (SOURCE / "Dockerfile").read_text()
        self.assertRegex(dockerfile, r"FROM golang:1\.27\.3-bookworm@sha256:[a-f0-9]{64} AS build")
        self.assertRegex(dockerfile, r"gcr.io/distroless/static-debian12:nonroot@sha256:[a-f0-9]{64}")
        self.assertIn("ENV GOTOOLCHAIN=local", dockerfile)
        self.assertIn("go version -m /build/kindle-sender", dockerfile)
        self.assertIn("CGO_ENABLED=1 GOOS=linux go build", dockerfile)

    def test_all_trust_paths_replaced_before_runtime(self):
        text = (SOURCE / "Dockerfile").read_text()
        self.assertIn("COPY --from=kindle-policy emudhra-2026.json", text)
        self.assertLess(text.index("/build/kindle-clear-ca /usr/share"), text.index("update-ca-certificates --fresh"))
        self.assertLess(text.index("rm -rf /runtime-root/etc/ssl/certs"), text.index("FROM scratch AS runtime"))
        self.assertIn("cp -a /usr/share/ca-certificates /runtime-root/usr/share/", text)
        self.assertIn("ENV SSL_CERT_FILE=/etc/ssl/certs/ca-certificates.crt", text)
        self.assertIn("FROM runtime AS final", text)
        self.assertIn("USER 65532:65532", text)

    def test_public_fixtures_match_exact_canonical_intermediates(self):
        fixtures = json.loads((SOURCE / "kindle-ca-spkis.json").read_text())
        denied = VERIFY.CA.load_policy()
        self.assertEqual(len(fixtures), 8)
        hashes = set()
        for fixture in fixtures:
            digest = hashlib.sha256(base64.b64decode(fixture["spki_der_base64"], validate=True)).hexdigest()
            self.assertEqual(digest, fixture["spki_sha256"])
            self.assertIn(digest, denied)
            self.assertRegex(fixture["certificate_sha256"], r"^[a-f0-9]{64}$")
            hashes.add(digest)
        self.assertEqual(len(hashes), 8)

    def test_native_tests_include_policy_errors_not_generic_tls_failure(self):
        text = (SOURCE / "kindle_ca_test.go").read_text()
        for name in ("Toolchain", "NativeDenylist", "TLSControls", "RuntimeRoots"):
            self.assertIn(f"func TestKindleCA{name}", text)
        self.assertIn('verification.Err.Error() != "x509: no valid chains built"', text)
        self.assertIn("leaf.CheckSignatureFrom(root)", text)
        self.assertIn('net.Listen("tcp", "127.0.0.1:0")', text)
        self.assertNotIn("InsecureSkipVerify", text)

    def test_workflow_never_publishes_or_uses_credentials(self):
        text = (ROOT / ".github" / "workflows" / "kindle-ca-build.yaml").read_text()
        self.assertIn("contents: read", text)
        self.assertIn("python3 .github/scripts/verify-kindle-ca.py", text)
        self.assertIn("GITHUB_STEP_SUMMARY", text)
        for forbidden in ("secrets.", "login-action", "packages: write", "push: true", "pull_request_target"):
            self.assertNotIn(forbidden, text)

    def build_info(self):
        dependencies = re.findall(r"^\s*([a-zA-Z0-9._/-]+)\s+(v[^\s]+)", (SOURCE / "go.mod").read_text(), re.MULTILINE)
        return (
            "binary: go1.27.3\n\tpath\tgithub.com/blazepower/kindle-sender\n"
            + "".join(f"\tdep\t{name}\t{version}\th1:public-checksum\n" for name, version in dependencies)
            + "\tbuild\tCGO_ENABLED=1\n\tbuild\tGOOS=linux\n\tbuild\tGOARCH=amd64\n"
        )

    def test_native_info_requires_actual_toolchain_and_exact_dependencies(self):
        with patch.object(VERIFY, "command", return_value=self.build_info()):
            self.assertIn("go1.27.3", VERIFY.native_build_info(Path("binary")))
        for bad in (
            self.build_info().replace("go1.27.3", "go1.21.13"),
            self.build_info().replace("CGO_ENABLED=1", "CGO_ENABLED=0"),
            self.build_info().replace("v1.18.0", "v1.19.0"),
        ):
            with self.subTest(info=bad), patch.object(VERIFY, "command", return_value=bad):
                with self.assertRaises(ValueError):
                    VERIFY.native_build_info(Path("binary"))

    def exercise(self, different_binary=False, missing_test=False):
        calls = []
        output = ROOT / f".kindle-ca-test-proof-{uuid.uuid4().hex}.json"
        output.write_text('{"stale":true}')
        before = set(ROOT.glob(".kindle-ca-check-*"))

        def fake(*args, capture=False):
            calls.append(args)
            if args[:3] == ("docker", "buildx", "build"):
                Path(args[args.index("--iidfile") + 1]).write_text("sha256:" + "1" * 64)
            elif args[:3] == ("docker", "image", "inspect"):
                return "linux/amd64"
            elif args[:2] == ("docker", "create"):
                return "a" * 64
            elif args[:2] == ("docker", "cp"):
                destination = Path(args[-1])
                data = b"binary" if destination.name.endswith("-binary") else b"bundle"
                if different_binary and destination.name == "kindle-ca-acceptance-binary":
                    data = b"different"
                destination.write_bytes(data)
            elif args[:2] == ("docker", "run"):
                names = ["Toolchain", "NativeDenylist", "TLSControls", "RuntimeRoots"]
                if missing_test:
                    names.pop()
                return "\n".join(f"--- PASS: TestKindleCA{name} (0.01s)" for name in names)
            return ""

        try:
            with patch.object(VERIFY, "command", side_effect=fake), patch.object(VERIFY, "native_build_info", return_value=self.build_info()), patch.object(VERIFY.CA, "check_bundle", return_value=100), patch("builtins.print"):
                if different_binary or missing_test:
                    with self.assertRaises(ValueError):
                        VERIFY.verify(output)
                    self.assertFalse(output.exists())
                else:
                    proof = VERIFY.verify(output)
                    self.assertEqual(proof["blocked_spki_count"], 11)
                    self.assertTrue(output.exists())
                    run = next(call for call in calls if call[:2] == ("docker", "run"))
                    self.assertIn("--read-only", run)
                    self.assertEqual(run[run.index("--network") + 1], "none")
                    self.assertIn("--cap-drop", run)
            self.assertEqual(set(ROOT.glob(".kindle-ca-check-*")), before)
            self.assertEqual(sum(call[:2] == ("docker", "rm") for call in calls), 2)
        finally:
            output.unlink(missing_ok=True)

    def test_acceptance_matches_production_and_cleans_up(self):
        self.exercise()

    def test_differing_actual_binary_fails_closed(self):
        self.exercise(different_binary=True)

    def test_missing_native_root_evidence_fails_closed(self):
        self.exercise(missing_test=True)


if __name__ == "__main__":
    unittest.main()
