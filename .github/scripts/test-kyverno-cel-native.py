#!/usr/bin/env python3
"""Accept held CEL policies against real versioned engines, without a cluster."""

import argparse
from copy import deepcopy
import hashlib
import importlib.util
import json
import os
from pathlib import Path
import re
import shutil
import subprocess
import tempfile

import yaml


ROOT = Path(__file__).resolve().parents[2]
SPEC = importlib.util.spec_from_file_location("candidate", Path(__file__).with_name("kyverno-cel-candidate.py"))
candidate = importlib.util.module_from_spec(SPEC)
SPEC.loader.exec_module(candidate)
KINDS = ("Pod", "Deployment", "DaemonSet", "StatefulSet", "ReplicaSet",
         "ReplicationController", "Job", "CronJob")
USER = "system:serviceaccount:policy-test:synthetic"
AUDIT_REPAIR = "require-run-as-nonroot"


def resource(kind, spec, namespace="policy-test", name="fixture"):
    spec = deepcopy(spec)
    annotations = spec.pop("_annotations", None)
    metadata = {"name": name, "labels": {"app": "fixture"}}
    if kind == "Namespace":
        return {"apiVersion": "v1", "kind": kind, "metadata": metadata}
    metadata["namespace"] = namespace
    template = {"metadata": {"labels": {"app": "fixture"}}, "spec": deepcopy(spec)}
    if annotations is not None:
        (metadata if kind == "Pod" else template["metadata"])["annotations"] = annotations
    if kind == "Pod":
        body, api = spec, "v1"
    elif kind == "CronJob":
        template["spec"]["restartPolicy"] = "Never"
        body, api = {"schedule": "0 0 * * *", "jobTemplate": {"spec": {"template": template}}}, "batch/v1"
    elif kind == "Job":
        template["spec"]["restartPolicy"] = "Never"
        body, api = {"template": template}, "batch/v1"
    else:
        body, api = {"template": template}, "apps/v1"
        body["selector"] = {"matchLabels": {"app": "fixture"}}
        if kind == "ReplicationController":
            api, body["selector"] = "v1", {"app": "fixture"}
        if kind == "StatefulSet":
            body["serviceName"] = "fixture"
    return {"apiVersion": api, "kind": kind, "metadata": metadata, "spec": deepcopy(body)}


def pod_spec(obj):
    if obj["kind"] == "Pod":
        return obj["spec"]
    if obj["kind"] == "CronJob":
        return obj["spec"]["jobTemplate"]["spec"]["template"]["spec"]
    return obj["spec"]["template"]["spec"]


