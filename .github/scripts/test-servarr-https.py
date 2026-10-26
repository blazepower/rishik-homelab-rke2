"""Protect HTTPS login redirects behind the cluster's TLS-terminating proxies."""

from ipaddress import ip_network
from pathlib import Path
import unittest

import yaml


ROOT = Path(__file__).resolve().parents[2]


def load(path):
    return yaml.safe_load((ROOT / path).read_text())


class ServarrHttpsTests(unittest.TestCase):
    def test_trusted_forwarders_are_limited_to_cluster_pod_addresses(self):
        for app in ("sonarr", "radarr"):
            with self.subTest(app=app):
                release = load("apps/" + app + "/helmrelease.yaml")
                controller = release["spec"]["values"]["controllers"][app]
                env = controller["containers"]["app"]["env"]
                name = app.upper() + "__SERVER__TRUSTEDNETWORKS"
                entries = [item for item in env if item["name"] == name]
                self.assertEqual(len(entries), 1)
                networks = [ip_network(value) for value in entries[0]["value"].split(",")]
                self.assertEqual(networks, [ip_network("10.42.0.0/16")])
                self.assertNotIn("ASPNETCORE_FORWARDEDHEADERS_ENABLED",
                                 [item["name"] for item in env])

    def test_restarts_keep_the_existing_config_volume_single_writer(self):
        for app in ("sonarr", "radarr"):
            with self.subTest(app=app):
                values = load("apps/" + app + "/helmrelease.yaml")["spec"]["values"]
                self.assertEqual(values["controllers"][app]["strategy"], "Recreate")
                self.assertEqual(values["persistence"]["config"]["existingClaim"],
                                 app + "-config")

    def test_ingresses_continue_to_require_tls(self):
        for app in ("sonarr", "radarr"):
            with self.subTest(app=app):
                ingress = load("apps/" + app + "/ingress.yaml")
                self.assertEqual(ingress["metadata"]["annotations"][
                    "traefik.ingress.kubernetes.io/router.tls"], "true")
                self.assertEqual(ingress["spec"]["tls"][0]["hosts"],
                                 [app + ".homelab"])

    def test_homepage_links_start_on_https_for_both_access_modes(self):
        for path, suffix in (
            ("charts/homepage/values.yaml", ".homelab"),
            ("apps/homepage/helmrelease-tailscale.yaml", ".tail4217c.ts.net"),
        ):
            document = load(path)
            config = document.get("spec", {}).get("values", document)["config"]
            links = {
                name.lower(): service["href"]
                for group in config["services"]
                for entries in group.values()
                for entry in entries
                for name, service in entry.items()
            }
            for app in ("sonarr", "radarr"):
                with self.subTest(path=path, app=app):
                    self.assertEqual(links[app], "https://" + app + suffix)


if __name__ == "__main__":
    unittest.main()
