"""Actual static qBittorrent HTTPS fixture; synthetic-key mechanics, not live/custom TLS clearance."""

import argparse
import datetime
import hashlib
import importlib.util
import ipaddress
import json
import os
from pathlib import Path
import ssl
import subprocess
import sys
import tempfile
import unittest
import urllib.error
import urllib.request
import uuid

from cryptography import x509
from cryptography.hazmat.primitives import hashes, serialization
from cryptography.hazmat.primitives.asymmetric import ec
from cryptography.x509.oid import ExtendedKeyUsageOID, NameOID


ROOT = Path(__file__).resolve().parents[2]
SOURCE = Path(__file__).parent


def load(name, path):
    spec = importlib.util.spec_from_file_location(name, path)
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


native = load("native_acceptance", SOURCE / "test-native-app-ca-hardening.py")
fixture = load("qt_fixture", SOURCE / "qt-ca-fixture.py")
DOCKER = False
RESULTS = []


def docker(*command, **kwargs):
    try:
        return native.docker(*command, **kwargs)
    except subprocess.CalledProcessError as error:
        def output(value):
            return value.decode("utf-8", errors="replace") if isinstance(value, bytes) else value or ""
        raise AssertionError("Isolated public fixture failed:\n" + output(error.stdout)[-5000:] +
                             "\n" + output(error.stderr)[-5000:]) from error


def certificates(public, private):
    now = datetime.datetime.now(datetime.timezone.utc)
    keys = {}
    for label in ("good", "bad"):
        authority_key = ec.generate_private_key(ec.SECP256R1())
        authority_name = x509.Name([x509.NameAttribute(NameOID.COMMON_NAME, "qt-fixture-" + label)])
        authority = (
            x509.CertificateBuilder().subject_name(authority_name).issuer_name(authority_name)
            .public_key(authority_key.public_key()).serial_number(x509.random_serial_number())
            .not_valid_before(now - datetime.timedelta(days=1)).not_valid_after(now + datetime.timedelta(days=2))
            .add_extension(x509.BasicConstraints(ca=True, path_length=0), critical=True)
            .add_extension(x509.KeyUsage(False, False, False, False, False, True, True, False, False), critical=True)
            .add_extension(x509.SubjectKeyIdentifier.from_public_key(authority_key.public_key()), critical=False)
            .sign(authority_key, hashes.SHA256())
        )
        server_key = ec.generate_private_key(ec.SECP256R1())
        server = (
            x509.CertificateBuilder().subject_name(x509.Name([x509.NameAttribute(NameOID.COMMON_NAME, "localhost")]))
            .issuer_name(authority_name).public_key(server_key.public_key())
            .serial_number(x509.random_serial_number())
            .not_valid_before(now - datetime.timedelta(hours=1)).not_valid_after(now + datetime.timedelta(days=1))
            .add_extension(x509.BasicConstraints(ca=False, path_length=None), critical=True)
            .add_extension(x509.KeyUsage(True, False, False, False, False, False, False, False, False), critical=True)
            .add_extension(x509.AuthorityKeyIdentifier.from_issuer_public_key(authority_key.public_key()), critical=False)
            .add_extension(x509.SubjectAlternativeName([
                x509.DNSName("localhost"), x509.IPAddress(ipaddress.ip_address("127.0.0.1")),
            ]), critical=False)
            .add_extension(x509.ExtendedKeyUsage([ExtendedKeyUsageOID.SERVER_AUTH]), critical=False)
            .sign(authority_key, hashes.SHA256())
        )
        (public / (label + "-ca.pem")).write_bytes(authority.public_bytes(serialization.Encoding.PEM))
        (private / (label + "-server.pem")).write_bytes(
            server.public_bytes(serialization.Encoding.PEM) + authority.public_bytes(serialization.Encoding.PEM)
        )
        secret = private / (label + "-server.key")
        secret.write_bytes(server_key.private_bytes(
            serialization.Encoding.PEM, serialization.PrivateFormat.PKCS8, serialization.NoEncryption(),
        ))
        secret.chmod(0o600)
        keys[label] = hashlib.sha256(authority_key.public_key().public_bytes(
            serialization.Encoding.DER, serialization.PublicFormat.SubjectPublicKeyInfo,
        )).hexdigest().upper()
    return keys


