"""Calibre-specific overlay tests; Linux additionally requires isolated Docker."""
from collections import Counter
import datetime
import base64
import hashlib
import importlib.util
import json
import os
from pathlib import Path
import shutil
import ssl
import subprocess
import sys
import time
import unittest
import uuid

from cryptography import x509
from cryptography.hazmat.primitives import hashes, serialization
from cryptography.hazmat.primitives.asymmetric import ec
from cryptography.x509.oid import NameOID
import yaml

sys.dont_write_bytecode = True
ROOT = Path(__file__).resolve().parents[2]
APP = ROOT / "apps/calibre-web"
RELEASE = yaml.safe_load((APP / "helmrelease.yaml").read_text())
VALUES = RELEASE["spec"]["values"]
spec = importlib.util.spec_from_file_location("calibre_certifi_guard", APP / "harden-certifi.py")
guard = importlib.util.module_from_spec(spec)
spec.loader.exec_module(guard)


class CalibreWiringTests(unittest.TestCase):
    def test_policy_matches_canonical_keys_independent_of_line_endings(self):
        canonical = json.loads((ROOT / ".github/security/emudhra-2026.json").read_text())
        self.assertEqual(guard.policy(APP / "blocked-spkis.sha256"),
                         {item["sha256"].lower() for item in canonical["blocked_spkis"]})

    def test_same_pinned_image_and_original_vendor_startup_are_preserved(self):
        controller = VALUES["controllers"]["calibre-web"]
        image = controller["containers"]["app"]["image"]
        self.assertEqual(controller["initContainers"]["harden-certifi"]["image"], image)
        self.assertEqual(controller["initContainers"]["bootstrap-library"]["image"], image)
        self.assertNotIn("command", controller["containers"]["app"])
        self.assertNotIn("args", controller["containers"]["app"])
        self.assertEqual(image["tag"],
                         "0.6.27@sha256:470b4ed9a7d6ea1475fef17dbcf957cb60880910296d226bd0dca652c05f68ca")
        self.assertEqual(controller["initContainers"]["harden-certifi"]["command"],
                         ["python3", "-B", "/ca-hardening/harden-certifi.py"])
        self.assertTrue(controller["initContainers"]["harden-certifi"]
                        ["securityContext"]["readOnlyRootFilesystem"])

    def test_entire_package_not_just_bundle_is_mounted_read_only(self):
        overlay = VALUES["persistence"]["certifi-overlay"]
        self.assertEqual(overlay["type"], "emptyDir")
        self.assertEqual(overlay["advancedMounts"]["calibre-web"]["app"], [{
            "path": guard.PACKAGE.as_posix(), "subPath": "certifi", "readOnly": True,
        }])
        self.assertEqual(overlay["advancedMounts"]["calibre-web"]["harden-certifi"],
                         [{"path": "/ca-output"}])
        for name in ("config", "library", "legacy-library"):
            self.assertNotIn("globalMounts", VALUES["persistence"][name])
            self.assertNotIn("harden-certifi",
                             VALUES["persistence"][name]["advancedMounts"]["calibre-web"])

    def test_hashed_policy_config_changes_roll_the_template_reference(self):
        rendered = list(yaml.safe_load_all(subprocess.check_output(
            ["kubectl", "kustomize", str(APP)], text=True, timeout=60,
        )))
        release = next(doc for doc in rendered if doc["kind"] == "HelmRelease")
        config = next(doc for doc in rendered if doc["kind"] == "ConfigMap"
                      and doc["metadata"]["name"].startswith("calibre-web-ca-hardening-"))
        self.assertEqual(release["spec"]["values"]["persistence"]["ca-hardening"]["name"],
                         config["metadata"]["name"])
        self.assertEqual(set(config["data"]), {"harden-certifi.py", "blocked-spkis.sha256"})

    def test_exact_chart_preserves_bootstrap_order_and_isolates_guard_from_user_volumes(self):
        chart = os.environ.get("APPLICATION_CA_CHART", "oci://ghcr.io/bjw-s-labs/helm/app-template")
        rendered = subprocess.run(
            ["helm", "template", "calibre-web", chart, "--version", "5.2.1",
             "--namespace", "media", "--values", "-"],
            input=yaml.safe_dump(VALUES), text=True, capture_output=True, timeout=120,
        )
        self.assertEqual(rendered.returncode, 0, rendered.stderr)
        deployment = next(doc for doc in yaml.safe_load_all(rendered.stdout)
                          if doc and doc["kind"] == "Deployment")
        pod = deployment["spec"]["template"]["spec"]
        self.assertEqual([container["name"] for container in pod["initContainers"]],
                         ["bootstrap-library", "harden-certifi"])
        initializer = pod["initContainers"][1]
        self.assertEqual({mount["name"] for mount in initializer["volumeMounts"]},
                         {"ca-hardening", "certifi-overlay"})
        app = pod["containers"][0]
        self.assertNotIn("command", app)
        self.assertNotIn("args", app)
        mount = next(mount for mount in app["volumeMounts"] if mount["name"] == "certifi-overlay")
        self.assertEqual(mount, {"mountPath": guard.PACKAGE.as_posix(), "name": "certifi-overlay",
                                 "readOnly": True, "subPath": "certifi"})


class CalibreFilterTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.certs = []
        for _ in range(12):
            key = ec.generate_private_key(ec.SECP256R1())
            name = x509.Name([x509.NameAttribute(NameOID.COMMON_NAME, "Public negative fixture")])
            cert = (x509.CertificateBuilder().subject_name(name).issuer_name(name)
                    .public_key(key.public_key()).serial_number(x509.random_serial_number())
                    .not_valid_before(datetime.datetime(2025, 1, 1, tzinfo=datetime.timezone.utc))
                    .not_valid_after(datetime.datetime(2035, 1, 1, tzinfo=datetime.timezone.utc))
                    .add_extension(x509.BasicConstraints(ca=True, path_length=None), critical=True)
                    .add_extension(x509.SubjectKeyIdentifier.from_public_key(key.public_key()), critical=False)
                    .add_extension(x509.AuthorityKeyIdentifier.from_issuer_public_key(key.public_key()), critical=False)
                    .sign(key, hashes.SHA256()))
            cls.certs.append(cert.public_bytes(serialization.Encoding.PEM))
        cls.denied = {guard.certificates(cls.certs[0])[0][1]}

    def setUp(self):
        self.base = ROOT / ("fleet-application-ca-test-" + uuid.uuid4().hex)
        self.base.mkdir()
        self.addCleanup(shutil.rmtree, self.base)
        self.source = self.base / "package"
        self.source.mkdir()
        self.output = self.base / "output"
        self.output.mkdir()
        self.files = {"cacert.pem": b"".join(self.certs), "__init__.py": b"", "__main__.py": b"",
                      "core.py": b"", "py.typed": b""}
        for name, data in self.files.items():
            (self.source / name).write_bytes(data)
        self.expected = {name: hashlib.sha256(data).hexdigest() for name, data in self.files.items()}

    def test_exact_spki_removal_preserves_every_other_certificate_and_module(self):
        counts = guard.build_overlay(self.source, self.output, self.denied, self.expected)
        self.assertEqual(counts, {"before": 12, "after": 11, "removed": 1})
        output = self.output / "certifi"
        self.assertEqual(guard.PEM.findall((output / "cacert.pem").read_bytes()), [
            pem.rstrip() for pem in self.certs[1:]
        ])
        for name in ("__init__.py", "__main__.py", "core.py", "py.typed"):
            self.assertEqual((output / name).read_bytes(), self.files[name])
        native = ssl.create_default_context(cafile=str(output / "cacert.pem"))
        self.assertEqual(len(native.get_ca_certs()), 11)
        self.assertEqual(guard.build_overlay(self.source, self.output, self.denied, self.expected), counts)

    def test_policy_crlf_is_accepted_but_missing_partial_duplicate_or_bad_policy_is_not(self):
        path = self.base / "policy"
        lines = [next(iter(self.denied)).upper()] + [f"{n:064X}" for n in range(10)]
        path.write_bytes(("\r\n".join(lines) + "\r\n").encode())
        self.assertEqual(len(guard.policy(path)), 11)
        for text in ("", "bad\n", "\n".join(lines[:10]), "\n".join([lines[0]] * 11)):
            path.write_text(text)
            with self.assertRaises(ValueError):
                guard.policy(path)
        path.unlink()
        with self.assertRaises(FileNotFoundError):
            guard.policy(path)

    def test_malformed_empty_and_unexpected_package_inputs_fail_before_output(self):
        for content in (b"", b"-----BEGIN CERTIFICATE-----\nbad\n", b"-----END CERTIFICATE-----"):
            with self.assertRaises(ValueError):
                guard.sanitize(content, self.denied)
        (self.source / "core.py").write_bytes(b"changed")
        with self.assertRaises(ValueError):
            guard.build_overlay(self.source, self.output, self.denied, self.expected)
        self.assertEqual(list(self.output.iterdir()), [])

    def test_certificate_only_bundle_cannot_shrink_to_an_empty_trust_store(self):
        with self.assertRaises(ValueError):
            guard.sanitize(self.certs[0], self.denied)

    def test_guard_process_exits_nonzero_before_startup_when_policy_is_missing(self):
        script = self.base / "harden-certifi.py"
        shutil.copyfile(APP / "harden-certifi.py", script)
        result = subprocess.run([sys.executable, "-B", str(script)], capture_output=True, timeout=30)
        self.assertEqual(result.returncode, 78)
        self.assertIn(b"refusing application startup", result.stderr)
        self.assertEqual(list(self.output.iterdir()), [])

    def test_native_openssl_rejects_removed_root_and_keeps_unaffected_root(self):
        openssl = os.environ.get("APPLICATION_CA_OPENSSL") or shutil.which("openssl")
        if not openssl:
            self.skipTest("OpenSSL executable unavailable; native check also required in vendor Docker test")
        clean, _ = guard.sanitize(b"".join(self.certs), self.denied)
        (self.base / "clean.pem").write_bytes(clean)
        for index, expected in ((0, False), (1, True)):
            cert = self.base / "root.pem"
            cert.write_bytes(self.certs[index])
            result = subprocess.run([openssl, "verify", "-no-CApath", "-no-CAstore",
                                     "-CAfile", str(self.base / "clean.pem"), str(cert)],
                                    capture_output=True, timeout=20)
            self.assertEqual(result.returncode == 0, expected, result.stderr.decode(errors="replace"))


