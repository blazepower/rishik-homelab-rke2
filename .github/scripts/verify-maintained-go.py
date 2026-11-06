"""Build and exercise the actual same-version derivative, never production."""
import argparse
import hashlib
import json
import os
from pathlib import Path
import re
import shutil
import subprocess
import time
import urllib.request
import zipfile

ROOT = Path(__file__).resolve().parents[2]
BUILD = ROOT / "apps" / "_shared" / "maintained-go"


def run(*args, check=True):
    result = subprocess.run(args, text=True, stdout=subprocess.PIPE, stderr=subprocess.STDOUT)
    if check and result.returncode:
        raise RuntimeError(f"{args[:4]} failed ({result.returncode}):\n{result.stdout[-64000:]}")
    return result


def docker(*args, **kwargs):
    return run("docker", *args, **kwargs)


def http(container, path, payload=None):
    code = (
        "import urllib.request,json; "
        f"u=urllib.request.Request('http://127.0.0.1:9999{path}',"
        f"data={repr(json.dumps(payload).encode()) if payload is not None else 'None'},"
        "headers={'Content-Type':'application/json'}); "
        "print(urllib.request.urlopen(u,timeout=5).read().decode())"
    )
    return docker("exec", container, "python3", "-c", code).stdout


def wait_http(container):
    deadline = time.monotonic() + 120
    while time.monotonic() < deadline:
        try:
            return http(container, "/")
        except RuntimeError:
            if docker("inspect", "-f", "{{.State.Running}}", container).stdout.strip() != "true":
                break
            time.sleep(2)
    raise RuntimeError("Native startup failed:\n" + docker("logs", container).stdout[-12000:])


def proof_binary(image, app, scratch, builder):
    container = docker("create", image).stdout.strip()
    try:
        docker("cp", container + ":" + app["binary"], str(scratch / "application.elf"))
        docker("cp", container + ":/usr/share/maintained-go/source.json", str(scratch / "source.json"))
        info = docker(
            "run", "--rm", "--network", "none", "--mount",
            f"type=bind,src={scratch},dst=/proof,readonly", builder,
            "go", "version", "-m", "/proof/application.elf",
        ).stdout
        assert re.search(r"application\.elf: go1\.27\.3\b", info), info
        source = json.loads((scratch / "source.json").read_text())
        assert source["revision"] == app["revision"]
        assert source["binary_sha256"] == hashlib.sha256((scratch / "application.elf").read_bytes()).hexdigest()
        assert "CGO_ENABLED=" + ("1" if source["app"] == "stash" else "0") in info
        if source["app"] == "stash":
            assert "vcs.revision=" + app["revision"] in info
            assert "-buildmode=pie" in info
            assert "-tags=sqlite_stat4,sqlite_math_functions,sqlite_omit_load_extension,osusergo,netgo" in info
        return {"sha256": source["binary_sha256"], "actual_elf_buildinfo": info, "source": source}
    finally:
        docker("rm", container)
        (scratch / "application.elf").unlink(missing_ok=True)


def native_gluetun(image, app):
    health = docker("run", "--rm", "--network", "none", image, "healthcheck", check=False)
    assert health.returncode != 0 and "http://127.0.0.1:9999" in health.stdout and "connection refused" in health.stdout, health.stdout
    startup = docker("run", "--rm", "--network", "none", "--cap-drop", "ALL",
                     "-e", "VPN_SERVICE_PROVIDER=invalid-synthetic-provider", image, check=False)
    assert startup.returncode != 0
    assert app["version"] in startup.stdout and app["commit_label"] in startup.stdout, startup.stdout
    assert "error" in startup.stdout.lower(), startup.stdout
    return {"native_startup_exit": startup.returncode, "native_startup": startup.stdout,
            "healthcheck_exit": health.returncode, "healthcheck": health.stdout,
            "scope": "Network-none, no capabilities, no VPN credentials. Native startup stops at missing default route; native healthcheck reports connection refused. Not VPN connectivity or provider-validation clearance."}


