"""Compile locked public source without changing dependency versions."""
import hashlib
import json
import os
from pathlib import Path
import shutil
import subprocess


def run(*args, **kwargs):
    return subprocess.check_output(args, text=True, **kwargs).strip()


settings = json.loads(Path("/build/settings.json").read_text())
assert run("go", "version").split()[2] == "go1.27.3"
assert run("git", "rev-parse", "HEAD") == settings["revision"]
assert not run("git", "status", "--porcelain", "--untracked-files=no")
module_hashes = {p: hashlib.sha256(Path(p).read_bytes()).hexdigest() for p in ("go.mod", "go.sum")}
if settings["app"] == "gluetun":
    flags = (
        f"-s -w -X main.version={settings['version']} "
        f"-X main.created={settings['created']} -X main.commit={settings['commit_label']}"
    )
    subprocess.run(
        ["go", "build", "-trimpath", "-ldflags=" + flags,
         "-o", "/build/application", "cmd/gluetun/main.go"],
        env={**os.environ, "CGO_ENABLED": "0"}, check=True,
    )
else:
    subprocess.run(["make", "touch-ui"], check=True)
    generated = subprocess.run(["go", "run", "github.com/99designs/gqlgen", "--verbose"])
    if generated.returncode:
        subprocess.run(["go", "mod", "tidy", "-diff"])
        generated.check_returncode()
    subprocess.run(["make", "generate-login-locale"], check=True)
    subprocess.run(
        ["make", "flags-release", "flags-static-pie", "stash",
         "STASH_OUTPUT=/build/application", "GITHASH=" + settings["githash"],
         "STASH_VERSION=" + settings["version"], "BUILD_DATE=" + settings["build_date"],
         "OFFICIAL_BUILD=" + settings["official_build"]],
        check=True,
    )
assert module_hashes == {p: hashlib.sha256(Path(p).read_bytes()).hexdigest() for p in module_hashes}
proof = Path("/build/provenance")
proof.mkdir()
settings["module_sha256"] = module_hashes
settings["binary_sha256"] = hashlib.sha256(Path("/build/application").read_bytes()).hexdigest()
settings["buildinfo"] = run("go", "version", "-m", "/build/application")
(proof / "source.json").write_text(json.dumps(settings, indent=2))
(proof / "buildinfo.txt").write_text(settings["buildinfo"] + "\n")
for path in Path(".").glob("LICENSE*"):
    if path.is_file():
        shutil.copyfile(path, proof / path.name)
