#!/usr/bin/env python3
"""Discover releases, audit nodes, and execute a hash-approved local SSH plan."""

import argparse
import base64
import hashlib
import json
from pathlib import Path
import re
import shlex
import subprocess
import sys
import tempfile
import time
import urllib.request
import uuid


ROOT = Path(__file__).resolve().parents[2]
POLICY = ROOT / "node-config" / "maintenance" / "policy.json"
HOST = Path(__file__).with_name("node-maintenance-host.py")
HOST_BYTES = HOST.read_bytes()
HOST_SOURCE = HOST_BYTES.decode("utf-8").replace("\r\n", "\n")
LOCK = "operator-node-maintenance"
MAX_OUTAGE_SECONDS = 12 * 3600
RECOVERY_RESERVE_SECONDS = 2 * 3600


def require(condition, message):
    if not condition:
        raise RuntimeError(message)


def sha(value):
    return hashlib.sha256(value).hexdigest()


def canonical(value):
    return json.dumps(value, sort_keys=True, separators=(",", ":")).encode()


class OutageBudget:
    def __init__(self):
        self.started_epoch = time.time()
        self.started_monotonic = time.monotonic()
        self.deadline_epoch = self.started_epoch + MAX_OUTAGE_SECONDS

    def remaining(self):
        wall_elapsed = time.time() - self.started_epoch
        require(wall_elapsed >= 0, "Workstation clock moved backwards; stop new work and inspect recovery")
        elapsed = max(wall_elapsed, time.monotonic() - self.started_monotonic)
        remaining = MAX_OUTAGE_SECONDS - elapsed
        require(remaining > 0, "12-hour outage budget exhausted; restore services, do not continue upgrades")
        return remaining

    def before_disruption(self):
        require(self.remaining() > RECOVERY_RESERVE_SECONDS,
                "Recovery reserve reached; restore services instead of starting another disruptive step")


def run(args, timeout=120):
    result = subprocess.run(args, capture_output=True, text=True, timeout=timeout)
    require(result.returncode == 0, f"{args[0]} command failed (exit {result.returncode}); inspect locally")
    return result.stdout.strip()


def kube(*args):
    return run(["kubectl", "--request-timeout=30s", *args])


def get(resource, namespace=None, name=None):
    args = ["get", resource]
    if name:
        args.append(name)
    args.extend(["-n", namespace] if namespace else ([] if name else ["-A"]))
    return json.loads(kube(*args, "-o", "json"))


def items(resource, namespace=None):
    return get(resource, namespace).get("items", [])


def version(value):
    match = re.fullmatch(r"v?(1)\.(\d+)\.(\d+)\+rke2r(\d+)", value)
    require(match is not None, f"Unsupported RKE2 version: {value}")
    return tuple(int(part) for part in match.groups())


def check_transition(current, target):
    before, after = version(current), version(target)
    require(before <= after and 0 <= after[1] - before[1] <= 1,
            "Refusing downgrade or skipped Kubernetes minor")


def check_client_skew(client, current, target=None):
    def minor(value):
        match = re.fullmatch(r"v?(\d+)\.(\d+)\.\d+(?:\+[A-Za-z0-9._-]+)?", value)
        require(match is not None, "Unsupported kubectl/API version: " + value)
        return int(match[1]), int(match[2])

    client_minor = minor(client)
    for server in (current, target) if target else (current,):
        server_minor = minor(server)
        require(client_minor[0] == server_minor[0] and abs(client_minor[1] - server_minor[1]) <= 1,
                f"kubectl {client} is outside supported one-minor skew of API {server}; "
                "select a client compatible with both current and target API versions")


def remote(node, payload, privileged=False):
    encoded = base64.b64encode(canonical(payload)).decode()
    command = ["python3", "-c", HOST_SOURCE, encoded]
    if privileged:
        command = ["sudo", "systemd-run", "--unit=node-maintenance-" + payload["run_id"] + "-" + payload["operation"],
                   "--property=SendSIGKILL=no", "--property=KillMode=process",
                   "--wait", "--pipe", "--", *command]
    args = ["ssh", "-t" if privileged else "-T", "-o", "BatchMode=yes", "-o", "StrictHostKeyChecking=yes",
            "-o", "ConnectTimeout=10", node["ssh"], shlex.join(command)]
    if privileged:
        # Inherit the operator's terminal: never capture or pass a sudo password.
        # A lost terminal does not terminate the host's systemd-owned operation.
        try:
            result = subprocess.run(args, timeout=5400)
        except subprocess.TimeoutExpired as error:
            raise RuntimeError("Workstation wait expired; inspect the named host systemd unit and receipt; do not rerun") from error
        require(result.returncode == 0, "Privileged host step failed; retain cordon/lock and investigate")
        return None
    return json.loads(run(args))


def receipt(node, run_id):
    code = (
        "import os,stat,pathlib; "
        f"p=pathlib.Path('/var/tmp/node-maintenance-{run_id}.json'); "
        "s=p.lstat(); "
        "assert stat.S_ISREG(s.st_mode) and s.st_uid==0 and stat.S_IMODE(s.st_mode)==0o444; "
        "print(p.read_text())"
    )
    return json.loads(run(["ssh", "-T", "-o", "BatchMode=yes", "-o", "StrictHostKeyChecking=yes", "-o", "ConnectTimeout=10",
                          node["ssh"], shlex.join(["python3", "-c", code])]))


def matches(selector, labels):
    if selector is None:
        return False
    if any(labels.get(key) != value for key, value in selector.get("matchLabels", {}).items()):
        return False
    for expression in selector.get("matchExpressions", []):
        key, operator = expression["key"], expression["operator"]
        values = expression.get("values", [])
        if operator == "In" and labels.get(key) not in values:
            return False
        if operator == "NotIn" and key in labels and labels[key] in values:
            return False
        if operator == "Exists" and key not in labels:
            return False
        if operator == "DoesNotExist" and key in labels:
            return False
        require(operator in ("In", "NotIn", "Exists", "DoesNotExist"), "Unknown label selector operator")
    return True