def variants(rule):
    clean = {"containers": [{"name": "main", "image": "busybox:1.37.0"}]}
    if rule in ("disallow-latest-tag", "validate-trusted-registries"):
        module_spec = importlib.util.spec_from_file_location(
            "image_cases", Path(__file__).with_name("test-kyverno-image-policy.py")
        )
        image_cases = importlib.util.module_from_spec(module_spec)
        module_spec.loader.exec_module(image_cases)
        results = [("valid", clean, True)]
        for field in ("containers", "initContainers", "ephemeralContainers"):
            for index, (image, tag_ok, registry_ok) in enumerate(image_cases.CASES):
                spec = deepcopy(clean)
                spec[field] = [{"name": "tested", "image": image}]
                results.append((field + "-" + str(index), spec,
                                tag_ok if rule == "disallow-latest-tag" else registry_ok))
        return results
    if rule in ("deny-privilege-escalation", "require-run-as-nonroot", "require-drop-all",
                "restrict-added-capabilities", "restrict-seccomp-containers", "validate-resources"):
        safe = {
            "deny-privilege-escalation": {"securityContext": {"allowPrivilegeEscalation": False}},
            "require-run-as-nonroot": {"securityContext": {"runAsNonRoot": True}},
            "require-drop-all": {"securityContext": {"capabilities": {"drop": ["ALL"]}}},
            "restrict-added-capabilities": {},
            "restrict-seccomp-containers": {},
            "validate-resources": {"resources": {
                "requests": {"cpu": "10m", "memory": "16Mi"},
                "limits": {"cpu": "100m", "memory": "64Mi"},
            }},
        }[rule]
        clean["containers"][0].update(deepcopy(safe))
        results = [("valid", clean, True)]
        for field in ("containers", "initContainers", "ephemeralContainers"):
            options = []
            if rule == "deny-privilege-escalation":
                options = [("false", {"allowPrivilegeEscalation": False}, True),
                           ("true", {"allowPrivilegeEscalation": True}, False),
                           ("empty", {}, False), ("missing", None, False)]
            elif rule == "require-run-as-nonroot":
                options = [("true", {"runAsNonRoot": True}, True),
                           ("false", {"runAsNonRoot": False}, False),
                           ("empty", {}, False), ("missing", None, False)]
            elif rule == "require-drop-all":
                options = [(label, {"capabilities": {"drop": drop}}, allowed)
                           for label, drop, allowed in (
                               ("all", ["ALL"], True), ("extra", ["NET_RAW", "ALL"], True),
                               ("empty", [], False), ("lowercase", ["all"], False))]
                options.append(("missing", None, False))
            elif rule == "restrict-added-capabilities":
                options = [(cap.lower(), {"capabilities": {"add": [cap]}}, allowed)
                           for cap, allowed in (
                               ("CHOWN", True), ("DAC_OVERRIDE", True), ("FOWNER", True),
                               ("FSETID", True), ("KILL", True), ("SETGID", True), ("SETUID", True),
                               ("SETPCAP", True), ("NET_BIND_SERVICE", True), ("SYS_CHROOT", True),
                               ("SETFCAP", True), ("SYS_ADMIN", False), ("NET_ADMIN", False),
                               ("SYS_PTRACE", False), ("NET_RAW", False))]
                options.append(("missing", None, True))
            elif rule == "restrict-seccomp-containers":
                options = [(value.lower(), {"seccompProfile": {
                    "type": value, **({"localhostProfile": "synthetic.json"} if value == "Localhost" else {})
                }}, value != "Unconfined") for value in ("RuntimeDefault", "Localhost", "Unconfined")]
                options.append(("missing", None, True))
            else:
                for category in ("requests", "limits"):
                    for quantity in ("cpu", "memory"):
                        missing = deepcopy(safe["resources"])
                        del missing[category][quantity]
                        options.append((category + "-" + quantity, missing, False))
                options.append(("missing", None, False))
            for label, context, allowed in options:
                spec = deepcopy(clean)
                tested = {"name": "tested", "image": "busybox:1.37.0"}
                if context is not None:
                    tested["resources" if rule == "validate-resources" else "securityContext"] = context
                spec[field] = [tested]
                results.append((field + "-" + label, spec, allowed))
                if rule == "require-run-as-nonroot" and label in ("false", "missing"):
                    inherited = deepcopy(spec)
                    inherited["securityContext"] = {"runAsNonRoot": True}
                    results.append((field + "-" + label + "-pod-true", inherited, True))
        return results
    if rule == "restrict-seccomp-pod":
        return [("missing", clean, True)] + [
            (value.lower(), {**clean, "securityContext": {"seccompProfile": {
                "type": value, **({"localhostProfile": "synthetic.json"} if value == "Localhost" else {})
            }}}, value != "Unconfined") for value in ("RuntimeDefault", "Localhost", "Unconfined")
        ]
    if rule == "restrict-apparmor-profiles":
        results = [("missing", clean, True)]
        for field in ("containers", "initContainers", "ephemeralContainers"):
            for profile in ("runtime/default", "localhost/synthetic", "unconfined"):
                spec = deepcopy(clean)
                spec[field] = [{"name": "tested", "image": "busybox:1.37.0"}]
                spec["_annotations"] = {"container.apparmor.security.beta.kubernetes.io/tested": profile}
                results.append((field + "-" + profile, spec, profile != "unconfined"))
        return results
    results = [("absent", clean, True)]
    if rule == "deny-privileged-containers":
        for field in ("containers", "initContainers", "ephemeralContainers"):
            for value in (False, True):
                spec = deepcopy(clean)
                spec[field] = [{"name": "tested", "image": "busybox:1.37.0",
                                "securityContext": {"privileged": value}}]
                results.append((field + "-" + str(value).lower(), spec, not value))
    elif rule in ("deny-host-network", "deny-host-pid", "deny-host-ipc"):
        field = {"deny-host-network": "hostNetwork", "deny-host-pid": "hostPID",
                 "deny-host-ipc": "hostIPC"}[rule]
        results.extend([("false", {**clean, field: False}, True),
                        ("true", {**clean, field: True}, False)])
    elif rule == "deny-host-path-volumes":
        results.extend([
            ("empty-dir", {**clean, "volumes": [{"name": "scratch", "emptyDir": {}}]}, True),
            ("host-path", {**clean, "volumes": [
                {"name": "scratch", "emptyDir": {}},
                {"name": "host", "hostPath": {"path": "/var/tmp/synthetic", "type": "Directory"}},
            ]}, False),
        ])
    elif rule == "deny-host-ports":
        for field in ("containers", "initContainers", "ephemeralContainers"):
            for port in (None, 0, 8080):
                spec = deepcopy(clean)
                entry = {"containerPort": 8080}
                if port is not None:
                    entry["hostPort"] = port
                spec[field] = [{"name": "tested", "image": "busybox:1.37.0", "ports": [entry]}]
                results.append((field + "-" + str(port), spec, port in (None, 0)))
    else:
        raise RuntimeError(f"No reviewed fixture variants for {rule}")
    return results


