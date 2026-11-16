#!/usr/bin/env python3
"""Render held CEL replacements; never install policies or change production YAML."""

import argparse
import hashlib
import json
from pathlib import Path
import re

import yaml


ROOT = Path(__file__).resolve().parents[2]
POLICIES = ROOT / "infrastructure/policies"
API = "policies.kyverno.io/v1"
REVIEWED = {
    "block-risky-capabilities": "8a4b0237e78a660dd3cba170d337a2535fcf20cf06403737b9b390658a730907",
    "disallow-host-namespaces": "8c8523f9302da20598bae8dafdbd336adb4e0e95e64ab42e6e1756586d6a391a",
    "enforce-namespace-labels": "ca4fdabee9478d50b3c68850b1b2064d630cda443a20472713ad7df877356832",
    "restrict-host-ports": "e630edfd25e13559850b1ccdbda780dc2cb3c704bc590a52bd08e6b678b85877",
    "homelab-ingress-tailscale-privilege": "013363047b5616376d01627930272a0ed5ce32c2195ce16838be690898c38fe3",
    "metallb-speaker-host-network": "1b67fd5b10a64a8b678b11eb0357e0f568c698829b9ebf6b9036a12cc2e4b045",
}
POD_SPEC = (
    "(object.kind == 'Pod' ? object.spec : object.kind == 'CronJob' ? "
    "object.spec.jobTemplate.spec.template.spec : object.spec.template.spec)"
)
CONTAINERS = (
    f"({POD_SPEC}.containers + {POD_SPEC}.?initContainers.orValue([])"
    f" + {POD_SPEC}.?ephemeralContainers.orValue([]))"
)
EXPRESSIONS = {
    ("block-risky-capabilities", "deny-privileged-containers"):
        "variables.containers.all(c, !c.?securityContext.?privileged.orValue(false))",
    ("block-risky-capabilities", "deny-host-network"):
        "!variables.podSpec.?hostNetwork.orValue(false)",
    ("block-risky-capabilities", "deny-host-path-volumes"):
        "variables.podSpec.?volumes.orValue([]).all(v, !has(v.hostPath))",
    ("disallow-host-namespaces", "deny-host-pid"):
        "!variables.podSpec.?hostPID.orValue(false)",
    ("disallow-host-namespaces", "deny-host-ipc"):
        "!variables.podSpec.?hostIPC.orValue(false)",
    ("restrict-host-ports", "deny-host-ports"):
        "variables.containers.all(c, c.?ports.orValue([]).all(p, p.?hostPort.orValue(0) == 0))",
    ("enforce-namespace-labels", "require-environment-label"):
        "object.metadata.?labels['environment'].orValue('') != ''",
    ("enforce-namespace-labels", "require-owner-label"):
        "object.metadata.?labels['owner'].orValue('') != ''",
}


def spec_hash(spec):
    return hashlib.sha256(
        json.dumps(spec, sort_keys=True, separators=(",", ":")).encode()
    ).hexdigest()


def reviewed_sources():
    found = {}
    for folder in ("kyverno-policies", "kyverno-install"):
        for path in sorted((POLICIES / folder).rglob("*.yaml")):
            doc = yaml.safe_load(path.read_text(encoding="utf-8"))
            name = doc.get("metadata", {}).get("name")
            if name not in REVIEWED:
                continue
            if name in found or spec_hash(doc["spec"]) != REVIEWED[name]:
                raise RuntimeError(f"Unreviewed or duplicate source specification: {name}")
            found[name] = doc
    if found.keys() != REVIEWED.keys():
        raise RuntimeError(f"Missing reviewed sources: {sorted(REVIEWED.keys() - found.keys())}")
    return found


def candidate_name(policy, rule):
    name = policy + "-" + rule
    if len(name) > 63 or not re.fullmatch(r"[a-z0-9][a-z0-9-]*[a-z0-9]", name):
        raise RuntimeError(f"Invalid candidate policy name: {name}")
    return name


def inline(expression, root="object"):
    value = expression.replace("variables.containers", CONTAINERS)
    value = value.replace("variables.podSpec", POD_SPEC)
    return re.sub(r"\bobject\b", root, value)


def resource_rules(pod):
    # Legacy Pod webhooks also cover ephemeral-container admission.
    groups = [
        ("", ["pods", "pods/ephemeralcontainers", "replicationcontrollers"]),
        ("apps", ["deployments", "daemonsets", "statefulsets", "replicasets"]),
        ("batch", ["jobs", "cronjobs"]),
    ] if pod else [("", ["namespaces"])]
    return [
        {"apiGroups": [group], "apiVersions": ["v1"], "operations": ["CREATE", "UPDATE"],
         "resources": resources}
        for group, resources in groups
    ]


def exception(name, policy, conditions):
    return {
        "apiVersion": API, "kind": "PolicyException",
        "metadata": {"name": name, "namespace": "kyverno"},
        "spec": {
            "policyRefs": [{"name": policy, "kind": "ValidatingPolicy"}],
            "matchConditions": [
                {"name": label, "expression": expression} for label, expression in conditions
            ],
        },
    }


