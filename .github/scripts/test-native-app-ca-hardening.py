"""Bounded native-app CA preparation checks; optional public registry and isolated Docker evidence."""

import argparse
import datetime
import hashlib
import importlib.util
import json
import os
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
from cryptography.hazmat.primitives.asymmetric import ec
from cryptography.x509.oid import NameOID
import yaml


ROOT = Path(__file__).resolve().parents[2]
SHARED = ROOT / "apps" / "_shared" / "media-ca-hardening"
APPS = ("bookshelf", "qbittorrent", "overseerr")
PROFILES = (*APPS, "gluetun")
REGISTRY = DOCKER = RENDER = False
CRANE = "crane"
RESULTS = []
spec = importlib.util.spec_from_file_location("native_ca_overlay", SHARED / "ca-overlay.py")
guard = importlib.util.module_from_spec(spec)
spec.loader.exec_module(guard)
POLICY = guard.policy_keys(SHARED / "blocked-spkis.sha256")
BASELINE = json.loads((ROOT / ".github" / "security" / "fleet-application-ca-report.json").read_text())
EVIDENCE = ROOT / ".github" / "security" / "fleet-native-app-ca-report.json"


def release(app):
    return yaml.safe_load((ROOT / "apps" / app / "helmrelease.yaml").read_text())


def binding(profile):
    return ("qbittorrent", "gluetun", "-gluetun", "gluetun-ca-store") if profile == "gluetun" else (
        profile, "app", "", "ca-store",
    )


def image(profile, init=None):
    app, container, suffix, _ = binding(profile)
    controller = release(app)["spec"]["values"]["controllers"][app]
    entry = controller["containers"][container] if init is None else controller["initContainers"][init + suffix]
    return entry["image"]["repository"] + ":" + entry["image"]["tag"]


def baseline(profile):
    needle = "seerr-team/seerr" if profile == "overseerr" else "/" + profile + "@"
    return next(row for row in BASELINE["images"] if needle in row["running_imageID"])


def run(command, **kwargs):
    return subprocess.run(command, capture_output=True, check=True, timeout=kwargs.pop("timeout", 120), **kwargs)


def docker(*command, **kwargs):
    return run(["docker", *command], **kwargs)


def readonly_probe(profile):
    command = "set -eu; LC_ALL=C; export LC_ALL; "
    for tree in guard.NATIVE_TREES[profile]:
        command += (
            f"if error=$(touch /{tree}/.native-ca-write-probe 2>&1); then exit 31; fi; "
            'case "$error" in *"Read-only file system"*) ;; '
            '*) printf "%s\\n" "$error" >&2; exit 35 ;; esac; '
            f"if error=$(mv /{tree} /{tree}-replaced 2>&1); then exit 32; fi; "
            'case "$error" in *"Read-only file system"*|*"Device or resource busy"*|*"Resource busy"*) ;; '
            '*) printf "%s\\n" "$error" >&2; exit 36 ;; esac; '
        )
    command += (
        "if error=$({ echo denied > /etc/ssl/cert.pem; } 2>&1); then exit 33; fi; "
        'case "$error" in *"Read-only file system"*) ;; '
        '*) printf "%s\\n" "$error" >&2; exit 37 ;; esac'
    )
    return command


