#!/usr/bin/env python3
"""Local source-contract simulations only; no cluster, SSH, sudo or native acceptance."""

import base64
import copy
from contextlib import contextmanager, ExitStack
import hashlib
import importlib.util
import json
from pathlib import Path
import shlex
import stat
import sys
import threading
import time
from types import SimpleNamespace
import unittest
from unittest.mock import MagicMock, patch
import zlib


sys.dont_write_bytecode = True
DIRECTORY = Path(__file__).resolve().parent


def load(name, filename):
    spec = importlib.util.spec_from_file_location(name, DIRECTORY / filename)
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


c = load("cold_test_source", "node-maintenance-cold.py")
h = load("cold_test_host", "node-maintenance-host.py")
peer_source = load("cold_test_peer", "node-maintenance-peer.py")

IM_IMAGE = "longhornio/longhorn-instance-manager:v1.12.1"
PAUSED_DEFAULT_RULES = {
    "scaleUp": {"selectPolicy": "Disabled", "stabilizationWindowSeconds": 0,
                "policies": [{"type": "Pods", "value": 4, "periodSeconds": 15},
                             {"type": "Percent", "value": 100, "periodSeconds": 15}]},
    "scaleDown": {"selectPolicy": "Disabled",
                  "policies": [{"type": "Percent", "value": 100, "periodSeconds": 15}]},
}


def default_hpa_behavior(spec):
    """API v2 defaulting fills rules only when behavior is non-nil."""
    if spec.get("behavior") is None:
        return
    for direction, defaults in PAUSED_DEFAULT_RULES.items():
        rules = spec["behavior"].setdefault(direction, {})
        for name, value in defaults.items():
            rules.setdefault(name, copy.deepcopy(value) if name != "selectPolicy" else "Max")


def native_im_labels(node):
    image_checksum = hashlib.sha512(b"longhornio-longhorn-instance-manager-v1.12.1").hexdigest()[:8]
    return {"longhorn.io/managed-by": "longhorn-manager", "longhorn.io/component": "instance-manager",
            "longhorn.io/instance-manager-type": "aio", "longhorn.io/node": node,
            "longhorn.io/data-engine": "v1", "longhorn.io/instance-manager-image": "imi-" + image_checksum}


def native_hpa(ns, name, target, scaled, behavior, replicas=1):
    spec = {"scaleTargetRef": {"apiVersion": "apps/v1", "kind": "Deployment", "name": target},
            "minReplicas": 1, "maxReplicas": 2,
            "metrics": [{"type": "External", "external": {
                "metric": {"name": "s0-http-pending-requests",
                           "selector": {"matchLabels": {"scaledobject.keda.sh/name": scaled["metadata"]["name"]}}},
                "target": {"type": "AverageValue", "averageValue": "100"}}}]}
    if behavior is not None:
        spec["behavior"] = copy.deepcopy(behavior)
        default_hpa_behavior(spec)
    value = obj("HorizontalPodAutoscaler", ns, name, spec,
                {"kind": "ScaledObject", "name": scaled["metadata"]["name"], "uid": scaled["metadata"]["uid"]})
    value["metadata"]["ownerReferences"][0].update(apiVersion="keda.sh/v1alpha1", blockOwnerDeletion=True)
    value["status"] = {"currentReplicas": replicas, "desiredReplicas": replicas, "currentMetrics": [],
                       "conditions": [{"type": "AbleToScale", "status": "True", "reason": "SucceededGetScale"}]}
    return value


def obj(kind, ns, name, spec, owner=None):
    return {"kind": kind, "metadata": {"namespace": ns, "name": name, "uid": ns + "/" + name + "-uid",
                                      "generation": 1, "resourceVersion": "1",
                                      "labels": {"app": name},
                                      "ownerReferences": ([{**owner, "controller": True}] if owner else [])},
            "spec": copy.deepcopy(spec), "status": {"observedGeneration": 1}}


def controller(ns, name, kind="Deployment", replicas=1):
    result = obj(kind, ns, name, {
        "replicas": replicas, "selector": {"matchLabels": {"app": name}},
        "template": {"metadata": {"labels": {"app": name}},
                     "spec": {"containers": [{"name": "app", "image": "example.invalid/app@sha256:" + "a" * 64}]}}})
    result["status"].update(replicas=replicas, readyReplicas=replicas, currentRevision="revision", updateRevision="revision")
    return result


def pod_for(control, node, suffix):
    meta = control["metadata"]
    rs = obj("ReplicaSet", meta["namespace"], meta["name"] + "-rs", control["spec"],
             {"kind": "Deployment", "name": meta["name"], "uid": meta["uid"]})
    owner = ({"kind": "ReplicaSet", "name": rs["metadata"]["name"], "uid": rs["metadata"]["uid"]}
             if control["kind"] == "Deployment" else
             {"kind": "StatefulSet", "name": meta["name"], "uid": meta["uid"]})
    pod = obj("Pod", meta["namespace"], meta["name"] + suffix, control["spec"]["template"]["spec"], owner)
    pod["metadata"]["labels"] = meta["labels"].copy()
    pod["spec"]["nodeName"] = node
    pod["status"] = {"phase": "Running", "conditions": [{"type": "Ready", "status": "True"}]}
    return pod, rs


class MemoryJournal:
    def __init__(self, state):
        self.state = copy.deepcopy(state)
        self.writes = []

    def read(self):
        return copy.deepcopy(self.state)

    def write(self, state):
        self.state = copy.deepcopy(state)
        self.writes.append(copy.deepcopy(state))

    def transaction(self, callback):
        state = self.read()
        result = callback(state)
        self.write(state)
        return result

    def recovery(self, callback):
        return callback()


class FakeAPI:
    """Simulated API controllers actually undo unsafe ordering in this fixture."""
    def __init__(self, policy):
        self.policy, self.resources, self.events, self.fail = policy, {}, [], None
        self.host_boot = "old-worker2"
        self.plan = None

    def add(self, resource, value):
        self.resources.setdefault(resource, []).append(value)
        return value

    def items(self, resource, namespace=None):
        rows = self.resources.get(resource, [])
        return [r for r in rows if namespace is None or r["metadata"].get("namespace") == namespace]

    def get(self, resource, namespace=None, name=None):
        aliases = {"node": "nodes", "helmrelease": "helmreleases.helm.toolkit.fluxcd.io"}
        resource = aliases.get(resource, resource)
        rows = [r for r in self.items(resource, namespace) if r["metadata"]["name"] == name]
        if len(rows) != 1:
            raise RuntimeError("Missing/ambiguous simulated resource " + resource + "/" + str(name))
        return rows[0]

    def kube(self, *args):
        self.events.append(("kube", args))
        if args[:1] == ("cordon",):
            self.get("nodes", name=c.WORKER)["spec"]["unschedulable"] = True
            # Mock the native controller only after its actual contract has been met.
            c.cold_storage(self.plan, self.policy, self, detached=True)
            for r in self.items("replicas.longhorn.io"):
                if r["spec"].get("nodeID") == c.WORKER:
                    peers = [p for p in self.items("replicas.longhorn.io")
                             if p["spec"]["volumeName"] == r["spec"]["volumeName"] and p["spec"].get("nodeID") == c.PEER
                             and p["spec"].get("healthyAt") and not p["spec"].get("failedAt")]
                    if not peers:
                        return ""
            self.resources["poddisruptionbudgets"] = [
                r for r in self.items("poddisruptionbudgets")
                if r["metadata"]["name"] != c.IM_NAMES[c.WORKER]]
        elif args[:1] == ("uncordon",):
            self.get("nodes", name=c.WORKER)["spec"]["unschedulable"] = False
            old = self.plan["native"][c.WORKER]
            if not any(p["metadata"]["name"] == old["name"] for p in self.items("pods", "storage")):
                original = copy.deepcopy(self.original_im_pod)
                original["metadata"]["uid"] += "-returned"
                self.add("pods", original)
            if not any(p["metadata"]["name"] == old["name"] for p in self.items("poddisruptionbudgets", "storage")):
                pdb = copy.deepcopy(self.original_im_pdb)
                pdb["metadata"]["uid"] += "-returned"
                self.add("poddisruptionbudgets", pdb)
            for p in self.items("pods"):
                if p["status"].get("phase") == "Pending":
                    p["spec"]["nodeName"] = c.WORKER
                    p["status"] = {"phase": "Running", "conditions": [{"type": "Ready", "status": "True"}]}
        elif "delete" in args and c.m.LOCK in args:
            self.resources["configmap"] = []
        return ""

    def patch(self, row, value, operations):
        previous_replicas = value["spec"].get("replicas", 1)
        if row["kind"] in ("Deployment", "StatefulSet") and c.identity(value) not in c.OPERATORS:
            desired = next((op["value"] for op in operations if op["path"] == "/spec/replicas" and op["op"] == "add"),
                           value["spec"].get("replicas", 1))
            if (value["spec"].get("replicas", 1) == 0) != (desired == 0):
                handler = self.get("cronjobs", *c.CROSS_PRODUCER)
                if handler["spec"].get("suspend") is not True:
                    raise RuntimeError("Cross-namespace writer was not stopped before app pause/warmup")
        self.events.append(("patch", c.key(row), copy.deepcopy(operations)))
        for op in operations:
            segments = [s.replace("~1", "/").replace("~0", "~") for s in op["path"].split("/")[1:]]
            target = value
            for segment in segments[:-1]:
                target = target[segment]
            if op["op"] == "remove":
                target.pop(segments[-1])
            elif op["op"] == "test":
                assert target[segments[-1]] == op["value"]
            else:
                target[segments[-1]] = op["value"]
        value["metadata"]["generation"] += 1
        value["metadata"]["resourceVersion"] = str(int(value["metadata"]["resourceVersion"]) + 1)
        if row["kind"] == "PodDisruptionBudget":
            value["status"].update(observedGeneration=value["metadata"]["generation"],
                                   disruptionsAllowed=1 if value["spec"].get("maxUnavailable") == 1 else 0)
        if row["kind"] == "ScaledObject":
            annotations = value["metadata"].get("annotations", {})
            value["status"]["conditions"] = [{"type": "Paused", "status": "True"}]
            hpa = next(r for r in self.items("horizontalpodautoscalers") if c.owners(r)[0]["uid"] == row["uid"])
            hpa["spec"] = copy.deepcopy(self.original_hpa_specs[hpa["metadata"]["uid"]])
            for name, direction in zip(c.PAUSE, ("scaleDown", "scaleUp")):
                if annotations.get(name) == "true":
                    hpa["spec"].setdefault("behavior", {}).setdefault(direction, {})["selectPolicy"] = "Disabled"
            default_hpa_behavior(hpa["spec"])
            hpa["metadata"]["generation"] += 1
            # horizontal.go setStatus does not populate observedGeneration.
            hpa["status"].pop("observedGeneration", None)
        if row["kind"] in ("Deployment", "StatefulSet"):
            count = value["spec"].get("replicas", 1)
            if count == 0 and previous_replicas != 0 and (row["namespace"], row["name"]) not in c.OPERATORS:
                operators = [d for d in self.items("deployments") if c.identity(d) in c.OPERATORS]
                if any(d["spec"]["replicas"] != 0 for d in operators):
                    # Prometheus parent and KEDA min>0 correction cannot be wished away by the mock.
                    raise RuntimeError("Real producer/operator was not stopped first")
                if (row["namespace"], row["name"]) in c.BACKENDS:
                    consumers = [d for d in self.items("deployments") + self.items("statefulsets")
                                 if c.identity(d)[0] in c.ALLOWED and c.identity(d) not in c.BACKENDS | c.OPERATORS]
                    if any(d["spec"].get("replicas", 1) != 0 for d in consumers):
                        raise RuntimeError("Consumers/producers are still writing to this backend")
            self.resources["pods"] = [p for p in self.items("pods")
                                       if c.pod_controller(p, self.rs_map())["uid"] != row["uid"]]
            for i in range(count):
                pod, _ = pod_for(value, c.PEER, "-restored-" + str(i))
                self.add("pods", pod)
            value["status"].update(observedGeneration=value["metadata"]["generation"], replicas=count, readyReplicas=count)
            backends = [d for d in self.items("deployments") + self.items("statefulsets") if c.identity(d) in c.BACKENDS]
            if all(d["spec"]["replicas"] == 0 for d in backends):
                for v in self.items("volumes.longhorn.io"):
                    if v["metadata"]["name"] in self.policy["quarantined_volumes"]:
                        continue
                    v["spec"]["nodeID"] = ""
                    v["status"].update(state="detached", currentNodeID="")
                for r in self.items("replicas.longhorn.io"):
                    r["spec"]["desireState"] = r["status"]["currentState"] = "stopped"
                    r["status"]["instanceManagerName"] = ""
                for e in self.items("engines.longhorn.io"):
                    e["status"]["currentState"] = "stopped"
            elif count and c.identity(value) in c.BACKENDS:
                # Model native two-RW warmup, not a singleton write acceptance.
                for v in self.items("volumes.longhorn.io"):
                    if v["metadata"]["name"] not in self.policy["quarantined_volumes"]:
                        v["spec"]["nodeID"] = c.PEER
                        v["status"].update(state="attached", robustness="healthy", currentNodeID=c.PEER)
                for r in self.items("replicas.longhorn.io"):
                    if r["spec"]["volumeName"] not in self.policy["quarantined_volumes"]:
                        r["spec"]["desireState"] = r["status"]["currentState"] = "running"
                        r["spec"]["lastHealthyAt"] = "2026-11-24T04:55:00Z"
                        r["status"]["instanceManagerName"] = c.IM_NAMES[r["spec"]["nodeID"]]
                for e in self.items("engines.longhorn.io"):
                    if e["spec"]["volumeName"] not in self.policy["quarantined_volumes"]:
                        e["status"]["currentState"] = "running"
        if self.fail == c.key(row):
            self.fail = None
            raise RuntimeError("Ambiguous response after simulated patch")

    def rs_map(self):
        return {r["metadata"]["uid"]: r for r in self.items("replicasets")}

    def host_audit(self, policy, name):
        return {"boot_id": self.host_boot, "kernel": policy["kernel"], "default_target": "multi-user.target"}

    def host_idle(self, policy, step_id, run_id=None):
        # systemd-run's transient units live under /run and vanish at reboot.
        return self.host_boot == "old-worker2"

    def worker_status(self, policy, run_id):
        # Reads never invent an acknowledgment from controller revocation.
        return {"record": self.worker.read(), "unit": copy.deepcopy(self.worker_native),
                "unit_sha256": self.worker_unit_sha256, "finalizer": copy.deepcopy(self.finalizer)}


