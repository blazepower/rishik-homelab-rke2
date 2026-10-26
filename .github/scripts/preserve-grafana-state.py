#!/usr/bin/env python3
"""Coordinator-only preservation runner. Default plan performs no cluster writes."""

import argparse
import json
from pathlib import Path
import ssl
import subprocess
import time
import urllib.error
import urllib.request
import uuid

import yaml


ROOT = Path(__file__).resolve().parents[2]
ASSETS = ROOT / "infrastructure/monitoring/preservation"
SOURCE_UID = "62fb40e7-ab02-46d5-b6fc-0a7b4d05633d"
PROMETHEUS_UID = "d5436bdf-a643-4028-8a7f-513a52ed66a8"
PROMETHEUS_CLAIM = "prometheus-kube-prometheus-stack-prometheus-db-prometheus-kube-prometheus-stack-prometheus-0"
RECONCILE = "kustomize.toolkit.fluxcd.io/reconcile"
URLS = ("https://grafana.homelab/api/health", "https://grafana.tail4217c.ts.net/api/health")
INVENTORY = r'''
import os,json,requests,collections
s=requests.Session();s.auth=(os.environ["REQ_USERNAME"],os.environ["REQ_PASSWORD"])
def get(path):
 r=s.get("http://localhost:3000"+path,timeout=20);r.raise_for_status();return r.json()
stats=get("/api/admin/stats")
dash=get("/api/search?type=dash-db&limit=5000")
provisioned=sum(bool(get("/api/dashboards/uid/"+d["uid"]).get("meta",{}).get("provisioned")) for d in dash)
plugins=get("/api/plugins"); health=get("/api/health")
prefs=get("/api/user/preferences");orgprefs=get("/api/org/preferences")
print(json.dumps({"stats":{k:v for k,v in stats.items() if isinstance(v,(int,float))},
 "dashboards":len(dash),"provisionedDashboards":provisioned,"database":health.get("database"),
 "preferenceFieldsSet":[k for k,v in prefs.items() if v not in [None,"",0,False,{}]],
 "orgPreferenceFieldsSet":[k for k,v in orgprefs.items() if v not in [None,"",0,False,{}]],
 "plugins":[{"id":p["id"],"version":p.get("info",{}).get("version",""),"enabled":p.get("enabled")} for p in plugins]}))
'''


def kubectl(*arguments, payload=None):
    result = subprocess.run(
        ["kubectl", *arguments], input=payload, capture_output=True, text=True, check=False,
    )
    if result.returncode:
        raise RuntimeError(f"kubectl {arguments[0]} failed; inspect resource status separately")
    return result.stdout


def get(kind, name=None, optional=False):
    arguments = ["get", kind, "-n", "monitoring", "-o", "json"]
    if name:
        arguments.insert(2, name)
    if optional:
        arguments.append("--ignore-not-found")
    output = kubectl(*arguments)
    return json.loads(output) if output.strip() else None


def source_pod(required=True):
    pods = get("pods")["items"]
    matches = [pod for pod in pods if pod["metadata"].get("labels", {}).get("app.kubernetes.io/name") == "grafana"]
    if required:
        matches = [pod for pod in matches if pod["metadata"]["uid"] == SOURCE_UID]
        if len(matches) != 1 or matches[0]["spec"]["nodeName"] != "rishik-worker2":
            raise RuntimeError("Source pod UID/node changed; never reuse the old hostPath")
    else:
        matches = [pod for pod in matches if pod["status"].get("phase") == "Running"
                   and all(c.get("ready") for c in pod["status"].get("containerStatuses", []))]
    if len(matches) != 1:
        raise RuntimeError("Exactly one ready Grafana pod is required")
    return matches[0]


def inventory(pod):
    return json.loads(kubectl("exec", "-n", "monitoring", pod["metadata"]["name"],
                             "-c", "grafana-sc-dashboard", "--", "python", "-c", INVENTORY))