def evictable(pod):
    return (pod["status"].get("phase") not in ("Succeeded", "Failed")
            and "kubernetes.io/config.mirror" not in pod["metadata"].get("annotations", {})
            and not any(owner["kind"] == "DaemonSet" for owner in pod["metadata"].get("ownerReferences", [])))


def pod_ready(pod):
    return (not pod["metadata"].get("deletionTimestamp")
            and any(c["type"] == "Ready" and c["status"] == "True"
                    for c in pod["status"].get("conditions", [])))


def quantity(value):
    value = str(value)
    match = re.fullmatch(r"([0-9.]+)([a-zA-Z]*)", value)
    require(match is not None, f"Unsupported Kubernetes resource quantity: {value}")
    factors = {"": 1, "m": .001, "Ki": 1024, "Mi": 1024**2, "Gi": 1024**3,
               "Ti": 1024**4, "k": 1000, "M": 1000**2, "G": 1000**3, "T": 1000**4}
    require(match[2] in factors, "Unsupported resource quantity suffix")
    return float(match[1]) * factors[match[2]]


def requests(pod):
    resources, peak, sidecars = {}, {}, {}
    containers = pod["spec"].get("containers", [])
    for container in containers:
        for key, value in container.get("resources", {}).get("requests", {}).items():
            resources[key] = resources.get(key, 0) + quantity(value)
    for container in pod["spec"].get("initContainers", []):
        current = {key: quantity(value) for key, value in container.get("resources", {}).get("requests", {}).items()}
        if container.get("restartPolicy") == "Always":
            for key, value in current.items():
                sidecars[key] = sidecars.get(key, 0) + value
            current = dict(sidecars)
        else:
            current = {key: current.get(key, 0) + sidecars.get(key, 0) for key in current.keys() | sidecars.keys()}
        for key, value in current.items():
            peak[key] = max(peak.get(key, 0), value)
    resources = {key: max(resources.get(key, 0) + sidecars.get(key, 0), peak.get(key, 0))
                 for key in resources.keys() | peak.keys() | sidecars.keys()}
    for key, value in pod["spec"].get("overhead", {}).items():
        resources[key] = resources.get(key, 0) + quantity(value)
    resources["pods"] = 1
    return resources


def reviewed_csi_sockets(pods, replicasets, deployments, namespace):
    names = {"csi-attacher", "csi-provisioner", "csi-resizer", "csi-snapshotter"}
    roots = {d["metadata"]["uid"]: d for d in deployments
             if d["metadata"]["namespace"] == namespace and d["metadata"]["name"] in names}
    sets = {r["metadata"]["uid"]: r for r in replicasets if r["metadata"]["namespace"] == namespace}
    reviews = []
    for pod in pods:
        meta, spec = pod["metadata"], pod["spec"]
        if meta["namespace"] != namespace or not evictable(pod):
            continue
        owner = next((o for o in meta.get("ownerReferences", [])
                      if o.get("controller") and o["kind"] == "ReplicaSet"), None)
        replica = sets.get(owner["uid"]) if owner else None
        if replica is None or owner["name"] != replica["metadata"]["name"]:
            continue
        parent = next((o for o in replica["metadata"].get("ownerReferences", [])
                       if o.get("controller") and o["kind"] == "Deployment"), None)
        deployment = roots.get(parent["uid"]) if parent else None
        if deployment is None or parent["name"] != deployment["metadata"]["name"]:
            continue
        if not matches(deployment["spec"]["selector"], meta.get("labels", {})):
            continue
        paths = [v for v in spec.get("volumes", []) if "hostPath" in v]
        expected = {"name": "socket-dir", "hostPath": {
            "path": "/var/lib/kubelet/plugins/driver.longhorn.io", "type": "DirectoryOrCreate"}}
        if paths != [expected] or any(v.get("persistentVolumeClaim") for v in spec.get("volumes", [])):
            continue
        containers = spec.get("containers", [])
        desired = deployment["spec"]["template"]["spec"].get("containers", [])
        if len(containers) != 1 or len(desired) != 1 or spec.get("initContainers"):
            continue
        container = containers[0]
        if (container["name"] != deployment["metadata"]["name"]
                or container["image"] != desired[0]["image"]
                or any(spec.get(key) for key in ("hostNetwork", "hostPID", "hostIPC"))):
            continue
        mounts = [m for m in container.get("volumeMounts", []) if m["name"] == "socket-dir"]
        if mounts != [{"name": "socket-dir", "mountPath": "/csi/"}]:
            continue
        reviews.append({
            "namespace": namespace, "pod": meta["name"], "pod_uid": meta["uid"],
            "replicaset_uid": replica["metadata"]["uid"], "deployment": deployment["metadata"]["name"],
            "deployment_uid": deployment["metadata"]["uid"],
            "template_sha256": sha(canonical(deployment["spec"]["template"])), "hostpath": expected,
        })
    return sorted(reviews, key=lambda r: (r["namespace"], r["pod"]))


def pending_spec_hash(pod):
    # Only the scheduled node and API-generated projection suffix change on replacement.
    spec = json.loads(canonical(pod["spec"]))
    spec.pop("nodeName", None)
    generated = [v["name"] for v in spec.get("volumes", [])
                 if re.fullmatch(r"kube-api-access-[a-z0-9]{5}", v["name"])]
    require(len(generated) <= 1, "Unexpected service-account projection layout")
    for volume in spec.get("volumes", []):
        if volume["name"] in generated:
            require("projected" in volume, "Service-account projection is not a projected volume")
            volume["name"] = "kube-api-access-generated"
    for container in spec.get("containers", []) + spec.get("initContainers", []):
        for mount in container.get("volumeMounts", []):
            if mount["name"] in generated:
                mount["name"] = "kube-api-access-generated"
    return sha(canonical(spec))


