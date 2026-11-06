"""Bookshelf's stock CLR default-root proof, not daemon/custom TLS or runtime-security clearance."""

import argparse
import datetime
import hashlib
import importlib.util
import json
import os
from pathlib import Path
import subprocess
import sys
import tempfile
import unittest
import uuid

from cryptography import x509
from cryptography.hazmat.primitives import serialization


ROOT = Path(__file__).resolve().parents[2]
SOURCE = Path(__file__).with_name("native-dotnet-ca")
spec = importlib.util.spec_from_file_location(
    "native_acceptance", Path(__file__).with_name("test-native-app-ca-hardening.py")
)
native = importlib.util.module_from_spec(spec)
spec.loader.exec_module(native)
COMPILER = "mcr.microsoft.com/dotnet/sdk@sha256:b4679f3a872201a5db24156f71f729f9d14b3444aa6e449bc859a610b7625f55"
VERIFICATION_TIME = datetime.datetime(2026, 11, 6, tzinfo=datetime.timezone.utc)
DOCKER = False
RESULTS = []


def docker(*command, **kwargs):
    try:
        return native.docker(*command, **kwargs)
    except subprocess.CalledProcessError as error:
        def output(value):
            return value.decode("utf-8", errors="replace") if isinstance(value, bytes) else value or ""
        raise AssertionError(
            "Isolated public fixture failed:\n" + output(error.stdout)[-8000:] +
            "\n" + output(error.stderr)[-8000:]
        ) from error


def public_cases(bundle, denied, destination):
    rows = {}
    for certificate in x509.load_pem_x509_certificates(bundle):
        if certificate.subject != certificate.issuer:
            continue
        if not certificate.extensions.get_extension_for_class(x509.BasicConstraints).value.ca:
            continue
        key = hashlib.sha256(certificate.public_key().public_bytes(
            serialization.Encoding.DER, serialization.PublicFormat.SubjectPublicKeyInfo,
        )).hexdigest().upper()
        der = certificate.public_bytes(serialization.Encoding.DER)
        fingerprint = hashlib.sha256(der).hexdigest().upper()
        if not certificate.not_valid_before_utc <= VERIFICATION_TIME <= certificate.not_valid_after_utc:
            if key in denied:
                raise ValueError("Denied public root is not valid at the fixed policy-test time")
            continue
        rows[fingerprint] = {
            "certificate_sha256": fingerprint, "spki_sha256": key, "blocked": key in denied,
        }
    bad = [row for row in rows.values() if row["blocked"]]
    good = sorted((row for row in rows.values() if not row["blocked"]),
                  key=lambda row: row["certificate_sha256"])[:10]
    if not bad or len(good) != 10:
        raise ValueError("Need real denied roots and ten retained positive controls")
    selected = sorted([*bad, *good], key=lambda row: row["certificate_sha256"])
    for certificate in x509.load_pem_x509_certificates(bundle):
        der = certificate.public_bytes(serialization.Encoding.DER)
        fingerprint = hashlib.sha256(der).hexdigest().upper()
        if any(row["certificate_sha256"] == fingerprint for row in selected):
            (destination / (fingerprint + ".der")).write_bytes(der)
    result = {"verification_time_utc": VERIFICATION_TIME.isoformat(), "cases": selected}
    (destination / "cases.json").write_text(json.dumps(result), encoding="utf-8")
    return result