def build():
    sources = reviewed_sources()
    candidates, compatibility = [], []
    seen = set()
    for policy in sorted(sources.values(), key=lambda item: item["metadata"]["name"]):
        if policy["kind"] != "ClusterPolicy":
            continue
        policy_name = policy["metadata"]["name"]
        if policy["spec"]["validationFailureAction"] != "Enforce":
            raise RuntimeError("The first migration stage must not promote Audit policies")
        for rule in policy["spec"]["rules"]:
            key = policy_name, rule["name"]
            expression = EXPRESSIONS[key]
            seen.add(key)
            name = candidate_name(*key)
            pod = rule["match"]["any"][0]["resources"]["kinds"] == ["Pod"]
            excluded = rule["exclude"]["any"][0]["resources"]
            conditions = [{
                "name": "same-legacy-exclusions",
                "expression": "!(" + ("request.namespace" if pod else "object.metadata.name")
                + " in " + json.dumps(excluded["namespaces" if pod else "names"]) + ")",
            }]
            spec = {
                "failurePolicy": "Fail",
                "evaluation": {"admission": {"enabled": True}, "background": {"enabled": True}},
                "validationActions": ["Deny"],
                "matchConstraints": {"resourceRules": resource_rules(pod)},
                "matchConditions": conditions,
                "validations": [{"expression": expression, "message": rule["validate"]["message"]}],
            }
            if pod:
                spec["variables"] = [
                    {"name": "podSpec", "expression": POD_SPEC},
                    {"name": "containers", "expression": CONTAINERS},
                ]
            candidates.append({
                "apiVersion": API, "kind": "ValidatingPolicy",
                "metadata": {
                    "name": name,
                    "labels": {"homelab.rishik.dev/legacy-policy": policy_name,
                               "homelab.rishik.dev/legacy-rule": rule["name"]},
                },
                "spec": spec,
            })
            # Legacy Enforce defaults to skipping still-invalid UPDATEs of invalid objects.
            # Use a narrow exception, not a pass-shaped validation or a blanket UPDATE waiver.
            compatibility.append(exception("existing-" + name, name, [
                ("update-with-old-object", "request.operation == 'UPDATE' && oldObject != null"),
                ("new-object-still-violates", "!(" + inline(expression) + ")"),
                ("old-object-already-violated", "oldObject != null && !("
                 + inline(expression, "oldObject") + ")"),
            ]))
    if seen != EXPRESSIONS.keys():
        raise RuntimeError("Candidate mapping does not cover exactly the reviewed enforced rules")

    for source_name in ("homelab-ingress-tailscale-privilege", "metallb-speaker-host-network"):
        source = sources[source_name]["spec"]
        resources = source["match"]["any"][0]["resources"]
        reference = source["exceptions"][0]
        name, prefix = resources["names"]
        if prefix != name + "-*":
            raise RuntimeError("Unreviewed exception resource-name pattern")
        conditions = [
            # Actual background scans synthesize CREATE with an empty UserInfo.
            ("admission-only", "request.userInfo.?username.orValue('') != ''"),
            ("same-kinds", "object.kind in " + json.dumps(resources["kinds"])),
            ("same-namespace", "request.namespace == " + json.dumps(resources["namespaces"][0])),
            ("same-name", "object.metadata.name == " + json.dumps(name)
             + " || object.metadata.name.startsWith(" + json.dumps(name + "-") + ")"),
        ]
        for index, (label, value) in enumerate(resources["selector"]["matchLabels"].items()):
            conditions.append((
                f"same-label-{index}",
                "object.metadata.?labels[" + json.dumps(label) + "].orValue('') == "
                + json.dumps(value),
            ))
        account = "homelab-ingress" if source_name.startswith("homelab") else "metallb-speaker"
        conditions.append((
            "same-service-account", POD_SPEC + ".?serviceAccountName.orValue('') == "
            + json.dumps(account),
        ))
        if source_name.startswith("homelab"):
            for field, permitted in (("containers", "tailscale"), ("initContainers", "sysctler"),
                                     ("ephemeralContainers", None)):
                expression = (
                    POD_SPEC + ".?" + field + ".orValue([]).all(c, "
                    "!c.?securityContext.?privileged.orValue(false)"
                )
                if permitted is not None:
                    expression += " || c.name == " + json.dumps(permitted)
                conditions.append(("same-" + field.lower(), expression + ")"))
        compatibility.append(exception(
            source_name, candidate_name(reference["policyName"], reference["ruleNames"][0]),
            conditions,
        ))
    return candidates, compatibility


if __name__ == "__main__":
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--render", type=Path, required=True, help="Local output directory, never a cluster")
    args = parser.parse_args()
    policies, exceptions = build()
    args.render.mkdir(parents=True, exist_ok=True)
    for name, documents in (("policies.yaml", policies), ("exceptions.yaml", exceptions)):
        (args.render / name).write_text(yaml.safe_dump_all(documents, sort_keys=False), encoding="utf-8")
    print(f"Rendered HELD stage one: {len(policies)} policies and {len(exceptions)} exceptions")
