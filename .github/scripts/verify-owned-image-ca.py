#!/usr/bin/env python3
"""Check an owned image's system CA bundle without executing its entrypoint.

This is a fail-closed bundle check, not proof that every embedded runtime or
application-specific trust store is patched.
"""

from __future__ import annotations

import argparse
import hashlib
import json
import re
import shutil
import subprocess
import uuid
from pathlib import Path


POLICY = Path(__file__).resolve().parents[1] / "security" / "emudhra-2026.json"
BUNDLE_PATHS = (
    "/etc/ssl/certs/ca-certificates.crt",
    "/usr/lib/python3/dist-packages/certifi/cacert.pem",
)
CERTIFICATE = re.compile(
    rb"-----BEGIN CERTIFICATE-----.*?-----END CERTIFICATE-----", re.DOTALL
)


def load_policy() -> dict[str, str]:
    entries = json.loads(POLICY.read_text(encoding="utf-8"))["blocked_spkis"]
    if not isinstance(entries, list) or not entries:
        raise ValueError("Expected a nonempty SPKI policy")
    denied = {}
    for entry in entries:
        digest, name = entry["sha256"], entry["name"]
        if not isinstance(digest, str) or not re.fullmatch(r"[a-fA-F0-9]{64}", digest):
            raise ValueError("Invalid policy SPKI SHA-256")
        if not isinstance(name, str) or not name.strip() or digest.lower() in denied:
            raise ValueError("Invalid or duplicate policy entry")
        denied[digest.lower()] = name
    return denied


def check_bundle(bundle: bytes) -> int:
    denied = load_policy()
    certificates = CERTIFICATE.findall(bundle)
    if len(certificates) < 10:
        raise ValueError("Missing or unexpectedly small system CA bundle")
    for certificate in certificates:
        public_key = subprocess.run(
            ["openssl", "x509", "-pubkey", "-noout"],
            input=certificate, capture_output=True, check=True,
        ).stdout
        der = subprocess.run(
            ["openssl", "pkey", "-pubin", "-outform", "DER"],
            input=public_key, capture_output=True, check=True,
        ).stdout
        if not der:
            raise ValueError("Certificate has no parseable public key")
        digest = hashlib.sha256(der).hexdigest()
        if digest in denied:
            raise ValueError(f"System bundle still trusts {denied[digest]} ({digest})")
    return len(certificates)


def resolve_platform_image(image: str, platform: str) -> str:
    manifest = json.loads(subprocess.run(
        ["docker", "buildx", "imagetools", "inspect", "--raw", image],
        capture_output=True, text=True, check=True,
    ).stdout)
    image_types = {
        "application/vnd.oci.image.manifest.v1+json",
        "application/vnd.docker.distribution.manifest.v2+json",
    }
    index_types = {
        "application/vnd.oci.image.index.v1+json",
        "application/vnd.docker.distribution.manifest.list.v2+json",
    }
    if not isinstance(manifest, dict) or manifest.get("schemaVersion") != 2:
        raise ValueError("Expected an image manifest or platform index")
    if manifest.get("mediaType") in image_types:
        return image
    if manifest.get("mediaType") not in index_types or not isinstance(manifest.get("manifests"), list):
        raise ValueError("Unsupported image manifest or platform index")
    system, architecture = platform.split("/")
    matches = [
        item for item in manifest["manifests"]
        if isinstance(item, dict) and isinstance(item.get("platform"), dict)
        and item["platform"].get("os") == system
        and item["platform"].get("architecture") == architecture
    ]
    if len(matches) != 1:
        raise ValueError(f"Expected exactly one image manifest for {platform}")
    digest = matches[0].get("digest")
    if (matches[0].get("mediaType") not in image_types
            or not isinstance(digest, str)
            or not re.fullmatch(r"sha256:[a-f0-9]{64}", digest)):
        raise ValueError(f"Invalid image manifest descriptor for {platform}")
    # The classic Docker store cannot retain two platforms under one index digest.
    return image.split("@", 1)[0] + "@" + digest


def check_image(
    image: str, platform: str = "linux/amd64", bundle_path: str = BUNDLE_PATHS[0],
) -> int:
    if not re.fullmatch(r"[a-z0-9./_-]+@sha256:[a-f0-9]{64}", image):
        raise ValueError("Supply the immutable published image@sha256:digest")
    if platform not in ("linux/amd64", "linux/arm64"):
        raise ValueError("Unsupported image platform")
    if bundle_path not in BUNDLE_PATHS:
        raise ValueError("Unsupported CA bundle path")
    platform_image = resolve_platform_image(image, platform)
    workspace = Path.cwd() / f".owned-image-ca-check-{uuid.uuid4().hex}"
    workspace.mkdir()
    container = None
    try:
        subprocess.run(["docker", "pull", "--platform", platform, platform_image], check=True)
        actual_platform = subprocess.run(
            ["docker", "image", "inspect", "--format", "{{.Os}}/{{.Architecture}}", platform_image],
            capture_output=True, text=True, check=True,
        ).stdout.strip()
        if actual_platform != platform:
            raise ValueError(f"Expected {platform}, pulled image is {actual_platform}")
        container = subprocess.run(
            ["docker", "create", "--platform", platform, platform_image, "/__ca_check_never_started__"],
            capture_output=True, text=True, check=True,
        ).stdout.strip()
        if not re.fullmatch(r"[a-f0-9]{64}", container):
            raise ValueError("Docker did not return a valid container ID")
        bundle = workspace / "ca-certificates.crt"
        subprocess.run(
            ["docker", "cp", "-L", f"{container}:{bundle_path}", str(bundle)],
            check=True,
        )
        if bundle.is_symlink() or not bundle.is_file():
            raise ValueError("Expected a regular system CA bundle")
        return check_bundle(bundle.read_bytes())
    finally:
        try:
            if container and re.fullmatch(r"[a-f0-9]{64}", container):
                subprocess.run(["docker", "rm", container], check=True, capture_output=True)
        finally:
            shutil.rmtree(workspace)


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("image", help="Published immutable image reference")
    parser.add_argument(
        "--platform", choices=("linux/amd64", "linux/arm64"), default="linux/amd64"
    )
    parser.add_argument("--bundle-path", choices=BUNDLE_PATHS, default=BUNDLE_PATHS[0])
    args = parser.parse_args()
    count = check_image(args.image, args.platform, args.bundle_path)
    print(f"Checked {count} roots at {args.bundle_path} in {args.image} ({args.platform}); denied SPKIs absent.")
    print("Other embedded/runtime-specific trust stores require separate verification.")


if __name__ == "__main__":
    main()
