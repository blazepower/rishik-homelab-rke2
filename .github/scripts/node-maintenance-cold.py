#!/usr/bin/env python3
"""Held worker2 OS cold profile. No fleet fallback or automatic host repair."""

import argparse
import base64
import copy
import hashlib
import importlib.util
import json
import os
from pathlib import Path
import re
import shlex
import stat
import subprocess
import sys
import time
import uuid
import zlib


DIRECTORY = Path(__file__).resolve().parent
spec = importlib.util.spec_from_file_location("cold_maintenance", DIRECTORY / "node-maintenance.py")
m = importlib.util.module_from_spec(spec)
spec.loader.exec_module(m)
require, sha, canonical = m.require, m.sha, m.canonical
WORKER = "rishik-worker2"
PEER = "rishik-worker1"
SERVER = "rishik-controller"
ACTION = "worker2-os-cold"
PAUSE = ("autoscaling.keda.sh/paused-scale-in", "autoscaling.keda.sh/paused-scale-out")
FULL_PAUSE = ("autoscaling.keda.sh/paused", "autoscaling.keda.sh/paused-replicas")
RESOURCES = {
    "Deployment": "deployments", "StatefulSet": "statefulsets", "ScaledObject": "scaledobjects.keda.sh",
    "HorizontalPodAutoscaler": "horizontalpodautoscalers", "CronJob": "cronjobs",
    "HelmRelease": "helmreleases.helm.toolkit.fluxcd.io",
    "Kustomization": "kustomizations.kustomize.toolkit.fluxcd.io",
    "Prometheus": "prometheuses.monitoring.coreos.com", "Alertmanager": "alertmanagers.monitoring.coreos.com",
    "PodDisruptionBudget": "poddisruptionbudgets",
    "HTTPScaledObject": "httpscaledobjects.http.keda.sh", "InterceptorRoute": "interceptorroutes.http.keda.sh",
}
ALLOWED = {
    "adguard-home": {"adguard-home"},
    "homebox": {"homebox"},
    "kaneo": {"kaneo", "kaneo-postgresql"},
    "media": {"bazarr", "bookshelf", "calibre-web", "overseerr", "prowlarr", "qbittorrent", "radarr",
              "rreading-glasses", "rreading-glasses-postgresql", "sabnzbd", "sonarr", "syncthing",
              "plex-plex-media-server"},
    "monitoring": {"kube-prometheus-stack-grafana", "kube-prometheus-stack-kube-state-metrics",
                   "kube-prometheus-stack-operator", "alertmanager-kube-prometheus-stack-alertmanager",
                   "loki", "prometheus-kube-prometheus-stack-prometheus"},
    "paperless": {"paperless-paperless-ngx", "paperless-paperless-ngx-postgresql", "paperless-paperless-ngx-redis"},
    "siloarr": {"stash", "whisparr"},
    "sure-finance": {"sure-finance-maybe-finance", "sure-finance-maybe-finance-redis",
                     "sure-finance-maybe-finance-sidekiq", "sure-finance-maybe-finance-postgresql"},
}
BACKENDS = {
    ("kaneo", "kaneo-postgresql"), ("media", "rreading-glasses-postgresql"),
    ("monitoring", "alertmanager-kube-prometheus-stack-alertmanager"), ("monitoring", "loki"),
    ("monitoring", "prometheus-kube-prometheus-stack-prometheus"),
    ("paperless", "paperless-paperless-ngx-postgresql"), ("paperless", "paperless-paperless-ngx-redis"),
    ("sure-finance", "sure-finance-maybe-finance-redis"), ("sure-finance", "sure-finance-maybe-finance-postgresql"),
}
OPERATORS = {
    ("keda", "keda-operator"), ("keda", "keda-add-ons-http-controller-manager"),
    ("keda", "keda-add-ons-http-external-scaler"), ("keda", "keda-add-ons-http-interceptor"),
    ("monitoring", "kube-prometheus-stack-operator"),
}
HA = {
    ("csi-driver-nfs", "csi-nfs-controller"), ("kube-system", "rke2-coredns-rke2-coredns"),
    ("kyverno", "kyverno-admission-controller"), ("kyverno", "kyverno-background-controller"),
}
TEMP_PDBS = {
    ("flux-system", "kustomize-controller-pdb"),
    ("flux-system", "notification-controller-pdb"), ("flux-system", "sealed-secrets-pdb"),
}
CROSS_PRODUCER = ("kube-system", "media-mount-handler")
MEDIA_SCHEDULES = {("media", name) for name in
                   ("hardcover-sync", "radarr-codec-policy", "sonarr-codec-policy", "sonarr-queue-cleanup")}
SCHEDULES = MEDIA_SCHEDULES | {CROSS_PRODUCER}
IM_NAMES = {WORKER: "instance-manager-450c2767cbf12dc65f11345105b360d4",
            PEER: "instance-manager-ecde826c8c6fccf5ad1dae442d1de064"}
RELEASES = {
    "adguard-home": {"adguard-home"}, "homebox": {"homebox"}, "kaneo": {"kaneo"},
    "media": {"bazarr", "bookshelf", "calibre-web", "overseerr", "plex", "prowlarr", "qbittorrent",
              "radarr", "rreading-glasses", "sabnzbd", "sonarr", "syncthing"},
    "monitoring": {"kube-prometheus-stack", "loki"}, "paperless": {"paperless"},
    "siloarr": {"stash", "whisparr"}, "sure-finance": {"sure-finance"}, "keda": {"keda", "keda-add-ons-http"},
}
RECONCILIATION = {("HelmRelease", ns, name) for ns, names in RELEASES.items() for name in names} | {
    ("Kustomization", "flux-system", name) for name in ("apps", "flux-system", "kyverno-install", "kyverno-policies")}
IM_PATHS = {
    "host": {"path": "/", "type": ""},
    "engine-binaries": {"path": "/var/lib/longhorn/engine-binaries/", "type": ""},
    "metadata": {"path": "/var/lib/longhorn/metadata/", "type": "DirectoryOrCreate"},
    "unix-domain-socket": {"path": "/var/lib/longhorn/unix-domain-socket/", "type": ""},
    "log": {"path": "/var/lib/longhorn/logs", "type": "DirectoryOrCreate"},
}
BUNDLE_FILES = (".github/scripts/node-maintenance.py", ".github/scripts/node-maintenance-host.py",
                ".github/scripts/node-maintenance-cold.py", ".github/scripts/node-maintenance-peer.py",
                "node-config/maintenance/policy.json",
                "apps/_shared/http-addon-owned/source.json")


def identity(obj):
    return obj["metadata"]["namespace"], obj["metadata"]["name"]


def key(row):
    return row["kind"] + ":" + row["namespace"] + "/" + row["name"]


def owners(obj):
    return [{k: o[k] for k in ("kind", "name", "uid")}
            for o in obj["metadata"].get("ownerReferences", []) if o.get("controller")]


def binding(obj, original=None):
    meta = obj["metadata"]
    require(not meta.get("deletionTimestamp"), "Terminating object cannot be approved")
    return {"kind": obj["kind"], "namespace": meta["namespace"], "name": meta["name"],
            "uid": meta["uid"], "generation": meta.get("generation"),
            "spec_sha256": sha(canonical(obj["spec"])), "controllers": owners(obj),
            "original": original or {}}


def replica_binding(obj):
    require(obj["kind"] in ("Deployment", "StatefulSet"), "Unknown scalable controller")
    desired = obj["spec"].get("replicas", 1)
    status = obj.get("status", {})
    require(type(desired) is int and desired >= 0 and not obj["metadata"].get("deletionTimestamp")
            and status.get("observedGeneration", 0) >= obj["metadata"]["generation"]
            and status.get("readyReplicas", 0) == desired
            and status.get("replicas", 0) == desired, "Controller must be fully stable before approval")
    require(obj["spec"].get("persistentVolumeClaimRetentionPolicy", {}).get("whenScaled", "Retain") == "Retain",
            "Destructive StatefulSet scale-down is prohibited")
    if obj["kind"] == "StatefulSet" and desired:
        require(status.get("currentRevision") == status.get("updateRevision") and status.get("currentRevision"),
                "StatefulSet revision is unstable")
    return binding(obj, {"replicas_present": "replicas" in obj["spec"], "replicas": desired})


def suspend_binding(obj):
    return binding(obj, {"suspend_present": "suspend" in obj["spec"],
                         "suspend": obj["spec"].get("suspend", False)})


def mount_handler_contract(obj):
    labels = obj["metadata"].get("labels", {})
    template = obj["spec"].get("jobTemplate", {}).get("spec", {}).get("template", {}).get("spec", {})
    require(obj["kind"] == "CronJob" and identity(obj) == CROSS_PRODUCER and not owners(obj)
            and obj["spec"].get("schedule") == "*/5 * * * *"
            and obj["spec"].get("concurrencyPolicy") == "Forbid"
            and template.get("serviceAccountName") == CROSS_PRODUCER[1]
            and not any("persistentVolumeClaim" in volume for volume in template.get("volumes", []))
            and labels.get("kustomize.toolkit.fluxcd.io/name") == "flux-system"
            and labels.get("kustomize.toolkit.fluxcd.io/namespace") == "flux-system",
            "Exact media-mount-handler identity/service-account/root reconciler contract drift")


def mount_handler_scope(api):
    require(not any(identity(job) != CROSS_PRODUCER
                    and job["spec"].get("jobTemplate", {}).get("spec", {}).get("template", {})
                    .get("spec", {}).get("serviceAccountName") == CROSS_PRODUCER[1]
                    for job in api.items("cronjobs", CROSS_PRODUCER[0])),
            "Foreign CronJob uses the cross-namespace producer service account")


def pod_controller(pod, sets):
    own = owners(pod)
    if len(own) == 1 and own[0]["kind"] == "ReplicaSet":
        rs = sets.get(own[0]["uid"])
        require(rs and rs["metadata"]["namespace"] == pod["metadata"]["namespace"]
                and rs["metadata"]["name"] == own[0]["name"], "Unbound ReplicaSet owner")
        own = owners(rs)
    require(len(own) == 1, "Unmanaged or ambiguously owned Pod")
    return own[0]


def assert_binding(obj, row, mutable=()):
    require(identity(obj) == (row["namespace"], row["name"]) and obj["kind"] == row["kind"]
            and obj["metadata"]["uid"] == row["uid"] and owners(obj) == row["controllers"]
            and not obj["metadata"].get("deletionTimestamp"), key(row) + ": identity/owner drift")
    spec = copy.deepcopy(obj["spec"])
    for field in mutable:
        if field in row["original"] or field + "_present" in row["original"]:
            present = row["original"].get(field + "_present", True)
            if present:
                spec[field] = row["original"][field]
            else:
                spec.pop(field, None)
    require(sha(canonical(spec)) == row["spec_sha256"], key(row) + ": specification drift")


def ha_binding(controller, pods, sets):
    require(identity(controller) in HA and controller["spec"].get("replicas") == 2,
            "Only four exact two-replica HA Deployments are eligible")
    row = replica_binding(controller)
    children = [p for p in pods if p["metadata"]["namespace"] == row["namespace"]
                and m.evictable(p) and pod_controller(p, sets)["uid"] == row["uid"]]
    require(len(children) == 2 and all(m.pod_ready(p) for p in children)
            and {p["spec"].get("nodeName") for p in children} == {WORKER, PEER},
            key(row) + ": requires one Ready Pod on each worker")
    hashes = {m.pending_spec_hash(p) for p in children}
    require(len(hashes) == 1, "HA admitted specifications differ")
    for pod in children:
        affinity = pod["spec"].get("affinity", {})
        terms = affinity.get("podAntiAffinity", {}).get("requiredDuringSchedulingIgnoredDuringExecution", [])
        require(terms and not affinity.get("podAffinity", {}).get("requiredDuringSchedulingIgnoredDuringExecution")
                and not any(t.get("whenUnsatisfiable") == "DoNotSchedule"
                            for t in pod["spec"].get("topologySpreadConstraints", [])),
                "HA contract requires only reviewed hard hostname anti-affinity")
        for term in terms:
            require(set(term) <= {"topologyKey", "labelSelector", "namespaces"}
                    and term["topologyKey"] == "kubernetes.io/hostname"
                    and term.get("namespaces", [row["namespace"]]) == [row["namespace"]]
                    and all(m.matches(term.get("labelSelector"), p["metadata"].get("labels", {}))
                            for p in children), "HA anti-affinity selector/namespace/topology is not exact")
        paths = {v["name"]: v["hostPath"] for v in pod["spec"].get("volumes", []) if "hostPath" in v}
        if identity(controller) == ("csi-driver-nfs", "csi-nfs-controller"):
            require("4.13.6" in controller["metadata"].get("labels", {}).get("helm.sh/chart", ""),
                    "NFS chart is not reviewed 4.13.6")
            require(paths == {"pods-mount-dir": {"path": "/var/lib/kubelet/pods", "type": "Directory"}}
                    and any(v["name"] == "socket-dir" and v.get("emptyDir") == {}
                            for v in pod["spec"].get("volumes", [])), "NFS hostPath/socket locality drift")
            mounts = [(c["name"], v) for c in pod["spec"]["containers"]
                      for v in c.get("volumeMounts", []) if v["name"] == "pods-mount-dir"]
            require(mounts == [("nfs", {"name": "pods-mount-dir", "mountPath": "/var/lib/kubelet/pods",
                                      "mountPropagation": "Bidirectional"})],
                    "NFS bidirectional mount must remain exactly reviewed; no migration waiver")
        else:
            require(not paths, "Unreviewed HA hostPath")
    row.update({"pod_spec_sha256": hashes.pop(),
                "peer_uid": next(p["metadata"]["uid"] for p in children if p["spec"]["nodeName"] == PEER),
                "old_worker_uid": next(p["metadata"]["uid"] for p in children if p["spec"]["nodeName"] == WORKER)})
    row["replicasets"] = sorted([binding(sets[o["uid"]]) for o in owners(children[0])
                                if o["kind"] == "ReplicaSet"], key=key)
    return row


