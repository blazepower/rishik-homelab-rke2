#!/usr/bin/env python3
"""Exercise maintenance-safe mutation with the actual desired Kyverno engine."""

import argparse
from copy import deepcopy
import importlib.util
from pathlib import Path
import subprocess
import tempfile

import yaml


ROOT = Path(__file__).resolve().parents[2]
POLICIES = ROOT / "infrastructure/policies/kyverno-policies/cluster-policies"
spec = importlib.util.spec_from_file_location(
    "image_policy", Path(__file__).with_name("test-kyverno-image-policy.py"),
)
image_policy = importlib.util.module_from_spec(spec)
spec.loader.exec_module(image_policy)


def pod_spec(resource):
    if resource["kind"] == "Pod":
        return resource["spec"]
    if resource["kind"] == "CronJob":
        return resource["spec"]["jobTemplate"]["spec"]["template"]["spec"]
    return resource["spec"]["template"]["spec"]


def resource(kind, name):
    template = {
        "metadata": {"labels": {"app": "mutation-test"}},
        "spec": {
            "containers": [{"name": "main", "image": "busybox:1.37.0"}],
            "initContainers": [{"name": "init", "image": "busybox:1.37.0"}],
        },
    }
    body = {"template": template}
    api_version = "apps/v1"
    if kind == "Pod":
        body = template["spec"]
        api_version = "v1"
    elif kind == "CronJob":
        body = {"schedule": "0 0 * * *", "jobTemplate": {"spec": body}}
        api_version = "batch/v1"
    else:
        body.update({"replicas": 1, "selector": {"matchLabels": {"app": "mutation-test"}}})
        if kind == "StatefulSet":
            body["serviceName"] = "mutation-test"
    return {
        "apiVersion": api_version, "kind": kind,
        "metadata": {"name": name, "namespace": "mutation-policy-test", "labels": {"app": "mutation-test"}},
        "spec": body,
    }


def add_defaults(resource):
    body = pod_spec(resource)
    for field in ("containers", "initContainers"):
        for container in body.get(field, []):
            container["securityContext"] = {
                "allowPrivilegeEscalation": False, "capabilities": {"drop": ["ALL"]},
            }
    add_topology_defaults(resource)


def add_topology_defaults(resource):
    if resource["kind"] == "Deployment":
        pod_spec(resource)["topologySpreadConstraints"] = [{
            "maxSkew": 1, "topologyKey": "kubernetes.io/hostname",
            "whenUnsatisfiable": "ScheduleAnyway",
            "labelSelector": {"matchLabels": resource["spec"]["selector"]["matchLabels"]},
        }]


