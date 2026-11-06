#!/usr/bin/env python3
"""Exercise the actual storage rule with the production-version native promtool."""

import argparse
import json
from pathlib import Path
import subprocess
import tempfile

import yaml

ROOT = Path(__file__).resolve().parents[2]
IMAGE = "quay.io/prometheus/prometheus@sha256:451a845bae1d51622b41af1780eb2ad911bbb79a9855bc8e9bb67fabbc03f934"
ALERT = "NodePhysicalDiskWriteLatencyHigh"


def inputs(instance="node-a:9100", device="nvme0n1", nodename="fixture-nvme",
           write_time="0+900x10", writes="0+3000x10", queue="0+900x10"):
    labels = f'job="node-exporter",instance="{instance}",device="{device}"'
    return [
        {"series": f"node_disk_write_time_seconds_total{{{labels}}}", "values": write_time},
        {"series": f"node_disk_writes_completed_total{{{labels}}}", "values": writes},
        {"series": f"node_disk_io_time_weighted_seconds_total{{{labels}}}", "values": queue},
        {"series": f'node_uname_info{{job="node-exporter",instance="{instance}",nodename="{nodename}"}}',
         "values": "1x10"},
    ]


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


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--docker", action="store_true", required=True)
    parser.add_argument("--evidence", type=Path)
    args = parser.parse_args()
    rules = yaml.safe_load(
        (ROOT / "infrastructure" / "monitoring" / "prometheusrules-storage-health-alerts.yaml").read_text(
            encoding="utf-8"))["spec"]
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
        case("counter reset does not turn a brief spike into a sustained stall",
             inputs(write_time="0 900 0 15 30 45 60 75 90 105 120",
                    writes="0 3000 0 3000 6000 9000 12000 15000 18000 21000 24000",
                    queue="0 900 0 15 30 45 60 75 90 105 120"),
             [("1m30s", []), ("3m", [])]),
    ]
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
        subprocess.run([*command, "check", "rules", "/rules/rules.yaml"], check=True)
        subprocess.run([*command, "test", "rules", "/rules/tests.yaml"], check=True)
    evidence = {"image": IMAGE, "native_promtool_version": "3.15.1", "test_cases": len(tests),
                "alert_evaluations": sum(len(test["alert_rule_test"]) for test in tests),
                "skips": 0, "source": "Actual PrometheusRule spec; no copied expression",
                "production_changes": "Alert evaluation only; no automated remediation"}
    if args.evidence:
        args.evidence.write_text(json.dumps(evidence, indent=2) + "\n", encoding="utf-8")
    print(json.dumps(evidence, indent=2))


if __name__ == "__main__":
    main()