def check_prometheus():
    claim = get("pvc", PROMETHEUS_CLAIM)
    if claim["metadata"]["uid"] != PROMETHEUS_UID or claim["spec"]["volumeName"] != "pvc-" + PROMETHEUS_UID:
        raise RuntimeError("Existing Prometheus PVC identity changed")
    deployment = get("deployment", "kube-prometheus-stack-grafana")
    for container in deployment["spec"]["template"]["spec"]["containers"]:
        refs = [env["valueFrom"]["secretKeyRef"] for env in container.get("env", [])
                if env.get("valueFrom", {}).get("secretKeyRef")]
        if {ref["name"] for ref in refs} != {"grafana-admin-credentials"}:
            raise RuntimeError("Grafana credential Secret references changed")
        if {ref["key"] for ref in refs} != {"admin-user", "admin-password"}:
            raise RuntimeError("Grafana credential key references changed")


def health_code(url):
    try:
        with urllib.request.urlopen(url, context=ssl.create_default_context(), timeout=15) as response:
            return response.status
    except urllib.error.HTTPError as error:
        return error.code
    except urllib.error.URLError as error:
        if isinstance(error.reason, ssl.SSLError):
            raise ssl.SSLError(f"TLS verification failed for {url}") from error
        return 0


def tailscale_backend(service_name, ingress_uid):
    """Verify the live forwarder without reading its Secret-mounted serve file."""
    service = get("service", service_name)
    pods = json.loads(kubectl("get", "pods", "-A", "-l",
                             "tailscale.com/parent-resource=grafana-tailscale", "-o", "json"))["items"]
    pods = [pod for pod in pods if pod["metadata"]["labels"].get("app") == ingress_uid
            and pod["metadata"]["labels"].get("tailscale.com/parent-resource-ns") == "monitoring"
            and all(container.get("ready") for container in pod["status"].get("containerStatuses", []))]
    if len(pods) != 1:
        raise RuntimeError("Exactly one ready Tailscale forwarder for the captured ingress UID is required")
    pod = pods[0]
    config = json.loads(kubectl("exec", "-n", pod["metadata"]["namespace"], pod["metadata"]["name"],
                               "-c", "tailscale", "--", "tailscale", "serve", "status", "--json"))
    proxies = [handler.get("Proxy") for web in config.get("Web", {}).values()
               for handler in web.get("Handlers", {}).values() if handler.get("Proxy")]
    if proxies != [f"http://{service['spec']['clusterIP']}:80/"]:
        raise RuntimeError("Tailscale runtime forwarder has not reached the expected Service")
    slices = json.loads(kubectl("get", "endpointslices.discovery.k8s.io", "-n", "monitoring", "-l",
                               f"kubernetes.io/service-name={service_name}", "-o", "json"))["items"]
    ready = sum(len(endpoint.get("addresses", [])) for item in slices for endpoint in item.get("endpoints", [])
                if endpoint.get("conditions", {}).get("ready") is not False)
    if service_name == "grafana-maintenance-hold":
        legacy = get("endpoints", service_name, optional=True)
        if ready or (legacy and legacy.get("subsets")) or service["spec"].get("selector"):
            raise RuntimeError("The maintenance Service must have no selector or endpoints")
    elif ready == 0:
        raise RuntimeError("The real Grafana Service has no ready endpoints")
    return {"proxyPodUID": pod["metadata"]["uid"], "serviceName": service_name,
            "runtimeBackendVerified": True, "readyBackendAddresses": ready}


def write_state(path, state):
    path.write_text(json.dumps(state, indent=2), encoding="utf-8")


def apply_owned(document, state, path):
    labels = document.setdefault("metadata", {}).setdefault("labels", {})
    labels["preservation.homelab/run"] = state["run"]
    kubectl("apply", "-f", "-", payload=json.dumps(document))
    current = get(document["kind"], document["metadata"]["name"])
    record = {"kind": current["kind"], "apiVersion": current["apiVersion"],
              "name": current["metadata"]["name"], "uid": current["metadata"]["uid"]}
    state["owned"].append(record)
    write_state(path, state)