def expected_pending(reviews, pods, sets, get):
    allowed = set()
    require({(r["namespace"], r["name"]) for r in reviews} == HA, "Incomplete exact HA contract")
    for row in reviews:
        obj = get("deployments", row["namespace"], row["name"])
        assert_binding(obj, row)
        require(obj["status"].get("observedGeneration", 0) >= obj["metadata"]["generation"],
                "HA controller generation is unobserved")
        children = [p for p in pods if p["metadata"]["namespace"] == row["namespace"] and m.evictable(p)
                    and pod_controller(p, sets)["uid"] == row["uid"]]
        require(len(children) == 2, "HA replacement missing or unexpected replica count")
        for replica in row["replicasets"]:
            current = sets.get(replica["uid"])
            require(current is not None, "HA ReplicaSet identity changed")
            assert_binding(current, replica)
        require(all(owners(p) == [{"kind": "ReplicaSet", "name": row["replicasets"][0]["name"],
                                  "uid": row["replicasets"][0]["uid"]}] for p in children),
                "HA admitted Pod/ReplicaSet binding drift")
        peer = [p for p in children if p["metadata"]["uid"] == row["peer_uid"]]
        pending = [p for p in children if p["metadata"]["uid"] != row["peer_uid"]]
        require(len(peer) == len(pending) == 1 and peer[0]["spec"].get("nodeName") == PEER
                and m.pod_ready(peer[0]), "Exact Ready worker1 peer was lost")
        for pod in children:
            require(m.pending_spec_hash(pod) == row["pod_spec_sha256"]
                    and m.matches(obj["spec"]["selector"], pod["metadata"].get("labels", {})),
                    "HA Pod/template/selector drift")
        pod = pending[0]
        require(pod["metadata"]["uid"] != row["old_worker_uid"] and not pod["metadata"].get("deletionTimestamp")
                and pod["status"].get("phase") == "Pending" and not pod["spec"].get("nodeName")
                and not m.pod_ready(pod)
                and any(c["type"] == "PodScheduled" and c["status"] == "False"
                        and c.get("reason") == "Unschedulable" for c in pod["status"].get("conditions", [])),
                "Only exact unscheduled HA replacements may be expected Pending")
        allowed.add(pod["metadata"]["uid"])
    return allowed


def im_binding(im, pod, pdb, node):
    require(im["metadata"]["name"] == IM_NAMES[node] and im["spec"].get("nodeID") == node
            and im["spec"].get("type") == "aio" and im["status"].get("currentState") == "running"
            and pod["metadata"]["name"] == im["metadata"]["name"]
            and owners(pod) == [{"kind": "InstanceManager", "name": im["metadata"]["name"],
                                 "uid": im["metadata"]["uid"]}]
            and pod["spec"].get("nodeName") == node and m.pod_ready(pod), "Native IM node/CR/Pod contract drift")
    paths = {v["name"]: v["hostPath"] for v in pod["spec"].get("volumes", []) if "hostPath" in v}
    require(paths == IM_PATHS and len(pod["spec"]["containers"]) == 1
            and pod["spec"]["containers"][0]["name"] == "instance-manager"
            and m.requests(pod).get("cpu") == .3
            and pod["spec"]["containers"][0]["image"] == im["spec"].get("image"),
            "Native IM data-path/image/300m contract is not reviewed")
    require(pdb["metadata"]["name"] == im["metadata"]["name"] and pdb["spec"].get("minAvailable") == 1
            and "maxUnavailable" not in pdb["spec"]
            and m.matches(pdb["spec"].get("selector"), pod["metadata"].get("labels", {})),
            "Native IM must have its original minAvailable=1 protection")
    return {"name": im["metadata"]["name"], "uid": im["metadata"]["uid"], "node": node,
            "spec_sha256": sha(canonical(im["spec"])), "pod_uid": pod["metadata"]["uid"],
            "pod_spec_sha256": m.pending_spec_hash(pod), "pdb": binding(pdb)}


def manager_binding(policy, api):
    ns = policy["storage_namespace"]
    ds = api.get("daemonsets", ns, "longhorn-manager")
    images = [v["image"] for v in ds["spec"]["template"]["spec"]["containers"] if v["name"] == "longhorn-manager"]
    require(len(images) == 1 and re.search(r":v1\.12\.1(?:@sha256:[a-f0-9]{64})?$", images[0]),
            "Native manager runtime is not reviewed v1.12.1")
    chart = api.get("helmrelease", ns, "longhorn")
    require(chart["spec"]["chart"]["spec"]["version"] == "1.12.1", "Longhorn chart version drift")
    rows = []
    for node in (WORKER, PEER):
        pods = [p for p in api.items("pods", ns) if p["metadata"].get("labels", {}).get("app") == "longhorn-manager"
                and p["spec"].get("nodeName") == node]
        require(len(pods) == 1 and m.pod_ready(pods[0])
                and owners(pods[0]) == [{"kind": "DaemonSet", "name": "longhorn-manager", "uid": ds["metadata"]["uid"]}],
                "Native manager DaemonSet ownership/readiness proof missing")
        pod = pods[0]
        statuses = pod["status"].get("containerStatuses", [])
        require(statuses and all(re.search(r"sha256:[a-f0-9]{64}$", s.get("imageID", "")) for s in statuses),
                "Native manager admitted runtime image digest proof missing")
        rows.append({"node": node, "uid": pod["metadata"]["uid"], "pod_spec_sha256": m.pending_spec_hash(pod),
                     "image_ids": {s["name"]: sha(s["imageID"].encode()) for s in statuses},
                     "restarts": {s["name"]: s["restartCount"] for s in statuses}})
    return {"daemonset": binding(ds), "chart": binding(chart), "pods": rows}


def source_bindings():
    return {name: sha((m.ROOT / Path(name)).read_bytes()) for name in BUNDLE_FILES}


def peer():
    script = DIRECTORY / "node-maintenance-peer.py"
    spec = importlib.util.spec_from_file_location("cold_peer", script)
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


def check_peer_scope(plan, starting=True):
    require(set(plan["peer_scope"]) == {SERVER, WORKER}, "Exact controller/worker2 restricted peer scope required")
    for node, proof in plan["peer_scope"].items():
        other = plan["peer_scope"][WORKER if node == SERVER else SERVER]
        require(proof["node"] == node and proof["peer"] == other["node"]
                and re.fullmatch("[a-f0-9]{32}", proof["setup_id"])
                and proof["setup_id"] == other["setup_id"]
                and proof["started_epoch"] == other["started_epoch"]
                and proof["expires_epoch"] == proof["started_epoch"] + 86400 == other["expires_epoch"]
                and proof["started_epoch"] <= time.time() < proof["expires_epoch"] - (43200 if starting else 0)
                and proof["source_sha256"] == {name: plan["source_bindings"][".github/scripts/" + name]
                                               for name in ("node-maintenance-peer.py", "node-maintenance-host.py")}
                and proof["original_boot_id"] == plan["bindings"][node]["boot_id"]
                and proof["original_kernel"] == plan["bindings"][node]["kernel"]
                and proof["incoming_key_fingerprint"] == other["public_key_fingerprint"]
                and proof["probe"]["peer_proof_sha256"] == sha(canonical({k: v for k, v in other.items() if k != "probe"}))
                and proof["started_epoch"] <= proof["probe"]["verified_epoch"] <= time.time(),
                "Restricted peer proof/source/identity/clock/expiry drift; block BEFORE first pause")


def worker_contract(plan, policy, run_id, step_id, started, deadline):
    require(re.fullmatch("[a-f0-9]{32}", run_id) and re.fullmatch("[a-f0-9]{32}", step_id)
            and run_id != step_id and deadline == started + 43200, "Invalid exact worker standby slot")
    return {
        "run_id": run_id, "host_step_id": step_id, "plan_sha256": sha(canonical(plan)),
        "source_bindings": plan["source_bindings"], "bindings": plan["bindings"],
        "peer_scope": plan["peer_scope"],
        "started_epoch": started, "deadline_epoch": deadline,
        "os_payload": {
            "operation": "os", "run_id": step_id, "cold_run_id": run_id, "name": WORKER, "role": "agent",
            "boot_id": plan["bindings"][WORKER]["boot_id"], "ubuntu_lts": policy["ubuntu_lts"],
            "pending_packages": plan["bindings"][WORKER]["pending_packages"], "kernel": policy["kernel"],
            "kernel_package": policy["kernel_package"], "deadline_epoch": deadline, "recovery_reserve_seconds": 7200,
        },
    }


def worker_unit(contract):
    directory = "/var/lib/node-maintenance-cold-worker/" + contract["run_id"]
    payload = base64.b64encode(canonical({"operation": "cold-standby", "run_id": contract["run_id"]})).decode()
    return ("[Unit]\nDescription=Single approved worker2 cold OS standby\n[Service]\nType=exec\nUser=root\n"
            f"ExecStart=/usr/bin/python3 -B {directory}/.github/scripts/node-maintenance-host.py {payload}\n"
            "UMask=0077\nSlice=system.slice\nRestart=no\nTimeoutStartSec=infinity\nTimeoutStopSec=infinity\n"
            "SendSIGKILL=no\nKillMode=process\n")


def worker_finalizer_units(contract):
    unit = "node-maintenance-cold-worker-" + contract["run_id"] + "-finalize"
    directory = "/var/lib/node-maintenance-cold-worker/" + contract["run_id"]
    payload = base64.b64encode(canonical({"operation": "cold-worker-finalize", "run_id": contract["run_id"]})).decode()
    return {
        unit + ".service": (
            "[Unit]\nDescription=Exact worker2 revoked dead-job finalizer\n[Service]\nType=oneshot\nUser=root\n"
            f"ExecStart=/usr/bin/python3 -B {directory}/.github/scripts/node-maintenance-host.py {payload}\n"
            "UMask=0077\nRestart=no\nTimeoutStartSec=60s\nTimeoutStopSec=30s\nSendSIGKILL=no\nKillMode=process\n"),
        unit + ".timer": ("[Unit]\nDescription=Run-scoped worker2 revocation acknowledgment\n[Timer]\n"
                         f"OnActiveSec=10s\nOnUnitInactiveSec=10s\nUnit={unit}.service\n"),
    }


def worker_finalizer_ready(proof, contract, now):
    finalizer = proof["finalizer"]
    units = worker_finalizer_units(contract)
    name = "node-maintenance-cold-worker-" + contract["run_id"] + "-finalize"
    service, timer = finalizer["service"], finalizer["timer"]
    require(finalizer["service_sha256"] == sha(units[name + ".service"].encode())
            and finalizer["timer_sha256"] == sha(units[name + ".timer"].encode())
            and service["LoadState"] == "loaded" and service["User"] == "root"
            and service["ActiveState"] in ("inactive", "activating", "active")
            and service["FragmentPath"] == "/run/systemd/system/" + name + ".service"
            and timer["LoadState"] == "loaded" and timer["ActiveState"] == "active" and timer["Job"] == "0"
            and timer["Unit"] == name + ".service"
            and timer["FragmentPath"] == "/run/systemd/system/" + name + ".timer"
            and 0 <= now - proof["record"].get("finalizer_verified_epoch", 0) < 30,
            "Actual exact independent worker revocation finalizer required BEFORE first pause")


def verify_worker(record, contract):
    require(record["contract"] == contract and record["contract_sha256"] == sha(canonical(contract)),
            "Worker standby run/step/approval/source/host/deadline binding drift")


def worker_ready(proof, contract, now):
    record, unit = proof["record"], proof["unit"]
    verify_worker(record, contract)
    worker_finalizer_ready(proof, contract, now)
    original = contract["bindings"][WORKER]
    require(record["original_host"] == {k: original[k] for k in ("boot_id", "kernel", "pending_packages")},
            "Standby did not verify original worker boot/kernel/package bindings")
    require(record["phase"] == "waiting" and 0 <= now - record["ready_epoch"] < 30
            and 0 <= now - record["peer_epoch"] < 30
            and unit["LoadState"] == "loaded" and unit["ActiveState"] == "active" and unit["Job"] == "0"
            and unit["User"] == "root" and int(unit["MainPID"]) == record["pid"] > 0
            and proof["unit_sha256"] == sha(worker_unit(contract).encode()),
            "Genuine live exact worker standby readiness required BEFORE first pause")


