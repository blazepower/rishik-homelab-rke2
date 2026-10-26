#!/usr/bin/env python3
"""Check monitoring lifecycle policy and optionally compare exact Helm renders."""

import argparse
import configparser
from pathlib import Path

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


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--baseline", type=Path)
    parser.add_argument("--target", type=Path)
    args = parser.parse_args()
    if bool(args.baseline) != bool(args.target):
        parser.error("--baseline and --target must be supplied together")
    release = documents(RELEASE)[0]["spec"]
    assert release["upgrade"]["crds"] == "CreateReplace", "Flux must upgrade the Operator CRDs"
    assert not release["values"].get("crds", {}).get("upgradeJob", {}).get("enabled", False), \
        "Use Flux CRD lifecycle management, not a second CRD writer"
    spec = release["values"]["prometheus"]["prometheusSpec"]
    assert spec["retention"] == "7d"
    assert spec["storageSpec"]["volumeClaimTemplate"]["spec"] == {
        "storageClassName": "longhorn", "accessModes": ["ReadWriteOnce"],
        "resources": {"requests": {"storage": "10Gi"}},
    }
    assert release["values"]["grafana"]["admin"]["existingSecret"] == "grafana-admin-credentials"
    if args.baseline:
        compare_renders(documents(args.baseline), documents(args.target))
        print("PASS: rendered CRD, PVC, TSDB, Grafana and ServiceMonitor upgrade contracts")
    print("PASS: monitoring HelmRelease lifecycle and data-preservation policy")


if __name__ == "__main__":
    main()
