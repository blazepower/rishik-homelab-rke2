"""Prevent a desired policy-engine upgrade from passing tests on the old engine."""

import importlib.util
from pathlib import Path
import unittest

spec = importlib.util.spec_from_file_location(
    "image_policy", Path(__file__).with_name("test-kyverno-image-policy.py"),
)
module = importlib.util.module_from_spec(spec)
spec.loader.exec_module(module)


class EngineCompatibilityTests(unittest.TestCase):
    def test_accepts_the_actual_desired_engine(self):
        module.require_matching_engine(
            "3.9.2", {"name": "kyverno", "version": "3.9.2", "appVersion": "v1.19.2"}, "1.19.2",
        )

    def test_rejects_upgrades_tested_on_the_old_engine(self):
        for chart, app in (("3.9.3", "1.19.3"), ("3.10.0", "1.20.0")):
            with self.subTest(chart=chart), self.assertRaisesRegex(RuntimeError, "native tests use"):
                module.require_matching_engine(
                    chart, {"name": "kyverno", "version": chart, "appVersion": "v" + app}, "1.19.2",
                )

    def test_rejects_unexpected_chart_metadata(self):
        for name, version in (("different-chart", "3.9.2"), ("kyverno", "3.9.1")):
            with self.subTest(name=name, version=version), self.assertRaisesRegex(RuntimeError, "metadata"):
                module.require_matching_engine(
                    "3.9.2", {"name": name, "version": version, "appVersion": "v1.19.2"}, "1.19.2",
                )


if __name__ == "__main__":
    unittest.main()