def fixtures(policies, exceptions):
    sources = candidate.reviewed_sources()
    cases = []
    actions = {
        policy["metadata"]["name"]: [
            "Deny" if sources[policy["metadata"]["labels"]["homelab.rishik.dev/legacy-policy"]]
            ["spec"]["validationFailureAction"] == "Enforce" else "Audit"
        ] for policy in policies
    }
    original_names = {
        policy["metadata"]["name"]: policy["metadata"]["labels"]["homelab.rishik.dev/legacy-policy"]
        for policy in policies
    }

    def add(policy, label, obj, expected, old=None, operation="CREATE", subresource="",
            username=USER, exception=False, keep_name=False):
        obj, old = deepcopy(obj), deepcopy(old)
        if not keep_name:
            obj["metadata"]["name"] += "-" + str(len(cases))
        if old is not None:
            old["metadata"]["name"] = obj["metadata"]["name"]
        cases.append({
            "id": f"{len(cases):04d}:{label}", "policy": policy, "object": obj,
            "oldObject": old, "operation": operation, "subresource": subresource,
            "username": username, "expected": expected, "expectedException": exception,
            "expectedActions": actions[policy],
            # The pinned original uses the identifier `false`, not the JSON literal.
            # Its Audit check passes all Boolean false/missing cases; CEL repairs reporting only.
            "legacyExpected": "pass" if original_names[policy] == AUDIT_REPAIR
            and expected != "skip" else expected,
        })

    for policy in policies:
        labels = policy["metadata"]["labels"]
        source = sources[labels["homelab.rishik.dev/legacy-policy"]]
        rule = next(rule for rule in source["spec"]["rules"]
                    if rule["name"] == labels["homelab.rishik.dev/legacy-rule"])
        name, rule_name = policy["metadata"]["name"], rule["name"]
        kinds = ("Namespace",) if source["metadata"]["name"] == "enforce-namespace-labels" else KINDS
        for kind in kinds:
            if kind == "Namespace":
                field = "environment" if rule_name == "require-environment-label" else "owner"
                good = resource(kind, {})
                good["metadata"]["labels"] = {"environment": "production", "owner": "rishik"}
                bad = deepcopy(good)
                del bad["metadata"]["labels"][field]
                add(name, rule_name + "-nonempty", good, "pass")
                add(name, rule_name + "-missing", bad, "fail")
                empty = deepcopy(good)
                empty["metadata"]["labels"][field] = ""
                add(name, rule_name + "-empty", empty, "fail")
                no_labels = deepcopy(good)
                del no_labels["metadata"]["labels"]
                add(name, rule_name + "-no-labels", no_labels, "fail")
            else:
                options = variants(rule_name)
                for label, spec, allowed in options:
                    add(name, f"{rule_name}-{kind}-{label}", resource(kind, spec),
                        "pass" if allowed else "fail")
                good = resource(kind, options[0][1])
                bad = resource(kind, next(spec for _, spec, allowed in options if not allowed))
            grandfather = "skip" if actions[name] == ["Deny"] else "fail"
            for old, new, expected in ((good, bad, "fail"), (bad, bad, grandfather),
                                       (bad, good, "pass"), (good, good, "pass")):
                add(name, f"{rule_name}-{kind}-update-{expected}", new, expected, old,
                    operation="UPDATE", exception=expected == "skip")
            deletion = deepcopy(bad)
            add(name, f"{rule_name}-{kind}-delete", deletion,
                "fail" if rule_name == "restrict-seccomp-pod" else "skip",
                deletion, operation="DELETE")
            cases[-1]["object"] = None
            if rule_name == "restrict-seccomp-pod":
                add(name, f"{rule_name}-{kind}-delete-valid", good, "pass", good, operation="DELETE")
                cases[-1]["object"] = None

        exclusions = rule["exclude"]["any"][0]["resources"]
        for excluded in exclusions.get("namespaces", exclusions.get("names", [])):
            obj = deepcopy(bad)
            if kind == "Namespace":
                obj["metadata"]["name"] = excluded
            else:
                obj["metadata"]["namespace"] = excluded
            add(name, rule_name + "-excluded-" + excluded, obj, "skip", keep_name=kind == "Namespace")
            if rule_name == "restrict-seccomp-pod":
                add(name, rule_name + "-delete-excluded-" + excluded, obj, "skip",
                    obj, operation="DELETE")
                cases[-1]["object"] = None
        obj = deepcopy(bad)
        if kind == "Namespace":
            obj["metadata"]["name"] = exclusions["names"][0] + "-untrusted"
        else:
            obj["metadata"]["namespace"] = exclusions["namespaces"][0] + "-untrusted"
        add(name, rule_name + "-exclusion-lookalike", obj, "fail")
        if rule_name == "restrict-seccomp-pod":
            add(name, rule_name + "-delete-exclusion-lookalike", obj, "fail", obj, operation="DELETE")
            cases[-1]["object"] = None

    for field in ("privileged", "hostPort"):
        rule = "deny-privileged-containers" if field == "privileged" else "deny-host-ports"
        source = "block-risky-capabilities" if field == "privileged" else "restrict-host-ports"
        good = resource("Pod", {"containers": [{"name": "main", "image": "busybox:1.37.0"}]})
        bad = deepcopy(good)
        added = {"name": "debug", "image": "busybox:1.37.0", "targetContainerName": "main"}
        added.update({"securityContext": {"privileged": True}} if field == "privileged"
                     else {"ports": [{"containerPort": 8080, "hostPort": 8080}]})
        bad["spec"]["ephemeralContainers"] = [added]
        add(candidate.candidate_name(source, rule), field + "-real-ephemeral-update", bad, "fail",
            good, operation="UPDATE", subresource="ephemeralcontainers")

    for legacy_name, kinds, target_rule, flag in (
        ("homelab-ingress-tailscale-privilege", ("Pod", "StatefulSet"), "deny-privileged-containers", "privileged"),
        ("metallb-speaker-host-network", ("Pod", "DaemonSet"), "deny-host-network", "hostNetwork"),
    ):
        legacy = sources[legacy_name]["spec"]["match"]["any"][0]["resources"]
        policy = candidate.candidate_name("block-risky-capabilities", target_rule)
        for kind in kinds:
            spec = {"containers": [{"name": "tailscale" if flag == "privileged" else "speaker",
                                    "image": "busybox:1.37.0"}],
                    "serviceAccountName": "homelab-ingress" if flag == "privileged" else "metallb-speaker"}
            if flag == "privileged":
                spec["containers"][0]["securityContext"] = {"privileged": True}
                spec["initContainers"] = [{"name": "sysctler", "image": "busybox:1.37.0",
                                           "securityContext": {"privileged": True}}]
            else:
                spec["hostNetwork"] = True
            obj = resource(kind, spec, legacy["namespaces"][0], legacy["names"][0])
            obj["metadata"]["labels"].update(legacy["selector"]["matchLabels"])
            add(policy, legacy_name + "-" + kind + "-allowed", obj, "skip", exception=True)
            add(policy, legacy_name + "-" + kind + "-background", obj, "fail", username="")
            wrong = deepcopy(obj)
            wrong["metadata"]["namespace"] += "-untrusted"
            add(policy, legacy_name + "-" + kind + "-wrong-namespace", wrong, "fail")
            wrong = deepcopy(obj)
            wrong["metadata"]["name"] += "x"
            add(policy, legacy_name + "-" + kind + "-name-lookalike", wrong, "fail")
            wrong = deepcopy(obj)
            pod_spec(wrong)["serviceAccountName"] += "-untrusted"
            add(policy, legacy_name + "-" + kind + "-wrong-service-account", wrong, "fail")
            for label in legacy["selector"]["matchLabels"]:
                wrong = deepcopy(obj)
                del wrong["metadata"]["labels"][label]
                add(policy, legacy_name + "-" + kind + "-missing-" + label, wrong, "fail")
            wrong = resource("Deployment" if flag == "privileged" else "StatefulSet", spec,
                             legacy["namespaces"][0], legacy["names"][0])
            wrong["metadata"]["labels"].update(legacy["selector"]["matchLabels"])
            add(policy, legacy_name + "-" + kind + "-wrong-controller-kind", wrong, "fail")
            if flag == "privileged":
                for field in ("containers", "initContainers", "ephemeralContainers"):
                    wrong = deepcopy(obj)
                    pod_spec(wrong)[field] = [{"name": "unexpected", "image": "busybox:1.37.0",
                                              "securityContext": {"privileged": True}}]
                    add(policy, legacy_name + "-" + kind + "-wrong-" + field, wrong, "fail")
                wrong = deepcopy(obj)
                pod_spec(wrong)["hostNetwork"] = True
                add(candidate.candidate_name("block-risky-capabilities", "deny-host-network"),
                    legacy_name + "-" + kind + "-no-host-network-waiver", wrong, "fail")
            else:
                wrong = deepcopy(obj)
                pod_spec(wrong)["containers"][0]["securityContext"] = {"privileged": True}
                add(candidate.candidate_name("block-risky-capabilities", "deny-privileged-containers"),
                    legacy_name + "-" + kind + "-no-privilege-waiver", wrong, "fail")
    return {"policies": policies, "exceptions": exceptions, "cases": cases}