class WiringTests(unittest.TestCase):
    def test_readonly_proof_requires_filesystem_errors_not_missing_tools_or_permissions(self):
        for profile in PROFILES:
            probe = readonly_probe(profile)
            self.assertIn("Read-only file system", probe)
            self.assertIn("Device or resource busy", probe)
            self.assertIn('"Resource busy"', probe)
            self.assertIn("LC_ALL=C", probe)
            self.assertNotIn("update-ca-certificates", probe)
            self.assertNotIn("2>/dev/null", probe)
            for tree in guard.NATIVE_TREES[profile]:
                self.assertIn(f"touch /{tree}/.native-ca-write-probe", probe)

    def test_exact_images_and_existing_versioned_policy_are_bound(self):
        canonical = json.loads((ROOT / ".github" / "security" / "emudhra-2026.json").read_text())
        self.assertEqual(POLICY, {x["sha256"].upper() for x in canonical["blocked_spkis"]})
        for profile in PROFILES:
            ref = image(profile)
            self.assertEqual(ref.split(":")[0] + "@" + ref.split("@")[1],
                             baseline(profile)["running_imageID"].removeprefix("docker.io/"))
            self.assertEqual(image(profile, "ca-copy"), ref)
            self.assertEqual(image(profile, "ca-filter"), image("bookshelf", "ca-filter"))
        self.assertIn("3.15-alpine@sha256:", release("qbittorrent")["spec"]["values"][
            "controllers"]["qbittorrent"]["containers"]["seed-policy"]["image"]["tag"])

    def test_fail_closed_init_and_no_user_volumes_or_entrypoint_changes(self):
        for profile in PROFILES:
            app, container, suffix, store = binding(profile)
            doc = release(app)
            self.assertTrue(doc["spec"]["suspend"])
            self.assertEqual(doc["spec"]["chart"]["spec"]["version"], "5.3.0")
            values = doc["spec"]["values"]
            controller = values["controllers"][app]
            self.assertNotIn("command", controller["containers"][container])
            self.assertNotIn("args", controller["containers"][container])
            init = controller["initContainers"]
            self.assertEqual(init["ca-copy" + suffix]["command"],
                             ["/bin/sh", "/ca-guard/copy-ca-overlay.sh", profile])
            self.assertEqual(init["ca-filter" + suffix]["args"], [
                "--root", "/ca-store", "--app", profile, "--policy", "/ca-guard/blocked-spkis.sha256",
            ])
            for name in ("ca-copy" + suffix, "ca-filter" + suffix):
                self.assertNotIn("restartPolicy", init[name])
                self.assertTrue(init[name]["securityContext"]["readOnlyRootFilesystem"])
                self.assertEqual(init[name]["securityContext"]["capabilities"]["drop"], ["ALL"])
                for entry in values["persistence"].values():
                    if "existingClaim" in entry:
                        self.assertNotIn("globalMounts", entry)
                        self.assertNotIn(name, entry.get("advancedMounts", {}).get(app, {}))
            mounts = values["persistence"][store]["advancedMounts"][app][container]
            self.assertEqual({m["subPath"] for m in mounts}, set(guard.NATIVE_TREES[profile]))
            self.assertTrue(all(m["readOnly"] and m["path"] == "/" + m["subPath"] for m in mounts))
            self.assertFalse(guard.PACKAGES[profile], "These profiles are SYSTEM-only")
        for name in ("ca-copy", "ca-filter"):
            context = release("overseerr")["spec"]["values"]["controllers"]["overseerr"]["initContainers"][name]["securityContext"]
            self.assertEqual(context["runAsUser"], 0)
            self.assertFalse(context["runAsNonRoot"])

    def test_vpn_dependencies_auth_and_capabilities_are_preserved(self):
        values = release("qbittorrent")["spec"]["values"]
        containers = values["controllers"]["qbittorrent"]["containers"]
        self.assertEqual(containers["app"]["dependsOn"], "gluetun")
        self.assertEqual(containers["vpn-sync"]["dependsOn"], "gluetun")
        self.assertEqual(containers["seed-policy"]["dependsOn"], "app")
        self.assertEqual(containers["gluetun"]["securityContext"]["capabilities"]["add"], ["NET_ADMIN"])
        env = {e["name"]: e for e in containers["gluetun"]["env"]}
        self.assertEqual(env["WIREGUARD_PRIVATE_KEY"]["valueFrom"]["secretKeyRef"]["name"], "qbittorrent-vpn")
        self.assertEqual(env["VPN_TYPE"]["value"], "wireguard")
        self.assertTrue(containers["gluetun"]["probes"]["liveness"]["enabled"])
        self.assertNotIn("gluetun", values["persistence"]["ca-store"]["advancedMounts"]["qbittorrent"])
        self.assertNotIn("app", values["persistence"]["gluetun-ca-store"]["advancedMounts"]["qbittorrent"])
        node_env = release("overseerr")["spec"]["values"]["controllers"]["overseerr"]["containers"]["app"]["env"]
        self.assertFalse(any(e["name"] in ("NODE_OPTIONS", "NODE_EXTRA_CA_CERTS") for e in node_env))

    def test_configmap_hash_reference_configuration(self):
        for app in APPS:
            directory = ROOT / "apps" / app
            config = yaml.safe_load((directory / "kustomization.yaml").read_text())
            self.assertEqual(config["namespace"], "media")
            self.assertIn("ca-guard", config["resources"])
            self.assertIn("kustomize-ca.json", config["configurations"])
            wrapper = yaml.safe_load((directory / "ca-guard" / "kustomization.yaml").read_text())
            self.assertEqual(wrapper["namePrefix"], app + "-")
            self.assertEqual(wrapper["resources"], ["../../_shared/media-ca-hardening"])
            ref = json.loads((directory / "kustomize-ca.json").read_text())
            self.assertEqual(ref["nameReference"][0]["fieldSpecs"][0]["path"],
                             "spec/values/persistence/ca-guard/name")
            self.assertEqual(release(app)["spec"]["values"]["persistence"]["ca-guard"]["name"], "ca-guard")

    def test_preparation_does_not_claim_unexecuted_native_or_vpn_clearance(self):
        evidence = json.loads(EVIDENCE.read_text())
        self.assertFalse(evidence["rollout_clearance"])
        self.assertFalse(evidence["cluster_changes"])
        self.assertEqual(evidence["gluetun_go"]["compiled_version"], "go1.25.11")
        self.assertEqual(evidence["gluetun_go"]["status"], "BLOCKED_BELOW_POLICY_FLOOR")
        self.assertTrue(evidence["remaining_blockers"])
        self.assertEqual(evidence["seerr_node"]["scope"], "SYSTEM_ONLY_DEFAULT_AND_BUNDLED_ALREADY_CLEAR")

    def test_optional_secret_free_offline_kustomize_and_helm_render(self):
        if not RENDER:
            self.skipTest("Explicit --render required")
        scratch = ROOT / (".native-ca-render-" + uuid.uuid4().hex)
        self.addCleanup(shutil.rmtree, scratch)
        helm_home = scratch / "helm"
        helm_home.mkdir(parents=True)
        helm_env = dict(os.environ, HELM_CACHE_HOME=str(helm_home / "cache"),
                        HELM_CONFIG_HOME=str(helm_home / "config"), HELM_DATA_HOME=str(helm_home / "data"),
                        HELM_REPOSITORY_CACHE=str(helm_home / "repository"),
                        HELM_REPOSITORY_CONFIG=str(helm_home / "repositories.yaml"),
                        TMP=str(helm_home), TEMP=str(helm_home), TMPDIR=str(helm_home))
        (helm_home / "repository").mkdir()
        # Deliberately never load the application's Secret/SealedSecret resources.
        shutil.copytree(SHARED, scratch / "apps" / "_shared" / "media-ca-hardening")
        kustomize = ["kustomize", "build"] if shutil.which("kustomize") else ["kubectl", "kustomize"]
        for app in APPS:
            directory = scratch / "apps" / app
            shutil.copytree(ROOT / "apps" / app / "ca-guard", directory / "ca-guard")
            for name in ("helmrelease.yaml", "kustomize-ca.json"):
                shutil.copyfile(ROOT / "apps" / app / name, directory / name)
            (directory / "kustomization.yaml").write_text(
                "namespace: media\nresources:\n  - ca-guard\n  - helmrelease.yaml\nconfigurations:\n  - kustomize-ca.json\n",
                encoding="utf-8",
            )
            rendered = list(yaml.safe_load_all(run([*kustomize, str(directory)], text=True).stdout))
            cm = next(x for x in rendered if x["kind"] == "ConfigMap")
            hr = next(x for x in rendered if x["kind"] == "HelmRelease")
            self.assertRegex(cm["metadata"]["name"], "^" + app + "-ca-guard-[a-z0-9]{10}$")
            self.assertEqual(hr["spec"]["values"]["persistence"]["ca-guard"]["name"], cm["metadata"]["name"])
            helper = scratch / "apps" / "_shared" / "media-ca-hardening" / "ca-overlay.py"
            helper.write_bytes(helper.read_bytes() + b"\n")
            changed = list(yaml.safe_load_all(run([*kustomize, str(directory)], text=True).stdout))
            changed_cm = next(x for x in changed if x["kind"] == "ConfigMap")
            changed_hr = next(x for x in changed if x["kind"] == "HelmRelease")
            self.assertNotEqual(cm["metadata"]["name"], changed_cm["metadata"]["name"])
            self.assertEqual(changed_hr["spec"]["values"]["persistence"]["ca-guard"]["name"],
                             changed_cm["metadata"]["name"])
            valuefile = directory / "values.yaml"
            valuefile.write_text(yaml.safe_dump(hr["spec"]["values"]), encoding="utf-8")
            chart = run([
                "helm", "template", app, "app-template", "--repo", "https://bjw-s-labs.github.io/helm-charts",
                "--version", "5.3.0", "--namespace", "media", "--values", str(valuefile),
            ], text=True, timeout=180, env=helm_env)
            manifests = list(yaml.safe_load_all(chart.stdout))
            deployment = next(x for x in manifests if x and x["kind"] == "Deployment")
            pod = deployment["spec"]["template"]["spec"]
            init_names = [x["name"] for x in pod["initContainers"]]
            for suffix in ("", "-gluetun") if app == "qbittorrent" else ("",):
                self.assertLess(init_names.index("ca-copy" + suffix), init_names.index("ca-filter" + suffix))
            if app == "qbittorrent":
                self.assertIn("gluetun", {container["name"] for container in pod["containers"]})
            pvc_names = {v["name"] for v in pod["volumes"] if "persistentVolumeClaim" in v}
            for init in pod["initContainers"]:
                if init["name"].startswith("ca-"):
                    self.assertFalse(pvc_names & {v["name"] for v in init["volumeMounts"]})
            RESULTS.append({"app": app, "render": "HASHED_CONFIGMAP_AND_SERIAL_INIT_VALIDATED"})


class FilterTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.keys, cls.pem, cls.digests = [], [], []
        for index in range(12):
            key = ec.generate_private_key(ec.SECP256R1())
            name = x509.Name([x509.NameAttribute(NameOID.COMMON_NAME, "Native fixture " + str(index))])
            cert = (
                x509.CertificateBuilder().subject_name(name).issuer_name(name).public_key(key.public_key())
                .serial_number(x509.random_serial_number())
                .not_valid_before(datetime.datetime(2025, 1, 1, tzinfo=datetime.timezone.utc))
                .not_valid_after(datetime.datetime(2035, 1, 1, tzinfo=datetime.timezone.utc))
                .add_extension(x509.BasicConstraints(ca=True, path_length=None), critical=True)
                .sign(key, hashes.SHA256())
            )
            cls.keys.append(key)
            cls.pem.append(cert.public_bytes(serialization.Encoding.PEM))
            cls.digests.append(hashlib.sha256(key.public_key().public_bytes(
                serialization.Encoding.DER, serialization.PublicFormat.SubjectPublicKeyInfo,
            )).hexdigest().upper())

    def setUp(self):
        self.scratch = ROOT / (".native-ca-fixture-" + uuid.uuid4().hex)
        self.addCleanup(shutil.rmtree, self.scratch)
        self.root = self.scratch / "snapshot"
        for tree in guard.NATIVE_TREES["bookshelf"]:
            (self.root / tree).mkdir(parents=True)
        (self.root / "etc/ssl/certs").mkdir()
        self.bundle = self.root / "etc/ssl/certs/ca-certificates.crt"
        self.bundle.write_bytes(b"".join(self.pem))
        self.source = self.root / "usr/share/ca-certificates"
        for index, pem in enumerate(self.pem):
            (self.source / ("fixture-" + str(index) + ".crt")).write_bytes(pem)
        self.denied = {self.digests[0]}

    def test_all_profiles_filter_exact_keys_and_preserve_unaffected_der(self):
        for profile in ("bookshelf", "qbittorrent", "gluetun"):
            result = guard.harden(self.root, profile, self.denied)
            self.assertEqual(result["unaffected_certificates"], 11)
            self.assertNotIn(self.digests[0], {key for _, key, _ in guard.certificates(self.bundle.read_bytes())})
            self.assertEqual({pem for pem, _, _ in guard.certificates(self.bundle.read_bytes())}, set(self.pem[1:]))
            guard.harden(self.root, profile, self.denied, verify_only=True)
        self.assertFalse((self.source / "fixture-0.crt").exists())
        self.assertTrue(all((self.source / ("fixture-" + str(i) + ".crt")).read_bytes() == self.pem[i]
                            for i in range(1, 12)))

    def test_missing_native_sources_and_malformed_bundles_fail_closed(self):
        for profile in ("bookshelf", "qbittorrent", "gluetun"):
            missing = self.root / "usr/local/share/ca-certificates"
            missing.rmdir()
            with self.assertRaises(ValueError):
                guard.harden(self.root, profile, self.denied)
            missing.mkdir()
        malformed_key_header = b"-----BEGIN " + b"PRIVATE KEY-----"
        for data in (b"", b"not pem", self.pem[0], self.pem[0] + malformed_key_header):
            self.bundle.write_bytes(data)
            with self.assertRaises((ValueError, ssl.SSLError)):
                guard.harden(self.root, "bookshelf", self.denied)

    def test_seerr_bundle_only_profile_does_not_invent_source_stores_or_certifi(self):
        shutil.rmtree(self.root / "usr")
        self.assertEqual(guard.NATIVE_TREES["overseerr"], ("etc/ssl",))
        guard.harden(self.root, "overseerr", self.denied)
        self.assertFalse((self.root / "usr").exists())
        self.assertEqual(guard.PACKAGES["overseerr"], ())
        (self.root / "usr/share/ca-certificates").mkdir(parents=True)
        with self.assertRaises(ValueError):
            guard.harden(self.root, "overseerr", self.denied)

    def test_empty_native_source_store_cannot_silently_pass_with_a_good_bundle(self):
        for path in self.source.iterdir():
            path.unlink()
        with self.assertRaises(ValueError):
            guard.harden(self.root, "bookshelf", self.denied)

    def test_cross_signed_copy_is_denied_by_key_not_name_or_certificate_fingerprint(self):
        source = x509.load_pem_x509_certificate(self.pem[0])
        cross = (
            x509.CertificateBuilder().subject_name(source.subject)
            .issuer_name(x509.load_pem_x509_certificate(self.pem[1]).subject)
            .public_key(self.keys[0].public_key()).serial_number(x509.random_serial_number())
            .not_valid_before(datetime.datetime(2025, 1, 1, tzinfo=datetime.timezone.utc))
            .not_valid_after(datetime.datetime(2035, 1, 1, tzinfo=datetime.timezone.utc))
            .add_extension(x509.BasicConstraints(ca=True, path_length=None), critical=True)
            .sign(self.keys[1], hashes.SHA256()).public_bytes(serialization.Encoding.PEM)
        )
        self.bundle.write_bytes(self.bundle.read_bytes() + cross)
        (self.source / "cross-signed.crt").write_bytes(cross)
        result = guard.harden(self.root, "bookshelf", self.denied)
        self.assertEqual(result["removed_certificate_copies"], 4)
        self.assertEqual(result["unaffected_certificates"], 11)
        self.assertFalse((self.source / "cross-signed.crt").exists())

    @unittest.skipUnless(sys.platform.startswith("linux"), "Linux absolute public-store aliases")
    def test_full_ssl_alias_hash_compaction_and_escape_rejection(self):
        (self.root / "etc/ssl/cert.pem").symlink_to("certs/ca-certificates.crt")
        (self.bundle.parent / "deadbeef.0").symlink_to("/usr/share/ca-certificates/fixture-0.crt")
        (self.bundle.parent / "deadbeef.1").symlink_to("/usr/share/ca-certificates/fixture-1.crt")
        guard.harden(self.root, "bookshelf", self.denied)
        self.assertEqual((self.bundle.parent / "deadbeef.0").readlink().as_posix(),
                         "/usr/share/ca-certificates/fixture-1.crt")
        self.assertFalse((self.bundle.parent / "deadbeef.1").is_symlink())
        (self.root / "etc/ssl/escape.pem").symlink_to("/outside.pem")
        with self.assertRaises(ValueError):
            guard.harden(self.root, "bookshelf", self.denied)

    def handshake(self, index):
        certfile, keyfile = self.scratch / "fixture.pem", self.scratch / "fixture.key"
        certfile.write_bytes(self.pem[index])
        keyfile.write_bytes(self.keys[index].private_bytes(
            serialization.Encoding.PEM, serialization.PrivateFormat.PKCS8, serialization.NoEncryption(),
        ))
        server = ssl.SSLContext(ssl.PROTOCOL_TLS_SERVER)
        server.load_cert_chain(certfile, keyfile)
        client = ssl.SSLContext(ssl.PROTOCOL_TLS_CLIENT)
        client.check_hostname = False
        client.load_verify_locations(cafile=str(self.bundle))
        cin, cout, sin, sout = [ssl.MemoryBIO() for _ in range(4)]
        endpoints = (client.wrap_bio(cin, cout, server_hostname="fixture"), server.wrap_bio(sin, sout, server_side=True))
        done = set()
        for _ in range(20):
            for i, endpoint in enumerate(endpoints):
                try:
                    endpoint.do_handshake()
                    done.add(i)
                except ssl.SSLWantReadError:
                    pass
            if cout.pending:
                sin.write(cout.read())
            if sout.pending:
                cin.write(sout.read())
            if len(done) == 2:
                return
        self.fail("Fixture TLS memory-BIO handshake did not finish")

    def test_positive_and_negative_tls_fixtures_are_helper_not_dotnet_qt_go_claims(self):
        self.handshake(0)
        self.handshake(1)
        guard.harden(self.root, "bookshelf", self.denied)
        self.handshake(1)
        with self.assertRaises(ssl.SSLCertVerificationError):
            self.handshake(0)


