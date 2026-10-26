"""Media trust-overlay tests. --registry reads public images; --docker requires isolated Linux Docker."""

import argparse
import datetime
import hashlib
import importlib.util
import json
from pathlib import Path
import shutil
import ssl
import subprocess
import sys
import tarfile
import time
import unittest
import uuid

from cryptography import x509
from cryptography.hazmat.primitives import hashes, serialization
from cryptography.hazmat.primitives.asymmetric import ec, rsa
from cryptography.x509.oid import NameOID
import yaml


ROOT = Path(__file__).resolve().parents[2]
APPS = ("bazarr", "prowlarr", "sabnzbd")
REGISTRY = DOCKER = False
CRANE = "crane"
spec = importlib.util.spec_from_file_location("media_overlay", ROOT / "apps" / "bazarr" / "ca-overlay.py")
guard = importlib.util.module_from_spec(spec)
spec.loader.exec_module(guard)
CANONICAL = {
    entry["sha256"].upper()
    for entry in json.loads((ROOT / ".github" / "security" / "emudhra-2026.json").read_text())["blocked_spkis"]
}


def values(app):
    return yaml.safe_load((ROOT / "apps" / app / "helmrelease.yaml").read_text())["spec"]["values"]


def image(app, container="app"):
    controller = values(app)["controllers"][app]
    entry = controller["containers"][container] if container == "app" else controller["initContainers"][container]
    ref = entry["image"]
    return ref["repository"] + ":" + ref["tag"]


class MediaOverlayWiringTests(unittest.TestCase):
    def test_preparation_metadata_has_portable_policy_and_exact_vendor_image_bindings(self):
        evidence = json.loads((ROOT / ".github" / "security" / "media-runtime-ca-hardening-20261026.json").read_text())
        policy = (ROOT / ".github" / "security" / "emudhra-2026.json").read_text(encoding="utf-8")
        self.assertEqual(hashlib.sha256(policy.encode("utf-8")).hexdigest(),
                         evidence["policy_binding"]["canonical_utf8_lf_sha256"])
        self.assertFalse(evidence["deployment_approved_by_this_report"])
        for app in APPS:
            ref = values(app)["controllers"][app]["containers"]["app"]["image"]
            self.assertEqual(ref["repository"] + "@" + ref["tag"].split("@", 1)[1],
                             evidence["immutable_platform_evidence"][app]["index"])

    def test_every_app_has_identical_guard_and_exact_eleven_policy_keys(self):
        for app in APPS:
            directory = ROOT / "apps" / app
            self.assertEqual(guard.policy_keys(directory / "blocked-spkis.sha256"), CANONICAL)
            for name in ("ca-overlay.py", "copy-ca-overlay.sh"):
                self.assertEqual((directory / name).read_text(), (ROOT / "apps" / "bazarr" / name).read_text())

    def test_kubernetes_init_failure_blocks_unmodified_original_application(self):
        for app in APPS:
            v = values(app)
            controller = v["controllers"][app]
            main = controller["containers"]["app"]
            self.assertNotIn("command", main)
            self.assertNotIn("args", main)
            init = controller["initContainers"]
            self.assertEqual(init["ca-copy"]["command"], ["/bin/sh", "/ca-guard/copy-ca-overlay.sh", app])
            self.assertEqual(init["ca-copy"]["image"], main["image"])
            self.assertEqual(init["ca-filter"]["command"], ["python3", "-B", "/ca-guard/ca-overlay.py"])
            self.assertEqual(init["ca-filter"]["args"], [
                "--root", "/ca-store", "--app", app, "--policy", "/ca-guard/blocked-spkis.sha256",
            ])
            for name in ("ca-copy", "ca-filter"):
                self.assertNotIn("restartPolicy", init[name])
                self.assertTrue(init[name]["securityContext"]["readOnlyRootFilesystem"])
                for volume in ("config", "media"):
                    entry = v["persistence"].get(volume, {})
                    self.assertNotIn("globalMounts", entry)
                    self.assertNotIn(name, entry.get("advancedMounts", {}).get(app, {}))
            self.assertEqual(image(app, "ca-filter"), image("bazarr") if app == "prowlarr" else image(app))

    def test_all_system_trees_and_entire_certifi_packages_are_read_only(self):
        for app in APPS:
            persistence = values(app)["persistence"]
            store = persistence["ca-store"]
            self.assertEqual(store["type"], "emptyDir")
            mounts = store["advancedMounts"][app]["app"]
            expected = set(guard.TREES + guard.PACKAGES[app])
            self.assertEqual({m["subPath"] for m in mounts}, expected)
            for mount in mounts:
                self.assertTrue(mount["readOnly"])
                self.assertEqual(mount["path"], "/" + mount["subPath"])
                self.assertFalse(mount["path"].endswith("cacert.pem"))
            if app == "sabnzbd":
                self.assertEqual(store["advancedMounts"][app]["codec-config"], mounts)

    def test_hashed_configmap_name_is_wired_into_helm_persistence(self):
        for app in APPS:
            directory = ROOT / "apps" / app
            config = yaml.safe_load((directory / "kustomization.yaml").read_text())
            generator = next(g for g in config["configMapGenerator"] if g["name"] == app + "-ca-guard")
            self.assertCountEqual(generator["files"], ["ca-overlay.py", "copy-ca-overlay.sh", "blocked-spkis.sha256"])
            self.assertFalse(config.get("generatorOptions", {}).get("disableNameSuffixHash", False))
            reference = json.loads((directory / "kustomize-ca.json").read_text())
            self.assertEqual(reference["nameReference"][0]["fieldSpecs"][0]["path"],
                             "spec/values/persistence/ca-guard/name")
            self.assertEqual(values(app)["persistence"]["ca-guard"]["name"], app + "-ca-guard")


class MediaOverlayFilterTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.keys, cls.pem, cls.spki = [], [], []
        for index in range(11):
            key = rsa.generate_private_key(public_exponent=65537, key_size=2048) if index == 0 else ec.generate_private_key(ec.SECP256R1())
            subject = x509.Name([x509.NameAttribute(NameOID.COMMON_NAME, "Same fixture subject")])
            cert = (
                x509.CertificateBuilder().subject_name(subject).issuer_name(subject)
                .public_key(key.public_key()).serial_number(x509.random_serial_number())
                .not_valid_before(datetime.datetime(2025, 1, 1, tzinfo=datetime.timezone.utc))
                .not_valid_after(datetime.datetime(2035, 1, 1, tzinfo=datetime.timezone.utc))
                .add_extension(x509.BasicConstraints(ca=True, path_length=None), critical=True)
                .add_extension(x509.SubjectKeyIdentifier.from_public_key(key.public_key()), critical=False)
                .add_extension(x509.AuthorityKeyIdentifier.from_issuer_public_key(key.public_key()), critical=False)
                .add_extension(x509.SubjectAlternativeName([x509.DNSName("localhost")]), critical=False)
                .sign(key, hashes.SHA256())
            )
            cls.keys.append(key)
            cls.pem.append(cert.public_bytes(serialization.Encoding.PEM))
            cls.spki.append(hashlib.sha256(key.public_key().public_bytes(
                serialization.Encoding.DER, serialization.PublicFormat.SubjectPublicKeyInfo,
            )).hexdigest().upper())

    def setUp(self):
        self.base = ROOT / (".media-ca-test-" + uuid.uuid4().hex)
        self.addCleanup(shutil.rmtree, self.base)
        self.root = self.base / "snapshot"
        for directory in guard.TREES + guard.PACKAGES["bazarr"] + guard.PACKAGES["sabnzbd"]:
            (self.root / directory).mkdir(parents=True, exist_ok=True)
        self.bundle = self.root / "etc" / "ssl" / "certs" / "ca-certificates.crt"
        self.bundle.write_bytes(b"".join(self.pem))
        for app in ("bazarr", "sabnzbd"):
            for directory in guard.PACKAGES[app]:
                (self.root / directory / "cacert.pem").write_bytes(b"".join(self.pem))
        self.sources = self.root / "usr" / "share" / "ca-certificates"
        for index, cert in enumerate(self.pem):
            (self.sources / f"root-{index}.crt").write_bytes(cert)
        self.denied = {self.spki[0]}

    def test_stdlib_spki_der_parser_matches_independent_crypto_for_rsa_and_ec(self):
        for pem, expected in zip(self.pem, self.spki, strict=True):
            der = x509.load_pem_x509_certificate(pem).public_bytes(serialization.Encoding.DER)
            self.assertEqual(guard.spki_digest(der), expected)
            self.assertEqual(guard.certificates(pem)[0][1], expected)
        for broken in (b"", b"\x30\x80", b"\x30\x01\x00", b"\x30\x81\x01\x00"):
            with self.assertRaises((ValueError, IndexError)):
                guard.spki_digest(broken)

    def test_removes_exact_key_from_os_sources_bundle_and_application_stores(self):
        for app in ("bazarr", "sabnzbd"):
            result = guard.harden(self.root, app, self.denied)
            self.assertIn("not certified", result["coverage"])
            self.assertFalse((self.sources / "root-0.crt").exists())
            for path in [self.bundle] + [self.root / d / "cacert.pem" for d in guard.PACKAGES[app]]:
                certs = guard.certificates(path.read_bytes())
                self.assertEqual({key for _, key, _ in certs}, set(self.spki[1:]))
            guard.harden(self.root, app, self.denied, verify_only=True)
        for index in range(1, 11):
            self.assertEqual((self.sources / f"root-{index}.crt").read_bytes(), self.pem[index])

    def test_hash_collision_sequence_is_compacted_without_losing_an_allowed_ca(self):
        directory = self.bundle.parent
        (directory / "deadbeef.0").write_bytes(self.pem[0])
        (directory / "deadbeef.1").write_bytes(self.pem[1])
        guard.harden(self.root, "prowlarr", self.denied)
        self.assertEqual((directory / "deadbeef.0").read_bytes(), self.pem[1])
        self.assertFalse((directory / "deadbeef.1").exists())

    @unittest.skipUnless(sys.platform.startswith("linux"), "Linux absolute CA-directory aliases")
    def test_absolute_hash_aliases_are_removed_and_compacted_inside_snapshot(self):
        directory = self.bundle.parent
        (directory / "deadbeef.0").symlink_to("/usr/share/ca-certificates/root-0.crt")
        (directory / "deadbeef.1").symlink_to("/usr/share/ca-certificates/root-1.crt")
        guard.harden(self.root, "prowlarr", self.denied)
        self.assertEqual((directory / "deadbeef.0").readlink().as_posix(), "/usr/share/ca-certificates/root-1.crt")
        self.assertFalse((directory / "deadbeef.1").is_symlink())
        outside = self.base / "outside.crt"
        outside.write_bytes(self.pem[0])
        (self.sources / "escape.crt").symlink_to(outside)
        with self.assertRaises(ValueError):
            guard.harden(self.root, "prowlarr", self.denied)
        self.assertEqual(outside.read_bytes(), self.pem[0])

    def test_bad_policy_missing_malformed_or_partial_bundles_fail_closed(self):
        policy = self.base / "policy"
        for content in ("", "bad\n", self.spki[0] + "\n", (self.spki[0] + "\n") * 11):
            policy.write_text(content)
            with self.assertRaises(ValueError):
                guard.policy_keys(policy)
        policy.write_bytes(("\r\n".join([self.spki[0]] + [f"{n:064X}" for n in range(10)]) + "\r\n").encode())
        self.assertEqual(len(guard.policy_keys(policy)), 11)
        for content in (b"", self.pem[0], b"-----BEGIN CERTIFICATE-----\nbroken\n", self.pem[0] + b"-----BEGIN PRIVATE KEY-----"):
            self.bundle.write_bytes(content)
            with self.assertRaises((ValueError, ssl.SSLError)):
                guard.harden(self.root, "prowlarr", self.denied)
        self.bundle.unlink()
        with self.assertRaises(ValueError):
            guard.harden(self.root, "prowlarr", self.denied)

    def test_missing_application_store_and_cli_policy_failure_cannot_report_success(self):
        (self.root / guard.PACKAGES["bazarr"][0] / "cacert.pem").unlink()
        with self.assertRaises(ValueError):
            guard.harden(self.root, "bazarr", self.denied)
        policy = self.base / "invalid-policy"
        policy.write_text("invalid\n")
        original = self.bundle.read_bytes()
        result = subprocess.run([
            sys.executable, "-B", str(ROOT / "apps" / "bazarr" / "ca-overlay.py"),
            "--root", str(self.root), "--app", "prowlarr", "--policy", str(policy),
        ], capture_output=True, text=True, timeout=30)
        self.assertNotEqual(result.returncode, 0)
        self.assertEqual(result.stdout, "")
        self.assertEqual(self.bundle.read_bytes(), original)

    def handshake(self, index, bundle):
        cert, key = self.base / "tls-cert.pem", self.base / "tls-key.pem"
        cert.write_bytes(self.pem[index])
        key.write_bytes(self.keys[index].private_bytes(
            serialization.Encoding.PEM, serialization.PrivateFormat.PKCS8, serialization.NoEncryption(),
        ))
        server_context = ssl.SSLContext(ssl.PROTOCOL_TLS_SERVER)
        server_context.load_cert_chain(cert, key)
        client_context = ssl.SSLContext(ssl.PROTOCOL_TLS_CLIENT)
        client_context.load_verify_locations(cafile=str(bundle))
        cin, cout, sin, sout = (ssl.MemoryBIO() for _ in range(4))
        client = client_context.wrap_bio(cin, cout, server_hostname="localhost")
        server = server_context.wrap_bio(sin, sout, server_side=True)
        done = set()
        for _ in range(20):
            for name, endpoint in (("client", client), ("server", server)):
                try:
                    endpoint.do_handshake()
                    done.add(name)
                except ssl.SSLWantReadError:
                    pass
            if cout.pending:
                sin.write(cout.read())
            if sout.pending:
                cin.write(sout.read())
            if len(done) == 2:
                return
        self.fail("TLS memory-BIO handshake failed to converge")

    def test_native_python_tls_positive_control_survives_and_denied_fixture_rejects(self):
        self.handshake(0, self.bundle)
        guard.harden(self.root, "prowlarr", self.denied)
        self.handshake(1, self.bundle)
        with self.assertRaises(ssl.SSLCertVerificationError):
            self.handshake(0, self.bundle)