class API:
    get = staticmethod(m.get)
    items = staticmethod(m.items)
    kube = staticmethod(m.kube)

    def patch(self, row, obj, changes):
        require(row["kind"] != "PodDisruptionBudget"
                or (row["namespace"], row["name"]) in TEMP_PDBS,
                "Native/storage/CSI/Kyverno PDB writes are prohibited")
        operations = [{"op": "test", "path": "/metadata/uid", "value": row["uid"]},
                      {"op": "test", "path": "/metadata/resourceVersion",
                       "value": obj["metadata"]["resourceVersion"]}, *changes]
        self.kube("-n", row["namespace"], "patch", RESOURCES[row["kind"]], row["name"], "--type=json",
                  "-p", json.dumps(operations))

    def host_audit(self, policy, name):
        require(name == WORKER, "Cold peer host audit is worker2 only")
        return peer().request("audit")

    def worker_status(self, policy, run_id):
        return peer().request("worker-status", run_id)

    def host_idle(self, policy, step_id, run_id=None):
        observed = peer().request("worker-idle", run_id, step_id)
        state = observed["states"]
        require(len(state) == 6 and all(s["ActiveState"] in ("inactive", "failed") and s["Job"] == "0"
                                       for s in state[:3]),
                "Host APT/reboot service or timer is active/queued; warming is unsafe")
        require(all(s["LoadState"] == "loaded" and s["Job"] == "0" and s["ActiveState"] == "inactive"
                    for s in state[3:5]), "Host shutdown/reboot job is pending; warming is unsafe")
        require(state[5]["ActiveState"] in ("inactive", "failed") and state[5]["Job"] == "0",
                "Standby can still claim/execute; waiting is not idle authorization")
        require(observed["cgroups_empty"] is True, "Bound package cgroup populated/unknown; no warmup")
        return state[0]["LoadState"] != "not-found"


def build_plan(policy, api=None):
    api = api or API()
    inventory = m.cold_inventory(policy)
    old_advisory = ("Inventory only:", "Durable restoration", "Autoscaler/operator", "Worker2 router DNS")
    require(not [b for b in inventory["blockers"] if not b.startswith(old_advisory)],
            "Cold inventory has unresolved writer/owner/attachment blockers")
    require(set(inventory["namespaces"]) == set(ALLOWED)
            and {(r["namespace"], r["name"]) for r in inventory["controllers"]}
            == {(ns, name) for ns, names in ALLOWED.items() for name in names},
            "Unknown/missing namespace or application controller; no arbitrary cold cohort")
    pods, deployments, sets = api.items("pods"), api.items("deployments"), api.items("replicasets")
    set_map = {r["metadata"]["uid"]: r for r in sets}
    dmap = {identity(d): d for d in deployments}
    objects = deployments + api.items("statefulsets")
    cmap = {identity(d): d for d in objects}
    controllers = [replica_binding(cmap[(r["namespace"], r["name"])]) for r in inventory["controllers"]]
    extra = [replica_binding(dmap[k]) for k in sorted(OPERATORS) if k[0] == "keda"]
    controllers += extra
    require(all(not r["controllers"] for r in controllers if (r["namespace"], r["name"]) in OPERATORS),
            "Operator controller is controlled by an uncaptured parent")
    # Bind exact operator image pins to repository provenance, never accept a tag-only substitute.
    pins = json.loads((m.ROOT / "apps" / "_shared" / "http-addon-owned" / "source.json").read_text())
    keda_image = "ghcr.io/kedacore/keda:" + pins["native"]["core_image_tags"]["keda"]
    expected_images = {
        "keda-operator": keda_image,
        "keda-add-ons-http-controller-manager": "ghcr.io/blazepower/http-addon-owned-operator@sha256:849c089d111384df6bf4c59725ff886fc145bd423909084b774d6a31c8942e53",
        "keda-add-ons-http-external-scaler": "ghcr.io/blazepower/http-addon-owned-scaler@sha256:6d3d3953627f7049ecee26b9034d346d2a3f96c2bcab1f2561557acd5f391130",
        "keda-add-ons-http-interceptor": "ghcr.io/blazepower/http-addon-owned-interceptor@sha256:3b0aeb314779f6e379736ed0b00a987843caac7e6805e9fc930f462f07812a9d",
    }
    for row in extra:
        obj = dmap[(row["namespace"], row["name"])]
        require([c["image"] for c in obj["spec"]["template"]["spec"]["containers"]]
                == [expected_images[row["name"]]], "KEDA/HTTP runtime image differs from owned pin")
    parents = []
    for row in controllers:
        if not row["controllers"]:
            continue
        require(len(row["controllers"]) == 1 and row["namespace"] == "monitoring"
                and row["controllers"][0]["kind"] in ("Prometheus", "Alertmanager")
                and (row["namespace"], row["name"]) in BACKENDS, "Unknown application parent reconciler")
        own = row["controllers"][0]
        parent = api.get(RESOURCES[own["kind"]], "monitoring", own["name"])
        require(parent["metadata"]["uid"] == own["uid"], "Monitoring parent UID drift")
        parents.append(binding(parent))
    autoscalers = []
    for row in inventory["autoscalers"]:
        obj = api.get(RESOURCES[row["kind"]], row["namespace"], row["name"])
        assert_binding(obj, row)
        annotations = obj["metadata"].get("annotations", {})
        require(not any(k in annotations for k in FULL_PAUSE),
                "Full KEDA pause deletes HPA identity; this profile cannot preserve that original state")
        item = copy.deepcopy(row)
        item["original"]["pause_annotations"] = {k: annotations[k] for k in PAUSE if k in annotations}
        autoscalers.append(item)
    require(len(autoscalers) == len(inventory["hpas"]) == 5, "Expected five exact KEDA/HPA pairs")
    interceptor = api.get("scaledobjects.keda.sh", "keda", "keda-add-ons-http-interceptor")
    target = next(e for e in extra if e["name"] == "keda-add-ons-http-interceptor")
    require(interceptor["spec"]["scaleTargetRef"].get("kind", "Deployment") == "Deployment"
            and interceptor["spec"]["scaleTargetRef"]["name"] == target["name"]
            and not owners(interceptor), "HTTP interceptor ScaledObject parent contract is not reviewed")
    annotations = interceptor["metadata"].get("annotations", {})
    require(not any(k in annotations for k in FULL_PAUSE), "HTTP interceptor full pause cannot preserve HPA")
    ancillary = binding(interceptor, {"pause_annotations": {k: annotations[k] for k in PAUSE if k in annotations}})
    ancillary["target"] = {"kind": "Deployment", "name": target["name"], "uid": target["uid"]}
    autoscalers.append(ancillary)
    native_hpas = [o for o in api.items("horizontalpodautoscalers", "keda")
                   if owners(o) == [{"kind": "ScaledObject", "name": ancillary["name"], "uid": ancillary["uid"]}]]
    require(len(native_hpas) == 1 and native_hpas[0]["spec"]["scaleTargetRef"].get("kind") == "Deployment"
            and native_hpas[0]["spec"]["scaleTargetRef"]["name"] == target["name"],
            "HTTP interceptor native HPA identity/ownership proof missing")
    hpa = binding(native_hpas[0])
    hpa["target"] = ancillary["target"]
    inventory["hpas"].append(hpa)
    require({o["metadata"]["uid"] for o in api.items("scaledobjects.keda.sh")}
            == {r["uid"] for r in autoscalers}, "Uncaptured KEDA scaling parent; no global operator pause")
    for row in inventory["hpas"]:
        obj = api.get("horizontalpodautoscalers", row["namespace"], row["name"])
        assert_binding(obj, row)
        row["paused_spec_sha256"] = sha(canonical(hpa_paused_spec(obj["spec"])))
        row["owner_references_sha256"] = sha(canonical(obj["metadata"].get("ownerReferences", [])))
    http = [binding(o) for resource in ("httpscaledobjects.http.keda.sh", "interceptorroutes.http.keda.sh")
            for o in api.items(resource)]
    require(not [o for o in http if o["kind"] == "HTTPScaledObject"],
            "Legacy HTTPScaledObject parent requires separate reviewed pause; no guessed waiver")
    recon = [suspend_binding(api.get(RESOURCES[r["kind"]], r["namespace"], r["name"]))
             for r in inventory["reconciliation"]]
    for name in ("keda", "keda-add-ons-http"):
        recon.append(suspend_binding(api.get("helmreleases.helm.toolkit.fluxcd.io", "keda", name)))
    require(len(recon) == 27 and len({key(r) for r in recon}) == 27, "Reconciliation cohort drift")
    require({(r["kind"], r["namespace"], r["name"]) for r in recon} == RECONCILIATION,
            "Unknown/missing reconciliation object; no arbitrary suspension")
    schedules = [suspend_binding(api.get("cronjobs", r["namespace"], r["name"])) for r in inventory["schedules"]]
    require(len(schedules) == 4 and {(r["namespace"], r["name"]) for r in schedules} == MEDIA_SCHEDULES,
            "Unexpected scheduled producer")
    handler = api.get("cronjobs", *CROSS_PRODUCER)
    mount_handler_contract(handler)
    mount_handler_scope(api)
    schedules.append(suspend_binding(handler))
    ha = [ha_binding(dmap[k], pods, set_map) for k in sorted(HA)]
    ns = policy["storage_namespace"]
    native = {}
    for node, name in IM_NAMES.items():
        native[node] = im_binding(api.get("instancemanagers.longhorn.io", ns, name),
                                  api.get("pods", ns, name), api.get("poddisruptionbudgets", ns, name), node)
    native_manager = manager_binding(policy, api)
    default_im_image = api.get("settings.longhorn.io", ns, "default-instance-manager-image")["value"]
    require(default_im_image and all(api.get("instancemanagers.longhorn.io", ns, row["name"])["spec"].get("image")
                                    == default_im_image for row in native.values()),
            "Native default IM image must match both approved worker IMs")
    temp = []
    pdbs = api.items("poddisruptionbudgets")
    for pdb in pdbs:
        if identity(pdb) not in TEMP_PDBS:
            continue
        affected = [p for p in pods if p["metadata"]["namespace"] == pdb["metadata"]["namespace"]
                    and m.evictable(p) and m.matches(pdb["spec"].get("selector"), p["metadata"].get("labels", {}))]
        require(len(affected) == 1 and m.pod_ready(affected[0])
                and pdb["spec"].get("minAvailable") == "50%" and "maxUnavailable" not in pdb["spec"],
                "Temporary budget is not an exact healthy singleton minAvailable=50%")
        owner = pod_controller(affected[0], set_map)
        require(owner["kind"] == "Deployment" and dmap[(pdb["metadata"]["namespace"], owner["name"])]["spec"].get("replicas") == 1,
                "Temporary availability change is not a singleton Deployment")
        row = binding(pdb, {"minAvailable_present": True, "minAvailable": "50%",
                            "maxUnavailable_present": False})
        row["workload"] = replica_binding(dmap[(pdb["metadata"]["namespace"], owner["name"])])
        temp.append(row)
    require({(r["namespace"], r["name"]) for r in temp} == TEMP_PDBS, "Missing exact temporary infrastructure PDB")
    volume_records = storage_snapshot(policy, api)
    claims = []
    for row in inventory["claims"]:
        pvc, pv = api.get("persistentvolumeclaims", row["namespace"], row["pvc"]), api.get("persistentvolumes", name=row["pv"])
        require(pvc["metadata"]["uid"] == row["pvc_uid"] and pv["metadata"]["uid"] == row["pv_uid"]
                and pv["spec"]["csi"]["volumeHandle"] == row["volume"], "Claim identity drift")
        claims.append({**row, "pvc_spec_sha256": sha(canonical(pvc["spec"])), "pv_spec_sha256": sha(canonical(pv["spec"]))})
    backup = [r for r in claims if r["namespace"] == "sure-finance"
              and r["pvc"] == "sure-upgrade-20261123-encrypted"]
    require(len(backup) == 1 and backup[0]["volume"] == "pvc-c200e7ff-e182-45e6-9523-c2fac869df7c",
            "Encrypted Sure recovery claim/reference must remain preserved")
    inventory_hosts = m.audit_nodes(policy)
    baseline = m.build_plan(policy, "os", None, inventory_hosts, worker2_only=True)
    server = next(n for n in policy["nodes"] if n["name"] == SERVER)
    controller_client = m.remote(server, {"operation": "cold-client-audit"})
    m.check_client_skew(controller_client["version"], baseline["kubectl"]["server"])
    host = inventory_hosts[WORKER]
    require(host["name"] == WORKER and host["os"] == "ubuntu" and host["os_version"] == policy["ubuntu_lts"]
            and host["architecture"] == "x86_64" and host["kernel_metapackage_installed"]
            and host["root_free_bytes"] >= 4 * 1024**3 and host["boot_free_bytes"] >= 512 * 1024**2
            and host["memory_available_bytes"] >= 1536 * 1024**2 and host["sunreclaim_bytes"] < 1024**3
            and 0 <= time.time() - host["apt_metadata_epoch"] < 48 * 3600, "Worker2 package/host preflight failed")
    m.check_client_skew(baseline["kubectl"]["client"], baseline["kubectl"]["server"])
    plan = {
        "action": ACTION, "worker2_only": True, "nodes": baseline["nodes"], "release": None,
        "created_epoch": int(time.time()), "cluster_uid": inventory["cluster_uid"],
        "policy_sha256": baseline["policy_sha256"], "runner_sha256": baseline["runner_sha256"],
        "host_sha256": baseline["host_sha256"], "source_bindings": source_bindings(),
        "bindings": baseline["bindings"], "kubectl": baseline["kubectl"], "quarantine": inventory["baseline_health"]["quarantine"],
        "controller_client": controller_client,
        "peer_scope": {n["name"]: m.remote(n, {"operation": "cold-peer-proof"})
                       for n in policy["nodes"] if n["name"] in (SERVER, WORKER)},
        "outage_budget": {"maximum_seconds": 43200, "recovery_reserve_seconds": 7200},
        "controllers": sorted(controllers, key=key), "parents": sorted(parents, key=key),
        "claims": claims, "storage": volume_records, "autoscalers": autoscalers, "hpas": inventory["hpas"],
        "storage_users": inventory["storage_users"],
        "http": http, "schedules": schedules, "reconciliation": sorted(recon, key=key),
        "ha": ha, "native": native, "temporary_pdbs": temp,
        "native_default_image": default_im_image,
        "native_manager": native_manager,
        "reviewed_csi_sockets": m.reviewed_csi_sockets(pods, sets, deployments, ns),
        "operator_attestation": "AdGuard is unused; no DNS change requested or independently verified",
        "application_probes": sorted(set(policy["probe_urls"] + ["https://sure.homelab/"])),
        "blockers": [],
    }
    plan["blockers"] = cold_scheduling(plan, policy, api)
    plan["migration"] = migration_snapshot(plan, policy, api)
    return plan