class QtPublicStoreTests(unittest.TestCase):
    def test_fixture_distinguishes_trust_failure_from_network_failure(self):
        with tempfile.TemporaryDirectory() as temporary:
            directory = Path(temporary)
            certificates(directory, directory)
            good = fixture.Endpoint(directory, "good")
            bad = fixture.Endpoint(directory, "bad")
            try:
                context = ssl.create_default_context(cafile=str(directory / "good-ca.pem"))
                opener = urllib.request.build_opener(
                    urllib.request.ProxyHandler({}), urllib.request.HTTPSHandler(context=context),
                )
                with opener.open(f"https://127.0.0.1:{good.server_port}/", timeout=5) as response:
                    self.assertEqual(response.status, 200)
                with self.assertRaises(urllib.error.URLError) as error:
                    opener.open(f"https://127.0.0.1:{bad.server_port}/", timeout=5)
                self.assertIsInstance(error.exception.reason, ssl.SSLCertVerificationError)
                self.assertIn(error.exception.reason.verify_code, (19, 20, 21))
                self.assertEqual(good.snapshot()["http_requests"], 1)
                self.assertEqual(bad.snapshot()["http_requests"], 0)
            finally:
                good.close()
                bad.close()

    def test_optional_unchanged_static_daemon_https_context(self):
        if not DOCKER:
            self.skipTest("Explicit --docker required on Linux/amd64")
        self.assertEqual(docker("version", "--format", "{{.Server.Os}}/{{.Server.Arch}}", text=True).stdout.strip(),
                         "linux/amd64")
        canonical = json.loads((ROOT / ".github" / "security" / "emudhra-2026.json").read_text())
        self.assertEqual(native.POLICY, {row["sha256"].upper() for row in canonical["blocked_spkis"]})
        declared = native.image("qbittorrent").replace("lscr.io/", "ghcr.io/")
        vendor = native.baseline("qbittorrent")["immutable_amd64"].replace("lscr.io/", "ghcr.io/")
        manifest = json.loads(docker("manifest", "inspect", declared, text=True).stdout)
        self.assertEqual([row["digest"] for row in manifest["manifests"] if
                          row.get("platform", {}).get("os") == "linux" and
                          row.get("platform", {}).get("architecture") == "amd64"], [vendor.split("@")[1]])
        helper = native.image("qbittorrent", "ca-filter").replace("lscr.io/", "ghcr.io/")
        for image in (vendor, helper):
            docker("pull", "--platform", "linux/amd64", image, timeout=300)
        token = uuid.uuid4().hex
        volume, name = "qt-ca-" + token, "qt-ca-app-" + token
        docker("volume", "create", volume)
        isolated = ["--network", "none", "--read-only", "--cap-drop", "ALL",
                    "--security-opt", "no-new-privileges", "--cpus", "1", "--memory", "512m"]
        try:
            with tempfile.TemporaryDirectory() as temporary:
                scratch = Path(temporary)
                scratch.chmod(0o755)
                public, private = scratch / "public", scratch / "private"
                public.mkdir()
                private.mkdir()
                private.chmod(0o700)
                keys = certificates(public, private)
                (public / "blocked-spkis.sha256").write_text(
                    "\n".join(sorted(native.POLICY | {keys["bad"]})) + "\n", encoding="ascii",
                )
                common = [
                    *isolated, "--user", "0:0", "--volume", str(native.SHARED) + ":/ca-guard:ro",
                    "--volume", str(SOURCE) + ":/probe:ro",
                    "--volume", str(public) + ":/public:ro", "--volume", volume + ":/ca-store",
                ]
                docker("run", "--rm", *common, "--entrypoint", "/bin/sh", vendor,
                       "/ca-guard/copy-ca-overlay.sh", "qbittorrent")
                binary_hash = docker("run", "--rm", *isolated, "--entrypoint", "sha256sum",
                                     vendor, "/app/qbittorrent-nox", text=True).stdout.split()[0]
                docker("run", "--rm", *common, "--entrypoint", "python3", helper,
                       "-B", "/probe/qt-ca-fixture.py", "prepare", "/public")
                overlays = []
                for tree in native.guard.NATIVE_TREES["qbittorrent"]:
                    overlays += ["--mount", f"type=volume,src={volume},dst=/{tree},readonly,volume-subpath={tree}"]
                modes = {}
                for mode in ("factory-plus-fixtures", "filtered"):
                    if mode == "filtered":
                        filtered = json.loads(docker(
                            "run", "--rm", *common, "--entrypoint", "python3", helper,
                            "-B", "/ca-guard/ca-overlay.py", "--root", "/ca-store", "--app", "qbittorrent",
                            "--policy", "/public/blocked-spkis.sha256", text=True,
                        ).stdout)
                        self.assertGreater(filtered["removed_certificate_copies"], 0)
                    docker(
                        "run", "--detach", "--name", name, *isolated, "--user", "1000:1000",
                        "--tmpfs", "/config:rw,nosuid,nodev,size=134217728,uid=1000,gid=1000,mode=0770",
                        "--tmpfs", "/tmp:rw,nosuid,nodev,size=33554432,mode=1777",
                        "--env", "HOME=/config", "--env", "XDG_CONFIG_HOME=/config",
                        "--env", "XDG_DATA_HOME=/config/data", "--env", "XDG_CACHE_HOME=/config/cache",
                        "--env", "LC_ALL=C",
                        "--volume", str(SOURCE) + ":/probe:ro", *overlays,
                        "--entrypoint", "/bin/sh", vendor, "-ec",
                        "mkdir -p /config/qBittorrent; "
                        "cp /probe/qt-ca-fixture.conf /config/qBittorrent/qBittorrent.conf; "
                        "exec /app/qbittorrent-nox --webui-port=8080 --confirm-legal-notice",
                    )
                    try:
                        modes[mode] = json.loads(docker(
                            "run", "--rm", "--network", "container:" + name, "--read-only",
                            "--cap-drop", "ALL", "--security-opt", "no-new-privileges",
                            "--cpus", "0.5", "--memory", "256m",
                            "--user", f"{os.getuid()}:{os.getgid()}",
                            "--volume", str(SOURCE) + ":/probe:ro",
                            "--volume", str(private) + ":/servers:ro",
                            "--entrypoint", "python3", helper,
                            "-B", "/probe/qt-ca-fixture.py", mode, "/servers", text=True, timeout=120,
                        ).stdout)
                        if mode == "filtered":
                            docker("exec", "--user", "0", name, "/bin/sh", "-c",
                                   native.readonly_probe("qbittorrent"))
                    finally:
                        docker("rm", "--force", name)
                self.assertEqual(modes["factory-plus-fixtures"]["build_info"], modes["filtered"]["build_info"])
                RESULTS.append({
                    "immutable_amd64": vendor, "binary_sha256": binary_hash,
                    "unmodified_static_vendor_binary": True, "production_changes": False,
                    "default_public_filter": filtered, "modes": modes,
                    "synthetic_denied_spki": keys["bad"],
                    "real_policy_keys_preserved": len(native.POLICY),
                    "scope": "Actual daemon Qt HTTPS URL-download context under a disposable profile. "
                             "Explicit synthetic denied-key extension exercises filtering, not possession "
                             "of real denied private keys or native eleven-key/intermediate denial. "
                             "Not live profiles, trackers/libtorrent, VPN, RSS/search/plugins, custom contexts, "
                             "production authentication or comprehensive security clearance.",
                })
        finally:
            subprocess.run(["docker", "rm", "--force", name], capture_output=True, timeout=30)
            docker("volume", "rm", volume)


if __name__ == "__main__":
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--docker", action="store_true")
    parser.add_argument("--evidence", type=Path)
    options, remaining = parser.parse_known_args()
    DOCKER = options.docker
    program = unittest.main(argv=[sys.argv[0], *remaining], exit=False)
    if options.evidence:
        destination = options.evidence.absolute()
        if not destination.is_relative_to(ROOT):
            raise ValueError("Evidence must remain inside this worktree")
        destination.write_text(json.dumps({
            "successful": program.result.wasSuccessful(), "tests_run": program.result.testsRun,
            "skipped": len(program.result.skipped), "requested_docker": DOCKER,
            "results": RESULTS, "rollout_clearance": False,
        }, indent=2), encoding="utf-8")
    sys.exit(not program.result.wasSuccessful())
