"""Check fail-closed Plex startup and its isolated Linux trust-store sanitizer."""

import datetime
import hashlib
import importlib.util
import json
from pathlib import Path
import shutil
import subprocess
import sys
import tempfile
import unittest
import uuid

from cryptography import x509
from cryptography.hazmat.primitives import hashes, serialization
from cryptography.hazmat.primitives.asymmetric import ec
from cryptography.x509.oid import NameOID
import yaml


ROOT = Path(__file__).resolve().parents[2]
PLEX = ROOT / "apps" / "plex"
RELEASE = yaml.safe_load((PLEX / "helmrelease.yaml").read_text())


class PlexCaWiringTests(unittest.TestCase):
    def test_runtime_policy_exactly_matches_the_authoritative_policy(self):
        canonical = json.loads((ROOT / ".github" / "security" / "emudhra-2026.json").read_text())
        digests = (PLEX / "blocked-spkis.sha256").read_text().splitlines()
        self.assertCountEqual(digests, [e["sha256"].upper() for e in canonical["blocked_spkis"]])
        self.assertEqual(len(digests), len(set(digests)))

    def test_startup_is_fail_closed_and_preserves_original_init(self):
        patches = RELEASE["spec"]["postRenderers"][0]["kustomize"]["patches"]
        patch = yaml.safe_load(patches[0]["patch"])
        self.assertEqual(patches[0]["target"], {"kind": "StatefulSet", "name": "plex-plex-media-server"})
        container = patch["spec"]["template"]["spec"]["containers"][0]
        self.assertEqual(container["name"], "plex-plex-media-server-pms")
        self.assertEqual(container["command"], ["/bin/bash", "-ec"])
        self.assertEqual(container["args"], ["/bin/bash /ca-hardening/harden-ca.sh\nexec /init"])

    def test_main_and_auth_initializer_use_the_same_pinned_vendor_image(self):
        values = RELEASE["spec"]["values"]
        tag = values["image"]["tag"]
        self.assertRegex(tag, r"^1\.43\.5\.11029-6bf8af3f0@sha256:[a-f0-9]{64}$")
        self.assertEqual(values["extraInitContainers"][0]["name"], "require-plex-auth")
        self.assertEqual(values["extraInitContainers"][0]["image"], "plexinc/pms-docker:" + tag)
        self.assertEqual(values["extraEnv"]["ALLOWED_NETWORKS"], "")

    def test_policy_content_changes_trigger_a_helm_pod_template_change(self):
        config = yaml.safe_load((PLEX / "kustomization.yaml").read_text())
        generator = next(g for g in config["configMapGenerator"] if g["name"] == "plex-ca-hardening")
        self.assertCountEqual(generator["files"], ["harden-ca.sh", "blocked-spkis.sha256"])
        self.assertFalse(config.get("generatorOptions", {}).get("disableNameSuffixHash", False))
        name_ref = json.loads((PLEX / "kustomizeconfig.json").read_text())
        self.assertIn({
            "group": "helm.toolkit.fluxcd.io", "kind": "HelmRelease",
            "path": "spec/values/extraVolumes/configMap/name",
        }, name_ref["nameReference"][0]["fieldSpecs"])
        values = RELEASE["spec"]["values"]
        volume = next(v for v in values["extraVolumes"] if v["name"] == "plex-ca-hardening")
        self.assertEqual(volume["configMap"]["name"], "plex-ca-hardening")
        mount = next(v for v in values["extraVolumeMounts"] if v["name"] == "plex-ca-hardening")
        self.assertEqual(mount, {"name": "plex-ca-hardening", "mountPath": "/ca-hardening", "readOnly": True})


