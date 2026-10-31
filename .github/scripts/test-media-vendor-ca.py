"""Media vendor CA regressions; --registry adds fresh daemonless PEM scans, not runtime certification."""

import argparse
import datetime
import hashlib
import importlib.util
import io
import json
from pathlib import Path
import posixpath
import subprocess
import sys
import tarfile
import unittest
import uuid

from cryptography import x509
from cryptography.hazmat.primitives import hashes, serialization
from cryptography.hazmat.primitives.asymmetric import ec
from cryptography.x509.oid import NameOID
import yaml


ROOT = Path(__file__).resolve().parents[2]
POLICY = ROOT / ".github" / "security" / "emudhra-2026.json"
EVIDENCE = ROOT / ".github" / "security" / "media-vendor-ca-20261026.json"
spec = importlib.util.spec_from_file_location(
    "media_pem_verifier", ROOT / ".github" / "scripts" / "verify-emudhra-trust.py"
)
verifier = importlib.util.module_from_spec(spec)
spec.loader.exec_module(verifier)
DENIED = verifier.load_policy(POLICY)
REGISTRY = False
CRANE = "crane"
SYSTEM_BUNDLE = "etc/ssl/certs/ca-certificates.crt"
TRUST_ROOTS = (
    "etc/ssl/certs/", "etc/pki/ca-trust/", "usr/share/ca-certificates/",
    "usr/local/share/ca-certificates/",
)


def inspect_archive(stream, denied):
    """Read only public trust-store members, without extracting the filesystem."""
    stores, aliases = {}, {}
    with tarfile.open(fileobj=stream, mode="r|") as archive:
        for member in archive:
            name = member.name.removeprefix("./").lstrip("/")
            if ".." in name.split("/"):
                raise ValueError("Unsafe archive path")
            if member.issym() or member.islnk():
                target = member.linkname
                aliases[name] = (
                    target.lstrip("/") if target.startswith("/") or member.islnk()
                    else posixpath.normpath(posixpath.join(posixpath.dirname(name), target))
                )
            relevant = (
                name.startswith(TRUST_ROOTS)
                or name == "etc/ssl/cert.pem"
                or name.endswith(("/cacert.pem", "/cert.pem", "/ca-bundle.crt"))
            )
            if not member.isfile() or not relevant:
                continue
            if member.size > 8 * 1024 * 1024:
                raise ValueError(f"Uninspected oversized trust file: {name}")
            content = archive.extractfile(member).read()
            if name.startswith(TRUST_ROOTS) and not name.endswith((".crt", ".pem")):
                if b"-----BEGIN CERTIFICATE-----" not in content:
                    continue
            stores[name] = verifier.scan_bundle(content, denied)
    target, seen = SYSTEM_BUNDLE, set()
    while target in aliases:
        if target in seen:
            raise ValueError("Cyclic system trust-bundle alias")
        seen.add(target)
        target = aliases[target]
    if target not in stores or stores[target]["certificate_count"] < 10:
        raise ValueError("Missing or unexpectedly small system trust bundle")
    matches = [
        {"path": path, **match} for path, store in stores.items()
        for match in store["denied_certificates"]
    ]
    return {"coverage": "Public PEM stores only; runtime/chain rejection not certified",
            "stores": stores, "denied": matches}


def release(app):
    return yaml.safe_load((ROOT / "apps" / app / "helmrelease.yaml").read_text())["spec"]["values"]


