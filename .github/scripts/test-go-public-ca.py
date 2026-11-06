"""Immutable-runtime public-store acceptance; no custom Go TLS certification."""

import argparse
import importlib.util
import json
from pathlib import Path
import shutil
import subprocess
import sys
import tempfile
import time
import unittest
import uuid

import yaml


ROOT = Path(__file__).resolve().parents[2]
SHARED = ROOT / "apps" / "_shared"
APPS = {"stash": "vault", "syncthing": "syncthing"}
spec = importlib.util.spec_from_file_location(
    "native_acceptance", Path(__file__).with_name("test-native-app-ca-hardening.py")
)
native = importlib.util.module_from_spec(spec)
spec.loader.exec_module(native)
DOCKER = False


def release(app):
    return yaml.safe_load((ROOT / "apps" / APPS[app] / "helmrelease.yaml").read_text())


native.release = release
native.PROFILES = tuple(APPS)
original_baseline = native.baseline
SYNCTHING_V2 = json.loads((ROOT / ".github" / "security" / "syncthing-v2-runtime.json").read_text())


def baseline(app):
    if app == "syncthing" and native.image(app).split("@")[-1] == SYNCTHING_V2["declared_imageID"].split("@")[-1]:
        # Adapt the legacy comparison field without rewriting historical running-image evidence.
        return {**SYNCTHING_V2, "running_imageID": SYNCTHING_V2["declared_imageID"]}
    return original_baseline(app)


native.baseline = baseline
for app in APPS:
    native.guard.NATIVE_TREES[app] = ("etc/ssl", *native.guard.TREES[1:]) + (
        ("usr/lib/python3.12/site-packages/pip/_vendor/certifi",) if app == "stash" else ()
    )


def packaged_guard(app, destination):
    destination.chmod(0o755)
    with tempfile.TemporaryDirectory() as temporary:
        root = Path(temporary)
        for name in ("media-ca-hardening", "go-ca-hardening"):
            shutil.copytree(SHARED / name, root / "apps" / "_shared" / name)
        source = ROOT / "apps" / APPS[app]
        target = root / "apps" / APPS[app]
        shutil.copytree(source / "ca-guard", target / "ca-guard")
        for name in ("helmrelease.yaml", "kustomize-ca.json"):
            shutil.copyfile(source / name, target / name)
        (target / "kustomization.yaml").write_text(yaml.safe_dump({
            "apiVersion": "kustomize.config.k8s.io/v1beta1", "kind": "Kustomization",
            "namespace": release(app)["metadata"]["namespace"],
            "resources": ["ca-guard", "helmrelease.yaml"],
            "configurations": ["kustomize-ca.json"],
        }))
        rendered = native.run(["kubectl", "kustomize", str(target)]).stdout
        objects = list(yaml.safe_load_all(rendered))
    cm = next(item for item in objects if item["kind"] == "ConfigMap")
    hr = next(item for item in objects if item["kind"] == "HelmRelease")
    assert hr["spec"]["values"]["persistence"]["ca-guard"]["name"] == cm["metadata"]["name"]
    assert cm["metadata"]["namespace"] == hr["metadata"]["namespace"]
    for name, value in cm["data"].items():
        (destination / name).write_bytes(value.replace("\r\n", "\n").encode())
    return cm