def patch_tailscale(state, hold):
    original = state["ingresses"]["grafana-tailscale"]
    live = get("ingress", "grafana-tailscale")
    if live["metadata"]["uid"] != original["uid"]:
        raise RuntimeError("Tailscale ingress UID changed")
    desired = json.loads(json.dumps(original["spec"]))
    desired["defaultBackend"]["service"]["name"] = "grafana-maintenance-hold"
    expected, replacement = (original["spec"], desired) if hold else (desired, original["spec"])
    if live["spec"] == replacement:
        return
    operations = [
        {"op": "test", "path": "/metadata/uid", "value": original["uid"]},
        {"op": "test", "path": "/metadata/resourceVersion", "value": live["metadata"]["resourceVersion"]},
        {"op": "test", "path": "/spec", "value": expected},
        {"op": "replace", "path": "/spec", "value": replacement},
    ]
    annotation_path = "/metadata/annotations/" + RECONCILE.replace("/", "~1")
    if hold:
        operations.append({"op": "add", "path": annotation_path, "value": "disabled"})
    elif original["annotations"].get(RECONCILE) is None:
        operations.append({"op": "remove", "path": annotation_path})
    else:
        operations.append({"op": "add", "path": annotation_path,
                           "value": original["annotations"][RECONCILE]})
    kubectl("patch", "ingress", "grafana-tailscale", "-n", "monitoring",
            "--type=json", "-p", json.dumps(operations))


def assert_hold(state):
    main = get("ingress", "grafana")
    original = state["ingresses"]["grafana"]
    if main["metadata"]["uid"] != original["uid"] or main["spec"] != original["spec"]:
        raise RuntimeError("The managed main Grafana ingress changed")
    route = get("ingressroute", "grafana-maintenance-hold")
    record = next(item for item in state["owned"] if item["kind"] == "IngressRoute")
    if route["metadata"]["uid"] != record["uid"]:
        raise RuntimeError("Maintenance overlay UID changed")
    tail = get("ingress", "grafana-tailscale")
    if tail["metadata"]["uid"] != state["ingresses"]["grafana-tailscale"]["uid"] or \
            tail["spec"]["defaultBackend"]["service"]["name"] != "grafana-maintenance-hold" or \
            tail["metadata"].get("annotations", {}).get(RECONCILE) != "disabled":
        raise RuntimeError("Tailscale hold identity/backend/reconcile guard changed")
    codes = [health_code(url) for url in URLS]
    if codes[0] != 503 or codes[1] not in (0, 404, 502, 503):
        raise RuntimeError(f"Both user ingress paths must remain blocked; status codes={codes}")
    tailscale_backend("grafana-maintenance-hold", state["ingresses"]["grafana-tailscale"]["uid"])


