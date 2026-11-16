"""Compile locked public source with only explicitly declared security changes."""
import hashlib
import json
import os
from pathlib import Path
import shutil
import subprocess


def run(*args, **kwargs):
    return subprocess.check_output(args, text=True, **kwargs).strip()


def advisory_summary(text, mode, version):
    decoder = json.JSONDecoder()
    position = 0
    messages = []
    while position < len(text):
        while position < len(text) and text[position].isspace():
            position += 1
        if position == len(text):
            break
        message, position = decoder.raw_decode(text, position)
        messages.append(message)
    config, = [m["config"] for m in messages if "config" in m]
    assert config["scanner_name"] == "govulncheck" and config["scanner_version"] == version
    assert config["scan_mode"] == mode and config["scan_level"] == "symbol"
    assert config["db"] == "https://vuln.go.dev" and config["db_last_modified"]
    findings = [m["finding"] for m in messages if "finding" in m]
    symbols = [f for f in findings if any(t.get("function") for t in f.get("trace", []))]
    assert not symbols, (mode, "Advisory matches remain", sorted({f["osv"] for f in symbols}))
    return {"config": config, "module_advisory_ids": sorted({f["osv"] for f in findings}),
            "symbol_or_package_wide_findings": 0}


def main():
    settings = json.loads(Path("/build/settings.json").read_text())
    assert run("go", "version").split()[2] == "go1.27.4"
    assert run("git", "rev-parse", "HEAD") == settings["revision"]
    security = settings.get("security")
    patch = Path("/build/security.patch")
    if security:
        assert settings["app"] == "gluetun"
        assert hashlib.sha256(patch.read_bytes()).hexdigest() == security["patch"]["sha256"]
        assert not run("git", "status", "--porcelain", "--untracked-files=no")
        paths = {line.split("\t")[2] for line in run("git", "apply", "--numstat", str(patch)).splitlines()}
        assert paths == {"go.mod", "go.sum"}, "Only locked module files may change"
        subprocess.run(["git", "apply", "--check", str(patch)], check=True)
        subprocess.run(["git", "apply", str(patch)], check=True)
    else:
        assert not patch.read_bytes(), "Undeclared source overlay"
    module_hashes = {p: hashlib.sha256(Path(p).read_bytes()).hexdigest() for p in ("go.mod", "go.sum")}
    if security:
        assert module_hashes == security["patch"]["module_sha256"]
    if settings["app"] == "gluetun":
        flags = (
            f"-s -w -X main.version={settings['version']} "
            f"-X main.created={settings['created']} -X main.commit={settings['commit_label']}"
        )
        subprocess.run(
            ["go", "build", "-mod=readonly", "-trimpath", "-ldflags=" + flags,
             "-o", "/build/application", "cmd/gluetun/main.go"],
            env={**os.environ, "CGO_ENABLED": "0"}, check=True,
        )
    else:
        subprocess.run(
            ["make", "flags-release", "flags-static-pie", "stash",
             "GITHASH=" + settings["githash"],
             "STASH_VERSION=" + settings["version"], "BUILD_DATE=" + settings["build_date"],
             "OFFICIAL_BUILD=" + settings["official_build"]],
            env={**os.environ, "STASH_OUTPUT": "/build/application"},
            check=True,
        )
    proof = Path("/build/provenance")
    proof.mkdir()
    settings["module_sha256"] = module_hashes
    settings["binary_sha256"] = hashlib.sha256(Path("/build/application").read_bytes()).hexdigest()
    settings["buildinfo"] = run("go", "version", "-m", "/build/application")
    if security:
        subprocess.run(["go", "test", "-mod=readonly", "-short", "./..."],
                       env={**os.environ, "CGO_ENABLED": "0"}, check=True)
        settings["advisory_scans"] = {}
        for mode, target in (("source", "./cmd/gluetun"), ("binary", "/build/application")):
            result = subprocess.check_output(
                ["go", "run", "golang.org/x/vuln/cmd/govulncheck@" + security["govulncheck"],
                 "-json", "-scan=symbol", "-mode=" + mode, target],
                env={**os.environ, "CGO_ENABLED": "0"}, encoding="utf-8",
            )
            (proof / (mode + "-advisories.jsonl")).write_text(result)
            settings["advisory_scans"][mode] = advisory_summary(result, mode, security["govulncheck"])
        settings["advisory_binary_sha256"] = hashlib.sha256(Path("/build/application").read_bytes()).hexdigest()
        assert settings["advisory_binary_sha256"] == settings["binary_sha256"]
        assert set(run("git", "diff", "--name-only").splitlines()) == {"go.mod", "go.sum"}
        shutil.copyfile(patch, proof / "security.patch")
        for name, version in security["modules"].items():
            module_directory = run("go", "list", "-m", "-f", "{{.Dir}}", name)
            assert module_directory, (name, "Module source directory missing")
            directory = Path(module_directory)
            licenses = [p for p in directory.glob("LICENSE*") if p.is_file()]
            assert directory.is_dir() and licenses, (name, "Module license missing")
            destination = proof / "module-licenses" / (name + "@" + version)
            destination.mkdir(parents=True)
            for path in licenses:
                shutil.copyfile(path, destination / path.name)
    assert module_hashes == {p: hashlib.sha256(Path(p).read_bytes()).hexdigest() for p in module_hashes}
    (proof / "source.json").write_text(json.dumps(settings, indent=2))
    (proof / "buildinfo.txt").write_text(settings["buildinfo"] + "\n")
    for path in Path(".").glob("LICENSE*"):
        if path.is_file():
            shutil.copyfile(path, proof / path.name)


if __name__ == "__main__":
    main()
