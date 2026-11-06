"""Separate locked gqlgen export-data compatibility from the app compiler."""
import hashlib
import json
from pathlib import Path
import subprocess

settings = json.loads(Path("/build/settings.json").read_text())
assert subprocess.check_output(["git", "rev-parse", "HEAD"], text=True).strip() == settings["revision"]
assert not subprocess.check_output(["git", "status", "--porcelain", "--untracked-files=no"], text=True).strip()
hashes = {p: hashlib.sha256(Path(p).read_bytes()).hexdigest() for p in ("go.mod", "go.sum")}
if settings["app"] == "stash":
    assert subprocess.check_output(["go", "version"], text=True).split()[2] == "go1.26.10"
    subprocess.run(["make", "touch-ui"], check=True)
    result = subprocess.run(["go", "run", "github.com/99designs/gqlgen", "--verbose"])
    if result.returncode:
        subprocess.run(["go", "mod", "tidy", "-diff"])
        result.check_returncode()
    subprocess.run(["make", "generate-login-locale"], check=True)
assert hashes == {p: hashlib.sha256(Path(p).read_bytes()).hexdigest() for p in hashes}
