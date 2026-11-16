#!/usr/bin/env python3
"""Exercise actual storage alerts and reboot dashboards with pinned native promtool."""

import argparse
import json
from pathlib import Path
import re
import subprocess
import tempfile

import yaml

ROOT = Path(__file__).resolve().parents[2]
IMAGE = "quay.io/prometheus/prometheus@sha256:9764044e09eb2c9e1abdb12d0f0188d4efb7384b0262d3c080f0fc59045c89ba"
ALERT = "NodePhysicalDiskWriteLatencyHigh"


def inputs(instance="node-a:9100", device="nvme0n1", nodename="fixture-nvme",
           write_time="0+900x10", writes="0+3000x10", queue="0+900x10", model="fixture-physical-disk"):
    labels = f'job="node-exporter",instance="{instance}",device="{device}"'
    series = [
        {"series": f"node_disk_write_time_seconds_total{{{labels}}}", "values": write_time},
        {"series": f"node_disk_writes_completed_total{{{labels}}}", "values": writes},
        {"series": f"node_disk_io_time_weighted_seconds_total{{{labels}}}", "values": queue},
        {"series": f'node_uname_info{{job="node-exporter",instance="{instance}",nodename="{nodename}"}}',
         "values": "1x10"},
    ]
    if model is not None:
        series.append({"series": f'node_disk_info{{{labels},model="{model}"}}', "values": "1x10"})
    return series


def expected(instance, device, nodename):
    return {
        "exp_labels": {"job": "node-exporter", "instance": instance, "device": device,
                       "nodename": nodename, "severity": "warning"},
        "exp_annotations": {
            "summary": f"High physical disk write latency on {nodename} ({device})",
            "description": "Physical disk writes average 0.300s over 2 minutes, "
                           "with more than 10 completed writes/s and average queue depth above 8. "
                           "Investigate host and workload I/O; do not automatically restart storage or uncordon nodes.",
        },
    }


def case(name, series, evaluations):
    return {
        "name": name, "interval": "30s", "input_series": series,
        "alert_rule_test": [{"eval_time": time, "alertname": ALERT, "exp_alerts": alerts}
                            for time, alerts in evaluations],
    }


def node_observation(node, flag=0, kernel=0, security=0, success=None,
                     attempt=None, metadata=None, repeats=4, job="observer"):
    labels = f'node="{node}",job="{job}"'
    fresh = f"0+30x{repeats}"
    series = [{"series": f"homelab_node_observer_last_attempt_seconds{{{labels}}}",
               "values": fresh if attempt is None else attempt}]
    for check, metric, value in [
        ("reboot", "homelab_node_reboot_required", flag),
        ("kernel", "homelab_node_kernel_abi_pending", kernel),
        ("security", "homelab_node_apt_security_updates_available", security),
    ]:
        if value is not None:
            series.append({"series": f"{metric}{{{labels}}}", "values": f"{value}x{repeats}"})
        valid = (success or {}).get(check, int(value is not None))
        series.append({"series": f'homelab_node_observer_check_success{{{labels},check="{check}"}}',
                       "values": f"{valid}x{repeats}"})
    if security is not None:
        series.append({"series": f"homelab_node_apt_security_metadata_seconds{{{labels}}}",
                       "values": fresh if metadata is None else metadata})
    return series


