#!/usr/bin/env python3
"""Check monitoring lifecycle policy and optionally compare exact Helm renders."""

import argparse
import configparser
import hashlib
import importlib.util
import json
from pathlib import Path
import shutil
import sqlite3
import sys
import uuid
from unittest import mock

import yaml


ROOT = Path(__file__).resolve().parents[2]
RELEASE = ROOT / "infrastructure/monitoring/helmrelease-kube-prometheus-stack.yaml"


def documents(path):
    # Kubernetes CRD schemas can contain the unquoted enum value '='.
    class Loader(yaml.SafeLoader):
        pass

    Loader.add_constructor(
        "tag:yaml.org,2002:value", lambda loader, node: loader.construct_scalar(node)
    )
    return [item for item in yaml.load_all(path.read_text(encoding="utf-8-sig"), Loader=Loader) if item]


def compare_renders(baseline, target):
    def index(items):
        return {(item["kind"], item["metadata"]["name"]): item for item in items}

    old, new = index(baseline), index(target)
    for key, item in old.items():
        if key[0] == "CustomResourceDefinition":
            assert key in new, f"CRD removed: {key[1]}"
            served = {v["name"] for v in new[key]["spec"]["versions"] if v["served"]}
            assert all(v["name"] in served for v in item["spec"]["versions"] if v["storage"]), key

    prometheus = ("Prometheus", "kube-prometheus-stack-prometheus")
    for field in (
        "storage", "retention", "resources", "query", "scrapeInterval",
        "evaluationInterval", "serviceMonitorSelector", "ruleSelector", "probeSelector",
    ):
        assert old[prometheus]["spec"][field] == new[prometheus]["spec"][field], field
    assert new[prometheus]["spec"]["nodeSelector"]["kubernetes.io/os"] == "linux"
    assert not new[prometheus]["spec"].get("enableFeatures"), "Review new TSDB feature flags"

    grafana = ("Deployment", "kube-prometheus-stack-grafana")
    before = old[grafana]["spec"]["template"]["spec"]
    after = new[grafana]["spec"]["template"]["spec"]
    containers = {container["name"]: container for container in after["containers"]}
    for previous in before["containers"]:
        current = containers[previous["name"]]
        old_auth = [env for env in previous.get("env", []) if env.get("valueFrom", {}).get("secretKeyRef")]
        new_auth = [env for env in current.get("env", []) if env.get("valueFrom", {}).get("secretKeyRef")]
        assert old_auth == new_auth, f"Grafana credential references changed: {current['name']}"
    main = containers["grafana"]
    storage = next(volume for volume in after["volumes"] if volume["name"] == "storage")
    assert storage["persistentVolumeClaim"]["claimName"] == "grafana-data"
    assert "emptyDir" not in storage, "Volume must not contain both emptyDir and a PVC"
    assert not after.get("initContainers"), "Seed ownership must not require a privileged chown init"
    assert main["securityContext"]["readOnlyRootFilesystem"] is True
    assert any(mount["mountPath"] == "/tmp" for mount in main["volumeMounts"])
    assert not any(env["name"].endswith("__FILE") or env["name"] == "GF_INSTALL_PLUGINS"
                   for env in main.get("env", [])), "Unsupported distroless environment"
    for name in ("kube-prometheus-stack-grafana", "kube-prometheus-stack-grafana-datasource"):
        key = ("ConfigMap", name)
        if name.endswith("-datasource"):
            assert old[key]["data"] == new[key]["data"], f"Grafana configuration changed: {name}"
        else:
            previous, current = configparser.ConfigParser(), configparser.ConfigParser()
            previous.read_string(old[key]["data"]["grafana.ini"])
            current.read_string(new[key]["data"]["grafana.ini"])
            # Grafana chart 14 disables automatic bundled-plugin updates.
            assert dict(current["plugins"]) == {"preinstall_auto_update": "false"}
            current.remove_section("plugins")
            assert {s: dict(previous[s]) for s in previous} == \
                {s: dict(current[s]) for s in current}, "Grafana INI configuration changed"
    old_dashboards = {key for key, item in old.items()
                      if item["metadata"].get("labels", {}).get("grafana_dashboard") == "1"}
    assert old_dashboards <= new.keys(), "Provisioned dashboard removed"

    token = new[("Secret", "kube-prometheus-stack-prometheus-token")]
    assert token["type"] == "kubernetes.io/service-account-token"
    assert token["metadata"]["annotations"]["kubernetes.io/service-account.name"] == \
        new[prometheus]["spec"]["serviceAccountName"]
    for name in ("kube-prometheus-stack-apiserver", "kube-prometheus-stack-kubelet"):
        monitor = new[("ServiceMonitor", name)]
        for endpoint in monitor["spec"]["endpoints"]:
            assert "bearerTokenFile" not in endpoint
            assert endpoint["authorization"]["credentials"] == {
                "name": token["metadata"]["name"], "key": "token"
            }
            assert endpoint["tlsConfig"]["ca"]["configMap"] == {
                "name": "kube-root-ca.crt", "key": "ca.crt"
            }


