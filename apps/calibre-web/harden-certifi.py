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


PACKAGE = Path("/lsiopy/lib/python3.13/site-packages/certifi")
PACKAGE_HASHES = {
    "__init__.py": "7a52e5f4205f9b3d12a31898ceaa7e3f6837d19cc230700bcae1a9d91d1486c1",
    "__main__.py": "c410688fdd394d45812d118034e71fee88ba7beddd30fe1c1281bd3b232cd758",
    "cacert.pem": "9cc2a774b5198dcff14d9be1e66091f538975d867ce029a96bce15a55dfd730f",
    "core.py": "5c55f2727746e697f7edac9e17c377d8752e0da7ecca191531b3b80403d61dad",
    "py.typed": "e3b0c44298fc1c149afbf4c8996fb92427ae41e4649b934ca495991b7852b855",
}
PIP_PACKAGE = Path("/lsiopy/lib/python3.13/site-packages/pip/_vendor/certifi")
PIP_PACKAGE_HASHES = {
    "__init__.py": "f96d51ff2f1609a4a44f5b5d8eec47ff34c1658d581fac5081d3759db05a8ce1",
    "__main__.py": "d64dc2afde6f0b1c464460e58eb5b7c0c76965d2f73617f4bb59fe936a9db026",
    "LICENSE": "e93716da6b9c0d5a4a1df60fe695b370f0695603d21f6f83f053e42cfc10caf7",
    "cacert.pem": "bbc7e9c01d7551bb8a159b5dedd989b8ee3ce105aff522b68eb1b01bf854cab0",
    "core.py": "82efc40952359b746ad32b69b0d13ad6181019c29a380b7d46cf46f0ab1308e2",
    "py.typed": "e3b0c44298fc1c149afbf4c8996fb92427ae41e4649b934ca495991b7852b855",
}
VENDOR_STARTUP_HASHES = {
    "etc/s6-overlay/s6-rc.d/cwa-init/run":
        "b8cffb33376ea7f92b374742987faff7a6d104fded3d1110fbc10853894d2357",
    "etc/s6-overlay/s6-rc.d/svc-calibre-web-automated/run":
        "44bf6a134eb02bd59b3ab3a1e63c06b5c245be737e6aae75956e965bfc2c4f6e",
    "etc/s6-overlay/s6-rc.d/calibre-binaries-setup/run":
        "a02ff2265a48d66153d5c008c230bd89038a766e7994874a2adcb12f37e77c8b",
    "etc/s6-overlay/s6-rc.d/cwa-auto-library/run":
        "0f740e2eb03007d35998752f9ac0c6a146537bad3d0e2b02669023353a433df7",
}
PEM = re.compile(rb"-----BEGIN CERTIFICATE-----.*?-----END CERTIFICATE-----", re.S)
SYSTEM_BUNDLE_HASH = "9481fcd95f41b221f02f14d896535fe500bec539bc563c4cdca1acee483a8bdd"
HASH_NAME = re.compile(r"([0-9a-f]{8})\.([0-9]+)")


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


def build_overlay(source, destination, denied, expected_hashes, package_name="certifi"):
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
    target = destination / package_name
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


def build_system_overlay(source, destination, denied, expected_hash):
    original = (source / "ca-certificates.crt").read_bytes()
    if hashlib.sha256(original).hexdigest() != expected_hash:
        raise ValueError("system CA bundle differs from audited pinned image")
    bundle, counts = sanitize(original, denied)
    expected = Counter(der for _, spki, der in certificates(original) if spki not in denied)
    groups = {}
    actual = Counter()
    for path in source.iterdir():
        match = HASH_NAME.fullmatch(path.name)
        if not match:
            continue
        content = path.read_bytes()
        records = certificates(content)
        if len(records) != 1:
            raise ValueError("system subject-hash input must contain one certificate")
        _, spki, der = records[0]
        if spki not in denied:
            groups.setdefault(match[1], []).append((int(match[2]), content))
            actual[der] += 1
    if actual != expected:
        raise ValueError("hashed CA directory differs from audited bundle")
    target = destination / "system-certs"
    if target.exists() or target.is_symlink():
        raise ValueError("unexpected system CA overlay output")
    target.mkdir()
    (target / "ca-certificates.crt").write_bytes(bundle)
    for subject, group in groups.items():
        for index, (_, content) in enumerate(sorted(group)):
            (target / f"{subject}.{index}").write_bytes(content)
    return counts


def main():
    denied = policy(Path(__file__).with_name("blocked-spkis.sha256"))
    for name, expected in VENDOR_STARTUP_HASHES.items():
        if hashlib.sha256(Path("/" + name).read_bytes()).hexdigest() != expected:
            raise ValueError("vendor startup differs from audited pinned CWA image")
    counts = build_overlay(PACKAGE, Path("/ca-output"), denied, PACKAGE_HASHES)
    pip = build_overlay(PIP_PACKAGE, Path("/ca-output"), denied, PIP_PACKAGE_HASHES, "pip-certifi")
    system = build_system_overlay(
        Path("/etc/ssl/certs"), Path("/ca-output"), denied, SYSTEM_BUNDLE_HASH,
    )
    print(json.dumps({
        "calibre_certifi_overlay_verified": True, **counts, "system_ca": system, "pip_certifi": pip,
    }), flush=True)


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