def check_plan(plan, approval, policy, api=None, age=True):
    api = api or API()
    require(sha(canonical(plan)) == approval, "Cold approval does not match complete plan")
    require(plan.get("action") == ACTION and plan.get("worker2_only") is True
            and plan.get("release") is None and plan.get("nodes") == [n for n in policy["nodes"] if n["name"] == WORKER]
            and len(plan["nodes"]) == 1 and plan["nodes"][0]["role"] == "agent", "Cold mode is worker2 OS ONLY")
    require(plan.get("outage_budget") == {"maximum_seconds": 43200, "recovery_reserve_seconds": 7200},
            "Cold deadline/reserve is not approved")
    require(plan["source_bindings"] == source_bindings() and plan["policy_sha256"] == sha(m.POLICY.read_bytes()),
            "Cold source/policy changed after approval")
    check_peer_scope(plan, starting=age)
    require(plan["runner_sha256"] == sha((DIRECTORY / "node-maintenance.py").read_bytes())
            and plan["host_sha256"] == sha((DIRECTORY / "node-maintenance-host.py").read_bytes()),
            "Runner/host byte binding drift")
    require(plan["cluster_uid"] == api.get("namespace", name="kube-system")["metadata"]["uid"], "Wrong cold cluster")
    require(not plan["blockers"], "Cold plan has unresolved native/scheduling blockers")
    require(not age or 0 <= time.time() - plan["created_epoch"] < 4 * 3600, "Expired/future cold approval")
    require({(r["namespace"], r["name"]) for r in plan["controllers"]}
            == {(ns, name) for ns, names in ALLOWED.items() for name in names} | {k for k in OPERATORS if k[0] == "keda"},
            "Malformed cold controller scope")
    require({(r["kind"], r["namespace"], r["name"]) for r in plan["reconciliation"]} == RECONCILIATION,
            "Malformed cold reconciliation scope")
    require(len(plan["schedules"]) == 5 and
            {(r["kind"], r["namespace"], r["name"]) for r in plan["schedules"]}
            == {("CronJob", ns, name) for ns, name in SCHEDULES},
            "Malformed cold scheduled producer scope")
    require({(r["namespace"], r["name"]) for r in plan["ha"]} == HA
            and {(r["namespace"], r["name"]) for r in plan["temporary_pdbs"]} == TEMP_PDBS,
            "Malformed cold HA/PDB scope")


def storage_snapshot(policy, api):
    ns = policy["storage_namespace"]
    volumes, engines, replicas = api.items("volumes.longhorn.io", ns), api.items("engines.longhorn.io", ns), api.items("replicas.longhorn.io", ns)
    result = []
    for volume in volumes:
        name = volume["metadata"]["name"]
        # Only attachment lifecycle fields may vary; data policy, encryption references and images are hash-bound.
        v_spec = {k: v for k, v in volume["spec"].items() if k not in ("nodeID", "lastAttachedBy")}
        copies = [r for r in replicas if r["spec"]["volumeName"] == name]
        engine_rows = [e for e in engines if e["spec"]["volumeName"] == name]
        # Native RW transitions update these timestamps. Current health is checked
        # separately, while untouched quarantine specifications remain exact.
        replica_lifecycle = ("desireState", "instanceManagerName")
        if name not in policy.get("quarantined_volumes", []):
            replica_lifecycle += ("healthyAt", "lastHealthyAt", "failedAt", "lastFailedAt")
        attachment = api.get("volumeattachments.longhorn.io", ns, name)
        result.append({"name": name, "uid": volume["metadata"]["uid"], "spec_sha256": sha(canonical(v_spec)),
                       "attachment_uid": attachment["metadata"]["uid"],
                       "attachment_spec_sha256": sha(canonical({k: v for k, v in attachment["spec"].items()
                                                               if k != "attachmentTickets"})),
                       "replicas": sorted([{"name": r["metadata"]["name"], "uid": r["metadata"]["uid"],
                                           "node": r["spec"].get("nodeID"), "data_directory": r["spec"].get("dataDirectoryName"),
                                           "spec_sha256": sha(canonical({k: v for k, v in r["spec"].items()
                                                                        if k not in replica_lifecycle}))}
                                          for r in copies], key=lambda r: r["name"]),
                       "engines": sorted([{"name": e["metadata"]["name"], "uid": e["metadata"]["uid"],
                                          "spec_sha256": sha(canonical({k: v for k, v in e["spec"].items()
                                                                       if k not in ("nodeID", "desireState", "replicaAddressMap",
                                                                                    "upgradedReplicaAddressMap")}))}
                                         for e in engine_rows], key=lambda r: r["name"])})
    return sorted(result, key=lambda r: r["name"])


def cold_storage(plan, policy, api, detached=False):
    ns = policy["storage_namespace"]
    require(storage_snapshot(policy, api) == plan["storage"], "Volume/engine/replica/data-directory/policy drift")
    for row in plan["claims"]:
        pvc, pv = api.get("persistentvolumeclaims", row["namespace"], row["pvc"]), api.get("persistentvolumes", name=row["pv"])
        require(pvc["metadata"]["uid"] == row["pvc_uid"] and pv["metadata"]["uid"] == row["pv_uid"]
                and sha(canonical(pvc["spec"])) == row["pvc_spec_sha256"]
                and sha(canonical(pv["spec"])) == row["pv_spec_sha256"], "Claim/PV/encryption-reference drift")
    volumes, engines, replicas = api.items("volumes.longhorn.io", ns), api.items("engines.longhorn.io", ns), api.items("replicas.longhorn.io", ns)
    protected = m.quarantine_state({**policy, "_quarantine": plan["quarantine"]}, volumes, engines, replicas)
    regular = [v for v in volumes if v["metadata"]["name"] not in protected]
    counts = m.storage_check(regular, engines, replicas, {WORKER, PEER})
    if detached:
        for volume in regular:
            name = volume["metadata"]["name"]
            require(volume["status"]["state"] == "detached" and not volume["spec"].get("nodeID")
                    and not volume["status"].get("currentNodeID")
                    and not api.get("volumeattachments.longhorn.io", ns, name)["spec"].get("attachmentTickets"),
                    name + ": all regular volumes must be detached with empty tickets")
        require(all(r["status"].get("currentState") == r["spec"].get("desireState") == "stopped"
                    for r in replicas if r["spec"]["volumeName"] not in protected)
                and all(e["status"].get("currentState") == e["spec"].get("desireState") == "stopped" for e in engines),
                "Cold engines/replicas have not stopped")
    return counts


def instance_manager_labels(node, image, data_engine):
    labels = {"longhorn.io/managed-by": "longhorn-manager", "longhorn.io/component": "instance-manager",
              "longhorn.io/instance-manager-type": "aio", "longhorn.io/node": node,
              "longhorn.io/data-engine": data_engine}
    if image:
        canonical_image = image.replace(":", "-").replace("/", "-").strip()
        labels["longhorn.io/instance-manager-image"] = "imi-" + hashlib.sha512(canonical_image.encode()).hexdigest()[:8]
    return labels


def replica_instance_manager(replica, policy, api):
    ns, spec, status = policy["storage_namespace"], replica["spec"], replica["status"]
    engine, node = spec.get("dataEngine"), spec.get("nodeID")
    require(engine in ("v1", "v2") and node == PEER, "Unreviewed native peer data engine/node")
    assigned = status.get("instanceManagerName")
    if assigned:
        im = api.get("instancemanagers.longhorn.io", ns, assigned)
    else:
        require(status.get("currentState") == spec.get("desireState") == "stopped"
                and not status.get("starting"), "Native peer instance is not stably stopped")
        # GetInstanceManagerByInstanceRO selects v1's default image, but v2's
        # unique running aio manager. A stopped replica has no assigned IM.
        image = api.get("settings.longhorn.io", ns, "default-instance-manager-image")["value"] if engine == "v1" else ""
        labels = instance_manager_labels(node, image, engine)
        candidates = [im for im in api.items("instancemanagers.longhorn.io", ns)
                      if all(im["metadata"].get("labels", {}).get(k) == v for k, v in labels.items())]
        if len(candidates) > 1 and engine == "v2":
            candidates = [im for im in candidates if im["status"].get("currentState") == "running"]
        require(len(candidates) == 1, "Missing/ambiguous native peer IM selector resolution")
        im = candidates[0]
    require(im["metadata"]["name"] == IM_NAMES[PEER] and not im["metadata"].get("deletionTimestamp")
            and im["spec"].get("nodeID") == node and im["spec"].get("type") == "aio"
            and im["spec"].get("dataEngine") == engine and im["status"].get("currentState") == "running",
            "Native replica did not resolve to the exact running peer IM")
    return im


def native_gate(plan, policy, api, released=False, returned=False):
    ns = policy["storage_namespace"]
    require(api.get("settings.longhorn.io", ns, "node-drain-policy")["value"] == "block-if-contains-last-replica",
            "Native strict drain policy changed")
    require(api.get("settings.longhorn.io", ns, "kubernetes-cluster-autoscaler-enabled")["value"] == "false",
            "Native autoscaler policy is not reviewed")
    require(api.get("settings.longhorn.io", ns, "default-instance-manager-image")["value"] == plan["native_default_image"],
            "Native default IM image drift")
    current_manager = manager_binding(policy, api)
    require(current_manager["daemonset"] == plan["native_manager"]["daemonset"]
            and current_manager["chart"] == plan["native_manager"]["chart"], "Native manager/chart source drift")
    for old, new in zip(plan["native_manager"]["pods"], current_manager["pods"]):
        require(old["node"] == new["node"] and old["pod_spec_sha256"] == new["pod_spec_sha256"]
                and old["image_ids"] == new["image_ids"]
                and (old["node"] == WORKER and returned or old["uid"] == new["uid"] and old["restarts"] == new["restarts"]),
                "Native manager runtime/peer/restart binding drift")
    nodes = {n["metadata"]["name"]: n for n in api.items("nodes")}
    require(not nodes[PEER]["spec"].get("unschedulable") and
            any(c["type"] == "Ready" and c["status"] == "True" for c in nodes[PEER]["status"]["conditions"]),
            "Schedulable Ready worker1 peer is mandatory")
    for node, row in plan["native"].items():
        im = api.get("instancemanagers.longhorn.io", ns, row["name"])
        require(im["metadata"]["uid"] == row["uid"] and sha(canonical(im["spec"])) == row["spec_sha256"],
                "Native IM CR/image/node binding drift")
        if released and node == WORKER:
            require(not im["status"].get("instanceEngines") and not im["status"].get("instanceEngineFrontends"),
                    "Worker2 native engine/frontend instances remain")
            continue
        pod = api.get("pods", ns, row["name"])
        require(im["status"].get("currentState") == "running" and m.pod_ready(pod)
                and pod["spec"].get("nodeName") == node and m.pending_spec_hash(pod) == row["pod_spec_sha256"]
                and owners(pod) == [{"kind": "InstanceManager", "name": row["name"], "uid": row["uid"]}]
                and (node == WORKER and returned or pod["metadata"]["uid"] == row["pod_uid"]),
                "Native running peer/returned IM proof missing")
        pdb = api.get("poddisruptionbudgets", ns, row["name"])
        if node == WORKER and returned:
            require(pdb["spec"].get("minAvailable") == 1 and "maxUnavailable" not in pdb["spec"]
                    and m.matches(pdb["spec"].get("selector"), pod["metadata"].get("labels", {})),
                    "Returned IM lacks renewed native PDB")
        else:
            assert_binding(pdb, row["pdb"])
    if released:
        require(nodes[WORKER]["spec"].get("unschedulable") is True, "Native release requires worker2 cordon")
        require(not any(im["status"].get("instanceEngines") or im["status"].get("instanceEngineFrontends")
                        for im in api.items("instancemanagers.longhorn.io", ns) if im["spec"].get("nodeID") == WORKER),
                "Native worker2 engine/frontends remain in another IM")
        replicas = api.items("replicas.longhorn.io", ns)
        for local in [r for r in replicas if r["spec"].get("nodeID") == WORKER]:
            # No last-copy/unused-copy exclusion, including quarantines.
            peers = [r for r in replicas if r["spec"]["volumeName"] == local["spec"]["volumeName"]
                     and r["spec"].get("nodeID") == PEER and r["spec"].get("healthyAt")
                     and not r["spec"].get("failedAt") and r["spec"].get("active")]
            require(len(peers) == 1,
                    local["metadata"]["name"] + ": missing native PDB-protected healthy peer")
            im = replica_instance_manager(peers[0], policy, api)
            require(im["metadata"]["uid"] == plan["native"][PEER]["uid"],
                    "Native peer replica resolved to an unapproved IM identity")
        require(not [p for p in api.items("poddisruptionbudgets", ns) if p["metadata"]["name"] == IM_NAMES[WORKER]],
                "Longhorn has NOT natively released worker2 IM PDB; never patch/delete it")