def preservation_checks():
    assets = ROOT / "infrastructure/monitoring/preservation"
    job = documents(assets / "job-grafana-state-seed.yaml")[0]
    pod = job["spec"]["template"]["spec"]
    assert job["spec"]["backoffLimit"] == 0 and job["spec"]["activeDeadlineSeconds"] == 600
    assert pod["automountServiceAccountToken"] is False
    assert pod["nodeSelector"] == {"kubernetes.io/hostname": "rishik-worker2"}
    assert {key: pod["securityContext"][key] for key in ("runAsUser", "runAsGroup", "fsGroup")} == \
        {"runAsUser": 472, "runAsGroup": 472, "fsGroup": 472}
    container = pod["containers"][0]
    assert container["image"] == "quay.io/kiwigrid/k8s-sidecar@sha256:7eac5c4fed714a18d038fc9fea57d8744d113367935dac0ea4eb6a87cef704a3"
    assert container["securityContext"]["readOnlyRootFilesystem"] is True
    assert container["securityContext"]["allowPrivilegeEscalation"] is False
    assert container["securityContext"]["capabilities"] == {"drop": ["ALL"]}
    volumes = {volume["name"]: volume for volume in pod["volumes"]}
    assert volumes["source"]["hostPath"] == {
        "path": "/var/lib/kubelet/pods/62fb40e7-ab02-46d5-b6fc-0a7b4d05633d/volumes/kubernetes.io~empty-dir/storage",
        "type": "Directory",
    }
    assert next(mount for mount in container["volumeMounts"] if mount["name"] == "source")["readOnly"] is True
    assert volumes["destination"]["persistentVolumeClaim"]["claimName"] == "grafana-data"
    policy = documents(assets / "networkpolicy-grafana-state-seed.yaml")[0]["spec"]
    assert policy["ingress"] == [] and policy["policyTypes"] == ["Ingress", "Egress"]
    assert len(policy["egress"]) == 2
    assert policy["egress"][0]["ports"] == [{"protocol": "UDP", "port": 53}, {"protocol": "TCP", "port": 53}]
    assert policy["egress"][1] == {
        "to": [{"podSelector": {"matchLabels": {
            "app.kubernetes.io/name": "alertmanager", "alertmanager": "kube-prometheus-stack-alertmanager",
        }}}], "ports": [{"protocol": "TCP", "port": 9093}],
    }
    route = documents(assets / "ingressroute-grafana-maintenance-hold.yaml")[0]["spec"]
    assert route["entryPoints"] == ["websecure"] and route["tls"] == {"secretName": "grafana-tls"}
    assert route["routes"][0]["match"] == "Host(`grafana.homelab`)" and route["routes"][0]["priority"] == 1000000
    assert "selector" not in documents(assets / "service-grafana-maintenance-hold.yaml")[0]["spec"]
    root_resources = documents(ROOT / "infrastructure/monitoring/kustomization.yaml")[0]["resources"]
    assert not any("preservation" in resource for resource in root_resources), "Helpers must never auto-run under Flux"
    assert root_resources.count("pvc-grafana-data.yaml") == 1
    claim = documents(ROOT / "infrastructure/monitoring/pvc-grafana-data.yaml")[0]
    assert claim["metadata"]["annotations"]["kustomize.toolkit.fluxcd.io/prune"] == "disabled"
    assert claim["spec"] == {
        "accessModes": ["ReadWriteOnce"], "storageClassName": "longhorn-retain",
        "resources": {"requests": {"storage": "2Gi"}},
    }
    storage_class = documents(ROOT / "infrastructure/storage/longhorn/storageclass-longhorn-retain.yaml")[0]
    assert storage_class["reclaimPolicy"] == "Retain" and storage_class["parameters"]["numberOfReplicas"] == "2"
    assert {item["name"] for item in json.loads(storage_class["parameters"]["recurringJobSelector"])} == \
        {"snapshot-weekly", "backup-daily"}
    workflow = documents(ROOT / ".github/workflows/ci.yaml")[0]
    commands = [step.get("run") for step in workflow["jobs"]["yaml-lint"]["steps"]]
    assert "python3 .github/scripts/test-monitoring-stack-upgrade.py" in commands
    runner_spec = importlib.util.spec_from_file_location("grafana_runner", ROOT / ".github/scripts/preserve-grafana-state.py")
    runner = importlib.util.module_from_spec(runner_spec)
    sys.dont_write_bytecode = True
    runner_spec.loader.exec_module(runner)
    assert "_create_unverified_context" not in Path(runner_spec.origin).read_text()
    with mock.patch.object(runner.urllib.request, "urlopen") as request:
        request.return_value.__enter__.return_value.status = 200
        assert runner.health_code("https://grafana.homelab/api/health") == 200
        context = request.call_args.kwargs["context"]
        assert context.check_hostname is True and context.verify_mode == runner.ssl.CERT_REQUIRED
    for tls_failure in (
        runner.ssl.SSLError("fixture TLS failure"),
        runner.urllib.error.URLError(runner.ssl.SSLCertVerificationError("fixture verification failure")),
    ):
        with mock.patch.object(runner.urllib.request, "urlopen", side_effect=tls_failure):
            try:
                runner.health_code("https://grafana.homelab/api/health")
            except runner.ssl.SSLError as error:
                assert not isinstance(error, RuntimeError), "Maintenance retries must not mask TLS failures"
            else:
                raise AssertionError("TLS failure must not become an unavailable-probe fallback")
    for failure, expected in (
        (runner.urllib.error.HTTPError("https://grafana.homelab", 503, "maintenance", {}, None), 503),
        (runner.urllib.error.URLError("fixture DNS failure"), 0),
    ):
        with mock.patch.object(runner.urllib.request, "urlopen", side_effect=failure):
            assert runner.health_code("https://grafana.homelab/api/health") == expected
    record = {"kind": "Job", "name": "grafana-state-seed", "apiVersion": "batch/v1", "uid": "owned-helper-uid"}
    with mock.patch.object(runner, "get", return_value={"metadata": {"uid": record["uid"]}}), \
            mock.patch.object(runner, "kubectl") as transport:
        runner.delete_owned(record)
        options = json.loads(transport.call_args.kwargs["payload"])
        assert options["preconditions"] == {"uid": record["uid"]}
        assert transport.call_args.args[:2] == ("delete", "--raw")
    with mock.patch.object(runner, "kubectl") as transport:
        try:
            runner.delete_owned({"kind": "PersistentVolumeClaim", "name": "grafana-data"})
        except RuntimeError:
            pass
        else:
            raise AssertionError("Cleanup must never delete any PVC")
        transport.assert_not_called()
    with mock.patch.object(runner, "get", return_value={"items": []}):
        try:
            runner.source_pod()
        except RuntimeError:
            pass
        else:
            raise AssertionError("Missing exact source UID must fail closed")
    proxy = {"metadata": {"namespace": "tailscale", "name": "proxy", "uid": "proxy-uid", "labels": {
        "app": "captured-ingress-uid", "tailscale.com/parent-resource-ns": "monitoring",
    }}, "status": {"containerStatuses": [{"ready": True}]}}
    wrong_backend = False
    has_endpoint = False

    def fake_proxy_command(*arguments, **kwargs):
        if arguments[:2] == ("get", "pods"):
            return json.dumps({"items": [proxy]})
        if arguments[0] == "exec":
            ip = "10.0.0.9" if wrong_backend else "10.0.0.1"
            return json.dumps({"Web": {"grafana:443": {"Handlers": {"/": {"Proxy": f"http://{ip}:80/"}}}}})
        if arguments[:2] == ("get", "endpointslices.discovery.k8s.io"):
            return json.dumps({"items": [{"endpoints": [{"addresses": ["10.0.0.2"]}]}] if has_endpoint else []})
        raise AssertionError("Unexpected proxy test command")

    def fake_proxy_get(kind, *arguments, **kwargs):
        return {"spec": {"clusterIP": "10.0.0.1"}} if kind == "service" else None

    with mock.patch.object(runner, "get", side_effect=fake_proxy_get), \
            mock.patch.object(runner, "kubectl", side_effect=fake_proxy_command):
        assert runner.tailscale_backend("grafana-maintenance-hold", "captured-ingress-uid")["readyBackendAddresses"] == 0
        for unsafe in ("wrong-backend", "nonempty-service"):
            wrong_backend = unsafe == "wrong-backend"
            has_endpoint = unsafe == "nonempty-service"
            try:
                runner.tailscale_backend("grafana-maintenance-hold", "captured-ingress-uid")
            except RuntimeError:
                pass
            else:
                raise AssertionError("Unavailable client DNS must never bypass runtime ingress protection")

    specification = importlib.util.spec_from_file_location("grafana_seed", assets / "grafana-state-seed.py")
    helper = importlib.util.module_from_spec(specification)
    sys.dont_write_bytecode = True
    specification.loader.exec_module(helper)
    fixture = Path(".monitoring-preservation-checks-" + str(uuid.uuid4()))
    fixture.mkdir()
    try:
        source, destination = fixture / "source", fixture / "destination"
        source.mkdir()
        destination.mkdir()
        database = source / "grafana.db"
        with sqlite3.connect(database) as connection:
            connection.executescript("CREATE TABLE user (id INTEGER); INSERT INTO user VALUES(1);")
        connection.close()
        before = hashlib.sha256(database.read_bytes()).digest()
        assert helper.backup_database(database, destination / "grafana.db") == {"user": 1}
        assert hashlib.sha256(database.read_bytes()).digest() == before, "Source database was mutated"
        try:
            helper.backup_database(database, destination / "grafana.db")
        except RuntimeError:
            pass
        else:
            raise AssertionError("Seed must refuse to overwrite an existing destination database")
        (source / "plugins").mkdir()
        (source / "plugins/plugin.json").write_text('{"id":"fixture-plugin"}')
        for name in helper.SIDECARS:
            (source / name).write_text("do-not-copy")
        assert helper.copy_other_files(source, destination)["nonDatabaseFiles"] == 1
        assert (destination / "plugins/plugin.json").read_bytes() == (source / "plugins/plugin.json").read_bytes()
        assert all(not (destination / name).exists() for name in helper.SIDECARS)

        # A WAL writer commits after counting, but the held read snapshot must win.
        wal = fixture / "wal.db"
        writer = sqlite3.connect(wal)
        writer.execute("PRAGMA journal_mode=WAL")
        writer.executescript("CREATE TABLE user (id INTEGER); INSERT INTO user VALUES(1);")
        original = helper.table_counts
        changed = False

        def racing_counts(connection):
            nonlocal changed
            result = original(connection)
            if not changed:
                changed = True
                writer.execute("INSERT INTO user VALUES(2)")
                writer.commit()
            return result

        try:
            with mock.patch.object(helper, "table_counts", side_effect=racing_counts):
                assert helper.backup_database(wal, fixture / "wal-backup.db") == {"user": 1}
            assert original(writer) == {"user": 2}
        finally:
            writer.close()
    finally:
        shutil.rmtree(fixture)
    print("PASS: restricted helper, retention/backup policy, native SQLite/WAL snapshot and overwrite guards")


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--baseline", type=Path)
    parser.add_argument("--target", type=Path)
    args = parser.parse_args()
    if bool(args.baseline) != bool(args.target):
        parser.error("--baseline and --target must be supplied together")
    release = documents(RELEASE)[0]["spec"]
    assert release["upgrade"]["crds"] == "CreateReplace", "Flux must upgrade the Operator CRDs"
    assert release["upgrade"]["serverSideApply"] == "disabled", "PVC migration requires Helm three-way patching"
    assert release["rollback"]["serverSideApply"] == "disabled"
    assert not release["values"].get("crds", {}).get("upgradeJob", {}).get("enabled", False), \
        "Use Flux CRD lifecycle management, not a second CRD writer"
    spec = release["values"]["prometheus"]["prometheusSpec"]
    assert spec["retention"] == "7d"
    assert spec["storageSpec"]["volumeClaimTemplate"]["spec"] == {
        "storageClassName": "longhorn", "accessModes": ["ReadWriteOnce"],
        "resources": {"requests": {"storage": "10Gi"}},
    }
    assert release["values"]["grafana"]["admin"]["existingSecret"] == "grafana-admin-credentials"
    assert release["values"]["grafana"]["persistence"] == {"enabled": True, "existingClaim": "grafana-data"}
    assert release["values"]["grafana"]["initChownData"]["enabled"] is False
    preservation_checks()
    if args.baseline:
        compare_renders(documents(args.baseline), documents(args.target))
        print("PASS: rendered CRD, PVC, TSDB, Grafana and ServiceMonitor upgrade contracts")
    print("PASS: monitoring HelmRelease lifecycle and data-preservation policy")


if __name__ == "__main__":
    main()