class GoPublicStoreTests(unittest.TestCase):
    def test_hash_binding_original_identity_and_init_data_isolation(self):
        for app in APPS:
            with self.subTest(app=app), tempfile.TemporaryDirectory() as temporary:
                packaged_guard(app, Path(temporary))
                values = release(app)["spec"]["values"]
                controller = values["controllers"][app]
                self.assertEqual(native.image(app), native.image(app, "ca-copy"))
                self.assertEqual(
                    native.image(app).split(":")[0] + "@" + native.image(app).split("@")[1],
                    native.baseline(app)["running_imageID"].removeprefix("docker.io/"),
                )
                self.assertNotIn("command", controller["containers"]["app"])
                if app == "syncthing":
                    expected_args = SYNCTHING_V2["application_args"] if "public_store_denied_copies" in baseline(app) else [
                        "serve", "--home=/config", "--gui-address=0.0.0.0:8384", "--no-default-folder",
                    ]
                    self.assertEqual(controller["containers"]["app"]["args"], expected_args)
                for persistence in values["persistence"].values():
                    if "existingClaim" in persistence:
                        self.assertNotIn("globalMounts", persistence)
                        self.assertEqual(set(persistence["advancedMounts"][app]), {"app"})
                mounts = values["persistence"]["ca-store"]["advancedMounts"][app]["app"]
                self.assertEqual({m["path"].lstrip("/") for m in mounts},
                                 set(native.guard.NATIVE_TREES[app]))
                self.assertTrue(all(m["readOnly"] for m in mounts))
        legacy = list(yaml.safe_load_all(native.run([
            "kubectl", "kustomize", str(ROOT / "apps" / "bookshelf" / "ca-guard")
        ]).stdout))[0]
        self.assertNotIn("go-ca-overlay.py", legacy["data"])
        self.assertNotIn("copy-go-ca.sh", legacy["data"])

    def test_new_profiles_filter_system_and_package_roots_and_fail_closed(self):
        native.FilterTests.setUpClass()
        for app in APPS:
            fixture = native.FilterTests()
            fixture.setUp()
            try:
                with tempfile.TemporaryDirectory() as temporary:
                    packaged_guard(app, Path(temporary))
                    spec = importlib.util.spec_from_file_location(
                        "packaged_go_guard", Path(temporary) / "go-ca-overlay.py"
                    )
                    module = importlib.util.module_from_spec(spec)
                    spec.loader.exec_module(module)
                    guard = module.load_guard()
                    for package in guard.PACKAGES[app]:
                        path = fixture.root / package
                        path.mkdir(parents=True)
                        (path / "cacert.pem").write_bytes(b"".join(fixture.pem))
                    denied = {fixture.digests[0]}
                    result = guard.harden(fixture.root, app, denied)
                    self.assertGreater(result["removed_certificate_copies"], 0)
                    self.assertEqual(guard.harden(fixture.root, app, denied, True)["removed_certificate_copies"], 0)
                    fixture.bundle.write_bytes(b"-----BEGIN CERTIFICATE-----\ninvalid\n")
                    with self.assertRaises(ValueError):
                        guard.harden(fixture.root, app, denied)
            finally:
                fixture.doCleanups()

    def test_optional_immutable_factory_layout_and_independent_spki(self):
        native.ImmutableImageTests.test_optional_public_export_has_exact_expected_layout_and_independent_spki(self)

    def test_optional_actual_init_readonly_and_original_startup(self):
        if not DOCKER:
            self.skipTest("Explicit --docker required on Linux/amd64")
        self.assertEqual(native.docker(
            "version", "--format", "{{.Server.Os}}/{{.Server.Arch}}", text=True
        ).stdout.strip(), "linux/amd64")
        for app in APPS:
            token = uuid.uuid4().hex
            volume, name = "go-ca-" + token, "go-ca-app-" + token
            for ref in (native.image(app), native.image(app, "ca-filter")):
                native.docker("pull", "--platform", "linux/amd64", ref, timeout=300)
            native.docker("volume", "create", volume)
            try:
                with tempfile.TemporaryDirectory() as temporary:
                    packaged_guard(app, Path(temporary))
                    common = [
                        "--network", "none", "--read-only", "--cap-drop", "ALL",
                        "--security-opt", "no-new-privileges", "--user", "0:0",
                        "--volume", temporary + ":/ca-guard:ro", "--volume", volume + ":/ca-store",
                    ]
                    copy = subprocess.run(
                        ["docker", "run", "--rm", *common, "--entrypoint", "/bin/sh",
                         native.image(app), "/ca-guard/copy-go-ca.sh", app],
                        capture_output=True, text=True, timeout=120,
                    )
                    self.assertEqual(copy.returncode, 0, app + " public-root copy failed: " + copy.stderr)
                    command = [
                        "run", "--rm", *common, "--entrypoint", "python3", native.image(app, "ca-filter"),
                        "-B", "/ca-guard/go-ca-overlay.py", "--root", "/ca-store", "--app", app,
                        "--policy", "/ca-guard/blocked-spkis.sha256",
                    ]
                    result = json.loads(native.docker(*command, text=True).stdout)
                    expected_denied = baseline(app).get("public_store_denied_copies")
                    if expected_denied is None:
                        self.assertGreater(result["removed_certificate_copies"], 0)
                    else:
                        self.assertEqual(result["removed_certificate_copies"], expected_denied)
                    overlays = []
                    for tree in native.guard.NATIVE_TREES[app]:
                        overlays += ["--mount", f"type=volume,src={volume},dst=/{tree},readonly,volume-subpath={tree}"]
                    options = ["--cap-drop", "ALL", "--security-opt", "no-new-privileges"]
                    config = "/config" if app == "syncthing" else "/root/.stash"
                    options += ["--tmpfs", config + ":rw,nosuid,nodev,size=134217728,uid=1000,gid=1000,mode=0777"]
                    if app == "syncthing":
                        options += ["--user", "1000:1000", "--cpus", "0.5", "--memory", "512m",
                                    "--env", "STNOUPGRADE=true"]
                    arguments = release(app)["spec"]["values"]["controllers"][app]["containers"]["app"].get("args", [])
                    native.docker("run", "--detach", "--name", name, "--network", "none",
                                  *options, *overlays, native.image(app), *arguments)
                    url = "http://127.0.0.1:" + (
                        "8384/rest/noauth/health" if app == "syncthing" else "9999/"
                    )
                    ready = False
                    for _ in range(90):
                        probe = subprocess.run(
                            ["docker", "exec", name, "wget", "-q", "-O", "/dev/null", url],
                            capture_output=True, timeout=10,
                        )
                        if probe.returncode == 0:
                            ready = True
                            break
                        if native.docker("inspect", "--format", "{{.State.Running}}", name, text=True).stdout.strip() != "true":
                            break
                        time.sleep(2)
                    self.assertTrue(ready, app + " isolated original startup not ready; logs intentionally withheld")
                    native.ImmutableImageTests.assert_public_readonly(
                        self, app, "exec", "--user", "0", name, "/bin/sh", "-c", native.readonly_probe(app)
                    )
                    native.docker(*command, "--verify-only")
                    native.RESULTS.append({
                        "app": app, "public_store": "FILTERED_READONLY", "startup": "ORIGINAL_VENDOR_HTTP_READY",
                        "configuration": "DISPOSABLE_ONLY", "compiled_or_custom_go_tls": "NOT_CERTIFIED",
                    })
            finally:
                subprocess.run(["docker", "rm", "--force", name], capture_output=True, timeout=30)
                native.docker("volume", "rm", volume)


if __name__ == "__main__":
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--registry", action="store_true")
    parser.add_argument("--crane", default="crane")
    parser.add_argument("--docker", action="store_true")
    parser.add_argument("--evidence", type=Path)
    options, remaining = parser.parse_known_args()
    native.REGISTRY, native.CRANE, DOCKER = options.registry, options.crane, options.docker
    result = unittest.main(argv=[sys.argv[0], *remaining], exit=False).result
    if options.evidence:
        options.evidence.write_text(json.dumps({
            "successful": result.wasSuccessful(), "tests_run": result.testsRun,
            "skipped": len(result.skipped), "results": native.RESULTS,
            "rollout_clearance": False, "compiled_or_custom_go_tls": "NOT_CERTIFIED",
        }, indent=2) + "\n")
    sys.exit(not result.wasSuccessful())
