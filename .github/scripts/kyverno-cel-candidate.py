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
    "disallow-latest-tag": "cfecfd52bdd784af8796f8bddfdb54530a2773b8bfcf5481b9c774b4cbdf4608",
    "require-resources-limits": "e1aeb0c15316ba52d6e2dffb9d553c606429a94fec2b80b2d7d77f0f95710da5",
    "disallow-privilege-escalation": "38c960cb8df7628520084ebeff88eb65f9ebd3d2681e03f0caf08307848f1acc",
    "require-run-as-nonroot": "3efb8593ce5d468963b21597b9ce970246635e350c27eee00aff24d61d97a037",
    "restrict-apparmor": "5e68c4f350d4037d784b4098c2ccbdf6fb88a2e79d82f21b43d715fdb1345257",
    "restrict-capabilities": "20bb92c262bce6d219239635abbc6770e30acef6cb4bbe4e902ae40900bef528",
    "restrict-seccomp": "67806e43e8144935cde17ba4630ca2045d89cef05ad8d80b9938e2801920d69d",
    "homelab-ingress-tailscale-privilege": "013363047b5616376d01627930272a0ed5ce32c2195ce16838be690898c38fe3",
    "metallb-speaker-host-network": "1b67fd5b10a64a8b678b11eb0357e0f568c698829b9ebf6b9036a12cc2e4b045",
}
POD_SPEC = (
    "(object.kind == 'Pod' ? object.spec : object.kind == 'CronJob' ? "
    "object.spec.jobTemplate.spec.template.spec : object.spec.template.spec)"
)
POD_METADATA = (
    "(object.kind == 'Pod' ? object.metadata : object.kind == 'CronJob' ? "
    "object.spec.jobTemplate.spec.template.?metadata.orValue({})"
    " : object.spec.template.?metadata.orValue({}))"
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
    ("disallow-latest-tag", "disallow-latest-tag"):
        "variables.containers.all(c, !c.image.matches('(^|/)[^/:@]+(:latest(@[^/]+)?)?$'))",
    ("disallow-latest-tag", "validate-trusted-registries"):
        "variables.containers.all(c, isImage(c.image) && ("
        "image(c.image).registry() in ['docker.io', 'index.docker.io', 'ghcr.io', 'quay.io',"
        " 'gcr.io', 'registry.k8s.io', 'mcr.microsoft.com', 'lscr.io'] || "
        "(image(c.image).registry() == 'mirror.gcr.io' && "
        "image(c.image).repository() in ['aquasec/trivy', 'aquasec/trivy-operator'])))",
    ("disallow-privilege-escalation", "deny-privilege-escalation"):
        "variables.containers.all(c, c.?securityContext.?allowPrivilegeEscalation.orValue(true) == false)",
    ("require-run-as-nonroot", "require-run-as-nonroot"):
        "variables.containers.all(c, c.?securityContext.?runAsNonRoot.orValue(false) || "
        "variables.podSpec.?securityContext.?runAsNonRoot.orValue(false))",
    ("require-resources-limits", "validate-resources"):
        "variables.containers.all(c, "
        "c.?resources.?requests['cpu'].orValue('') != '' && "
        "c.?resources.?requests['memory'].orValue('') != '' && "
        "c.?resources.?limits['cpu'].orValue('') != '' && "
        "c.?resources.?limits['memory'].orValue('') != '')",
    ("restrict-apparmor", "restrict-apparmor-profiles"):
        "variables.containers.all(c, variables.podMetadata.?annotations["
        "'container.apparmor.security.beta.kubernetes.io/' + c.name"
        "].orValue('runtime/default') != 'unconfined')",
    ("restrict-capabilities", "require-drop-all"):
        "variables.containers.all(c, 'ALL' in c.?securityContext.?capabilities.?drop.orValue([]))",
    ("restrict-capabilities", "restrict-added-capabilities"):
        "variables.containers.all(c, c.?securityContext.?capabilities.?add.orValue([]).all(cap, "
        "cap in ['CHOWN', 'DAC_OVERRIDE', 'FOWNER', 'FSETID', 'KILL', 'SETGID', 'SETUID', "
        "'SETPCAP', 'NET_BIND_SERVICE', 'SYS_CHROOT', 'SETFCAP']))",
    ("restrict-seccomp", "restrict-seccomp-pod"):
        "variables.podSpec.?securityContext.?seccompProfile.?type.orValue('RuntimeDefault') != 'Unconfined'",
    ("restrict-seccomp", "restrict-seccomp-containers"):
        "variables.containers.all(c, "
        "c.?securityContext.?seccompProfile.?type.orValue('RuntimeDefault') != 'Unconfined')",
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
    value = value.replace("variables.podMetadata", POD_METADATA)
    return re.sub(r"\bobject\b", root, value)


def resource_rules(pod, operations):
    # Legacy Pod webhooks also cover ephemeral-container admission.
    groups = [
        ("", ["pods", "pods/ephemeralcontainers", "replicationcontrollers"]),
        ("apps", ["deployments", "daemonsets", "statefulsets", "replicasets"]),
        ("batch", ["jobs", "cronjobs"]),
    ] if pod else [("", ["namespaces"])]
    return [
        {"apiGroups": [group], "apiVersions": ["v1"], "operations": operations,
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
        action = policy["spec"]["validationFailureAction"]
        if action not in ("Enforce", "Audit"):
            raise RuntimeError(f"Unreviewed source validation action: {policy_name}")
        for rule in policy["spec"]["rules"]:
            key = policy_name, rule["name"]
            expression = EXPRESSIONS[key]
            scalar_delete = key == ("restrict-seccomp", "restrict-seccomp-pod")
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
                "validationActions": ["Deny" if action == "Enforce" else "Audit"],
                "matchConstraints": {"resourceRules": resource_rules(
                    pod, ["CREATE", "UPDATE", "DELETE"] if scalar_delete else ["CREATE", "UPDATE"]
                )},
                "matchConditions": conditions,
                "validations": [{"expression": expression, "message": rule["validate"]["message"]}],
            }
            if pod:
                spec["variables"] = [
                    {"name": "podSpec", "expression": POD_SPEC},
                    {"name": "podMetadata", "expression": POD_METADATA},
                    {"name": "containers", "expression": CONTAINERS},
                ]
                if scalar_delete:
                    # The legacy scalar deny audits the deleted resource; pattern/foreach rules skip.
                    spec["variables"][0]["expression"] = (
                        f"object != null ? {POD_SPEC} : {inline(POD_SPEC, 'oldObject')}"
                    )
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
            if action == "Enforce":
                compatibility.append(exception("existing-" + name, name, [
                    ("update-with-old-object", "request.operation == 'UPDATE' && oldObject != null"),
                    ("new-object-still-violates", "!(" + inline(expression) + ")"),
                    ("old-object-already-violated", "oldObject != null && !("
                     + inline(expression, "oldObject") + ")"),
                ]))
    if seen != EXPRESSIONS.keys():
        raise RuntimeError("Candidate mapping does not cover exactly the reviewed validation rules")

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
    print(f"Rendered HELD validations: {len(policies)} policies and {len(exceptions)} exceptions")
