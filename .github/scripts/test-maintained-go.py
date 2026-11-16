"""Focused fail-closed tests for actual ELF acceptance."""
import hashlib
import importlib.util
import json
from pathlib import Path
import shutil
import subprocess
import unittest
from unittest.mock import patch

spec = importlib.util.spec_from_file_location("maintained", Path(__file__).with_name("verify-maintained-go.py"))
module = importlib.util.module_from_spec(spec)
spec.loader.exec_module(module)
build_spec = importlib.util.spec_from_file_location("maintained_build", module.BUILD / "build.py")
build = importlib.util.module_from_spec(build_spec)
build_spec.loader.exec_module(build)


def scanner_config(mode):
    return {"scanner_name": "govulncheck", "scanner_version": "v1.9.0",
            "db": "https://vuln.go.dev", "db_last_modified": "2026-11-11T02:51:11Z",
            "scan_mode": mode, "scan_level": "symbol"}


class PlatformDigestTest(unittest.TestCase):
    def test_selects_runtime_instead_of_index_or_attestation(self):
        digest = "sha256:" + "a" * 64
        manifest = {"schemaVersion": 2, "manifests": [
            {"digest": "sha256:" + "b" * 64, "platform": {"os": "unknown", "architecture": "unknown"}},
            {"digest": digest, "platform": {"os": "linux", "architecture": "amd64"}},
        ]}
        self.assertEqual(module.select_amd64_digest(manifest, "sha256:" + "c" * 64), digest)

    def test_accepts_a_direct_runtime_manifest(self):
        digest = "sha256:" + "a" * 64
        for media in ("application/vnd.oci.image.manifest.v1+json",
                      "application/vnd.docker.distribution.manifest.v2+json"):
            with self.subTest(media=media):
                self.assertEqual(module.select_amd64_digest(
                    {"schemaVersion": 2, "mediaType": media}, digest,
                ), digest)

    def test_rejects_missing_ambiguous_or_invalid_runtime(self):
        runtime = {"digest": "sha256:" + "a" * 64, "platform": {"os": "linux", "architecture": "amd64"}}
        for children in ([], [runtime, runtime], [{**runtime, "digest": "not-a-sha256"}]):
            with self.subTest(children=children), self.assertRaises(AssertionError):
                module.select_amd64_digest({"schemaVersion": 2, "manifests": children}, "sha256:" + "b" * 64)


class ArtifactGateTest(unittest.TestCase):
    def setUp(self):
        self.directory = module.ROOT / ".maintained-go-unit"
        self.directory.mkdir(exist_ok=False)

    def tearDown(self):
        shutil.rmtree(self.directory)

    def execute(self, compiler="go1.27.4", claimed_hash=None):
        binary = b"synthetic ELF fixture"
        source = {"revision": "fixture-revision", "app": "gluetun",
                  "binary_sha256": claimed_hash or hashlib.sha256(binary).hexdigest()}

        def docker(*args):
            output = ""
            if args[0] == "create":
                output = "fixture-container"
            elif args[0] == "cp":
                Path(args[2]).write_bytes(binary if args[1].endswith("/binary") else json.dumps(source).encode())
            elif args[0] == "run":
                output = f"/proof/application.elf: {compiler}\n\tbuild\tCGO_ENABLED=0\n"
            return subprocess.CompletedProcess(args, 0, stdout=output)

        with patch.object(module, "docker", side_effect=docker):
            return module.proof_binary("fixture", {"binary": "/binary", "revision": "fixture-revision"},
                                       self.directory, "fixture-builder")

    def test_rejects_old_actual_application_compiler(self):
        for compiler in ("go1.25.11", "go1.27.3"):
            with self.subTest(compiler=compiler), self.assertRaises(AssertionError):
                self.execute(compiler=compiler)

    def test_rejects_source_evidence_not_bound_to_actual_binary(self):
        with self.assertRaises(AssertionError):
            self.execute(claimed_hash="0" * 64)

    def test_accepts_actual_patched_binary_bound_to_source_evidence(self):
        self.assertIn("go1.27.4", self.execute()["actual_elf_buildinfo"])
        self.assertFalse((self.directory / "application.elf").exists())


class RuntimeIdentityTest(unittest.TestCase):
    def fixture(self):
        return {"Id": "local-descriptor-id", "Architecture": "amd64", "Os": "linux",
                "Created": "2026-11-16T00:00:00Z",
                "RootFS": {"Type": "layers", "Layers": ["sha256:" + "a" * 64]},
                "Config": {"Entrypoint": ["/application"], "User": "1000",
                           "Env": ["FIXTURE=original"], "Healthcheck": {"Test": ["CMD", "health"]}}}

    def test_accepts_identical_runtime_with_a_different_local_descriptor_id(self):
        accepted = self.fixture()
        pulled = {**accepted, "Id": "platform-manifest-or-config-id", "RepoTags": []}
        self.assertEqual(module.runtime_identity(accepted), module.runtime_identity(pulled))

    def test_rejects_changed_filesystem_or_any_execution_configuration(self):
        accepted = self.fixture()
        for field in ("RootFS", "Config"):
            pulled = json.loads(json.dumps(accepted))
            if field == "RootFS":
                pulled[field]["Layers"] = ["sha256:" + "b" * 64]
            else:
                pulled[field]["User"] = "0"
            with self.subTest(field=field):
                self.assertNotEqual(module.runtime_identity(accepted), module.runtime_identity(pulled))

    def test_rejects_missing_or_wrong_platform_identity(self):
        for change in ({"Architecture": "arm64"}, {"Config": None}, {"RootFS": {"Type": "layers", "Layers": []}}):
            with self.subTest(change=change), self.assertRaises(AssertionError):
                module.runtime_identity({**self.fixture(), **change})


