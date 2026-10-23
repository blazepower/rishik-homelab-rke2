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


def check_image(image: str, platform: str = "linux/amd64") -> int:
    if not re.fullmatch(r"[a-z0-9./_-]+@sha256:[a-f0-9]{64}", image):
        raise ValueError("Supply the immutable published image@sha256:digest")
    workspace = Path.cwd() / f".owned-image-ca-check-{uuid.uuid4().hex}"
    workspace.mkdir()
    container = None
    try:
        subprocess.run(["docker", "pull", "--platform", platform, image], check=True)
        container = subprocess.run(
            ["docker", "create", "--platform", platform, image, "/__ca_check_never_started__"],
            capture_output=True, text=True, check=True,
        ).stdout.strip()
        if not re.fullmatch(r"[a-f0-9]{64}", container):
            raise ValueError("Docker did not return a valid container ID")
        bundle = workspace / "ca-certificates.crt"
        subprocess.run(
            ["docker", "cp", "-L", f"{container}:/etc/ssl/certs/ca-certificates.crt", str(bundle)],
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
    args = parser.parse_args()
    count = check_image(args.image, args.platform)
    print(f"Checked {count} system roots in {args.image} ({args.platform}); denied SPKIs absent.")
    print("Embedded/runtime-specific trust stores require separate verification.")


if __name__ == "__main__":
    main()
