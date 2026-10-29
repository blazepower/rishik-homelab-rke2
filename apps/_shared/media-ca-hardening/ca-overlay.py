"""Filter isolated public trust snapshots before Kubernetes permits application startup."""

import argparse
import base64
import hashlib
import json
from pathlib import Path, PurePosixPath
import re
import ssl


TREES = ("etc/ssl/certs", "usr/share/ca-certificates", "usr/local/share/ca-certificates")
PACKAGES = {
    "bazarr": (
        "app/bazarr/bin/libs/certifi",
        "lsiopy/lib/python3.12/site-packages/pip/_vendor/certifi",
    ),
    "prowlarr": (),
    "sabnzbd": (
        "lsiopy/lib/python3.14/site-packages/certifi",
        "lsiopy/lib/python3.14/site-packages/pip/_vendor/certifi",
    ),
    "bookshelf": (),
    "qbittorrent": (),
    "gluetun": (),
    "overseerr": (),
}
NATIVE_TREES = {
    name: ("etc/ssl", *TREES[1:])
    for name in ("bookshelf", "qbittorrent", "gluetun")
}
# The exact Seerr image has the Alpine bundle and alias, but no source trees.
NATIVE_TREES["overseerr"] = ("etc/ssl",)
PEM = re.compile(rb"-----BEGIN CERTIFICATE-----\s+([A-Za-z0-9+/=\r\n]+)-----END CERTIFICATE-----")
HASH_NAME = re.compile(r"([0-9a-f]{8})\.([0-9]+)")


def policy_keys(path):
    values = path.read_text(encoding="ascii").splitlines()
    if len(values) != 11 or len(set(values)) != 11:
        raise ValueError("Exactly eleven unique policy keys are required")
    if any(not re.fullmatch(r"[A-F0-9]{64}", value) for value in values):
        raise ValueError("Invalid policy key")
    return set(values)


def tlv(data, offset, limit):
    start = offset
    if offset + 2 > limit:
        raise ValueError("Truncated DER object")
    tag, size = data[offset], data[offset + 1]
    offset += 2
    if size & 128:
        count = size & 127
        if not 1 <= count <= 4 or offset + count > limit or data[offset] == 0:
            raise ValueError("Invalid DER length")
        size = int.from_bytes(data[offset:offset + count], "big")
        offset += count
        if size < 128:
            raise ValueError("Noncanonical DER length")
    end = offset + size
    if end > limit:
        raise ValueError("DER object exceeds its parent")
    return tag, start, offset, end


def spki_digest(der):
    tag, _, body, end = tlv(der, 0, len(der))
    if tag != 0x30 or end != len(der):
        raise ValueError("Expected one DER certificate")
    tag, _, fields, tbs_end = tlv(der, body, end)
    if tag != 0x30:
        raise ValueError("Expected TBSCertificate")
    if der[fields] == 0xA0:
        fields = tlv(der, fields, tbs_end)[3]
    for expected_tag in (0x02, 0x30, 0x30, 0x30, 0x30):
        tag, _, _, fields = tlv(der, fields, tbs_end)
        if tag != expected_tag:
            raise ValueError("Unexpected TBSCertificate field")
    tag, start, _, finish = tlv(der, fields, tbs_end)
    if tag != 0x30:
        raise ValueError("Expected SubjectPublicKeyInfo")
    return hashlib.sha256(der[start:finish]).hexdigest().upper()


def certificates(content):
    matches = list(PEM.finditer(content))
    if not matches or len(matches) != content.count(b"-----BEGIN CERTIFICATE-----"):
        raise ValueError("Empty or malformed PEM certificate store")
    outside = PEM.sub(b"", content)
    if b"-----BEGIN" in outside or b"-----END" in outside:
        raise ValueError("Unsupported or malformed PEM object")
    result = []
    for match in matches:
        der = base64.b64decode(re.sub(rb"\s", b"", match[1]), validate=True)
        pem = ssl.DER_cert_to_PEM_cert(der).encode("ascii")
        context = ssl.SSLContext(ssl.PROTOCOL_TLS_CLIENT)
        context.load_verify_locations(cadata=pem.decode("ascii"))
        result.append((pem, spki_digest(der), hashlib.sha256(der).hexdigest()))
    return result


def resolved(root, relative, allowed):
    seen = set()
    while True:
        if relative in seen:
            raise ValueError("Cyclic trust-store alias")
        seen.add(relative)
        if not any(relative == prefix or relative.startswith(prefix + "/") for prefix in allowed):
            raise ValueError("Trust alias escapes the public trust snapshot")
        path = root / relative
        if not path.is_symlink():
            if not path.is_file():
                raise ValueError("Missing trust-store alias target: " + relative)
            return relative
        target = path.readlink().as_posix()
        parts = [] if target.startswith("/") else list(PurePosixPath(relative).parent.parts)
        for part in PurePosixPath(target).parts:
            if part in ("/", "."):
                continue
            if part == "..":
                if not parts:
                    raise ValueError("Trust alias escapes snapshot root")
                parts.pop()
            else:
                parts.append(part)
        relative = "/".join(parts)


