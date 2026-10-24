#!/usr/bin/env python3
"""Offline regression tests for conservative trust-bundle inventory."""

from __future__ import annotations

import contextlib
import datetime
import hashlib
import importlib.util
import io
import json
import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch

from cryptography import x509
from cryptography.hazmat.primitives import hashes, serialization
from cryptography.hazmat.primitives.asymmetric import ec
from cryptography.x509.oid import NameOID


SCRIPT = Path(__file__).with_name("verify-emudhra-trust.py")
SPEC = importlib.util.spec_from_file_location("verify_emudhra_trust", SCRIPT)
assert SPEC is not None and SPEC.loader is not None
verifier = importlib.util.module_from_spec(SPEC)
SPEC.loader.exec_module(verifier)


class TrustBundleTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        key = ec.generate_private_key(ec.SECP256R1())
        name = x509.Name([x509.NameAttribute(NameOID.COMMON_NAME, "Offline test CA")])
        certificate = (
            x509.CertificateBuilder()
            .subject_name(name)
            .issuer_name(name)
            .public_key(key.public_key())
            .serial_number(x509.random_serial_number())
            .not_valid_before(datetime.datetime(2025, 1, 1, tzinfo=datetime.timezone.utc))
            .not_valid_after(datetime.datetime(2035, 1, 1, tzinfo=datetime.timezone.utc))
            .add_extension(x509.BasicConstraints(ca=True, path_length=None), critical=True)
            .sign(key, hashes.SHA256())
        )
        cls.pem = certificate.public_bytes(serialization.Encoding.PEM)
        spki = key.public_key().public_bytes(
            serialization.Encoding.DER, serialization.PublicFormat.SubjectPublicKeyInfo
        )
        cls.digest = hashlib.sha256(spki).hexdigest().upper()

    def test_policy_covers_eight_keys_and_three_alternate_trust_paths(self):
        policy = verifier.load_policy(verifier.DEFAULT_POLICY)
        self.assertEqual(len(policy), 11)
        self.assertIn("AAA Certificate Services", policy.values())
        self.assertIn("emSign Root TLS CA - G3", policy.values())
        self.assertIn("emSign ECC Root CA - G3", policy.values())

    def test_unaffected_certificate_passes(self):
        report = verifier.scan_bundle(self.pem, verifier.load_policy(verifier.DEFAULT_POLICY))
        self.assertEqual(report["certificate_count"], 1)
        self.assertEqual(report["denied_certificates"], [])

    def test_matching_key_is_rejected_in_later_bundle_entry(self):
        report = verifier.scan_bundle(self.pem + self.pem, {self.digest: "test"})
        self.assertEqual(report["certificate_count"], 2)
        self.assertEqual(len(report["denied_certificates"]), 2)
        self.assertEqual(report["denied_certificates"][0]["spki_sha256"], self.digest)

    def test_empty_and_malformed_bundles_fail(self):
        for data in (b"", b"not a certificate", b"-----BEGIN CERTIFICATE-----\nbad\n"):
            with self.subTest(data=data), self.assertRaises(ValueError):
                verifier.scan_bundle(data, {})

    def test_empty_duplicate_and_invalid_policies_fail(self):
        entries = {"name": "test", "sha256": self.digest}
        for blocked in ([], [entries, entries], [{"name": "test", "sha256": "bad"}]):
            with self.subTest(blocked=blocked), tempfile.TemporaryDirectory() as directory:
                path = Path(directory) / "policy.json"
                path.write_text(json.dumps({"blocked_spkis": blocked}), encoding="utf-8")
                with self.assertRaises(ValueError):
                    verifier.load_policy(path)

    def test_cli_distinguishes_denial_from_invalid_input(self):
        with tempfile.TemporaryDirectory() as directory:
            path = Path(directory) / "bundle.pem"
            policy = Path(directory) / "policy.json"
            path.write_bytes(self.pem)
            policy.write_text(
                json.dumps({"blocked_spkis": [{"name": "test", "sha256": self.digest}]}),
                encoding="utf-8",
            )
            with contextlib.redirect_stdout(io.StringIO()):
                self.assertEqual(verifier.main(["--bundle", str(path)]), 0)
                self.assertEqual(
                    verifier.main(["--bundle", str(path), "--policy", str(policy)]), 1
                )
            path.write_bytes(b"")
            with contextlib.redirect_stderr(io.StringIO()):
                self.assertEqual(verifier.main(["--bundle", str(path)]), 2)

    def test_cli_stdin_is_supported_but_cannot_be_reused(self):
        stream = io.TextIOWrapper(io.BytesIO(self.pem), encoding="utf-8")
        with patch("sys.stdin", stream), contextlib.redirect_stdout(io.StringIO()):
            self.assertEqual(verifier.main(["--bundle", "-"]), 0)
        with contextlib.redirect_stderr(io.StringIO()):
            self.assertEqual(verifier.main(["--bundle", "-", "--bundle", "-"]), 2)


if __name__ == "__main__":
    unittest.main()