def native_stash(image, app, expected_version, scratch, baseline=False):
    version = docker("run", "--rm", "--network", "none", image, "stash", "--version").stdout.strip()
    assert version == expected_version and app["version"] in version, (version, expected_version)
    config = scratch / ("vendor-synthetic" if baseline else "synthetic")
    config.mkdir(exist_ok=True)
    (config / "config.yml").write_text(
        "host: 127.0.0.1\nport: 9999\ndatabase: /proof/stash-go.sqlite\n"
        "generated: /proof/generated\nmetadata: /proof/metadata\ncache: /proof/cache\n"
        "stash: []\n"
    )
    name = "maintained-stash-baseline" if baseline else "maintained-stash-proof"
    docker("run", "-d", "--name", name, "--network", "none",
           "--mount", f"type=bind,src={config},dst=/proof",
           "-e", "STASH_CONFIG_FILE=/proof/config.yml", image)
    try:
        ui = wait_http(name)
        assert "<html" in ui.lower() and "<script" in ui.lower(), ui[:300]
        asset_paths = re.findall(r'(?:src|href)="([^"]+\.(?:js|css))"', ui)
        assert asset_paths, "Original UI assets missing"
        assets = {}
        with zipfile.ZipFile(scratch / "stash-ui.zip") as archive:
            for asset in asset_paths:
                path = asset.lstrip("./")
                code = (
                    "import urllib.request,hashlib; "
                    f"print(hashlib.sha256(urllib.request.urlopen('http://127.0.0.1:9999/{path}').read()).hexdigest())"
                )
                actual = docker("exec", name, "python3", "-c", code).stdout.strip()
                assert actual == hashlib.sha256(archive.read(path)).hexdigest(), path
                assets[path] = actual
        created = json.loads(http(name, "/graphql", {"query": 'mutation { tagCreate(input:{name:"synthetic compiler proof"}) { id name } }'}))
        assert not created.get("errors"), created
        tag = created["data"]["tagCreate"]
        found = json.loads(http(name, "/graphql", {"query": f'query {{ findTag(id:"{tag["id"]}") {{ id name }} }}'}))
        assert found["data"]["findTag"] == tag, found
        # Restart only this disposable container to prove the synthetic DB persists.
        docker("restart", name)
        wait_http(name)
        persisted = json.loads(http(name, "/graphql", {"query": f'query {{ findTag(id:"{tag["id"]}") {{ id name }} }}'}))
        assert persisted["data"]["findTag"] == tag, persisted
        ffmpeg = docker("exec", name, "ffmpeg", "-version").stdout.splitlines()[0]
        logs = docker("logs", name).stdout
        assert "v0.31.1" in logs
        return {"version": version, "ui_sha256": hashlib.sha256(ui.encode()).hexdigest(),
                "locked_ui_assets": assets, "synthetic_tag": tag,
                "synthetic_db_reopen": True, "ffmpeg": ffmpeg,
                "scope": "Actual native UI, locked vendor assets, synthetic SQLite write/read/reopen, no production data or external network."}
    finally:
        docker("rm", "-f", name)


