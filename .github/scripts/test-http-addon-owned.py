"""Focused rejection tests for the three-image native acceptance boundary."""
import importlib.util
from pathlib import Path
import unittest

spec = importlib.util.spec_from_file_location("http_owned", Path(__file__).with_name("verify-http-addon-owned.py"))
module = importlib.util.module_from_spec(spec)
spec.loader.exec_module(module)


class BinaryIdentityTest(unittest.TestCase):
    def test_requires_actual_compiler_and_dependency_versions(self):
        lock = {"compiler": "go1.27.4", "required_modules": {
            "github.com/kedacore/keda/v2": "v2.21.1"}}
        info = ("/proof/application: go1.27.4\n\tbuild\tCGO_ENABLED=0\n"
                "\tbuild\tGOOS=linux\n\tbuild\tGOARCH=amd64\n"
                "\tdep\tgithub.com/kedacore/keda/v2\tv2.21.1\th1:fixture\n")
        module.validate_buildinfo(info, lock)
        for candidate in (info.replace("go1.27.4", "go1.27.1"), info.replace("v2.21.1", "v2.18.3"),
                          info.replace("CGO_ENABLED=0", "CGO_ENABLED=1")):
            with self.subTest(candidate=candidate), self.assertRaises(AssertionError):
                module.validate_buildinfo(candidate, lock)


class FactoryFilesystemTest(unittest.TestCase):
    def fixture(self):
        before = {"ko-app/interceptor": ("0", 0o555, 0, 0, "", "factory-elf"),
                  "etc/ssl/certs/ca-certificates.crt": ("0", 0o444, 0, 0, "", "original-ca")}
        after = {**before, "ko-app/interceptor": ("0", 0o555, 0, 0, "", "patched-elf"),
                 "usr/share/http-addon-owned/source.json": ("0", 0o644, 0, 0, "", "proof")}
        return before, after

    def test_accepts_only_declared_elf_replacement_and_provenance(self):
        before, after = self.fixture()
        module.compare_filesystems(before, after, "interceptor", "factory-elf")

    def test_rejects_changed_or_removed_factory_files_and_new_runtime_paths(self):
        before, after = self.fixture()
        changed_ca = {**after, "etc/ssl/certs/ca-certificates.crt": ("0", 0o444, 0, 0, "", "other-ca")}
        removed_ca = {k: v for k, v in after.items() if not k.startswith("etc/")}
        extra_runtime = {**after, "etc/new-config": ("0", 0o644, 0, 0, "", "extra")}
        for candidate in (changed_ca, removed_ca, extra_runtime):
            with self.subTest(candidate=candidate), self.assertRaises(AssertionError):
                module.compare_filesystems(before, candidate, "interceptor", "factory-elf")

    def test_rejects_wrong_factory_binary_and_changed_executable_permissions(self):
        before, after = self.fixture()
        with self.assertRaises(AssertionError):
            module.compare_filesystems(before, after, "interceptor", "different-factory")
        after["ko-app/interceptor"] = ("0", 0o755, 0, 0, "", "patched-elf")
        with self.assertRaises(AssertionError):
            module.compare_filesystems(before, after, "interceptor", "factory-elf")


if __name__ == "__main__":
    unittest.main()
