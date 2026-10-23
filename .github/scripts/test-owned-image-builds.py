#!/usr/bin/env python3
"""Focused regression tests for owned image rebuilds and CA acceptance."""

from __future__ import annotations

import hashlib
import importlib.util
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
WORKFLOWS = ("cooklang-build.yaml", "cook-sync-build.yaml", "build-hardcover-sync.yaml")
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
        cooklang = (ROOT / ".github" / "workflows" / WORKFLOWS[0]).read_text()
        self.assertIn("--platform linux/arm64", cooklang)

    def test_fixed_toolchain_and_package_floors(self):
        hardcover = (ROOT / "apps" / "hardcover-sync" / "src" / "Dockerfile").read_text()
        sync = (ROOT / "apps" / "cooklang" / "Dockerfile.sync").read_text()
        self.assertIn("FROM golang:1.27.3-bookworm AS build", hardcover)
        self.assertIn("ge 20250419~deb12u2", hardcover)
        self.assertIn("FROM ubuntu:22.04", sync)
        self.assertIn("ge 20260816ubuntu0.22.04.1", sync)

    def test_cook_sync_preserves_supported_native_abi_and_app_version(self):
        sync = (ROOT / "apps" / "cooklang" / "Dockerfile.sync").read_text()
        self.assertLess(sync.index("ge 20260816ubuntu0.22.04.1"), sync.index("RUN curl"))
        self.assertIn("ARG COOK_DESKTOP_VERSION=0.2.5", sync)
        self.assertIn("    libwebkit2gtk-4.0-37 \\", sync)
        self.assertIn("apt-get install -y --no-install-recommends /opt/cook-desktop.deb", sync)
        self.assertIn('test "$(dpkg-query -W -f=\'${Version}\' cook-desktop)" = "${COOK_DESKTOP_VERSION}"', sync)
        self.assertIn("ldd /usr/bin/cook-desktop > /opt/cook-desktop.ldd", sync)
        self.assertIn("! grep -q 'not found' /opt/cook-desktop.ldd", sync)
        self.assertNotIn("apt-get install -f", sync)
        self.assertNotIn("; apt-get", sync)
        self.assertNotIn("ln -s", sync)
        self.assertNotIn("/tmp/", sync)
        self.assertNotRegex(sync, r"(?:noble|resolute|add-apt-repository|sources\.list)")

    def test_distroless_replaces_entire_trust_directories_before_final_runtime(self):
        cases = (
            ("apps/cooklang/Dockerfile", "certificates", "/usr/local/bin/cook"),
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
        cooklang = (ROOT / "apps" / "cooklang" / "Dockerfile").read_text()
        self.assertLess(cooklang.index("ge 20250419~deb12u2"), cooklang.index("curl -fsSL"))

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

    def test_image_is_only_created_and_cleaned_on_copy_failure(self):
        container = "a" * 64
        calls = []

        def docker(command, **kwargs):
            calls.append(command)
            if command[1] == "create":
                return subprocess.CompletedProcess(command, 0, stdout=container)
            if command[1] == "cp":
                raise subprocess.CalledProcessError(1, command)
            return subprocess.CompletedProcess(command, 0)

        with patch.object(CA.subprocess, "run", side_effect=docker):
            with self.assertRaises(subprocess.CalledProcessError):
                CA.check_image("ghcr.io/blazepower/cooklang@sha256:" + "b" * 64)
        self.assertEqual([c[1] for c in calls], ["pull", "create", "cp", "rm"])
        self.assertEqual(calls[1][-1], "/__ca_check_never_started__")
        self.assertIn("-L", calls[2])
        self.assertEqual(calls[-1], ["docker", "rm", container])
        self.assertFalse(list(Path.cwd().glob(".owned-image-ca-check-*")))


if __name__ == "__main__":
    unittest.main()