def cold_scheduling(plan, policy, api):
    pods, sets = api.items("pods"), {r["metadata"]["uid"]: r for r in api.items("replicasets")}
    nodes = {n["metadata"]["name"]: n for n in api.items("nodes")}
    cold_uids = {r["uid"] for r in plan["controllers"]}
    excluded = cold_uids | {r["uid"] for r in plan["ha"]}
    incoming = []
    for pod in pods:
        if m.evictable(pod) and pod_controller(pod, sets)["uid"] in cold_uids:
            continue
        if pod["spec"].get("nodeName") != WORKER or not m.evictable(pod):
            incoming.append(pod)
            continue
        if pod["metadata"]["namespace"] == policy["storage_namespace"] and pod["metadata"]["name"] == IM_NAMES[WORKER]:
            continue
        if pod_controller(pod, sets)["uid"] not in excluded:
            incoming.append(pod)
    blockers = m.scheduling_blockers(WORKER, nodes[PEER], incoming, plan["reviewed_csi_sockets"])
    selected = [p for p in pods if p["spec"].get("nodeName") == WORKER and m.evictable(p)
                and not (p["metadata"]["namespace"] == policy["storage_namespace"] and p["metadata"]["name"] == IM_NAMES[WORKER])
                and pod_controller(p, sets)["uid"] not in cold_uids]
    for pod in selected:
        for pdb in api.items("poddisruptionbudgets"):
            if pdb["metadata"]["namespace"] == pod["metadata"]["namespace"] and m.matches(pdb["spec"].get("selector"), pod["metadata"].get("labels", {})):
                if identity(pdb) in TEMP_PDBS:
                    continue
                if pdb.get("status", {}).get("observedGeneration", 0) < pdb["metadata"]["generation"] or pdb["status"].get("disruptionsAllowed", 0) < 1:
                    blockers.append("Unreviewed PDB blocks normal eviction: " + "/".join(identity(pdb)))
    return sorted(set(blockers))


def migration_snapshot(plan, policy, api):
    sets = {r["metadata"]["uid"]: r for r in api.items("replicasets")}
    excluded = {r["uid"] for r in plan["controllers"]} | {r["uid"] for r in plan["ha"]}
    pods, controllers, claims = [], {}, {}
    for pod in api.items("pods"):
        if pod["spec"].get("nodeName") != WORKER or not m.evictable(pod):
            continue
        if pod["metadata"]["namespace"] == policy["storage_namespace"] and pod["metadata"]["name"] == IM_NAMES[WORKER]:
            continue
        owner = pod_controller(pod, sets)
        if owner["uid"] in excluded:
            continue
        require(owner["kind"] in ("Deployment", "StatefulSet"), "Unreviewed non-migratable controller")
        ns = pod["metadata"]["namespace"]
        control = api.get(RESOURCES[owner["kind"]], ns, owner["name"])
        require(control["metadata"]["uid"] == owner["uid"], "Migration controller identity drift")
        row = replica_binding(control)
        controllers[key(row)] = row
        pods.append({"namespace": ns, "name": pod["metadata"]["name"], "uid": pod["metadata"]["uid"],
                     "owner": owner, "spec_sha256": m.pending_spec_hash(pod)})
        for volume in pod["spec"].get("volumes", []):
            claim = volume.get("persistentVolumeClaim", {}).get("claimName")
            if not claim:
                continue
            pvc = api.get("persistentvolumeclaims", ns, claim)
            require(pvc["spec"].get("volumeName"), "Unbound migration PVC")
            pv = api.get("persistentvolumes", name=pvc["spec"]["volumeName"])
            require(not any(pv["spec"].get(k) for k in ("local", "hostPath", "nodeAffinity")),
                    "Locality-constrained migration PVC requires a separate reviewed contract")
            claims[(ns, claim)] = {"namespace": ns, "name": claim, "uid": pvc["metadata"]["uid"],
                                   "spec_sha256": sha(canonical(pvc["spec"])), "pv": pv["metadata"]["name"],
                                   "pv_uid": pv["metadata"]["uid"], "pv_spec_sha256": sha(canonical(pv["spec"]))}
    return {"pods": sorted(pods, key=lambda r: (r["namespace"], r["name"])),
            "controllers": sorted(controllers.values(), key=key),
            "claims": sorted(claims.values(), key=lambda r: (r["namespace"], r["name"]))}


class Journal:
    """Root-owned fsynced records; a flock serializes guard and executor mutations."""
    def __init__(self, directory):
        self.directory = Path(directory)
        self.path = self.directory / "state.json"

    def read(self):
        return json.loads(self.path.read_text())

    @staticmethod
    def discard_uncommitted(path):
        if path.exists() or path.is_symlink():
            meta = path.lstat()
            require(stat.S_ISREG(meta.st_mode) and meta.st_uid == 0, "Untrusted pending journal artifact")
            # No API mutation is allowed until write-ahead os.replace returns.
            path.unlink()

    def write(self, state):
        new = self.directory / "state.next"
        self.discard_uncommitted(new)
        with new.open("x", encoding="utf-8") as stream:
            os.chmod(new, 0o600)
            stream.write(json.dumps(state, sort_keys=True))
            stream.flush()
            os.fsync(stream.fileno())
        os.replace(new, self.path)
        fd = os.open(self.directory, os.O_RDONLY | os.O_DIRECTORY)
        try:
            os.fsync(fd)
        finally:
            os.close(fd)
        status_directory = Path("/var/lib/node-maintenance-cold-status")
        status_directory.mkdir(mode=0o755, exist_ok=True)
        require(status_directory.stat().st_uid == 0 and stat.S_IMODE(status_directory.stat().st_mode) == 0o755
                and not status_directory.is_symlink(), "Status receipt directory is not trusted")
        status = {k: state[k] for k in ("run_id", "plan_sha256", "mode", "phase", "started_epoch", "deadline_epoch",
                                       "guard_registered", "host_step_id", "host_started", "host_complete", "completed")}
        status["host_slot_cancelled"] = state.get("host_slot_cancelled", False)
        status["host_packages_complete"] = state.get("host_packages_complete", False)
        status["host_reboot_authorized"] = state.get("host_reboot_authorized", False)
        for name in ("last_epoch", "guard_verified_epoch", "worker_contract", "worker_contract_sha256",
                     "live_source_bindings", "host_native_gate_epoch"):
            status[name] = state.get(name)
        status["failure_type"] = state.get("failure_type")
        status["failure_stage"] = state.get("failure_stage")
        status["failure_reason"] = state.get("failure_reason")
        path = status_directory / (state["run_id"] + ".next")
        self.discard_uncommitted(path)
        with path.open("x", encoding="utf-8") as stream:
            stream.write(json.dumps(status))
            stream.flush()
            os.fsync(stream.fileno())
            os.fchmod(stream.fileno(), 0o444)
        os.replace(path, status_directory / (state["run_id"] + ".json"))

    def transaction(self, callback):
        import fcntl
        with (self.directory / "mutex").open("a") as mutex:
            fcntl.flock(mutex, fcntl.LOCK_EX)
            state = self.read()
            result = callback(state)
            self.write(state)
            return result

    def recovery(self, callback):
        import fcntl
        with (self.directory / "recovery-mutex").open("a") as mutex:
            fcntl.flock(mutex, fcntl.LOCK_EX)
            return callback()


