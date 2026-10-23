#!/usr/bin/env python3
"""Check supplied PEM bundles for affected keys; do not infer runtime safety."""

from __future__ import annotations

import argparse
import hashlib
import json
import re
import sys
from pathlib import Path

from cryptography import x509
from cryptography.hazmat.primitives import hashes, serialization


DEFAULT_POLICY = Path(__file__).resolve().parents[1] / "security" / "emudhra-2026.json"


def load_policy(path: Path) -> dict[str, str]:
    entries = json.loads(path.read_text(encoding="utf-8"))["blocked_spkis"]
    if not isinstance(entries, list) or not entries:
        raise ValueError("policy must contain a nonempty blocked_spkis list")
    denied: dict[str, str] = {}
    for entry in entries:
        name, digest = entry["name"], entry["sha256"]
        if not isinstance(name, str) or not name.strip():
            raise ValueError("policy names must be nonempty strings")
        if not isinstance(digest, str) or not re.fullmatch(r"[0-9A-Fa-f]{64}", digest):
            raise ValueError(f"invalid SPKI SHA-256 for {name}")
        digest = digest.upper()
        if digest in denied:
            raise ValueError(f"duplicate policy SPKI SHA-256 for {name}")
        denied[digest] = name
    return denied


def scan_bundle(data: bytes, denied: dict[str, str]) -> dict:
    certificates = x509.load_pem_x509_certificates(data)
    if not certificates:
        raise ValueError("bundle contains no PEM certificates")
    matches = []
    for certificate in certificates:
        spki = certificate.public_key().public_bytes(
            serialization.Encoding.DER,
            serialization.PublicFormat.SubjectPublicKeyInfo,
        )
        digest = hashlib.sha256(spki).hexdigest().upper()
        if digest in denied:
            matches.append(
                {
                    "policy_name": denied[digest],
                    "spki_sha256": digest,
                    "certificate_sha256": certificate.fingerprint(hashes.SHA256())
                    .hex()
                    .upper(),
                }
            )
    return {
        "bundle_sha256": hashlib.sha256(data).hexdigest(),
        "certificate_count": len(certificates),
        "denied_certificates": matches,
    }


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument(
        "--bundle", type=Path, action="append", required=True, help="PEM file, or - for stdin"
    )
    parser.add_argument("--policy", type=Path, default=DEFAULT_POLICY)
    args = parser.parse_args(argv)
    reports = []
    try:
        if args.bundle.count(Path("-")) > 1:
            raise ValueError("stdin can only be supplied once")
        denied = load_policy(args.policy)
        for path in args.bundle:
            data = sys.stdin.buffer.read() if path == Path("-") else path.read_bytes()
            reports.append({"bundle": str(path), **scan_bundle(data, denied)})
    except (OSError, ValueError, KeyError, TypeError) as error:
        print(f"Trust-bundle verification failed: {error}", file=sys.stderr)
        return 2
    print(
        json.dumps(
            {
                "scope": "Supplied PEM bundles only; embedded/runtime trust and chain rejection are not verified.",
                "bundles": reports,
            },
            indent=2,
        )
    )
    return int(any(report["denied_certificates"] for report in reports))


if __name__ == "__main__":
    raise SystemExit(main())