class SecurityOverlayTest(unittest.TestCase):
    def fixture(self):
        patch = b"synthetic module patch"
        security = {"patch": {"sha256": hashlib.sha256(patch).hexdigest(),
                              "module_sha256": {"go.mod": "mod-hash", "go.sum": "sum-hash"}},
                    "modules": {"golang.org/x/net": "v0.62.0"}, "govulncheck": "v1.9.0"}
        app = {"security": security}
        source = {"security": json.loads(json.dumps(security)),
                  "module_sha256": security["patch"]["module_sha256"],
                  "binary_sha256": "actual-elf", "advisory_binary_sha256": "actual-elf",
                  "advisory_scans": {mode: {"config": scanner_config(mode),
                                          "symbol_or_package_wide_findings": 0}
                                     for mode in ("source", "binary")}}
        info = "\tdep\tgolang.org/x/net\tv0.62.0\th1:fixture\n"
        return app, source, info, patch

    def test_requires_actual_elf_dependencies_not_only_claimed_source(self):
        app, source, info, patch = self.fixture()
        module.validate_security_proof(app, source, info, patch)
        with self.assertRaises(AssertionError):
            module.validate_security_proof(app, source, info.replace("v0.62.0", "v0.47.0"), patch)

    def test_rejects_changed_patch_and_different_scanned_binary(self):
        app, source, info, patch = self.fixture()
        with self.assertRaises(AssertionError):
            module.validate_security_proof(app, source, info, patch + b"changed")
        source["advisory_binary_sha256"] = "different-elf"
        with self.assertRaises(AssertionError):
            module.validate_security_proof(app, source, info, patch)

    def test_rejects_retained_advisory_matches(self):
        app, source, info, patch = self.fixture()
        source["advisory_scans"]["source"]["symbol_or_package_wide_findings"] = 1
        with self.assertRaises(AssertionError):
            module.validate_security_proof(app, source, info, patch)

    def test_requires_exact_reviewed_module_only_findings_when_declared(self):
        app, source, info, patch = self.fixture()
        reviewed = {"source": [], "binary": ["GO-reviewed"]}
        app["security"]["reviewed_module_advisory_ids"] = reviewed
        source["security"] = json.loads(json.dumps(app["security"]))
        for mode, ids in reviewed.items():
            source["advisory_scans"][mode]["module_advisory_ids"] = list(ids)
        module.validate_security_proof(app, source, info, patch)
        source["advisory_scans"]["binary"]["module_advisory_ids"].append("GO-unreviewed")
        with self.assertRaises(AssertionError):
            module.validate_security_proof(app, source, info, patch)


class AdvisoryProtocolTest(unittest.TestCase):
    def test_retains_module_only_findings_without_claiming_universal_clearance(self):
        text = json.dumps({"config": scanner_config("binary")}) + "\n" + json.dumps(
            {"finding": {"osv": "GO-fixture", "trace": [{"module": "module-only"}]}})
        result = build.advisory_summary(text, "binary", "v1.9.0")
        self.assertEqual(result["module_advisory_ids"], ["GO-fixture"])

    def test_rejects_symbol_or_wildcard_in_success_shaped_json(self):
        for function in ("NewDict", "example/package/*", "main"):
            text = json.dumps({"config": scanner_config("binary")}) + json.dumps(
                {"finding": {"osv": "GO-fixture", "trace": [{"function": function}]}})
            with self.subTest(function=function), self.assertRaises(AssertionError):
                build.advisory_summary(text, "binary", "v1.9.0")

    def test_reviewed_module_id_does_not_exempt_a_main_function_finding(self):
        config = json.dumps({"config": scanner_config("binary")})
        trace = {"module": "k8s.io/kubernetes", "version": "v1.36.6+dirty",
                 "package": "k8s.io/kubernetes/cmd/kubectl", "function": "main"}
        for advisory in ("GO-2025-3521", "GO-2025-3547"):
            with self.subTest(advisory=advisory), self.assertRaises(AssertionError):
                build.advisory_summary(
                    config + json.dumps({"finding": {"osv": advisory, "trace": [trace]}}),
                    "binary", "v1.9.0",
                )

    def test_rejects_missing_or_weakened_scanner_configuration(self):
        with self.assertRaises(ValueError):
            build.advisory_summary("", "binary", "v1.9.0")
        config = {**scanner_config("binary"), "scan_level": "module"}
        with self.assertRaises(AssertionError):
            build.advisory_summary(json.dumps({"config": config}), "binary", "v1.9.0")


if __name__ == "__main__":
    unittest.main()