def reboot_dashboard_cases():
    directory = ROOT / "infrastructure" / "monitoring" / "custom-dashboards"
    expressions = []
    for filename, key, panel_id in [
        ("observability-homepage.yaml", "observability-homepage.json", 33),
        ("configmap-node-health-dashboard.yaml", "node-health.json", 7),
    ]:
        dashboard = json.loads(yaml.safe_load((directory / filename).read_text(encoding="utf-8"))["data"][key])
        panel = next(p for p in dashboard["panels"] if p["id"] == panel_id)
        target, = panel["targets"]
        assert target.get("instant") is True and target.get("range") is False, (
            "Current reboot status must not reduce old range samples with lastNotNull")
        defaults = panel["fieldConfig"]["defaults"]
        assert defaults.get("noValue") == "Unknown"
        assert any(mapping["type"] == "special" and mapping["options"]["match"] == "null+nan" and
                   mapping["options"]["result"]["text"] == "Unknown" and
                   mapping["options"]["result"]["color"] == "gray" for mapping in defaults["mappings"]), (
            "Missing/NaN reboot telemetry must not use a healthy value/color")
        expressions.append(target["expr"])
    nodes = ("fixture-a", "fixture-b", "fixture-c")
    inventory = [{"series": f'kube_node_info{{node="{node}",job="inventory"}}', "values": "1x4"}
                 for node in nodes]

    def telemetry(values, job="observer"):
        return [entry for node, value in values.items() for entry in node_observation(node, flag=value, job=job)]

    zero = {node: 0 for node in nodes}
    mixed = dict(zip(nodes, (1, 0, 1)))
    partial = {nodes[0]: 1}
    scenarios = [
        ("missing telemetry is unknown", inventory, [], None, {}),
        ("partial coverage preserves known positives but not a cluster zero",
         inventory, telemetry(partial), None, partial),
        ("complete measured zeros are healthy", inventory, telemetry(zero), 0, zero),
        ("complete measured requirements are counted", inventory, telemetry(mixed), 2, mixed),
        ("nonmember metrics are ignored", inventory, telemetry({**zero, "outside": 1}), 0, zero),
        ("duplicate scrapes count a node once", inventory,
         telemetry({**zero, nodes[0]: 1}) + telemetry({nodes[0]: 1}, "duplicate"), 1,
         {**zero, nodes[0]: 1}),
        ("missing membership is unknown", [], telemetry(zero), None, {}),
        ("duplicate inventories do not double count", inventory + [
            {"series": f'kube_node_info{{node="{nodes[0]}",job="duplicate"}}', "values": "1x4"}],
         telemetry(mixed), 2, mixed),
        ("kernel pending is a known positive even without a reboot flag", inventory,
         node_observation(nodes[0], flag=None, kernel=1), None, {nodes[0]: 1}),
        ("flag positive is preserved when only the kernel check fails", inventory,
         node_observation(nodes[0], flag=1, kernel=None), None, {nodes[0]: 1}),
        ("flag zero alone cannot mean no reboot", inventory,
         node_observation(nodes[0], flag=0, kernel=None), None, {}),
        ("failed check cannot authorize raw zeros", inventory,
         node_observation(nodes[0], success={"reboot": 0}), None, {}),
        ("future observer clock is unknown", inventory,
         node_observation(nodes[0], attempt="120+30x4"), None, {}),
        ("a different fresh scrape cannot authorize a future source's zeros", inventory,
         node_observation(nodes[0], attempt="120+30x4", job="future") +
         node_observation(nodes[0], flag=None, kernel=None, job="fresh"), None, {}),
    ]
    tests = []
    node_expression = "(" + expressions[1] + ")"
    for name, members, reboot, total, known in scenarios:
        expected_nodes = sorted({entry["series"].split('node="')[1].split('"')[0] for entry in members})
        tests.append({
            "name": "reboot dashboard: " + name, "interval": "30s", "input_series": members + reboot,
            "promql_expr_test": [
                {"expr": expressions[0], "eval_time": "1m",
                 "exp_samples": [] if total is None else [{"labels": "{}", "value": total}]},
                {"expr": f"{node_expression} == on (node) {node_expression}", "eval_time": "1m", "exp_samples": [
                    {"labels": f'{{node="{node}"}}', "value": known[node]}
                    for node in expected_nodes if node in known
                ]},
                # The pinned matcher rejects NaN-valued expected samples; assert exact NaN membership separately.
                {"expr": f"count by (node) ({node_expression} != on (node) {node_expression})",
                 "eval_time": "1m", "exp_samples": [
                    {"labels": f'{{node="{node}"}}', "value": 1}
                    for node in expected_nodes if node not in known
                ]},
            ],
        })
    return tests


