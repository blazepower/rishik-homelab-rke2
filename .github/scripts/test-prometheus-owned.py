"""Focused failures for the new dependency/filesystem/archive acceptance gates."""
import copy
import importlib.util
import io
from pathlib import Path
import tarfile
import tempfile
import unittest

spec = importlib.util.spec_from_file_location(
    "prometheus_owned", Path(__file__).with_name("verify-prometheus-owned.py"))
module = importlib.util.module_from_spec(spec)
spec.loader.exec_module(module)


class Dependencies(unittest.TestCase):
    def setUp(self):
        self.lock = {"modules": {"example.org/security": {"from": "v1.0.0", "to": "v1.0.1"}}}
        self.before = {"example.org/security": ("v1.0.0", "h1:before"), "example.org/stable": ("v2.0.0", "h1:same")}
        self.after = {**self.before, "example.org/security": ("v1.0.1", "h1:after")}

    def test_exact_security_delta_retains_other_modules(self):
        self.assertEqual(list(module.dependency_delta(self.before, self.after, self.lock)), ["example.org/security"])

    def test_other_version_or_checksum_changes_fail(self):
        for value in (("v2.0.1", "h1:same"), ("v2.0.0", "h1:different")):
            with self.subTest(value=value), self.assertRaises(AssertionError):
                module.dependency_delta(self.before, {**self.after, "example.org/stable": value}, self.lock)

    def test_absent_update_or_added_module_fails(self):
        for value in (self.before, {**self.after, "example.org/extra": ("v1.0.0", "h1:extra")}):
            with self.subTest(value=value), self.assertRaises(AssertionError):
                module.dependency_delta(self.before, value, self.lock)

    def test_linked_replacements_or_duplicates_fail(self):
        line = "\tdep\texample.org/security\tv1.0.1\th1:after\n"
        self.assertEqual(module.modules(line), {"example.org/security": ("v1.0.1", "h1:after")})
        for value in (line + line, line + "\t=> example.org/renamed v1.0.1 h1:after\n"):
            with self.subTest(value=value), self.assertRaises(AssertionError):
                module.modules(value)


class Filesystem(unittest.TestCase):
    def setUp(self):
        self.before = {
            "usr/bin/prometheus": ("0", 0o755, 0, 0, "", "original-prometheus"),
            "usr/bin/promtool": ("0", 0o755, 0, 0, "", "original-promtool"),
            "etc/ssl/certs/ca-certificates.crt": ("0", 0o644, 0, 0, "", "stock-ca"),
        }
        self.after = copy.deepcopy(self.before)
        for name in ("prometheus", "promtool"):
            path = "usr/bin/" + name
            self.after[path] = (*self.before[path][:-1], "patched-" + name)
        self.after["usr/share/prometheus-owned/source.json"] = ("0", 0o644, 0, 0, "", "public-provenance")

    def test_only_two_programs_and_provenance_may_change(self):
        self.assertEqual(module.compare_filesystems(self.before, self.after)["unchanged_factory_paths"], 1)

    def test_ca_or_binary_owner_changes_fail(self):
        for path, value in (
            ("etc/ssl/certs/ca-certificates.crt", ("0", 0o644, 0, 0, "", "other-ca")),
            ("usr/bin/prometheus", ("0", 0o755, 65532, 0, "", "patched-prometheus")),
        ):
            with self.subTest(path=path), self.assertRaises(AssertionError):
                module.compare_filesystems(self.before, {**self.after, path: value})

    def test_removed_factory_or_added_runtime_file_fails(self):
        missing = {name: value for name, value in self.after.items() if not name.startswith("etc/ssl")}
        added = {**self.after, "etc/new-root.crt": ("0", 0o644, 0, 0, "", "new-root")}
        for value in (missing, added):
            with self.subTest(value=value), self.assertRaises(AssertionError):
                module.compare_filesystems(self.before, value)


class UiArchive(unittest.TestCase):
    def archive(self, root, entries):
        archive = root / "ui.tar.gz"
        with tarfile.open(archive, "w:gz") as output:
            for name, kind in entries:
                member = tarfile.TarInfo(name)
                member.type = kind
                member.size = 1 if kind == tarfile.REGTYPE else 0
                if kind == tarfile.SYMTYPE:
                    member.linkname = "/outside"
                output.addfile(member, io.BytesIO(b"x") if member.isfile() else None)
        return archive

    def entries(self):
        return [("static/mantine-ui/index.html", tarfile.REGTYPE), ("static/react-app/index.html", tarfile.REGTYPE)]

    def test_valid_official_layout_is_hashed(self):
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            self.assertEqual(set(module.unpack_ui(self.archive(root, self.entries()), root / "ui")),
                             {name for name, _ in self.entries()})

    def test_traversal_links_and_duplicate_files_fail(self):
        for entry in (
            ("static/../../outside", tarfile.REGTYPE),
            ("/static/absolute", tarfile.REGTYPE),
            ("static/link", tarfile.SYMTYPE),
            ("static/mantine-ui/index.html", tarfile.REGTYPE),
        ):
            with self.subTest(entry=entry), tempfile.TemporaryDirectory() as temporary:
                root = Path(temporary)
                with self.assertRaises(AssertionError):
                    module.unpack_ui(self.archive(root, self.entries() + [entry]), root / "ui")

    def test_factory_ui_collision_fails(self):
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            path = root / "ui" / "static" / "mantine-ui" / "index.html"
            path.parent.mkdir(parents=True)
            path.write_bytes(b"unexpected factory asset")
            with self.assertRaises(AssertionError):
                module.unpack_ui(self.archive(root, self.entries()), root / "ui")


if __name__ == "__main__":
    unittest.main()
