"""Verify the shell-less proxy's exact vendor public-root snapshot, not compiled Go."""

import argparse
import hashlib
import importlib.util
import json
from pathlib import Path
import subprocess
import tarfile
import tempfile
import unittest

from cryptography import x509
from cryptography.hazmat.primitives import hashes, serialization
import yaml


ROOT = Path(__file__).resolve().parents[2]
APP = ROOT / "apps" / "rreading-glasses"
BUNDLE = APP / "public-ca" / "ca-certificates.crt"
RECORD = APP / "public-ca" / "source.json"
POLICY = ROOT / ".github" / "security" / "emudhra-2026.json"
spec = importlib.util.spec_from_file_location(
    "rreading_public_ca", Path(__file__).with_name("verify-emudhra-trust.py")
)
verifier = importlib.util.module_from_spec(spec)
spec.loader.exec_module(verifier)
DENIED = verifier.load_policy(POLICY)


def release():
    return yaml.safe_load((APP / "helmrelease.yaml").read_text())


def source_image():
    image = release()["spec"]["values"]["controllers"]["rreading-glasses"]["containers"]["app"]["image"]
    return image["repository"] + ":" + image["tag"]


def identities(content):
    return {
        cert.fingerprint(hashes.SHA256()).hex(): hashlib.sha256(
            cert.public_key().public_bytes(
                serialization.Encoding.DER, serialization.PublicFormat.SubjectPublicKeyInfo,
            )
        ).hexdigest().upper()
        for cert in x509.load_pem_x509_certificates(content)
    }


def policy_hash():
    return hashlib.sha256(POLICY.read_text(encoding="utf-8").encode("utf-8")).hexdigest()


def bundle_bytes():
    return BUNDLE.read_bytes().replace(b"\r\n", b"\n")


def factory(crane):
    digest = subprocess.check_output(
        [crane, "digest", "--platform=linux/amd64", source_image()], timeout=90,
    ).decode().strip()
    ref = source_image().split("@")[0].rsplit(":", 1)[0] + "@" + digest
    with subprocess.Popen(
        [crane, "export", ref, "-"], stdout=subprocess.PIPE, stderr=subprocess.PIPE,
    ) as process:
        stores = {}
        try:
            with tarfile.open(fileobj=process.stdout, mode="r|") as archive:
                for member in archive:
                    name = member.name.removeprefix("./").lstrip("/")
                    relevant = name.startswith((
                        "etc/ssl/", "etc/pki/", "usr/share/ca-certificates/",
                        "usr/local/share/ca-certificates/",
                    ))
                    if relevant and member.isfile() and name.endswith((".crt", ".pem")):
                        if member.size > 2 * 1024 * 1024:
                            raise ValueError("Unexpected oversized public store")
                        stores[name] = archive.extractfile(member).read()
            process.stdout.read()
            process.stderr.read()
            if process.wait(timeout=120):
                raise RuntimeError("Public immutable image export failed; no diagnostic payload retained")
        finally:
            if process.poll() is None:
                process.kill()
                process.wait()
    if set(stores) != {"etc/ssl/certs/ca-certificates.crt"}:
        raise ValueError("Factory public-store layout changed; reassessment required")
    return digest, stores["etc/ssl/certs/ca-certificates.crt"]


def refresh(digest, original):
    certs = x509.load_pem_x509_certificates(original)
    keys = identities(original)
    retained = [
        cert for cert in certs if keys[cert.fingerprint(hashes.SHA256()).hex()] not in DENIED
    ]
    if len(retained) < 10:
        raise ValueError("Too few unaffected vendor roots")
    header = (
        "# Public CA data derived from the immutable vendor image recorded in source.json.\n"
        "# Modification: remove only the versioned incident-policy SPKIs.\n"
        "# Mozilla CA data: Mozilla Public License 2.0, https://mozilla.org/MPL/2.0/.\n"
    ).encode() + original.split(b"-----BEGIN CERTIFICATE-----", 1)[0]
    data = header + b"".join(cert.public_bytes(serialization.Encoding.PEM) for cert in retained)
    BUNDLE.parent.mkdir(exist_ok=True)
    BUNDLE.write_bytes(data)
    RECORD.write_text(json.dumps({
        "source_image": source_image(), "selected_amd64": digest,
        "source_bundle_sha256": hashlib.sha256(original).hexdigest(),
        "filtered_bundle_sha256": hashlib.sha256(data).hexdigest(),
        "canonical_policy_sha256": policy_hash(),
        "factory_certificates": len(certs), "retained_certificates": len(retained),
        "coverage": "Exact vendor public PEM roots minus policy keys; compiled Go and custom TLS not certified",
    }, indent=2) + "\n", encoding="utf-8")


