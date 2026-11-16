"""Cases that must not turn missing/stale/failed observations into healthy zeros."""
import datetime
import email.utils
import errno
import importlib.util
from pathlib import Path
import tempfile
from types import SimpleNamespace as Record
import unittest
from unittest.mock import patch

spec = importlib.util.spec_from_file_location("node_observer", Path(__file__).with_name("node-patch-observer.py"))
observer = importlib.util.module_from_spec(spec)
spec.loader.exec_module(observer)
NOW = 1794859200


class RebootAndKernel(unittest.TestCase):
    def test_only_absent_regular_flag_means_no_reboot(self):
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            with self.assertRaises(FileNotFoundError):
                observer.reboot(root)
            (root / "run").mkdir()
            self.assertEqual(observer.reboot(root)[0][1], 0)
            flag = root / "run/reboot-required"
            flag.write_text("reboot needed")
            self.assertEqual(observer.reboot(root)[0][1], 1)
            flag.unlink()
            flag.mkdir()
            with self.assertRaises(observer.ObservationError):
                observer.reboot(root)

    def test_permission_error_is_unknown_not_zero(self):
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            (root / "run").mkdir()
            with patch.object(Path, "lstat", side_effect=PermissionError(errno.EACCES, "denied")):
                text, errors = observer.collect("worker", NOW, {"reboot": lambda: observer.reboot(root)})
            self.assertTrue(errors)
            self.assertNotIn("homelab_node_reboot_required{", text)
            self.assertIn('homelab_node_observer_check_success{check="reboot",node="worker"} 0', text)

    def test_numeric_newest_installed_abi_ignores_removed_packages(self):
        packages = ("linux-image-7.0.0-9-generic:amd64\tinstalled\n"
                    "linux-image-7.0.0-43-generic\tinstalled\n"
                    "linux-image-7.0.0-99-generic\tconfig-files\n")
        values = observer.kernel_abi("7.0.0-9-generic", packages)
        self.assertEqual(values[0][1], 1)
        self.assertEqual(values[1][2]["newest_installed_release"], "7.0.0-43-generic")
        self.assertEqual(observer.kernel_abi("7.0.0-43-generic", packages)[0][1], 0)
        for running, data in (("7.0.0-38-generic", packages), ("custom-kernel", packages),
                              ("7.0.0-43-generic", "malformed output")):
            with self.subTest(running=running), self.assertRaises(observer.ObservationError):
                observer.kernel_abi(running, data)


class AptSecurity(unittest.TestCase):
    def setUp(self):
        self.temporary = tempfile.TemporaryDirectory()
        self.addCleanup(self.temporary.cleanup)
        self.root = Path(self.temporary.name)
        directory = self.root / "var/lib/apt/lists"
        directory.mkdir(parents=True)
        self.release = directory / "security.ubuntu.com_ubuntu_dists_resolute-security_InRelease"
        self.package_file = Record(
            origin="Ubuntu", archive="resolute-security",
            filename=str(directory / "security.ubuntu.com_ubuntu_dists_resolute-security_main_binary-amd64_Packages"))
        self.cache = Record(file_list=[self.package_file], packages=[])
        self.write_release(NOW - 3600)

    def write_release(self, epoch, extra=""):
        date = email.utils.format_datetime(datetime.datetime.fromtimestamp(epoch, datetime.timezone.utc))
        self.release.write_text("Suite: resolute-security\nDate: " + date + "\n" + extra)

    def version(self, number, security=True):
        origin = Record(origin="Ubuntu", archive="resolute-security" if security else "resolute-updates")
        return Record(ver_str=str(number), file_list=[(origin, 0)])

    def package(self, installed, candidate, versions):
        return Record(current_ver=self.version(installed), candidate=self.version(candidate),
                      version_list=versions)

    def observe(self, candidate=None):
        return observer.security_updates(
            self.root, NOW, self.cache, candidate or (lambda package: package.candidate),
            lambda left, right: (int(left) > int(right)) - (int(left) < int(right)),
        )

    def test_newer_updates_candidate_and_holds_do_not_hide_security_version(self):
        self.cache.packages = [
            self.package(1, 3, [self.version(3, False), self.version(2), self.version(1)]),
            self.package(1, 1, [self.version(2), self.version(1)]),
            self.package(2, 2, [self.version(2), self.version(1)]),
        ]
        values = {name: value for name, value, _ in self.observe()}
        self.assertEqual(values["homelab_node_apt_security_updates_available"], 2)
        self.assertEqual(values["homelab_node_apt_upgradable_packages"], 1)

    def test_fresh_release_not_individual_component_mtime_is_used(self):
        path = Path(self.package_file.filename)
        path.write_text("unchanged component index")
        import os
        os.utime(path, (0, 0))
        self.assertEqual(self.observe()[1][1], 0)
        self.assertEqual(self.observe()[2][1], NOW - 3600)

    def test_missing_stale_future_or_expired_security_metadata_never_means_zero(self):
        for epoch, extra in ((NOW - 86401, ""), (NOW + 1, ""),
                             (NOW - 3600, "Valid-Until: Sun, 15 Nov 2026 00:00:00 +0000\n"),
                             (NOW - 3600, "Date: Mon, 16 Nov 2026 00:00:00 +0000\n")):
            with self.subTest(epoch=epoch, extra=extra):
                self.write_release(epoch, extra)
                with self.assertRaises(observer.ObservationError):
                    self.observe()
        self.cache.file_list = []
        with self.assertRaises(observer.ObservationError):
            self.observe()

    def test_changing_metadata_during_cache_observation_is_rejected(self):
        self.cache.packages = [self.package(1, 2, [self.version(2)])]

        def candidate(package):
            self.write_release(NOW - 7200)
            return package.candidate

        with self.assertRaises(observer.ObservationError):
            self.observe(candidate)

    def test_index_and_installed_status_identity_is_bound_before_cache_loading(self):
        directory = self.root / "var/lib/dpkg"
        directory.mkdir()
        status = directory / "status"
        status.write_text("before")
        before = observer.apt_input_identity(self.root)
        status.write_text("after")
        self.assertNotEqual(before, observer.apt_input_identity(self.root))
        before = observer.apt_input_identity(self.root)
        Path(self.package_file.filename).write_text("new component")
        self.assertNotEqual(before, observer.apt_input_identity(self.root))


class Publication(unittest.TestCase):
    def test_failed_check_replaces_previous_healthy_file_but_keeps_independent_known_values(self):
        def failed():
            raise observer.ObservationError("Security input stale")

        with tempfile.TemporaryDirectory() as temporary:
            path = Path(temporary) / "node-maintenance.prom"
            path.write_text('homelab_node_apt_security_updates_available{node="worker"} 0\n')
            text, errors = observer.collect("worker", NOW, {
                "reboot": lambda: [("homelab_node_reboot_required", 1, {})], "security": failed,
            })
            observer.publish(path, text)
            self.assertEqual(path.read_text(), text)
            self.assertEqual(len(errors), 1)
            self.assertIn('homelab_node_reboot_required{node="worker"} 1', text)
            self.assertIn('homelab_node_observer_check_success{check="security",node="worker"} 0', text)
            self.assertNotIn("homelab_node_apt_security_updates_available{", text)
            self.assertEqual(list(path.parent.iterdir()), [path])


if __name__ == "__main__":
    unittest.main()