def node_observer_cases(node_rules):
    node = "fixture-a"
    inventory = [{"series": f'kube_node_info{{node="{node}",job="inventory"}}', "values": "1x3001"}]
    metrics = ["homelab:node_reboot_flag:current", "homelab:node_kernel_abi_pending:current",
               "homelab:node_security_updates_available:current", "homelab:node_reboot_required:current"]
    scenarios = [
        ("healthy zeros", node_observation(node), "1m", [0, 0, 0, 0]),
        ("ABI update requires reboot despite zero flag", node_observation(node, kernel=1), "1m", [0, 1, 0, 1]),
        ("invalid nonbinary flag is unknown", node_observation(node, flag=2), "1m", [None, 0, 0, None]),
        ("failed security check is not zero patches", node_observation(node, success={"security": 0}),
         "1m", [0, 0, None, 0]),
        ("frozen file goes stale despite continuing successful scrapes",
         node_observation(node, attempt="0x100", repeats=100), "31m", [None, None, None, None]),
        ("security publication expires between otherwise fresh successful attempts",
         node_observation(node, metadata="0x3001", repeats=3001), "25h", [0, 0, None, 0]),
    ]
    tests = []
    for name, series, when, values in scenarios:
        tests.append({
            "name": "observer rules: " + name, "interval": "30s", "input_series": inventory + series,
            "promql_expr_test": [
                {"expr": metric, "eval_time": when,
                 "exp_samples": [] if value is None else [
                     {"labels": metric + f'{{node="{node}"}}', "value": value}]}
                for metric, value in zip(metrics, values)
            ],
        })
    alerts = {rule["alert"]: rule for group in node_rules["groups"] for rule in group["rules"] if "alert" in rule}

    def expected_alert(name):
        rule = alerts[name]
        return {"exp_labels": {**rule["labels"], "node": node}, "exp_annotations": {
            key: text.replace("{{ $labels.node }}", node) for key, text in rule["annotations"].items()}}

    for name, series, evaluations in [
        ("missing observer holds before alerting", [], [
            ("NodeRebootTelemetryMissing", "14m", False), ("NodeRebootTelemetryMissing", "15m", True),
            ("NodePatchTelemetryMissing", "14m", False), ("NodePatchTelemetryMissing", "15m", True)]),
        ("security publication expiry raises unknown not false healthy",
         node_observation(node, metadata="0x3001", repeats=3001),
         [("NodePatchTelemetryMissing", "25h", True), ("NodeRebootTelemetryMissing", "25h", False)]),
        ("persistent security packages respect daily patch hold",
         node_observation(node, security=2, repeats=3001),
         [("NodeSecurityUpdatesAvailable", "23h59m", False), ("NodeSecurityUpdatesAvailable", "24h", True),
          ("NodePatchTelemetryMissing", "25h", False)]),
        ("ABI pending respects reboot holds without Kured",
         node_observation(node, kernel=1, repeats=3001),
         [("NodeRebootPending", "59m", False), ("NodeRebootPending", "1h", True),
          ("NodeRebootRequired", "23h59m", False), ("NodeRebootRequired", "24h", True)]),
    ]:
        tests.append({"name": "observer alerts: " + name, "interval": "30s", "input_series": inventory + series,
                      "alert_rule_test": [
                          {"alertname": alert, "eval_time": when,
                           "exp_alerts": [expected_alert(alert)] if firing else []}
                          for alert, when, firing in evaluations]})
    return tests


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--docker", action="store_true", required=True)
    parser.add_argument("--evidence", type=Path)
    args = parser.parse_args()
    rules = yaml.safe_load(
        (ROOT / "infrastructure" / "monitoring" / "prometheusrules-storage-health-alerts.yaml").read_text(
            encoding="utf-8"))["spec"]
    node_rules = yaml.safe_load(
        (ROOT / "infrastructure/monitoring/prometheusrules-node-health-alerts.yaml").read_text(
            encoding="utf-8"))["spec"]
    rules["groups"].extend(node_rules["groups"])
    tests = [
        case("sustained NVMe and SATA stalls respect hold time and node labels",
             inputs() + inputs("node-b:9100", "sda", "fixture-sata"),
             [("1m", []), ("2m", [
                 expected("node-a:9100", "nvme0n1", "fixture-nvme"),
                 expected("node-b:9100", "sda", "fixture-sata"),
             ])]),
        case("queued reads with fast writes do not page", inputs(write_time="0+15x10"), [("3m", [])]),
        case("isolated slow writes below workload threshold do not page",
             inputs(write_time="0+15x10", writes="0+30x10"), [("3m", [])]),
        case("slow writes without a queue do not page", inputs(queue="0+30x10"), [("3m", [])]),
        case("brief spike does not satisfy the hold",
             inputs(write_time="0 900 915 930 945 960 975 990 1005 1020 1035",
                    queue="0 900 915 930 945 960 975 990 1005 1020 1035"),
             [("1m30s", []), ("3m", [])]),
        case("zero completions and virtual devices do not page",
             inputs(write_time="0x10", writes="0x10") +
             inputs("node-b:9100", "dm-0", "fixture-mapper"),
             [("3m", [])]),
        case("Longhorn iSCSI disks and unknown disk inventory do not page",
             inputs(device="sdb", model="VIRTUAL-DISK") +
             inputs("node-b:9100", "sdc", "fixture-unknown", model=None) +
             inputs("node-c:9100", "sdd", "fixture-empty-model", model=""),
             [("3m", [])]),
        case("counter reset does not turn a brief spike into a sustained stall",
             inputs(write_time="0 900 0 15 30 45 60 75 90 105 120",
                    writes="0 3000 0 3000 6000 9000 12000 15000 18000 21000 24000",
                    queue="0 900 0 15 30 45 60 75 90 105 120"),
             [("1m30s", []), ("3m", [])]),
    ]
    tests.extend(reboot_dashboard_cases())
    tests.extend(node_observer_cases(node_rules))
    subprocess.run(["docker", "pull", IMAGE], check=True)
    with tempfile.TemporaryDirectory() as temporary:
        directory = Path(temporary)
        directory.chmod(0o755)
        for name, value in {
            "rules.yaml": rules,
            "tests.yaml": {"rule_files": ["rules.yaml"], "evaluation_interval": "30s", "tests": tests},
        }.items():
            path = directory / name
            path.write_text(yaml.safe_dump(value, sort_keys=False), encoding="utf-8")
            path.chmod(0o644)
        command = [
            "docker", "run", "--rm", "--network=none", "--read-only",
            "--cap-drop=ALL", "--security-opt=no-new-privileges", "--pids-limit=64",
            "--memory=256m", "--user=65534:65534",
            "--tmpfs", "/tmp:rw,noexec,nosuid,size=32m,mode=1777",
            "--mount", f"type=bind,source={directory},target=/rules,readonly",
            "--workdir", "/rules", "--entrypoint", "/bin/promtool", IMAGE,
        ]
        version = subprocess.run([*command, "--version"], check=True, capture_output=True, text=True)
        native_version = re.search(r"\bversion (\d+\.\d+\.\d+)\b", version.stdout + version.stderr)
        assert native_version and native_version.group(1) == "3.16.0", "Unexpected native promtool version"
        subprocess.run([*command, "check", "rules", "/rules/rules.yaml"], check=True)
        subprocess.run([*command, "test", "rules", "/rules/tests.yaml"], check=True)
    evidence = {"image": IMAGE, "native_promtool_version": native_version.group(1), "test_cases": len(tests),
                "alert_evaluations": sum(len(test.get("alert_rule_test", [])) for test in tests),
                "promql_evaluations": sum(len(test.get("promql_expr_test", [])) for test in tests),
                "skips": 0, "source": "Actual PrometheusRule spec and current Grafana queries; no copied expression",
                "production_changes": "Offline native evaluation only; no production mutation or automated remediation"}
    if args.evidence:
        args.evidence.write_text(json.dumps(evidence, indent=2) + "\n", encoding="utf-8")
    print(json.dumps(evidence, indent=2))


if __name__ == "__main__":
    main()
