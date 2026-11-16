#!/usr/bin/env python3
"""Reproduce the standalone scan-cache lock and verify the rendered fix."""

import argparse
import hashlib
import io
import json
import os
from pathlib import Path
import re
import subprocess
import tarfile
import tempfile

import yaml

ROOT = Path(__file__).resolve().parents[2]
SOURCE = ROOT / "infrastructure" / "policies" / "trivy-operator"


def command(*args, env=None):
    return subprocess.check_output(args, text=True, env=env)


def rendered_config(directory):
    release = yaml.safe_load((SOURCE / "helmrelease.yaml").read_text())
    repository = yaml.safe_load((SOURCE / "helmrepository.yaml").read_text())
    cache = yaml.safe_load((SOURCE / "scan-cache-config.yaml").read_text())
    assert "scan-cache-config.yaml" in yaml.safe_load((SOURCE / "kustomization.yaml").read_text())["resources"]
    values = directory / "values.yaml"
    values.write_text(yaml.safe_dump(release["spec"]["values"]))
    home = directory / "helm"
    (home / "repository").mkdir(parents=True)
    env = dict(os.environ, HELM_CACHE_HOME=str(home / "cache"),
               HELM_CONFIG_HOME=str(home / "config"), HELM_DATA_HOME=str(home / "data"),
               HELM_REPOSITORY_CACHE=str(home / "repository"),
               HELM_REPOSITORY_CONFIG=str(home / "repositories.yaml"),
               TMP=str(home), TEMP=str(home), TMPDIR=str(home))
    resources = list(yaml.safe_load_all(command(
        "helm", "template", release["metadata"]["name"], release["spec"]["chart"]["spec"]["chart"],
        "--namespace", release["metadata"]["namespace"], "--repo", repository["spec"]["url"],
        "--version", release["spec"]["chart"]["spec"]["version"], "--values", str(values),
        env=env,
    )))
    configs = [r["data"] for r in resources if r and r["kind"] == "ConfigMap"]
    scanner, = [c for c in configs if "trivy.repository" in c]
    image = scanner["trivy.repository"] + ":" + scanner["trivy.tag"]
    jobs, = [c for c in configs if "scanJob.customVolumes" in c]
    volume, = [v for v in json.loads(jobs["scanJob.customVolumes"])
               if v.get("configMap", {}).get("name") == cache["metadata"]["name"]]
    mount, = [m for m in json.loads(jobs["scanJob.customVolumesMount"]) if m["name"] == volume["name"]]
    assert mount["readOnly"] and cache["metadata"]["namespace"] == release["metadata"]["namespace"]
    config = directory / "trivy.yaml"
    config.write_text(cache["data"][mount["subPath"]])
    return image, config, mount["mountPath"]


def fixture_archive(path):
    layer = io.BytesIO()
    with tarfile.open(fileobj=layer, mode="w") as archive:
        data = b"Public cache-lock regression fixture.\n"
        item = tarfile.TarInfo("fixture.txt")
        item.size = len(data)
        item.mode = 0o644
        archive.addfile(item, io.BytesIO(data))
    blob = layer.getvalue()
    config = json.dumps({"architecture": "amd64", "os": "linux", "config": {},
                         "rootfs": {"type": "layers", "diff_ids": ["sha256:" + hashlib.sha256(blob).hexdigest()]}}).encode()
    manifest = json.dumps([{"Config": "config.json", "RepoTags": ["cache-fixture:latest"],
                            "Layers": ["layer.tar"]}]).encode()
    with tarfile.open(path, "w") as archive:
        for name, data in (("config.json", config), ("layer.tar", blob), ("manifest.json", manifest)):
            item = tarfile.TarInfo(name)
            item.mode = 0o644
            item.size = len(data)
            archive.addfile(item, io.BytesIO(data))


def scan(image, directory, config=None, target=None):
    args = [
        "docker", "run", "--detach", "--read-only", "--network", "none", "--cap-drop", "ALL",
        "--security-opt", "no-new-privileges", "--cpus", "1", "--memory", "512m",
        "--user", f"{os.getuid()}:{os.getgid()}",
        "--tmpfs", "/tmp:rw,noexec,nosuid,size=64m",
        "--mount", f"type=bind,source={directory / 'cache'},target=/tmp/trivy/.cache",
        "--mount", f"type=bind,source={directory / 'fixture.tar'},target=/fixture.tar,readonly",
    ]
    if config:
        args += ["--mount", f"type=bind,source={config},target={target},readonly"]
    args += [image, "--cache-dir", "/tmp/trivy/.cache", "--skip-version-check", "image",
             "--scanners", "secret", "--offline-scan", "--format", "json", "--input", "/fixture.tar"]
    identifier = command(*args).strip()
    assert re.fullmatch(r"[a-f0-9]{64}", identifier), "Docker did not return an owned container ID"
    try:
        result = int(subprocess.check_output(["docker", "wait", identifier], text=True, timeout=60).strip())
        logs = subprocess.run(["docker", "logs", identifier], capture_output=True, text=True, check=True)
        state = json.loads(command("docker", "inspect", "--format", "{{json .State}}", identifier))
        assert not state["OOMKilled"], "Native cache probe exceeded its memory bound"
        return result, logs.stdout, logs.stderr
    finally:
        subprocess.run(["docker", "rm", "--force", identifier], check=True, stdout=subprocess.DEVNULL)


def main():
    parser = argparse.ArgumentParser()
    parser.add_argument("--render-only", action="store_true", help="Validate exact chart wiring without claiming native acceptance")
    args = parser.parse_args()
    with tempfile.TemporaryDirectory() as temporary:
        directory = Path(temporary)
        image, config, target = rendered_config(directory)
        print("EXACT_CHART_SCAN_CACHE_WIRING_VERIFIED", image, flush=True)
        if args.render_only:
            print("NATIVE_CACHE_LOCK_CHECK_NOT_RUN", flush=True)
            return
        import fcntl

        subprocess.run(["docker", "pull", image], check=True, stdout=subprocess.DEVNULL)
        (directory / "cache").mkdir()
        fixture_archive(directory / "fixture.tar")
        result, output, error = scan(image, directory)
        assert result == 0, error
        assert json.loads(output)["SchemaVersion"] == 2
        database, = (directory / "cache").rglob("*.db")
        with database.open("r+b") as lock:
            fcntl.flock(lock, fcntl.LOCK_EX | fcntl.LOCK_NB)
            result, _, error = scan(image, directory)
            assert result != 0 and "cache may be in use by another process" in error, error
            result, output, error = scan(image, directory, config, target)
            assert result == 0, error
            assert json.loads(output)["SchemaVersion"] == 2
        print("NATIVE_EXCLUSIVE_FS_CACHE_FAILURE_AND_CONFIGURED_MEMORY_CACHE_SUCCESS_VERIFIED", flush=True)


if __name__ == "__main__":
    main()