def plex_identity(policy, controller, pod):
    rule = policy["plex_downtime"]
    meta, spec, status = controller["metadata"], controller["spec"], controller.get("status", {})
    require(controller["kind"] == "StatefulSet" and meta["namespace"] == "media"
            and meta["name"] == "plex-plex-media-server" and not meta.get("deletionTimestamp"),
            "Reviewed Plex StatefulSet identity changed")
    require(spec.get("replicas", 1) == 1
            and status.get("observedGeneration", 0) >= meta["generation"]
            and status.get("currentRevision") and status["currentRevision"] == status.get("updateRevision"),
            "Plex replicas/controller revision are not stable")
    require(sha(canonical(spec["template"])) == rule["template_sha256"],
            "Plex template changed; review its downtime contract again")
    owners = [o for o in pod["metadata"].get("ownerReferences", []) if o.get("controller")]
    require(pod["kind"] == "Pod" and pod["metadata"]["namespace"] == "media"
            and pod["metadata"]["name"] == "plex-plex-media-server-0"
            and len(owners) == 1 and owners[0]["kind"] == "StatefulSet"
            and owners[0]["name"] == meta["name"] and owners[0]["uid"] == meta["uid"]
            and not pod["metadata"].get("deletionTimestamp"),
            "Plex Pod/controller ownership changed")
    require(matches(spec["selector"], pod["metadata"].get("labels", {}))
            and pod["metadata"].get("labels", {}).get("controller-revision-hash") == status["currentRevision"]
            and pod["spec"].get("nodeSelector") == {"kubernetes.io/hostname": "rishik-worker1"}
            and not pod["spec"].get("ephemeralContainers")
            and pending_spec_hash(pod) == rule["pod_spec_sha256"],
            "Plex admitted Pod specification changed; no downtime exception")
    return {"namespace": "media", "pod": pod["metadata"]["name"], "pod_uid": pod["metadata"]["uid"],
            "controller": meta["name"], "controller_uid": meta["uid"],
            "controller_generation": meta["generation"], "revision": status["currentRevision"],
            "template_sha256": rule["template_sha256"], "pod_spec_sha256": rule["pod_spec_sha256"],
            "node": "rishik-worker1", "probe_url": "https://plex.homelab/identity"}


def reviewed_plex_downtime(policy, pods):
    if not policy.get("plex_downtime"):
        return []
    candidates = [p for p in pods if p["metadata"]["namespace"] == "media"
                  and p["metadata"]["name"] == "plex-plex-media-server-0"]
    require(len(candidates) == 1, "Expected the reviewed single Plex Pod")
    pod = candidates[0]
    controller = get("statefulset", "media", "plex-plex-media-server")
    review = plex_identity(policy, controller, pod)
    require(pod["spec"].get("nodeName") == review["node"] and pod_ready(pod)
            and controller["status"].get("readyReplicas") == 1,
            "Plex must be healthy on worker1 before approving downtime")
    return [review]


def expected_pending_pods(policy, pods, reviews, draining_node):
    if not reviews:
        return set(), set()
    require(draining_node == "rishik-worker1" and len(reviews) == 1
            and reviews[0]["node"] == draining_node and policy.get("plex_downtime"),
            "Plex downtime review cannot apply to another maintenance step")
    review = reviews[0]
    candidates = [p for p in pods if p["metadata"]["namespace"] == review["namespace"]
                  and p["metadata"]["name"] == review["pod"]]
    require(len(candidates) == 1, "Expected the reviewed replacement Plex Pod, not a missing workload")
    pod = candidates[0]
    current = plex_identity(policy, get("statefulset", "media", review["controller"]), pod)
    require(all(current[key] == review[key] for key in current if key != "pod_uid"),
            "Plex controller/specification drifted during maintenance")
    require(pod["status"].get("phase") == "Pending" and not pod["spec"].get("nodeName")
            and not pod_ready(pod) and pod["metadata"]["uid"] != review["pod_uid"]
            and any(c["type"] == "PodScheduled" and c["status"] == "False"
                    and c.get("reason") == "Unschedulable" for c in pod["status"].get("conditions", [])),
            "Only the unscheduled replacement Plex Pod may be expected Pending")
    return {pod["metadata"]["uid"]}, {review["probe_url"]}


def scheduling_blockers(node, target, pods, socket_reviews=(), pending_reviews=()):
    blockers = []
    reviewed = {r["pod_uid"] for r in socket_reviews}
    labels = target["metadata"].get("labels", {})
    pending = {r["pod_uid"] for r in pending_reviews if r["node"] == node}
    incoming = [p for p in pods if p["spec"].get("nodeName") == node and evictable(p)
                and p["metadata"].get("uid") not in pending]
    remaining = [p for p in pods if p["spec"].get("nodeName") == target["metadata"]["name"]
                 and p["status"].get("phase") not in ("Succeeded", "Failed")]
    free = {key: quantity(value) for key, value in target["status"]["allocatable"].items()}
    for pod in remaining + incoming:
        for key, value in requests(pod).items():
            free[key] = free.get(key, 0) - value
    if any(value < 0 for value in free.values()):
        blockers.append(node + ": other worker lacks requested resource/pod capacity")
    for pod in incoming:
        spec = pod["spec"]
        identity = pod["metadata"]["namespace"] + "/" + pod["metadata"]["name"]
        if not pod["metadata"].get("ownerReferences"):
            blockers.append(identity + ": unmanaged pod cannot be safely drained")
        if any(labels.get(k) != v for k, v in spec.get("nodeSelector", {}).items()):
            blockers.append(identity + ": nodeSelector prevents migration to the other worker")
        # Do not pretend that a resource-sum heuristic implements the scheduler.
        affinity = spec.get("affinity") or {}
        if (any(affinity.get(kind, {}).get("requiredDuringSchedulingIgnoredDuringExecution")
                for kind in ("nodeAffinity", "podAffinity", "podAntiAffinity"))
                or any(c["whenUnsatisfiable"] == "DoNotSchedule" for c in spec.get("topologySpreadConstraints", []))):
            blockers.append(identity + ": affinity/topology constraints require explicit maintenance design")
        if (any(v.get("hostPath") for v in spec.get("volumes", []))
                and pod["metadata"].get("uid") not in reviewed):
            blockers.append(identity + ": hostPath requires explicit data-locality review")
        for taint in target["spec"].get("taints", []):
            if taint["effect"] not in ("NoSchedule", "NoExecute"):
                continue
            tolerates = any(
                t.get("effect", taint["effect"]) == taint["effect"]
                and (t.get("key", "") == taint["key"] or (not t.get("key") and t.get("operator") == "Exists"))
                and (t.get("operator", "Equal") == "Exists" or t.get("value", "") == taint.get("value", ""))
                for t in spec.get("tolerations", [])
            )
            if not tolerates:
                blockers.append(identity + ": other worker has an untolerated taint")
    return blockers