class Coordinator:
    def __init__(self, plan, approval, policy, journal, api=None, now=None):
        self.plan, self.approval, self.policy, self.journal = plan, approval, policy, journal
        self.api, self.now = api or API(), now or time.time

    def gate(self, state, disrupting=False):
        require(sha(canonical(self.plan)) == self.approval and state["plan_sha256"] == self.approval
                and state["deadline_epoch"] == state["started_epoch"] + 43200,
                "Journal approval/deadline changed; never reset or extend clock")
        require(self.now() >= state["last_epoch"] >= state["started_epoch"], "Clock rollback; protection retained")
        state["live_source_bindings"] = source_bindings()
        require(state["live_source_bindings"] == self.plan["source_bindings"], "Live controller bound source drift")
        state["last_epoch"] = self.now()
        require(state["guard_registered"] is True, "Verified independent recovery guard required BEFORE first pause")
        lock = self.api.get("configmap", "kube-system", m.LOCK)
        require(lock["data"]["run_id"] == state["run_id"] and lock["data"]["plan_sha256"] == self.approval
                and float(lock["data"]["started_epoch"]) == state["started_epoch"]
                and float(lock["data"]["deadline_epoch"]) == state["deadline_epoch"]
                and lock["data"].get("host_step_id") == state["host_step_id"], "Maintenance lock ownership/deadline/host-slot drift")
        require(self.plan["cluster_uid"] == self.api.get("namespace", name="kube-system")["metadata"]["uid"],
                "Recovery context drift")
        if disrupting:
            require(state["mode"] == "disrupting" and self.now() < state["deadline_epoch"] - 7200,
                    "Recovery reserve/request reached; no further disruption")
            require(state.get("worker_ready_verified") is True, "Exact live worker standby required BEFORE first pause")

    def phase(self, value):
        def update(state):
            self.gate(state)
            state["phase"] = value
        self.journal.transaction(update)

    def wait(self, check, seconds=600, disrupting=False):
        end = time.monotonic() + seconds
        while True:
            self.journal.transaction(lambda state: self.gate(state, disrupting))
            try:
                return check()
            except RuntimeError:
                if time.monotonic() >= end:
                    raise
                time.sleep(5)

    def mutate(self, row, field, value, restoring=False):
        def action(state):
            self.gate(state, not restoring)
            obj = self.api.get(RESOURCES[row["kind"]], row["namespace"], row["name"])
            if (row["namespace"], row["name"]) == CROSS_PRODUCER:
                mount_handler_contract(obj)
                mount_handler_scope(self.api)
            if field == "replicas":
                if (not restoring and (row["namespace"], row["name"]) not in OPERATORS
                        or restoring and not state.get("producer_warmup_complete")
                        and any(entry["row"]["kind"] == "CronJob"
                                and (entry["row"]["namespace"], entry["row"]["name"]) == CROSS_PRODUCER
                                for entry in state["mutations"].values())):
                    self.mount_handler_gated()
            mutable = ("replicas",) if field == "replicas" else ("suspend",) if field == "suspend" else ()
            if field == "budget":
                mutable = ("minAvailable", "maxUnavailable")
            assert_binding(obj, row, mutable)
            if field == "pause":
                annotations = obj["metadata"].get("annotations", {})
                expected = row["original"]["pause_annotations"]
                for name in PAUSE:
                    require(annotations.get(name) in (expected.get(name), "true"),
                            "Foreign autoscaler annotation drift")
            if field in ("replicas", "suspend"):
                require(obj["spec"].get(field, 1 if field == "replicas" else False)
                        in (row["original"][field], 0 if field == "replicas" else True),
                        "Original or coordinator-owned state required; no foreign drift overwrite")
            entry = state["mutations"].setdefault(key(row), {"row": row, "fields": []})
            if field not in entry["fields"]:
                entry["fields"].append(field)
            # Persist intention BEFORE the Kubernetes patch, including an ambiguous API response.
            self.journal.write(state)
            spec = obj["spec"]
            if field in ("replicas", "suspend"):
                present = not restoring or row["original"].get(field + "_present", True)
                changes = ([{"op": "add", "path": "/spec/" + field, "value": value}] if present else
                           [{"op": "remove", "path": "/spec/" + field}] if field in spec else [])
            elif field == "pause":
                annotations = obj["metadata"].get("annotations", {})
                require(not any(k in annotations for k in FULL_PAUSE), "Foreign full KEDA pause appeared")
                changes = []
                if "annotations" not in obj["metadata"]:
                    changes.append({"op": "add", "path": "/metadata/annotations", "value": {}})
                for name in PAUSE:
                    path = "/metadata/annotations/" + name.replace("/", "~1")
                    original = row["original"]["pause_annotations"]
                    if restoring and name not in original:
                        if name in annotations:
                            changes.append({"op": "remove", "path": path})
                    else:
                        changes.append({"op": "add", "path": path,
                                        "value": original[name] if restoring else "true"})
            else:
                require(field == "budget" and (row["namespace"], row["name"]) in TEMP_PDBS,
                        "No native budget write path")
                require((spec.get("minAvailable") == "50%" and "maxUnavailable" not in spec)
                        or ("minAvailable" not in spec and type(spec.get("maxUnavailable")) is int
                            and spec["maxUnavailable"] == 1), "Foreign PDB availability state; no overwrite")
                if restoring:
                    changes = ([{"op": "remove", "path": "/spec/maxUnavailable"}] if "maxUnavailable" in spec else [])
                    changes.append({"op": "add", "path": "/spec/minAvailable", "value": "50%"})
                else:
                    changes = ([{"op": "remove", "path": "/spec/minAvailable"}] if "minAvailable" in spec else [])
                    changes.append({"op": "add", "path": "/spec/maxUnavailable", "value": 1})
            if changes:
                self.api.patch(row, obj, changes)
        self.journal.transaction(action)

    def controllers_stopped(self, rows):
        pods = self.api.items("pods")
        sets = {r["metadata"]["uid"]: r for r in self.api.items("replicasets")}
        for row in rows:
            obj = self.api.get(RESOURCES[row["kind"]], row["namespace"], row["name"])
            assert_binding(obj, row, ("replicas",))
            require(obj["spec"].get("replicas") == 0
                    and obj["status"].get("observedGeneration", 0) >= obj["metadata"]["generation"]
                    and obj["status"].get("replicas", 0) == 0 and obj["status"].get("readyReplicas", 0) == 0,
                    key(row) + ": controller has not observed zero")
            for pod in pods:
                if (pod["metadata"]["namespace"] == row["namespace"]
                        and pod["status"].get("phase") not in ("Succeeded", "Failed")
                        and pod_controller(pod, sets)["uid"] == row["uid"]):
                    raise RuntimeError(key(row) + ": actual Pod disappearance required (including terminating Pods)")

    def producers_gated(self):
        for row in self.plan["schedules"]:
            obj = self.api.get("cronjobs", row["namespace"], row["name"])
            assert_binding(obj, row, ("suspend",))
            require(obj["spec"].get("suspend") is True and not obj.get("status", {}).get("active"),
                    "Scheduled producer not suspended/idle")
        require(not [j for j in self.api.items("jobs") if j["metadata"]["namespace"] in ALLOWED
                     and (j.get("status", {}).get("active", 0) or not any(
                         c["type"] in ("Complete", "Failed") and c["status"] == "True"
                         for c in j.get("status", {}).get("conditions", [])))],
                "Active/new Job prevents cold stop")
        self.mount_handler_gated()
        for row in self.plan["reconciliation"]:
            obj = self.api.get(RESOURCES[row["kind"]], row["namespace"], row["name"])
            assert_binding(obj, row, ("suspend",))
            require(obj["spec"].get("suspend") is True, "Reconciliation was not frozen")
            require(not any(c["type"] == "Reconciling" and c["status"] == "True"
                            for c in obj.get("status", {}).get("conditions", [])), "In-flight GitOps reconciliation")
        for row in self.plan["parents"] + self.plan["http"]:
            assert_binding(self.api.get(RESOURCES[row["kind"]], row["namespace"], row["name"]), row)

    def mount_handler_gated(self):
        row = next(r for r in self.plan["schedules"] if (r["namespace"], r["name"]) == CROSS_PRODUCER)
        cron = self.api.get("cronjobs", *CROSS_PRODUCER)
        assert_binding(cron, row, ("suspend",))
        mount_handler_contract(cron)
        mount_handler_scope(self.api)
        require(cron["spec"].get("suspend") is True and not cron.get("status", {}).get("active"),
                "media-mount-handler is not suspended/idle")
        root = next(r for r in self.plan["reconciliation"] if
                    (r["kind"], r["namespace"], r["name"]) == ("Kustomization", "flux-system", "flux-system"))
        reconciler = self.api.get(RESOURCES[root["kind"]], root["namespace"], root["name"])
        assert_binding(reconciler, root, ("suspend",))
        require(reconciler["spec"].get("suspend") is True and not any(
            c["type"] == "Reconciling" and c["status"] == "True" for c in reconciler.get("status", {}).get("conditions", [])),
            "media-mount-handler root reconciliation is not frozen/idle")
        children = {}
        for job in self.api.items("jobs", CROSS_PRODUCER[0]):
            own = owners(job)
            template = job["spec"].get("template", {}).get("spec", {})
            if (template.get("serviceAccountName") != CROSS_PRODUCER[1]
                    and not any(o["kind"] == "CronJob" and (o["name"] == row["name"] or o["uid"] == row["uid"])
                                for o in own)):
                continue
            require(own == [{"kind": "CronJob", "name": row["name"], "uid": row["uid"]}]
                    and template.get("serviceAccountName") == CROSS_PRODUCER[1],
                    "Foreign/unbound media-mount-handler Job")
            children[job["metadata"]["name"]] = job
            status = job.get("status", {})
            require(not status.get("active") and not status.get("terminating")
                    and any(c["type"] in ("Complete", "Failed") and c["status"] == "True"
                            for c in status.get("conditions", [])),
                    "media-mount-handler Job has not finished; never delete it")
        child_uids = {job["metadata"]["uid"] for job in children.values()}
        for pod in self.api.items("pods", CROSS_PRODUCER[0]):
            own = owners(pod)
            if (pod["spec"].get("serviceAccountName") != CROSS_PRODUCER[1]
                    and not any(o["kind"] == "Job" and (o["name"] in children or o["uid"] in child_uids) for o in own)):
                continue
            parent = children.get(own[0]["name"]) if len(own) == 1 and own[0]["kind"] == "Job" else None
            require(parent and own[0]["uid"] == parent["metadata"]["uid"]
                    and pod["spec"].get("serviceAccountName") == CROSS_PRODUCER[1],
                    "Foreign/unbound media-mount-handler Pod/Job identity")
            require(pod["status"].get("phase") in ("Succeeded", "Failed")
                    and all("terminated" in s.get("state", {}) for s in
                            pod["status"].get("containerStatuses", []) + pod["status"].get("initContainerStatuses", [])),
                    "media-mount-handler Pod has not finished; never delete it")

    def hpas_paused(self):
        for row in self.plan["autoscalers"]:
            obj = self.api.get(RESOURCES[row["kind"]], row["namespace"], row["name"])
            assert_binding(obj, row)
            require(all(obj["metadata"].get("annotations", {}).get(k) == "true" for k in PAUSE)
                    and any(c["type"] == "Paused" and c["status"] == "True"
                            for c in obj.get("status", {}).get("conditions", [])), "KEDA native pause not observed")
        for row in self.plan["hpas"]:
            obj = self.api.get("horizontalpodautoscalers", row["namespace"], row["name"])
            require(obj["metadata"]["uid"] == row["uid"] and owners(obj) == row["controllers"],
                    "HPA identity/ownership drift; full pause is not allowed")
            spec = copy.deepcopy(obj["spec"])
            behavior = spec.get("behavior", {})
            require(all(behavior.get(k, {}).get("selectPolicy") == "Disabled" for k in ("scaleUp", "scaleDown")),
                    "Both native HPA directions must actually be Disabled")
            require(sha(canonical(obj["metadata"].get("ownerReferences", []))) == row["owner_references_sha256"],
                    "HPA owner reference drift")
            paused = {**row, "spec_sha256": row["paused_spec_sha256"]}
            assert_binding(obj, paused)

    def infrastructure(self, drained=False):
        self.api.kube("get", "--raw=/readyz")
        m.check_nodes(self.policy, self.api.items("nodes"), WORKER if drained else None)
        require(self.api.get("helmrelease", "monitoring", "kured")["spec"].get("suspend") is True, "Kured protection drift")
        self.producers_gated()
        self.hpas_paused()
        cold_storage(self.plan, self.policy, self.api, detached=True)
        pods = self.api.items("pods")
        sets = {r["metadata"]["uid"]: r for r in self.api.items("replicasets")}
        allowed = expected_pending(self.plan["ha"], pods, sets, self.api.get) if drained else set()
        bad = [p for p in pods if p["status"].get("phase") not in ("Succeeded", "Failed")
               and not m.pod_ready(p) and p["metadata"]["uid"] not in allowed]
        require(not bad, "Unreviewed non-Ready infrastructure Pod")
        managers = [p for p in pods if p["metadata"]["namespace"] == self.policy["storage_namespace"]
                    and p["metadata"].get("labels", {}).get("app") == "longhorn-manager"]
        require(len(managers) == 2 and all(m.pod_ready(p) for p in managers)
                and {p["spec"].get("nodeName") for p in managers} == {WORKER, PEER},
                "Both native worker Longhorn DAEMONSET managers must be Ready")
        if drained:
            native_gate(self.plan, self.policy, self.api, released=True)
            require(not [p for p in pods if p["spec"].get("nodeName") == WORKER and m.evictable(p)],
                    "Actual normal eviction is incomplete")

    def execute(self):
        check_plan(self.plan, self.approval, self.policy, self.api)
        try:
            self.journal.transaction(lambda state: self.gate(state, True))
            for row in (self.plan["controllers"] + self.plan["reconciliation"] + self.plan["schedules"]
                        + self.plan["autoscalers"] + self.plan["hpas"] + self.plan["temporary_pdbs"]):
                assert_binding(self.api.get(RESOURCES[row["kind"]], row["namespace"], row["name"]), row)
            cold_storage(self.plan, self.policy, self.api)
            native_gate(self.plan, self.policy, self.api)
            state = self.journal.read()
            worker_ready(self.api.worker_status(self.policy, state["run_id"]), state["worker_contract"], self.now())
            for row in sorted(self.plan["reconciliation"], key=lambda r: (
                    (r["kind"], r["namespace"], r["name"]) != ("Kustomization", "flux-system", "flux-system"),
                    r["kind"] != "Kustomization", key(r))):
                self.mutate(row, "suspend", True)
            for row in self.plan["schedules"]:
                self.mutate(row, "suspend", True)
            self.wait(self.producers_gated, disrupting=True)
            for row in self.plan["autoscalers"]:
                self.mutate(row, "pause", True)
            self.wait(self.hpas_paused, disrupting=True)
            operators = [r for r in self.plan["controllers"] if (r["namespace"], r["name"]) in OPERATORS]
            operators.sort(key=lambda r: (r["name"] != "keda-operator", key(r)))
            for row in operators:
                self.mutate(row, "replicas", 0)
                self.wait(lambda r=row: self.controllers_stopped([r]), disrupting=True)
            self.phase("operators-stopped")
            consumers = [r for r in self.plan["controllers"]
                         if (r["namespace"], r["name"]) not in OPERATORS | BACKENDS]
            backends = [r for r in self.plan["controllers"] if (r["namespace"], r["name"]) in BACKENDS]
            for rows, phase in ((consumers, "consumers-stopped"), (backends, "cold-detached")):
                self.producers_gated()
                self.hpas_paused()
                for row in rows:
                    self.mutate(row, "replicas", 0)
                self.wait(lambda: self.controllers_stopped(rows), disrupting=True)
                self.phase(phase)
            self.wait(lambda: self.infrastructure(), disrupting=True)
            for row in self.plan["temporary_pdbs"]:
                self.mutate(row, "budget", 1)
            self.wait(self.temporary_budgets_ready, disrupting=True)
            self.journal.transaction(lambda state: self.cordon(state))
            self.wait(lambda: native_gate(self.plan, self.policy, self.api, released=True), disrupting=True)
            self.drain()
            self.wait(lambda: self.infrastructure(drained=True), disrupting=True)
            self.phase("awaiting-host")
            self.authorize_host()
            self.wait(lambda: self.host_completed(), seconds=36000, disrupting=True)
            self.recover(success=True)
        except BaseException:
            self.request_recovery("executor-interrupted-or-step-failed")
            self.recover()
            raise

    def cordon(self, state):
        self.gate(state, True)
        require(not cold_scheduling(self.plan, self.policy, self.api), "Fresh cold capacity/scheduling/PDB blockers")
        require(migration_snapshot(self.plan, self.policy, self.api) == self.plan["migration"],
                "Selected migration Pod/controller/PVC identity or specification drift")
        cold_storage(self.plan, self.policy, self.api, detached=True)
        state["cordon_intent"] = True
        self.journal.write(state)
        self.api.kube("cordon", WORKER)

    def temporary_budgets_ready(self):
        for row in self.plan["temporary_pdbs"]:
            obj = self.api.get("poddisruptionbudgets", row["namespace"], row["name"])
            assert_binding(obj, row, ("minAvailable", "maxUnavailable"))
            require(obj["status"].get("observedGeneration", 0) >= obj["metadata"]["generation"]
                    and obj["status"].get("disruptionsAllowed", 0) >= 1, "Temporary singleton budget is not natively available")
            workload = row["workload"]
            assert_binding(self.api.get("deployments", workload["namespace"], workload["name"]), workload)

    def drain(self):
        self.phase("draining")
        def launch(state):
            self.gate(state, True)
            command = getattr(self.api, "kubectl_command", ["kubectl"])
            process = subprocess.Popen([*command, "--request-timeout=30s", "drain", WORKER,
                                        "--ignore-daemonsets", "--delete-emptydir-data", "--timeout=10m"],
                                       stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)
            state["drain_pid"] = process.pid
            return process
        process = self.journal.transaction(launch)
        # The guard may request recovery, but never kills a drain/dpkg/RKE2 process.
        code = process.wait()
        self.journal.transaction(lambda state: state.update(drain_pid=None))
        require(code == 0, "Normal drain failed; recovery required, no forced eviction")
        self.journal.transaction(lambda state: self.gate(state, True))

    def authorize_host(self):
        def mark(state):
            self.gate(state, True)
            require(state["phase"] == "awaiting-host" and not state["host_started"]
                    and not state.get("host_slot_cancelled"), "Exact single host slot not available")
            # Guard/recovery uses the same mutex; publish authorization only after
            # ALL fresh native storage, released PDB, actual drain and HA gates.
            self.infrastructure(drained=True)
            worker_ready(self.api.worker_status(self.policy, state["run_id"]),
                         state["worker_contract"], self.now())
            self.gate(state, True)
            state.update(host_started=True, host_started_epoch=self.now(), host_reboot_authorized=True,
                         host_native_gate_epoch=self.now())
        self.journal.transaction(mark)

    def ingest_worker_receipt(self, receipt):
        def finish(state):
            self.gate(state)
            require(state["host_started"] and receipt["run_id"] == state["host_step_id"]
                    and receipt["name"] == WORKER and receipt["operation"] == "os"
                    and receipt["old_boot_id"] == self.plan["bindings"][WORKER]["boot_id"]
                    and receipt["expected_kernel"] == self.policy["kernel"]
                    and receipt["headless"] is True and receipt["package_removals"] is False
                    and receipt["contract_sha256"] == state["worker_contract_sha256"]
                    and state["host_started_epoch"] <= receipt["completed_epoch"] <= self.now(),
                    "Invalid exact root-owned worker2 package receipt")
            if state.get("host_packages_complete"):
                require(state["host_receipt"] == receipt, "Worker terminal receipt replay/drift")
            state.update(host_packages_complete=True, host_packages_complete_epoch=receipt["completed_epoch"],
                         host_receipt=receipt)
        self.journal.transaction(finish)

    def observe_worker(self):
        state = self.journal.read()
        proof = self.api.worker_status(self.policy, state["run_id"])
        record = proof["record"]
        verify_worker(record, state["worker_contract"])
        require(proof["unit_sha256"] in (sha(worker_unit(state["worker_contract"]).encode()), None),
                "Worker standby unit source drift")
        if record["phase"] in ("staged", "waiting", "claimed") and (
                proof["unit"]["LoadState"] != "loaded"
                or proof["unit"]["ActiveState"] in ("inactive", "failed")):
            self.request_recovery("worker-standby-native-exit-without-terminal-ack")
        if record.get("receipt"):
            self.ingest_worker_receipt(record["receipt"])
        return record

    def host_completed(self):
        record = self.observe_worker()
        if record["phase"] in ("failed", "cancelled"):
            self.request_recovery("worker-standby-revoked-or-host-failed")
            raise RuntimeError("Worker standby terminal failure; original-state recovery required")
        if record["phase"] in ("waiting", "claimed"):
            try:
                self.infrastructure(drained=True)
                def refresh(state):
                    self.gate(state, True)
                    state["host_native_gate_epoch"] = self.now()
                self.journal.transaction(refresh)
            except RuntimeError:
                self.request_recovery("native-peer-or-infrastructure-drift-in-host-slot")
                raise
        state = self.journal.read()
        require(state.get("host_packages_complete"), "Awaiting exact autonomous worker2 package receipt")
        # Transient host units disappear across reboot. Their absence cannot
        # replace the new boot proof, nor veto a genuinely verified new boot.
        self.api.host_idle(self.policy, state["host_step_id"], state["run_id"])
        host = self.api.host_audit(self.policy, WORKER)
        if host["boot_id"] == self.plan["bindings"][WORKER]["boot_id"]:
            if self.now() - state["host_packages_complete_epoch"] > 120:
                self.request_recovery("worker2-reboot-refused-or-failed")
            raise RuntimeError("Package completion is not reboot completion; awaiting new worker2 boot")
        require(host["kernel"] == self.policy["kernel"] and host["default_target"] == "multi-user.target",
                "Worker2 did not boot approved OS/kernel")
        def finish(s):
            self.gate(s, True)
            s.update(host_complete=True, host_reboot_authorized=False)
        self.journal.transaction(finish)
        return True

    def request_recovery(self, reason):
        def request(state):
            if not state.get("completed"):
                state["mode"] = "recovering"
                state["failure"] = reason
                state["host_reboot_authorized"] = False
                state["host_slot_cancelled"] = True
        self.journal.transaction(request)

    def replicas_ready(self, row):
        obj = self.api.get(RESOURCES[row["kind"]], row["namespace"], row["name"])
        assert_binding(obj, row, ("replicas",))
        original = row["original"]["replicas"]
        require(obj["spec"].get("replicas", 1) == original
                and obj["status"].get("observedGeneration", 0) >= obj["metadata"]["generation"]
                and obj["status"].get("readyReplicas", 0) == original
                and obj["status"].get("replicas", 0) == original, key(row) + ": original replicas not Ready")
        if original == 0:
            self.controllers_stopped([row])

    def recover(self, success=False):
        return self.journal.recovery(lambda: self._recover(success))

    def _recover(self, success=False):
        state = self.journal.read()
        if state.get("completed"):
            return
        self.request_recovery(state.get("failure") or "restoring")
        self.phase("restoring-storage-gate")
        # Every warming action still requires two protected copies; no unsafe force restoration.
        cold_storage(self.plan, self.policy, self.api)
        if state.get("drain_pid"):
            self.phase("waiting-normal-drain")
            try:
                os.kill(state["drain_pid"], 0)
            except ProcessLookupError:
                self.journal.transaction(lambda s: s.update(drain_pid=None))
            else:
                raise RuntimeError("Normal drain still running; guard will retry restoration, no process kill")
        self.phase("waiting-worker-standby-revocation")
        record = self.observe_worker()
        # A stale authorized read can race with revocation. Only a terminal
        # local acknowledgment under the worker mutex excludes a future claim.
        require(record["phase"] in ("cancelled", "failed", "packages-complete"),
                "Worker standby has not acknowledged irrevocable slot revocation; no warming")
        observed = self.api.host_idle(self.policy, state["host_step_id"], state["run_id"])
        node = next(n for n in self.api.items("nodes") if n["metadata"]["name"] == WORKER)
        self.phase("waiting-worker2-ready")
        m.check_nodes(self.policy, self.api.items("nodes"), WORKER if node["spec"].get("unschedulable") else None)
        require(self.api.get("helmrelease", "monitoring", "kured")["spec"].get("suspend") is True,
                "Kured must remain suspended during recovery")
        require(any(c["type"] == "Ready" and c["status"] == "True" for c in node["status"]["conditions"]),
                "Worker2 host unavailable; two-copy protection retained, no unsafe warmup")
        if state.get("host_started"):
            self.phase("validating-worker2-boot")
            host = self.api.host_audit(self.policy, WORKER)
            rebooted = host["boot_id"] != self.plan["bindings"][WORKER]["boot_id"]
            if not observed and not rebooted and not state.get("host_packages_complete"):
                require(self.now() - state["host_started_epoch"] > 120,
                        "Host launch handoff pending; guard will retry")
            if rebooted:
                require(host["kernel"] == self.policy["kernel"] and host["default_target"] == "multi-user.target",
                        "Worker2 did not boot approved OS/kernel; retain recovery lock")
            require(not state.get("host_complete") or rebooted, "Verified worker2 boot identity regressed")
            self.journal.transaction(lambda s: s.update(host_complete=rebooted,
                                                       host_reboot_outcome="verified" if rebooted else "not-rebooted"))
        require(not success or self.journal.read().get("host_complete"),
                "Maintenance success requires actual approved worker2 reboot completion")
        # Re-enable only worker2, with all regular volumes still cold after an attempted drain/host step.
        if state.get("cordon_intent") and not state.get("uncordoned"):
            self.phase("waiting-native-return-protection")
            cold_storage(self.plan, self.policy, self.api, detached=True)
            self.journal.transaction(lambda s: self.uncordon(s))
            self.wait(lambda: native_gate(self.plan, self.policy, self.api, returned=True))
        native_gate(self.plan, self.policy, self.api, returned=bool(state.get("cordon_intent")))
        entries = self.journal.read()["mutations"]
        changed = lambda r: key(r) in entries
        handler = next(r for r in self.plan["schedules"] if (r["namespace"], r["name"]) == CROSS_PRODUCER)
        if changed(handler):
            if state.get("producer_warmup_complete"):
                self.wait(self.full_health)
            else:
                self.phase("waiting-media-mount-handler")
                self.wait(self.mount_handler_gated)
        backends = [r for r in self.plan["controllers"] if (r["namespace"], r["name"]) in BACKENDS and changed(r)]
        consumers = [r for r in self.plan["controllers"] if (r["namespace"], r["name"]) not in BACKENDS | OPERATORS and changed(r)]
        operators = [r for r in self.plan["controllers"] if (r["namespace"], r["name"]) in OPERATORS and changed(r)]
        operators.sort(key=lambda r: (r["name"] == "keda-operator", key(r)))
        for rows, phase in ((backends, "restoring-backends"), (consumers, "restoring-consumers")):
            self.phase(phase)
            for row in rows:
                cold_storage(self.plan, self.policy, self.api)
                self.mutate(row, "replicas", row["original"]["replicas"], restoring=True)
            for row in rows:
                self.wait(lambda r=row: self.replicas_ready(r))
        for row in self.plan["autoscalers"]:
            if changed(row):
                self.mutate(row, "pause", False, restoring=True)
        for row in operators:
            self.mutate(row, "replicas", row["original"]["replicas"], restoring=True)
        for row in operators:
            self.wait(lambda r=row: self.replicas_ready(r))
        self.wait(self.autoscaling_restored)
        for row in self.plan["temporary_pdbs"]:
            if changed(row):
                self.mutate(row, "budget", None, restoring=True)
        # Health while schedules/GitOps remain frozen; then exact original reconciliation states.
        self.wait(self.full_health)
        if changed(handler) and not state.get("producer_warmup_complete"):
            self.mount_handler_gated()
        # A retry after an ambiguous unsuspend must not mistake our completed
        # warmup for an unfinished cold phase and require a new outage.
        self.journal.transaction(lambda s: s.update(producer_warmup_complete=True))
        for row in self.plan["schedules"] + list(reversed(self.plan["reconciliation"])):
            if changed(row):
                self.mutate(row, "suspend", row["original"]["suspend"], restoring=True)
        self.wait(self.original_state)
        for _ in range(3):
            self.full_health()
            time.sleep(10)
        self.journal.transaction(lambda s: self.complete(s, success))

    def uncordon(self, state):
        self.gate(state)
        self.api.kube("uncordon", WORKER)
        state["uncordoned"] = True

    def autoscaling_restored(self):
        for row in self.plan["autoscalers"]:
            obj = self.api.get(RESOURCES[row["kind"]], row["namespace"], row["name"])
            assert_binding(obj, row)
            require({k: v for k, v in obj["metadata"].get("annotations", {}).items() if k in PAUSE}
                    == row["original"]["pause_annotations"], "Original KEDA annotations not restored")
        for row in self.plan["hpas"]:
            obj = self.api.get("horizontalpodautoscalers", row["namespace"], row["name"])
            assert_binding(obj, row)
            require(sha(canonical(obj["metadata"].get("ownerReferences", []))) == row["owner_references_sha256"],
                    "Original HPA owner references not restored")

    def original_state(self):
        for row in self.plan["controllers"]:
            self.replicas_ready(row)
        self.autoscaling_restored()
        for row in self.plan["schedules"] + self.plan["reconciliation"] + self.plan["temporary_pdbs"]:
            obj = self.api.get(RESOURCES[row["kind"]], row["namespace"], row["name"])
            assert_binding(obj, row)
            if (row["namespace"], row["name"]) == CROSS_PRODUCER:
                mount_handler_contract(obj)
                mount_handler_scope(self.api)
            if row["kind"] == "PodDisruptionBudget":
                require(obj["status"].get("observedGeneration", 0) >= obj["metadata"]["generation"],
                        "Original singleton PDB restoration is not observed")

    def full_health(self):
        cold_storage(self.plan, self.policy, self.api)
        native_gate(self.plan, self.policy, self.api, returned=bool(self.journal.read().get("cordon_intent")))
        self.original_controllers()
        self.ha_restored()
        for row in self.plan["temporary_pdbs"]:
            self.replicas_ready(row["workload"])
        for row in self.plan["migration"]["controllers"]:
            self.replicas_ready(row)
        for row in self.plan["migration"]["claims"]:
            pvc = self.api.get("persistentvolumeclaims", row["namespace"], row["name"])
            pv = self.api.get("persistentvolumes", name=row["pv"])
            require(pvc["metadata"]["uid"] == row["uid"] and sha(canonical(pvc["spec"])) == row["spec_sha256"]
                    and pv["metadata"]["uid"] == row["pv_uid"] and sha(canonical(pv["spec"])) == row["pv_spec_sha256"],
                    "Migrated claim/PV/locality binding drift")
        m.health({**self.policy, "_quarantine": self.plan["quarantine"]})
        for url in self.plan["application_probes"]:
            with m.urllib.request.urlopen(url, timeout=15) as response:
                require(response.status == 200, "Full native HTTPS application probe failed")

    def ha_restored(self):
        pods = self.api.items("pods")
        sets = {r["metadata"]["uid"]: r for r in self.api.items("replicasets")}
        for row in self.plan["ha"]:
            self.replicas_ready(row)
            obj = self.api.get("deployments", row["namespace"], row["name"])
            children = [p for p in pods if p["metadata"]["namespace"] == row["namespace"] and m.evictable(p)
                        and pod_controller(p, sets)["uid"] == row["uid"]]
            require(len(children) == 2 and all(m.pod_ready(p) for p in children)
                    and {p["spec"].get("nodeName") for p in children} == {WORKER, PEER}
                    and any(p["metadata"]["uid"] == row["peer_uid"] and p["spec"].get("nodeName") == PEER for p in children),
                    "Full HA recovery requires both genuine Ready copies and the preserved worker1 peer")
            require(all(m.pending_spec_hash(p) == row["pod_spec_sha256"]
                        and m.matches(obj["spec"]["selector"], p["metadata"].get("labels", {})) for p in children),
                    "Recovered HA Pod/specification/selector drift")
            for replica in row["replicasets"]:
                require(replica["uid"] in sets, "Recovered HA ReplicaSet identity missing")
                assert_binding(sets[replica["uid"]], replica)

    def original_controllers(self):
        for row in self.plan["controllers"]:
            self.replicas_ready(row)

    def complete(self, state, success):
        self.gate(state)
        require(self.now() < state["deadline_epoch"], "Restoration exceeded 12h: report failure, keep protection/lock")
        self.original_state()
        self.full_health()
        self.api.kube("-n", "kube-system", "delete", "configmap", m.LOCK)
        state.update(completed=True, phase="accepted" if success else "recovered-after-failure",
                     mode="complete", completed_epoch=self.now())