def command(args, cwd, evidence, log, env=None):
    completed = subprocess.run(args, cwd=cwd, env=env, text=True, stdout=subprocess.PIPE,
                               stderr=subprocess.STDOUT, timeout=1500)
    (evidence / log).write_text(completed.stdout, encoding="utf-8")
    if completed.returncode:
        print(completed.stdout)
        completed.check_returncode()
    return completed.stdout


def mutation_fixtures(policies):
    sources = candidate.reviewed_sources(candidate.REVIEWED_MUTATIONS)
    by_name = {policy["metadata"]["name"]: policy for policy in policies}
    cases = []

    def add(policy, label, obj, patched):
        for operation in ("CREATE", "UPDATE"):
            original, expected = deepcopy(obj), deepcopy(patched)
            name = "mutation-" + str(len(cases))
            original["metadata"]["name"] = expected["metadata"]["name"] = name
            legacy = deepcopy(expected)
            labels = by_name[policy]["metadata"]["labels"]
            if labels["homelab.rishik.dev/legacy-policy"] == "add-security-defaults":
                field = ("initContainers" if labels["homelab.rishik.dev/legacy-rule"].endswith("init-containers")
                         else "containers")
                if field in pod_spec(legacy):
                    # Actual original SMP groups patched targets before untouched true-valued targets.
                    pod_spec(legacy)[field].sort(
                        key=lambda item: item.get("securityContext", {}).get("allowPrivilegeEscalation") is True
                    )
            cases.append({
                "id": f"mutation-{len(cases):04d}:{label}-{operation}",
                "policy": policy, "object": original, "expectedPatched": expected,
                "legacyExpectedPatched": legacy,
                "oldObject": deepcopy(original) if operation == "UPDATE" else None,
                "operation": operation, "subresource": "", "username": USER,
            })

    for policy in policies:
        labels = policy["metadata"]["labels"]
        source_name, rule_name = labels["homelab.rishik.dev/legacy-policy"], labels["homelab.rishik.dev/legacy-rule"]
        rule = next(rule for rule in sources[source_name]["spec"]["rules"] if rule["name"] == rule_name)
        name = policy["metadata"]["name"]
        if source_name == "add-security-defaults":
            field = "initContainers" if rule_name.endswith("init-containers") else "containers"
            for kind in KINDS:
                for label, contexts in (
                    ("missing", [None]), ("empty", [{}]),
                    ("explicit-true", [{"allowPrivilegeEscalation": True, "capabilities": {"drop": ["NET_RAW"]}}]),
                    ("explicit-false", [{"allowPrivilegeEscalation": False, "runAsUser": 1234,
                                         "capabilities": {"drop": ["NET_RAW"], "add": ["NET_BIND_SERVICE"]}}]),
                    ("already-defaulted", [{"allowPrivilegeEscalation": False, "capabilities": {"drop": ["ALL"]}}]),
                    ("mixed", [None, {"allowPrivilegeEscalation": True}, {"allowPrivilegeEscalation": False}]),
                ):
                    spec = {"containers": [{"name": "untouched", "image": "busybox:1.37.0",
                                            "securityContext": {"allowPrivilegeEscalation": True}}],
                            "ephemeralContainers": [{"name": "debug", "image": "busybox:1.37.0",
                                                     "securityContext": {"allowPrivilegeEscalation": False}}]}
                    spec[field] = [{"name": f"tested-{index}", "image": "busybox:1.37.0",
                                    **({"securityContext": deepcopy(context)} if context is not None else {})}
                                   for index, context in enumerate(contexts)]
                    obj = resource(kind, spec)
                    patched = deepcopy(obj)
                    for container in pod_spec(patched)[field]:
                        context = container.setdefault("securityContext", {})
                        if context.get("allowPrivilegeEscalation") is not True:
                            context["allowPrivilegeEscalation"] = False
                            context.setdefault("capabilities", {})["drop"] = ["ALL"]
                    add(name, kind + "-" + label, obj, patched)
                    add(name, kind + "-" + label + "-idempotent", patched, patched)
                if field == "initContainers":
                    obj = resource(kind, {"containers": [{"name": "main", "image": "busybox:1.37.0"}]})
                    add(name, kind + "-absent-init", obj, obj)
        elif source_name == "mutate-topology-spread":
            for label, existing in (("missing", None), ("empty", []), ("existing", [{
                "maxSkew": 2, "topologyKey": "topology.kubernetes.io/zone",
                "whenUnsatisfiable": "ScheduleAnyway", "labelSelector": {"matchLabels": {"app": "fixture"}},
            }])):
                obj = resource("Deployment", {"containers": [{"name": "main", "image": "busybox:1.37.0"}]})
                if existing is not None:
                    pod_spec(obj)["topologySpreadConstraints"] = existing
                patched = deepcopy(obj)
                if not existing:
                    pod_spec(patched)["topologySpreadConstraints"] = [{
                        "maxSkew": 1, "topologyKey": "kubernetes.io/hostname",
                        "whenUnsatisfiable": "ScheduleAnyway",
                        "labelSelector": {"matchLabels": deepcopy(obj["spec"]["selector"]["matchLabels"])},
                    }]
                add(name, label, obj, patched)
                add(name, label + "-idempotent", patched, patched)
        else:
            defaults = {key[2:-1]: value for key, value in
                        rule["mutate"]["patchStrategicMerge"]["metadata"]["annotations"].items()}
            for label, annotations in (("missing", None), ("empty", {}), ("partial", {
                "traefik.ingress.kubernetes.io/router.tls": "false", "unrelated.example/key": "preserved",
            }), ("existing", {key: "" for key in defaults})):
                obj = {"apiVersion": "networking.k8s.io/v1", "kind": "Ingress",
                       "metadata": {"name": "fixture", "namespace": "policy-test"},
                       "spec": {"ingressClassName": "traefik", "defaultBackend": {
                           "service": {"name": "fixture", "port": {"number": 80}}}}}
                if annotations is not None:
                    obj["metadata"]["annotations"] = annotations
                patched = deepcopy(obj)
                for key, value in defaults.items():
                    patched["metadata"].setdefault("annotations", {}).setdefault(key, value)
                add(name, label, obj, patched)
                add(name, label + "-idempotent", patched, patched)
        probe = next(case for case in cases if case["policy"] == name)
        exclusions = rule["exclude"]["any"][0]["resources"]["namespaces"]
        for excluded in exclusions:
            obj = deepcopy(probe["object"])
            obj["metadata"]["namespace"] = excluded
            add(name, "excluded-" + excluded, obj, obj)
        obj, patched = deepcopy(probe["object"]), deepcopy(probe["expectedPatched"])
        obj["metadata"]["namespace"] = patched["metadata"]["namespace"] = exclusions[0] + "-untrusted"
        add(name, "exclusion-lookalike", obj, patched)
    return cases