class ImmutableImageTests(unittest.TestCase):
    def assert_public_readonly(self, profile, *command):
        result = subprocess.run(
            ["docker", *command], capture_output=True, text=True, timeout=120,
        )
        self.assertEqual(result.returncode, 0,
                         profile + " public filesystem barrier failed: " + result.stderr)

    def test_optional_public_export_has_exact_expected_layout_and_independent_spki(self):
        if not REGISTRY:
            self.skipTest("Explicit --registry required; no image execution")
        for profile in PROFILES:
            expected = baseline(profile)["immutable_amd64"]
            actual = run([CRANE, "digest", "--platform=linux/amd64",
                          image(profile).replace("lscr.io/", "ghcr.io/")], text=True).stdout.strip()
            self.assertEqual(actual, expected.split("@")[1])
            # lscr.io's platform-manifest redirect can require authentication;
            # the same digest is public in its upstream ghcr.io repository.
            ref = expected.replace("lscr.io/", "ghcr.io/")
            log = ROOT / (".native-ca-export-" + uuid.uuid4().hex + ".log")
            self.addCleanup(log.unlink, missing_ok=True)
            present, dirs, aliases, denied, identities = set(), set(), {}, 0, set()
            with log.open("w+b") as errors:
                process = subprocess.Popen([CRANE, "export", ref, "-"], stdout=subprocess.PIPE, stderr=errors)
                try:
                    with tarfile.open(fileobj=process.stdout, mode="r|") as archive:
                        for member in archive:
                            name = member.name.replace("\\", "/").removeprefix("./").rstrip("/")
                            if member.isdir():
                                dirs.add(name)
                            if member.issym() and name.startswith(("etc/ssl/", "usr/share/ca-certificates/", "usr/local/share/ca-certificates/")):
                                aliases[name] = member.linkname.replace("\\", "/")
                            if not member.isfile() or not name.startswith(
                                ("etc/ssl/", "usr/share/ca-certificates/", "usr/local/share/ca-certificates/")
                            ) or not (name.endswith((".crt", ".pem")) or guard.HASH_NAME.fullmatch(Path(name).name)):
                                continue
                            data = archive.extractfile(member).read()
                            parsed = guard.certificates(data)
                            independent = x509.load_pem_x509_certificates(data)
                            self.assertEqual(len(parsed), len(independent))
                            for (_, key, fingerprint), cert in zip(parsed, independent, strict=True):
                                independent_key = hashlib.sha256(cert.public_key().public_bytes(
                                    serialization.Encoding.DER, serialization.PublicFormat.SubjectPublicKeyInfo,
                                )).hexdigest().upper()
                                self.assertEqual(key, independent_key)
                                denied += key in POLICY
                                if key not in POLICY:
                                    identities.add(fingerprint)
                            present.add(name)
                finally:
                    while process.stdout.read(1024 * 1024):
                        pass
                    process.stdout.close()
                    code = process.wait(timeout=300)
                errors.seek(0)
                self.assertEqual(code, 0, errors.read().decode(errors="replace"))
            self.assertTrue(set(guard.NATIVE_TREES[profile]) <= dirs)
            self.assertIn("etc/ssl/certs/ca-certificates.crt", present)
            self.assertEqual(aliases["etc/ssl/cert.pem"], "certs/ca-certificates.crt")
            if profile == "overseerr":
                self.assertFalse(any("share/ca-certificates" in name for name in dirs))
            self.assertGreater(denied, 0)
            self.assertGreater(len(identities), 10)
            RESULTS.append({"profile": profile, "immutable_amd64": expected, "public_pem_paths": len(present),
                            "baseline_denied_copies": denied, "unaffected_der_identities": len(identities),
                            "layout": sorted(set(guard.NATIVE_TREES[profile])), "native_runtime_claim": False})

    def test_optional_original_startup_readonly_stores_and_seerr_native_system_inventory(self):
        if not DOCKER:
            self.skipTest("Explicit --docker required on GitHub-hosted Linux/amd64")
        self.assertEqual(docker("version", "--format", "{{.Server.Os}}/{{.Server.Arch}}", text=True).stdout.strip(),
                         "linux/amd64")
        for profile in PROFILES:
            token = uuid.uuid4().hex[:12]
            volume, name = "native-ca-" + token, "native-ca-app-" + token
            for ref in (image(profile), image(profile, "ca-filter")):
                docker("pull", "--platform", "linux/amd64", ref, timeout=300)
            docker("volume", "create", volume)
            try:
                common = ["--network", "none", "--read-only", "--cap-drop", "ALL",
                          "--security-opt", "no-new-privileges", "--user", "0:0",
                          "--volume", str(SHARED) + ":/ca-guard:ro", "--volume", volume + ":/ca-store"]
                docker("run", "--rm", *common, "--entrypoint", "/bin/sh", image(profile),
                       "/ca-guard/copy-ca-overlay.sh", profile)
                filtered = docker(
                    "run", "--rm", *common, "--entrypoint", "python3", image(profile, "ca-filter"),
                    "-B", "/ca-guard/ca-overlay.py", "--root", "/ca-store", "--app", profile,
                    "--policy", "/ca-guard/blocked-spkis.sha256", text=True,
                )
                self.assertGreater(json.loads(filtered.stdout)["removed_certificate_copies"], 0)
                overlays = []
                for tree in guard.NATIVE_TREES[profile]:
                    overlays += ["--mount", f"type=volume,src={volume},dst=/{tree},readonly,volume-subpath={tree}"]
                # Gluetun's real VPN startup requires credentials/NET_ADMIN. Do
                # not bypass it, supply credentials, or claim full VPN validation.
                if profile == "gluetun":
                    self.assert_public_readonly(
                        profile, "run", "--rm", "--network", "none", "--cap-drop", "ALL",
                        "--security-opt", "no-new-privileges", *overlays,
                        "--entrypoint", "/bin/sh", image(profile), "-c", readonly_probe(profile),
                    )
                    RESULTS.append({"profile": profile, "public_store": "FILTERED_READONLY",
                                    "original_vpn_startup": "NOT_RUN_CREDENTIALS_AND_PRIVILEGES_PROHIBITED",
                                    "go_compiled_policy": "BLOCKED_GO1.25.11"})
                else:
                    config = "/app/config" if profile == "overseerr" else "/config"
                    tmpfs = config + ":rw,nosuid,nodev,size=134217728" + (
                        ",uid=1000,gid=1000,mode=0770" if profile == "overseerr" else ""
                    )
                    docker("run", "--detach", "--name", name, "--network", "none",
                           "--security-opt", "no-new-privileges", "--tmpfs", tmpfs, *overlays, image(profile))
                    port = {"bookshelf": 8787, "qbittorrent": 8080, "overseerr": 5055}[profile]
                    ready = False
                    for _ in range(90):
                        if profile == "overseerr":
                            command = ["node", "-e", (
                                "require('http').get('http://127.0.0.1:" + str(port)
                                + "/',r=>process.exit([200,301,302,307,308,401,403].includes(r.statusCode)?0:1))"
                                + ".on('error',()=>process.exit(1));setTimeout(()=>process.exit(1),2000)"
                            )]
                        else:
                            command = ["curl", "--silent", "--max-time", "2", "--output", "/dev/null",
                                       "--write-out", "%{http_code}", "http://127.0.0.1:" + str(port) + "/"]
                        probe = subprocess.run(["docker", "exec", name, *command],
                                               capture_output=True, text=True, timeout=10)
                        if probe.returncode == 0 and (
                            profile == "overseerr" or probe.stdout in ("200", "301", "302", "307", "308", "401", "403")
                        ):
                            ready = True
                            break
                        state = docker("inspect", "--format", "{{.State.Running}}", name, text=True).stdout.strip()
                        if state != "true":
                            break
                        time.sleep(2)
                    # Do not print application logs: startup may generate auth credentials.
                    self.assertTrue(ready, profile + " original startup failed isolated HTTP readiness; no logs dumped")
                    self.assert_public_readonly(
                        profile, "exec", "--user", "0", name, "/bin/sh", "-c", readonly_probe(profile),
                    )
                    if profile == "overseerr":
                        native = (
                            "const fs=require('fs'),tls=require('tls'),crypto=require('crypto');"
                            "const denied=new Set(" + json.dumps(sorted(POLICY)) + ");"
                            "const scan=kind=>{const roots=tls.getCACertificates(kind);"
                            "if(roots.length<10)throw Error('Missing native roots');"
                            "for(const pem of roots){const c=new crypto.X509Certificate(pem);"
                            "const spki=crypto.createHash('sha256').update(c.publicKey.export({type:'spki',format:'der'}))"
                            ".digest('hex').toUpperCase();if(denied.has(spki))throw Error('Denied '+kind+' key')}"
                            "return roots.length};"
                            "if(process.version!=='v22.23.4')throw Error('Unexpected Node version');"
                            "console.log(JSON.stringify({version:process.version,system:scan('system'),"
                            "default:scan('default'),bundled:scan('bundled')}));"
                        )
                        roots = json.loads(docker("exec", name, "node", "-e", native, text=True).stdout)
                        RESULTS.append({"profile": profile, "original_startup": "ISOLATED_HTTP_READY",
                                        "native_node_root_inventory": roots,
                                        "scope": "SYSTEM_ONLY_NO_CUSTOM_CONTEXT_OR_EXTRA_CA_CLAIM"})
                    else:
                        RESULTS.append({"profile": profile, "original_startup": "ISOLATED_HTTP_READY",
                                        "native_chain_validation": "NOT_CERTIFIED_DOTNET_QT_OR_CUSTOM",
                                        "vpn_gating_integration": "NOT_TESTED" if profile == "qbittorrent" else None})
                docker("run", "--rm", *common, "--entrypoint", "python3", image(profile, "ca-filter"),
                       "-B", "/ca-guard/ca-overlay.py", "--root", "/ca-store", "--app", profile,
                       "--policy", "/ca-guard/blocked-spkis.sha256", "--verify-only")
            finally:
                # Only this invocation's named resources; no broad process/container cleanup.
                subprocess.run(["docker", "rm", "--force", name], capture_output=True, timeout=30)
                docker("volume", "rm", volume)


