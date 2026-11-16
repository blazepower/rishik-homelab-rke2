"""Exercise the actual pinned chart's monitor, API-load and artifact contracts."""
import argparse
import copy
import hashlib
import json
from pathlib import Path
import shlex
import subprocess
import tempfile
import unittest

import yaml

ROOT = Path(__file__).resolve().parents[2]
parser = argparse.ArgumentParser(add_help=False)
parser.add_argument("--chart", type=Path)
options, remaining = parser.parse_known_args()


class ActivationTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.release = yaml.safe_load(
            (ROOT / "infrastructure" / "monitoring" / "helmrelease-node-problem-detector.yaml").read_text()
        )
        cls.evidence = json.loads((ROOT / ".github" / "security" / "npd-owned-runtime.json").read_text())
        cls.source = json.loads((ROOT / "apps" / "_shared" / "npd-owned" / "source.json").read_text())
        cls.directory = tempfile.TemporaryDirectory(prefix="npd-chart-")
        cls.addClassCleanup(cls.directory.cleanup)
        cls.directory = Path(cls.directory.name)
        cls.chart = options.chart or cls.directory / "chart.tgz"
        if options.chart is None:
            subprocess.run(
                ["curl", "--fail", "--silent", "--show-error", "--location",
                 "--max-time", "90", "--output", str(cls.chart), cls.evidence["chart"]["url"]],
                check=True, timeout=100,
            )
        assert hashlib.sha256(cls.chart.read_bytes()).hexdigest() == cls.evidence["chart"]["sha256"]
        assert cls.release["spec"]["chart"]["spec"]["version"] == cls.evidence["chart"]["version"]
        assert cls.evidence["supported_api_minor"] == cls.source["supported_api_minor"]
        assert cls.evidence["compiler"] == cls.source["compiler"]

    def render(self, release):
        values = self.directory / "values.yaml"
        values.write_text(yaml.safe_dump(release["spec"]["values"]), encoding="utf-8")
        rendered = subprocess.check_output(
            ["helm", "template", "node-problem-detector", str(self.chart),
             "--namespace", "monitoring", "--kube-version", "1.35.10",
             "--values", str(values)], encoding="utf-8", timeout=60,
        )
        return next(x for x in yaml.safe_load_all(rendered) if x and x["kind"] == "DaemonSet")

    def verify(self, release):
        daemonset = self.render(release)
        pod = daemonset["spec"]["template"]["spec"]
        container, = pod["containers"]
        self.assertEqual(container["image"], self.evidence["image"])
        self.assertEqual(container["command"][:2], ["/bin/sh", "-c"])
        args = shlex.split(container["command"][2])
        self.assertEqual(args[:2], ["exec", "/node-problem-detector"])
        self.assertEqual(
            [x for x in args if x.startswith("--config.system-log-monitor=")],
            ["--config.system-log-monitor=/config/kernel-monitor.json,"
             "/config/systemd-monitor.json,/config/readonly-monitor.json"],
        )
        for flag in ("--kube-api-qps=5", "--kube-api-burst=10",
                     "--k8s-exporter-heartbeat-period=5m0s"):
            self.assertEqual(args.count(flag), 1)
            self.assertEqual(sum(x.startswith(flag.split("=")[0] + "=") for x in args), 1)
        self.assertEqual(container["resources"], {
            "requests": {"cpu": "20m", "memory": "64Mi"},
            "limits": {"cpu": "100m", "memory": "128Mi"},
        })
        self.assertFalse(pod["hostPID"])
        self.assertFalse(pod["hostNetwork"])
        self.assertEqual(container["securityContext"], {"privileged": True})
        self.assertEqual(daemonset["spec"]["updateStrategy"]["type"], "RollingUpdate")
        strategy = daemonset["spec"]["updateStrategy"]["rollingUpdate"]
        self.assertEqual(strategy["maxUnavailable"], 1)
        self.assertEqual(strategy.get("maxSurge", 0), 0)

    def test_actual_chart_preserves_monitor_coverage_api_load_and_immutable_artifact(self):
        self.verify(self.release)

    def test_rejects_lost_filesystem_coverage_or_an_unaccepted_image(self):
        for mutation in ("monitor", "image"):
            with self.subTest(mutation=mutation):
                release = copy.deepcopy(self.release)
                values = release["spec"]["values"]
                if mutation == "monitor":
                    values["settings"]["log_monitors"].remove("/config/readonly-monitor.json")
                else:
                    values["image"]["digest"] = "sha256:" + "0" * 64
                with self.assertRaises(AssertionError):
                    self.verify(release)

    def test_rejects_api_load_regression_and_chart_concatenation_trap(self):
        for arguments in ([], ["--kube-api-qps=500 --kube-api-burst=500"],
                          ["--kube-api-qps=5", "--kube-api-burst=10"]):
            with self.subTest(arguments=arguments):
                release = copy.deepcopy(self.release)
                release["spec"]["values"]["settings"]["extraArgs"] = arguments
                with self.assertRaises(AssertionError):
                    self.verify(release)


if __name__ == "__main__":
    unittest.main(argv=[__file__, *remaining])