def cases():
    for kind in ("Pod", "Deployment", "StatefulSet", "CronJob"):
        scenarios = ["create", "metadata", "template", "existing-defaults", "existing-template"]
        if kind in ("Deployment", "StatefulSet"):
            scenarios.append("scale")
        if kind != "Pod":
            scenarios.append("template-metadata")
        for scenario in scenarios:
            old = resource(kind, (kind + "-" + scenario).lower())
            if scenario == "metadata":
                old["metadata"]["annotations"] = {"maintenance.example/check": "pending"}
            if scenario == "template-metadata":
                # CLI Values deep-merges oldObject: specify the previous annotation explicitly.
                template = old["spec"]
                if kind == "CronJob":
                    template = template["jobTemplate"]["spec"]
                template["template"]["metadata"]["annotations"] = {"maintenance.example/check": "pending"}
            if scenario.startswith("existing-"):
                add_defaults(old)
                for field in ("containers", "initContainers"):
                    pod_spec(old)[field][0]["securityContext"]["capabilities"]["drop"] = ["NET_RAW"]
                if kind == "Deployment":
                    pod_spec(old)["topologySpreadConstraints"][0]["maxSkew"] = 2
            current = deepcopy(old)
            if scenario == "metadata":
                current["metadata"]["annotations"] = {"maintenance.example/check": "done"}
            elif scenario == "template-metadata":
                template = current["spec"]
                if kind == "CronJob":
                    template = template["jobTemplate"]["spec"]
                template["template"]["metadata"]["annotations"] = {"maintenance.example/check": "done"}
            elif scenario in ("template", "existing-template"):
                pod_spec(current)["containers"][0]["image"] = "busybox:1.37.1"
            elif scenario in ("scale", "existing-defaults") and kind in ("Deployment", "StatefulSet"):
                current["spec"]["replicas"] = 0
            expected = deepcopy(current)
            if scenario == "create" or (scenario == "template" and kind != "Pod"):
                add_defaults(expected)
            elif scenario == "template-metadata":
                add_topology_defaults(expected)
            yield current, None if scenario == "create" else old, expected

        for explicit in (False, True):
            current = resource(kind, (kind + "-explicit-" + str(explicit)).lower())
            for field in ("containers", "initContainers"):
                pod_spec(current)[field][0]["securityContext"] = {
                    "allowPrivilegeEscalation": explicit, "capabilities": {"drop": ["NET_RAW"]},
                }
            expected = deepcopy(current)
            add_topology_defaults(expected)
            yield current, None, expected

    current = resource("Pod", "pod-no-init-containers")
    current["spec"].pop("initContainers")
    expected = deepcopy(current)
    add_defaults(expected)
    yield current, None, expected

    old = resource("Pod", "pod-deadline-update")
    old["spec"]["activeDeadlineSeconds"] = 120
    current = deepcopy(old)
    current["spec"]["activeDeadlineSeconds"] = 60
    yield current, old, deepcopy(current)

    scale = {
        "apiVersion": "autoscaling/v1", "kind": "Scale",
        "metadata": {"name": "deployment-scale-subresource", "namespace": "mutation-policy-test"},
        "spec": {"replicas": 0},
    }
    old = deepcopy(scale)
    old["spec"]["replicas"] = 1
    yield scale, old, deepcopy(scale)


def run(kyverno):
    image_policy.verify_engine(kyverno)
    fixtures = list(cases())
    policies = [POLICIES / "add-security-defaults.yaml", POLICIES / "mutate-topology-spread.yaml"]
    with tempfile.TemporaryDirectory(prefix=".kyverno-mutation-", dir=ROOT) as directory:
        directory = Path(directory)
        resources = directory / "resources.yaml"
        values = directory / "values.yaml"
        output = directory / "actual.yaml"
        resources.write_text(yaml.safe_dump_all([current for current, _, _ in fixtures]), encoding="utf-8")
        values.write_text(yaml.safe_dump({
            "apiVersion": "cli.kyverno.io/v1alpha1", "kind": "Values",
            "metadata": {"name": "maintenance-safe-mutation"},
            "policies": [{
                "name": policy.stem,
                "resources": [{
                    "name": current["metadata"]["name"],
                    "values": {"request.operation": "CREATE" if old is None else "UPDATE",
                               "request.oldObject": old},
                } for current, old, _ in fixtures],
            } for policy in policies],
        }), encoding="utf-8")
        completed = subprocess.run([
            kyverno, "apply", *(str(policy) for policy in policies),
            "--resource", str(resources), "--values-file", str(values),
            "--output", str(output), "--remove-color",
        ], capture_output=True, text=True)
        summary = next((line for line in completed.stdout.splitlines() if line.startswith("pass:")), None)
        if completed.returncode or summary is None or "fail: 0" not in summary or "error: 0" not in summary:
            raise AssertionError(completed.stdout + completed.stderr)
        actual = {
            item["metadata"]["name"]: item
            for item in yaml.safe_load_all(output.read_text(encoding="utf-8")) if item
        }
        for current, _, expected in fixtures:
            name = current["metadata"]["name"]
            if actual.get(name) != expected:
                raise AssertionError(
                    f"{name}: admission result differs from the complete expected resource\n"
                    f"expected: {yaml.safe_dump(expected)}\nactual: {yaml.safe_dump(actual.get(name))}"
                )
    print(summary)
    print(f"PASS: {len(fixtures)} native mutation results preserve exact admission intent")


if __name__ == "__main__":
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--kyverno", default="kyverno", help="Native Kyverno CLI executable")
    run(parser.parse_args().kyverno)