if __name__ == "__main__":
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--registry", action="store_true")
    parser.add_argument("--crane", default="crane")
    parser.add_argument("--docker", action="store_true")
    parser.add_argument("--render", action="store_true")
    parser.add_argument("--evidence", type=Path)
    options, remaining = parser.parse_known_args()
    REGISTRY, CRANE, DOCKER, RENDER = options.registry, options.crane, options.docker, options.render
    program = unittest.main(argv=[sys.argv[0], *remaining], exit=False)
    if options.evidence:
        destination = options.evidence.absolute()
        if not destination.is_relative_to(ROOT):
            raise ValueError("Evidence must remain inside this worktree")
        destination.write_text(json.dumps({
            "created_utc": datetime.datetime.now(datetime.timezone.utc).isoformat(),
            "successful": program.result.wasSuccessful(), "tests_run": program.result.testsRun,
            "skipped": len(program.result.skipped), "results": RESULTS,
            "requested_modes": {"registry": REGISTRY, "docker": DOCKER, "render": RENDER},
            "rollout_clearance": False,
            "coverage_limit": "Only recorded results executed. Fixtures prove helper TLS behavior; "
                              "Docker mode tests read-only startup and Node root inventory. "
                              ".NET/Qt/Go/custom/VPN chain validation is not certified.",
        }, indent=2) + "\n", encoding="utf-8")
    sys.exit(not program.result.wasSuccessful())