def fixture(hpa_behavior=None, handler_suspend=False):
    policy = json.loads(c.m.POLICY.read_text())
    api = FakeAPI(policy)
    sts = {"plex-plex-media-server", "alertmanager-kube-prometheus-stack-alertmanager", "loki",
           "prometheus-kube-prometheus-stack-prometheus", "paperless-paperless-ngx-postgresql",
           "paperless-paperless-ngx-redis", "sure-finance-maybe-finance-postgresql"}
    for ns, names in c.ALLOWED.items():
        for name in sorted(names):
            kind = "StatefulSet" if name in sts else "Deployment"
            count = 0 if name == "paperless-paperless-ngx" else 1
            d = controller(ns, name, kind, count)
            if name.startswith(("prometheus-kube-", "alertmanager-kube-")):
                parent_kind = "Prometheus" if name.startswith("prometheus-") else "Alertmanager"
                parent = api.add(c.RESOURCES[parent_kind], obj(parent_kind, ns, name.removeprefix(parent_kind.lower() + "-"), {}))
                d["metadata"]["ownerReferences"] = [{"kind": parent_kind, "name": parent["metadata"]["name"],
                                                     "uid": parent["metadata"]["uid"], "controller": True}]
            api.add(c.RESOURCES[kind], d)
            p, rs = pod_for(d, c.WORKER if len(name) % 2 else c.PEER, "-original")
            if count:
                api.add("pods", p)
            if kind == "Deployment":
                api.add("replicasets", rs)
    images = {
        "keda-operator": "ghcr.io/kedacore/keda:" + json.loads((c.m.ROOT / "apps/_shared/http-addon-owned/source.json").read_text())["native"]["core_image_tags"]["keda"],
        "keda-add-ons-http-controller-manager": "ghcr.io/blazepower/http-addon-owned-operator@sha256:849c089d111384df6bf4c59725ff886fc145bd423909084b774d6a31c8942e53",
        "keda-add-ons-http-external-scaler": "ghcr.io/blazepower/http-addon-owned-scaler@sha256:6d3d3953627f7049ecee26b9034d346d2a3f96c2bcab1f2561557acd5f391130",
        "keda-add-ons-http-interceptor": "ghcr.io/blazepower/http-addon-owned-interceptor@sha256:3b0aeb314779f6e379736ed0b00a987843caac7e6805e9fc930f462f07812a9d",
    }
    for ns, name in sorted(c.OPERATORS):
        if ns != "keda":
            continue
        d = controller(ns, name)
        d["spec"]["template"]["spec"]["containers"][0]["image"] = images[name]
        api.add("deployments", d)
        p, rs = pod_for(d, c.PEER, "-original")
        api.add("pods", p)
        api.add("replicasets", rs)
    for ns, name in sorted(c.HA):
        d = controller(ns, name, replicas=2)
        d["spec"]["template"]["spec"]["affinity"] = {"podAntiAffinity": {
            "requiredDuringSchedulingIgnoredDuringExecution": [{
                "topologyKey": "kubernetes.io/hostname", "labelSelector": {"matchLabels": {"app": name}}}]}}
        if ns == "csi-driver-nfs":
            d["metadata"]["labels"]["helm.sh/chart"] = "csi-driver-nfs-4.13.6"
            d["spec"]["template"]["spec"].update({
                "volumes": [{"name": "socket-dir", "emptyDir": {}},
                            {"name": "pods-mount-dir", "hostPath": {"path": "/var/lib/kubelet/pods", "type": "Directory"}}],
                "containers": [{"name": "nfs", "image": "example.invalid/nfs", "volumeMounts": [
                    {"name": "pods-mount-dir", "mountPath": "/var/lib/kubelet/pods", "mountPropagation": "Bidirectional"}]}]})
        api.add("deployments", d)
        for node, suffix in ((c.PEER, "-peer"), (c.WORKER, "-worker")):
            p, rs = pod_for(d, node, suffix)
            api.add("pods", p)
            if suffix == "-peer":
                api.add("replicasets", rs)
    schedules = [api.add("cronjobs", obj("CronJob", "media", name, {"suspend": False}))
                 for name in ("hardcover-sync", "radarr-codec-policy", "sonarr-codec-policy", "sonarr-queue-cleanup")]
    handler = api.add("cronjobs", obj("CronJob", "kube-system", "media-mount-handler", {
        "schedule": "*/5 * * * *", "concurrencyPolicy": "Forbid",
        "successfulJobsHistoryLimit": 1, "failedJobsHistoryLimit": 3,
        "jobTemplate": {"spec": {"ttlSecondsAfterFinished": 300, "template": {"spec": {
            "serviceAccountName": "media-mount-handler", "restartPolicy": "OnFailure",
            "containers": [{"name": "handler",
                            "image": "docker.io/alpine/k8s:1.34.13@sha256:4011c68d73de6803987509ad1ab9520124a27e31484e3f0e45f95a74ecac7a45",
                            "command": ["/bin/sh", "-c", "kubectl rollout restart deployment -n media radarr"]}]}}}}}))
    if handler_suspend is not None:
        handler["spec"]["suspend"] = handler_suspend
    handler["metadata"]["labels"].update({"kustomize.toolkit.fluxcd.io/name": "flux-system",
                                           "kustomize.toolkit.fluxcd.io/namespace": "flux-system"})
    handler["status"] = {}
    # Exact production inventory identities; only fake metadata/specs are used.
    releases = {"adguard-home": ["adguard-home"], "homebox": ["homebox"], "kaneo": ["kaneo"],
                "media": ["bazarr", "bookshelf", "calibre-web", "overseerr", "plex", "prowlarr", "qbittorrent", "radarr",
                          "rreading-glasses", "sabnzbd", "sonarr", "syncthing"],
                "monitoring": ["kube-prometheus-stack", "loki"], "paperless": ["paperless"],
                "siloarr": ["stash", "whisparr"], "sure-finance": ["sure-finance"]}
    reconciliation = []
    for ns, names in releases.items():
        for name in names:
            reconciliation.append(api.add(c.RESOURCES["HelmRelease"], obj("HelmRelease", ns, name, {})))
    for name in ("apps", "flux-system", "kyverno-install", "kyverno-policies"):
        reconciliation.append(api.add(c.RESOURCES["Kustomization"], obj("Kustomization", "flux-system", name, {})))
    for name in ("keda", "keda-add-ons-http"):
        api.add(c.RESOURCES["HelmRelease"], obj("HelmRelease", "keda", name, {}))
    api.add(c.RESOURCES["HelmRelease"], obj("HelmRelease", "monitoring", "kured", {"suspend": True}))
    scalers, hpas = [], []
    api.original_hpa_specs = {}
    for ns, name in (("homebox", "homebox"), ("media", "bookshelf"), ("media", "calibre-web"),
                     ("media", "overseerr"), ("paperless", "paperless-paperless-ngx")):
        target = api.get("deployments", ns, name)
        scaled = api.add(c.RESOURCES["ScaledObject"], obj("ScaledObject", ns, name + "-scaler",
                         {"scaleTargetRef": {"name": name}, "minReplicaCount": 1 if name == "calibre-web" else 0}))
        if hpa_behavior is not None:
            scaled["spec"]["advanced"] = {"horizontalPodAutoscalerConfig": {"behavior": copy.deepcopy(hpa_behavior)}}
        hpa = api.add("horizontalpodautoscalers", native_hpa(
            ns, name + "-hpa", name, scaled, hpa_behavior, replicas=target["spec"]["replicas"]))
        sr, hr = c.binding(scaled, {"pause_annotations": {}}), c.binding(hpa)
        for row in (sr, hr):
            row["target"] = {"kind": "Deployment", "name": name, "uid": target["metadata"]["uid"]}
        scalers.append(sr)
        hpas.append(hr)
        api.original_hpa_specs[hpa["metadata"]["uid"]] = copy.deepcopy(hpa["spec"])
    interceptor = api.get("deployments", "keda", "keda-add-ons-http-interceptor")
    scaled = api.add(c.RESOURCES["ScaledObject"], obj("ScaledObject", "keda", "keda-add-ons-http-interceptor",
                    {"scaleTargetRef": {"name": interceptor["metadata"]["name"]}, "minReplicaCount": 1}))
    if hpa_behavior is not None:
        scaled["spec"]["advanced"] = {"horizontalPodAutoscalerConfig": {"behavior": copy.deepcopy(hpa_behavior)}}
    hpa = api.add("horizontalpodautoscalers", native_hpa(
        "keda", "keda-hpa-keda-add-ons-http-interceptor", interceptor["metadata"]["name"], scaled, hpa_behavior))
    api.original_hpa_specs[hpa["metadata"]["uid"]] = copy.deepcopy(hpa["spec"])
    for node in policy["nodes"]:
        n = obj("Node", "", node["name"], {"unschedulable": node["name"] == c.SERVER})
        n["status"] = {"conditions": [{"type": "Ready", "status": "True"}],
                       "nodeInfo": {"kubeletVersion": "v1.35.10+rke2r1"},
                       "allocatable": {"cpu": "100", "memory": "100Gi", "pods": "1000"}}
        api.add("nodes", n)
    native = {}
    for node, name in c.IM_NAMES.items():
        im = api.add("instancemanagers.longhorn.io", obj("InstanceManager", "storage", name,
                     {"nodeID": node, "type": "aio", "image": IM_IMAGE, "dataEngine": "v1"}))
        im["metadata"]["labels"] = native_im_labels(node)
        im["status"] = {"currentState": "running", "instanceEngines": {}, "instanceReplicas": {}, "instanceEngineFrontends": {}}
        pod = api.add("pods", obj("Pod", "storage", name,
                      {"nodeName": node, "containers": [{"name": "instance-manager", "image": im["spec"]["image"],
                                                       "resources": {"requests": {"cpu": "300m"}}}],
                       "volumes": [{"name": k, "hostPath": v} for k, v in c.IM_PATHS.items()]},
                      {"kind": "InstanceManager", "name": name, "uid": im["metadata"]["uid"]}))
        pod["status"] = {"phase": "Running", "conditions": [{"type": "Ready", "status": "True"}]}
        pod["metadata"]["labels"].update(native_im_labels(node))
        pdb = api.add("poddisruptionbudgets", obj("PodDisruptionBudget", "storage", name,
                      {"minAvailable": 1, "selector": {"matchLabels": native_im_labels(node)}}))
        native[node] = c.im_binding(im, pod, pdb, node)
        if node == c.WORKER:
            api.original_im_pod, api.original_im_pdb = copy.deepcopy(pod), copy.deepcopy(pdb)
    for setting, value in (("node-drain-policy", "block-if-contains-last-replica"),
                           ("kubernetes-cluster-autoscaler-enabled", "false"), ("default-instance-manager-image", IM_IMAGE)):
        api.add("settings.longhorn.io", {**obj("Setting", "storage", setting, {}), "value": value})
    manager = api.add("daemonsets", controller("storage", "longhorn-manager", kind="DaemonSet"))
    manager["spec"]["template"]["spec"]["containers"][0].update(
        name="longhorn-manager", image="longhornio/longhorn-manager:v1.12.1")
    api.add(c.RESOURCES["HelmRelease"], obj("HelmRelease", "storage", "longhorn",
            {"chart": {"spec": {"version": "1.12.1"}}}))
    for node in (c.PEER, c.WORKER):
        d = controller("storage", "longhorn-manager-" + node)
        p, _ = pod_for(d, node, "")
        p["metadata"]["labels"]["app"] = "longhorn-manager"
        p["metadata"]["ownerReferences"] = [{"kind": "DaemonSet", "name": "longhorn-manager",
                                             "uid": manager["metadata"]["uid"], "controller": True}]
        p["status"]["containerStatuses"] = [{"name": "longhorn-manager", "restartCount": 0,
                                             "imageID": "docker-pullable://longhornio/longhorn-manager@sha256:" + "a" * 64}]
        api.add("pods", p)
    for ns, name in c.TEMP_PDBS:
        app = name.removesuffix("-pdb")
        d = api.add("deployments", controller(ns, app))
        p, rs = pod_for(d, c.WORKER, "-original")
        api.add("pods", p)
        api.add("replicasets", rs)
        pdb = api.add("poddisruptionbudgets", obj("PodDisruptionBudget", ns, name,
                     {"minAvailable": "50%", "selector": {"matchLabels": {"app": app}}}))
        pdb["status"] = {"observedGeneration": 1, "disruptionsAllowed": 0}
    claims = []
    names = ["pvc-c200e7ff-e182-45e6-9523-c2fac869df7c", "regular-fixture", *policy["quarantined_volumes"]]
    for index, name in enumerate(names):
        quarantine = name in policy["quarantined_volumes"]
        volume = api.add("volumes.longhorn.io", obj("Volume", "storage", name,
                         {"numberOfReplicas": 2, "nodeID": "", "dataEngine": "v1"}))
        volume["status"] = {"state": "detached", "currentNodeID": "", "robustness": "healthy"}
        mode_map = {}
        for node in ([c.PEER] if quarantine else [c.PEER, c.WORKER]):
            replica = api.add("replicas.longhorn.io", obj("Replica", "storage", name + "-" + node, {
                "volumeName": name, "nodeID": node, "active": True, "healthyAt": "2026-11-20T00:00:00Z",
                "lastHealthyAt": "2026-11-20T00:00:00Z", "failedAt": "", "lastFailedAt": "",
                "dataDirectoryName": "fixture-directory-" + name, "desireState": "stopped", "dataEngine": "v1"}))
            replica["status"] = {"currentState": "stopped", "instanceManagerName": "", "starting": False}
            mode_map[replica["metadata"]["name"]] = "RW"
        engine = api.add("engines.longhorn.io", obj("Engine", "storage", name + "-engine",
                        {"volumeName": name, "salvageRequested": quarantine, "desireState": "stopped"}))
        engine["status"] = {"currentState": "stopped", "salvageExecuted": False, "replicaModeMap": mode_map}
        api.add("volumeattachments.longhorn.io", obj("VolumeAttachment", "storage", name, {"attachmentTickets": {}}))
        pvc_name = "sure-upgrade-20261123-encrypted" if index == 0 else "claim-" + name
        pvc = api.add("persistentvolumeclaims", obj("PersistentVolumeClaim", "sure-finance", pvc_name, {"volumeName": name}))
        pv = api.add("persistentvolumes", obj("PersistentVolume", "", name,
                     {"csi": {"driver": "driver.longhorn.io", "volumeHandle": name}}))
        claims.append({"namespace": "sure-finance", "pvc": pvc_name, "pvc_uid": pvc["metadata"]["uid"],
                       "pv": name, "pv_uid": pv["metadata"]["uid"], "volume": name})
    api.add("namespace", obj("Namespace", "", "kube-system", {}))
    inventory = {"namespaces": sorted(c.ALLOWED),
                 "controllers": [c.binding(d, {"replicas": d["spec"]["replicas"]})
                                 for d in api.items("deployments") + api.items("statefulsets") if c.identity(d)[1] in c.ALLOWED.get(c.identity(d)[0], set())],
                 "autoscalers": scalers, "hpas": hpas, "schedules": [c.suspend_binding(o) for o in schedules],
                 "reconciliation": [c.suspend_binding(o) for o in reconciliation], "claims": claims, "storage_users": [],
                 "cluster_uid": api.get("namespace", name="kube-system")["metadata"]["uid"],
                 "blockers": [], "baseline_health": {"quarantine": {}}}
    with patch.object(c.m, "get", side_effect=api.get):
        inventory["baseline_health"]["quarantine"] = c.m.quarantine_state(policy, api.items("volumes.longhorn.io"),
                                                                         api.items("engines.longhorn.io"), api.items("replicas.longhorn.io"))
    host = {"name": c.WORKER, "os": "ubuntu", "os_version": policy["ubuntu_lts"], "architecture": "x86_64",
            "kernel_metapackage_installed": True, "root_free_bytes": 10 * 1024**3, "boot_free_bytes": 1024**3,
            "memory_available_bytes": 10 * 1024**3, "sunreclaim_bytes": 0, "apt_metadata_epoch": time.time()}
    baseline = {"nodes": [n for n in policy["nodes"] if n["name"] == c.WORKER],
                "kubectl": {"client": "v1.35.10", "server": "v1.35.10+rke2r1"},
                "bindings": {n["name"]: {"boot_id": "old-worker2" if n["name"] == c.WORKER else "old-other",
                                          "kernel": "7.0.0-38-generic", "source_version": "v1.35.10+rke2r1",
                                          "pending_packages": []} for n in policy["nodes"]},
                "policy_sha256": c.sha(c.m.POLICY.read_bytes()), "runner_sha256": c.sha((DIRECTORY / "node-maintenance.py").read_bytes()),
                "host_sha256": c.sha((DIRECTORY / "node-maintenance-host.py").read_bytes())}
    peer_scope = {}
    peer_started = int(time.time()) - 10
    for index, node in enumerate((c.SERVER, c.WORKER)):
        peer_scope[node] = {"node": node, "peer": c.WORKER if node == c.SERVER else c.SERVER,
                            "setup_id": "3" * 32, "started_epoch": peer_started,
                            "expires_epoch": peer_started + 86400,
                            "source_sha256": {name: c.sha((DIRECTORY / name).read_bytes()) for name in peer_source.SOURCES},
                            "original_boot_id": baseline["bindings"][node]["boot_id"],
                            "original_kernel": baseline["bindings"][node]["kernel"],
                            "public_key_fingerprint": "SHA256:" + str(index) * 43,
                            "incoming_key_fingerprint": "SHA256:" + str(1 - index) * 43,
                            "peer_host_fingerprint": "SHA256:" + "h" * 43}
    peer_probes = {node: {"config_sha256": "a" * 64, "verified_epoch": time.time(),
                          "peer_proof_sha256": c.sha(c.canonical(peer_scope[proof["peer"]]))}
                   for node, proof in peer_scope.items()}
    for node, proof in peer_scope.items():
        proof["probe"] = peer_probes[node]
    with patch.object(c.m, "cold_inventory", return_value=inventory), patch.object(c.m, "audit_nodes", return_value={c.WORKER: host}), \
            patch.object(c.m, "build_plan", return_value=baseline), \
            patch.object(c.m, "remote", side_effect=lambda node, payload: peer_scope[node["name"]]
                         if payload["operation"] == "cold-peer-proof" else {"version": "v1.35.10+rke2r1", "sha256": "a" * 64}):
        plan = c.build_plan(policy, api)
    approval = c.sha(c.canonical(plan))
    api.plan = plan
    start = time.time()
    state = {"run_id": "1" * 32, "plan_sha256": approval, "started_epoch": start, "deadline_epoch": start + 43200,
             "last_epoch": start, "guard_registered": True, "mode": "disrupting", "phase": "registered",
             "mutations": {}, "completed": False, "host_step_id": "2" * 32, "host_started": False, "host_complete": False,
             "worker_ready_verified": True}
    contract = c.worker_contract(plan, policy, state["run_id"], state["host_step_id"], start, start + 43200)
    state.update(worker_contract=contract, worker_contract_sha256=c.sha(c.canonical(contract)))
    api.add("configmap", {**obj("ConfigMap", "kube-system", c.m.LOCK, {}), "data": {
        "run_id": state["run_id"], "plan_sha256": approval, "started_epoch": str(start), "deadline_epoch": str(start + 43200)}})
    api.get("configmap", "kube-system", c.m.LOCK)["data"]["host_step_id"] = state["host_step_id"]
    api.journal = MemoryJournal(state)
    api.worker = MemoryJournal({
        "contract": contract, "contract_sha256": state["worker_contract_sha256"], "phase": "waiting",
        "last_epoch": start, "ready_epoch": start, "peer_epoch": start, "pid": 1234,
        "original_host": {k: contract["bindings"][c.WORKER][k] for k in ("boot_id", "kernel", "pending_packages")},
        "finalizer_verified_epoch": start,
    })
    unit = "node-maintenance-cold-worker-" + state["run_id"]
    api.worker_native = {"LoadState": "loaded", "ActiveState": "active", "Job": "0", "MainPID": "1234",
                         "ControlPID": "0", "User": "root", "ControlGroup": "/system.slice/" + unit + ".service",
                         "FragmentPath": "/run/systemd/system/" + unit + ".service"}
    api.worker_unit_sha256 = c.sha(c.worker_unit(contract).encode())
    finalizer_units = c.worker_finalizer_units(contract)
    api.finalizer = {
        "service": {"LoadState": "loaded", "ActiveState": "inactive", "User": "root", "MainPID": "0",
                    "FragmentPath": "/run/systemd/system/" + unit + "-finalize.service"},
        "timer": {"LoadState": "loaded", "ActiveState": "active", "Job": "0", "Unit": unit + "-finalize.service",
                  "FragmentPath": "/run/systemd/system/" + unit + "-finalize.timer"},
        "service_sha256": c.sha(finalizer_units[unit + "-finalize.service"].encode()),
        "timer_sha256": c.sha(finalizer_units[unit + "-finalize.timer"].encode()),
    }
    return policy, api, plan, approval, api.journal


