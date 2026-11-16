"""Rebuild all three unchanged NPD programs, retaining journald and CGO."""
import hashlib
import json
from pathlib import Path
import shutil
import subprocess

from maintained_build import advisory_summary


def run(*args):
    return subprocess.check_output(args, encoding="utf-8").strip()


def main():
    settings = json.loads(Path("/build/source.json").read_text())
    assert run("go", "version").split()[2] == settings["compiler"]
    assert run("git", "rev-parse", "HEAD") == settings["revision"]
    assert run("git", "rev-parse", settings["version"] + "^{commit}") == settings["revision"]
    assert not run("git", "status", "--porcelain", "--untracked-files=no")
    assert {name: hashlib.sha256(Path(name).read_bytes()).hexdigest()
            for name in settings["module_sha256"]} == settings["module_sha256"]
    subprocess.run(["make", "bin/node-problem-detector", "bin/health-checker", "bin/log-counter",
                    "VERSION=" + settings["version"], "ENABLE_JOURNALD=1"], check=True)
    subprocess.run(["go", "test", "-mod=readonly", "-race", "-short", "-tags=journald",
                    "-timeout=1m", "./..."], check=True)
    proof = Path("/build/provenance")
    proof.mkdir()
    modules = {}
    settings["actual_binaries"] = {}
    for name, binary in settings["binaries"].items():
        path = Path("bin") / name
        info = run("go", "version", "-m", str(path))
        assert info.splitlines()[0].endswith(": " + settings["compiler"])
        assert "CGO_ENABLED=1" in info and "-tags=journald" in info
        subprocess.run(["go", "tool", "nm", str(path)], stdout=subprocess.DEVNULL, check=True)
        evidence = {"sha256": hashlib.sha256(path.read_bytes()).hexdigest(),
                    "buildinfo": info, "symbol_table_verified": True, "advisory_scans": {}}
        for line in info.splitlines():
            parts = line.split()
            if parts and parts[0] == "dep":
                assert parts[1] not in modules or modules[parts[1]] == parts[2]
                modules[parts[1]] = parts[2]
        for mode, target in (("source", binary["source"]), ("binary", str(path))):
            command = ["go", "run", "golang.org/x/vuln/cmd/govulncheck@" + settings["govulncheck"],
                       "-json", "-scan=symbol", "-mode=" + mode]
            if mode == "source":
                command.append("-tags=journald")
            text = run(*command, target)
            (proof / (name + "-" + mode + ".jsonl")).write_text(text)
            evidence["advisory_scans"][mode] = advisory_summary(text, mode, settings["govulncheck"])
        assert evidence["sha256"] == hashlib.sha256(path.read_bytes()).hexdigest()
        settings["actual_binaries"][name] = evidence
    assert not run("git", "status", "--porcelain", "--untracked-files=no")
    assert {name: hashlib.sha256(Path(name).read_bytes()).hexdigest()
            for name in settings["module_sha256"]} == settings["module_sha256"]
    shutil.copyfile(Path(run("go", "env", "GOROOT")) / "LICENSE", proof / "GO-LICENSE")
    shutil.copyfile("LICENSE", proof / "LICENSE")
    for name, version in modules.items():
        directory = Path(run("go", "list", "-m", "-f", "{{.Dir}}", name))
        licenses = [p for pattern in ("LICENSE*", "LICENCE*", "COPYING*", "NOTICE*")
                    for p in directory.glob(pattern) if p.is_file()]
        assert directory.is_dir() and licenses, (name, "Linked module license missing")
        destination = proof / "module-licenses" / (name + "@" + version)
        destination.mkdir(parents=True)
        for path in licenses:
            shutil.copyfile(path, destination / path.name)
    settings["linked_modules"] = modules
    (proof / "source.json").write_text(json.dumps(settings, indent=2))


if __name__ == "__main__":
    main()