class DotnetPublicStoreTests(unittest.TestCase):
    def test_public_fixtures_require_denied_roots_and_retained_controls(self):
        native.FilterTests.setUpClass()
        denied = {native.FilterTests.digests[0]}
        bundle = b"".join(native.FilterTests.pem)
        with tempfile.TemporaryDirectory() as temporary:
            destination = Path(temporary)
            cases = public_cases(bundle, denied, destination)["cases"]
            self.assertEqual(sum(row["blocked"] for row in cases), 1)
            self.assertEqual(sum(not row["blocked"] for row in cases), 10)
            self.assertEqual(len(list(destination.glob("*.der"))), 11)
            with self.assertRaises(ValueError):
                public_cases(bundle, set(), destination)
            with self.assertRaises(ValueError):
                public_cases(native.FilterTests.pem[0], denied, destination)

    def test_optional_stock_clr_factory_and_filtered_trust(self):
        if not DOCKER:
            self.skipTest("Explicit --docker required on Linux/amd64")
        self.assertEqual(docker(
            "version", "--format", "{{.Server.Os}}/{{.Server.Arch}}", text=True
        ).stdout.strip(), "linux/amd64")
        policy = json.loads((ROOT / ".github" / "security" / "emudhra-2026.json").read_text())
        self.assertEqual(native.POLICY, {row["sha256"].upper() for row in policy["blocked_spkis"]})
        declared = native.image("bookshelf")
        vendor = native.baseline("bookshelf")["immutable_amd64"]
        manifest = json.loads(docker("manifest", "inspect", declared, text=True).stdout)
        children = [row["digest"] for row in manifest.get("manifests", []) if
                    row.get("platform", {}).get("os") == "linux" and
                    row.get("platform", {}).get("architecture") == "amd64"]
        self.assertEqual(children, [vendor.split("@")[1]])
        for ref in (COMPILER, vendor, native.image("bookshelf", "ca-filter")):
            docker("pull", "--platform", "linux/amd64", ref, timeout=300)
        volume = "dotnet-public-ca-" + uuid.uuid4().hex
        docker("volume", "create", volume)
        isolated = [
            "--network", "none", "--read-only", "--cap-drop", "ALL",
            "--security-opt", "no-new-privileges", "--cpus", "1", "--memory", "1g",
        ]
        try:
            with tempfile.TemporaryDirectory() as temporary:
                scratch = Path(temporary)
                scratch.chmod(0o755)
                build, fixtures = scratch / "build", scratch / "fixtures"
                build.mkdir()
                fixtures.mkdir()
                docker(
                    "run", "--rm", *isolated, "--user", f"{os.getuid()}:{os.getgid()}",
                    "--tmpfs", "/tmp:rw,nosuid,nodev,size=268435456,mode=1777",
                    "--env", "DOTNET_CLI_HOME=/tmp/dotnet-home",
                    "--env", "DOTNET_CLI_TELEMETRY_OPTOUT=1",
                    "--env", "DOTNET_SKIP_FIRST_TIME_EXPERIENCE=1",
                    "--env", "DOTNET_GENERATE_ASPNET_CERTIFICATE=false",
                    "--volume", str(SOURCE) + ":/probe:ro",
                    "--volume", str(build) + ":/build",
                    "--entrypoint", "/bin/sh", COMPILER, "-ec",
                    'test "$(dotnet --version)" = 6.0.428; '
                    "dotnet restore /probe/Probe.csproj --configfile /probe/NuGet.Config "
                    "-p:BaseIntermediateOutputPath=/build/obj/; "
                    "dotnet build /probe/Probe.csproj --no-restore "
                    "-p:BaseIntermediateOutputPath=/build/obj/ -o /build/out",
                    timeout=180,
                )
                bundle = docker(
                    "run", "--rm", *isolated, "--entrypoint", "/bin/cat", vendor,
                    "/etc/ssl/certs/ca-certificates.crt",
                ).stdout
                cases = public_cases(bundle, native.POLICY, fixtures)
                probe = [
                    "run", "--rm", *isolated, "--user", "1000:1000",
                    "--tmpfs", "/tmp:rw,nosuid,nodev,size=67108864,mode=1777",
                    "--env", "HOME=/tmp/isolated-home",
                    "--volume", str(fixtures) + ":/fixtures:ro",
                    "--volume", str(build / "out" / "Readarr.dll") + ":/app/readarr/bin/Readarr.dll:ro",
                ]
                host = ["--entrypoint", "/app/readarr/bin/Readarr", vendor, "/fixtures"]
                before = json.loads(docker(*probe, *host, "factory", text=True).stdout)
                common = [
                    *isolated, "--user", "0:0", "--volume", str(native.SHARED) + ":/ca-guard:ro",
                    "--volume", volume + ":/ca-store",
                ]
                docker(
                    "run", "--rm", *common, "--entrypoint", "/bin/sh", vendor,
                    "/ca-guard/copy-ca-overlay.sh", "bookshelf",
                )
                filtered = json.loads(docker(
                    "run", "--rm", *common, "--entrypoint", "python3",
                    native.image("bookshelf", "ca-filter"), "-B", "/ca-guard/ca-overlay.py",
                    "--root", "/ca-store", "--app", "bookshelf",
                    "--policy", "/ca-guard/blocked-spkis.sha256", text=True,
                ).stdout)
                self.assertGreater(filtered["removed_certificate_copies"], 0)
                overlays = []
                for tree in native.guard.NATIVE_TREES["bookshelf"]:
                    overlays += [
                        "--mount", f"type=volume,src={volume},dst=/{tree},readonly,volume-subpath={tree}",
                    ]
                after = json.loads(docker(
                    *probe, *overlays, *host, "filtered", text=True,
                ).stdout)
                self.assertTrue(before["successful"] and after["successful"])
                docker(
                    "run", "--rm", *isolated, *overlays, "--entrypoint", "/bin/sh", vendor,
                    "-c", native.readonly_probe("bookshelf"),
                )
                RESULTS.append({
                    "declared_image": declared, "immutable_amd64": vendor,
                    "compiler_image": COMPILER, "compiler_version": "6.0.428",
                    "production_changes": False, "vendor_native_runtime_replaced": False,
                    "managed_main_replaced_only_in_disposable_fixture": True,
                    "public_store": filtered, "factory": before, "filtered": after,
                    "denied_root_spkis_tested": sorted({
                        row["spki_sha256"] for row in cases["cases"] if row["blocked"]
                    }),
                    "coverage_limit": "Standard system-root X509Chain anchor decisions only; fixed "
                                      "policy-test time, revocation/downloads disabled, empty disposable HOME. "
                                      "Not TLS handshakes, eight-intermediate denial, actual daemon/custom "
                                      "contexts, user stores, supported-runtime or comprehensive security clearance.",
                })
        finally:
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