def storage_check(volumes, engines, replicas, workers):
    counts = {"attached_two_rw": 0, "detached_two_clean": 0}
    require(bool(volumes), "No Longhorn volumes found")
    for volume in volumes:
        name, state = volume["metadata"]["name"], volume["status"]["state"]
        require(volume["spec"]["numberOfReplicas"] == 2, name + ": replica policy changed")
        copies = [r for r in replicas if r["spec"]["volumeName"] == name and r["spec"].get("active")
                  and r["spec"].get("healthyAt") and not r["spec"].get("failedAt")]
        if state == "attached":
            live = [e for e in engines if e["spec"]["volumeName"] == name
                    and e["status"].get("currentState") == "running"]
            require(len(live) == 1 and volume["status"].get("robustness") == "healthy",
                    name + ": missing healthy engine")
            modes = live[0]["status"].get("replicaModeMap", {})
            good = [r for r in copies if modes.get(r["metadata"]["name"]) == "RW"
                    and r["status"].get("currentState") == "running"]
            counts["attached_two_rw"] += 1
        else:
            require(state == "detached", name + ": unexpected storage state")
            good = [r for r in copies if r["status"].get("currentState") == "stopped"]
            counts["detached_two_clean"] += 1
        require(len(good) == 2 and {r["spec"].get("nodeID") for r in good} == workers,
                name + ": requires two clean copies on distinct workers")
    return counts


def quarantine_state(policy, volumes, engines, replicas):
    protected = {}
    for name in policy.get("quarantined_volumes", []):
        selected = [v for v in volumes if v["metadata"]["name"] == name]
        copies = [r for r in replicas if r["spec"]["volumeName"] == name]
        controllers = [e for e in engines if e["spec"]["volumeName"] == name]
        require(len(selected) == len(copies) == len(controllers) == 1, name + ": quarantine topology changed")
        volume, replica, engine = selected[0], copies[0], controllers[0]
        require(volume["status"]["state"] == "detached" and not volume["spec"].get("nodeID")
                and not volume["status"].get("currentNodeID")
                and replica["spec"].get("nodeID") == "rishik-worker1"
                and replica["spec"].get("desireState") == replica["status"].get("currentState") == "stopped"
                and replica["spec"].get("healthyAt") and not replica["spec"].get("failedAt")
                and engine["status"].get("currentState") == "stopped"
                and engine["spec"].get("salvageRequested") is True
                and not engine["status"].get("salvageExecuted"), name + ": quarantine invariants changed")
        require(not get("volumeattachments.longhorn.io", policy["storage_namespace"], name)["spec"].get("attachmentTickets"),
                name + ": quarantined volume acquired an attachment ticket")
        protected[name] = {
            "volume_uid": volume["metadata"]["uid"], "replica_uid": replica["metadata"]["uid"],
            "engine_uid": engine["metadata"]["uid"], "data_directory": replica["spec"].get("dataDirectoryName"),
        }
    if "_quarantine" in policy:
        require(protected == policy["_quarantine"], "Quarantined volume/replica identity changed during maintenance")
    return protected


def etcd_tool(policy):
    pods = [p for p in items("pods", "kube-system") if p["metadata"].get("labels", {}).get("component") == "etcd"]
    require(len(pods) == 1, "Expected the existing single embedded-etcd server")
    images = [c["image"] for c in pods[0]["spec"]["containers"] if "hardened-etcd:" in c["image"]]
    require(len(images) == 1, "Cannot identify current etcd release")
    match = re.search(r"hardened-etcd:v(3\.\d+)\.", images[0])
    require(match is not None and match[1] in policy["etcdutl"], "Current etcd requires a reviewed offline verifier pin")
    return policy["etcdutl"][match[1]]


def check_nodes(policy, nodes, draining_node=None):
    expected = {n["name"]: n for n in policy["nodes"]}
    require({n["metadata"]["name"] for n in nodes} == set(expected), "Cluster membership drifted")
    require(draining_node is None or draining_node in expected, "Unknown maintenance node")
    for node in nodes:
        name = node["metadata"]["name"]
        require(any(c["type"] == "Ready" and c["status"] == "True"
                    for c in node["status"]["conditions"]), name + ": not Ready")
        require(all(c["status"] == "False" for c in node["status"]["conditions"]
                    if c["type"] in ("MemoryPressure", "DiskPressure", "PIDPressure")),
                name + ": node resource pressure")
        require(bool(node["spec"].get("unschedulable")) ==
                (expected[name]["role"] == "server" or name == draining_node),
                name + ": cordon state differs from approved baseline")
    return expected