def delete_owned(record):
    allowed = {
        ("Job", "grafana-state-seed"), ("ConfigMap", "grafana-state-seed-code"),
        ("NetworkPolicy", "grafana-state-seed"), ("Service", "grafana-maintenance-hold"),
        ("IngressRoute", "grafana-maintenance-hold"), ("Job", "grafana-silences-restore"),
    }
    if (record["kind"], record["name"]) not in allowed:
        raise RuntimeError("Cleanup refuses all data, workloads, claims and unmanaged resources")
    live = get(record["kind"], record["name"], optional=True)
    if live is None:
        return
    version = record["apiVersion"]
    prefix = "/api/v1" if version == "v1" else "/apis/" + version
    plurals = {"Job": "jobs", "ConfigMap": "configmaps", "NetworkPolicy": "networkpolicies",
               "Service": "services", "IngressRoute": "ingressroutes"}
    uri = f"{prefix}/namespaces/monitoring/{plurals[record['kind']]}/{record['name']}"
    options = {"apiVersion": "v1", "kind": "DeleteOptions", "preconditions": {"uid": record["uid"]},
               "propagationPolicy": "Foreground"}
    kubectl("delete", "--raw", uri, "-f", "-", payload=json.dumps(options))


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("action", choices=("plan", "hold", "seed", "restore-silences", "verify", "release", "abort-hold", "cleanup"), nargs="?", default="plan")
    parser.add_argument("--state-directory", default=".grafana-preservation-state")
    parser.add_argument("--approve-live-writes", action="store_true")
    parser.add_argument("--hold-tailscale-ingress", action="store_true",
                        help="Explicit exception: reversible per-resource Tailscale backend hold")
    args = parser.parse_args()
    directory = Path(args.state_directory)
    if directory.is_absolute() or ".." in directory.parts:
        parser.error("--state-directory must be relative to the current working directory")
    path = directory / "state.json"
    if args.action not in ("plan", "verify") and not args.approve_live_writes:
        parser.error("Live actions require --approve-live-writes; the agent must not invoke them")
    check_prometheus()
    if args.action == "plan":
        if path.exists():
            raise RuntimeError("State already exists; do not overwrite captured pre-hold route identities")
        pod = source_pod()
        inv = inventory(pod)
        ingresses = {name: get("ingress", name) for name in ("grafana", "grafana-tailscale")}
        for name, resource in ingresses.items():
            if "grafana" not in json.dumps(resource["spec"]):
                raise RuntimeError("Unexpected Grafana ingress configuration")
        main_spec = ingresses["grafana"]["spec"]
        if main_spec["rules"][0]["host"] != "grafana.homelab" or main_spec["tls"][0]["secretName"] != "grafana-tls":
            raise RuntimeError("Maintenance host/TLS preconditions changed")
        traefik = json.loads(kubectl("get", "deployment", "traefik", "-n", "kube-system", "-o", "json"))
        arguments = [arg.lower() for c in traefik["spec"]["template"]["spec"]["containers"] for arg in c.get("args", [])]
        if "--providers.kubernetescrd.allowemptyservices=true" not in arguments:
            raise RuntimeError("Empty-service overlay requires Traefik allowEmptyServices=true")
        routes = json.loads(kubectl("get", "ingressroutes.traefik.io", "-A", "-o", "json"))["items"]
        if any("grafana.homelab" in json.dumps(route["spec"]) for route in routes):
            raise RuntimeError("An existing Grafana IngressRoute needs coordinator review")
        codes = [health_code(url) for url in URLS]
        if codes[0] != 200 or codes[1] not in (0, 200):
            raise RuntimeError("Original Grafana user ingress health preconditions failed")
        tail_proof = tailscale_backend("kube-prometheus-stack-grafana", ingresses["grafana-tailscale"]["metadata"]["uid"])
        state = {"run": str(uuid.uuid4()), "sourcePodUID": SOURCE_UID, "owned": [], "phase": "planned",
                 "inventory": inv, "originalHTTPStatusCodes": codes, "tailscaleRuntimeProof": tail_proof,
                 "ingresses": {name: {
                     "uid": item["metadata"]["uid"], "resourceVersion": item["metadata"]["resourceVersion"],
                     "spec": item["spec"], "annotations": item["metadata"].get("annotations", {}),
                 } for name, item in ingresses.items()}}
        directory.mkdir(parents=True, exist_ok=True)
        write_state(path, state)
        print(json.dumps({"phase": state["phase"], "sourcePodUID": SOURCE_UID, "inventory": inv}))
        return
    state = json.loads(path.read_text(encoding="utf-8"))
    if args.action == "hold":
        source_pod()
        if state["phase"] != "planned" or not args.hold_tailscale_ingress:
            raise RuntimeError("Tailscale bypasses Traefik; explicitly approve its scoped reversible hold")
        if inventory(source_pod())["stats"].get("activeSessions") != 0:
            raise RuntimeError("An active Grafana session prevents the final maintenance seed")
        for filename in ("service-grafana-maintenance-hold.yaml", "ingressroute-grafana-maintenance-hold.yaml"):
            document = yaml.safe_load((ASSETS / filename).read_text())
            kind, name = document["kind"], document["metadata"]["name"]
            live = get(kind, name, optional=True)
            owned = next((record for record in state["owned"] if record["kind"] == kind and record["name"] == name), None)
            if live:
                if owned is None or live["metadata"]["uid"] != owned["uid"]:
                    raise RuntimeError("Maintenance resource exists with an unowned UID; refusing to adopt it")
            elif owned:
                raise RuntimeError("An owned maintenance resource disappeared; review before recreating it")
            else:
                apply_owned(document, state, path)
        patch_tailscale(state, True)
        state["tailscaleHeld"] = True
        write_state(path, state)
        for _ in range(36):
            try:
                assert_hold(state)
                break
            except RuntimeError:
                time.sleep(5)
        else:
            raise RuntimeError("Maintenance did not block both routes; do not seed or merge")
        time.sleep(5)
        if inventory(source_pod())["stats"].get("activeSessions") != 0:
            raise RuntimeError("Active sessions remain; keep hold and do not seed")
        state["phase"] = "held"
    elif args.action == "seed":
        if state["phase"] != "held":
            raise RuntimeError("Verified maintenance hold is required before seeding")
        assert_hold(state)
        source_pod()
        inv = inventory(source_pod())
        if inv["stats"].get("activeSessions") != 0:
            raise RuntimeError("Active sessions appeared during maintenance")
        claim = get("pvc", "grafana-data")
        if claim["status"].get("phase") != "Bound" or claim["spec"]["storageClassName"] != "longhorn-retain":
            raise RuntimeError("Phase A claim must be Bound on the retained Longhorn class")
        if claim["spec"]["accessModes"] != ["ReadWriteOnce"] or claim["spec"]["resources"]["requests"]["storage"] != "2Gi":
            raise RuntimeError("Destination claim access/size preconditions changed")
        storage_class = json.loads(kubectl("get", "storageclass", "longhorn-retain", "-o", "json"))
        parameters = storage_class["parameters"]
        jobs = {item["name"] for item in json.loads(parameters["recurringJobSelector"])}
        if parameters["numberOfReplicas"] != "2" or not {"backup-daily", "snapshot-weekly"} <= jobs:
            raise RuntimeError("Normal two-replica backup/snapshot policy must remain enabled")
        pv = json.loads(kubectl("get", "pv", claim["spec"]["volumeName"], "-o", "json"))
        volume = json.loads(kubectl("get", "volumes.longhorn.io", claim["spec"]["volumeName"], "-n", "storage", "-o", "json"))
        if pv["spec"]["persistentVolumeReclaimPolicy"] != "Retain" or volume["spec"]["numberOfReplicas"] != 2:
            raise RuntimeError("Destination volume retention/replica preconditions changed")
        state["destinationPVCUID"] = claim["metadata"]["uid"]
        state["inventory"] = inv
        write_state(path, state)
        documents = list(yaml.safe_load_all(kubectl("kustomize", str(ASSETS))))
        documents.sort(key=lambda item: item["kind"] == "Job")
        for document in documents:
            if get(document["kind"], document["metadata"]["name"], optional=True):
                raise RuntimeError("Seed resource already exists; refusing to replay or adopt it")
            if document["kind"] == "Job":
                env = document["spec"]["template"]["spec"]["containers"][0]["env"]
                next(item for item in env if item["name"] == "EXPECTED_DESTINATION_PVC_UID")["value"] = state["destinationPVCUID"]
            apply_owned(document, state, path)
        deadline = time.monotonic() + 650
        while time.monotonic() < deadline:
            job = get("job", "grafana-state-seed")
            if job.get("status", {}).get("failed"):
                raise RuntimeError("Seed failed; retain both source and destination and keep maintenance hold")
            if job.get("status", {}).get("succeeded"):
                break
            time.sleep(5)
        else:
            raise RuntimeError("Seed deadline elapsed; do not merge or remove hold")
        receipt = json.loads(kubectl("logs", "-n", "monitoring", "job/grafana-state-seed"))
        if receipt.get("sourcePodUID") != SOURCE_UID or receipt.get("destinationPVCUID") != state["destinationPVCUID"]:
            raise RuntimeError("Seed receipt identities do not match")
        source_pod()
        assert_hold(state)
        state["receipt"] = receipt
        state["silencesRestored"] = receipt["silenceCount"] == 0
        state["phase"] = "seeded"
    elif args.action == "restore-silences":
        if state["phase"] != "seeded":
            raise RuntimeError("Seed receipt required before native silence import")
        assert_hold(state)
        if get("pvc", "grafana-data")["metadata"]["uid"] != state["destinationPVCUID"]:
            raise RuntimeError("Destination PVC UID changed")
        pod = source_pod(required=False)
        document = yaml.safe_load((ASSETS / "job-grafana-silences-restore.yaml").read_text())
        # RWO can be shared with the running Grafana only on the same node.
        document["spec"]["template"]["spec"]["nodeSelector"] = {"kubernetes.io/hostname": pod["spec"]["nodeName"]}
        am = get("pod", "alertmanager-kube-prometheus-stack-alertmanager-0")
        image = next(item["imageID"] for item in am["status"]["containerStatuses"] if item["name"] == "alertmanager")
        if "@sha256:" not in image:
            raise RuntimeError("An exact cached Alertmanager image digest is required")
        document["spec"]["template"]["spec"]["containers"][0]["image"] = image.removeprefix("docker-pullable://")
        if get("job", "grafana-silences-restore", optional=True):
            raise RuntimeError("Refusing to replay an existing silence import")
        apply_owned(document, state, path)
        kubectl("wait", "-n", "monitoring", "--for=condition=complete",
                "job/grafana-silences-restore", "--timeout=150s")
        if json.loads(kubectl("logs", "-n", "monitoring", "job/grafana-silences-restore")) != {"silenceImportSucceeded": True}:
            raise RuntimeError("Native silence import did not complete")
        state["silencesRestored"] = True
    elif args.action == "verify":
        if state["phase"] not in ("seeded", "verified"):
            raise RuntimeError("Successful seed receipt is required")
        if not state.get("silencesRestored"):
            raise RuntimeError("Native silence import is required before verification")
        release = get("helmrelease", "kube-prometheus-stack")
        if release["spec"]["chart"]["spec"]["version"] != "92.13.1" or not any(
                condition["type"] == "Ready" and condition["status"] == "True"
                and condition.get("observedGeneration") == release["metadata"]["generation"]
                for condition in release.get("status", {}).get("conditions", [])):
            raise RuntimeError("Exact target HelmRelease must be Ready at its current generation")
        assert_hold(state)
        pod = source_pod(required=False)
        storage = next(volume for volume in pod["spec"]["volumes"] if volume["name"] == "storage")
        if storage.get("persistentVolumeClaim", {}).get("claimName") != "grafana-data":
            raise RuntimeError("Upgraded Grafana is not using the seeded durable claim")
        if get("pvc", "grafana-data")["metadata"]["uid"] != state["destinationPVCUID"]:
            raise RuntimeError("Destination PVC UID changed")
        after = inventory(pod)
        before = state["inventory"]
        for key in ("users", "orgs", "playlists", "stars"):
            if after["stats"].get(key) != before["stats"].get(key):
                raise RuntimeError("Saved Grafana object count changed")
        if after["dashboards"] < before["dashboards"] or after["database"] != "ok":
            raise RuntimeError("Grafana database/dashboard verification failed")
        for key in ("preferenceFieldsSet", "orgPreferenceFieldsSet"):
            if before[key] != after[key]:
                raise RuntimeError("Grafana preference inventory changed")
        old_ids = {plugin["id"] for plugin in before["plugins"] if plugin.get("enabled")}
        new_ids = {plugin["id"] for plugin in after["plugins"] if plugin.get("enabled")}
        if not old_ids <= new_ids:
            raise RuntimeError("An existing enabled plugin is missing or disabled")
        state["postUpgradeInventory"] = after
        state["phase"] = "verified"
    elif args.action in ("release", "abort-hold"):
        if args.action == "release":
            if state["phase"] not in ("verified", "released"):
                raise RuntimeError("Internal post-upgrade verification must precede hold removal")
            if state["phase"] == "verified":
                assert_hold(state)
        else:
            # Recover a failed pre-upgrade hold only while the untouched source is healthy.
            if get("helmrelease", "kube-prometheus-stack")["spec"]["chart"]["spec"]["version"] != "88.6.5":
                raise RuntimeError("abort-hold is only available before the chart upgrade")
            if inventory(source_pod())["database"] != "ok":
                raise RuntimeError("Do not abort the hold unless the original Grafana is healthy")
            job = get("job", "grafana-state-seed", optional=True)
            if job and job.get("status", {}).get("active"):
                raise RuntimeError("Wait for the bounded seed Job to finish before aborting hold")
            state["aborted"] = True
        patch_tailscale(state, False)
        state["tailscaleHeld"] = False
        for record in state["owned"]:
            if record["kind"] == "IngressRoute":
                delete_owned(record)
        state["phase"] = "released"
        write_state(path, state)
        for _ in range(30):
            codes = [health_code(url) for url in URLS]
            if codes[0] == 200 and codes[1] in (0, 200):
                try:
                    state["tailscaleRuntimeProof"] = tailscale_backend(
                        "kube-prometheus-stack-grafana", state["ingresses"]["grafana-tailscale"]["uid"])
                    state["releasedHTTPStatusCodes"] = codes
                    state["publicHealthy"] = True
                    break
                except RuntimeError:
                    pass
            time.sleep(5)
        else:
            raise RuntimeError("Routes restored but public health not yet recovered; do not clean up")
    elif args.action == "cleanup":
        if state["phase"] != "released" or not state.get("publicHealthy"):
            raise RuntimeError("Cleanup requires successful verification and released routes")
        for record in reversed(state["owned"]):
            delete_owned(record)
        state["phase"] = "cleaned"
    write_state(path, state)
    print(json.dumps({"phase": state["phase"], "receipt": state.get("receipt")}))


if __name__ == "__main__":
    main()