@unittest.skipUnless(sys.platform.startswith("linux"), "Exact-image startup tests require isolated Linux/Docker")
class CalibreVendorDockerTests(unittest.TestCase):
    def setUp(self):
        if not shutil.which("docker"):
            self.fail("Linux Docker test requires Docker; do not treat this as runtime clearance")
        self.base = ROOT / ("fleet-application-ca-docker-test-" + uuid.uuid4().hex)
        self.base.mkdir()
        self.addCleanup(shutil.rmtree, self.base)
        self.names = []
        self.addCleanup(self.cleanup_containers)
        image = VALUES["controllers"]["calibre-web"]["containers"]["app"]["image"]
        self.image = image["repository"] + ":" + image["tag"]
        self.docker("pull", "--platform=linux/amd64", self.image, timeout=300)

    def cleanup_containers(self):
        for name in self.names:
            subprocess.run(["docker", "rm", "--force", "--volumes", name], capture_output=True, timeout=45)

    def docker(self, *args, timeout=60):
        result = subprocess.run(["docker", *args], capture_output=True, timeout=timeout)
        self.assertEqual(result.returncode, 0, "isolated Docker operation failed: " + " ".join(args[:2]))
        return result.stdout

    def create(self, options, command=()):
        name = "calibre-ca-test-" + uuid.uuid4().hex[:12]
        self.names.append(name)
        self.docker("create", "--name", name, "--platform=linux/amd64", "--network=none",
                    *options, self.image, *command)
        return name

    def test_pinned_full_vendor_init_cannot_restore_certifi_and_native_rejects_bad_root(self):
        report = json.loads((ROOT / ".github/security/fleet-application-runtime-ca-hardening-report.json").read_text())
        expected_startup = report["vendor_startup"]["sha256"]
        self.assertEqual(json.loads(self.docker("image", "inspect", "--format={{json .Config.Entrypoint}}",
                                                self.image)), ["/init"])
        self.assertIsNone(json.loads(self.docker("image", "inspect", "--format={{json .Config.Cmd}}",
                                                self.image)))
        baseline_probe = (
            "import certifi,base64,json,hashlib,pathlib;"
            "print(json.dumps({'bundle':base64.b64encode(pathlib.Path(certifi.where()).read_bytes()).decode(),"
            "'startup':{p:hashlib.sha256(pathlib.Path('/'+p).read_bytes()).hexdigest() for p in "
            + repr(list(expected_startup)) + "}}))"
        )
        baseline = self.create(["--entrypoint=python3"], [
            "-B", "-c", baseline_probe,
        ])
        baseline_result = json.loads(self.docker("start", "--attach", baseline))
        self.assertEqual(baseline_result["startup"], expected_startup)
        original = base64.b64decode(baseline_result["bundle"])
        before = guard.certificates(original)
        denied = guard.policy(APP / "blocked-spkis.sha256")
        clean, counts = guard.sanitize(original, denied)
        self.assertEqual(counts, {"before": 119, "after": 118, "removed": 1})
        (self.base / "denied.pem").write_bytes(next(pem for pem, spki, _ in before if spki in denied))
        (self.base / "allowed.pem").write_bytes(next(pem for pem, spki, _ in before if spki not in denied))
        output = self.base / "output"
        output.mkdir(mode=0o777)
        output.chmod(0o777)
        initializer = self.create([
            "--entrypoint=/bin/sh", "--read-only", "--user=1000:1000", "--cap-drop=ALL",
            "--security-opt=no-new-privileges",
            "--volume", str(APP) + ":/ca-hardening:ro",
            "--volume", str(output) + ":/ca-output",
        ], ["-c", (
            "python3 -B /ca-hardening/harden-certifi.py; result=$?; "
            # Allow the host runner to remove files owned by the image's UID.
            "if [ -d /ca-output/certifi ]; then chmod 0777 /ca-output/certifi; fi; "
            'exit "$result"'
        )])
        self.docker("start", "--attach", initializer)
        self.assertEqual((output / "certifi/cacert.pem").read_bytes(), clean)
        self.assertEqual(Counter(row[2] for row in guard.certificates(clean)),
                         Counter(der for _, spki, der in before if spki not in denied))
        main = self.create([
            "--cap-drop=ALL", "--cap-add=CHOWN", "--cap-add=SETUID", "--cap-add=SETGID",
            "--env=PUID=1000", "--env=PGID=1000",
            "--volume", str(output / "certifi") + ":" + str(guard.PACKAGE) + ":ro",
            "--volume", str(self.base) + ":/public-fixtures:ro",
        ])
        self.docker("start", main)
        ready = False
        for _ in range(60):
            probe = subprocess.run(["docker", "exec", main, "python3", "-B", "-c",
                                    "import socket;socket.create_connection(('127.0.0.1',8083),1).close()"],
                                   capture_output=True, timeout=10)
            if probe.returncode == 0:
                ready = True
                break
            time.sleep(1)
        self.assertTrue(ready, "original /init did not reach Calibre service readiness")
        probe = r"""
import certifi,requests,ssl,json,hashlib,os
from pathlib import Path
from cryptography import x509
from cryptography.hazmat.primitives.serialization import Encoding,PublicFormat
blocked=BLOCKED
assert certifi.where()=='/lsiopy/lib/python3.12/site-packages/certifi/cacert.pem'
assert requests.certs.where()==certifi.where()
contexts=[ssl.create_default_context()]
contexts += [ssl.create_default_context(cafile=module.where()) for module in
             (certifi,__import__('pip._vendor.certifi',fromlist=['where']))]
for ctx in contexts:
 for der in ctx.get_ca_certs(binary_form=True):
  c=x509.load_der_x509_certificate(der)
  assert hashlib.sha256(c.public_key().public_bytes(Encoding.DER,PublicFormat.SubjectPublicKeyInfo)).hexdigest() not in blocked
try:
 Path(certifi.where()).write_bytes(b'forbidden')
 raise AssertionError('read-only package was writable')
except OSError: pass
try:
 Path(certifi.__file__).parent.rename(Path(certifi.__file__).parent.with_name('certifi-moved'))
 raise AssertionError('mounted package could be replaced by pip')
except OSError: pass
print('direct certifi/requests and pip native contexts clear; package replacement blocked')
""".replace("BLOCKED", repr(sorted(denied)))
        self.docker("exec", main, "python3", "-B", "-c", probe)
        for fixture, allowed in (("denied.pem", False), ("allowed.pem", True)):
            result = subprocess.run([
                "docker", "exec", main, "openssl", "verify", "-no-CApath", "-no-CAstore",
                "-CAfile", str(guard.PACKAGE / "cacert.pem"), "/public-fixtures/" + fixture,
            ], capture_output=True, timeout=20)
            self.assertEqual(result.returncode == 0, allowed)
        native_default = subprocess.run([
            "docker", "exec", main, "openssl", "verify", "-no-CAstore", "/public-fixtures/denied.pem",
        ], capture_output=True, timeout=20)
        self.assertNotEqual(native_default.returncode, 0)
        self.docker("exec", main, "openssl", "verify", "-no-CApath", "-no-CAstore",
                    "-CAfile", "/public-fixtures/denied.pem", "/public-fixtures/denied.pem")

    def test_invalid_policy_init_fails_without_an_overlay_or_application_start(self):
        scripts = self.base / "scripts"
        scripts.mkdir()
        shutil.copyfile(APP / "harden-certifi.py", scripts / "harden-certifi.py")
        (scripts / "blocked-spkis.sha256").write_text("bad\n")
        output = self.base / "output"
        output.mkdir(mode=0o777)
        output.chmod(0o777)
        initializer = self.create([
            "--entrypoint=python3", "--read-only", "--cap-drop=ALL", "--user=1000:1000",
            "--volume", str(scripts) + ":/ca-hardening:ro", "--volume", str(output) + ":/ca-output",
        ], ["-B", "/ca-hardening/harden-certifi.py"])
        subprocess.run(["docker", "start", "--attach", initializer], capture_output=True, timeout=60)
        state = json.loads(self.docker("inspect", "--format={{json .State}}", initializer))
        self.assertEqual(state["ExitCode"], 78)
        self.assertEqual(list(output.iterdir()), [])


if __name__ == "__main__":
    unittest.main()