def harden(root, app, denied, verify_only=False):
    root = root.absolute()
    if root.is_symlink() or not root.is_dir():
        raise ValueError("Trust snapshot root must be a real directory")
    required = [TREES[0] + "/ca-certificates.crt"] + [p + "/cacert.pem" for p in PACKAGES[app]]
    trees = NATIVE_TREES.get(app, TREES)
    if app == "overseerr" and any(
        (root / tree).exists() or (root / tree).is_symlink() for tree in TREES[1:]
    ):
        raise ValueError("Unexpected source tree in bundle-only Seerr snapshot")
    files, links = {}, {}
    for tree in trees:
        directory = root / tree
        if directory.is_symlink() or not directory.is_dir():
            raise ValueError("Missing or symlinked trust directory: " + tree)
        for path in directory.rglob("*"):
            relative = path.relative_to(root).as_posix()
            if path.is_symlink():
                links[relative] = resolved(root, relative, trees)
            elif path.is_file():
                if path.suffix in (".crt", ".pem") or HASH_NAME.fullmatch(path.name):
                    files[relative] = certificates(path.read_bytes())
    for relative in required:
        path = root / relative
        if path.is_symlink() or not path.is_file():
            raise ValueError("Required bundle is absent or symlinked: " + relative)
        files[relative] = certificates(path.read_bytes())
    for target in links.values():
        if target not in files:
            raise ValueError("Alias targets an uninspected certificate")
    for relative in required:
        if sum(key not in denied for _, key, _ in files[relative]) < 10:
            raise ValueError("Required bundle would contain fewer than ten unaffected roots")
    if app in NATIVE_TREES and app != "overseerr" and sum(
        key not in denied
        for relative, certs in files.items() if relative.startswith(TREES[1] + "/")
        for _, key, _ in certs
    ) < 10:
        raise ValueError("Required native public source store is empty or unexpectedly small")
    before = {fingerprint for certs in files.values() for _, key, fingerprint in certs if key not in denied}
    removed, deleted = 0, set()
    for relative, certs in files.items():
        kept = [pem for pem, key, _ in certs if key not in denied]
        count = len(certs) - len(kept)
        removed += count
        if verify_only and count:
            raise ValueError("Denied key remains: " + relative)
        if count:
            if kept:
                (root / relative).write_bytes(b"".join(kept))
            else:
                (root / relative).unlink()
                deleted.add(relative)
    if not verify_only:
        for relative, target in links.items():
            if target in deleted:
                (root / relative).unlink()
        # OpenSSL stops at gaps in a subject-hash sequence. Compact remaining
        # aliases/files without recomputing or changing their vendor hash.
        groups = {}
        for path in (root / TREES[0]).iterdir():
            match = HASH_NAME.fullmatch(path.name)
            if match:
                groups.setdefault(match[1], []).append((int(match[2]), path))
        for group in groups.values():
            ordered = sorted(group)
            if [index for index, _ in ordered] == list(range(len(ordered))):
                continue
            paths = {p.relative_to(root).as_posix() for _, p in ordered}
            if any(target in paths for alias, target in links.items() if alias not in paths):
                raise ValueError("Cannot compact a hash sequence referenced by another alias")
            staged = []
            for _, path in ordered:
                stage = path.with_name(".media-ca-" + path.name)
                if stage.exists() or stage.is_symlink():
                    raise ValueError("Unexpected hash staging path")
                path.rename(stage)
                staged.append((stage, path.name.split(".")[0]))
            for index, (stage, subject_hash) in enumerate(staged):
                stage.rename(stage.with_name(subject_hash + "." + str(index)))
    after = set()
    for directory in [root / t for t in trees] + [root / p for p in PACKAGES[app]]:
        for path in directory.rglob("*"):
            if path.is_symlink():
                target = resolved(root, path.relative_to(root).as_posix(), trees)
                path = root / target
            if not path.is_file() or not (
                path.suffix in (".crt", ".pem") or HASH_NAME.fullmatch(path.name)
            ):
                continue
            for _, key, fingerprint in certificates(path.read_bytes()):
                if key in denied:
                    raise ValueError("Denied key remains after filtering")
                after.add(fingerprint)
    if before != after:
        raise ValueError("Unaffected certificate preservation check failed")
    return {
        "app": app, "removed_certificate_copies": removed,
        "unaffected_certificates": len(after), "required_bundles": required,
        "coverage": "Public PEM stores only; native/compiled/custom chain rejection is not certified",
    }


if __name__ == "__main__":
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--root", type=Path, required=True)
    parser.add_argument("--app", choices=PACKAGES, required=True)
    parser.add_argument("--policy", type=Path, required=True)
    parser.add_argument("--verify-only", action="store_true")
    args = parser.parse_args()
    print(json.dumps(harden(args.root, args.app, policy_keys(args.policy), args.verify_only)))