def main():
    parser = argparse.ArgumentParser()
    parser.add_argument("--app", required=True, choices=["gluetun", "stash"])
    parser.add_argument("--publish", action="store_true")
    args = parser.parse_args()
    lock = json.loads((BUILD / "sources.json").read_text())
    app = lock["applications"][args.app]
    scratch = ROOT / ".maintained-go" / args.app
    scratch.mkdir(parents=True, exist_ok=False)
    report = {"app": args.app, "head": run("git", "rev-parse", "HEAD").stdout.strip(),
              "workflow_head": os.environ.get("GITHUB_SHA"),
              "vendor": app["vendor"], "source_revision": app["revision"],
              "compiler": lock["compiler"], "published": False}
    tag = "ghcr.io/blazepower/maintained-" + args.app + ":" + report["head"]
    try:
        docker("pull", app["vendor"])
        original = json.loads(docker("image", "inspect", app["vendor"]).stdout)[0]
        run("git", "clone", "--quiet", "--no-checkout", app["repository"], str(scratch / "upstream"))
        run("git", "-C", str(scratch / "upstream"), "checkout", "--quiet", app["revision"])
        settings = {**app, "app": args.app}
        expected_version = None
        if args.app == "stash":
            expected_version = docker("run", "--rm", "--network", "none", app["vendor"], "stash", "--version").stdout.strip()
            match = re.search(r"v0\.31\.1 \(([^)]+)\) - (Official|Unofficial) Build - (\d{4}-\d\d-\d\d \d\d:\d\d:\d\d)", expected_version)
            assert match, expected_version
            settings.update(githash=match[1], official_build=str(match[2] == "Official").lower(), build_date=match[3])
            with urllib.request.urlopen(app["ui_url"], timeout=120) as response:
                (scratch / "stash-ui.zip").write_bytes(response.read())
            assert hashlib.sha256((scratch / "stash-ui.zip").read_bytes()).hexdigest() == app["ui_sha256"]
            dest = scratch / "upstream" / "ui" / "v2.5" / "build"
            dest.mkdir(parents=True, exist_ok=True)
            with zipfile.ZipFile(scratch / "stash-ui.zip") as archive:
                for member in archive.infolist():
                    assert not member.filename.startswith("/") and ".." not in Path(member.filename).parts
                archive.extractall(dest)
        (scratch / "settings.json").write_text(json.dumps(settings))
        shutil.copyfile(BUILD / "build.py", scratch / "build.py")
        docker("build", "--platform", "linux/amd64", "--progress", "plain",
               "--build-arg", "BUILDER=" + lock["builder"], "--build-arg", "VENDOR=" + app["vendor"],
               "--build-arg", "BINARY=" + app["binary"], "-f", str(BUILD / "Dockerfile"),
               "-t", tag, str(scratch))
        actual = json.loads(docker("image", "inspect", tag).stdout)[0]
        for key in ("Entrypoint", "Cmd", "User", "WorkingDir", "Env", "ExposedPorts", "Healthcheck"):
            assert actual["Config"].get(key) == original["Config"].get(key), key
        inherited = original["RootFS"]["Layers"]
        assert actual["RootFS"]["Layers"][:len(inherited)] == inherited, "Vendor auxiliary filesystem was replaced"
        report["image_id"] = actual["Id"]
        report["vendor_layers_preserved"] = len(inherited)
        report["binary"] = proof_binary(tag, app, scratch, lock["builder"])
        if args.app == "stash":
            report["vendor_native"] = native_stash(app["vendor"], app, expected_version, scratch, baseline=True)
            report["native"] = native_stash(tag, app, expected_version, scratch)
            for key in ("version", "ui_sha256", "locked_ui_assets", "ffmpeg"):
                assert report["native"][key] == report["vendor_native"][key], key
        else:
            report["native"] = native_gluetun(tag, app)
        if args.publish:
            result = docker("push", tag).stdout
            digest = re.search(r"digest: (sha256:[0-9a-f]{64})", result)
            assert digest, result
            immutable = tag.split(":")[0] + "@" + digest[1]
            docker("pull", immutable)
            assert json.loads(docker("image", "inspect", immutable).stdout)[0]["Id"] == report["image_id"]
            registry_proof = proof_binary(immutable, app, scratch, lock["builder"])
            assert registry_proof["sha256"] == report["binary"]["sha256"]
            report.update(published=True, image=immutable, amd64_digest=digest[1])
        report["limitations"] = "Compiler/public source and bounded native behavior only; not universal custom/intermediate TLS or forensic clearance. Existing GitOps CA guards remain required."
        output = ROOT / ("maintained-go-" + args.app + "-evidence.json")
        output.write_text(json.dumps(report, indent=2))
        print(json.dumps(report, indent=2))
    finally:
        shutil.rmtree(scratch)


if __name__ == "__main__":
    main()