class MediaVendorPinTests(unittest.TestCase):
    def test_prepared_releases_remain_held_for_serial_activation(self):
        held = (
            "calibre-web", "bazarr", "prowlarr", "sabnzbd",
            "sure-finance", "bookshelf", "qbittorrent", "overseerr",
        )
        for app in held:
            with self.subTest(app=app):
                manifest = yaml.safe_load(
                    (ROOT / "apps" / app / "helmrelease.yaml").read_text()
                )
                self.assertIs(manifest["spec"]["suspend"], True)
        radarr = yaml.safe_load((ROOT / "apps" / "radarr" / "helmrelease.yaml").read_text())
        self.assertIs(radarr["spec"]["suspend"], False)

    def test_manifest_pins_match_the_recorded_immutable_audits(self):
        evidence = json.loads(EVIDENCE.read_text())
        policy_bytes = POLICY.read_text(encoding="utf-8").encode("utf-8")
        self.assertEqual(evidence["policy_hash_encoding"], "UTF-8 with LF newlines")
        self.assertEqual(evidence["policy_sha256"], hashlib.sha256(policy_bytes).hexdigest())
        self.assertEqual(len(DENIED), 11)
        self.assertEqual(set(evidence["applications"]), {"bazarr", "prowlarr", "radarr", "sonarr", "sabnzbd"})
        for app, entry in evidence["applications"].items():
            image = release(app)["controllers"][app]["containers"]["app"]["image"]
            self.assertEqual(image["repository"] + ":" + image["tag"], entry["gitops_image"])
            self.assertRegex(entry["selected_amd64"], r"^lscr\.io/linuxserver/" + app + r"@sha256:[0-9a-f]{64}$")
            self.assertIn("runtime", entry["coverage"].lower())

    def test_radarr_is_a_same_application_version_vendor_rebuild(self):
        values = release("radarr")
        controller = values["controllers"]["radarr"]
        image = controller["containers"]["app"]["image"]
        self.assertRegex(image["tag"], r"^6\.4\.4\.10685-ls324@sha256:[0-9a-f]{64}$")
        self.assertEqual(controller["strategy"], "Recreate")
        self.assertEqual(values["persistence"]["config"]["existingClaim"], "radarr-config")
        self.assertEqual(values["persistence"]["media"]["existingClaim"], "media-root")
        self.assertEqual(values["persistence"]["media"]["globalMounts"], [
            {"path": "/media/Movies", "subPath": "movies"},
            {"path": "/media/torrents", "subPath": "downloads/torrents"},
        ])
        self.assertNotIn("command", controller["containers"]["app"])
        self.assertNotIn("initContainers", controller)

    def test_only_pem_clear_candidates_are_selected_as_clear(self):
        apps = json.loads(EVIDENCE.read_text())["applications"]
        for app in ("radarr", "sonarr"):
            entry = apps[app]
            self.assertEqual(entry["selected_pem_status"], "PEM_STORES_CLEAR")
            self.assertFalse(entry["selected_denied"])
            self.assertFalse(entry["selected_uninspected_stores"])
            self.assertIn(SYSTEM_BUNDLE, entry["selected_system_bundle_paths"])
        for app in ("bazarr", "prowlarr", "sabnzbd"):
            self.assertEqual(apps[app]["selected_pem_status"], "DENIED")
            self.assertTrue(apps[app]["selected_denied"])
            self.assertEqual(apps[app]["remediation"], "BLOCKED")

    def test_sonarr_build_number_reset_must_not_cause_a_downgrade(self):
        app = json.loads(EVIDENCE.read_text())["applications"]["sonarr"]
        self.assertIn("4.0.20.3014-ls1@", app["gitops_image"])
        self.assertEqual(app["rejected_older_candidate"]["version"], "4.0.20.3014-ls329")
        self.assertEqual(app["rejected_older_candidate"]["pem_status"], "DENIED")
        self.assertLess(app["rejected_older_candidate"]["created"], app["selected_created"])


class MediaPemScannerTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.certs, cls.spki = [], []
        for _ in range(10):
            key = ec.generate_private_key(ec.SECP256R1())
            name = x509.Name([x509.NameAttribute(NameOID.COMMON_NAME, "Identical CA name")])
            cert = (
                x509.CertificateBuilder().subject_name(name).issuer_name(name)
                .public_key(key.public_key()).serial_number(x509.random_serial_number())
                .not_valid_before(datetime.datetime(2025, 1, 1, tzinfo=datetime.timezone.utc))
                .not_valid_after(datetime.datetime(2035, 1, 1, tzinfo=datetime.timezone.utc))
                .add_extension(x509.BasicConstraints(ca=True, path_length=None), critical=True)
                .sign(key, hashes.SHA256())
            )
            cls.certs.append(cert.public_bytes(serialization.Encoding.PEM))
            cls.spki.append(hashlib.sha256(key.public_key().public_bytes(
                serialization.Encoding.DER, serialization.PublicFormat.SubjectPublicKeyInfo,
            )).hexdigest().upper())

    def scan(self, members):
        stream = io.BytesIO()
        with tarfile.open(fileobj=stream, mode="w") as archive:
            for name, value in members.items():
                member = tarfile.TarInfo(name)
                if isinstance(value, str):
                    member.type, member.linkname = tarfile.SYMTYPE, value
                    archive.addfile(member)
                else:
                    member.size = len(value)
                    archive.addfile(member, io.BytesIO(value))
        stream.seek(0)
        return inspect_archive(stream, {self.spki[0]: "Blocked test key"})

    def test_same_subject_is_not_an_identity_and_certifi_is_independent(self):
        report = self.scan({
            SYSTEM_BUNDLE: b"".join(self.certs[1:] + [self.certs[1]]),
            "lsiopy/lib/python3.14/site-packages/certifi/cacert.pem": self.certs[0],
            "app/bazarr/bin/libs/certifi/cacert.pem": self.certs[0],
        })
        self.assertEqual(len(report["denied"]), 2)
        self.assertNotIn(SYSTEM_BUNDLE, [d["path"] for d in report["denied"]])
        self.assertIn("not certified", report["coverage"])

    def test_missing_small_or_malformed_required_stores_fail_closed(self):
        for members in ({}, {SYSTEM_BUNDLE: self.certs[0]}, {SYSTEM_BUNDLE: b"broken"},
                        {SYSTEM_BUNDLE: b"".join(self.certs),
                         "app/vendor/cacert.pem": b"-----BEGIN CERTIFICATE-----\nbroken\n"}):
            with self.subTest(paths=list(members)), self.assertRaises(ValueError):
                self.scan(members)

    def test_system_bundle_alias_is_resolved_but_not_silently_dropped(self):
        report = self.scan({
            SYSTEM_BUNDLE: "vendor-bundle.pem",
            "etc/ssl/certs/vendor-bundle.pem": b"".join(self.certs),
        })
        self.assertEqual(len(report["denied"]), 1)
        with self.assertRaises(ValueError):
            self.scan({SYSTEM_BUNDLE: "missing.pem"})

    def test_invalid_cert_in_source_fails_closed(self):
        with self.assertRaises(ValueError):
            self.scan({
                SYSTEM_BUNDLE: b"".join(self.certs),
                "usr/share/ca-certificates/mozilla/broken.crt": b"invalid",
            })


class MediaRegistryPemTests(unittest.TestCase):
    def test_selected_vendor_platform_manifests_have_clear_public_pem_stores(self):
        if not REGISTRY:
            self.skipTest("Fresh registry scan requires explicit --registry")
        evidence = json.loads(EVIDENCE.read_text())["applications"]
        for app in ("radarr", "sonarr"):
            with self.subTest(app=app):
                image = evidence[app]["selected_amd64"]
                resolved = subprocess.run(
                    [CRANE, "--platform=linux/amd64", "digest", evidence[app]["gitops_image"]],
                    capture_output=True, text=True, check=True, timeout=120,
                ).stdout.strip()
                self.assertEqual(image.rsplit("@", 1)[1], resolved)
                config = json.loads(subprocess.run(
                    [CRANE, "config", image], capture_output=True, text=True,
                    check=True, timeout=120,
                ).stdout)
                self.assertEqual((config["os"], config["architecture"]), ("linux", "amd64"))
                self.assertEqual(
                    config["config"]["Labels"]["org.opencontainers.image.source"],
                    "https://github.com/linuxserver/docker-" + app,
                )
                error_file = ROOT / ("media-ca-registry-" + uuid.uuid4().hex + ".log")
                try:
                    with error_file.open("w+b") as errors:
                        process = subprocess.Popen(
                            [CRANE, "--platform=linux/amd64", "export", image, "-"],
                            stdout=subprocess.PIPE, stderr=errors,
                        )
                        try:
                            report = inspect_archive(process.stdout, DENIED)
                        finally:
                            while process.stdout.read(1024 * 1024):
                                pass
                            process.stdout.close()
                            code = process.wait(timeout=300)
                        errors.seek(0)
                        self.assertEqual(code, 0, errors.read().decode(errors="replace"))
                    self.assertEqual(report["denied"], [])
                    print(f"\n{app} {image}: {len(report['stores'])} public PEM paths clear; runtime not certified")
                finally:
                    error_file.unlink(missing_ok=True)


if __name__ == "__main__":
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--registry", action="store_true")
    parser.add_argument("--crane", default="crane")
    options, remaining = parser.parse_known_args()
    REGISTRY, CRANE = options.registry, options.crane
    unittest.main(argv=[sys.argv[0], *remaining])