def health(policy, pods=None, draining_node=None, pending_reviews=()):
    kube("get", "--raw=/readyz")
    expected = check_nodes(policy, items("nodes"), draining_node)
    require(get("helmrelease", "monitoring", "kured")["spec"].get("suspend") is True,
            "Kured must remain suspended")
    ns = policy["storage_namespace"]
    require(get("settings.longhorn.io", ns, "node-drain-policy")["value"] == "block-if-contains-last-replica",
            "Longhorn strict drain policy changed")
    volumes, engines, replicas = items("volumes.longhorn.io", ns), items("engines.longhorn.io", ns), items("replicas.longhorn.io", ns)
    protected = quarantine_state(policy, volumes, engines, replicas)
    counts = storage_check([v for v in volumes if v["metadata"]["name"] not in protected], engines, replicas,
                           {n["name"] for n in policy["nodes"] if n["role"] == "agent"})
    counts["quarantine"] = protected
    managers = [p for p in items("pods", ns) if p["metadata"].get("labels", {}).get("app") == "longhorn-manager"]
    require(len(managers) == 2 and all(pod_ready(p) and expected[p["spec"]["nodeName"]]["role"] == "agent"
                                     for p in managers), "Both worker Longhorn managers must be Ready")
    counts["managers"] = {
        p["metadata"]["name"]: {"uid": p["metadata"]["uid"], "node": p["spec"]["nodeName"],
                                "restarts": {s["name"]: s["restartCount"] for s in p["status"].get("containerStatuses", [])}}
        for p in managers
    }
    active = pods if pods is not None else items("pods")
    pending, skipped_probes = expected_pending_pods(policy, active, pending_reviews, draining_node)
    counts["expected_pending"] = sorted(pending)
    bad = [p["metadata"]["namespace"] + "/" + p["metadata"]["name"] for p in active
           if p["status"].get("phase") not in ("Succeeded", "Failed") and not pod_ready(p)
           and p["metadata"].get("uid") not in pending]
    require(not bad, "Active pods not Ready: " + ", ".join(bad[:12]))
    for url in policy["probe_urls"]:
        if url in skipped_probes:
            continue
        with urllib.request.urlopen(url, timeout=15) as response:
            require(response.status == 200, "Native HTTPS probe failed")
    kube("-n", "media", "exec", "deploy/qbittorrent", "-c", "gluetun", "--",
         "/gluetun-entrypoint", "healthcheck")
    return counts


def drained_health(policy, node, pending_reviews=()):
    pods = items("pods")
    remaining = [p["metadata"]["namespace"] + "/" + p["metadata"]["name"] for p in pods
                 if p["spec"].get("nodeName") == node["name"] and evictable(p)]
    require(not remaining, "Drain left active evictable pods: " + ", ".join(remaining[:12]))
    if pending_reviews:
        return health(policy, pods, draining_node=node["name"], pending_reviews=pending_reviews)
    return health(policy, pods, draining_node=node["name"])


def stable_health(policy, budget=None):
    previous = None
    for index in range(3):
        if budget:
            budget.remaining()
        state = health(policy)
        if previous is not None:
            require(state["managers"] == previous, "Longhorn manager identity/restarts changed during stability window")
        previous = state["managers"]
        if index < 2:
            time.sleep(min(30, budget.remaining()) if budget else 30)
    if budget:
        budget.remaining()


def audit_nodes(policy):
    return {node["name"]: remote(node, {"operation": "audit"}) for node in policy["nodes"]}