def mutation_baseline(suite, cli, evidence, directory):
    sources = candidate.reviewed_sources(candidate.REVIEWED_MUTATIONS)
    count = 0
    for policy in suite["mutationPolicies"]:
        labels = policy["metadata"]["labels"]
        source = deepcopy(sources[labels["homelab.rishik.dev/legacy-policy"]])
        source["spec"]["rules"] = [rule for rule in source["spec"]["rules"]
                                  if rule["name"] == labels["homelab.rishik.dev/legacy-rule"]]
        for operation in ("CREATE", "UPDATE"):
            cases = [case for case in suite["mutationCases"]
                     if case["policy"] == policy["metadata"]["name"] and case["operation"] == operation]
            folder = directory / (policy["metadata"]["name"] + "-" + operation.lower())
            folder.mkdir()
            (folder / "policy.yaml").write_text(yaml.safe_dump(source), encoding="utf-8")
            (folder / "resources.yaml").write_text(yaml.safe_dump_all(
                [case["object"] for case in cases]), encoding="utf-8")
            output = folder / "patched.yaml"
            command([str(cli), "apply", str(folder / "policy.yaml"), "--resource",
                     str(folder / "resources.yaml"), "--set", "request.operation=" + operation,
                     "--output", str(output)], ROOT, evidence, folder.name + ".log")
            documents = list(yaml.safe_load_all(output.read_text(encoding="utf-8")))
            actual = {doc["metadata"]["name"]: doc for doc in documents if doc is not None}
            expected = {case["object"]["metadata"]["name"]: case["legacyExpectedPatched"] for case in cases}
            if actual != expected:
                details = {"actual": actual, "expected": expected}
                (evidence / (folder.name + "-mismatch.json")).write_text(
                    json.dumps(details, indent=2) + "\n", encoding="utf-8")
                raise RuntimeError(f"Exact original-engine patched objects differ: {folder.name}")
            count += len(cases)
    return count


