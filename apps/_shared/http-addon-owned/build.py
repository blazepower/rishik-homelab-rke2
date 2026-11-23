"""Build unchanged stable application source with one declared module overlay."""
import hashlib
import importlib.util
import json
from pathlib import Path
import re
import shutil
import subprocess

spec = importlib.util.spec_from_file_location("maintained_build", "/build/maintained_build.py")
shared = importlib.util.module_from_spec(spec)
spec.loader.exec_module(shared)


def run(*args):
    return subprocess.check_output(args, text=True)


def main():
    lock = json.loads(Path("/build/source.json").read_text())
    assert run("go", "version").split()[2] == lock["compiler"]
    assert run("git", "rev-parse", "HEAD").strip() == lock["revision"]
    assert not run("git", "status", "--porcelain", "--untracked-files=no")
    patch = Path("/build/security.patch")
    assert hashlib.sha256(patch.read_bytes()).hexdigest() == lock["patch_sha256"]
    changed = {line.split("\t")[2] for line in run("git", "apply", "--numstat", str(patch)).splitlines()}
    assert changed == {"go.mod", "go.sum"}, "Only declared module files may change"
    subprocess.run(["git", "apply", "--check", str(patch)], check=True)
    subprocess.run(["git", "apply", str(patch)], check=True)
    hashes = {name: hashlib.sha256(Path(name).read_bytes()).hexdigest() for name in lock["module_sha256"]}
    assert hashes == lock["module_sha256"]
    subprocess.run(["go", "test", "-p=2", "-mod=readonly", "-short", "-timeout=90s",
                    "./operator/...", "./scaler/...", "./interceptor/...", "./pkg/..."], check=True)
    output = Path("/build/bin")
    output.mkdir()
    proof = Path("/build/provenance")
    proof.mkdir()
    actual = {}
    modules = set()
    flags = (f"-X github.com/kedacore/http-add-on/pkg/build.version={lock['version']} "
             f"-X github.com/kedacore/http-add-on/pkg/build.gitCommit={lock['revision']}")
    for name in lock["components"]:
        binary = output / name
        subprocess.run(["go", "build", "-p=2", "-mod=readonly", "-trimpath",
                        "-ldflags=" + flags, "-o", str(binary), "./" + name], check=True)
        info = run("go", "version", "-m", str(binary))
        assert info.splitlines()[0] == str(binary) + ": " + lock["compiler"]
        assert "CGO_ENABLED=0" in info and "GOOS=linux" in info and "GOARCH=amd64" in info
        symbols = run("readelf", "--sections", "--wide", str(binary))
        assert all(section in symbols for section in (".symtab", ".strtab", ".gopclntab"))
        for module, version in lock["required_modules"].items():
            assert re.search(r"^\s+dep\s+" + re.escape(module) + r"\s+" + re.escape(version) + r"\s",
                             info, re.MULTILINE), (name, module, version)
        modules.update(line.split()[1] for line in info.splitlines() if line.strip().startswith("dep\t"))
        actual[name] = {"sha256": hashlib.sha256(binary.read_bytes()).hexdigest(),
                        "buildinfo": info, "symbol_table_verified": True}
    scans = {}
    for name, mode, targets in [
        ("all-source", "source", ["./" + name for name in lock["components"]]),
        *((name + "-binary", "binary", [str(output / name)]) for name in lock["components"]),
    ]:
        text = run("go", "run", "golang.org/x/vuln/cmd/govulncheck@" + lock["govulncheck"],
                   "-json", "-scan=symbol", "-mode=" + mode, *targets)
        (proof / (name + ".jsonl")).write_text(text)
        scans[name] = shared.advisory_summary(text, mode, lock["govulncheck"])
    licenses = proof / "licenses"
    licenses.mkdir()
    for module in sorted(modules):
        location = run("go", "list", "-m", "-f", "{{.Dir}}", module).strip()
        assert location, (module, "Missing module source directory")
        directory = Path(location)
        paths = [p for p in directory.iterdir()
                 if p.is_file() and p.name.upper().startswith(("LICENSE", "NOTICE", "COPYING", "PATENTS"))]
        assert paths, (module, "Missing module license")
        destination = licenses / module
        destination.mkdir(parents=True)
        for path in paths:
            shutil.copyfile(path, destination / path.name)
    shutil.copyfile(Path(run("go", "env", "GOROOT").strip()) / "LICENSE", licenses / "Go-LICENSE")
    shutil.copyfile("LICENSE", licenses / "HTTP-add-on-LICENSE")
    shutil.copyfile(patch, proof / "security.patch")
    assert hashes == {name: hashlib.sha256(Path(name).read_bytes()).hexdigest() for name in hashes}
    assert set(run("git", "diff", "--name-only").splitlines()) == {"go.mod", "go.sum"}
    (proof / "source.json").write_text(json.dumps({
        **lock, "actual_binaries": actual, "advisory_scans": scans,
    }, indent=2))


if __name__ == "__main__":
    main()