def build_plan(policy, action, minor, inventory):
    pods, nodes, pdbs = items("pods"), items("nodes"), items("poddisruptionbudgets")
    pvcs = {(p["metadata"]["namespace"], p["metadata"]["name"]): p["spec"].get("volumeName")
            for p in items("persistentvolumeclaims")}
    pvs = {p["metadata"]["name"]: p for p in items("persistentvolumes")}
    sockets = reviewed_csi_sockets(pods, items("replicasets"), items("deployments"), policy["storage_namespace"])
    node_map = {n["metadata"]["name"]: n for n in nodes}
    blockers = []
    pending_reviews = []
    try:
        pending_reviews = reviewed_plex_downtime(policy, pods)
    except RuntimeError as error:
        blockers.append(str(error))
    protected = {}
    try:
        protected = (health(policy, pods) or {}).get("quarantine", {})
    except Exception as error:
        blockers.append(str(error))
    release = None
    if action == "rke2":
        require(minor in policy["rke2_releases"], "Target minor must be pinned in Git first")
        release = policy["rke2_releases"][minor]
        require(re.fullmatch(r"[a-f0-9]{64}", release["sha256"]) is not None, "Missing RKE2 archive pin")
        for node in nodes:
            check_transition(node["status"]["nodeInfo"]["kubeletVersion"], release["version"])
        require(len({n["status"]["nodeInfo"]["kubeletVersion"] for n in nodes}) == 1,
                "Mixed versions require recovery assessment before a new fleet run")
    versions = json.loads(kube("version", "-o", "json"))
    require(all(isinstance(versions.get(key), dict)
                and isinstance(versions[key].get("gitVersion"), str)
                for key in ("clientVersion", "serverVersion")),
            "kubectl did not return client and API versions; no maintenance")
    client = {"client": versions["clientVersion"]["gitVersion"], "server": versions["serverVersion"]["gitVersion"]}
    try:
        check_client_skew(client["client"], client["server"], release["version"] if release else None)
    except RuntimeError as error:
        blockers.append(str(error))
    order = policy["nodes"] if action == "rke2" else sorted(policy["nodes"], key=lambda n: (
        n["role"] == "server", n["name"] != "rishik-worker2"))
    for node in order:
        name, host = node["name"], inventory[node["name"]]
        require(host["name"] == name and host["os"] == "ubuntu"
                and host["os_version"] == policy["ubuntu_lts"] and host["architecture"] == "x86_64",
                name + ": host identity/Ubuntu LTS drift")
        if host["root_free_bytes"] < 4 * 1024**3 or host["memory_available_bytes"] < 1536 * 1024**2:
            blockers.append(name + ": insufficient host disk/memory headroom")
        if action == "os" and (host["boot_free_bytes"] < 512 * 1024**2 or not host["kernel_metapackage_installed"]):
            blockers.append(name + ": insufficient /boot space or missing Ubuntu kernel metapackage")
        if host["sunreclaim_bytes"] >= 1024**3:
            blockers.append(name + ": unreclaimable kernel slab requires investigation")
        if time.time() - host["apt_metadata_epoch"] > 48 * 3600:
            blockers.append(name + ": APT metadata older than 48 hours")
        worker = next((n for n in nodes if n["metadata"]["name"] != name
                       and n["metadata"]["name"] in {p["name"] for p in policy["nodes"] if p["role"] == "agent"}), None)
        require(worker is not None, "No spare worker exists")
        blockers.extend(scheduling_blockers(name, worker, pods, sockets, pending_reviews))
        for pod in [p for p in pods if p["spec"].get("nodeName") == name and evictable(p)]:
            for volume in pod["spec"].get("volumes", []):
                claim = volume.get("persistentVolumeClaim", {}).get("claimName")
                if claim:
                    pv = pvs.get(pvcs.get((pod["metadata"]["namespace"], claim)))
                    if pv is None or any(pv["spec"].get(key) for key in ("nodeAffinity", "local", "hostPath")):
                        blockers.append(f"{pod['metadata']['namespace']}/{claim}: unbound/locality-constrained PVC needs review")
            for pdb in pdbs:
                if (pdb["metadata"]["namespace"] == pod["metadata"]["namespace"]
                        and matches(pdb["spec"].get("selector"), pod["metadata"].get("labels", {}))):
                    if (pdb["status"].get("observedGeneration", 0) < pdb["metadata"].get("generation", 1)
                            or pdb["status"].get("disruptionsAllowed", 0) == 0):
                        blockers.append(f"{name}: PDB {pdb['metadata']['namespace']}/{pdb['metadata']['name']} blocks drain")
    bindings = {name: {"boot_id": host["boot_id"], "kernel": host["kernel"],
                       "source_version": node_map[name]["status"]["nodeInfo"]["kubeletVersion"],
                       "pending_packages": host["pending_packages"]}
                for name, host in inventory.items()}
    return {
        "created_epoch": int(time.time()), "cluster_uid": get("namespace", name="kube-system")["metadata"]["uid"],
        "policy_sha256": sha(POLICY.read_bytes()), "runner_sha256": sha(Path(__file__).read_bytes()),
        "host_sha256": sha(HOST.read_bytes()), "action": action, "release": release,
        "outage_budget": {"maximum_seconds": MAX_OUTAGE_SECONDS, "recovery_reserve_seconds": RECOVERY_RESERVE_SECONDS},
        "nodes": order, "bindings": bindings, "kubectl": client, "quarantine": protected, "blockers": sorted(set(blockers)),
        "reviewed_csi_sockets": sockets,
        "reviewed_plex_downtime": pending_reviews,
        "impact": "Serial drains delete reviewed emptyDir data. OS: host reboot and disable desktop, printing, "
                  "Bluetooth, mDNS advertisement and modem services; preserve networking/storage/GPU drivers. "
                  "RKE2: service restarts. Controller step interrupts the sole API/etcd server. "
                  "No package removal, forced drain, automatic restore, or controller uncordon.",
    }


def check_plan(plan, approval, cluster_uid, now=None):
    require(sha(canonical(plan)) == approval, "Approval does not match the complete plan")
    age = (time.time() if now is None else now) - plan["created_epoch"]
    require(0 <= age < 4 * 3600, "Plan is expired or has a future timestamp")
    require(plan["cluster_uid"] == cluster_uid, "Kubernetes context points at a different cluster")
    require(plan["action"] in ("os", "rke2"), "Unsupported maintenance operation")
    require(plan["policy_sha256"] == sha(POLICY.read_bytes())
            and plan["runner_sha256"] == sha(Path(__file__).read_bytes())
            and plan["host_sha256"] == sha(HOST.read_bytes()) == sha(HOST_BYTES),
            "Policy/maintenance code changed after approval")
    require(not plan["blockers"], "Plan has unresolved blockers")


def wait_for(check, seconds=900, budget=None):
    deadline, error = time.monotonic() + seconds, None
    while time.monotonic() < deadline:
        if budget:
            budget.remaining()
        try:
            result = check()
        except Exception as current:
            error = current
            time.sleep(min(10, budget.remaining()) if budget else 10)
        else:
            if budget:
                budget.remaining()
            return result
    raise RuntimeError(f"Recovery timed out; stop and inspect: {error}")


def node_recovered(node, binding, plan, policy):
    state = remote(node, {"operation": "audit"})
    info = get("node", name=node["name"])
    require(any(c["type"] == "Ready" and c["status"] == "True"
                for c in info["status"]["conditions"]), "Node not Ready yet")
    kube("get", "--raw=/readyz")
    if plan["action"] == "os":
        require(state["boot_id"] != binding["boot_id"], "Host has not rebooted")
        require(state["kernel"] == policy["kernel"], "Host did not boot the approved kernel")
        require(state["default_target"] == "multi-user.target" and not state["reboot_required"],
                "Headless/reboot acceptance failed")
    else:
        require(info["status"]["nodeInfo"]["kubeletVersion"] == plan["release"]["version"],
                "Kubelet has not reached approved RKE2 version")
    return state