def baseline(suite, cli, evidence, directory):
    sources = candidate.reviewed_sources()
    count = 0
    for policy in suite["policies"]:
        labels = policy["metadata"]["labels"]
        source_name, rule_name = labels["homelab.rishik.dev/legacy-policy"], labels["homelab.rishik.dev/legacy-rule"]
        source = deepcopy(sources[source_name])
        source["spec"]["rules"] = [rule for rule in source["spec"]["rules"] if rule["name"] == rule_name]
        cases = [case for case in suite["cases"] if case["policy"] == policy["metadata"]["name"]
                 and case["operation"] in ("CREATE", "DELETE")
                 and case["username"] and not case["subresource"]]
        folder = directory / policy["metadata"]["name"]
        folder.mkdir()
        (folder / "policy.yaml").write_text(yaml.safe_dump(source), encoding="utf-8")
        (folder / "exceptions.yaml").write_text(yaml.safe_dump_all(
            [source for source in sources.values() if source["kind"] == "PolicyException"]
        ), encoding="utf-8")
        (folder / "resources.yaml").write_text(yaml.safe_dump_all(
            [case["object"] or case["oldObject"] for case in cases]
        ), encoding="utf-8")
        (folder / "userinfo.yaml").write_text(yaml.safe_dump({
            "apiVersion": "cli.kyverno.io/v1alpha1", "kind": "UserInfo",
            "metadata": {"name": "synthetic"}, "userInfo": {"username": USER},
        }), encoding="utf-8")
        results = []
        for case in cases:
            resource = case["object"] or case["oldObject"]
            kind = resource["kind"]
            prefix = "" if kind in ("Pod", "Namespace") else (
                "autogen-cronjob-" if kind == "CronJob" else "autogen-"
            )
            results.append({
                "policy": source_name, "rule": prefix + rule_name, "kind": kind,
                "resources": [resource["metadata"]["name"]], "operation": case["operation"],
                "result": case["legacyExpected"],
            })
        (folder / "kyverno-test.yaml").write_text(yaml.safe_dump({
            "apiVersion": "cli.kyverno.io/v1alpha1", "kind": "Test",
            "metadata": {"name": "actual-legacy-rule"}, "policies": ["policy.yaml"],
            "exceptions": ["exceptions.yaml"], "resources": ["resources.yaml"],
            "userinfo": "userinfo.yaml", "results": results,
        }), encoding="utf-8")
        count += len(results)
    output = command([str(cli), "test", str(directory), "--require-tests", "--fail-only",
                      "--remove-color"], ROOT, evidence, "legacy-cli.log")
    summary = re.search(r"Test Summary:\s*(\d+) out of (\d+) tests failed", output)
    if not summary or tuple(map(int, summary.groups())) != (0, count):
        raise RuntimeError(f"Native legacy summary did not prove all {count} expected results")
    return count


