#!/usr/bin/env python3
"""Exercise the actual image policy with the native Kyverno CLI."""

import argparse
from pathlib import Path
import subprocess
import tempfile

import yaml

ROOT = Path(__file__).resolve().parents[2]
POLICY = ROOT / "infrastructure/policies/kyverno-policies/cluster-policies/disallow-latest-tag.yaml"
DIGEST = "sha256:" + "a" * 64
CASES = [
    (registry + "/example/app:1.2", True, True)
    for registry in (
        "docker.io", "index.docker.io", "ghcr.io", "quay.io", "gcr.io",
        "registry.k8s.io", "mcr.microsoft.com", "lscr.io",
    )
] + [
    ("busybox:1.37.0", True, True),
    ("library/busybox:1.37.0", True, True),
    ("ghcr.io/example/app:latest-alpine", True, True),
    ("ghcr.io/example/latest-tools:1.2", True, True),
    ("ghcr.io/example/app@" + DIGEST, True, True),
    ("ghcr.io/example/app:1.2@" + DIGEST, True, True),
    ("busybox@" + DIGEST, True, True),
    ("busybox:latest", False, True),
    ("busybox", False, True),
    ("ghcr.io/example/app", False, True),
    ("ghcr.io/example/app:latest", False, True),
    ("ghcr.io/example/app:latest@" + DIGEST, False, True),
    ("untrusted.example/app:1.2", True, False),
    ("untrusted.example/app@" + DIGEST, True, False),
    ("untrusted.example/app:1.2@" + DIGEST, True, False),
    ("ghcr.io.untrusted.example/app:1.2", True, False),
    ("ghcrXio/example/app:1.2", True, False),
    ("untrusted.example/ghcr.io/example/app:1.2", True, False),
    ("ghcr.io:5000/example/app:1.2", True, False),
    ("ghcr.io:5000/example/app", False, False),
    ("localhost:5000/example/app:latest", False, False),
]


def run(kyverno, policy=POLICY):
    resources, results = [], []
    for field in ("containers", "initContainers", "ephemeralContainers"):
        for index, (image, tag_ok, registry_ok) in enumerate(CASES):
            name = field.lower() + "-" + str(index)
            spec = {"containers": [{"name": "main", "image": "busybox:1.37.0"}]}
            spec[field] = [{"name": "tested", "image": image}]
            resources.append({
                "apiVersion": "v1", "kind": "Pod",
                "metadata": {"name": name, "namespace": "image-policy-test"}, "spec": spec,
            })
            for rule, allowed in (
                ("disallow-latest-tag", tag_ok), ("validate-trusted-registries", registry_ok),
            ):
                results.append({
                    "policy": "disallow-latest-tag", "rule": rule, "kind": "Pod",
                    "resources": [name],
                    "result": "pass" if allowed else "fail",
                })
    for kind, prefix in (("Deployment", "autogen-"), ("CronJob", "autogen-cronjob-")):
        for index, (image, tag_ok, registry_ok) in enumerate((
            ("ghcr.io/example/app@" + DIGEST, True, True),
            ("ghcr.io/example/app:latest", False, True),
            ("untrusted.example/app:1.2", True, False),
        )):
            name = kind.lower() + "-" + str(index)
            template = {"spec": {"containers": [{"name": "main", "image": image}]}}
            spec = {"template": template} if kind == "Deployment" else {
                "schedule": "0 0 * * *", "jobTemplate": {"spec": {"template": template}},
            }
            resources.append({
                "apiVersion": "apps/v1" if kind == "Deployment" else "batch/v1",
                "kind": kind, "metadata": {"name": name, "namespace": "image-policy-test"},
                "spec": spec,
            })
            for rule, allowed in (
                ("disallow-latest-tag", tag_ok), ("validate-trusted-registries", registry_ok),
            ):
                results.append({
                    "policy": "disallow-latest-tag", "rule": prefix + rule, "kind": kind,
                    "resources": [name],
                    "result": "pass" if allowed else "fail",
                })
    with tempfile.TemporaryDirectory(prefix="kyverno-image-policy-") as directory:
        directory = Path(directory)
        (directory / "resources.yaml").write_text(yaml.safe_dump_all(resources), encoding="utf-8")
        (directory / "kyverno-test.yaml").write_text(yaml.safe_dump({
            "apiVersion": "cli.kyverno.io/v1alpha1", "kind": "Test",
            "metadata": {"name": "image-tag-and-registry-rules"},
            "policies": [str(policy.resolve())], "resources": ["resources.yaml"],
            "results": results,
        }), encoding="utf-8")
        subprocess.run([
            kyverno, "test", str(directory), "--require-tests", "--fail-only", "--remove-color",
        ], check=True)
    print(f"PASS: {len(results)} native rule expectations across {len(resources)} resources")


if __name__ == "__main__":
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--kyverno", default="kyverno", help="Native Kyverno CLI executable")
    run(parser.parse_args().kyverno)