def verify_factory(digest, original):
    record = json.loads(RECORD.read_text())
    assert record["selected_amd64"] == digest
    assert record["source_bundle_sha256"] == hashlib.sha256(original).hexdigest()
    expected = {der for der, key in identities(original).items() if key not in DENIED}
    assert set(identities(bundle_bytes())) == expected, "Unaffected vendor identities changed"


class PublicRootSnapshotTests(unittest.TestCase):
    def test_snapshot_matches_policy_image_and_contains_no_denied_keys(self):
        record = json.loads(RECORD.read_text())
        self.assertEqual(record["source_image"], source_image())
        self.assertEqual(record["canonical_policy_sha256"], policy_hash())
        self.assertEqual(record["filtered_bundle_sha256"], hashlib.sha256(bundle_bytes()).hexdigest())
        scan = verifier.scan_bundle(bundle_bytes(), DENIED)
        self.assertEqual(scan["certificate_count"], record["retained_certificates"])
        self.assertGreater(scan["certificate_count"], 10)
        self.assertFalse(scan["denied_certificates"])

    def test_only_the_default_public_bundle_is_overlaid_readonly(self):
        values = release()["spec"]["values"]
        app = values["controllers"]["rreading-glasses"]["containers"]["app"]
        self.assertEqual(app["command"], ["/main"])
        self.assertEqual(app["args"], ["serve", "--verbose"])
        self.assertEqual(values["persistence"]["public-ca"]["advancedMounts"], {
            "rreading-glasses": {"app": [{
                "path": "/etc/ssl/certs/ca-certificates.crt",
                "subPath": "ca-certificates.crt", "readOnly": True,
            }]},
        })
        self.assertEqual(app["securityContext"]["runAsUser"], 1000)

    def test_secret_free_render_rewrites_the_configmap_reference(self):
        with tempfile.TemporaryDirectory(prefix="rreading-ca-") as directory:
            target = Path(directory)
            (target / "helmrelease.yaml").write_text((APP / "helmrelease.yaml").read_text())
            (target / "ca-certificates.crt").write_bytes(BUNDLE.read_bytes())
            (target / "kustomize-ca.json").write_text((APP / "kustomize-ca.json").read_text())
            (target / "kustomization.yaml").write_text(yaml.safe_dump({
                "apiVersion": "kustomize.config.k8s.io/v1beta1", "kind": "Kustomization",
                "namespace": "media", "resources": ["helmrelease.yaml"],
                "configurations": ["kustomize-ca.json"],
                "configMapGenerator": [{"name": "rreading-glasses-public-ca",
                                        "files": ["ca-certificates.crt"]}],
            }))
            result = subprocess.check_output(["kubectl", "kustomize", str(target)], timeout=60)
            docs = list(yaml.safe_load_all(result))
            config = next(doc for doc in docs if doc["kind"] == "ConfigMap")
            hr = next(doc for doc in docs if doc["kind"] == "HelmRelease")
            self.assertEqual(hr["spec"]["values"]["persistence"]["public-ca"]["name"],
                             config["metadata"]["name"])
            self.assertNotEqual(config["metadata"]["name"], "rreading-glasses-public-ca")


if __name__ == "__main__":
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--registry", action="store_true")
    parser.add_argument("--refresh", action="store_true")
    parser.add_argument("--crane", default="crane")
    args = parser.parse_args()
    current = factory(args.crane) if args.registry or args.refresh else None
    if args.refresh:
        refresh(*current)
    result = unittest.TextTestRunner(verbosity=2).run(
        unittest.defaultTestLoader.loadTestsFromTestCase(PublicRootSnapshotTests)
    )
    if not result.wasSuccessful():
        raise SystemExit(1)
    if current:
        verify_factory(*current)
        print("Exact unaffected vendor DER identities preserved; denied public roots absent.")