def capture_evidence(name):
    observed = [p for p in items("pods") if p["spec"].get("nodeName") == name
                and p["status"].get("phase") not in ("Succeeded", "Failed")]
    report = {"node": name, "created_epoch": int(time.time()), "pods": []}
    for pod in observed:
        meta = pod["metadata"]
        statuses = pod["status"].get("containerStatuses", []) + pod["status"].get("initContainerStatuses", [])
        row = {
            "namespace": meta["namespace"], "name": meta["name"], "uid": meta["uid"],
            "phase": pod["status"].get("phase"),
            "containers": [{"name": s["name"], "ready": s["ready"], "restarts": s["restartCount"],
                            "state": {key: {k: v for k, v in value.items()
                                           if k in ("reason", "exitCode", "signal", "startedAt", "finishedAt")}
                                      for key, value in s.get("state", {}).items()},
                            "last_termination": {k: v for k, v in s.get("lastState", {}).get("terminated", {}).items()
                                                 if k in ("reason", "exitCode", "signal", "startedAt", "finishedAt")}}
                           for s in statuses],
            "mounts": [{"container": c["name"], "mounts": c.get("volumeMounts", [])}
                       for c in pod["spec"]["containers"]],
            "volumes": [{"name": v["name"], "type": [k for k in v if k != "name"]}
                        for v in pod["spec"].get("volumes", [])],
        }
        # Raw logs may contain credentials/records: classify only in memory.
        logs = kube("-n", meta["namespace"], "logs", meta["name"], "--all-containers=true",
                    "--tail=80", "--limit-bytes=16384").lower()
        row["log_classification_counts"] = {word: logs.count(word) for word in ("error", "panic", "oom", "x509", "timeout")}
        row["previous_logs"] = {}
        for container in statuses:
            if container["restartCount"]:
                try:
                    old = kube("-n", meta["namespace"], "logs", meta["name"], "-c", container["name"],
                               "--previous", "--tail=80", "--limit-bytes=16384").lower()
                    row["previous_logs"][container["name"]] = {
                        word: old.count(word) for word in ("error", "panic", "oom", "x509", "timeout")}
                except RuntimeError:
                    row["previous_logs"][container["name"]] = {"available": False}
        report["pods"].append(row)
    uids = {p["uid"] for p in report["pods"]}
    events = [e for e in items("events") if e.get("involvedObject", {}).get("uid") in uids]
    report["event_reason_counts"] = {}
    for event in events:
        reason = event.get("type", "") + "/" + event.get("reason", "")
        report["event_reason_counts"][reason] = report["event_reason_counts"].get(reason, 0) + event.get("count", 1)
    return report


def apply(plan, approval, policy, recovery_directory, token_confirmed, compatibility_reviewed):
    check_plan(plan, approval, get("namespace", name="kube-system")["metadata"]["uid"])
    require(recovery_directory and recovery_directory.startswith("/") and token_confirmed,
            "Protected off-node recovery directory and existing server-token backup confirmation are required")
    require(plan["action"] != "rke2" or compatibility_reviewed,
            "Review the RKE2/Ubuntu, Longhorn/CSI, Canal, Kyverno, ingress and GPU compatibility first")
    policy = {**policy, "_quarantine": plan["quarantine"]}
    inventory = audit_nodes(policy)
    fresh = build_plan(policy, plan["action"],
                       ".".join(str(x) for x in version(plan["release"]["version"])[:2]) if plan["release"] else None,
                       inventory)
    require(not fresh["blockers"] and all(fresh[key] == plan[key]
            for key in ("bindings", "nodes", "release", "quarantine", "kubectl", "reviewed_csi_sockets")),
            "Cluster/host/package/client state changed or blockers appeared; generate a new plan")
    require(fresh.get("reviewed_plex_downtime", []) == plan.get("reviewed_plex_downtime", []),
            "Plex downtime identity changed after approval")
    server = next(n for n in policy["nodes"] if n["role"] == "server")
    protection = remote(server, {"operation": "recovery-audit", "recovery_directory": recovery_directory})
    require(protection.get("off_node_directory_verified") is True, "Controller recovery directory was not verified")
    run_id = uuid.uuid4().hex
    evidence_directory = Path(tempfile.mkdtemp(prefix="homelab-maintenance-"))
    budget = OutageBudget()
    # An unexpired lease could be stolen while a sole server is offline. This lock
    # deliberately never expires; an interrupted run needs human investigation.
    kube("-n", "kube-system", "create", "configmap", LOCK, "--from-literal=run_id=" + run_id,
         "--from-literal=plan_sha256=" + approval,
         "--from-literal=started_epoch=" + str(budget.started_epoch),
         "--from-literal=deadline_epoch=" + str(budget.deadline_epoch))
    (evidence_directory / "maintenance.json").write_text(json.dumps({
        "run_id": run_id, "plan_sha256": approval, "started_epoch": budget.started_epoch,
        "deadline_epoch": budget.deadline_epoch, "recovery_reserve_seconds": RECOVERY_RESERVE_SECONDS,
    }, indent=2), encoding="utf-8")
    print("Maintenance lock acquired. Failure retains lock and the current node cordon.", flush=True)
    print(f"Restoration deadline (Unix UTC): {budget.deadline_epoch}; stop new disruption within the recovery reserve.",
          flush=True)
    for node in plan["nodes"]:
        check_plan(plan, approval, get("namespace", name="kube-system")["metadata"]["uid"])
        name, binding = node["name"], plan["bindings"][node["name"]]
        budget.before_disruption()
        stable_health(policy, budget=budget)
        current = remote(node, {"operation": "audit"})
        require(current["boot_id"] == binding["boot_id"], "Unexpected reboot since approval")
        if plan["action"] == "os":
            require(current["pending_packages"] == binding["pending_packages"], "APT candidates changed since approval")
        pending_reviews = [r for r in plan.get("reviewed_plex_downtime", []) if r["node"] == name]
        if pending_reviews:
            require(reviewed_plex_downtime(policy, items("pods")) == pending_reviews,
                    "Plex downtime identity changed before drain")
        step_id = uuid.uuid4().hex
        payload = {
            "operation": plan["action"], "run_id": step_id, "name": name, "role": node["role"],
            "boot_id": binding["boot_id"], "ubuntu_lts": policy["ubuntu_lts"],
            "pending_packages": binding["pending_packages"], "kernel": policy["kernel"],
            "kernel_package": policy["kernel_package"], "release": plan["release"],
            "source_version": binding["source_version"], "etcdutl": etcd_tool(policy),
            "recovery_directory": recovery_directory,
            "deadline_epoch": budget.deadline_epoch, "recovery_reserve_seconds": RECOVERY_RESERVE_SECONDS,
        }
        evidence = capture_evidence(name)
        (evidence_directory / (name + "-before.json")).write_text(json.dumps(evidence, indent=2), encoding="utf-8")
        print(f"Technical-only pre-disruption evidence: {evidence_directory}", flush=True)
        budget.before_disruption()
        kube("cordon", name)
        run(["kubectl", "--request-timeout=30s", "drain", name, "--ignore-daemonsets",
             "--delete-emptydir-data", "--timeout=10m"], timeout=660)
        post_drain = (wait_for(lambda: drained_health(policy, node, pending_reviews=pending_reviews),
                              seconds=180, budget=budget) if pending_reviews else drained_health(policy, node))
        (evidence_directory / (name + "-after-drain.json")).write_text(json.dumps({
            "node": name, "created_epoch": int(time.time()), "health": post_drain,
        }, indent=2), encoding="utf-8")
        budget.before_disruption()
        remote(node, payload, privileged=True)
        result = receipt(node, step_id)
        (evidence_directory / (name + "-receipt.json")).write_text(json.dumps(result, indent=2), encoding="utf-8")
        require(result["run_id"] == step_id and result["name"] == name
                and result["operation"] == plan["action"] and time.time() - result["completed_epoch"] < 1800,
                "Invalid/stale host receipt")
        if node["role"] == "server":
            require(result["recovery"]["off_node_verified"], "Controller recovery copy not verified")
        state = wait_for(lambda: node_recovered(node, binding, plan, policy), budget=budget)
        if node["role"] == "agent":
            kube("uncordon", name)
        try:
            wait_for(lambda: health(policy), seconds=1200, budget=budget)
            stable_health(policy, budget=budget)
        except Exception:
            kube("cordon", name)
            raise
        if plan["action"] == "rke2":
            remote(node, {**payload, "operation": "accept", "boot_id": state["boot_id"]}, privileged=True)
        print(f"ACCEPTED {name}: {plan['action']}", flush=True)
    budget.remaining()
    require(get("configmap", "kube-system", LOCK)["data"]["run_id"] == run_id, "Maintenance lock ownership changed")
    kube("-n", "kube-system", "delete", "configmap", LOCK)
    print("Fleet maintenance accepted. Controller remains cordoned; Kured remains suspended.")