def hpa_paused_spec(spec):
    result = copy.deepcopy(spec)
    behavior = result.get("behavior") or {}
    result["behavior"] = behavior
    for direction in ("scaleUp", "scaleDown"):
        value = behavior.get(direction) or {}
        behavior[direction] = value
        value["selectPolicy"] = "Disabled"
        # Kubernetes v1.35.10 autoscaling/v2 defaults, not a normalization of
        # arbitrary live policies. The original FULL spec hash is never changed.
        if direction == "scaleUp" and value.get("stabilizationWindowSeconds") is None:
            value["stabilizationWindowSeconds"] = 0
        if value.get("policies") is None:
            value["policies"] = ([{"type": "Pods", "value": 4, "periodSeconds": 15},
                                 {"type": "Percent", "value": 100, "periodSeconds": 15}] if direction == "scaleUp" else
                                [{"type": "Percent", "value": 100, "periodSeconds": 15}])
    return result


def load_runtime(directory):
    directory = Path(directory)
    require(os.geteuid() == 0 and directory.parent == Path("/var/lib/node-maintenance-cold")
            and re.fullmatch("[a-f0-9]{32}", directory.name), "Controller-owned recovery directory required")
    for path in (directory, directory / "state.json", directory / "plan.json"):
        meta = path.lstat()
        require(meta.st_uid == 0 and not stat.S_ISLNK(meta.st_mode)
                and stat.S_IMODE(meta.st_mode) in (0o700, 0o600), "Recovery artifact ownership/mode drift")
    policy = json.loads(m.POLICY.read_text())
    require(os.uname().nodename == SERVER, "Recovery must run on the unchanged controller")
    # Use the existing root channel in place. Never read/export kubeconfig contents.
    kubectl = "/var/lib/rancher/rke2/bin/kubectl"
    def kube(*args):
        return m.run([kubectl, "--kubeconfig", "/etc/rancher/rke2/rke2.yaml", "--request-timeout=30s", *args])
    m.kube = kube
    api = API()
    api.kube = kube
    api.kubectl_command = [kubectl, "--kubeconfig", "/etc/rancher/rke2/rke2.yaml"]
    # Static methods refer to m.get/items, whose kube function is now controller-local.
    plan = json.loads((directory / "plan.json").read_text())
    require(sha(Path(kubectl).read_bytes()) == plan["controller_client"]["sha256"],
            "Controller native kubectl binary drift; no unbound recovery client")
    journal = Journal(directory)
    approval = journal.read()["plan_sha256"]
    check_plan(plan, approval, policy, api, age=False)
    return Coordinator(plan, approval, policy, journal, api)