class MediaRealVendorTests(unittest.TestCase):
    def test_registry_public_certificates_match_independent_spki_hashes(self):
        if not REGISTRY:
            self.skipTest("Explicit --registry required; no image execution is implied")
        for app in APPS:
            with self.subTest(app=app):
                log = ROOT / ("media-ca-export-" + uuid.uuid4().hex + ".log")
                observed, matches = set(), 0
                try:
                    with log.open("w+b") as errors:
                        process = subprocess.Popen([CRANE, "--platform=linux/amd64", "export", image(app), "-"],
                                                   stdout=subprocess.PIPE, stderr=errors)
                        try:
                            with tarfile.open(fileobj=process.stdout, mode="r|") as archive:
                                for member in archive:
                                    name = member.name.removeprefix("./")
                                    if not member.isfile() or not (
                                        name.endswith("/cacert.pem")
                                        or name == "etc/ssl/certs/ca-certificates.crt"
                                        or name.startswith(("usr/share/ca-certificates/", "usr/local/share/ca-certificates/"))
                                    ):
                                        continue
                                    data = archive.extractfile(member).read()
                                    certs = guard.certificates(data)
                                    independent = x509.load_pem_x509_certificates(data)
                                    self.assertEqual(len(certs), len(independent))
                                    for (_, key, _), cert in zip(certs, independent, strict=True):
                                        expected = hashlib.sha256(cert.public_key().public_bytes(
                                            serialization.Encoding.DER, serialization.PublicFormat.SubjectPublicKeyInfo,
                                        )).hexdigest().upper()
                                        self.assertEqual(key, expected)
                                        matches += key in CANONICAL
                                    observed.add(name)
                        finally:
                            while process.stdout.read(1024 * 1024):
                                pass
                            process.stdout.close()
                            code = process.wait(timeout=300)
                        errors.seek(0)
                        self.assertEqual(code, 0, errors.read().decode(errors="replace"))
                    self.assertGreater(matches, 0, "Historical vendor stores must not be relabeled as deployed clear")
                    for package in guard.PACKAGES[app]:
                        self.assertIn(package + "/cacert.pem", observed)
                    print(f"\n{app}: {len(observed)} public vendor stores parsed; {matches} baseline denied copies; no image execution")
                finally:
                    log.unlink(missing_ok=True)

    def test_real_linux_docker_init_and_readonly_overlay(self):
        if not DOCKER:
            self.skipTest("Explicit --docker required; real Linux/Docker validation remains outstanding")
        server = subprocess.run(["docker", "version", "--format", "{{.Server.Os}}/{{.Server.Arch}}"],
                                check=True, capture_output=True, text=True).stdout.strip()
        self.assertEqual(server, "linux/amd64")
        for app in APPS:
            with self.subTest(app=app):
                token = uuid.uuid4().hex[:12]
                volume, name = "media-ca-" + token, "media-ca-app-" + token
                directory = ROOT / "apps" / app
                subprocess.run(["docker", "pull", image(app)], check=True, capture_output=True, timeout=300)
                subprocess.run(["docker", "pull", image(app, "ca-filter")], check=True, capture_output=True, timeout=300)
                subprocess.run(["docker", "volume", "create", volume], check=True, capture_output=True)
                try:
                    mount = ["--volume", str(directory) + ":/ca-guard:ro", "--volume", volume + ":/ca-store"]
                    subprocess.run([
                        "docker", "run", "--rm", "--network", "none", "--read-only",
                        "--cap-drop", "ALL", "--security-opt", "no-new-privileges", *mount,
                        "--entrypoint", "/bin/sh", image(app), "/ca-guard/copy-ca-overlay.sh", app,
                    ], check=True, capture_output=True, timeout=90)
                    filtered = subprocess.run([
                        "docker", "run", "--rm", "--network", "none", "--read-only",
                        "--cap-drop", "ALL", "--security-opt", "no-new-privileges", *mount,
                        "--entrypoint", "python3", image(app, "ca-filter"),
                        "-B", "/ca-guard/ca-overlay.py", "--root", "/ca-store", "--app", app,
                        "--policy", "/ca-guard/blocked-spkis.sha256",
                    ], check=True, capture_output=True, text=True, timeout=90)
                    self.assertGreater(json.loads(filtered.stdout)["removed_certificate_copies"], 0)
                    command = [
                        "docker", "run", "--detach", "--name", name, "--network", "none",
                        "--security-opt", "no-new-privileges",
                        "--tmpfs", "/config:rw,nosuid,nodev,size=67108864",
                        "--volume", str(directory) + ":/ca-guard:ro",
                    ]
                    for path in guard.TREES + guard.PACKAGES[app]:
                        command += ["--mount", f"type=volume,src={volume},dst=/{path},readonly,volume-subpath={path}"]
                    subprocess.run([*command, image(app)], check=True, capture_output=True, timeout=90)
                    port = {"bazarr": 6767, "prowlarr": 9696, "sabnzbd": 8080}[app]
                    ready = False
                    for _ in range(90):
                        probe = subprocess.run([
                            "docker", "exec", name, "curl", "--silent", "--max-time", "2",
                            "--output", "/dev/null", "--write-out", "%{http_code}", f"http://127.0.0.1:{port}/",
                        ], capture_output=True, text=True, timeout=10)
                        if probe.returncode == 0 and probe.stdout in ("200", "301", "302", "401", "403"):
                            ready = True
                            break
                        time.sleep(2)
                    self.assertTrue(ready, app + " original /init did not reach an HTTP/auth endpoint")
                    update = subprocess.run(["docker", "exec", name, "/usr/sbin/update-ca-certificates"],
                                            capture_output=True, text=True, timeout=30)
                    self.assertNotEqual(update.returncode, 0, "Read-only system store unexpectedly writable")
                    if app != "prowlarr":
                        native = (
                            "import runpy,ssl,sys; from pathlib import Path; "
                            "g=runpy.run_path('/ca-guard/ca-overlay.py'); "
                            "d=g['policy_keys'](Path('/ca-guard/blocked-spkis.sha256')); "
                            "contexts=[ssl.create_default_context()]; "
                            + ("sys.path.insert(0,'/app/bazarr/bin/libs'); " if app == "bazarr" else "")
                            + "import certifi; contexts.append(ssl.create_default_context(cafile=certifi.where())); "
                            "roots=[c.get_ca_certs(binary_form=True) for c in contexts]; "
                            "assert all(len(r)>=10 and all(g['spki_digest'](x) not in d for x in r) for r in roots); "
                            "print('Native Python and application certifi contexts have no denied root keys; no custom TLS claim')"
                        )
                        subprocess.run(["docker", "exec", name, "python3", "-B", "-c", native],
                                       check=True, capture_output=True, timeout=30)
                        package = "/" + guard.PACKAGES[app][0]
                        replacement = subprocess.run([
                            "docker", "exec", name, "python3", "-B", "-c",
                            "import os; os.rename(" + repr(package) + "," + repr(package + "-replaced") + ")",
                        ], capture_output=True, timeout=30)
                        self.assertNotEqual(replacement.returncode, 0, "Entire certifi package mount could be replaced")
                    subprocess.run([
                        "docker", "run", "--rm", "--network", "none", "--read-only",
                        "--cap-drop", "ALL", "--security-opt", "no-new-privileges", *mount,
                        "--entrypoint", "python3", image(app, "ca-filter"),
                        "-B", "/ca-guard/ca-overlay.py", "--root", "/ca-store", "--app", app,
                        "--policy", "/ca-guard/blocked-spkis.sha256", "--verify-only",
                    ], check=True, capture_output=True, timeout=90)
                    print(f"\n{app}: stock /init HTTP/auth ready; public overlays clear and read-only; compiled/custom TLS not certified")
                finally:
                    state = subprocess.run(["docker", "container", "ls", "--all", "--quiet", "--filter", "name=^/" + name + "$"],
                                           check=True, capture_output=True, text=True).stdout.strip()
                    if state:
                        subprocess.run(["docker", "rm", "--force", name], check=True, capture_output=True)
                    subprocess.run(["docker", "volume", "rm", volume], check=True, capture_output=True)


if __name__ == "__main__":
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--registry", action="store_true")
    parser.add_argument("--crane", default="crane")
    parser.add_argument("--docker", action="store_true")
    options, remaining = parser.parse_known_args()
    REGISTRY, CRANE, DOCKER = options.registry, options.crane, options.docker
    unittest.main(argv=[sys.argv[0], *remaining])