def structural_schemas(suite, sdk, evidence):
    from jsonschema import Draft7Validator

    checks = []
    for kind, plural, documents in (
        ("ValidatingPolicy", "validatingpolicies", suite["policies"]),
        ("PolicyException", "policyexceptions", suite["exceptions"]),
        ("MutatingPolicy", "mutatingpolicies", suite["mutationPolicies"]),
    ):
        path = sdk / "config/crds/policies.kyverno.io" / f"policies.kyverno.io_{plural}.yaml"
        data = path.read_bytes()
        crd = yaml.safe_load(data)
        if crd["spec"]["group"] != "policies.kyverno.io" or crd["spec"]["names"]["kind"] != kind:
            raise RuntimeError(f"Wrong actual SDK CRD: {path.name}")
        version = next(item for item in crd["spec"]["versions"] if item["name"] == "v1")
        if not version["served"]:
            raise RuntimeError(f"Actual SDK CRD does not serve v1: {kind}")
        validator = Draft7Validator(version["schema"]["openAPIV3Schema"])
        for document in documents:
            if document["apiVersion"] != candidate.API or document["kind"] != kind:
                raise RuntimeError(f"Wrong candidate API identity: {document['metadata']['name']}")
            errors = sorted(validator.iter_errors(document), key=lambda error: str(error.path))
            if errors:
                raise RuntimeError(f"{document['metadata']['name']}: structural CRD errors: "
                                   + "; ".join(error.message for error in errors))
        checks.append({
            "kind": kind, "version": version["name"], "documents": len(documents),
            "sdk_crd_sha256": hashlib.sha256(data).hexdigest(),
        })
    result = {
        "checks": checks,
        "scope": "exact SDK v1 CRD structural shape; native policy expressions checked separately",
        "crd_admission_cel": "not executed offline; server dry-run still required before activation",
    }
    (evidence / "structural-schemas.json").write_text(json.dumps(result, indent=2) + "\n", encoding="utf-8")
    return result


