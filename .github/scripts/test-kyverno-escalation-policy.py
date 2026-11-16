#!/usr/bin/env python3
"""Verify explicit false stays false in the actual escalation policy engine."""

import argparse
from pathlib import Path
import subprocess
import tempfile

import yaml


ROOT = Path(__file__).resolve().parents[2]
POLICY = ROOT / "infrastructure/policies/kyverno-policies/pss-baseline/disallow-privilege-escalation.yaml"


def run(kyverno):
    resources, results = [], []
    for kind in ("Pod", "Deployment", "CronJob"):
        for field in ("containers", "initContainers", "ephemeralContainers"):
            for label, context, allowed in (
                ("false", {"allowPrivilegeEscalation": False}, True),
                ("true", {"allowPrivilegeEscalation": True}, False),
                ("missing-field", {}, False),
                ("missing-context", None, False),
            ):
                name = (kind + "-" + field + "-" + label).lower()
                tested = {"name": "tested", "image": "busybox:1.37.0"}
                if context is not None:
                    tested["securityContext"] = context
                spec = {"containers": [{"name": "main", "image": "busybox:1.37.0",
                                        "securityContext": {"allowPrivilegeEscalation": False}}]}
                spec[field] = [tested]
                prefix = ""
                if kind == "Deployment":
                    spec = {"template": {"spec": spec}}
                    prefix = "autogen-"
                elif kind == "CronJob":
                    spec = {"schedule": "0 0 * * *", "jobTemplate": {"spec": {"template": {"spec": spec}}}}
                    prefix = "autogen-cronjob-"
                resources.append({
                    "apiVersion": {"Pod": "v1", "Deployment": "apps/v1", "CronJob": "batch/v1"}[kind],
                    "kind": kind, "metadata": {"name": name, "namespace": "policy-test"}, "spec": spec,
                })
                results.append({
                    "policy": "disallow-privilege-escalation", "rule": prefix + "deny-privilege-escalation",
                    "kind": kind, "resources": [name], "result": "pass" if allowed else "fail",
                })
    with tempfile.TemporaryDirectory(prefix="kyverno-escalation-") as directory:
        directory = Path(directory)
        (directory / "resources.yaml").write_text(yaml.safe_dump_all(resources), encoding="utf-8")
        (directory / "kyverno-test.yaml").write_text(yaml.safe_dump({
            "apiVersion": "cli.kyverno.io/v1alpha1", "kind": "Test",
            "metadata": {"name": "escalation-false-and-missing"},
            "policies": [str(POLICY.resolve())], "resources": ["resources.yaml"], "results": results,
        }), encoding="utf-8")
        subprocess.run([kyverno, "test", str(directory), "--require-tests", "--fail-only", "--remove-color"], check=True)
    print(f"PASS: {len(results)} native escalation-policy expectations")


if __name__ == "__main__":
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--kyverno", default="kyverno")
    run(parser.parse_args().kyverno)
