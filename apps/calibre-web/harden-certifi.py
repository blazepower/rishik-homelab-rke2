"""Build a read-only certifi package overlay for the pinned Calibre-Web image."""
from collections import Counter
import hashlib
import json
import os
from pathlib import Path
import re
import ssl
import sys

from cryptography import x509
from cryptography.hazmat.primitives.serialization import Encoding, PublicFormat


PACKAGE = Path("/lsiopy/lib/python3.12/site-packages/certifi")
PACKAGE_HASHES = {
    "__init__.py": "fbc96373bc3b7f28b92b28b4e9cd7f2f4946fe40ff27416f6d44daf261297734",
    "__main__.py": "c410688fdd394d45812d118034e71fee88ba7beddd30fe1c1281bd3b232cd758",
    "cacert.pem": "7cef540c6af3bf0a3c7d0070bd3fd7aae34f8aa805b408d1c27a01c8aeb305df",
    "core.py": "5c55f2727746e697f7edac9e17c377d8752e0da7ecca191531b3b80403d61dad",
    "py.typed": "e3b0c44298fc1c149afbf4c8996fb92427ae41e4649b934ca495991b7852b855",
}
PEM = re.compile(rb"-----BEGIN CERTIFICATE-----.*?-----END CERTIFICATE-----", re.S)


def policy(path):
    lines = path.read_text(encoding="ascii").splitlines()
    if len(lines) != 11 or len(set(lines)) != 11 or any(
        not re.fullmatch(r"[A-F0-9]{64}", line) for line in lines
    ):
        raise ValueError("expected all eleven unique policy SPKIs")
    return {line.lower() for line in lines}


def certificates(content):
    blocks = PEM.findall(content)
    outside = PEM.sub(b"", content)
    if not blocks or b"-----BEGIN" in outside or b"-----END" in outside:
        raise ValueError("empty, malformed, or unsupported PEM input")
    result = []
    for block in blocks:
        cert = x509.load_pem_x509_certificate(block)
        spki = cert.public_key().public_bytes(Encoding.DER, PublicFormat.SubjectPublicKeyInfo)
        result.append((block, hashlib.sha256(spki).hexdigest(),
                       hashlib.sha256(cert.public_bytes(Encoding.DER)).hexdigest()))
    return result


def sanitize(content, denied):
    before = certificates(content)
    output = PEM.sub(
        lambda match: b"" if hashlib.sha256(
            x509.load_pem_x509_certificate(match.group()).public_key().public_bytes(
                Encoding.DER, PublicFormat.SubjectPublicKeyInfo,
            )
        ).hexdigest() in denied else match.group(),
        content,
    )
    after = certificates(output)
    expected = Counter(der for _, spki, der in before if spki not in denied)
    actual = Counter(der for _, _, der in after)
    if len(after) < 10 or expected != actual or any(spki in denied for _, spki, _ in after):
        raise ValueError("filtered bundle failed exact unaffected-certificate preservation")
    return output, {"before": len(before), "after": len(after), "removed": len(before) - len(after)}


def build_overlay(source, destination, denied, expected_hashes):
    if source.is_symlink() or not source.is_dir():
        raise ValueError("unsupported certifi package directory")
    if {path.name for path in source.iterdir() if path.is_file()} != set(expected_hashes):
        raise ValueError("unexpected certifi package files")
    content = {}
    for name, expected in expected_hashes.items():
        path = source / name
        if path.is_symlink() or not path.is_file():
            raise ValueError("unsupported certifi package input")
        content[name] = path.read_bytes()
        if hashlib.sha256(content[name]).hexdigest() != expected:
            raise ValueError("certifi input differs from audited pinned image")
    content["cacert.pem"], counts = sanitize(content["cacert.pem"], denied)
    if destination.is_symlink() or not destination.is_dir():
        raise ValueError("overlay output must be the dedicated mounted directory")
    target = destination / "certifi"
    if target.is_symlink():
        raise ValueError("symlinked overlay package")
    target.mkdir(exist_ok=True)
    if any(path.name not in expected_hashes for path in target.iterdir()):
        raise ValueError("unexpected overlay output")
    for name, data in content.items():
        path = target / name
        if path.is_symlink():
            raise ValueError("symlinked overlay output")
        flags = os.O_WRONLY | os.O_CREAT | os.O_TRUNC | getattr(os, "O_NOFOLLOW", 0)
        descriptor = os.open(path, flags, 0o644)
        with os.fdopen(descriptor, "wb") as output:
            output.write(data)
        if path.read_bytes() != data:
            raise ValueError("overlay output verification failed")
    ssl.create_default_context(cafile=str(target / "cacert.pem"))
    return counts


def main():
    denied = policy(Path(__file__).with_name("blocked-spkis.sha256"))
    counts = build_overlay(PACKAGE, Path("/ca-output"), denied, PACKAGE_HASHES)
    print(json.dumps({"calibre_certifi_overlay_verified": True, **counts}), flush=True)


if __name__ == "__main__":
    try:
        main()
    except (OSError, ValueError) as error:
        print(
            f"Calibre certifi hardening failed ({type(error).__name__}: {error}); "
            "refusing application startup.",
            file=sys.stderr,
        )
        raise SystemExit(78)