def run(args):
    evidence = args.evidence.resolve()
    evidence.mkdir(parents=True, exist_ok=True)
    policies, exceptions = candidate.build()
    suite = fixtures(policies, exceptions)
    suite["mutationPolicies"] = candidate.build_mutations()
    suite["mutationCases"] = mutation_fixtures(suite["mutationPolicies"])
    (evidence / "suite.json").write_text(json.dumps(suite, indent=2) + "\n", encoding="utf-8")
    receipt = {"outcome": "incomplete", "scope": "held-validation-controls-only",
               "sdk_version": args.sdk_version, "requests_expected": len(suite["cases"]),
               "policies": len(policies), "exceptions": len(exceptions),
               "production_activation": False}
    receipt["mutation_requests_expected"] = len(suite["mutationCases"])
    receipt["mutation_policies"] = len(suite["mutationPolicies"])
    receipt["scope"] = "held-validation-and-mutation-controls"
    receipt["intentional_mutation_order_repair"] = {
        "original": "actual SMP groups patched containers before untouched true-valued containers",
        "candidate": "preserve input regular/init container order, including init startup order",
        "original_and_candidate_exact_objects_checked_separately": True,
        "production_activated": False,
    }
    receipt["intentional_audit_repair"] = {
        "policy": AUDIT_REPAIR,
        "original": "unquoted false identifier makes the Boolean false/missing Audit cases pass",
        "candidate": "report these existing violations as Audit failures; never deny admission",
        "production_activated": False,
    }
    (evidence / "evidence.json").write_text(json.dumps(receipt, indent=2), encoding="utf-8")
    if args.fixtures_only:
        print(f"Generated {len(suite['cases'])} validation and {len(suite['mutationCases'])} "
              "mutation requests; no native acceptance claimed")
        return
    if not args.legacy_cli:
        raise RuntimeError("--legacy-cli is required for actual native acceptance")
    cli = args.legacy_cli.resolve()
    version = command([str(cli), "version"], ROOT, evidence, "legacy-version.txt")
    if not re.search(r"(?m)^Version:\s*v?1\.19\.2\s*$", version):
        raise RuntimeError("Legacy baseline must use actual Kyverno 1.19.2")
    with tempfile.TemporaryDirectory(prefix="kyverno-cel-native-") as temporary:
        temporary = Path(temporary)
        baseline_dir = temporary / "legacy"
        baseline_dir.mkdir()
        receipt["legacy_expectations_passed"] = baseline(suite, cli, evidence, baseline_dir)
        receipt["legacy_exact_mutations_passed"] = mutation_baseline(suite, cli, evidence, baseline_dir)
        env = os.environ.copy()
        env.update({"GOTOOLCHAIN": "local", "GOSUMDB": "sum.golang.org", "CGO_ENABLED": "0"})
        go_version = command(["go", "version"], ROOT, evidence, "go-version.txt", env)
        if not re.search(r"\bgo1\.27\.4\b", go_version):
            raise RuntimeError("Native engine must be built with the selected patched Go 1.27.4")
        module = json.loads(command(
            ["go", "mod", "download", "-json", "github.com/kyverno/kyverno@v" + args.sdk_version],
            temporary, evidence, "upstream-module.json", env,
        ))
        if module.get("Version") != "v" + args.sdk_version or not module.get("Sum", "").startswith("h1:"):
            raise RuntimeError("Cannot bind the downloaded actual upstream source module")
        sdk = temporary / "sdk"
        shutil.copytree(module["Dir"], sdk)
        receipt["structural_schemas"] = structural_schemas(suite, sdk, evidence)
        (sdk / "cmd").chmod(0o755)
        target = sdk / "cmd/homelab-cel-native"
        target.mkdir()
        shutil.copy2(Path(__file__).with_name("kyverno-cel-native.go"), target / "main.go")
        binary = evidence / "native-engine"
        command(["go", "build", "-mod=readonly", "-trimpath", "-o", str(binary),
                 "./cmd/homelab-cel-native"], sdk, evidence, "native-build.log", env)
        command(["go", "version", "-m", str(binary)], sdk, evidence, "native-build-info.txt", env)
        command([str(binary), str(evidence / "suite.json"), str(evidence / "native-results.json")],
                temporary, evidence, "native-engine.log", env)
    results = json.loads((evidence / "native-results.json").read_text(encoding="utf-8"))
    if len(results) != len(suite["cases"]):
        raise RuntimeError("The actual engine did not execute every synthetic admission request")
    mutation_results = json.loads((evidence / "native-mutation-results.json").read_text(encoding="utf-8"))
    if len(mutation_results) != len(suite["mutationCases"]):
        raise RuntimeError("The actual mutation engine did not execute every request")
    receipt.update({
        "outcome": "passed", "requests_passed": len(results), "upstream_module_sum": module["Sum"],
        "mutation_requests_passed": len(mutation_results),
        "engine_sha256": hashlib.sha256(binary.read_bytes()).hexdigest(),
        "suite_sha256": hashlib.sha256((evidence / "suite.json").read_bytes()).hexdigest(),
        "source_sha": os.environ.get("GITHUB_SHA", subprocess.check_output(
            ["git", "rev-parse", "HEAD"], cwd=ROOT, text=True).strip()),
    })
    (evidence / "evidence.json").write_text(json.dumps(receipt, indent=2) + "\n", encoding="utf-8")
    print(json.dumps(receipt, indent=2))


if __name__ == "__main__":
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--sdk-version", choices=("1.19.2", "1.20.0"), required=True)
    parser.add_argument("--legacy-cli", type=Path)
    parser.add_argument("--evidence", type=Path, required=True)
    parser.add_argument("--fixtures-only", action="store_true")
    run(parser.parse_args())
