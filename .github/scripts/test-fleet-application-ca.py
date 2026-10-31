"""Guard the bounded application audit against false runtime or rollout clearance."""
from collections import Counter
import json
from pathlib import Path
import re
import unittest

ROOT = Path(__file__).resolve().parents[2]
REPORT = json.loads((ROOT / ".github/security/fleet-application-ca-report.json").read_text())
POLICY = json.loads((ROOT / ".github/security/emudhra-2026.json").read_text())


def canonical(image):
    return image.removeprefix("docker-pullable://").removeprefix(
        "index.docker.io/"
    ).removeprefix("docker.io/")


class FleetApplicationCaTests(unittest.TestCase):
    def test_report_uses_the_complete_canonical_policy(self):
        expected = {entry["sha256"].lower() for entry in POLICY["blocked_spkis"]}
        self.assertEqual(len(expected), 11)
        self.assertEqual(set(REPORT["policy_spkis"]), expected)

    def test_every_export_matches_its_resolved_running_platform(self):
        seen = set()
        for image in REPORT["images"]:
            with self.subTest(image=image["running_imageID"]):
                self.assertRegex(image["running_imageID"], r"^[^@\s]+@sha256:[0-9a-f]{64}$")
                self.assertRegex(image["immutable_amd64"], r"^[^@\s]+@sha256:[0-9a-f]{64}$")
                self.assertEqual(canonical(image["immutable_amd64"]),
                                 canonical(image["audit"]["immutable_amd64"]))
                self.assertNotIn(canonical(image["immutable_amd64"]), seen)
                seen.add(canonical(image["immutable_amd64"]))
                self.assertIn(image["source"], {
                    "exact historical amd64 match", "fresh read-only immutable export",
                })
                self.assertTrue(image["live_refs"])
                self.assertGreater(image["audit"]["inspected_pem_paths"], 0)
                self.assertTrue(image["audit"]["system_bundle_paths"])
                self.assertFalse(image["audit"]["uninspected_stores"])
                self.assertEqual(bool(image["audit"]["denied"]),
                                 image["audit"]["status"] == "DENIED")
                for match in image["audit"]["denied"]:
                    self.assertIn(match["spki"], REPORT["policy_spkis"])

    def test_summary_does_not_upgrade_pem_evidence_to_runtime_clearance(self):
        observed = dict(Counter(image["audit"]["status"] for image in REPORT["images"]))
        self.assertEqual(REPORT["summary"]["pem_status_counts"], observed)
        self.assertEqual(REPORT["summary"]["unique_running_images"], len(REPORT["images"]))
        self.assertEqual(observed, {"DENIED": 10, "PEM_STORES_CLEAR": 7})
        self.assertEqual(REPORT["summary"]["fully_certified_applications"], 0)
        self.assertFalse(REPORT["runtime_floors"]["version_metadata_is_trust_proof"])
        self.assertEqual(REPORT["status"], "BLOCKED")
        self.assertTrue(REPORT["blockers"])

    def test_node_default_and_system_evidence_are_distinguished(self):
        for app, expected in (("kaneo", 118), ("overseerr", 116)):
            row = next(row for row in REPORT["runtime"]["results"]
                       if row["pod"].startswith(app + "-") and "node" in row)
            node = row["node"]
            self.assertEqual(node["default"]["certificates"], expected)
            self.assertFalse(node["default"]["denied"])
            self.assertFalse(node["bundled"]["denied"])
            self.assertFalse(node["extra_ca_configured"])
            self.assertFalse(node["node_options_present"])
            self.assertTrue(node["existing_node_processes"])
            self.assertTrue(all(not process["trust_flags"]
                                for process in node["existing_node_processes"]))
            self.assertEqual(bool(node["system"]["denied"]), app == "overseerr")

    def test_calibre_certifi_is_not_cleared_by_the_clean_os_bundle(self):
        row = next(row for row in REPORT["runtime"]["results"]
                   if row["pod"].startswith("calibre-web-"))
        self.assertFalse(row["public_system_bundle"]["denied"])
        self.assertFalse(row["python"]["ssl_default_certs"]["denied"])
        self.assertTrue(row["python"]["certifi_bundle"]["denied"])
        self.assertEqual(row["python"]["certifi_version"], "2026.5.20")
        self.assertTrue(REPORT["additional_native_probes"]["calibre_requests"]
                        ["default_ca_is_packaged_certifi"])

    def test_ruby_native_default_trust_is_explicitly_blocked(self):
        roots = REPORT["additional_native_probes"]["sure_ruby"]["roots"]
        self.assertEqual(len(roots), 2)
        self.assertTrue(all(root["trusted"] for root in roots))
        self.assertTrue(all(root["spki_sha256"] in REPORT["policy_spkis"] for root in roots))

    def test_candidate_index_aliases_are_not_claimed_as_new_rebuilds(self):
        same_running = [row for row in REPORT["compatible_vendor_candidates"]
                        if row["same_running_platform"]]
        self.assertEqual(len(same_running), 2)
        self.assertTrue(all(row["status"] == "DENIED" for row in same_running))
        self.assertTrue(all(row["decision"] == "REJECTED_DENIED_CA_NO_GITOPS_CHANGE"
                            for row in REPORT["compatible_vendor_candidates"]))
        self.assertEqual(REPORT["summary"]["gitops_manifest_changes"], 0)

    def test_go_metadata_and_nonrunning_java_are_not_runtime_clearance(self):
        self.assertEqual(len(REPORT["native_build_metadata"]), 5)
        for row in REPORT["native_build_metadata"]:
            self.assertTrue(row["binaries"])
            for binary in row["binaries"]:
                self.assertRegex(binary["go_build_version"], r"^go1\.\d+\.\d+$")
            self.assertIn("not runtime trust certification", row["coverage"])
        java = REPORT["paperless_and_java"]
        self.assertEqual(java["paperless_desired_replicas"], 0)
        self.assertEqual(java["runtime_status"], "NOT_PROBED_SCALED_TO_ZERO_NO_CLEARANCE")
        self.assertFalse(java["tika_or_other_java_application_present_in_scoped_running_inventory"])

    def test_runtime_evidence_is_bound_to_observed_images(self):
        self.assertTrue(REPORT["inventory_recheck"]["all_scoped_refs_unchanged"])
        self.assertFalse(REPORT["inventory_recheck"]["running_init_containers"])
        self.assertEqual(REPORT["inventory_recheck"]["container_references"],
                         sum(len(image["live_refs"]) for image in REPORT["images"]))
        known = {canonical(image["running_imageID"]): image for image in REPORT["images"]}
        for row in REPORT["runtime"]["results"]:
            image = known[canonical(row["imageID"])]
            self.assertTrue(any(
                ref["namespace"] == row["namespace"] and ref["pod"] == row["pod"]
                and ref["container"] == row["container"] for ref in image["live_refs"]
            ))

    def test_storage_failure_and_read_only_boundary_are_preserved(self):
        self.assertEqual(REPORT["safety"]["cluster_operations"], "READ_ONLY")
        self.assertFalse(REPORT["safety"]["rollout_clearance"])
        self.assertEqual(REPORT["summary"]["cluster_changes"], 0)
        self.assertEqual(REPORT["safety"]["deployments_or_publications"], [])
        self.assertEqual(len(REPORT["safety"]["actual_nodes"]), 3)
        self.assertTrue(REPORT["safety"]["controller_cordoned"])

    def test_no_certificate_bodies_or_full_runtime_options_are_persisted(self):
        text = json.dumps(REPORT)
        self.assertNotIn("-----BEGIN CERTIFICATE-----", text)
        for row in REPORT["runtime"]["results"]:
            if "node" in row:
                self.assertNotIn("NODE_OPTIONS", row["node"])
                self.assertNotIn("env", row["node"])
        self.assertFalse(re.search(r"-----BEGIN (?:RSA |EC )?PRIVATE KEY-----", text))


if __name__ == "__main__":
    unittest.main()
