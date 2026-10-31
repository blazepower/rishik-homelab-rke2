#!/usr/bin/env python3
"""Focused regression tests for owned image rebuilds and CA acceptance."""

from __future__ import annotations

import hashlib
import importlib.util
import json
import re
import subprocess
import sys
import unittest
from pathlib import Path
from unittest.mock import patch


ROOT = Path(__file__).resolve().parents[2]
sys.dont_write_bytecode = True
SPEC = importlib.util.spec_from_file_location(
    "owned_ca", ROOT / ".github" / "scripts" / "verify-owned-image-ca.py"
)
CA = importlib.util.module_from_spec(SPEC)
SPEC.loader.exec_module(CA)
WORKFLOWS = ("build-hardcover-sync.yaml",)
CERTIFICATE = b"-----BEGIN CERTIFICATE-----\nfixture\n-----END CERTIFICATE-----\n"


class OwnedBuildTests(unittest.TestCase):
    def test_freshness_and_immutable_output(self):
        for name in WORKFLOWS:
            with self.subTest(workflow=name):
                text = (ROOT / ".github" / "workflows" / name).read_text()
                self.assertIn("  workflow_dispatch:\n", text)
                self.assertIn("      force_rebuild:\n", text)
                self.assertIn("        type: boolean\n", text)
                self.assertIn("        id: build\n", text)
                self.assertIn("          pull: true\n", text)
                self.assertIn("          no-cache: ${{ inputs.force_rebuild || false }}", text)
                self.assertIn("@${{ steps.build.outputs.digest }}", text)
                self.assertIn('verify-owned-image-ca.py "$IMAGE"', text)
                self.assertIn("Test owned image build guardrails", text)
                self.assertIn("'.github/scripts/verify-owned-image-ca.py'", text)

    def test_fixed_toolchain_and_package_floors(self):
        hardcover = (ROOT / "apps" / "hardcover-sync" / "src" / "Dockerfile").read_text()
        self.assertIn("FROM golang:1.27.3-bookworm AS build", hardcover)
        self.assertIn("ge 20250419~deb12u2", hardcover)

    def test_retired_cooklang_preserves_archived_configuration_and_certificates(self):
        resources = (ROOT / "apps/cooklang/kustomization.yaml").read_text()
        self.assertNotIn("helmrelease.yaml", resources)
        self.assertNotIn("ingress.yaml", resources)
        self.assertNotIn("ingress-cooklang-tailscale.yaml", resources)
        self.assertIn("pvc-sync-config.yaml", resources)
        retained = (ROOT / "apps/cooklang/pvc-sync-config.yaml").read_text()
        self.assertIn("helm.sh/resource-policy: keep", retained)
        self.assertIn("kustomize.toolkit.fluxcd.io/prune: disabled", retained)
        self.assertIn("name: cooklang-sync-config", retained)
        self.assertIn("storageClassName: longhorn", retained)
        self.assertIn("storage: 100Mi", retained)
        for filename, secret in (
            ("certificate.yaml", "cooklang-tls"),
            ("ingress-novnc.yaml", "cooklang-vnc-tls"),
        ):
            certificate = (ROOT / "apps/cooklang" / filename).read_text()
            self.assertEqual(re.findall(r"(?m)^kind: (\S+)", certificate), ["Certificate"])
            self.assertIn(f"secretName: {secret}", certificate)
            self.assertIn("kustomize.toolkit.fluxcd.io/prune: disabled", certificate)
            self.assertIn(filename, resources)
        self.assertFalse((ROOT / "charts/cooklang/Chart.yaml").exists())
        homepage = (ROOT / "charts/homepage/values.yaml").read_text()
        self.assertNotIn("cooklang.homelab", homepage)
        for path in (
            ".github/workflows/cooklang-build.yaml", "apps/cooklang/Dockerfile",
            "apps/cooklang/helmrelease.yaml", "apps/cooklang/ingress.yaml",
            "apps/cooklang/ingress-cooklang-tailscale.yaml",
            ".github/workflows/cook-sync-build.yaml", "apps/cooklang/Dockerfile.sync",
            "apps/cooklang/entrypoint-sync.sh", "charts/cooklang/templates/service-vnc.yaml",
            "charts/cooklang/templates/service-novnc.yaml",
        ):
            self.assertFalse((ROOT / path).exists(), path)

    def test_distroless_replaces_entire_trust_directories_before_final_runtime(self):
        cases = (
            ("apps/hardcover-sync/src/Dockerfile", "build", "/app/hardcover-sync"),
        )
        for path, stage, executable in cases:
            with self.subTest(dockerfile=path):
                text = (ROOT / path).read_text()
                seed = "COPY --from=gcr.io/distroless/static-debian12:nonroot / /runtime-root/"
                remove = "rm -rf /runtime-root/etc/ssl/certs /runtime-root/usr/share/ca-certificates"
                copy_bundle_directory = "cp -a /etc/ssl/certs /runtime-root/etc/ssl/"
                copy_source_directory = "cp -a /usr/share/ca-certificates /runtime-root/usr/share/"
                self.assertIn("dpkg --compare-versions", text)
                self.assertIn("ge 20250419~deb12u2", text)
                self.assertLess(text.index("ge 20250419~deb12u2"), text.index(seed))
                self.assertLess(text.index(seed), text.index(remove))
                self.assertLess(text.index(remove), text.index(copy_bundle_directory))
                self.assertLess(text.index(remove), text.index(copy_source_directory))
                self.assertLess(text.index(copy_bundle_directory), text.index("FROM scratch"))
                self.assertLess(text.index(copy_source_directory), text.index("FROM scratch"))
                final = text.split("FROM scratch", 1)[1]
                self.assertIn(f"COPY --from={stage} /runtime-root/ /", final)
                self.assertIn("USER 65532:65532", final)
                self.assertIn("ENV SSL_CERT_FILE=/etc/ssl/certs/ca-certificates.crt", final)
                self.assertIn(f'ENTRYPOINT ["{executable}"]', final)
                self.assertNotIn("gcr.io/distroless/", final)

    def test_quarantine_check_then_branch_gated_promotion(self):
        for name in WORKFLOWS:
            with self.subTest(workflow=name):
                text = (ROOT / ".github" / "workflows" / name).read_text()
                steps = re.split(r"(?=^      - name: )", text, flags=re.MULTILINE)[1:]
                build = next(s for s in steps if "        id: build\n" in s)
                check = next(s for s in steps if "name: Check quarantined runtime CA bundle" in s)
                candidate = next(s for s in steps if "name: Output passed candidate" in s)
                promote = next(s for s in steps if "        id: promote\n" in s)
                output = next(s for s in steps if "name: Output immutable production" in s)
                self.assertIn(
                    "tags: ${{ env.REGISTRY }}/${{ env.IMAGE_NAME }}:"
                    "ca-candidate-${{ github.run_id }}-${{ github.run_attempt }}",
                    build,
                )
                self.assertNotIn("steps.meta.outputs.tags", build)
                self.assertIn("labels: ${{ steps.meta.outputs.labels }}", build)
                self.assertLess(steps.index(build), steps.index(check))
                self.assertLess(steps.index(check), steps.index(candidate))
                self.assertLess(steps.index(candidate), steps.index(promote))
                self.assertLess(steps.index(promote), steps.index(output))
                before_promotion = "".join(steps[:steps.index(promote)])
                self.assertNotIn("imagetools create", before_promotion)
                self.assertNotIn("docker push", before_promotion)
                self.assertNotIn("if:", check)
                self.assertNotIn("continue-on-error", check)
                self.assertIn('PASSED CANDIDATE $IMAGE', candidate)
                self.assertIn("steps.build.outputs.digest", candidate)
                self.assertIn(
                    "if: github.ref == format('refs/heads/{0}', github.event.repository.default_branch)"
                    " || (github.event_name == 'push' && startsWith(github.ref, 'refs/tags/v'))",
                    promote,
                )
                self.assertNotIn("always()", promote)
                self.assertIn("RELEASE_TAGS: ${{ steps.meta.outputs.tags }}", promote)
                self.assertIn('set -euo pipefail', promote)
                self.assertIn('imagetools create --prefer-index=false --metadata-file', promote)
                self.assertIn('"${args[@]}" "$IMAGE"', promote)
                self.assertIn('["containerimage.descriptor"]["digest"]', promote)
                self.assertIn("if: steps.promote.outcome == 'success'", output)
                self.assertIn("steps.promote.outputs.digest", output)

    def test_empty_bundle_fails_closed(self):
        with self.assertRaisesRegex(ValueError, "small"):
            CA.check_bundle(b"")

    def test_allowed_bundle(self):
        output = subprocess.CompletedProcess([], 0, stdout=b"allowed-key")
        with patch.object(CA.subprocess, "run", return_value=output):
            self.assertEqual(CA.check_bundle(CERTIFICATE * 10), 10)

    def test_shared_policy_keys_and_cross_sign_paths_rejected(self):
        denied = CA.load_policy()
        self.assertTrue({
            "AAA Certificate Services", "emSign Root TLS CA - G3", "emSign ECC Root CA - G3"
        }.issubset(set(denied.values())))
        for digest, name in denied.items():
            with self.subTest(root=name):
                output = subprocess.CompletedProcess([], 0, stdout=b"denied-key")
                hashed = unittest.mock.Mock()
                hashed.hexdigest.return_value = digest
                with patch.object(CA.subprocess, "run", return_value=output), \
                        patch.object(hashlib, "sha256", return_value=hashed):
                    with self.assertRaisesRegex(ValueError, name):
                        CA.check_bundle(CERTIFICATE * 10)

    def test_invalid_certificate_fails_closed(self):
        with patch.object(CA.subprocess, "run", side_effect=subprocess.CalledProcessError(1, "openssl")):
            with self.assertRaises(subprocess.CalledProcessError):
                CA.check_bundle(CERTIFICATE * 10)

    def test_mutable_image_reference_rejected(self):
        with self.assertRaisesRegex(ValueError, "immutable"):
            CA.check_image("ghcr.io/blazepower/cooklang:latest")

    def test_unsupported_bundle_path_rejected_before_registry_access(self):
        with patch.object(CA.subprocess, "run") as docker:
            with self.assertRaisesRegex(ValueError, "bundle path"):
                CA.check_image(
                    "ghcr.io/blazepower/cooklang@sha256:" + "a" * 64,
                    bundle_path="/unexpected/path",
                )
            docker.assert_not_called()

    def test_platform_index_resolves_distinct_immutable_images(self):
        image = "ghcr.io/blazepower/cooklang@sha256:" + "a" * 64
        manifest = {
            "schemaVersion": 2,
            "mediaType": "application/vnd.oci.image.index.v1+json",
            "manifests": [
                {
                    "mediaType": "application/vnd.oci.image.manifest.v1+json",
                    "digest": "sha256:" + digest * 64,
                    "platform": {"os": "linux", "architecture": architecture},
                }
                for architecture, digest in (("amd64", "b"), ("arm64", "c"), ("unknown", "d"))
            ],
        }
        output = subprocess.CompletedProcess([], 0, stdout=json.dumps(manifest))
        with patch.object(CA.subprocess, "run", return_value=output):
            for architecture, digest in (("amd64", "b"), ("arm64", "c")):
                self.assertEqual(
                    CA.resolve_platform_image(image, f"linux/{architecture}"),
                    "ghcr.io/blazepower/cooklang@sha256:" + digest * 64,
                )
            manifest["manifests"] = manifest["manifests"][:1]
            output.stdout = json.dumps(manifest)
            with self.assertRaisesRegex(ValueError, "exactly one"):
                CA.resolve_platform_image(image, "linux/arm64")
            manifest["manifests"] *= 2
            output.stdout = json.dumps(manifest)
            with self.assertRaisesRegex(ValueError, "exactly one"):
                CA.resolve_platform_image(image, "linux/amd64")
            manifest["manifests"] = manifest["manifests"][:1]
            manifest["manifests"][0]["digest"] = "latest"
            output.stdout = json.dumps(manifest)
            with self.assertRaisesRegex(ValueError, "Invalid"):
                CA.resolve_platform_image(image, "linux/amd64")

    def test_single_manifest_and_invalid_manifest(self):
        image = "ghcr.io/blazepower/cooklang@sha256:" + "a" * 64
        output = subprocess.CompletedProcess([], 0, stdout=json.dumps({
            "schemaVersion": 2, "mediaType": "application/vnd.oci.image.manifest.v1+json",
        }))
        with patch.object(CA.subprocess, "run", return_value=output):
            self.assertEqual(CA.resolve_platform_image(image, "linux/amd64"), image)
            for invalid in ([], {}, {"schemaVersion": 2, "mediaType": "unknown"}):
                output.stdout = json.dumps(invalid)
                with self.assertRaises(ValueError):
                    CA.resolve_platform_image(image, "linux/amd64")

    def test_wrong_actual_platform_rejected_before_container_creation(self):
        image = "ghcr.io/blazepower/cooklang@sha256:" + "a" * 64
        with patch.object(CA, "resolve_platform_image", return_value=image), \
                patch.object(CA.subprocess, "run", side_effect=[
                    subprocess.CompletedProcess([], 0),
                    subprocess.CompletedProcess([], 0, stdout="linux/amd64\n"),
                ]) as docker:
            with self.assertRaisesRegex(ValueError, "Expected linux/arm64"):
                CA.check_image(image, "linux/arm64")
            self.assertEqual(docker.call_count, 2)
        self.assertFalse(list(Path.cwd().glob(".owned-image-ca-check-*")))

    def test_image_is_only_created_and_cleaned_on_copy_failure(self):
        container = "a" * 64
        calls = []

        def docker(command, **kwargs):
            calls.append(command)
            if command[1] == "image":
                return subprocess.CompletedProcess(command, 0, stdout="linux/amd64\n")
            if command[1] == "create":
                return subprocess.CompletedProcess(command, 0, stdout=container)
            if command[1] == "cp":
                raise subprocess.CalledProcessError(1, command)
            return subprocess.CompletedProcess(command, 0)

        image = "ghcr.io/blazepower/cooklang@sha256:" + "b" * 64
        with patch.object(CA, "resolve_platform_image", return_value=image), \
                patch.object(CA.subprocess, "run", side_effect=docker):
            with self.assertRaises(subprocess.CalledProcessError):
                CA.check_image("ghcr.io/blazepower/cooklang@sha256:" + "b" * 64)
        self.assertEqual([c[1] for c in calls], ["pull", "image", "create", "cp", "rm"])
        self.assertEqual(calls[2][-1], "/__ca_check_never_started__")
        self.assertIn("/etc/ssl/certs/ca-certificates.crt", calls[3][3])
        self.assertIn("-L", calls[3])
        self.assertEqual(calls[-1], ["docker", "rm", container])
        self.assertFalse(list(Path.cwd().glob(".owned-image-ca-check-*")))

    def test_certifi_path_is_followed_without_starting_the_image(self):
        container = "a" * 64
        bundle = "/usr/lib/python3/dist-packages/certifi/cacert.pem"
        calls = []

        def docker(command, **kwargs):
            calls.append(command)
            output = ""
            if command[1] == "image":
                output = "linux/amd64\n"
            elif command[1] == "create":
                output = container
            elif command[1] == "cp":
                Path(command[-1]).write_bytes(CERTIFICATE * 10)
            return subprocess.CompletedProcess(command, 0, stdout=output)

        image = "ghcr.io/blazepower/cook-sync@sha256:" + "b" * 64
        with patch.object(CA, "resolve_platform_image", return_value=image), \
                patch.object(CA.subprocess, "run", side_effect=docker), \
                patch.object(CA, "check_bundle", return_value=10):
            self.assertEqual(CA.check_image(image, bundle_path=bundle), 10)
        self.assertIn(f"{container}:{bundle}", calls[3])
        self.assertIn("-L", calls[3])
        self.assertEqual([c[1] for c in calls], ["pull", "image", "create", "cp", "rm"])
        self.assertFalse(list(Path.cwd().glob(".owned-image-ca-check-*")))


if __name__ == "__main__":
    unittest.main()