def discover(policy):
    releases = json.loads(run(["gh", "api", "repos/rancher/rke2/releases?per_page=100"]))
    changed = False
    for minor, pinned in policy["rke2_releases"].items():
        candidates = [r for r in releases if not r["draft"] and not r["prerelease"]
                      and re.fullmatch(r"v" + re.escape(minor) + r"\.\d+\+rke2r\d+", r["tag_name"])]
        require(bool(candidates), "No official release found for pinned minor " + minor)
        latest = max(candidates, key=lambda r: version(r["tag_name"]))
        require(version(latest["tag_name"]) >= version(pinned["version"]), "Release discovery would downgrade")
        assets = [a for a in latest["assets"] if a["name"] == "rke2.linux-amd64.tar.gz"]
        require(len(assets) == 1 and re.fullmatch(r"sha256:[a-f0-9]{64}", assets[0].get("digest") or ""),
                "Official release asset lacks publisher digest; manual review required")
        replacement = {"version": latest["tag_name"], "sha256": assets[0]["digest"].split(":")[1]}
        if replacement != pinned:
            policy["rke2_releases"][minor] = replacement
            changed = True
    if changed:
        POLICY.write_text(json.dumps(policy, indent=2) + "\n", encoding="utf-8", newline="\n")
    print("Pinned release proposal updated." if changed else "Pinned RKE2 releases are current.")


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    commands = parser.add_subparsers(dest="command", required=True)
    commands.add_parser("discover", help="Update release proposals from official GitHub asset metadata; no cluster access")
    commands.add_parser("audit", help="Read host/cluster state; no sudo or mutations")
    plan_parser = commands.add_parser("plan")
    plan_parser.add_argument("--action", choices=("os", "rke2"), required=True)
    plan_parser.add_argument("--minor", help="Pinned target minor, e.g. 1.36; no minor skipping")
    plan_parser.add_argument("--output", type=Path, required=True)
    apply_parser = commands.add_parser("apply")
    apply_parser.add_argument("--plan", type=Path, required=True)
    apply_parser.add_argument("--approve", required=True, help="SHA256 printed by plan")
    apply_parser.add_argument("--recovery-directory", required=True)
    apply_parser.add_argument("--token-backup-confirmed", action="store_true")
    apply_parser.add_argument("--compatibility-reviewed", action="store_true")
    args = parser.parse_args()
    policy = json.loads(POLICY.read_text(encoding="utf-8"))
    if args.command == "discover":
        discover(policy)
    elif args.command == "apply":
        apply(json.loads(args.plan.read_text(encoding="utf-8")), args.approve, policy,
              args.recovery_directory, args.token_backup_confirmed, args.compatibility_reviewed)
    else:
        inventory = audit_nodes(policy)
        if args.command == "audit":
            print(json.dumps(inventory, indent=2))
        else:
            plan = build_plan(policy, args.action, args.minor, inventory)
            args.output.write_text(json.dumps(plan, indent=2) + "\n", encoding="utf-8")
            print(json.dumps({"plan_sha256": sha(canonical(plan)), "blockers": plan["blockers"],
                              "impact": plan["impact"]}, indent=2))


if __name__ == "__main__":
    try:
        main()
    except Exception as error:
        print(f"STOP: {error}", file=sys.stderr)
        sys.exit(1)