@unittest.skipUnless(sys.platform.startswith("linux"), "Uses the Linux OpenSSL/CA-store toolchain")
class PlexCaSanitizerTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.certificates = []
        cls.digests = []
        for _ in range(12):
            key = ec.generate_private_key(ec.SECP256R1())
            name = x509.Name([x509.NameAttribute(NameOID.COMMON_NAME, "Identical test CA names")])
            cert = (
                x509.CertificateBuilder().subject_name(name).issuer_name(name)
                .public_key(key.public_key()).serial_number(x509.random_serial_number())
                .not_valid_before(datetime.datetime(2025, 1, 1, tzinfo=datetime.timezone.utc))
                .not_valid_after(datetime.datetime(2035, 1, 1, tzinfo=datetime.timezone.utc))
                .add_extension(x509.BasicConstraints(ca=True, path_length=None), critical=True)
                .sign(key, hashes.SHA256())
            )
            cls.certificates.append(cert.public_bytes(serialization.Encoding.PEM))
            cls.digests.append(hashlib.sha256(key.public_key().public_bytes(
                serialization.Encoding.DER, serialization.PublicFormat.SubjectPublicKeyInfo,
            )).hexdigest().upper())

    def setUp(self):
        self.directory = tempfile.TemporaryDirectory()
        self.addCleanup(self.directory.cleanup)
        self.base = Path(self.directory.name)
        self.root = self.base / "root"
        self.tools = self.base / "guard"
        self.tools.mkdir()
        shutil.copyfile(PLEX / "harden-ca.sh", self.tools / "harden-ca.sh")
        self.policy = self.tools / "blocked-spkis.sha256"
        self.policy.write_text("\n".join([self.digests[0]] + [f"{n:064X}" for n in range(10)]) + "\n")
        self.sources = self.root / "usr" / "share" / "ca-certificates"
        self.local = self.root / "usr" / "local" / "share" / "ca-certificates"
        self.certs = self.root / "etc" / "ssl" / "certs"
        self.app = self.root / "usr" / "lib" / "plexmediaserver" / "Resources" / "cacert.pem"
        for directory in (self.sources, self.local, self.certs, self.app.parent):
            directory.mkdir(parents=True)
        for index, cert in enumerate(self.certificates):
            (self.sources / f"test-{index}.crt").write_bytes(cert)
        (self.root / "etc" / "ca-certificates.conf").write_text(
            "".join(f"test-{index}.crt\n" for index in range(12))
        )
        self.certs.joinpath("ca-certificates.crt").write_bytes(b"".join(self.certificates))
        self.app.write_bytes(b"".join(self.certificates))

    def run_guard(self):
        return subprocess.run(
            ["bash", str(self.tools / "harden-ca.sh"), str(self.root)],
            capture_output=True, text=True, timeout=90,
        )

    def test_removes_exact_keys_from_sources_app_bundle_and_hash_aliases(self):
        (self.certs / "deadbeef.0").write_bytes(self.certificates[0])
        (self.local / "private-unaffected.crt").write_bytes(self.certificates[-1])
        first = self.run_guard()
        self.assertEqual(first.returncode, 0, first.stdout + first.stderr)
        self.assertFalse((self.sources / "test-0.crt").exists())
        self.assertFalse((self.certs / "deadbeef.0").exists())
        self.assertTrue((self.local / "private-unaffected.crt").exists())
        for bundle in (self.certs / "ca-certificates.crt", self.app):
            certs = x509.load_pem_x509_certificates(bundle.read_bytes())
            hashes_found = {hashlib.sha256(c.public_key().public_bytes(
                serialization.Encoding.DER, serialization.PublicFormat.SubjectPublicKeyInfo,
            )).hexdigest().upper() for c in certs}
            self.assertNotIn(self.digests[0], hashes_found)
            self.assertTrue(set(self.digests[1:]).issubset(hashes_found))
        second = self.run_guard()
        self.assertEqual(second.returncode, 0, second.stdout + second.stderr)

    def test_invalid_partial_duplicate_and_empty_policy_fail_closed(self):
        for text in ["", "bad\n", self.digests[0] + "\n", (self.digests[0] + "\n") * 11]:
            self.policy.write_text(text)
            result = self.run_guard()
            self.assertNotEqual(result.returncode, 0)
            self.assertNotIn("original /init may start", result.stdout)

    def test_malformed_and_missing_app_bundles_fail_closed(self):
        for text in [b"", b"-----BEGIN CERTIFICATE-----\nbroken\n", b"-----BEGIN CERTIFICATE-----\nbad\n-----END CERTIFICATE-----\n"]:
            self.app.write_bytes(text)
            self.assertNotEqual(self.run_guard().returncode, 0)
        self.app.unlink()
        self.assertNotEqual(self.run_guard().returncode, 0)

    def test_source_symlinks_cannot_modify_files_outside_the_trust_tree(self):
        external = self.base / "untouched.crt"
        external.write_bytes(self.certificates[0])
        (self.sources / "external.crt").symlink_to(external)
        self.assertNotEqual(self.run_guard().returncode, 0)
        self.assertEqual(external.read_bytes(), self.certificates[0])


@unittest.skipUnless(sys.platform.startswith("linux"), "Uses an isolated Linux Docker runner")
class PlexVendorImageTests(unittest.TestCase):
    def test_guard_sanitizes_the_pinned_vendor_image_without_starting_plex(self):
        image = RELEASE["spec"]["values"]["extraInitContainers"][0]["image"]
        name = "plex-ca-guard-test-" + uuid.uuid4().hex[:12]
        created = subprocess.run([
            "docker", "create", "--name", name, "--network", "none",
            "--cap-drop", "ALL", "--security-opt", "no-new-privileges",
            "--volume", str((PLEX / "harden-ca.sh").resolve()) + ":/ca-hardening/harden-ca.sh:ro",
            "--volume", str((PLEX / "blocked-spkis.sha256").resolve()) + ":/ca-hardening/blocked-spkis.sha256:ro",
            "--entrypoint", "/bin/bash", image, "/ca-hardening/harden-ca.sh",
        ], capture_output=True, text=True, timeout=300)
        self.assertEqual(created.returncode, 0, created.stdout + created.stderr)
        self.addCleanup(subprocess.run, ["docker", "rm", "--volumes", name], check=True, capture_output=True)
        result = subprocess.run(["docker", "start", "--attach", name], capture_output=True, text=True, timeout=180)
        self.assertEqual(result.returncode, 0, result.stdout + result.stderr)
        state = json.loads(subprocess.run(
            ["docker", "inspect", name], capture_output=True, text=True, check=True, timeout=30,
        ).stdout)[0]["State"]
        self.assertEqual(state["ExitCode"], 0, result.stdout + result.stderr)
        self.assertIn("original /init may start", result.stdout)
        spec = importlib.util.spec_from_file_location("plex_ca_verifier", ROOT / ".github" / "scripts" / "verify-emudhra-trust.py")
        verifier = importlib.util.module_from_spec(spec)
        spec.loader.exec_module(verifier)
        denied = verifier.load_policy(verifier.DEFAULT_POLICY)
        with tempfile.TemporaryDirectory() as directory:
            for index, source in enumerate((
                "/etc/ssl/certs/ca-certificates.crt",
                "/usr/lib/plexmediaserver/Resources/cacert.pem",
            )):
                target = Path(directory) / f"bundle-{index}.pem"
                subprocess.run(["docker", "cp", name + ":" + source, str(target)], check=True, capture_output=True, timeout=30)
                report = verifier.scan_bundle(target.read_bytes(), denied)
                self.assertGreaterEqual(report["certificate_count"], 10)
                self.assertEqual(report["denied_certificates"], [])


if __name__ == "__main__":
    unittest.main()