class ColdTests(unittest.TestCase):
    def setUp(self):
        self.policy, self.api, self.plan, self.approval, self.journal = fixture()
        self.get_patch = patch.object(c.m, "get", side_effect=self.api.get)
        self.get_patch.start()
        self.addCleanup(self.get_patch.stop)
        self.coordinator = c.Coordinator(self.plan, self.approval, self.policy, self.journal, self.api)
        self.coordinator.wait = lambda fn, **kwargs: fn()

    def freeze_mount_handler(self):
        root = next(r for r in self.plan["reconciliation"] if
                    (r["kind"], r["namespace"], r["name"]) == ("Kustomization", "flux-system", "flux-system"))
        row = next(r for r in self.plan["schedules"] if (r["namespace"], r["name"]) == c.CROSS_PRODUCER)
        self.coordinator.mutate(root, "suspend", True)
        self.coordinator.mutate(row, "suspend", True)
        return row

    def mount_handler_job(self):
        cron = self.api.get("cronjobs", *c.CROSS_PRODUCER)
        job = self.api.add("jobs", obj("Job", "kube-system", "media-mount-handler-29924965",
                                       copy.deepcopy(cron["spec"]["jobTemplate"]["spec"]),
                                       {"apiVersion": "batch/v1", "kind": "CronJob", "name": cron["metadata"]["name"],
                                        "uid": cron["metadata"]["uid"]}))
        job["status"] = {"active": 1}
        pod = self.api.add("pods", obj("Pod", "kube-system", "media-mount-handler-29924965-h6gvf",
                                      job["spec"]["template"]["spec"],
                                      {"apiVersion": "batch/v1", "kind": "Job", "name": job["metadata"]["name"],
                                       "uid": job["metadata"]["uid"]}))
        pod["spec"]["nodeName"] = c.PEER
        pod["status"] = {"phase": "Running", "conditions": [{"type": "Ready", "status": "True"}],
                         "containerStatuses": [{"name": "handler", "ready": True, "restartCount": 0,
                                                "state": {"running": {"startedAt": "2026-11-24T05:25:00Z"}}}]}
        return job, pod

    @staticmethod
    def finish_mount_handler_job(job, pod):
        job["status"] = {"active": 0, "succeeded": 1, "conditions": [{"type": "Complete", "status": "True"}]}
        pod["status"] = {"phase": "Succeeded", "containerStatuses": [
            {"name": "handler", "ready": False, "restartCount": 0,
             "state": {"terminated": {"exitCode": 0, "reason": "Completed",
                                       "finishedAt": "2026-11-24T05:26:00Z"}}}]}

    def test_hash_scope_code_clock_and_malformed_approval_fail_closed(self):
        c.check_plan(self.plan, self.approval, self.policy, self.api)
        for field, value in (("action", "rke2"), ("worker2_only", False), ("nodes", self.policy["nodes"]),
                             ("outage_budget", {"maximum_seconds": 86400}), ("source_bindings", {}),
                             ("created_epoch", 0), ("cluster_uid", "wrong-cluster")):
            changed = copy.deepcopy(self.plan)
            changed[field] = value
            with self.subTest(field=field), self.assertRaises(RuntimeError):
                c.check_plan(changed, c.sha(c.canonical(changed)), self.policy, self.api)
        with self.assertRaises(RuntimeError):
            c.check_plan(self.plan, "0" * 64, self.policy, self.api)
        changed = copy.deepcopy(self.plan)
        changed["controllers"] = []
        with self.assertRaises(RuntimeError):
            c.check_plan(changed, c.sha(c.canonical(changed)), self.policy, self.api)

    def test_guard_registration_is_required_before_any_pause(self):
        self.journal.state["guard_registered"] = False
        with self.assertRaisesRegex(RuntimeError, "BEFORE first pause"):
            self.coordinator.mutate(self.plan["schedules"][0], "suspend", True)
        self.assertEqual(self.api.events, [])
        self.assertEqual(self.journal.read()["mutations"], {})

    def standby_peer(self, authorized=False):
        peer = self.journal.read()
        contract = peer["worker_contract"]
        peer.update(mode="registered", phase="registered", guard_verified_epoch=time.time(),
                    guard_active=True, guard_enabled=True, executor_active=True, host_reboot_authorized=False,
                    controller_name=c.SERVER, controller_boot_id=contract["bindings"][c.SERVER]["boot_id"],
                    controller_kernel=contract["bindings"][c.SERVER]["kernel"],
                    live_source_bindings=contract["source_bindings"])
        if authorized:
            peer.update(mode="disrupting", phase="awaiting-host", host_started=True, host_reboot_authorized=True,
                        host_native_gate_epoch=time.time())
        return peer

    def worker_journal(self, phase="staged"):
        contract = self.journal.read()["worker_contract"]
        return MemoryJournal({"contract": contract, "contract_sha256": c.sha(c.canonical(contract)),
                              "phase": phase, "last_epoch": contract["started_epoch"], "pid": 0})

    def recover_with_live_worker(self, success=False, coordinator=None, api=None):
        coordinator, api = coordinator or self.coordinator, api or self.api
        coordinator.request_recovery("explicit-test-recovery")
        # Schedule the actual live standby code. A dead worker is deliberately
        # NOT scheduled: only the independent finalizer can acknowledge its exit.
        if api.worker_native["ActiveState"] == "active" and api.worker.read()["phase"] in ("staged", "waiting"):
            contract = api.worker.read()["contract"]
            peer = api.journal.read() | {
                "controller_name": c.SERVER, "controller_boot_id": contract["bindings"][c.SERVER]["boot_id"],
                "controller_kernel": contract["bindings"][c.SERVER]["kernel"],
                "live_source_bindings": contract["source_bindings"],
            }
            with patch.object(h, "worker_runtime", return_value=(Path("."), contract)), \
                    patch.object(h, "worker_transaction", side_effect=lambda _, fn: api.worker.transaction(fn)), \
                    patch.object(h, "cold_peer_status", return_value=peer), \
                    patch.object(h.Path, "read_text", return_value="old-worker2"), \
                    patch.object(h.platform, "release", return_value=contract["bindings"][c.WORKER]["kernel"]), \
                    patch.object(h, "main") as main:
                try:
                    h.cold_standby(contract["run_id"])
                except RuntimeError:
                    self.assertEqual(api.worker.read()["phase"], "cancelled")
                main.assert_not_called()
            api.worker_native.update(ActiveState="inactive", MainPID="0", ControlGroup="")
        return c.Coordinator.recover(coordinator, success)

    @contextmanager
    def finalizer_environment(self, groups=None, peer_changes=None, transaction=None):
        contract = self.api.worker.read()["contract"]
        worker_unit = "node-maintenance-cold-worker-" + contract["run_id"] + ".service"
        self.api.finalizer["service"].update(ActiveState="activating", MainPID=str(h.os.getpid()))
        groups = groups or {}
        def native(unit):
            if unit == worker_unit:
                return copy.deepcopy(self.api.worker_native)
            for suffix, section in (("-finalize.service", "service"), ("-finalize.timer", "timer")):
                if unit == worker_unit.removesuffix(".service") + suffix:
                    return copy.deepcopy(self.api.finalizer[section])
            if unit in ("reboot.target", "shutdown.target"):
                return {"LoadState": "loaded", "ActiveState": "inactive", "Job": "0"}
            self.assertIn(unit, {"node-maintenance-reboot-" + contract["host_step_id"] + suffix
                                 for suffix in (".timer", ".service")})
            if unit.endswith(".timer"):
                return {"LoadState": "not-found", "ActiveState": "inactive", "Job": "0"}
            return {"LoadState": "not-found", "ActiveState": "inactive", "MainPID": "0",
                    "ControlPID": "0", "Job": "0", "ControlGroup": ""}
        def peer(_):
            return self.journal.read() | {
                "controller_name": c.SERVER, "controller_boot_id": contract["bindings"][c.SERVER]["boot_id"],
                "controller_kernel": contract["bindings"][c.SERVER]["kernel"],
                "live_source_bindings": contract["source_bindings"],
            } | (peer_changes or {})
        def read(path, *args, **kwargs):
            if path == Path("/proc/sys/kernel/random/boot_id"):
                return self.api.host_boot
            for group, events in groups.items():
                if path == Path(group) / "cgroup.events":
                    return events
            raise AssertionError("Unexpected finalizer file read: " + str(path))
        def metadata(path):
            if str(path) in groups:
                return SimpleNamespace(st_uid=0, st_mode=stat.S_IFDIR | 0o755)
            raise FileNotFoundError(str(path))
        actions = MagicMock()
        def execute(args):
            if args[:2] == ["systemctl", "show"]:
                return "\n".join(k + "=" + str(v) for k, v in native(args[2]).items())
            self.assertEqual(args, ["systemctl", "stop", worker_unit.removesuffix(".service") + "-finalize.timer"])
            return actions(args)
        def unit_hash(name):
            if name == worker_unit:
                return self.api.worker_unit_sha256
            return self.api.finalizer["service_sha256" if name.endswith(".service") else "timer_sha256"]
        with ExitStack() as stack:
            for patcher in (
                patch.object(h, "worker_runtime", return_value=(Path("."), contract)),
                patch.object(h, "load_cold", return_value=c),
                patch.object(h, "worker_transaction",
                             side_effect=transaction or (lambda _, fn: self.api.worker.transaction(fn))),
                patch.object(h, "cold_peer_status", side_effect=peer),
                patch.object(h, "cold_public", side_effect=lambda _: self.api.worker.read()),
                patch.object(h, "worker_unit_digest", side_effect=unit_hash),
                patch.object(h.platform, "release", return_value=contract["bindings"][c.WORKER]["kernel"]),
                patch.object(h.Path, "lstat", autospec=True, side_effect=metadata),
                patch.object(h.Path, "read_text", autospec=True, side_effect=read),
            ):
                stack.enter_context(patcher)
            stack.enter_context(patch.object(h, "command", side_effect=execute))
            yield contract, actions

    def test_controller_heartbeat_between_request_and_response_uses_receive_time_for_wait_and_claim(self):
        worker = self.worker_journal()
        contract = worker.read()["contract"]
        clock = {"now": contract["started_epoch"] + 1}
        peer = self.standby_peer()
        exchanges = []
        def ssh(_):
            sent = clock["now"]
            clock["now"] += 0.1
            peer["last_epoch"] = peer["guard_verified_epoch"] = peer["host_native_gate_epoch"] = clock["now"]
            heartbeat = clock["now"]
            clock["now"] += 0.1
            exchanges.append((sent, heartbeat, clock["now"]))
            return copy.deepcopy(peer)
        with patch.object(h, "worker_transaction", side_effect=lambda _, fn: worker.transaction(fn)), \
                patch.object(h, "cold_peer_status", side_effect=ssh), patch.object(h.time, "time", side_effect=lambda: clock["now"]), \
                patch.object(h.Path, "read_text", return_value="old-worker2"), \
                patch.object(h.platform, "release", return_value=contract["bindings"][c.WORKER]["kernel"]), \
                patch.object(h, "audit", return_value=contract["bindings"][c.WORKER]):
            self.assertEqual(h.standby_tick(Path("."), contract), "waiting")
            peer.update(mode="disrupting", phase="awaiting-host", host_started=True, host_reboot_authorized=True)
            self.assertEqual(h.standby_tick(Path("."), contract), "claim")
        self.assertEqual(len(exchanges), 3)  # Waiting response, claim response, post-audit claim recheck.
        self.assertTrue(all(sent < heartbeat < received for sent, heartbeat, received in exchanges))
        self.assertEqual(worker.read()["last_epoch"], exchanges[-1][2])
        self.assertEqual(worker.read()["peer_epoch"], exchanges[-1][2])

    def test_ssh_response_clock_rollback_or_reserve_crossing_never_claims(self):
        for boundary in ("rollback", "reserve"):
            worker = self.worker_journal("waiting")
            contract = worker.read()["contract"]
            clock = {"now": contract["started_epoch"] + 1 if boundary == "rollback" else contract["deadline_epoch"] - 7200 - 0.1}
            peer = self.standby_peer(authorized=True)
            def ssh(_):
                requested = clock["now"]
                clock["now"] += -0.2 if boundary == "rollback" else 0.2
                peer["last_epoch"] = peer["host_native_gate_epoch"] = requested + 0.05
                return copy.deepcopy(peer)
            with self.subTest(boundary=boundary), \
                    patch.object(h, "worker_transaction", side_effect=lambda _, fn: worker.transaction(fn)), \
                    patch.object(h, "cold_peer_status", side_effect=ssh), patch.object(h.time, "time", side_effect=lambda: clock["now"]), \
                    patch.object(h.Path, "read_text", return_value="old-worker2"), \
                    patch.object(h.platform, "release", return_value=contract["bindings"][c.WORKER]["kernel"]), \
                    patch.object(h, "audit") as audit, self.assertRaisesRegex(RuntimeError, "rollback|reserve"):
                h.standby_tick(Path("."), contract)
            audit.assert_not_called()
            self.assertEqual(worker.read()["phase"], "waiting")

    def test_post_audit_claim_response_rechecks_clock_and_reserve_before_durable_claim(self):
        for boundary in ("rollback", "reserve"):
            worker = self.worker_journal("waiting")
            contract = worker.read()["contract"]
            clock = {"now": contract["started_epoch"] + 1, "calls": 0}
            peer = self.standby_peer(authorized=True)
            def ssh(_):
                requested = clock["now"]
                clock["calls"] += 1
                peer["last_epoch"] = peer["host_native_gate_epoch"] = requested + 0.05
                clock["now"] += -0.2 if clock["calls"] == 2 and boundary == "rollback" else 0.2
                return copy.deepcopy(peer)
            def audit():
                if boundary == "reserve":
                    clock["now"] = contract["deadline_epoch"] - 7200 - 0.1
                return contract["bindings"][c.WORKER]
            with self.subTest(boundary=boundary), \
                    patch.object(h, "worker_transaction", side_effect=lambda _, fn: worker.transaction(fn)), \
                    patch.object(h, "cold_peer_status", side_effect=ssh), patch.object(h.time, "time", side_effect=lambda: clock["now"]), \
                    patch.object(h.Path, "read_text", return_value="old-worker2"), \
                    patch.object(h.platform, "release", return_value=contract["bindings"][c.WORKER]["kernel"]), \
                    patch.object(h, "audit", side_effect=audit), self.assertRaisesRegex(RuntimeError, "rollback|reserve"):
                h.standby_tick(Path("."), contract)
            self.assertEqual(clock["calls"], 2)
            self.assertEqual(worker.read()["phase"], "waiting")

    def test_dead_standby_native_absent_timer_shape_needs_real_finalizer_ack_before_guard_can_restore(self):
        for row in self.plan["reconciliation"] + self.plan["schedules"]:
            self.coordinator.mutate(row, "suspend", True)
        for row in self.plan["autoscalers"]:
            self.coordinator.mutate(row, "pause", True)
        for row in sorted([r for r in self.plan["controllers"] if (r["namespace"], r["name"]) in c.OPERATORS],
                          key=lambda r: (r["name"] != "keda-operator", c.key(r))):
            self.coordinator.mutate(row, "replicas", 0)
        app = next(r for r in self.plan["controllers"] if (r["namespace"], r["name"]) == ("homebox", "homebox"))
        self.coordinator.mutate(app, "replicas", 0)
        self.api.worker_native.update(ActiveState="failed", MainPID="0", ControlGroup="")
        deadline = self.journal.read()["deadline_epoch"]
        with patch.object(c, "load_runtime", return_value=self.coordinator), patch.object(c.m, "run", return_value="failed"), \
                patch.object(c.m, "health", return_value={}), patch.object(c.m.urllib.request, "urlopen") as url, \
                patch.object(c.time, "sleep"):
            url.return_value.__enter__.return_value.status = 200
            with self.assertRaisesRegex(RuntimeError, "not acknowledged"):
                c.guard("bound-controller-run")
            self.assertTrue(self.journal.read()["host_slot_cancelled"])
            self.assertEqual(self.api.worker_status(self.policy, "1" * 32)["record"]["phase"], "waiting")
            self.assertEqual(self.api.get("deployments", "homebox", "homebox")["spec"]["replicas"], 0)
            self.assertTrue(self.api.items("configmap"))
            with self.finalizer_environment() as (contract, command):
                h.cold_worker_finalize(contract["run_id"])
                command.assert_called_once_with(["systemctl", "stop",
                                                "node-maintenance-cold-worker-" + contract["run_id"] + "-finalize.timer"])
            self.assertEqual(self.api.worker.read()["phase"], "cancelled")
            self.assertEqual(self.api.worker.read()["finalizer_ack"]["contract_sha256"], c.sha(c.canonical(contract)))
            c.guard("bound-controller-run")
        self.assertEqual(self.journal.read()["deadline_epoch"], deadline)
        self.assertEqual(self.journal.read()["phase"], "recovered-after-failure")
        self.assertEqual(self.api.get("deployments", "homebox", "homebox")["spec"]["replicas"], 1)
        self.coordinator.original_state()

    def test_finalizer_loaded_inactive_timer_has_no_service_pid_or_cgroup_properties(self):
        self.coordinator.request_recovery("test-revocation")
        self.api.worker_native.update(ActiveState="failed", MainPID="0", ControlGroup="")
        with self.finalizer_environment() as (contract, command):
            native = h.worker_unit_status
            timer = "node-maintenance-reboot-" + contract["host_step_id"] + ".timer"
            with patch.object(h, "worker_unit_status", side_effect=lambda u: (
                    {"LoadState": "loaded", "ActiveState": "inactive", "Job": "0"}
                    if u == timer else native(u))):
                h.cold_worker_finalize(contract["run_id"])
            self.assertEqual(self.api.worker.read()["phase"], "cancelled")
            self.assertIn("finalizer_ack", self.api.worker.read())
            command.assert_called_once()

    def test_finalizer_refuses_unknown_queued_active_child_or_drifted_unit_without_ack(self):
        self.coordinator.request_recovery("test-revocation")
        original = self.api.worker_native.copy()
        contract = self.api.worker.read()["contract"]
        group = str(Path("/sys/fs/cgroup") / ("system.slice/node-maintenance-cold-worker-" + contract["run_id"] + ".service"))
        changes = [
            {"LoadState": "not-found"}, {"ActiveState": "active"}, {"Job": "37"},
            {"MainPID": "1234"}, {"ControlPID": "1235"}, {"User": "rishik"},
            {"FragmentPath": "/unbound/unit.service"}, {"ControlGroup": "/unbound/cgroup"},
            {"child": True}, {"unit_source": True},
        ]
        for change in changes:
            self.api.worker_native = original | {"ActiveState": "failed", "MainPID": "0", "ControlGroup": ""} | change
            with self.subTest(change=change), \
                    self.finalizer_environment(groups={group: "populated 1\nfrozen 0\n"} if change.get("child") else {}) as (contract, command):
                if change.get("unit_source"):
                    with patch.object(h, "worker_unit_digest", return_value="0" * 64), self.assertRaises(RuntimeError):
                        h.cold_worker_finalize(contract["run_id"])
                else:
                    with self.assertRaises(RuntimeError):
                        h.cold_worker_finalize(contract["run_id"])
                command.assert_not_called()
            self.assertEqual(self.api.worker.read()["phase"], "waiting")
            self.assertNotIn("finalizer_ack", self.api.worker.read())
        self.assertTrue(self.api.items("configmap"))

    def test_finalizer_requires_exact_revocation_and_can_ack_after_deadline_without_renewal(self):
        self.api.worker_native.update(ActiveState="failed", MainPID="0", ControlGroup="")
        # A dead job is not itself controller revocation.
        with self.finalizer_environment() as (contract, command):
            h.cold_worker_finalize(contract["run_id"])
            command.assert_not_called()
        self.assertEqual(self.api.worker.read()["phase"], "waiting")
        self.coordinator.request_recovery("test-revocation")
        for change in ({"deadline_epoch": contract["deadline_epoch"] + 1}, {"host_step_id": "3" * 32},
                       {"live_source_bindings": {}}, {"controller_boot_id": "new-boot"}):
            with self.subTest(change=change), self.finalizer_environment(peer_changes=change), self.assertRaises(RuntimeError):
                h.cold_worker_finalize(contract["run_id"])
            self.assertEqual(self.api.worker.read()["phase"], "waiting")
        with self.finalizer_environment(), patch.object(h, "worker_runtime", side_effect=RuntimeError("root source drift")), \
                self.assertRaisesRegex(RuntimeError, "source drift"):
            h.cold_worker_finalize(contract["run_id"])
        deadline = contract["deadline_epoch"]
        with self.finalizer_environment(), patch.object(h.time, "time", return_value=deadline + 1):
            h.cold_worker_finalize(contract["run_id"])
        self.assertEqual(self.api.worker.read()["contract"]["deadline_epoch"], deadline)
        self.assertEqual(self.api.worker.read()["phase"], "cancelled")

    def test_dead_claimed_job_with_package_children_or_deferred_reboot_never_gets_ack(self):
        self.authorize_host_slot()
        self.coordinator.request_recovery("test-revocation")
        self.api.worker.transaction(lambda s: s.update(phase="claimed"))
        self.api.worker_native.update(ActiveState="failed", MainPID="0", ControlGroup="")
        contract = self.api.worker.read()["contract"]
        group = str(Path("/sys/fs/cgroup") / ("system.slice/node-maintenance-cold-worker-" + contract["run_id"] + ".service"))
        with self.finalizer_environment(groups={group: "populated 1\nfrozen 0\n"}) as (_, command), \
                self.assertRaisesRegex(RuntimeError, "descendants still active"):
            h.cold_worker_finalize(contract["run_id"])
        command.assert_not_called()
        self.assertEqual(self.api.worker.read()["phase"], "claimed")
        with self.finalizer_environment() as (_, command):
            native = h.worker_unit_status
            timer = "node-maintenance-reboot-" + contract["host_step_id"] + ".timer"
            with patch.object(h, "worker_unit_status", side_effect=lambda u: (
                    {"LoadState": "loaded", "ActiveState": "active", "Job": "42"}
                    if u == timer else native(u))), \
                    self.assertRaisesRegex(RuntimeError, "reboot unit"):
                h.cold_worker_finalize(contract["run_id"])
            command.assert_not_called()
        self.assertEqual(self.api.worker.read()["phase"], "claimed")
        with self.finalizer_environment(groups={group: "populated 0\nfrozen 0\n"}):
            h.cold_worker_finalize(contract["run_id"])
        self.assertEqual(self.api.worker.read()["phase"], "failed")
        self.assertNotIn("receipt", self.api.worker.read())
        self.assertFalse(self.journal.read()["host_complete"])

    def test_finalizer_ack_and_late_claim_are_serialized_by_same_worker_mutex(self):
        self.coordinator.request_recovery("test-revocation")
        self.api.worker_native.update(ActiveState="failed", MainPID="0", ControlGroup="")
        mutex, inspecting, release, attempting = threading.Lock(), threading.Event(), threading.Event(), threading.Event()
        errors, outcomes = [], []
        def transaction(_, callback):
            with mutex:
                return self.api.worker.transaction(callback)
        original = h.worker_quiescent
        def inspect(contract, cold):
            inspecting.set()
            if not release.wait(5):
                raise RuntimeError("Test did not release finalizer")
            return original(contract, cold)
        def finalize(run):
            try:
                h.cold_worker_finalize(run)
            except Exception as error:
                errors.append(error)
        def late_claim(contract):
            attempting.set()
            try:
                outcomes.append(h.standby_tick(Path("."), contract))
            except RuntimeError as error:
                outcomes.append(str(error))
        with self.finalizer_environment(transaction=transaction) as (contract, _), \
                patch.object(h, "worker_quiescent", side_effect=inspect):
            finalizer = threading.Thread(target=finalize, args=(contract["run_id"],))
            claimant = threading.Thread(target=late_claim, args=(contract,))
            finalizer.start()
            try:
                self.assertTrue(inspecting.wait(5))
                claimant.start()
                self.assertTrue(attempting.wait(5))
            finally:
                release.set()
                finalizer.join(5)
                if claimant.ident is not None:
                    claimant.join(5)
        self.assertFalse(finalizer.is_alive())
        self.assertFalse(claimant.is_alive())
        self.assertEqual(errors, [])
        self.assertEqual(len(outcomes), 1)
        self.assertIn("single-shot/terminal", outcomes[0])
        self.assertEqual(self.api.worker.read()["phase"], "cancelled")

    def test_finalizer_is_registered_and_actually_executed_before_standby_start(self):
        contract = self.api.worker.read()["contract"]
        stream = MagicMock()
        stream.__enter__.return_value = stream
        with patch.object(h.Path, "open", return_value=stream), patch.object(h.os, "fchmod", create=True), \
                patch.object(h.os, "fsync"), patch.object(h, "worker_status", side_effect=lambda _: self.api.worker_status(self.policy, contract["run_id"])), \
                patch.object(h, "command") as command:
            h.start_worker_standby(Path("."), contract, c)
        base = "node-maintenance-cold-worker-" + contract["run_id"]
        self.assertEqual([call.args[0] for call in command.call_args_list], [
            ["systemctl", "daemon-reload"], ["systemctl", "start", base + "-finalize.timer"],
            ["systemctl", "start", base + "-finalize.service"], ["systemctl", "start", "--no-block", base + ".service"],
        ])
        for section, field, value in (("timer", "LoadState", "not-found"), ("timer", "ActiveState", "inactive"),
                                      ("timer", "Unit", "unbound.service"), ("service", "User", "rishik")):
            proof = self.api.worker_status(self.policy, contract["run_id"])
            proof["finalizer"][section][field] = value
            with self.subTest(field=field), self.assertRaisesRegex(RuntimeError, "finalizer required"):
                c.worker_ready(proof, contract, time.time())
        proof = self.api.worker_status(self.policy, contract["run_id"])
        proof["record"]["finalizer_verified_epoch"] = 0
        with patch.object(h.Path, "open", return_value=stream), patch.object(h.os, "fchmod", create=True), \
                patch.object(h.os, "fsync"), patch.object(h, "worker_status", return_value=proof), \
                patch.object(h, "command") as command, self.assertRaisesRegex(RuntimeError, "finalizer required"):
            h.start_worker_standby(Path("."), contract, c)
        self.assertNotIn(["systemctl", "start", "--no-block", base + ".service"],
                         [call.args[0] for call in command.call_args_list])
        with patch.object(h, "cold_worker_finalize") as finalize:
            h.main({"operation": "cold-worker-finalize", "run_id": contract["run_id"]})
            finalize.assert_called_once_with(contract["run_id"])
            with self.assertRaisesRegex(RuntimeError, "no command/payload"):
                h.main({"operation": "cold-worker-finalize", "run_id": contract["run_id"], "command": "unbound"})

    def test_apply_frontloads_all_privileged_auth_and_only_observes_after_first_pause(self):
        start = self.journal.read()["started_epoch"]
        calls, paused = [], False
        statuses = iter([{"phase": "awaiting-host", "mode": "disrupting", "completed": False},
                         {"phase": "accepted", "mode": "recovering", "completed": True}])
        def remote(node, payload, privileged=False):
            nonlocal paused
            calls.append((payload["operation"], privileged, paused))
            if privileged:
                self.assertFalse(paused, "A privileged call occurred after first pause")
            if payload["operation"] == "recovery-audit":
                return {"off_node_directory_verified": True}
            if payload["operation"] == "cold-worker-status":
                return self.api.worker_status(self.policy, payload["run_id"])
            if payload["operation"] == "cold-start":
                paused = True
            if payload["operation"] == "cold-status":
                return {**self.journal.read(), **next(statuses)}
        with patch.object(c.sys.stdin, "isatty", return_value=True), patch.object(c.sys.stdout, "isatty", return_value=True), \
                patch.object(c, "API", return_value=self.api), \
                patch.object(c, "build_plan", return_value=self.plan), patch.object(c.m, "remote", side_effect=remote), \
                patch.object(c.m, "kube"), patch.object(c.m, "run", side_effect=AssertionError("No CLI SSH after setup")), \
                patch.object(c, "stage_transport", return_value="/bound/source.json"), \
                patch.object(c.Path, "mkdir"), patch.object(c.Path, "write_bytes"), \
                patch.object(c.uuid, "uuid4", side_effect=[SimpleNamespace(hex="1" * 32), SimpleNamespace(hex="2" * 32)]), \
                patch.object(c.time, "time", return_value=start), patch.object(c.time, "sleep"):
            c.apply(self.plan, self.approval, self.policy, "/existing/protected/mount", True)
        self.assertEqual([op for op, privileged, _ in calls if privileged],
                         ["cold-register", "cold-worker-register", "cold-start"])
        self.assertTrue(all(not privileged for _, privileged, after in calls if after))

    def test_cli_loss_after_setup_never_requests_late_privileged_recovery(self):
        start = self.journal.read()["started_epoch"]
        def remote(node, payload, privileged=False):
            if payload["operation"] == "recovery-audit":
                return {"off_node_directory_verified": True}
            if payload["operation"] == "cold-worker-status":
                return self.api.worker_status(self.policy, payload["run_id"])
            if payload["operation"] == "cold-status":
                raise RuntimeError("workstation disappeared")
        with patch.object(c.sys.stdin, "isatty", return_value=True), patch.object(c.sys.stdout, "isatty", return_value=True), \
                patch.object(c, "API", return_value=self.api), \
                patch.object(c, "build_plan", return_value=self.plan), patch.object(c.m, "remote", side_effect=remote), \
                patch.object(c.m, "kube"), patch.object(c, "stage_transport", return_value="/bound/source.json"), \
                patch.object(c.Path, "mkdir"), patch.object(c.Path, "write_bytes"), patch.object(c, "request_recovery") as recover, \
                patch.object(c.uuid, "uuid4", side_effect=[SimpleNamespace(hex="1" * 32), SimpleNamespace(hex="2" * 32)]), \
                patch.object(c.time, "time", return_value=start), self.assertRaisesRegex(RuntimeError, "workstation disappeared"):
            c.apply(self.plan, self.approval, self.policy, "/existing/protected/mount", True)
        recover.assert_not_called()

    def test_live_standby_proof_rejects_inactive_absent_stale_pid_unit_original_or_bound_identity(self):
        contract = self.journal.read()["worker_contract"]
        proof = self.api.worker_status(self.policy, contract["run_id"])
        c.worker_ready(proof, contract, time.time())
        changes = [
            ("unit", "ActiveState", "inactive"), ("unit", "LoadState", "not-found"),
            ("unit", "MainPID", "0"), ("unit", "Job", "123"), ("unit", "User", "rishik"),
            ("record", "phase", "claimed"), ("record", "ready_epoch", 0), ("record", "peer_epoch", time.time() + 60),
            ("record", "original_host", {}), ("record", "contract_sha256", "0" * 64),
        ]
        for area, field, value in changes:
            changed = copy.deepcopy(proof)
            changed[area][field] = value
            with self.subTest(field=field), self.assertRaises(RuntimeError):
                c.worker_ready(changed, contract, time.time())
        for field in ("run_id", "host_step_id", "plan_sha256", "source_bindings", "bindings", "deadline_epoch"):
            changed = copy.deepcopy(proof)
            changed["record"]["contract"][field] = "replayed"
            with self.subTest(binding=field), self.assertRaises(RuntimeError):
                c.worker_ready(changed, contract, time.time())
        with self.assertRaises(RuntimeError):
            c.worker_ready({**proof, "unit_sha256": "0" * 64}, contract, time.time())

    def test_missing_exact_worker_or_native_guard_proof_cannot_start_or_pause(self):
        self.journal.state.update(mode="registered", guard_verified_epoch=time.time())
        for failure in ("worker", "guard"):
            self.journal.state.update(mode="registered", host_slot_cancelled=False)
            with self.subTest(failure=failure), patch.object(h, "load_cold", return_value=c), \
                    patch.object(h, "load_peer", return_value=MagicMock()), \
                    patch.object(c, "load_runtime", return_value=self.coordinator), patch.object(h, "command") as command:
                command.side_effect = lambda args: "active" if "is-active" in args else "enabled"
                if failure == "worker":
                    with patch.object(self.api, "worker_status", side_effect=RuntimeError("No trusted existing SSH channel")), \
                            self.assertRaisesRegex(RuntimeError, "trusted"):
                        h.cold_control({"operation": "cold-start", "run_id": "1" * 32})
                else:
                    self.journal.state["guard_verified_epoch"] = 0
                    with self.assertRaisesRegex(RuntimeError, "guard readiness"):
                        h.cold_control({"operation": "cold-start", "run_id": "1" * 32})
                starts = [call.args[0] for call in command.call_args_list if call.args[0][1] == "start"]
                self.assertEqual(starts, [["systemctl", "start", "--no-block",
                                          "node-maintenance-cold-" + "1" * 32 + "-guard.service"]])
        self.assertEqual(self.api.events, [])
        self.assertEqual(self.journal.read()["mode"], "recovering")
        self.assertTrue(self.journal.read()["host_slot_cancelled"])

    def test_standby_is_inert_until_exact_authorized_slot_and_cannot_replay_terminal_job(self):
        worker = self.worker_journal()
        contract = worker.read()["contract"]
        peer = self.standby_peer()
        original = {k: contract["bindings"][c.WORKER][k] for k in ("boot_id", "kernel", "pending_packages")}
        with patch.object(h, "worker_transaction", side_effect=lambda _, fn: worker.transaction(fn)), \
                patch.object(h, "cold_peer_status", side_effect=lambda _: peer), patch.object(h.Path, "read_text", return_value="old-worker2"), \
                patch.object(h.platform, "release", return_value=original["kernel"]), patch.object(h, "audit", return_value=original), \
                patch.object(h, "command") as command:
            self.assertEqual(h.standby_tick(Path("."), contract), "waiting")
            self.assertEqual(worker.read()["original_host"], original)
            command.assert_not_called()
            peer.update(mode="disrupting", phase="cold-detached")
            self.assertEqual(h.standby_tick(Path("."), contract), "waiting")
            peer.update(phase="awaiting-host", host_started=True, host_reboot_authorized=True, host_native_gate_epoch=time.time())
            self.assertEqual(h.standby_tick(Path("."), contract), "claim")
            with self.assertRaisesRegex(RuntimeError, "single-shot"):
                h.standby_tick(Path("."), contract)
            command.assert_not_called()  # Claim journals authority; no caller-selected command.

    def test_standby_cancel_reserve_clock_source_boot_peer_or_executor_loss_never_launches(self):
        changes = [
            {"host_slot_cancelled": True}, {"mode": "recovering"}, {"completed": True},
            {"deadline_epoch": 0}, {"last_epoch": 0}, {"last_epoch": time.time() + 60},
            {"executor_active": False}, {"guard_active": False}, {"guard_enabled": False},
            {"controller_boot_id": "new"}, {"controller_kernel": "new"}, {"live_source_bindings": {}},
            {"host_native_gate_epoch": 0},
            {"host_step_id": "3" * 32}, {"plan_sha256": "0" * 64}, {"host_reboot_authorized": False},
        ]
        for change in changes + [{"peer_failure": True}, {"worker_boot": True}, {"local_clock": True}, {"reserve": True}]:
            worker = self.worker_journal("waiting")
            contract = worker.read()["contract"]
            peer = {**self.standby_peer(authorized=True), **change}
            now = contract["deadline_epoch"] - 7200 if change.get("reserve") else time.time()
            if change.get("local_clock"):
                worker.state["last_epoch"] = now + 60
            with self.subTest(change=change), \
                    patch.object(h, "worker_runtime", return_value=(Path("."), contract)), \
                    patch.object(h, "worker_transaction", side_effect=lambda _, fn: worker.transaction(fn)), \
                    patch.object(h, "cold_peer_status", side_effect=RuntimeError("strict SSH host trust missing") if change.get("peer_failure") else lambda _: peer), \
                    patch.object(h.Path, "read_text", return_value="new" if change.get("worker_boot") else "old-worker2"), \
                    patch.object(h.platform, "release", return_value=contract["bindings"][c.WORKER]["kernel"]), \
                    patch.object(h.time, "time", return_value=now), patch.object(h, "main") as launch:
                try:
                    h.cold_standby(contract["run_id"])
                except RuntimeError:
                    pass
                launch.assert_not_called()
                self.assertEqual(worker.read()["phase"], "cancelled")

    def test_revocation_between_worker_reads_cannot_claim_and_recovery_waits_for_ack_not_unit_absence(self):
        worker = self.worker_journal("waiting")
        contract = worker.read()["contract"]
        first = self.standby_peer(authorized=True)
        second = {**first, "host_slot_cancelled": True}
        with patch.object(h, "worker_runtime", return_value=(Path("."), contract)), \
                patch.object(h, "worker_transaction", side_effect=lambda _, fn: worker.transaction(fn)), \
                patch.object(h, "cold_peer_status", side_effect=[first, second]), \
                patch.object(h.Path, "read_text", return_value="old-worker2"), \
                patch.object(h.platform, "release", return_value=contract["bindings"][c.WORKER]["kernel"]), \
                patch.object(h, "audit", return_value=contract["bindings"][c.WORKER]), patch.object(h, "main") as launch:
            with self.assertRaisesRegex(RuntimeError, "revoked during claim"):
                h.cold_standby(contract["run_id"])
        launch.assert_not_called()
        self.assertEqual(worker.read()["phase"], "cancelled")
        proof = self.api.worker_status(self.policy, contract["run_id"])
        proof["record"]["phase"] = "waiting"
        proof["unit"].update(ActiveState="inactive", LoadState="not-found", MainPID="0")
        with patch.object(self.api, "worker_status", return_value=proof), \
                patch.object(self.api, "host_idle", return_value=False), self.assertRaisesRegex(RuntimeError, "acknowledged"):
            self.coordinator.recover()
        self.assertTrue(self.api.items("configmap"))
        self.assertFalse(any(e[0] == "patch" for e in self.api.events))

    def test_claim_before_revocation_cannot_warm_until_root_job_and_package_children_are_idle(self):
        contract = self.journal.read()["worker_contract"]
        self.authorize_host_slot()
        proof = self.api.worker_status(self.policy, contract["run_id"])
        proof["record"]["phase"] = "claimed"
        with patch.object(self.api, "worker_status", return_value=proof), self.assertRaisesRegex(RuntimeError, "acknowledged"):
            self.coordinator.recover()
        self.assertTrue(self.journal.read()["host_slot_cancelled"])
        payload = {**contract["os_payload"], "contract_sha256": c.sha(c.canonical(contract))}
        with patch.object(h, "cold_peer_status", return_value=self.standby_peer(authorized=True) | {"host_slot_cancelled": True}), \
                patch.object(h, "command") as command, self.assertRaisesRegex(RuntimeError, "revoked"):
            h.reserve(payload)
        command.assert_not_called()
        proof["record"]["phase"] = "cancelled"
        with patch.object(self.api, "worker_status", return_value=proof), \
                patch.object(self.api, "host_idle", side_effect=RuntimeError("package cgroup populated")), \
                self.assertRaisesRegex(RuntimeError, "populated"):
            self.coordinator.recover()
        self.assertFalse(any(e[0] == "patch" for e in self.api.events))

    def test_bound_receipt_replay_identity_and_clock_drift_are_rejected_before_new_boot(self):
        self.record_package_receipt()
        receipt = self.journal.read()["host_receipt"]
        self.coordinator.ingest_worker_receipt(receipt)
        for field, value in (("run_id", "3" * 32), ("old_boot_id", "new"), ("expected_kernel", "new"),
                             ("contract_sha256", "0" * 64), ("completed_epoch", time.time() + 60),
                             ("operation", "rke2"), ("name", c.PEER), ("headless", False), ("package_removals", True)):
            with self.subTest(field=field), self.assertRaises(RuntimeError):
                self.coordinator.ingest_worker_receipt({**receipt, field: value})
        with self.assertRaisesRegex(RuntimeError, "not reboot completion"):
            self.coordinator.host_completed()
        self.assertFalse(self.journal.read()["host_complete"])

    def test_setup_stage_failure_uses_only_before_pause_guarded_cancellation_and_keeps_lock_clock(self):
        start = self.journal.read()["started_epoch"]
        calls = []
        def remote(node, payload, privileged=False):
            calls.append(payload["operation"])
            if payload["operation"] == "recovery-audit":
                return {"off_node_directory_verified": True}
            if payload["operation"] == "cold-status":
                return self.journal.read()
            if payload["operation"] == "audit":
                return {"boot_id": "old-other"}
        with patch.object(c.sys.stdin, "isatty", return_value=True), patch.object(c.sys.stdout, "isatty", return_value=True), \
                patch.object(c, "API", return_value=self.api), \
                patch.object(c, "build_plan", return_value=self.plan), patch.object(c.m, "remote", side_effect=remote), \
                patch.object(c.m, "kube") as kube, patch.object(c, "stage_transport", side_effect=["/bound/source.json", RuntimeError("SCP failed")]), \
                patch.object(c.Path, "mkdir"), patch.object(c.Path, "write_bytes"), \
                patch.object(c.uuid, "uuid4", side_effect=[SimpleNamespace(hex="1" * 32), SimpleNamespace(hex="2" * 32)]), \
                patch.object(c.time, "time", return_value=start), self.assertRaisesRegex(RuntimeError, "before any pause"):
            c.apply(self.plan, self.approval, self.policy, "/existing/protected/mount", True)
        self.assertIn("cold-recover", calls)
        self.assertNotIn("cold-start", calls)
        self.assertFalse(any("delete" in args.args for args in kube.call_args_list))
        self.assertEqual(self.journal.read()["deadline_epoch"], start + 43200)

    def test_standby_stage_failure_never_invents_ack_from_idle_or_unknown_unit(self):
        for queued in (False, True):
            worker = self.worker_journal("waiting")
            contract = worker.read()["contract"]
            proof = self.api.worker_status(self.policy, contract["run_id"])
            proof["unit"].update(ActiveState="active" if queued else "inactive", Job="42" if queued else "0")
            stream = MagicMock()
            stream.__enter__.return_value = stream
            with self.subTest(queued=queued), patch.object(h.Path, "open", return_value=stream), \
                    patch.object(h.os, "fchmod", create=True), patch.object(h.os, "fsync"), \
                    patch.object(h, "worker_transaction", side_effect=lambda _, fn: worker.transaction(fn)), \
                    patch.object(h, "worker_status", return_value=proof), \
                    patch.object(h, "command", side_effect=RuntimeError("start failed")) as command, \
                    self.assertRaisesRegex(RuntimeError, "start failed"):
                h.start_worker_standby(Path("."), contract, c)
            self.assertEqual(worker.read()["phase"], "waiting")
            self.assertEqual(worker.read()["contract"]["deadline_epoch"], contract["deadline_epoch"])
            self.assertEqual(command.call_args.args[0], ["systemctl", "daemon-reload"])

    def test_standby_entry_rejects_any_caller_chosen_payload_or_command(self):
        with patch.object(h, "cold_standby") as standby:
            h.main({"operation": "cold-standby", "run_id": "1" * 32})
            standby.assert_called_once_with("1" * 32)
            for field in ("command", "os_payload", "deadline_epoch", "host_step_id"):
                with self.subTest(field=field), self.assertRaisesRegex(RuntimeError, "no command/payload"):
                    h.main({"operation": "cold-standby", "run_id": "1" * 32, field: "caller-chosen"})

    def test_long_normal_drain_is_inert_but_stale_authorized_slot_cannot_launch(self):
        contract = self.journal.read()["worker_contract"]
        now = contract["started_epoch"] + 600
        peer = self.standby_peer()
        peer.update(mode="disrupting", phase="draining", last_epoch=contract["started_epoch"])
        self.assertEqual(h.standby_peer(contract, peer, now), "waiting")
        peer.update(phase="awaiting-host", host_started=True, host_reboot_authorized=True, host_native_gate_epoch=now)
        with self.assertRaisesRegex(RuntimeError, "stale"):
            h.standby_peer(contract, peer, now)
        peer["last_epoch"] = now
        self.assertEqual(h.standby_peer(contract, peer, now), "claim")

    def test_peer_status_uses_only_bound_finite_protocol_not_remote_python(self):
        peer = self.standby_peer()
        protocol = MagicMock()
        protocol.request.return_value = peer
        with patch.object(h, "load_peer", return_value=protocol), patch.object(h, "command") as command:
            self.assertEqual(h.cold_peer_status(peer["run_id"]), peer)
        protocol.request.assert_called_once_with("controller-status", peer["run_id"])
        command.assert_not_called()

    def test_native_standby_idle_checks_waiting_unit_and_surviving_package_cgroup(self):
        state = [{"ActiveState": "inactive", "LoadState": "not-found", "Job": "0"} for _ in range(3)]
        state += [{"ActiveState": "inactive", "LoadState": "loaded", "Job": "0"} for _ in range(3)]
        state[5]["ActiveState"] = "active"
        protocol = MagicMock()
        protocol.request.return_value = {"states": state, "cgroups_empty": True}
        with patch.object(c, "peer", return_value=protocol), self.assertRaisesRegex(RuntimeError, "still claim"):
            c.API().host_idle(self.policy, "2" * 32, "1" * 32)
        state[5]["ActiveState"] = "failed"
        protocol.request.side_effect = RuntimeError("package cgroup populated")
        with patch.object(c, "peer", return_value=protocol), \
                self.assertRaisesRegex(RuntimeError, "populated"):
            c.API().host_idle(self.policy, "2" * 32, "1" * 32)
        protocol.request.side_effect = None
        with patch.object(c, "peer", return_value=protocol):
            self.assertFalse(c.API().host_idle(self.policy, "2" * 32, "1" * 32))
        protocol.request.assert_called_with("worker-idle", "1" * 32, "2" * 32)

    def peer_config(self, node=c.SERVER):
        value = copy.deepcopy(self.plan["peer_scope"][node])
        value.pop("probe")
        value.pop("incoming_key_fingerprint")
        blob = b"\x00\x00\x00\x0bssh-ed25519\x00\x00\x00\x20" + bytes([17]) * 32
        value["peer_host_key"] = "ssh-ed25519 " + base64.b64encode(blob).decode()
        value["peer_host_fingerprint"] = peer_source.public_key(value["peer_host_key"])
        value["operator_ip"] = "192.168.1.50"
        return value

    @contextmanager
    def peer_policy_environment(self):
        import tempfile
        value = self.peer_config()
        with tempfile.TemporaryDirectory(dir=Path.cwd(), prefix=".peer-policy-test-") as root, ExitStack() as stack:
            root = Path(root)
            public, parts, home, proc = (root / name for name in ("public", "parts", "home", "proc"))
            directory = public / value["setup_id"]
            for path in (directory, parts, home / ".ssh", proc / "1234"):
                path.mkdir(parents=True)
            config = root / "sshd_config"
            config.write_text("Include " + shlex.quote(str(parts / "*.conf")) + "\n# original global settings\n")
            baseline = parts / "50-original.conf"
            baseline.write_bytes(b"# original SSH settings\nPasswordAuthentication no\n")
            original = home / ".ssh" / "authorized_keys"
            original.write_bytes(b"original-public-entry\n")
            second = home / ".ssh" / "authorized_keys2"
            second.write_bytes(b"second-original-public-entry\n")
            user_config = home / ".ssh" / "config"
            user_config.write_bytes(b"unrelated user SSH settings\n")
            (proc / "1234" / "cmdline").write_bytes(b"sshd: /usr/sbin/sshd -D [listener] 0 of 10-100 startups\0")
            incoming = home / ".local/state/node-maintenance-peer" / value["setup_id"]
            incoming.mkdir(parents=True)
            (incoming / "peer-public-key.txt").write_text(value["peer_host_key"])
            state = SimpleNamespace(value=value, directory=directory, original=original, second=second,
                                    config=config, baseline=baseline, parts=parts, commands=[], syntax_fail=False,
                                    reload_fail=False, pid="1234", other_drift=False, wrong_context=False,
                                    original_paths=[".ssh/authorized_keys", ".ssh/authorized_keys2"],
                                    forcecommand="none", proc=proc)
            for name, path in (("PUBLIC", public), ("SSHD_PARTS", parts), ("SSHD_CONFIG", config),
                               ("SSHD_DEFAULTS", root / "ssh-defaults"), ("SSHD_PROC", proc), ("HOME", home)):
                stack.enter_context(patch.object(peer_source, name, path))
            state.keyfile, state.dropin = peer_source.ssh_files(value)
            def execute(args):
                state.commands.append(args)
                if args[:3] == ["/usr/bin/systemctl", "show", "ssh.service"]:
                    return ("LoadState=loaded\nActiveState=active\nMainPID=" + state.pid
                            + "\nExecStart={ path=/usr/sbin/sshd ; argv[]=/usr/sbin/sshd -D $SSHD_OPTS ; ignore_errors=no ; }"
                            + "\nExecReload={ path=/usr/sbin/sshd ; argv[]=/usr/sbin/sshd -t ; ignore_errors=no ; }"
                            + " { path=/bin/kill ; argv[]=/bin/kill -HUP $MAINPID ; ignore_errors=no ; }\nDropInPaths=")
                if args == ["/usr/bin/systemctl", "reload", "ssh.service"]:
                    if state.reload_fail:
                        raise RuntimeError("native reload refused")
                    return ""
                self.assertEqual(args[:2], ["/usr/sbin/sshd", args[1]])
                self.assertIn(args[1], ("-t", "-T"))
                if args[1] == "-t":
                    if state.syntax_fail and state.dropin.exists():
                        raise RuntimeError("native syntax refused")
                    return ""
                paths = list(state.original_paths)
                exact = args[-1] == next(iter(peer_source.ssh_contexts(value).values()))
                if state.dropin.exists() and (exact or state.wrong_context):
                    paths.append(str(state.keyfile))
                return ("\n".join(("port 22", "listenaddress 0.0.0.0:22", "listenaddress [::]:22",
                                   "pubkeyauthentication yes", "chrootdirectory none",
                                   "forcecommand " + state.forcecommand,
                                   "passwordauthentication " + ("yes" if state.dropin.exists() and state.other_drift else "no"),
                                   "authorizedkeysfile " + " ".join(paths), "allowtcpforwarding yes")))
            def metadata(path):
                mode = stat.S_IFDIR | 0o755 if path in (public, directory, parts) else stat.S_IFREG | 0o644
                return SimpleNamespace(st_uid=0, st_mode=mode)
            stack.enter_context(patch.object(peer_source, "config", return_value=(value, directory)))
            stack.enter_context(patch.object(peer_source, "checked", side_effect=lambda path, *_a, **_kw: path))
            stack.enter_context(patch.object(peer_source.Path, "lstat", autospec=True, side_effect=metadata))
            stack.enter_context(patch.object(peer_source.os, "fchmod", create=True))
            stack.enter_context(patch.object(peer_source, "execute", side_effect=execute))
            yield state

    def test_forced_peer_requests_reject_shell_unknown_ops_paths_injection_and_foreign_scope(self):
        value = self.peer_config()
        prefix = peer_source.PROTOCOL + " " + value["setup_id"] + " "
        for request in ("", "sh", "python3 -c 'print(1)'", prefix + "audit; id",
                        prefix + "audit\nid", prefix + "audit /etc/shadow", prefix + "audit '{}'",
                        prefix + "controller-status ../../etc/passwd " + "2" * 32,
                        prefix + "reboot " + "1" * 32 + " " + "2" * 32,
                        prefix + "worker-status " + "1" * 32 + " " + "2" * 32,
                        prefix + "controller-status " + "1" * 32,
                        peer_source.PROTOCOL + " " + "4" * 32 + " proof"):
            with self.subTest(request=request), patch.object(peer_source, "config", return_value=(value, Path("."))), \
                    patch.object(peer_source, "helper") as helper, patch.object(peer_source, "execute") as execute, \
                    self.assertRaises(RuntimeError):
                peer_source.dispatch(request)
            helper.assert_not_called()
            execute.assert_not_called()

    def test_peer_serve_requires_existing_nonroot_account_exact_remote_ip_no_pty(self):
        value = self.peer_config()
        for uid, connection, extra in ((0, "192.168.1.12 1234 192.168.1.10 22", {}),
                                      (1000, "192.168.1.11 1234 192.168.1.10 22", {}),
                                      (1000, "192.168.1.12 1234 192.168.1.11 22", {}),
                                      (1000, "192.168.1.12 1234 192.168.1.10 22", {"SSH_TTY": "/dev/pts/0"})):
            with self.subTest(uid=uid, connection=connection, extra=extra), \
                    patch.object(peer_source, "config", return_value=(value, Path("."))), \
                    patch.object(peer_source, "account", return_value=SimpleNamespace(pw_uid=1000)), \
                    patch.object(peer_source.os, "geteuid", return_value=uid, create=True), \
                    patch.dict(peer_source.os.environ, {"SSH_CONNECTION": connection, **extra}, clear=True), \
                    patch.object(peer_source.os.sys, "argv", ["peer.py", "serve"]), \
                    patch.object(peer_source, "dispatch") as dispatch, self.assertRaises(RuntimeError):
                peer_source.main()
            dispatch.assert_not_called()

    def test_peer_known_host_requires_authoritative_fingerprint_and_key_wire_shape(self):
        value = self.peer_config()
        expected = ("192.168.1.12 " + value["peer_host_key"] + "\n").encode()
        self.assertEqual(peer_source.known_host(value), expected)
        for changes in ({"peer_host_fingerprint": "SHA256:" + "0" * 43},
                        {"peer_host_key": value["peer_host_key"] + " injected-options"},
                        {"peer_host_key": "ssh-ed25519 " + base64.b64encode(b"invalid").decode()}):
            with self.subTest(changes=changes), self.assertRaises(RuntimeError):
                peer_source.known_host(value | changes)
        # Rejection precedes any installation of a new trust file.
        invalid = value | {"peer_host_fingerprint": "SHA256:" + "0" * 43}
        setup = {k: invalid[k] for k in ("setup_id", "node", "peer", "started_epoch", "expires_epoch",
                                       "peer_host_key", "peer_host_fingerprint", "source_sha256", "operator_ip")}
        with patch.object(peer_source.Path, "read_bytes", return_value=json.dumps(setup).encode()), \
                patch.object(peer_source, "host_identity"), patch.object(peer_source, "write_new") as write, \
                patch.object(peer_source, "execute") as execute, self.assertRaises(RuntimeError):
            peer_source.prepare(value["setup_id"])
        write.assert_not_called()
        execute.assert_not_called()

    def test_peer_serve_clears_caller_executable_bus_and_loader_environment_before_fixed_reads(self):
        value = self.peer_config()
        text = peer_source.PROTOCOL + " " + value["setup_id"] + " audit"
        def dispatch(request):
            self.assertEqual(request, text)
            self.assertEqual(dict(peer_source.os.environ), peer_source.READ_ENV)
            return {"technical": True}
        with patch.object(peer_source, "config", return_value=(value, Path("."))), \
                patch.object(peer_source, "account", return_value=SimpleNamespace(pw_uid=1000)), \
                patch.object(peer_source.os, "geteuid", return_value=1000, create=True), \
                patch.dict(peer_source.os.environ, {"SSH_CONNECTION": "192.168.1.12 1234 192.168.1.10 22",
                           "SSH_ORIGINAL_COMMAND": text, "PATH": "caller-bin", "LD_LIBRARY_PATH": "caller-library",
                           "DBUS_SESSION_BUS_ADDRESS": "caller-bus"}, clear=True), \
                patch.object(peer_source.os.sys, "argv", ["peer.py", "serve"]), \
                patch.object(peer_source, "dispatch", side_effect=dispatch), patch("builtins.print"):
            peer_source.main()

    def test_outgoing_peer_uses_only_host_local_identity_scoped_trust_and_finite_command(self):
        value = self.peer_config()
        directory = Path(".peer-scope-fixture")
        grant = {k: self.journal.read()["worker_contract"][k] for k in
                 ("run_id", "host_step_id", "plan_sha256", "source_bindings", "started_epoch", "deadline_epoch")}
        touched = []
        def read(path):
            touched.append(str(path))
            self.assertEqual(path.name, "known_hosts")
            return peer_source.known_host(value)
        with patch.object(peer_source.os, "geteuid", return_value=0, create=True), \
                patch.object(peer_source, "config", return_value=(value, directory)), \
                patch.object(peer_source, "public_json", return_value=grant), \
                patch.object(peer_source, "checked", side_effect=lambda p, *_args, **_kwargs: p), \
                patch.object(peer_source.Path, "read_bytes", autospec=True, side_effect=read), \
                patch.object(peer_source, "execute", return_value="{}") as execute:
            self.assertEqual(peer_source.request("worker-status", "1" * 32), {})
        args = execute.call_args.args[0]
        self.assertEqual(args[-2:], ["rishik@192.168.1.12",
                                     peer_source.PROTOCOL + " " + value["setup_id"] + " worker-status " + "1" * 32 + " " + "2" * 32])
        for restriction in ("IdentitiesOnly=yes", "IdentityAgent=none", "ForwardAgent=no", "ClearAllForwardings=yes",
                            "StrictHostKeyChecking=yes", "GlobalKnownHostsFile=/dev/null", "UpdateHostKeys=no",
                            "PasswordAuthentication=no", "ControlPath=none"):
            self.assertIn(restriction, args)
        self.assertEqual(args[1:3], ["-F", "/dev/null"])
        self.assertNotIn("sudo", args)
        self.assertFalse(any("identity" in path for path in touched))
        self.assertFalse(any("python" in arg or "-c " in arg for arg in args))
        with patch.object(peer_source.os, "geteuid", return_value=0, create=True), \
                patch.object(peer_source, "config", return_value=(value, directory)), \
                patch.object(peer_source, "public_json", return_value=grant), \
                patch.object(peer_source, "execute") as execute, self.assertRaises(RuntimeError):
            peer_source.request("worker-status", "4" * 32, "2" * 32)
        execute.assert_not_called()

    def test_scoped_authorize_and_cleanup_never_rewrite_concurrently_written_original_keys(self):
        with self.peer_policy_environment() as env:
            originals = {path: path.read_bytes() for path in (env.original, env.second, env.config, env.baseline)}
            write_new = peer_source.write_new
            def concurrent_authorize(path, data, *args):
                if path == env.keyfile:
                    env.original.write_bytes(env.original.read_bytes() + b"unrelated-concurrent-authorize-entry\n")
                return write_new(path, data, *args)
            with patch.object(peer_source, "write_new", side_effect=concurrent_authorize):
                proof = peer_source.authorize()
            record = peer_source.public_json(env.directory / "authorization.json")
            self.assertEqual(proof["ssh_policy_sha256"], peer_source.sha(peer_source.canonical(record["policy"])))
            self.assertIn('from="192.168.1.12",restrict,command="/usr/bin/python3 -I -B ', record["entry"])
            self.assertIn('expiry-time="', record["entry"])
            unlink = Path.unlink
            def concurrent_cleanup(path, *args, **kwargs):
                if path == env.keyfile:
                    env.original.write_bytes(env.original.read_bytes() + b"unrelated-concurrent-cleanup-entry\n")
                    env.second.write_bytes(env.second.read_bytes() + b"unrelated-concurrent-second-entry\n")
                return unlink(path, *args, **kwargs)
            with patch.object(peer_source.Path, "unlink", autospec=True, side_effect=concurrent_cleanup):
                peer_source.remove_authorization(env.value, env.directory, record)
            self.assertEqual(env.original.read_bytes(), originals[env.original]
                             + b"unrelated-concurrent-authorize-entry\nunrelated-concurrent-cleanup-entry\n")
            self.assertEqual(env.second.read_bytes(), originals[env.second] + b"unrelated-concurrent-second-entry\n")
            for path in (env.config, env.baseline):
                self.assertEqual(path.read_bytes(), originals[path])
            self.assertFalse(env.keyfile.exists())
            self.assertFalse(env.dropin.exists())
            self.assertEqual([cmd for cmd in env.commands if cmd[0] == "/usr/bin/systemctl" and cmd[1] != "show"],
                             [["/usr/bin/systemctl", "reload", "ssh.service"]] * 2)

    def test_peer_expiry_source_and_pointer_drift_fail_before_request_or_pause(self):
        value = self.peer_config()
        source = {name: (DIRECTORY / name).read_bytes() for name in peer_source.SOURCES}
        def bytes_for(path):
            if path.name in source:
                return source[path.name]
            self.fail("Unexpected peer source read: " + str(path))
        for change in ({"expires_epoch": value["started_epoch"] + 10},
                       {"started_epoch": value["started_epoch"] - 90000, "expires_epoch": value["expires_epoch"] - 90000},
                       {"node": "rishik-worker1"}, {"peer": "rishik-worker1"},
                       {"source_sha256": value["source_sha256"] | {"node-maintenance-host.py": "0" * 64}}):
            with self.subTest(change=change), patch.object(peer_source, "checked", side_effect=lambda p, *_a, **_k: p), \
                    patch.object(peer_source, "public_json", side_effect=[{"setup_id": value["setup_id"]}, value | change]), \
                    patch.object(peer_source, "host_identity"), \
                    patch.object(peer_source.Path, "read_bytes", autospec=True, side_effect=bytes_for), \
                    patch.object(peer_source, "execute") as execute, self.assertRaises(RuntimeError):
                peer_source.config()
            execute.assert_not_called()
        changed = copy.deepcopy(self.plan)
        for proof in changed["peer_scope"].values():
            proof["started_epoch"] -= 50000
            proof["expires_epoch"] -= 50000
        with patch.object(c.sys.stdin, "isatty", return_value=True), patch.object(c.sys.stdout, "isatty", return_value=True), \
                patch.object(c, "API", return_value=self.api), patch.object(c.m, "remote") as remote, \
                patch.object(c.m, "kube") as kube, self.assertRaisesRegex(RuntimeError, "expiry"):
            c.apply(changed, c.sha(c.canonical(changed)), self.policy, "/existing/protected/mount", True)
        remote.assert_not_called()
        kube.assert_not_called()
        self.assertEqual(self.api.events, [])

    def test_peer_prepare_generates_only_local_scoped_identity_and_bound_public_source(self):
        value = self.peer_config()
        setup = {k: value[k] for k in ("setup_id", "node", "peer", "started_epoch", "expires_epoch",
                                     "peer_host_key", "peer_host_fingerprint", "source_sha256", "operator_ip")}
        sources = {name: (DIRECTORY / name).read_bytes() for name in peer_source.SOURCES}
        reads = []
        def read_bytes(path):
            reads.append(path.name)
            return json.dumps(setup).encode() if path.name == "setup.json" else sources[path.name]
        def read_text(path):
            reads.append(path.name)
            if path.name == "identity.pub":
                return value["peer_host_key"] + " public-fixture-comment"
            self.assertEqual(path.name, "boot_id")
            return value["original_boot_id"]
        user = SimpleNamespace(pw_uid=1000)
        def metadata(path):
            return SimpleNamespace(st_uid=1000, st_mode=stat.S_IFDIR | 0o700 if path.name == ".ssh" else stat.S_IFREG | 0o600)
        with patch.object(peer_source, "host_identity"), patch.object(peer_source, "account", return_value=user), \
                patch.object(peer_source.Path, "read_bytes", autospec=True, side_effect=read_bytes), \
                patch.object(peer_source.Path, "read_text", autospec=True, side_effect=read_text), \
                patch.object(peer_source.Path, "lstat", autospec=True, side_effect=metadata), \
                patch.object(peer_source.Path, "exists", return_value=False), patch.object(peer_source.Path, "mkdir") as mkdir, \
                patch.object(peer_source.Path, "chmod"), patch.object(peer_source.platform, "release", return_value=value["original_kernel"]), \
                patch.object(peer_source, "checked", side_effect=lambda p, *_a, **_k: p) as checked, \
                patch.object(peer_source, "write_new") as write, patch.object(peer_source, "execute", return_value="") as execute:
            result = peer_source.prepare(value["setup_id"])
        args = execute.call_args.args[0]
        self.assertEqual(args[:4], ["/usr/bin/ssh-keygen", "-q", "-t", "ed25519"])
        self.assertNotIn("-y", args)
        self.assertEqual(Path(args[-1]), peer_source.PRIVATE / value["setup_id"] / "identity")
        self.assertEqual(result["public_key"], value["peer_host_key"])
        self.assertNotIn("identity", reads)
        self.assertTrue(any(call.args == (peer_source.PRIVATE / value["setup_id"] / "identity", 0o600)
                            for call in checked.call_args_list))
        self.assertTrue(any(call.kwargs.get("mode") == 0o700 for call in mkdir.call_args_list))
        written = {call.args[0].name: call.args[1] for call in write.call_args_list}
        self.assertEqual(written["known_hosts"], peer_source.known_host(value))
        self.assertEqual(written["node-maintenance-peer.py"], sources["node-maintenance-peer.py"])
        self.assertNotIn("authorized_keys", written)
        self.assertNotIn("sudoers", written)

    def test_peer_setup_replay_or_foreign_host_fails_without_rotation_or_new_expiry(self):
        value = self.peer_config()
        setup = {k: value[k] for k in ("setup_id", "node", "peer", "started_epoch", "expires_epoch",
                                     "peer_host_key", "peer_host_fingerprint", "source_sha256", "operator_ip")}
        sources = {name: (DIRECTORY / name).read_bytes() for name in peer_source.SOURCES}
        def read(path):
            return json.dumps(setup).encode() if path.name == "setup.json" else sources[path.name]
        metadata = lambda p: SimpleNamespace(st_uid=1000, st_mode=stat.S_IFDIR | 0o700 if p.name == ".ssh" else stat.S_IFREG | 0o600)
        with patch.object(peer_source.Path, "read_bytes", autospec=True, side_effect=read), \
                patch.object(peer_source.Path, "lstat", autospec=True, side_effect=metadata), \
                patch.object(peer_source.Path, "exists", return_value=True), \
                patch.object(peer_source, "account", return_value=SimpleNamespace(pw_uid=1000)), \
                patch.object(peer_source, "checked"), patch.object(peer_source, "write_new") as write, \
                patch.object(peer_source, "execute") as execute, \
                patch.object(peer_source, "host_identity") as identity:
            identity.side_effect = RuntimeError("foreign native host")
            with self.assertRaisesRegex(RuntimeError, "foreign"):
                peer_source.prepare(value["setup_id"])
            identity.side_effect = None
            with self.assertRaisesRegex(RuntimeError, "never reset"):
                peer_source.prepare(value["setup_id"])
        write.assert_not_called()
        execute.assert_not_called()

    def test_scoped_peer_probe_records_real_reverse_identity_and_audit_not_creation_success(self):
        value = self.peer_config()
        own = {k: v for k, v in self.plan["peer_scope"][c.SERVER].items() if k != "probe"}
        remote = {k: v for k, v in self.plan["peer_scope"][c.WORKER].items() if k != "probe"}
        audit = {"name": c.WORKER, "boot_id": remote["original_boot_id"], "kernel": remote["original_kernel"]}
        with patch.object(peer_source, "config", return_value=(value, Path(".peer-scope-fixture"))), \
                patch.object(peer_source, "local_proof", return_value=own), \
                patch.object(peer_source, "validate_policy"), patch.object(peer_source, "public_json"), \
                patch.object(peer_source, "request", side_effect=[remote, audit]) as request, \
                patch.object(peer_source, "write_new") as write:
            peer_source.probe()
        self.assertEqual([call.args for call in request.call_args_list], [("proof",), ("audit",)])
        probe = json.loads(write.call_args.args[1])
        self.assertEqual(probe["peer_proof_sha256"], peer_source.sha(peer_source.canonical(remote)))
        with patch.object(peer_source, "config", return_value=(value, Path(".peer-scope-fixture"))), \
                patch.object(peer_source, "local_proof", return_value=own), \
                patch.object(peer_source, "validate_policy"), patch.object(peer_source, "public_json"), \
                patch.object(peer_source, "request", return_value=remote | {"incoming_key_fingerprint": "foreign"}), \
                patch.object(peer_source, "write_new") as write, self.assertRaisesRegex(RuntimeError, "identity drift"):
            peer_source.probe()
        write.assert_not_called()

    def test_peer_authorization_receipt_proves_exact_restrictions_not_any_existing_public_key(self):
        with self.peer_policy_environment() as env:
            peer_source.authorize()
            record = peer_source.public_json(env.directory / "authorization.json")
            self.assertEqual(peer_source.local_proof(require_probe=False)["incoming_key_fingerprint"],
                             peer_source.public_key(env.value["peer_host_key"]))
            for unsafe in (env.value["peer_host_key"], record["entry"].replace(",restrict,", ","),
                           record["entry"].replace('from="192.168.1.12"', 'from="*"')):
                with self.subTest(entry=unsafe), patch.object(peer_source, "public_json", return_value=record | {"entry": unsafe}), \
                        self.assertRaisesRegex(RuntimeError, "drifted"):
                    peer_source.local_proof(require_probe=False)

    def test_native_peer_policy_preserves_all_original_paths_and_repeated_properties(self):
        with self.peer_policy_environment() as env:
            original = peer_source.policy_baseline(env.value)
            self.assertEqual(len(original["profiles"]), 7)
            peer_source.authorize()
            record = peer_source.public_json(env.directory / "authorization.json")
            for index, (name, context) in enumerate(peer_source.ssh_contexts(env.value).items()):
                effective = peer_source.ssh_effective(context)
                self.assertEqual(effective["listenaddress"], ["0.0.0.0:22", "[::]:22"])
                wanted = list(original["profiles"][name]["authorized_keys"])
                if index == 0:
                    wanted.append(str(env.keyfile))
                self.assertEqual(effective["authorizedkeysfile"][0].split(), wanted)
                self.assertEqual(peer_source.policy_shape(effective)["other_sha256"], original["profiles"][name]["other_sha256"])
            self.assertEqual(record["policy"], original)
            peer_source.remove_authorization(env.value, env.directory, record)
            self.assertEqual(peer_source.policy_baseline(env.value), original)

    def test_native_peer_policy_rejects_other_setting_or_nonpeer_effective_delta_before_reload(self):
        for change in ("other_drift", "wrong_context"):
            with self.subTest(change=change), self.peer_policy_environment() as env:
                setattr(env, change, True)
                with self.assertRaisesRegex(RuntimeError, "beyond exact peer"):
                    peer_source.authorize()
                self.assertFalse(env.keyfile.exists())
                self.assertFalse((env.directory / "authorization.json").exists())
                self.assertNotIn(["/usr/bin/systemctl", "reload", "ssh.service"], env.commands)
                pending = json.loads((env.directory / "authorization.pending.json").read_bytes())
                setattr(env, change, False)
                peer_source.remove_authorization(env.value, env.directory, pending)
                self.assertFalse(env.dropin.exists())

    def test_native_peer_setup_rejects_existing_match_foreign_include_and_disabled_original_keys(self):
        for change in ("match", "include", "foreign-scope", "disabled", "forcecommand", "ownership"):
            with self.subTest(change=change), self.peer_policy_environment() as env, ExitStack() as stack:
                if change == "match":
                    env.baseline.write_bytes(b"Match User rishik\n    PasswordAuthentication yes\n")
                elif change == "include":
                    env.baseline.write_bytes(b"Include another-config\n")
                elif change == "foreign-scope":
                    (env.parts / ("90-node-maintenance-peer-" + "9" * 32 + ".conf")).write_bytes(b"# foreign\n")
                elif change == "disabled":
                    env.original_paths = ["none"]
                elif change == "forcecommand":
                    env.forcecommand = "foreign-command"
                else:
                    stack.enter_context(patch.object(peer_source.Path, "lstat", return_value=
                                                     SimpleNamespace(st_uid=1000, st_mode=stat.S_IFREG | 0o644)))
                before = env.original.read_bytes()
                with self.assertRaises(RuntimeError):
                    peer_source.authorize()
                self.assertFalse(env.keyfile.exists())
                self.assertFalse(env.dropin.exists())
                self.assertFalse((env.directory / "authorization.pending.json").exists())
                self.assertEqual(env.original.read_bytes(), before)

    def test_native_peer_source_config_drift_stops_activation_and_cleanup_without_overwrite(self):
        with self.peer_policy_environment() as env:
            write_new = peer_source.write_new
            changed = b"# concurrent original SSH config writer\nPasswordAuthentication yes\n"
            def write(path, data, *args):
                if path == env.dropin:
                    env.baseline.write_bytes(changed)
                return write_new(path, data, *args)
            with patch.object(peer_source, "write_new", side_effect=write), self.assertRaisesRegex(RuntimeError, "config drift"):
                peer_source.authorize()
            pending = json.loads((env.directory / "authorization.pending.json").read_bytes())
            with self.assertRaisesRegex(RuntimeError, "config drift"):
                peer_source.remove_authorization(env.value, env.directory, pending)
            self.assertEqual(env.baseline.read_bytes(), changed)
            self.assertTrue(env.dropin.exists())
            self.assertFalse(env.keyfile.exists())
            self.assertFalse((env.directory / "authorization.revoked.json").exists())
            self.assertNotIn(["/usr/bin/systemctl", "reload", "ssh.service"], env.commands)

    def test_native_peer_syntax_or_reload_failure_never_creates_ready_credential_and_pending_cleanup_is_bounded(self):
        for change in ("syntax_fail", "reload_fail"):
            with self.subTest(change=change), self.peer_policy_environment() as env:
                before = {path: path.read_bytes() for path in (env.original, env.config, env.baseline)}
                setattr(env, change, True)
                with self.assertRaisesRegex(RuntimeError, "native .* refused"):
                    peer_source.authorize()
                self.assertFalse(env.keyfile.exists())
                self.assertFalse((env.directory / "authorization.json").exists())
                with self.assertRaises(OSError):
                    peer_source.local_proof(require_probe=False)
                pending = json.loads((env.directory / "authorization.pending.json").read_bytes())
                setattr(env, change, False)
                peer_source.remove_authorization(env.value, env.directory, pending)
                self.assertFalse(env.dropin.exists())
                self.assertFalse(env.keyfile.exists())
                for path, data in before.items():
                    self.assertEqual(path.read_bytes(), data)
                self.assertEqual(env.value["expires_epoch"], self.peer_config()["expires_epoch"])

    def test_native_peer_cleanup_reload_failure_revokes_protocol_and_retries_only_owned_artifacts(self):
        with self.peer_policy_environment() as env:
            before = {path: path.read_bytes() for path in (env.original, env.config, env.baseline)}
            peer_source.authorize()
            record = peer_source.public_json(env.directory / "authorization.json")
            env.reload_fail = True
            with self.assertRaisesRegex(RuntimeError, "native reload refused"):
                peer_source.remove_authorization(env.value, env.directory, record)
            self.assertTrue((env.directory / "authorization.revoked.json").exists())
            self.assertFalse((env.directory / "authorization.cleaned.json").exists())
            self.assertFalse(env.keyfile.exists())
            self.assertFalse(env.dropin.exists())
            with self.assertRaises(RuntimeError):
                peer_source.local_proof(require_probe=False)
            env.reload_fail = False
            peer_source.remove_authorization(env.value, env.directory, record)
            self.assertTrue((env.directory / "authorization.cleaned.json").exists())
            for path, data in before.items():
                self.assertEqual(path.read_bytes(), data)

    def test_native_peer_failed_postreload_validation_or_missing_owned_file_never_proves_readiness(self):
        with self.peer_policy_environment() as env:
            execute = peer_source.execute
            def changed_after_reload(args):
                result = execute(args)
                if args == ["/usr/bin/systemctl", "reload", "ssh.service"]:
                    env.other_drift = True
                return result
            with patch.object(peer_source, "execute", side_effect=changed_after_reload), \
                    self.assertRaisesRegex(RuntimeError, "beyond exact peer"):
                peer_source.authorize()
            self.assertFalse(env.keyfile.exists())
            self.assertFalse((env.directory / "authorization.json").exists())
        with self.peer_policy_environment() as env:
            peer_source.authorize()
            env.keyfile.unlink()
            with self.assertRaises(OSError):
                peer_source.local_proof(require_probe=False)

    def test_native_peer_daemon_alternate_running_arguments_or_nonstandard_reload_are_not_waived(self):
        with self.peer_policy_environment() as env:
            (env.proc / "1234" / "cmdline").write_bytes(b"/usr/sbin/sshd\0-D\0-f\0other-config\0")
            with self.assertRaisesRegex(RuntimeError, "alternate/unverified"):
                peer_source.authorize()
            self.assertFalse(env.dropin.exists())
        for old, new in (("/bin/kill -HUP $MAINPID", "/usr/bin/systemctl restart ssh.service"),
                         ("path=/bin/kill", "path=/usr/bin/python3"), ("ignore_errors=no", "ignore_errors=yes")):
            with self.subTest(new=new), self.peer_policy_environment() as env:
                execute = peer_source.execute
                def nonstandard(args):
                    return execute(args).replace(old, new)
                with patch.object(peer_source, "execute", side_effect=nonstandard), self.assertRaisesRegex(RuntimeError, "Nonstandard SSH"):
                    peer_source.authorize()
                self.assertFalse(env.dropin.exists())

    def test_pending_peer_cleanup_resumes_after_revocation_without_requiring_invalid_candidate_policy(self):
        with self.peer_policy_environment() as env:
            env.syntax_fail = True
            with self.assertRaises(RuntimeError):
                peer_source.authorize()
            pending = json.loads((env.directory / "authorization.pending.json").read_bytes())
            unlink = Path.unlink
            def interrupted(path, *args, **kwargs):
                if path == env.dropin:
                    raise OSError("interrupted exact owned removal")
                return unlink(path, *args, **kwargs)
            with patch.object(peer_source.Path, "unlink", autospec=True, side_effect=interrupted), self.assertRaises(OSError):
                peer_source.remove_authorization(env.value, env.directory, pending)
            self.assertTrue((env.directory / "authorization.revoked.json").exists())
            peer_source.remove_authorization(env.value, env.directory, pending)
            self.assertFalse(env.dropin.exists())
            self.assertTrue((env.directory / "authorization.cleaned.json").exists())

    def test_full_explicit_peer_cleanup_removes_only_owned_policy_and_private_identity_without_reading_key(self):
        with self.peer_policy_environment() as env:
            for name in peer_source.SOURCES:
                (env.directory / name).write_bytes((DIRECTORY / name).read_bytes())
            (env.directory / "config.json").write_bytes(peer_source.canonical(env.value))
            (peer_source.PUBLIC / "current.json").write_bytes(peer_source.canonical({"setup_id": env.value["setup_id"]}))
            private = env.directory.parent.parent / "private"
            identity = private / env.value["setup_id"]
            identity.mkdir(parents=True)
            for name in ("identity", "identity.pub", "known_hosts"):
                (identity / name).touch()
            originals = {path: path.read_bytes() for path in (env.original, env.second, env.config, env.baseline)}
            peer_source.authorize()
            read = Path.read_bytes
            def public_read_only(path):
                self.assertNotEqual(path.name, "identity", "Private identity bytes must never be read/exported")
                return read(path)
            with patch.object(peer_source, "PRIVATE", private), patch.object(peer_source, "host_identity"), \
                    patch.object(peer_source, "helper"), patch.object(peer_source.Path, "read_bytes", autospec=True, side_effect=public_read_only):
                result = peer_source.cleanup(env.value["setup_id"])
            self.assertTrue(result["owned_keyfile_dropin_removed"])
            self.assertFalse(identity.exists())
            self.assertFalse((peer_source.PUBLIC / "current.json").exists())
            self.assertTrue((env.directory / "config.json").exists())
            for path, data in originals.items():
                self.assertEqual(path.read_bytes(), data)

    def test_cold_transport_binds_peer_source_and_rejects_extra_source_or_arbitrary_path(self):
        bundle = {name: base64.b64encode((c.m.ROOT / Path(name)).read_bytes()).decode() for name in c.BUNDLE_FILES}
        for extra in (None, "../../arbitrary-source.py"):
            files = bundle if extra is None else bundle | {extra: base64.b64encode(b"not-approved").decode()}
            transport = {"plan": self.plan, "approval": self.approval, "host_step_id": "2" * 32,
                         "bundle": base64.b64encode(zlib.compress(c.canonical(files))).decode()}
            data = c.canonical(transport)
            payload = {"run_id": "1" * 32,
                       "input_file": str(Path("/home/rishik/.local/state/node-maintenance-cold") / ("1" * 32) / "source.json"),
                       "input_sha256": c.sha(data)}
            with self.subTest(extra=extra), patch.object(h.Path, "read_bytes", return_value=data), \
                    patch.object(h.Path, "is_symlink", return_value=False):
                if extra:
                    with self.assertRaisesRegex(RuntimeError, "source bundle"):
                        h.read_transport(payload)
                else:
                    _, actual = h.read_transport(payload)
                    self.assertIn(".github/scripts/node-maintenance-peer.py", actual)

    def test_run_grant_is_exact_single_use_and_does_not_renew_scope_or_outage_deadline(self):
        value = self.peer_config()
        start = self.journal.read()["started_epoch"]
        with patch.object(peer_source.os, "geteuid", return_value=0, create=True), \
                patch.object(peer_source, "local_proof", return_value=self.plan["peer_scope"][c.SERVER]), \
                patch.object(peer_source, "config", return_value=(value, Path(".peer-scope-fixture"))), \
                patch.object(peer_source, "validate_policy"), patch.object(peer_source, "public_json"), \
                patch.object(peer_source, "write_new") as write:
            peer_source.register_run(self.plan, "1" * 32, "2" * 32, start, start + 43200)
            grant = json.loads(write.call_args.args[1])
            self.assertEqual(grant["deadline_epoch"], start + 43200)
            self.assertEqual(grant["plan_sha256"], self.approval)
            write.side_effect = FileExistsError("immutable run already registered")
            with self.assertRaises(FileExistsError):
                peer_source.register_run(self.plan, "1" * 32, "2" * 32, start, start + 43200)
        with patch.object(c.time, "time", return_value=start + 40000):
            c.check_peer_scope(self.plan, starting=False)
        self.assertEqual(self.plan["peer_scope"][c.SERVER]["expires_epoch"], value["expires_epoch"])
        self.assertEqual(self.journal.read()["deadline_epoch"], start + 43200)

    def test_peer_worker_status_reads_actual_public_record_and_rejects_foreign_step(self):
        contract = self.journal.read()["worker_contract"]
        value = self.peer_config(c.WORKER)
        grant = {k: contract[k] for k in ("run_id", "host_step_id", "plan_sha256", "source_bindings",
                                        "started_epoch", "deadline_epoch")}
        text = peer_source.PROTOCOL + " " + value["setup_id"] + " worker-status " + "1" * 32 + " " + "2" * 32
        with self.finalizer_environment(), patch.object(peer_source, "config", return_value=(value, Path("."))), \
                patch.object(peer_source, "helper", return_value=h), \
                patch.object(peer_source, "activation_verified"), \
                patch.object(peer_source, "public_json", return_value=grant):
            proof = peer_source.dispatch(text)
            self.assertEqual(proof["record"], self.api.worker.read())
            self.assertEqual(proof["unit"]["MainPID"], self.api.worker_native["MainPID"])
            with self.assertRaisesRegex(RuntimeError, "Foreign"):
                peer_source.dispatch(text[:-32] + "4" * 32)
            wrong = dict(grant, source_bindings={})
            with patch.object(peer_source, "public_json", return_value=wrong), self.assertRaisesRegex(RuntimeError, "binding"):
                peer_source.dispatch(text)

    def test_peer_native_idle_keeps_timer_shape_and_checks_absent_and_live_service_cgroups(self):
        units = []
        def native(unit):
            units.append(unit)
            state = {"LoadState": "not-found", "ActiveState": "inactive", "Job": "0"}
            if unit in ("reboot.target", "shutdown.target"):
                state["LoadState"] = "loaded"
            return state
        with patch.object(h, "worker_unit_status", side_effect=native), patch.object(h, "worker_cgroup_empty") as groups:
            result = peer_source.idle(h, "1" * 32, "2" * 32)
        self.assertTrue(result["cgroups_empty"])
        self.assertEqual(len(result["states"]), 6)
        self.assertEqual(len(groups.call_args_list), 3)
        self.assertTrue(all(call.args[0].endswith(".service") for call in groups.call_args_list))
        with patch.object(h, "worker_unit_status", side_effect=native), \
                patch.object(h, "worker_cgroup_empty", side_effect=RuntimeError("surviving child")), \
                self.assertRaisesRegex(RuntimeError, "surviving child"):
            peer_source.idle(h, "1" * 32, "2" * 32)

    def test_peer_controller_status_uses_public_receipt_and_native_executor_guard_metadata(self):
        value = self.peer_config()
        contract = self.journal.read()["worker_contract"]
        grant = {k: contract[k] for k in ("run_id", "host_step_id", "plan_sha256", "source_bindings",
                                        "started_epoch", "deadline_epoch")}
        public = {k: self.journal.read()[k] for k in ("run_id", "host_step_id", "plan_sha256", "started_epoch",
                                                    "deadline_epoch", "worker_contract", "mode", "phase")}
        text = peer_source.PROTOCOL + " " + value["setup_id"] + " controller-status " + "1" * 32 + " " + "2" * 32
        def native(unit):
            return {"LoadState": "loaded", "ActiveState": "activating" if unit.endswith(".service") else "active",
                    "MainPID": "2345"}
        with patch.object(peer_source, "config", return_value=(value, Path("."))), \
                patch.object(peer_source, "helper", return_value=h), patch.object(peer_source, "public_json", return_value=grant), \
                patch.object(peer_source, "activation_verified"), \
                patch.object(h, "cold_public", return_value=copy.deepcopy(public)) as read, \
                patch.object(h, "worker_unit_status", side_effect=native), patch.object(peer_source, "execute", return_value="enabled"), \
                patch.object(peer_source.Path, "read_text", return_value="old-other"), \
                patch.object(peer_source.socket, "gethostname", return_value=c.SERVER), \
                patch.object(peer_source.platform, "release", return_value=value["original_kernel"]):
            result = peer_source.dispatch(text)
        read.assert_called_once_with(Path("/var/lib/node-maintenance-cold-status") / ("1" * 32 + ".json"))
        self.assertTrue(result["executor_active"])
        self.assertTrue(result["guard_active"])
        self.assertTrue(result["guard_enabled"])
        self.assertNotIn("mutations", result)
        self.assertNotIn("plan", result)

    def test_peer_host_audit_is_actual_finite_simulation_not_package_update_or_private_read(self):
        value = self.peer_config(c.WORKER)
        texts = {"/etc/os-release": 'ID=ubuntu\nVERSION_ID="26.04"\n',
                 "/proc/meminfo": "MemAvailable: 102400 kB\nSUnreclaim: 0 kB\n",
                 "/proc/stat": "btime 1234\n", "/proc/sys/kernel/random/boot_id": "old-worker2"}
        commands = []
        def command(args):
            commands.append(args)
            self.assertIn(args, [["apt-get", "-s", "--no-remove", "--with-new-pkgs", "upgrade"],
                                 ["systemctl", "get-default"]])
            return "Inst fixture (2 Ubuntu:26.04/stable [amd64])" if args[0] == "apt-get" else "multi-user.target"
        with patch.object(peer_source, "config", return_value=(value, Path("."))), \
                patch.object(peer_source, "helper", return_value=h), \
                patch.object(peer_source, "activation_verified"), \
                patch.object(h.Path, "read_text", autospec=True, side_effect=lambda path: texts[path.as_posix()]), \
                patch.object(h.Path, "glob", return_value=[]), patch.object(h.Path, "exists", return_value=False), \
                patch.object(h.shutil, "disk_usage", return_value=SimpleNamespace(free=10 * 1024**3)), \
                patch.object(h, "installed", return_value=False), patch.object(h, "command", side_effect=command), \
                patch.object(h.socket, "gethostname", return_value=c.WORKER), \
                patch.object(h.platform, "release", return_value=value["original_kernel"]), \
                patch.object(h.platform, "machine", return_value="x86_64"):
            audit = peer_source.dispatch(peer_source.PROTOCOL + " " + value["setup_id"] + " audit")
        self.assertEqual(audit["boot_id"], "old-worker2")
        self.assertEqual(audit["os_version"], "26.04")
        self.assertEqual(audit["pending_packages"], ["Inst fixture (2 Ubuntu:26.04/stable [amd64])"])
        self.assertEqual(len(commands), 2)

    def test_peer_expiry_in_root_start_cancels_before_first_pause_and_never_prints_ready(self):
        protocol = MagicMock()
        protocol.plan_scope.side_effect = RuntimeError("expired exact peer scope")
        self.journal.state.update(mode="registered")
        with patch.object(h, "load_peer", return_value=protocol), patch.object(h, "load_cold", return_value=c), \
                patch.object(c, "load_runtime", return_value=self.coordinator), \
                patch.object(h, "command") as command, patch("builtins.print") as output, \
                self.assertRaisesRegex(RuntimeError, "expired"):
            h.cold_control({"operation": "cold-start", "run_id": "1" * 32})
        self.assertTrue(self.journal.read()["host_slot_cancelled"])
        self.assertEqual(self.api.events, [])
        self.assertEqual(command.call_args.args[0][-1], "node-maintenance-cold-" + "1" * 32 + "-guard.service")
        output.assert_not_called()

    def test_peer_cleanup_retains_identity_for_live_foreign_or_nonterminal_controller_run(self):
        value = self.peer_config(c.WORKER)
        source = (DIRECTORY / "node-maintenance-peer.py").read_bytes()
        contract = self.journal.read()["worker_contract"]
        grant = {k: contract[k] for k in ("run_id", "host_step_id", "plan_sha256", "source_bindings",
                                        "started_epoch", "deadline_epoch")}
        def public(path):
            if path.name == "current.json":
                return {"setup_id": value["setup_id"]}
            return value if path.name == "config.json" else grant
        helper = MagicMock()
        for record in ({"phase": "waiting", "contract": contract},
                       {"phase": "packages-complete", "contract": contract | {"host_step_id": "4" * 32}},
                       {"phase": "packages-complete", "contract": contract}):
            helper.worker_status.return_value = {"record": record}
            with self.subTest(record=record["phase"], step=record["contract"]["host_step_id"]), \
                    patch.object(peer_source, "checked", side_effect=lambda p, *_a, **_k: p), \
                    patch.object(peer_source, "public_json", side_effect=public), \
                    patch.object(peer_source.Path, "read_bytes", return_value=source), \
                    patch.object(peer_source.Path, "exists", return_value=True), \
                    patch.object(peer_source, "host_identity"), patch.object(peer_source, "helper", return_value=helper), \
                    patch.object(peer_source, "request", return_value={"completed": False}) as request, \
                    patch.object(peer_source, "remove_authorization") as edit, patch.object(peer_source.Path, "unlink") as unlink, \
                    patch.object(peer_source, "execute") as execute, self.assertRaisesRegex(RuntimeError, "retain"):
                peer_source.cleanup(value["setup_id"])
            edit.assert_not_called()
            unlink.assert_not_called()
            execute.assert_not_called()

    def test_bound_host_reserve_revalidates_exact_payload_and_persistent_local_clock_before_reboot(self):
        peer = self.standby_peer(authorized=True)
        worker = self.worker_journal("packages-complete")
        contract = worker.read()["contract"]
        payload = {**contract["os_payload"], "operation": "reboot", "contract_sha256": c.sha(c.canonical(contract))}
        with patch.object(h, "cold_peer_status", return_value=peer), \
                patch.object(h, "worker_runtime", return_value=(Path("."), contract)), \
                patch.object(h, "worker_transaction", side_effect=lambda _, fn: worker.transaction(fn)), \
                patch.object(h, "command") as command:
            h.reserve(payload)
            for changed in ({"kernel": "caller-chosen"}, {"pending_packages": ["caller-chosen"]}, {"command": "caller-chosen"}):
                with self.subTest(changed=changed), self.assertRaisesRegex(RuntimeError, "payload drift"):
                    h.reserve(payload | changed)
            worker.state["last_epoch"] = time.time() + 60
            with self.assertRaisesRegex(RuntimeError, "local clock"):
                h.reserve(payload)
        command.assert_not_called()

    def test_root_executor_start_failure_revokes_slot_without_manual_late_recovery(self):
        self.journal.state.update(mode="registered", guard_verified_epoch=time.time())
        versions = {"clientVersion": {"gitVersion": self.plan["controller_client"]["version"]},
                    "serverVersion": {"gitVersion": self.plan["kubectl"]["server"]}}
        def command(args):
            if args[1] == "is-active":
                return "active"
            if args[1] == "is-enabled":
                return "enabled"
            if args[-1].endswith("-guard.service"):
                return ""
            raise RuntimeError("executor start failed")
        with patch.object(h, "load_cold", return_value=c), patch.object(c, "load_runtime", return_value=self.coordinator), \
                patch.object(h, "load_peer", return_value=MagicMock()), \
                patch.object(self.api, "kube", return_value=json.dumps(versions)), \
                patch.object(self.api, "host_audit", return_value=self.plan["bindings"][c.WORKER]), \
                patch.object(h, "command", side_effect=command) as invoke, patch("builtins.print") as output, \
                self.assertRaisesRegex(RuntimeError, "executor start failed"):
            h.cold_control({"operation": "cold-start", "run_id": "1" * 32})
        self.assertTrue(self.journal.read()["host_slot_cancelled"])
        self.assertEqual(self.journal.read()["mode"], "recovering")
        self.assertEqual(len([call for call in invoke.call_args_list if call.args[0][1] == "start"]), 2)
        self.assertIn("Manual authentication complete", output.call_args.args[0])
        self.assertFalse(any(e[0] == "patch" for e in self.api.events))

    def test_native_peer_failure_revokes_waiting_or_claimed_slot_not_just_waiting_for_timeout(self):
        for phase in ("waiting", "claimed"):
            proof = self.api.worker_status(self.policy, "1" * 32)
            proof["record"]["phase"] = phase
            self.authorize_host_slot()
            with self.subTest(phase=phase), patch.object(self.api, "worker_status", return_value=proof), \
                    patch.object(self.coordinator, "infrastructure", side_effect=RuntimeError("Native peer protection lost")), \
                    self.assertRaisesRegex(RuntimeError, "Native peer"):
                self.coordinator.host_completed()
            self.assertTrue(self.journal.read()["host_slot_cancelled"])
            self.assertFalse(self.journal.read()["host_reboot_authorized"])
        self.assertFalse(any(e[0] == "patch" for e in self.api.events))

    def test_exact_bound_deferred_reboot_accepts_only_original_boot_and_packages_complete_receipt(self):
        worker = self.worker_journal("packages-complete")
        contract = worker.read()["contract"]
        payload = contract["os_payload"] | {"operation": "reboot", "contract_sha256": c.sha(c.canonical(contract))}
        def read(path, *args, **kwargs):
            return "old-worker2" if str(path).endswith("boot_id") else json.dumps(worker.read())
        with patch.object(h.os, "geteuid", return_value=0, create=True), patch.object(h.socket, "gethostname", return_value=c.WORKER), \
                patch.object(h, "worker_runtime", return_value=(Path("."), contract)), \
                patch.object(h.Path, "read_text", autospec=True, side_effect=read), \
                patch.object(h, "cold_peer_status", side_effect=lambda _: self.standby_peer(authorized=True)), \
                patch.object(h, "worker_transaction", side_effect=lambda _, fn: worker.transaction(fn)), patch.object(h, "command") as command:
            h.main(payload)
            command.assert_called_once_with(["systemctl", "reboot"])
            command.reset_mock()
            for change in ({"kernel": "unbound"}, {"deadline_epoch": payload["deadline_epoch"] + 1}):
                with self.subTest(change=change), self.assertRaisesRegex(RuntimeError, "exact payload"):
                    h.main(payload | change)
            worker.state["phase"] = "failed"
            with self.assertRaisesRegex(RuntimeError, "terminal receipt"):
                h.main(payload)
            command.assert_not_called()
        with patch.object(h.os, "geteuid", return_value=0, create=True), patch.object(h.socket, "gethostname", return_value=c.WORKER), \
                patch.object(h.Path, "read_text", return_value="already-new-boot"), patch.object(h, "command") as command, \
                self.assertRaisesRegex(RuntimeError, "boot identity drift"):
            h.main(payload)
        command.assert_not_called()

    def test_expired_package_receipt_can_support_safe_recovery_but_not_renew_success_clock(self):
        self.authorize_host_slot()
        deadline = self.journal.read()["deadline_epoch"]
        self.coordinator.now = lambda: deadline + 60
        receipt = {"run_id": "2" * 32, "name": c.WORKER, "operation": "os", "old_boot_id": "old-worker2",
                   "expected_kernel": self.policy["kernel"], "completed_epoch": deadline + 30,
                   "headless": True, "package_removals": False,
                   "contract_sha256": self.journal.read()["worker_contract_sha256"]}
        self.coordinator.ingest_worker_receipt(receipt)
        self.assertTrue(self.journal.read()["host_packages_complete"])
        with self.assertRaisesRegex(RuntimeError, "reserve"):
            self.journal.transaction(lambda s: self.coordinator.gate(s, True))
        self.assertEqual(self.journal.read()["deadline_epoch"], deadline)

    def test_setup_without_original_cancellation_channel_reports_real_blocker_without_unlocking(self):
        start = self.journal.read()["started_epoch"]
        with patch.object(c.sys.stdin, "isatty", return_value=True), patch.object(c.sys.stdout, "isatty", return_value=True), \
                patch.object(c, "API", return_value=self.api), patch.object(c, "build_plan", return_value=self.plan), \
                patch.object(c.m, "remote", return_value={"off_node_directory_verified": True}), patch.object(c.m, "kube") as kube, \
                patch.object(c, "stage_transport", side_effect=RuntimeError("strict trusted host missing")), \
                patch.object(c.Path, "mkdir"), patch.object(c.Path, "write_bytes"), \
                patch.object(c, "request_recovery", side_effect=RuntimeError("original channel unavailable")), \
                patch.object(c.uuid, "uuid4", side_effect=[SimpleNamespace(hex="1" * 32), SimpleNamespace(hex="2" * 32)]), \
                patch.object(c.time, "time", return_value=start), self.assertRaisesRegex(RuntimeError, "runtime blocker"):
            c.apply(self.plan, self.approval, self.policy, "/existing/protected/mount", True)
        self.assertFalse(any("delete" in call.args for call in kube.call_args_list))
        self.assertEqual(self.journal.read()["deadline_epoch"], start + 43200)
        self.assertEqual(self.journal.read()["mutations"], {})

    def test_enlarged_exact_helper_still_fits_existing_windows_privileged_transport(self):
        payload = c.controller_payload(self.plan, self.policy, "1" * 32, "cold-worker-register",
                                       self.journal.read()["started_epoch"], self.journal.read()["deadline_epoch"])
        payload.update(input_file="/home/rishik/.local/state/node-maintenance-cold/" + "1" * 32 + "/source.json",
                       input_sha256="a" * 64, name=c.WORKER)
        with patch.object(c.m.subprocess, "run", return_value=SimpleNamespace(returncode=0)) as run:
            c.m.remote(self.plan["nodes"][0], payload, privileged=True)
        self.assertLess(len(c.m.subprocess.list2cmdline(run.call_args.args[0])), 31000)

    def test_deferred_reboot_preserves_exact_source_bytes_including_windows_line_endings(self):
        import ast
        payload = self.authorize_host_slot()
        with patch.object(h, "cold_peer_status", side_effect=lambda _: self.journal.read()), patch.object(h, "command") as command:
            h.schedule_reboot(payload)
        bootstrap = ast.parse(command.call_args.args[0][-2])
        values = [node.value for node in ast.walk(bootstrap) if isinstance(node, ast.Constant)
                  and isinstance(node.value, str)]
        self.assertEqual(len(values), 1)
        self.assertEqual(zlib.decompress(base64.b64decode(values[0])), Path(h.__file__).read_bytes())

    def test_ambiguous_final_start_failure_never_prompts_for_automatic_late_sudo(self):
        start = self.journal.read()["started_epoch"]
        def remote(node, payload, privileged=False):
            if payload["operation"] == "recovery-audit":
                return {"off_node_directory_verified": True}
            if payload["operation"] == "cold-worker-status":
                return self.api.worker_status(self.policy, payload["run_id"])
            if payload["operation"] == "cold-start":
                raise RuntimeError("SSH lost after unit might have started")
        with patch.object(c.sys.stdin, "isatty", return_value=True), patch.object(c.sys.stdout, "isatty", return_value=True), \
                patch.object(c, "API", return_value=self.api), patch.object(c, "build_plan", return_value=self.plan), \
                patch.object(c.m, "remote", side_effect=remote), patch.object(c.m, "kube"), \
                patch.object(c, "stage_transport", return_value="/bound/source.json"), patch.object(c.Path, "mkdir"), \
                patch.object(c.Path, "write_bytes"), patch.object(c, "request_recovery") as recover, \
                patch.object(c.uuid, "uuid4", side_effect=[SimpleNamespace(hex="1" * 32), SimpleNamespace(hex="2" * 32)]), \
                patch.object(c.time, "time", return_value=start), self.assertRaisesRegex(RuntimeError, "cancellation is unverified"):
            c.apply(self.plan, self.approval, self.policy, "/existing/protected/mount", True)
        recover.assert_not_called()

    def test_native_hpa_pause_is_observed_and_identity_preserved(self):
        for row in self.plan["autoscalers"]:
            self.coordinator.mutate(row, "pause", True)
        self.coordinator.hpas_paused()
        hpa = self.api.get("horizontalpodautoscalers", self.plan["hpas"][0]["namespace"], self.plan["hpas"][0]["name"])
        hpa["spec"]["behavior"]["scaleUp"]["selectPolicy"] = "Max"
        with self.assertRaisesRegex(RuntimeError, "actually be Disabled"):
            self.coordinator.hpas_paused()
        hpa["spec"]["behavior"]["scaleUp"]["selectPolicy"] = "Disabled"
        hpa["metadata"]["ownerReferences"][0]["uid"] = "foreign"
        with self.assertRaisesRegex(RuntimeError, "identity/ownership"):
            self.coordinator.hpas_paused()

    def test_native_hpa_status_without_observed_generation_is_supported(self):
        for row in self.plan["autoscalers"]:
            self.coordinator.mutate(row, "pause", True)
        for hpa in self.api.items("horizontalpodautoscalers"):
            self.assertNotIn("observedGeneration", hpa["status"])
            self.assertGreater(hpa["metadata"]["generation"], 1)
        self.coordinator.hpas_paused()
        row = self.plan["autoscalers"][0]
        self.api.get("scaledobjects.keda.sh", row["namespace"], row["name"])["status"]["conditions"] = []
        with self.assertRaisesRegex(RuntimeError, "native pause not observed"):
            self.coordinator.hpas_paused()

    def test_behaviorless_hpa_defaults_and_original_full_spec_restore_exactly(self):
        for row in self.plan["autoscalers"]:
            self.coordinator.mutate(row, "pause", True)
        self.coordinator.hpas_paused()
        for hpa in self.api.items("horizontalpodautoscalers"):
            original = self.api.original_hpa_specs[hpa["metadata"]["uid"]]
            self.assertNotIn("behavior", original)
            self.assertEqual(hpa["spec"]["behavior"], PAUSED_DEFAULT_RULES)
            self.assertEqual(hpa["spec"]["metrics"], original["metrics"])
            self.assertNotIn("stabilizationWindowSeconds", hpa["spec"]["behavior"]["scaleDown"])
        for row in self.plan["autoscalers"]:
            self.coordinator.mutate(row, "pause", False, restoring=True)
        self.coordinator.autoscaling_restored()
        for hpa in self.api.items("horizontalpodautoscalers"):
            self.assertEqual(hpa["spec"], self.api.original_hpa_specs[hpa["metadata"]["uid"]])
            self.assertNotIn("behavior", hpa["spec"])

    def test_native_paused_defaults_never_hide_metrics_policies_or_owner_drift(self):
        for row in self.plan["autoscalers"]:
            self.coordinator.mutate(row, "pause", True)
        row = self.plan["hpas"][0]
        hpa = self.api.get("horizontalpodautoscalers", row["namespace"], row["name"])
        original = copy.deepcopy(hpa)
        for drift in ("metric", "policy", "stabilization", "tolerance", "target", "owner"):
            hpa.clear()
            hpa.update(copy.deepcopy(original))
            if drift == "metric":
                hpa["spec"]["metrics"][0]["external"]["target"]["averageValue"] = "200"
            elif drift == "policy":
                hpa["spec"]["behavior"]["scaleUp"]["policies"][0]["value"] = 50
            elif drift == "stabilization":
                hpa["spec"]["behavior"]["scaleDown"]["stabilizationWindowSeconds"] = 300
            elif drift == "tolerance":
                hpa["spec"]["behavior"]["scaleUp"]["tolerance"] = "0.2"
            elif drift == "target":
                hpa["spec"]["scaleTargetRef"]["name"] = "foreign"
            else:
                hpa["metadata"]["ownerReferences"].append(
                    {"apiVersion": "apps/v1", "kind": "Deployment", "name": "foreign", "uid": "foreign"})
            with self.subTest(drift=drift), self.assertRaises(RuntimeError):
                self.coordinator.hpas_paused()

    def test_custom_native_hpa_behavior_is_preserved_without_broad_normalization(self):
        behavior = {"scaleUp": {"selectPolicy": "Min", "stabilizationWindowSeconds": 42, "tolerance": "0.2",
                                "policies": [{"type": "Percent", "value": 50, "periodSeconds": 60}]},
                    "scaleDown": {"selectPolicy": "Max", "stabilizationWindowSeconds": 300,
                                  "policies": [{"type": "Pods", "value": 1, "periodSeconds": 300}]}}
        policy, api, plan, approval, journal = fixture(hpa_behavior=behavior)
        coordinator = c.Coordinator(plan, approval, policy, journal, api)
        for row in plan["autoscalers"]:
            coordinator.mutate(row, "pause", True)
        coordinator.hpas_paused()
        expected = copy.deepcopy(behavior)
        for direction in expected.values():
            direction["selectPolicy"] = "Disabled"
        for hpa in api.items("horizontalpodautoscalers"):
            self.assertEqual(hpa["spec"]["behavior"], expected)
        for row in plan["autoscalers"]:
            coordinator.mutate(row, "pause", False, restoring=True)
        coordinator.autoscaling_restored()
        for hpa in api.items("horizontalpodautoscalers"):
            self.assertEqual(hpa["spec"], api.original_hpa_specs[hpa["metadata"]["uid"]])

    def test_schedules_jobs_and_gitops_are_actually_gated(self):
        with self.assertRaises(RuntimeError):
            self.coordinator.producers_gated()
        for row in self.plan["reconciliation"] + self.plan["schedules"]:
            self.coordinator.mutate(row, "suspend", True)
        self.coordinator.producers_gated()
        self.api.add("jobs", obj("Job", "media", "new-writer", {}))
        with self.assertRaisesRegex(RuntimeError, "Active/new Job"):
            self.coordinator.producers_gated()

    def test_cross_namespace_writer_is_captured_without_expanding_the_application_cohort(self):
        row = next(r for r in self.plan["schedules"] if (r["namespace"], r["name"]) == c.CROSS_PRODUCER)
        cron = self.api.get("cronjobs", *c.CROSS_PRODUCER)
        self.assertNotIn(row["namespace"], c.ALLOWED)
        self.assertEqual(row["uid"], cron["metadata"]["uid"])
        self.assertEqual(row["spec_sha256"], c.sha(c.canonical(cron["spec"])))
        self.assertEqual(row["original"], {"suspend_present": True, "suspend": False})
        self.assertEqual(len(self.plan["schedules"]), 5)
        self.assertNotIn("rollout restart", c.canonical(self.plan).decode())
        changed = copy.deepcopy(self.plan)
        changed["schedules"][-1]["name"] = "foreign-producer"
        with self.assertRaisesRegex(RuntimeError, "scheduled producer scope"):
            c.check_plan(changed, c.sha(c.canonical(changed)), self.policy, self.api)

    def test_active_cross_namespace_job_blocks_all_app_pauses_and_recovery_until_native_completion(self):
        self.successful_hooks()
        job, pod = self.mount_handler_job()
        deadline = self.journal.read()["deadline_epoch"]
        with patch.object(c.m, "health", return_value={}), patch.object(c.m.urllib.request, "urlopen") as url, \
                patch.object(c.time, "sleep"):
            url.return_value.__enter__.return_value.status = 200
            with self.assertRaisesRegex(RuntimeError, "Job has not finished"):
                self.coordinator.execute()
            self.assertTrue(self.api.get("cronjobs", *c.CROSS_PRODUCER)["spec"]["suspend"])
            self.assertTrue(self.api.items("configmap"))
            self.assertFalse(any(e[0] == "patch" and any(o["path"] == "/spec/replicas" for o in e[2])
                                 for e in self.api.events))
            self.assertIn(job, self.api.items("jobs"))
            self.assertIn(pod, self.api.items("pods"))
            self.finish_mount_handler_job(job, pod)
            self.coordinator.recover()
        self.assertFalse(self.api.get("cronjobs", *c.CROSS_PRODUCER)["spec"]["suspend"])
        self.assertEqual(self.journal.read()["deadline_epoch"], deadline)
        self.assertEqual(self.journal.read()["phase"], "recovered-after-failure")
        self.assertFalse(any(e[0] == "kube" and "delete" in e[1] and ("jobs" in e[1] or "pods" in e[1])
                             for e in self.api.events))

    def test_cross_namespace_job_conditions_do_not_hide_running_pods_or_stale_cron_active_references(self):
        self.freeze_mount_handler()
        job, pod = self.mount_handler_job()
        job["status"] = {"active": 0, "conditions": [{"type": "FailureTarget", "status": "True"}]}
        with self.assertRaisesRegex(RuntimeError, "Job has not finished"):
            self.coordinator.mount_handler_gated()
        job["status"] = {"active": 0, "conditions": [{"type": "Complete", "status": "True"}]}
        with self.assertRaisesRegex(RuntimeError, "Pod has not finished"):
            self.coordinator.mount_handler_gated()
        self.finish_mount_handler_job(job, pod)
        cron = self.api.get("cronjobs", *c.CROSS_PRODUCER)
        cron["status"]["active"] = [{"apiVersion": "batch/v1", "kind": "Job", "namespace": "kube-system",
                                     "name": job["metadata"]["name"], "uid": job["metadata"]["uid"]}]
        with self.assertRaisesRegex(RuntimeError, "not suspended/idle"):
            self.coordinator.mount_handler_gated()
        cron["status"]["active"] = []
        self.coordinator.mount_handler_gated()

    def test_cross_namespace_foreign_jobs_and_pod_owner_uid_drift_are_not_exempt(self):
        self.freeze_mount_handler()
        job, pod = self.mount_handler_job()
        self.finish_mount_handler_job(job, pod)
        owner = job["metadata"]["ownerReferences"][0]
        original = owner["uid"]
        owner["uid"] = "replaced-cronjob"
        with self.assertRaisesRegex(RuntimeError, "Foreign/unbound.*Job"):
            self.coordinator.mount_handler_gated()
        owner["uid"] = original
        pod["metadata"]["ownerReferences"][0]["uid"] = "foreign-job"
        with self.assertRaisesRegex(RuntimeError, "Pod/Job identity"):
            self.coordinator.mount_handler_gated()
        pod["metadata"]["ownerReferences"][0].update(uid=job["metadata"]["uid"], name="foreign-job")
        pod["spec"]["serviceAccountName"] = "default"
        with self.assertRaisesRegex(RuntimeError, "Pod/Job identity"):
            self.coordinator.mount_handler_gated()

    def test_cross_namespace_cron_identity_reconciler_and_foreign_producer_drift_fail_closed(self):
        row = self.freeze_mount_handler()
        cron = self.api.get("cronjobs", *c.CROSS_PRODUCER)
        original = copy.deepcopy(cron)
        for drift in ("uid", "service-account", "reconciler", "template"):
            cron.clear()
            cron.update(copy.deepcopy(original))
            if drift == "uid":
                cron["metadata"]["uid"] = "replaced-cronjob"
            elif drift == "service-account":
                cron["spec"]["jobTemplate"]["spec"]["template"]["spec"]["serviceAccountName"] = "foreign"
            elif drift == "reconciler":
                cron["metadata"]["labels"]["kustomize.toolkit.fluxcd.io/name"] = "foreign"
            else:
                cron["spec"]["jobTemplate"]["spec"]["template"]["spec"]["containers"][0]["command"] = ["foreign"]
            count = len(self.api.events)
            with self.subTest(drift=drift), self.assertRaises(RuntimeError):
                self.coordinator.mutate(row, "suspend", row["original"]["suspend"], restoring=True)
            self.assertEqual(len(self.api.events), count)
        cron.clear()
        cron.update(original)
        foreign = copy.deepcopy(cron)
        foreign["metadata"].update(name="foreign-producer", uid="foreign-producer")
        self.api.add("cronjobs", foreign)
        with self.assertRaisesRegex(RuntimeError, "Foreign CronJob"):
            c.mount_handler_scope(self.api)
        with self.assertRaisesRegex(RuntimeError, "Foreign CronJob"):
            self.coordinator.mount_handler_gated()
        count = len(self.api.events)
        with self.assertRaisesRegex(RuntimeError, "Foreign CronJob"):
            self.coordinator.mutate(row, "suspend", row["original"]["suspend"], restoring=True)
        self.assertEqual(len(self.api.events), count)

    def test_cross_namespace_original_suspend_presence_survives_ambiguous_restoration_and_retry(self):
        for original in (False, True, None):
            self.setUp_fixture_for_reboot_case(handler_suspend=original)
            row = self.freeze_mount_handler()
            deadline = self.journal.read()["deadline_epoch"]
            self.api.fail = c.key(row)
            with patch.object(c.m, "health", return_value={}), patch.object(c.m.urllib.request, "urlopen") as url, \
                    patch.object(c.time, "sleep"):
                url.return_value.__enter__.return_value.status = 200
                with self.assertRaisesRegex(RuntimeError, "Ambiguous response"):
                    self.recover_with_live_worker()
                self.assertTrue(self.journal.read()["producer_warmup_complete"])
                self.assertTrue(self.api.items("configmap"))
                self.recover_with_live_worker()
            restored = self.api.get("cronjobs", *c.CROSS_PRODUCER)
            with self.subTest(original=original):
                self.assertEqual("suspend" in restored["spec"], original is not None)
                if original is not None:
                    self.assertEqual(restored["spec"]["suspend"], original)
                self.assertEqual(self.journal.read()["deadline_epoch"], deadline)
                c.assert_binding(restored, row)

    def test_failed_warmup_keeps_cross_namespace_producer_suspended_until_full_health(self):
        self.successful_hooks()
        deadline = self.journal.read()["deadline_epoch"]
        with patch.object(self.coordinator, "full_health", side_effect=RuntimeError("Full HTTPS health failed")), \
                self.assertRaisesRegex(RuntimeError, "HTTPS"):
            self.coordinator.execute()
        self.assertTrue(self.api.get("cronjobs", *c.CROSS_PRODUCER)["spec"]["suspend"])
        self.assertFalse(self.journal.read().get("producer_warmup_complete", False))
        self.assertTrue(self.api.items("configmap"))
        with patch.object(c.m, "health", return_value={}), patch.object(c.m.urllib.request, "urlopen") as url, \
                patch.object(c.time, "sleep"):
            url.return_value.__enter__.return_value.status = 200
            self.coordinator.recover()
        self.assertFalse(self.api.get("cronjobs", *c.CROSS_PRODUCER)["spec"]["suspend"])
        self.assertEqual(self.journal.read()["deadline_epoch"], deadline)
        self.coordinator.original_state()

    def test_unrelated_kube_system_job_is_not_a_migration_or_readiness_exception(self):
        job, pod = self.mount_handler_job()
        job["metadata"]["ownerReferences"][0].update(name="unrelated-cronjob", uid="unrelated-cronjob")
        job["spec"]["template"]["spec"]["serviceAccountName"] = "default"
        pod["spec"].update(serviceAccountName="default", nodeName=c.WORKER)
        with self.assertRaisesRegex(RuntimeError, "non-migratable controller"):
            c.migration_snapshot(self.plan, self.policy, self.api)

    def make_pending(self):
        for row in self.plan["ha"]:
            pod = next(p for p in self.api.items("pods") if p["metadata"]["uid"] == row["old_worker_uid"])
            pod["metadata"]["uid"] += "-replacement"
            pod["spec"].pop("nodeName")
            pod["status"] = {"phase": "Pending", "conditions": [
                {"type": "PodScheduled", "status": "False", "reason": "Unschedulable"}]}

    def test_only_four_exact_pending_replacements_with_ready_peer_are_allowed(self):
        self.make_pending()
        self.assertEqual(len(c.expected_pending(self.plan["ha"], self.api.items("pods"), self.api.rs_map(), self.api.get)), 4)
        peer = next(p for p in self.api.items("pods") if p["metadata"]["uid"] == self.plan["ha"][0]["peer_uid"])
        peer["status"]["conditions"] = []
        with self.assertRaisesRegex(RuntimeError, "Ready worker1 peer"):
            c.expected_pending(self.plan["ha"], self.api.items("pods"), self.api.rs_map(), self.api.get)

    def test_pending_owner_template_selector_and_spec_drift_are_not_waived(self):
        self.make_pending()
        row = self.plan["ha"][0]
        pod = next(p for p in self.api.items("pods") if p["metadata"]["uid"] == row["old_worker_uid"] + "-replacement")
        original = copy.deepcopy(pod)
        for change in ("image", "owner", "selector"):
            pod.clear()
            pod.update(copy.deepcopy(original))
            if change == "image":
                pod["spec"]["containers"][0]["image"] = "unreviewed"
            elif change == "owner":
                pod["metadata"]["ownerReferences"][0]["uid"] = "unreviewed"
            else:
                pod["metadata"]["labels"]["app"] = "unreviewed"
            with self.subTest(change=change), self.assertRaises(RuntimeError):
                c.expected_pending(self.plan["ha"], self.api.items("pods"), self.api.rs_map(), self.api.get)

    def test_nfs_mount_review_never_becomes_generic_hostpath_waiver(self):
        d = self.api.get("deployments", "csi-driver-nfs", "csi-nfs-controller")
        pod = next(p for p in self.api.items("pods") if p["metadata"]["uid"] == self.plan["ha"][0]["old_worker_uid"])
        pod["spec"]["volumes"][-1]["hostPath"]["path"] = "/arbitrary-data"
        with self.assertRaises(RuntimeError):
            c.ha_binding(d, self.api.items("pods"), self.api.rs_map())

    def test_native_pdb_can_only_be_released_by_native_controller(self):
        self.api.get("nodes", name=c.WORKER)["spec"]["unschedulable"] = True
        with self.assertRaisesRegex(RuntimeError, "NOT natively released"):
            c.native_gate(self.plan, self.policy, self.api, released=True)
        with self.assertRaisesRegex(RuntimeError, "prohibited"):
            c.API().patch(self.plan["native"][c.WORKER]["pdb"],
                          self.api.original_im_pdb, [{"op": "remove", "path": "/spec/minAvailable"}])
        self.assertFalse(any(e[0] == "patch" and c.IM_NAMES[c.WORKER] in e[1] for e in self.api.events))

    def test_missing_last_copy_peer_or_peer_im_blocks_native_release(self):
        self.api.kube("cordon", c.WORKER)
        replica = next(r for r in self.api.items("replicas.longhorn.io") if r["spec"].get("nodeID") == c.PEER
                       and r["spec"]["volumeName"] == "regular-fixture")
        replica["spec"]["healthyAt"] = ""
        with self.assertRaisesRegex(RuntimeError, "missing native"):
            c.native_gate(self.plan, self.policy, self.api, released=True)
        replica["spec"]["healthyAt"] = "2026-11-20T00:00:00Z"
        im = self.api.get("instancemanagers.longhorn.io", "storage", c.IM_NAMES[c.PEER])
        im["status"]["currentState"] = "stopped"
        with self.assertRaisesRegex(RuntimeError, "running peer"):
            c.native_gate(self.plan, self.policy, self.api, released=True)

    def test_native_stopped_peer_uses_default_image_selector_not_cleared_status(self):
        self.api.kube("cordon", c.WORKER)
        for replica in self.api.items("replicas.longhorn.io"):
            self.assertEqual(replica["status"]["instanceManagerName"], "")
        c.native_gate(self.plan, self.policy, self.api, released=True)
        im = self.api.get("instancemanagers.longhorn.io", "storage", c.IM_NAMES[c.PEER])
        im["metadata"]["labels"]["longhorn.io/instance-manager-image"] = "imi-foreign"
        with self.assertRaisesRegex(RuntimeError, "selector resolution"):
            c.native_gate(self.plan, self.policy, self.api, released=True)

    def test_native_stopped_peer_ambiguity_source_policy_and_transition_fail_closed(self):
        self.api.kube("cordon", c.WORKER)
        peer = self.api.get("instancemanagers.longhorn.io", "storage", c.IM_NAMES[c.PEER])
        replica = next(r for r in self.api.items("replicas.longhorn.io")
                       if r["spec"].get("nodeID") == c.PEER and r["spec"]["volumeName"] == "regular-fixture")
        other = copy.deepcopy(peer)
        other["metadata"].update(name="other-native-im", uid="other-native-im")
        self.api.add("instancemanagers.longhorn.io", other)
        with self.assertRaisesRegex(RuntimeError, "ambiguous"):
            c.native_gate(self.plan, self.policy, self.api, released=True)
        self.api.resources["instancemanagers.longhorn.io"].remove(other)
        replica["status"]["starting"] = True
        with self.assertRaisesRegex(RuntimeError, "stably stopped"):
            c.native_gate(self.plan, self.policy, self.api, released=True)
        replica["status"]["starting"] = False
        setting = self.api.get("settings.longhorn.io", "storage", "default-instance-manager-image")
        setting["value"] += "-foreign"
        with self.assertRaisesRegex(RuntimeError, "default IM image drift"):
            c.native_gate(self.plan, self.policy, self.api, released=True)
        setting["value"] = IM_IMAGE
        peer["spec"]["image"] += "-foreign"
        with self.assertRaisesRegex(RuntimeError, "CR/image/node binding drift"):
            c.native_gate(self.plan, self.policy, self.api, released=True)

    def warm_regular_fixture(self):
        volume = self.api.get("volumes.longhorn.io", "storage", "regular-fixture")
        volume["spec"]["nodeID"] = c.PEER
        volume["status"].update(state="attached", robustness="healthy", currentNodeID=c.PEER)
        engine = self.api.get("engines.longhorn.io", "storage", "regular-fixture-engine")
        engine["spec"]["desireState"] = engine["status"]["currentState"] = "running"
        for replica in self.api.items("replicas.longhorn.io"):
            if replica["spec"]["volumeName"] == "regular-fixture":
                replica["spec"].update(lastHealthyAt="2026-11-24T04:55:00Z", desireState="running")
                replica["status"].update(currentState="running", instanceManagerName=c.IM_NAMES[replica["spec"]["nodeID"]])
        return volume, engine

    def test_native_rw_transition_timestamp_allows_warmup_and_guard_retry(self):
        identities = copy.deepcopy(self.plan["storage"])
        self.warm_regular_fixture()
        self.assertEqual(c.storage_snapshot(self.policy, self.api), identities)
        self.assertEqual(c.cold_storage(self.plan, self.policy, self.api)["attached_two_rw"], 1)
        self.coordinator.request_recovery("interrupted-after-healthy-backend-reattachment")
        with patch.object(c.m, "health", return_value={}), patch.object(c.m.urllib.request, "urlopen") as url, \
                patch.object(c.time, "sleep"):
            url.return_value.__enter__.return_value.status = 200
            self.recover_with_live_worker()
            self.recover_with_live_worker()
        self.assertEqual(c.storage_snapshot(self.policy, self.api), identities)
        self.assertEqual(self.journal.read()["phase"], "recovered-after-failure")

    def test_native_rw_timestamp_exclusion_preserves_volume_replica_and_directory_identity(self):
        volume, _ = self.warm_regular_fixture()
        replica = next(r for r in self.api.items("replicas.longhorn.io") if r["spec"]["volumeName"] == "regular-fixture")
        for target, field, changed in ((volume["metadata"], "uid", "other-volume"),
                                       (replica["metadata"], "uid", "other-replica"),
                                       (replica["spec"], "nodeID", c.SERVER),
                                       (replica["spec"], "volumeName", "other-volume"),
                                       (replica["spec"], "dataDirectoryName", "other-directory"),
                                       (volume["spec"], "numberOfReplicas", 1)):
            original = target[field]
            target[field] = changed
            with self.subTest(field=field), self.assertRaisesRegex(RuntimeError, "policy drift"):
                c.cold_storage(self.plan, self.policy, self.api)
            target[field] = original

    def test_native_rw_warmup_still_rejects_current_failed_degraded_and_non_rw_copies(self):
        volume, engine = self.warm_regular_fixture()
        replica = next(r for r in self.api.items("replicas.longhorn.io") if r["spec"]["volumeName"] == "regular-fixture")
        for target, field, changed in ((replica["spec"], "failedAt", "2026-11-24T04:56:00Z"),
                                       (replica["spec"], "healthyAt", ""),
                                       (volume["status"], "robustness", "degraded"),
                                       (engine["status"]["replicaModeMap"], replica["metadata"]["name"], "WO"),
                                       (replica["status"], "currentState", "error")):
            original = target[field]
            target[field] = changed
            self.assertEqual(c.storage_snapshot(self.policy, self.api), self.plan["storage"])
            with self.subTest(field=field), self.assertRaisesRegex(RuntimeError, "two clean copies|healthy engine"):
                self.coordinator.recover()
            self.assertTrue(self.api.items("configmap"))
            self.assertFalse(any(e[0] == "patch" for e in self.api.events))
            target[field] = original

    def test_tickets_degraded_copies_and_data_directory_drift_block_warmup(self):
        for case in ("ticket", "copy", "directory"):
            policy, api, plan, _, _ = fixture()
            with patch.object(c.m, "get", side_effect=api.get):
                if case == "ticket":
                    api.get("volumeattachments.longhorn.io", "storage", "regular-fixture")["spec"]["attachmentTickets"] = {"writer": {}}
                else:
                    replica = next(r for r in api.items("replicas.longhorn.io") if r["spec"]["volumeName"] == "regular-fixture")
                    if case == "copy":
                        replica["spec"]["failedAt"] = "now"
                    else:
                        replica["spec"]["dataDirectoryName"] = "other"
                with self.subTest(case=case), self.assertRaises(RuntimeError):
                    c.cold_storage(plan, policy, api, detached=True)

    def test_original_zero_suspend_and_annotation_presence_are_restored(self):
        self.freeze_mount_handler()
        row = next(r for r in self.plan["controllers"] if r["name"] == "paperless-paperless-ngx")
        # Stop operators first; the API simulation refuses a naive scale-zero.
        for operator in [r for r in self.plan["controllers"] if (r["namespace"], r["name"]) in c.OPERATORS]:
            self.coordinator.mutate(operator, "replicas", 0)
        self.coordinator.mutate(row, "replicas", 0)
        self.coordinator.mutate(row, "replicas", row["original"]["replicas"], restoring=True)
        self.coordinator.replicas_ready(row)
        self.assertEqual(self.api.get("deployments", "paperless", row["name"])["spec"]["replicas"], 0)
        recon = next(r for r in self.plan["reconciliation"] if not r["original"]["suspend_present"])
        self.coordinator.mutate(recon, "suspend", True)
        self.coordinator.mutate(recon, "suspend", False, restoring=True)
        self.assertNotIn("suspend", self.api.get(c.RESOURCES[recon["kind"]], recon["namespace"], recon["name"])["spec"])

    def test_partial_pause_ambiguous_patch_recovers_original_deadline(self):
        row = self.plan["reconciliation"][0]
        self.api.fail = c.key(row)
        with self.assertRaises(RuntimeError):
            self.coordinator.mutate(row, "suspend", True)
        self.assertIn(c.key(row), self.journal.read()["mutations"])
        old_deadline = self.journal.read()["deadline_epoch"]
        with patch.object(c.m, "health", return_value={}), patch.object(c.m.urllib.request, "urlopen") as url, patch.object(c.time, "sleep"):
            url.return_value.__enter__.return_value.status = 200
            self.recover_with_live_worker()
        self.assertEqual(self.journal.read()["deadline_epoch"], old_deadline)
        self.assertEqual(self.journal.read()["phase"], "recovered-after-failure")
        c.assert_binding(self.api.get(c.RESOURCES[row["kind"]], row["namespace"], row["name"]), row)

    def test_reserve_or_clock_rollback_cannot_restart_disruption_or_extend_deadline(self):
        original = self.journal.read()
        self.coordinator.now = lambda: original["deadline_epoch"] - 7200
        with self.assertRaisesRegex(RuntimeError, "reserve"):
            self.coordinator.mutate(self.plan["schedules"][0], "suspend", True)
        self.coordinator.now = lambda: original["started_epoch"] - 1
        with self.assertRaisesRegex(RuntimeError, "Clock rollback"):
            self.coordinator.mutate(self.plan["schedules"][0], "suspend", True)
        self.assertEqual(self.journal.read()["deadline_epoch"], original["deadline_epoch"])

    def successful_hooks(self, reboot=True):
        self.coordinator.recover = self.recover_with_live_worker
        original_authorize = self.coordinator.authorize_host
        def authorize():
            original_authorize()
            self.record_package_receipt()
            if not reboot:
                self.journal.transaction(lambda s: s.update(host_packages_complete_epoch=time.time() - 121))
        self.coordinator.authorize_host = authorize
        original_host_completed = self.coordinator.host_completed
        def host_completed():
            if reboot:
                self.api.host_boot = "rebooted-worker2"
            return original_host_completed()
        self.coordinator.host_completed = host_completed
        def drain():
            self.api.resources["pods"] = [p for p in self.api.items("pods")
                                         if p["metadata"]["name"] != c.IM_NAMES[c.WORKER]]
            self.make_pending()
            for pod in self.api.items("pods"):
                if pod["spec"].get("nodeName") == c.WORKER and c.m.evictable(pod):
                    pod["spec"]["nodeName"] = c.PEER
                    pod["metadata"]["uid"] += "-normally-evicted"
        self.coordinator.drain = drain

    def test_successful_source_coordinator_restores_only_worker2_and_all_original_state(self):
        self.successful_hooks()
        with patch.object(c.m, "health", return_value={}), patch.object(c.m.urllib.request, "urlopen") as url, patch.object(c.time, "sleep"):
            url.return_value.__enter__.return_value.status = 200
            self.coordinator.execute()
        state = self.journal.read()
        self.assertTrue(state["completed"])
        self.assertEqual(state["phase"], "accepted")
        self.assertEqual(self.api.get("deployments", "paperless", "paperless-paperless-ngx")["spec"]["replicas"], 0)
        for row in self.plan["controllers"] + self.plan["reconciliation"] + self.plan["schedules"] + self.plan["hpas"] + self.plan["temporary_pdbs"]:
            c.assert_binding(self.api.get(c.RESOURCES[row["kind"]], row["namespace"], row["name"]), row)
        cordons = [e for e in self.api.events if e[0] == "kube" and e[1][0] in ("cordon", "uncordon")]
        self.assertEqual(cordons, [("kube", ("cordon", c.WORKER)), ("kube", ("uncordon", c.WORKER))])
        self.assertFalse(any(e[0] == "patch" and "storage/" in e[1] for e in self.api.events))
        self.assertEqual(len([e for e in self.api.events if e[0] == "kube" and c.m.LOCK in e[1] and "delete" in e[1]]), 1)

    def test_failed_drain_and_interruption_restore_instead_of_resuming_upgrade(self):
        for error in (RuntimeError("normal drain failed"), KeyboardInterrupt()):
            policy, api, plan, approval, journal = fixture()
            coordinator = c.Coordinator(plan, approval, policy, journal, api)
            coordinator.wait = lambda fn, **kwargs: fn()
            coordinator.recover = lambda success=False: self.recover_with_live_worker(success, coordinator, api)
            coordinator.drain = lambda: (_ for _ in ()).throw(error)
            deadline = journal.read()["deadline_epoch"]
            with patch.object(c.m, "get", side_effect=api.get), patch.object(c.m, "health", return_value={}), \
                    patch.object(c.m.urllib.request, "urlopen") as url, patch.object(c.time, "sleep"), self.assertRaises(type(error)):
                url.return_value.__enter__.return_value.status = 200
                coordinator.execute()
            self.assertEqual(journal.read()["phase"], "recovered-after-failure")
            self.assertEqual(journal.read()["deadline_epoch"], deadline)
            self.assertFalse(journal.read()["host_started"])

    def test_unsafe_storage_failure_keeps_lock_and_never_warms(self):
        replica = next(r for r in self.api.items("replicas.longhorn.io") if r["spec"]["volumeName"] == "regular-fixture")
        replica["spec"]["failedAt"] = "failed"
        with self.assertRaises(RuntimeError):
            self.coordinator.recover()
        self.assertFalse(self.journal.read()["completed"])
        self.assertTrue(self.api.items("configmap"))
        self.assertFalse(any(e[0] == "patch" for e in self.api.events))

    def test_host_failure_and_busy_package_transaction_do_not_force_reboot_or_warm(self):
        self.journal.state.update(host_started=True, host_started_epoch=time.time())
        with patch.object(self.api, "host_idle", side_effect=RuntimeError("APT still busy")), self.assertRaises(RuntimeError):
            self.recover_with_live_worker()
        self.assertTrue(self.api.items("configmap"))
        self.assertFalse(self.journal.read()["completed"])
        self.assertFalse(any(e[0] == "patch" for e in self.api.events))

    def test_timeout_restores_state_but_cannot_release_lock_after_original_deadline(self):
        original = self.journal.read()
        self.coordinator.now = lambda: original["deadline_epoch"] + 1
        with patch.object(c.m, "health", return_value={}), patch.object(c.m.urllib.request, "urlopen") as url, \
                patch.object(c.time, "sleep"), self.assertRaisesRegex(RuntimeError, "exceeded 12h"):
            url.return_value.__enter__.return_value.status = 200
            self.recover_with_live_worker()
        self.assertEqual(self.journal.read()["deadline_epoch"], original["deadline_epoch"])
        self.assertTrue(self.api.items("configmap"))

    def test_deferred_reboot_checks_reserve_at_execution_not_just_before_apt(self):
        payload = {"operation": "reboot", "name": c.WORKER, "boot_id": "old", "deadline_epoch": 10000,
                   "recovery_reserve_seconds": 7200}
        with patch.object(h.os, "geteuid", return_value=0, create=True), patch.object(h.socket, "gethostname", return_value=c.WORKER), \
                patch.object(h.Path, "read_text", return_value="old"), patch.object(h.time, "time", return_value=3000), \
                patch.object(h, "command") as command, self.assertRaisesRegex(RuntimeError, "reserve"):
            h.main(payload)
        command.assert_not_called()
        payload.update(cold_run_id="1" * 32, run_id="2" * 32, deadline_epoch=50000)
        with patch.object(h.time, "time", return_value=3000), patch.object(h, "cold_peer_status", return_value={
            "mode": "recovering", "phase": "awaiting-host", "host_started": True,
            "host_step_id": "2" * 32, "deadline_epoch": 50000}), self.assertRaisesRegex(RuntimeError, "revoked"):
            h.reserve(payload)

    def authorize_host_slot(self):
        self.journal.transaction(lambda s: s.update(phase="awaiting-host", host_started=True,
                                                   host_started_epoch=time.time(), host_reboot_authorized=True))
        state = self.journal.read()
        return {"operation": "os", "run_id": state["host_step_id"], "cold_run_id": state["run_id"],
                "name": c.WORKER, "boot_id": "old-worker2", "deadline_epoch": state["deadline_epoch"],
                "recovery_reserve_seconds": 7200}

    def record_package_receipt(self, payload=None):
        payload = payload or (self.authorize_host_slot() if not self.journal.read()["host_started"] else {})
        state = self.journal.read()
        receipt = {"run_id": state["host_step_id"], "name": c.WORKER, "operation": "os",
                   "old_boot_id": "old-worker2", "expected_kernel": self.policy["kernel"], "completed_epoch": time.time(),
                   "headless": True, "package_removals": False,
                   "contract_sha256": state["worker_contract_sha256"]}
        self.coordinator.ingest_worker_receipt(receipt)
        self.api.worker.transaction(lambda s: s.update(phase="packages-complete", receipt=receipt))
        self.api.worker_native.update(ActiveState="inactive", MainPID="0", ControlGroup="")
        return payload

    def test_real_package_receipt_before_timer_keeps_authorization_until_verified_new_boot(self):
        payload = self.authorize_host_slot()
        deadline = self.journal.read()["deadline_epoch"]
        with patch.object(h, "cold_peer_status", side_effect=lambda _: self.journal.read()), patch.object(h, "command") as command:
            h.schedule_reboot(payload)
            scheduled = command.call_args.args[0]
            self.assertIn("--on-active=10s", scheduled)
            self.assertIn("--timer-property=RemainAfterElapse=no", scheduled)
            deferred = json.loads(base64.b64decode(scheduled[-1]))
            self.record_package_receipt(payload)
            self.assertTrue(self.journal.read()["host_packages_complete"])
            self.assertFalse(self.journal.read()["host_complete"])
            with patch.object(self.api, "host_idle", side_effect=RuntimeError("Reboot timer active")), \
                    self.assertRaisesRegex(RuntimeError, "timer active"):
                self.coordinator.host_completed()
            self.assertEqual(self.journal.read()["mode"], "disrupting")
            for wrong in ({"name": c.PEER}, {"operation": "rke2"}):
                with self.subTest(wrong=wrong), self.assertRaisesRegex(RuntimeError, "worker2 OS/reboot ONLY"):
                    h.reserve({**deferred, **wrong})
            command.reset_mock()
            with patch.object(h.os, "geteuid", return_value=0, create=True), \
                    patch.object(h.socket, "gethostname", return_value=c.WORKER), \
                    patch.object(h.Path, "read_text", return_value="old-worker2"), \
                    patch.object(h.time, "time", return_value=self.journal.read()["host_packages_complete_epoch"] + 10):
                h.main(deferred)
            command.assert_called_once_with(["systemctl", "reboot"])
        self.api.host_boot = "rebooted-worker2"
        self.coordinator.host_completed()
        self.assertTrue(self.journal.read()["host_complete"])
        self.assertFalse(self.journal.read()["host_reboot_authorized"])
        self.assertEqual(self.journal.read()["deadline_epoch"], deadline)

    def test_pending_reboot_is_revoked_on_reserve_or_executor_loss_with_original_state_recovery(self):
        for cause in ("reserve", "executor-loss"):
            self.setUp_fixture_for_reboot_case()
            row = self.plan["schedules"][0]
            self.coordinator.mutate(row, "suspend", True)
            payload = self.record_package_receipt()
            deadline = self.journal.read()["deadline_epoch"]
            now = deadline - 7200 if cause == "reserve" else time.time()
            self.coordinator.now = lambda: now
            with patch.object(c, "load_runtime", return_value=self.coordinator), \
                    patch.object(c.m, "run", return_value="active" if cause == "reserve" else "failed"), \
                    patch.object(c.time, "time", return_value=now), patch.object(c.time, "sleep"), \
                    patch.object(c.m, "health", return_value={}), patch.object(c.m.urllib.request, "urlopen") as url:
                url.return_value.__enter__.return_value.status = 200
                c.guard("not-a-host-directory")
                with patch.object(h, "cold_peer_status", side_effect=lambda _: self.journal.read()), \
                        patch.object(h, "command") as command, self.assertRaises(RuntimeError):
                    h.schedule_reboot(payload)
                command.assert_not_called()
            state = self.journal.read()
            self.assertEqual(state["deadline_epoch"], deadline)
            self.assertTrue(state["host_slot_cancelled"])
            self.assertFalse(state["host_reboot_authorized"])
            self.assertFalse(state["host_complete"])
            self.assertEqual(state["host_reboot_outcome"], "not-rebooted")
            self.assertEqual(state["phase"], "recovered-after-failure")
            self.coordinator.original_state()

    def setUp_fixture_for_reboot_case(self, handler_suspend=False):
        self.policy, self.api, self.plan, self.approval, self.journal = fixture(handler_suspend=handler_suspend)
        self.get_patch.stop()
        self.get_patch = patch.object(c.m, "get", side_effect=self.api.get)
        self.get_patch.start()
        self.addCleanup(self.get_patch.stop)
        self.coordinator = c.Coordinator(self.plan, self.approval, self.policy, self.journal, self.api)
        self.coordinator.wait = lambda fn, **kwargs: fn()

    def test_idle_refused_reboot_is_recovery_not_success_and_does_not_strand_apps(self):
        self.successful_hooks(reboot=False)
        deadline = self.journal.read()["deadline_epoch"]
        with patch.object(c.m, "health", return_value={}), patch.object(c.m.urllib.request, "urlopen") as url, \
                patch.object(c.time, "sleep"), self.assertRaisesRegex(RuntimeError, "not reboot completion"):
            url.return_value.__enter__.return_value.status = 200
            self.coordinator.execute()
        self.assertEqual(self.journal.read()["phase"], "recovered-after-failure")
        self.assertEqual(self.journal.read()["deadline_epoch"], deadline)
        self.assertFalse(self.journal.read()["host_reboot_authorized"])
        self.assertFalse(self.journal.read()["host_complete"])
        self.assertEqual(self.api.host_boot, "old-worker2")
        self.coordinator.original_state()
        self.assertTrue(any(e[0] == "patch" and any(o.get("path") == "/spec/replicas" and o.get("value") == 0
                                                  for o in e[2]) for e in self.api.events))

    def test_host_package_receipt_cannot_be_promoted_to_maintenance_success_without_reboot(self):
        self.record_package_receipt()
        with self.assertRaisesRegex(RuntimeError, "success requires actual"):
            self.coordinator.recover(success=True)
        self.assertTrue(self.api.items("configmap"))
        self.assertFalse(self.journal.read()["completed"])

    def test_native_shutdown_job_blocks_warmup_even_after_reboot_service_becomes_idle(self):
        states = [{"ActiveState": "inactive", "LoadState": "loaded", "Job": "0"} for _ in range(6)]
        protocol = MagicMock()
        protocol.request.return_value = {"states": states, "cgroups_empty": True}
        with patch.object(c, "peer", return_value=protocol):
            self.assertTrue(c.API().host_idle(self.policy, self.journal.read()["host_step_id"]))
        for index in range(5):
            states[index]["Job"] = "178"
            with self.subTest(unit=index), patch.object(c, "peer", return_value=protocol), \
                    self.assertRaisesRegex(RuntimeError, "active/queued|shutdown/reboot job is pending"):
                c.API().host_idle(self.policy, self.journal.read()["host_step_id"])
            states[index]["Job"] = "0"

    def test_native_transient_units_disappear_at_reboot_but_boot_proof_remains_mandatory(self):
        self.record_package_receipt()
        states = [{"ActiveState": "inactive", "LoadState": "not-found", "Job": "0"} for _ in range(3)]
        states += [{"ActiveState": "inactive", "LoadState": "loaded", "Job": "0"} for _ in range(3)]
        protocol = MagicMock()
        protocol.request.return_value = {"states": states, "cgroups_empty": True}
        with patch.object(c, "peer", return_value=protocol):
            self.assertFalse(c.API().host_idle(self.policy, self.journal.read()["host_step_id"]))
        self.api.host_boot = "rebooted-worker2"
        self.assertFalse(self.api.host_idle(self.policy, self.journal.read()["host_step_id"]))
        self.coordinator.host_completed()
        self.assertTrue(self.journal.read()["host_complete"])
        with patch.object(self.api, "host_audit", return_value={
                "boot_id": "rebooted-worker2", "kernel": "unapproved", "default_target": "multi-user.target"}), \
                self.assertRaisesRegex(RuntimeError, "approved OS/kernel"):
            self.coordinator.recover()
        self.assertTrue(self.api.items("configmap"))
        self.assertFalse(any(e[0] == "patch" for e in self.api.events))

    def test_guard_recovers_partial_pause_on_executor_loss_or_reserve_without_resetting_clock(self):
        for cause in ("executor-loss", "reserve"):
            policy, api, plan, approval, journal = fixture()
            coordinator = c.Coordinator(plan, approval, policy, journal, api)
            coordinator.wait = lambda fn, **kwargs: fn()
            coordinator.recover = lambda success=False: self.recover_with_live_worker(success, coordinator, api)
            deadline = journal.read()["deadline_epoch"]
            with patch.object(c.m, "get", side_effect=api.get):
                coordinator.mutate(plan["reconciliation"][0], "suspend", True)
                now = deadline - 7200 if cause == "reserve" else time.time()
                coordinator.now = lambda: now
                with patch.object(c, "load_runtime", return_value=coordinator), \
                        patch.object(c.m, "run", return_value="active" if cause == "reserve" else "failed"), \
                        patch.object(c.time, "time", return_value=now), patch.object(c.time, "sleep"), \
                        patch.object(c.m, "health", return_value={}), patch.object(c.m.urllib.request, "urlopen") as url:
                    url.return_value.__enter__.return_value.status = 200
                    c.guard("not-a-real-host-directory")
            self.assertEqual(journal.read()["phase"], "recovered-after-failure")
            self.assertEqual(journal.read()["deadline_epoch"], deadline)
            self.assertFalse(journal.read()["host_started"])

    def test_native_engine_frontend_presence_prevents_release(self):
        self.api.kube("cordon", c.WORKER)
        im = self.api.get("instancemanagers.longhorn.io", "storage", c.IM_NAMES[c.WORKER])
        im["status"]["instanceEngineFrontends"] = {"frontend": {}}
        with self.assertRaisesRegex(RuntimeError, "engine/frontend"):
            c.native_gate(self.plan, self.policy, self.api, released=True)

    def test_interceptor_native_scaler_is_included_and_foreign_parent_is_not_ignored(self):
        ancillary = [r for r in self.plan["autoscalers"] if r["namespace"] == "keda"]
        self.assertEqual(len(ancillary), 1)
        self.assertEqual(ancillary[0]["target"]["name"], "keda-add-ons-http-interceptor")
        self.assertEqual(len(self.plan["hpas"]), 6)
        for row in self.plan["autoscalers"]:
            self.coordinator.mutate(row, "pause", True)
        self.coordinator.hpas_paused()
        hpa = self.api.get("horizontalpodautoscalers", "keda", "keda-hpa-keda-add-ons-http-interceptor")
        hpa["spec"]["behavior"]["scaleDown"]["selectPolicy"] = "Min"
        with self.assertRaisesRegex(RuntimeError, "actually be Disabled"):
            self.coordinator.hpas_paused()

    def test_observed_zero_is_insufficient_until_terminating_pods_disappear(self):
        self.freeze_mount_handler()
        for operator in [r for r in self.plan["controllers"] if (r["namespace"], r["name"]) in c.OPERATORS]:
            self.coordinator.mutate(operator, "replicas", 0)
        row = next(r for r in self.plan["controllers"] if (r["namespace"], r["name"]) == ("homebox", "homebox"))
        original = copy.deepcopy(self.api.get("deployments", "homebox", "homebox"))
        self.coordinator.mutate(row, "replicas", 0)
        pod, _ = pod_for(original, c.WORKER, "-terminating")
        pod["metadata"]["deletionTimestamp"] = "2026-11-23T00:00:00Z"
        self.api.add("pods", pod)
        with self.assertRaisesRegex(RuntimeError, "actual Pod disappearance"):
            self.coordinator.controllers_stopped([row])

    def test_original_pause_value_and_absent_direction_restore_exactly(self):
        row = self.plan["autoscalers"][0]
        original = {c.PAUSE[1]: "false"}
        row["original"]["pause_annotations"] = original
        self.api.get("scaledobjects.keda.sh", row["namespace"], row["name"])["metadata"]["annotations"] = original.copy()
        approval = c.sha(c.canonical(self.plan))
        self.coordinator.approval = approval
        self.journal.state["plan_sha256"] = approval
        self.api.get("configmap", "kube-system", c.m.LOCK)["data"]["plan_sha256"] = approval
        self.coordinator.mutate(row, "pause", True)
        self.coordinator.mutate(row, "pause", False, restoring=True)
        self.assertEqual(self.api.get("scaledobjects.keda.sh", row["namespace"], row["name"])["metadata"]["annotations"], original)

    def test_missing_ha_duplicate_cannot_fake_full_recovery_with_ready_peer_only(self):
        self.make_pending()
        self.api.kube("uncordon", c.WORKER)
        row = self.plan["ha"][0]
        self.api.resources["pods"] = [p for p in self.api.items("pods")
                                      if p["metadata"]["uid"] != row["old_worker_uid"] + "-replacement"]
        with self.assertRaisesRegex(RuntimeError, "both genuine Ready copies"):
            self.coordinator.ha_restored()

    def test_cold_capacity_does_not_waive_remaining_migration_pv_locality(self):
        pod = next(p for p in self.api.items("pods") if p["metadata"]["namespace"] == "flux-system"
                   and p["spec"].get("nodeName") == c.WORKER)
        pod["spec"]["volumes"] = [{"name": "data", "persistentVolumeClaim": {"claimName": "local-data"}}]
        self.api.add("persistentvolumeclaims", obj("PersistentVolumeClaim", "flux-system", "local-data",
                                                  {"volumeName": "local-pv"}))
        self.api.add("persistentvolumes", obj("PersistentVolume", "", "local-pv",
                                             {"hostPath": {"path": "/not-reviewed"}}))
        with self.assertRaisesRegex(RuntimeError, "Locality-constrained"):
            c.migration_snapshot(self.plan, self.policy, self.api)


if __name__ == "__main__":
    unittest.main(verbosity=2)
