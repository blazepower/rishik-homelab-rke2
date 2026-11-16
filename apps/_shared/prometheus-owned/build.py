"""Build both real programs without changing application source or stock UI."""
import hashlib
import json
from pathlib import Path
import shutil
import subprocess

from maintained_build import advisory_summary


def run(*args):
    return subprocess.check_output(args, encoding="utf-8").strip()


def hashes(names):
    return {name: hashlib.sha256(Path(name).read_bytes()).hexdigest() for name in names}


def main():
    lock = json.loads(Path("/build/source.json").read_text())
    assert run("go", "version").split()[2] == lock["compiler"]
    assert run("git", "rev-parse", "HEAD") == lock["revision"]
    assert run("git", "rev-parse", lock["version"] + "^{commit}") == lock["revision"]
    assert not run("git", "status", "--porcelain", "--untracked-files=no")
    assert hashes(lock["original_module_sha256"]) == lock["original_module_sha256"]
    patch = Path("/build/security.patch")
    assert hashlib.sha256(patch.read_bytes()).hexdigest() == lock["patch_sha256"]
    assert {line.split("\t")[2] for line in run("git", "apply", "--numstat", str(patch)).splitlines()} == {
        "go.mod", "go.sum"}
    subprocess.run(["git", "apply", "--check", str(patch)], check=True)
    subprocess.run(["git", "apply", str(patch)], check=True)
    assert hashes(lock["module_sha256"]) == lock["module_sha256"]
    for name, versions in lock["modules"].items():
        assert run("go", "list", "-mod=readonly", "-m", "-f", "{{.Version}}", name) == versions["to"]
    ui = json.loads(Path("/build/ui-files.json").read_text())
    assert ui and hashes("web/ui/" + name for name in ui) == {
        "web/ui/" + name: checksum for name, checksum in ui.items()}
    assert {path.relative_to("web/ui").as_posix()
            for path in Path("web/ui/static").rglob("*") if path.is_file()} == ui.keys()
    subprocess.run(["bash", "scripts/compress_assets.sh"], check=True)
    subprocess.run(["go", "list", "-deps", "-test", "-mod=readonly", "-tags=netgo,builtinassets",
                    "./web/...", "./cmd/prometheus", "./cmd/promtool"],
                   stdout=subprocess.DEVNULL, check=True)
    assert hashes(lock["module_sha256"]) == lock["module_sha256"], run(
        "git", "diff", "--", "go.mod", "go.sum")
    output = Path("/build/bin")
    proof = Path("/build/provenance")
    output.mkdir()
    proof.mkdir()
    flags = " ".join("-X github.com/prometheus/common/version." + name + "=" + value for name, value in {
        "Version": lock["version"].removeprefix("v"), "Revision": lock["revision"],
        "Branch": "homelab-security-overlay", "BuildUser": "homelab",
        "BuildDate": lock["build_date"],
    }.items())
    lock["actual_binaries"] = {}
    modules = {}
    for name in ("prometheus", "promtool"):
        binary = output / name
        subprocess.run(["go", "build", "-p=2", "-mod=readonly", "-trimpath",
                        "-tags=netgo,builtinassets", "-ldflags=" + flags,
                        "-o", str(binary), "./cmd/" + name], check=True)
        info = run("go", "version", "-m", str(binary))
        assert info.splitlines()[0] == str(binary) + ": " + lock["compiler"]
        assert all(value in info for value in ("CGO_ENABLED=0", "GOOS=linux", "GOARCH=amd64",
                                              "-tags=netgo,builtinassets"))
        subprocess.run(["go", "tool", "nm", str(binary)], stdout=subprocess.DEVNULL, check=True)
        evidence = {"sha256": hashlib.sha256(binary.read_bytes()).hexdigest(),
                    "buildinfo": info, "symbol_table_verified": True, "advisory_scans": {}}
        for line in info.splitlines():
            fields = line.split()
            if fields and fields[0] == "dep":
                assert fields[1] not in modules or modules[fields[1]] == fields[2]
                modules[fields[1]] = fields[2]
        for mode, target in (("source", "./cmd/" + name), ("binary", str(binary))):
            command = ["go", "run", "golang.org/x/vuln/cmd/govulncheck@" + lock["govulncheck"],
                       "-json", "-scan=symbol", "-mode=" + mode]
            if mode == "source":
                command.append("-tags=netgo,builtinassets")
            scan = subprocess.run([*command, target], stdout=subprocess.PIPE,
                                  stderr=subprocess.PIPE, text=True)
            text = scan.stdout
            (proof / (name + "-" + mode + ".jsonl")).write_text(text)
            assert scan.returncode in (0, 3), (name, mode, scan.returncode, scan.stderr[-12000:])
            evidence["advisory_scans"][mode] = advisory_summary(text, mode, lock["govulncheck"])
            assert scan.returncode == 0, (name, mode, "Advisory scanner rejected artifact", scan.stderr[-12000:])
        assert evidence["sha256"] == hashlib.sha256(binary.read_bytes()).hexdigest()
        lock["actual_binaries"][name] = evidence
    assert hashes(lock["module_sha256"]) == lock["module_sha256"], run(
        "git", "diff", "--", "go.mod", "go.sum")
    assert set(run("git", "diff", "--name-only").splitlines()) == {"go.mod", "go.sum"}
    for name, version in modules.items():
        directory = Path(run("go", "list", "-m", "-f", "{{.Dir}}", name))
        licenses = [p for pattern in ("LICENSE*", "LICENCE*", "COPYING*", "NOTICE*")
                    for p in directory.glob(pattern) if p.is_file()]
        assert directory.is_dir() and licenses, (name, "Linked module license missing")
        destination = proof / "module-licenses" / (name + "@" + version)
        destination.mkdir(parents=True)
        for path in licenses:
            shutil.copyfile(path, destination / path.name)
    for name in ("LICENSE", "NOTICE", "go.mod", "go.sum"):
        shutil.copyfile(name, proof / name)
    shutil.copyfile(Path(run("go", "env", "GOROOT")) / "LICENSE", proof / "GO-LICENSE")
    shutil.copyfile(patch, proof / "security.patch")
    lock["ui_files"] = ui
    lock["linked_modules"] = modules
    (proof / "source.json").write_text(json.dumps(lock, indent=2))


if __name__ == "__main__":
    main()