def guard(directory, force=False):
    c = load_runtime(directory)
    state = c.journal.read()
    if state.get("completed"):
        return
    if state["mode"] == "registered" and not force and time.time() < state["deadline_epoch"] - 7200:
        cold_storage(c.plan, c.policy, c.api)
        native_gate(c.plan, c.policy, c.api)
        c.journal.transaction(lambda s: s.update(guard_verified_epoch=time.time(),
                                                 live_source_bindings=source_bindings()))
        return
    unit = "node-maintenance-cold-" + state["run_id"] + ".service"
    active = m.run(["systemctl", "show", unit, "-p", "ActiveState", "--value"])
    due = force or state["mode"] == "recovering" or time.time() >= state["deadline_epoch"] - 7200 or active in ("inactive", "failed")
    if due:
        c.request_recovery("explicit-recovery" if force else "reserve-or-executor-loss")
        try:
            c.recover()
        except Exception as error:
            # Our RuntimeError gates contain technical identities, never captured API/SSH output.
            def blocked(s):
                s.update(failure_stage=s["phase"], phase="recovery-blocked", failure_type=type(error).__name__)
                if isinstance(error, RuntimeError):
                    s["failure_reason"] = str(error)[:240]
            c.journal.transaction(blocked)
            raise


def controller_payload(plan, policy, run_id, operation, started, deadline):
    return {
        "operation": operation, "run_id": run_id, "name": SERVER, "role": "server",
        "boot_id": plan["bindings"][SERVER]["boot_id"], "ubuntu_lts": policy["ubuntu_lts"],
        "deadline_epoch": deadline, "recovery_reserve_seconds": 7200, "started_epoch": started,
    }


def request_recovery(run_id, policy):
    require(re.fullmatch("[a-f0-9]{32}", run_id), "Invalid existing cold run ID")
    server = next(n for n in policy["nodes"] if n["name"] == SERVER)
    status = m.remote(server, {"operation": "cold-status", "run_id": run_id})
    require(status["run_id"] == run_id and not status["completed"], "Cold run is already complete")
    host = m.remote(server, {"operation": "audit"})
    # Recovery is allowed after the reserve/deadline, never renews it. The host control
    # path has a separate reserve-free recovery branch below.
    payload = {"operation": "cold-recover", "run_id": run_id, "name": SERVER, "role": "server",
               "boot_id": host["boot_id"], "ubuntu_lts": policy["ubuntu_lts"],
               "deadline_epoch": status["deadline_epoch"], "recovery_reserve_seconds": 7200}
    m.remote(server, payload, privileged=True)


def stage_transport(node, run_id, local):
    incoming = "/home/rishik/.local/state/node-maintenance-cold/" + run_id
    m.run(["ssh", "-T", "-o", "BatchMode=yes", "-o", "StrictHostKeyChecking=yes", node["ssh"],
           shlex.join(["mkdir", "-m", "700", "-p", incoming])])
    m.run(["scp", "-q", "-o", "BatchMode=yes", "-o", "StrictHostKeyChecking=yes",
           str(local / "source.json"), node["ssh"] + ":" + incoming + "/source.json"])
    return incoming + "/source.json"


def apply(plan, approval, policy, recovery_directory, token_confirmed):
    require(sys.stdin.isatty() and sys.stdout.isatty(), "Cold apply requires the existing real operator sudo terminal")
    check_plan(plan, approval, policy)
    require(recovery_directory and recovery_directory.startswith("/") and token_confirmed,
            "Existing protected off-node directory and server-token backup attestation required")
    fresh = build_plan(policy)
    require({k: v for k, v in fresh.items() if k != "created_epoch"}
            == {k: v for k, v in plan.items() if k != "created_epoch"},
            "Fresh exact cold/host/package/client snapshot drifted; approve a new plan")
    server, worker = next(n for n in policy["nodes"] if n["name"] == SERVER), plan["nodes"][0]
    require(m.remote(server, {"operation": "recovery-audit", "recovery_directory": recovery_directory})
            .get("off_node_directory_verified") is True, "Protected controller recovery mount not verified")
    run_id, step_id = uuid.uuid4().hex, uuid.uuid4().hex
    started = time.time()
    deadline = started + 43200
    base = controller_payload(plan, policy, run_id, "cold-register", started, deadline)
    m.kube("-n", "kube-system", "create", "configmap", m.LOCK,
           "--from-literal=run_id=" + run_id, "--from-literal=plan_sha256=" + approval,
           "--from-literal=started_epoch=" + str(started), "--from-literal=deadline_epoch=" + str(deadline),
           "--from-literal=host_step_id=" + step_id)
    print("Cold lock acquired; run_id=" + run_id + "; recovery deadline=" + str(deadline), flush=True)
    try:
        bundle = {name: base64.b64encode((m.ROOT / Path(name)).read_bytes()).decode() for name in BUNDLE_FILES}
        transport = canonical({"plan": plan, "approval": approval, "host_step_id": step_id,
                               "bundle": base64.b64encode(zlib.compress(canonical(bundle))).decode()})
        local = Path.cwd() / ".cold-runs" / run_id
        local.mkdir(parents=True, exist_ok=False)
        (local / "source.json").write_bytes(transport)
        incoming = stage_transport(server, run_id, local)
        m.remote(server, {**base, "input_file": incoming, "input_sha256": sha(transport)}, privileged=True)
        incoming = stage_transport(worker, run_id, local)
        m.remote(worker, {**base, "operation": "cold-worker-register", "name": WORKER, "role": "agent",
                          "boot_id": plan["bindings"][WORKER]["boot_id"], "input_file": incoming,
                          "input_sha256": sha(transport)}, privileged=True)
        contract = worker_contract(plan, policy, run_id, step_id, started, deadline)
        worker_ready(m.remote(worker, {"operation": "cold-worker-status", "run_id": run_id}), contract, time.time())
    except (RuntimeError, OSError, ValueError, subprocess.SubprocessError, KeyboardInterrupt) as error:
        # Still BEFORE cold-start/any pause. The original operator channel may
        # request guarded cancellation; it never releases the lock itself.
        try:
            request_recovery(run_id, policy)
        except (RuntimeError, OSError, ValueError, subprocess.SubprocessError) as recovery_error:
            raise RuntimeError("Setup failed before any pause; guarded cancellation could not be verified. "
                               "Retain the original lock/deadline; required existing SSH/sudo channel is a "
                               "runtime blocker; inspect run " + run_id) from recovery_error
        raise RuntimeError("Setup failed before any pause; original-state guard requested irrevocable standby "
                           "cancellation. Retain lock until exact terminal acknowledgment; inspect run " + run_id) from error
    try:
        m.remote(server, {**base, "operation": "cold-start"}, privileged=True)
    except (RuntimeError, OSError, ValueError, subprocess.SubprocessError, KeyboardInterrupt) as error:
        # Transport failure is ambiguous: the root helper may already have
        # started pausing. Never prompt for another automatic late sudo here.
        raise RuntimeError("cold-start transport/authentication failed; cancellation is unverified. "
                           "Retain the original lock/deadline and inspect the bound read-only status. "
                           "If start never reached the root helper, the standby remains inert; use only "
                           "the existing explicit cold-recover operator channel. Run " + run_id) from error
    print("Controller-owned cold coordinator/guard active; run_id=" + run_id, flush=True)
    while True:
        status = m.remote(server, {"operation": "cold-status", "run_id": run_id})
        require(status["deadline_epoch"] == deadline and status["plan_sha256"] == approval, "Guard status drift")
        if status["completed"]:
            require(status["phase"] == "accepted", "Source coordinator recovered after failure, not maintenance success")
            print("Worker2-only cold OS accepted and original state restored; controller/worker1 untouched.")
            return
        if status["mode"] == "recovering":
            raise RuntimeError("Independent original-state recovery active/blocked; retain lock and inspect run " + run_id)
        time.sleep(5)


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("command", choices=("execute", "guard", "recover"))
    parser.add_argument("--directory", required=True)
    args = parser.parse_args()
    if args.command == "execute":
        load_runtime(args.directory).execute()
    else:
        guard(args.directory, args.command == "recover")


if __name__ == "__main__":
    try:
        main()
    except BaseException as error:
        print("STOP: " + str(error), file=sys.stderr)
        sys.exit(1)
