"""Candidate-only actual-artifact acceptance; never uses a production cluster."""
import hashlib
import importlib.util
import json
import os
from pathlib import Path, PurePosixPath
import shutil
import subprocess
import tarfile
import urllib.request

ROOT = Path(__file__).resolve().parents[2]
BUILD = ROOT / "apps" / "_shared" / "prometheus-owned"


def load(name, path):
    spec = importlib.util.spec_from_file_location(name, path)
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


shared = load("maintained_prometheus_shared", Path(__file__).with_name("verify-maintained-go.py"))
http_owned = load("maintained_prometheus_filesystem", Path(__file__).with_name("verify-http-addon-owned.py"))
scanner = load("maintained_prometheus_scanner", ROOT / "apps" / "_shared" / "maintained-go" / "build.py")
docker, run = shared.docker, shared.run
DERIVATIVE = "Same-version Prometheus with Go 1.27.4 and locked module updates; not an official Prometheus image"


def saved_config(archive):
    with tarfile.open(archive, "r:") as source:
        members = source.getmembers()
        names = {member.name: member for member in members}
        assert len(names) == len(members), "Duplicate image archive members"
        manifest_member = names["manifest.json"]
        assert manifest_member.isfile() and manifest_member.size <= 1024 * 1024
        manifest = json.load(source.extractfile(manifest_member))
        assert len(manifest) == 1, "Expected exactly one saved image"
        config_member = names[manifest[0]["Config"]]
        assert config_member.isfile() and config_member.size <= 1024 * 1024
        raw = source.extractfile(config_member).read()
        config = json.loads(raw)
        assert config["os"] == "linux" and config["architecture"] == "amd64"
        assert config["rootfs"]["type"] == "layers"
        assert len(manifest[0]["Layers"]) == len(config["rootfs"]["diff_ids"])
        return "sha256:" + hashlib.sha256(raw).hexdigest(), config


def verify_vendor_image(inspected, lock, archive):
    digest, config = saved_config(archive)
    assert digest == lock["vendor_config"], "Saved vendor OCI config differs from locked config"
    assert lock["vendor"] in inspected["RepoDigests"], "Pulled repository digest differs"
    assert inspected["Id"] in (digest, lock["vendor"].split("@")[1]), "Unbound engine image ID"
    assert inspected["RootFS"]["Layers"] == config["rootfs"]["diff_ids"], "Pulled rootfs differs"
    for field, value in config["config"].items():
        assert inspected["Config"].get(field) == value, (field, "Pulled OCI execution config differs")
    assert inspected["Config"]["User"] == "65532"
    assert inspected["Config"]["Labels"]["io.prometheus.image.variant"] == "distroless"
    return {"engine_image_id": inspected["Id"], "saved_config_digest": digest,
            "repository_digest": lock["vendor"], "rootfs_diff_ids": config["rootfs"]["diff_ids"],
            "inherited_user": inspected["Config"]["User"], "platform": "linux/amd64"}


def modules(info):
    result = {}
    for line in info.splitlines():
        fields = line.split()
        assert not fields or fields[0] != "=>", "Undeclared linked module replacement"
        if fields and fields[0] == "dep":
            assert len(fields) == 4 and fields[1] not in result, ("Malformed or duplicate linked module", fields)
            result[fields[1]] = tuple(fields[2:])
    assert result, "Actual ELF module inventory absent"
    return result


def dependency_delta(before, after, lock):
    assert before.keys() == after.keys(), "Linked module membership changed"
    changed = {name for name in before if before[name] != after[name]}
    assert changed == lock["modules"].keys(), ("Unexpected dependency delta", sorted(changed))
    for name in changed:
        assert before[name][0] == lock["modules"][name]["from"], (name, "Factory dependency differs")
        assert after[name][0] == lock["modules"][name]["to"], (name, "Patched dependency differs")
    return {name: {"from": before[name], "to": after[name]} for name in sorted(changed)}


def compare_filesystems(before, after):
    binaries = {"usr/bin/prometheus", "usr/bin/promtool"}
    assert binaries <= before.keys() and binaries <= after.keys(), "Runtime executables absent"
    for path, value in before.items():
        assert path in after, (path, "Factory path removed")
        assert after[path][:-1] == value[:-1], (path, "Factory type/mode/ownership/link changed")
        if path not in binaries:
            assert after[path] == value, (path, "Undeclared factory file/CA change")
        else:
            assert value[-1] != after[path][-1], (path, "Factory executable was not rebuilt")
    additions = after.keys() - before.keys()
    assert additions, "Licensed provenance absent"
    assert all(path in ("usr", "usr/share", "usr/share/prometheus-owned")
               or path.startswith("usr/share/prometheus-owned/") for path in additions), additions
    return {"unchanged_factory_paths": len(before) - 2, "provenance_additions": len(additions),
            "excluded_runtime_mount_files": ["etc/hostname", "etc/hosts", "etc/resolv.conf"]}


def unpack_ui(archive, target):
    files = {}
    with tarfile.open(archive, "r:gz") as source:
        members = source.getmembers()
        assert sum(member.size for member in members) <= 32 * 1024 * 1024, "UI archive exceeds reviewed bound"
        for member in members:
            path = PurePosixPath(member.name)
            assert not path.is_absolute() and ".." not in path.parts and "\\" not in member.name
            assert path.parts and path.parts[0] == "static" and (member.isdir() or member.isfile()), member.name
            if member.isdir():
                continue
            name = path.as_posix()
            assert name not in files, ("Duplicate UI member", name)
            data = source.extractfile(member).read()
            destination = target.joinpath(*path.parts)
            destination.parent.mkdir(parents=True, exist_ok=True)
            assert not destination.exists() or destination.read_bytes() == data, ("Factory UI collision", name)
            destination.write_bytes(data)
            files[name] = hashlib.sha256(data).hexdigest()
    assert "static/mantine-ui/index.html" in files and "static/react-app/index.html" in files
    return files


def binary_proof(image, sdk, lock, directory, candidate):
    container = docker("create", image).stdout.strip()
    result = {}
    try:
        source = None
        if candidate:
            docker("cp", container + ":/usr/share/prometheus-owned/source.json", str(directory / "source.json"))
            source = json.loads((directory / "source.json").read_text())
            assert {key: source[key] for key in lock} == lock, "Declared source contract differs"
        for name in ("prometheus", "promtool"):
            path = directory / name
            docker("cp", container + ":/usr/bin/" + name, str(path))
            sha = hashlib.sha256(path.read_bytes()).hexdigest()
            info = docker("run", "--rm", "--network=none", "--read-only", "--cap-drop=ALL",
                          "--security-opt=no-new-privileges", "--user=65532:65532", "--mount",
                          f"type=bind,src={directory},dst=/proof,readonly", sdk,
                          "go", "version", "-m", "/proof/" + name).stdout
            compiler = lock["compiler"] if candidate else "go1.27.3"
            assert info.splitlines()[0] == "/proof/" + name + ": " + compiler
            assert all(flag in info for flag in ("CGO_ENABLED=0", "GOOS=linux", "GOARCH=amd64",
                                                 "-tags=netgo,builtinassets")), (name, info)
            result[name] = {"sha256": sha, "actual_elf_buildinfo": info, "modules": modules(info)}
            if candidate:
                proof = source["actual_binaries"][name]
                assert sha == proof["sha256"] and proof["symbol_table_verified"] is True
                assert info.splitlines()[1:] == proof["buildinfo"].splitlines()[1:]
                for mode in ("source", "binary"):
                    filename = name + "-" + mode + ".jsonl"
                    docker("cp", container + ":/usr/share/prometheus-owned/" + filename, str(directory / filename))
                    actual = scanner.advisory_summary(
                        (directory / filename).read_text(), mode, lock["govulncheck"])
                    assert actual == proof["advisory_scans"][mode]
                result[name]["advisory_scans"] = proof["advisory_scans"]
        return result
    finally:
        docker("rm", container)


def native(images, sdk, lock, scratch, ui):
    fixture, data = scratch / "fixture", scratch / "synthetic-data"
    fixture.mkdir()
    fixture.chmod(0o755)
    data.mkdir()
    data.chmod(0o777)
    shutil.copyfile(BUILD / "probe.py", fixture / "probe.py")
    (fixture / "ui-files.json").write_text(json.dumps(ui))
    for command in (
        ["openssl", "req", "-x509", "-newkey", "rsa:2048", "-nodes", "-days", "1",
         "-subj", "/CN=synthetic-owned-root", "-keyout", "ca.key", "-out", "ca.crt"],
        ["openssl", "req", "-newkey", "rsa:2048", "-nodes", "-subj", "/CN=localhost",
         "-addext", "subjectAltName=DNS:localhost", "-keyout", "server.key", "-out", "server.csr"],
        ["openssl", "x509", "-req", "-in", "server.csr", "-CA", "ca.crt", "-CAkey", "ca.key",
         "-CAcreateserial", "-days", "1", "-copy_extensions", "copy", "-out", "server.crt"],
    ):
        subprocess.run(command, cwd=fixture, check=True, stdout=subprocess.PIPE, stderr=subprocess.PIPE)
    for path in fixture.iterdir():
        path.chmod(0o600 if path.suffix == ".key" else 0o644)
    jobs = []
    for name in ("plain", "trusted", "untrusted", "wrong-host"):
        job = {"job_name": name, "static_configs": [{"targets": [
            "127.0.0.1:" + ("18081" if name == "plain" else "18080")]}]}
        if name != "plain":
            job["scheme"] = "https"
            job["tls_config"] = {"server_name": "not-localhost.invalid" if name == "wrong-host" else "localhost"}
            if name != "untrusted":
                job["tls_config"]["ca_file"] = "/fixture/ca.crt"
        jobs.append(job)
    results = []
    try:
        for phase, image, previous, value in (
            ("vendor", images[0], None, 7), ("candidate", images[1], 7, 11), ("vendor-reopen", images[0], 11, 13)
        ):
            config = {"global": {"scrape_interval": "1s", "scrape_timeout": "1s"}, "scrape_configs": jobs}
            config_path = fixture / "prometheus.json"
            config_path.write_text(json.dumps(config))
            config_path.chmod(0o644)
            container = docker(
                "run", "-d", "--network=none", "--read-only", "--cap-drop=ALL",
                "--security-opt=no-new-privileges", "--pids-limit=128", "--memory=512m", "--cpus=1",
                "--tmpfs", "/tmp:rw,noexec,nosuid,size=32m,mode=1777", "--mount",
                f"type=bind,src={fixture},dst=/fixture,readonly", "--mount",
                f"type=bind,src={data},dst=/prometheus", image,
                "--config.file=/fixture/prometheus.json", "--storage.tsdb.path=/prometheus",
                "--web.listen-address=127.0.0.1:9090", "--web.enable-lifecycle",
            ).stdout.strip()
            try:
                args = ["--phase", phase, "--value", str(value), "--compiler",
                        lock["compiler"] if phase == "candidate" else "go1.27.3",
                        "--revision", lock["revision"]]
                if previous is not None:
                    args += ["--previous", str(previous)]
                output = docker("run", "--rm", "--network", "container:" + container, "--read-only",
                                "--cap-drop=ALL", "--security-opt=no-new-privileges", "--pids-limit=64",
                                "--memory=256m", "--cpus=0.5", "--user", f"{os.getuid()}:{os.getgid()}",
                                "--mount", f"type=bind,src={fixture},dst=/fixture", sdk,
                                "python3", "-B", "/fixture/probe.py", *args).stdout
                evidence = json.loads(output)
                state = json.loads(docker("inspect", container).stdout)[0]["State"]
                assert state["Running"] and not state["OOMKilled"], state
                results.append({**evidence, "image": image, "oom_killed": False})
            finally:
                docker("stop", "--time=30", container)
                exit_state = json.loads(docker("inspect", container).stdout)[0]["State"]
                logs = docker("logs", container).stdout
                docker("rm", container)
                assert exit_state["ExitCode"] == 0 and not exit_state["OOMKilled"], (exit_state, logs[-12000:])
        assert any(path.is_file() for path in (data / "wal").rglob("*")), "No actual native WAL persisted"
        assert all(result["ui_assets"] == results[0]["ui_assets"]
                   and result["rendered_index_sha256"] == results[0]["rendered_index_sha256"]
                   for result in results)
        return {"phases": results, "native_wal_persisted": True,
                "scope": "Synthetic loopback TLS, actual default UI/API/reload, vendor/candidate/vendor WAL reopen. "
                         "No host network, Kubernetes, external datasource, real data or production recovery proof."}
    finally:
        docker("run", "--rm", "--network=none", "--read-only", "--cap-drop=ALL",
               "--security-opt=no-new-privileges", "--user=65532:65532", "--mount",
               f"type=bind,src={data},dst=/synthetic-data", sdk, "python3", "-B", "-c",
               "from pathlib import Path; import shutil; "
               "root=Path('/synthetic-data'); "
               "[(shutil.rmtree(p) if p.is_dir() else p.unlink()) for p in root.iterdir()]")


def main():
    assert os.environ.get("RUNNER_ENVIRONMENT") == "github-hosted", "Native fixtures require isolated hosted CI"
    lock = json.loads((BUILD / "source.json").read_text())
    scratch = ROOT / ".prometheus-owned"
    scratch.mkdir(exist_ok=False)
    scratch.chmod(0o755)
    head = run("git", "rev-parse", "HEAD").stdout.strip()
    tag, sdk = "prometheus-owned-native:" + head, "prometheus-owned-build:" + head
    report = {"head": head, "workflow_head": os.environ.get("GITHUB_SHA"), "source": lock,
              "state": "build_or_native_incomplete", "published": False}
    try:
        run("git", "clone", "--quiet", "--depth=1", "--branch", lock["version"],
            lock["repository"], str(scratch / "upstream"))
        assert run("git", "-C", str(scratch / "upstream"), "rev-parse", "HEAD").stdout.strip() == lock["revision"]
        for name in ("source.json", "security.patch", "build.py"):
            shutil.copyfile(BUILD / name, scratch / name)
        shutil.copyfile(ROOT / "apps" / "_shared" / "maintained-go" / "build.py", scratch / "maintained_build.py")
        archive = scratch / "ui.tar.gz"
        with urllib.request.urlopen(lock["ui"]["url"], timeout=120) as response:
            content = response.read(10 * 1024 * 1024)
        assert hashlib.sha256(content).hexdigest() == lock["ui"]["sha256"], "Official UI archive mismatch"
        archive.write_bytes(content)
        ui = unpack_ui(archive, scratch / "upstream" / "web" / "ui")
        (scratch / "ui-files.json").write_text(json.dumps(ui))
        arguments = ["--platform=linux/amd64", "--progress=plain",
                     "--build-arg", "BUILDER=" + lock["builder"], "--build-arg", "VENDOR=" + lock["vendor"],
                     "-f", str(BUILD / "Dockerfile")]
        docker("pull", lock["vendor"])
        original = json.loads(docker("image", "inspect", lock["vendor"]).stdout)[0]
        report["vendor_engine_identity"] = {
            "Id": original["Id"], "RepoDigests": original["RepoDigests"],
            "RootFS": original["RootFS"], "User": original["Config"]["User"]}
        vendor_archive = scratch / "vendor-image.tar"
        docker("save", "--output", str(vendor_archive), lock["vendor"])
        report["verified_vendor_image"] = verify_vendor_image(original, lock, vendor_archive)
        vendor_archive.unlink()
        docker("build", *arguments, "-t", tag, str(scratch))
        docker("build", *arguments, "--target=build", "-t", sdk, str(scratch))
        tests = docker("run", "--rm", "--network=none", "--cap-drop=ALL",
                       "--security-opt=no-new-privileges", "--pids-limit=512", "--memory=4g", "--cpus=2",
                       sdk, "go", "test", "-p=2", "-mod=readonly", "-tags=netgo,builtinassets",
                       "-short", "-timeout=5m", "./web/...", "./cmd/prometheus", "./cmd/promtool").stdout
        report["offline_upstream_tests"] = tests
        actual = json.loads(docker("image", "inspect", tag).stdout)[0]
        expected = json.loads(json.dumps(original["Config"]))
        expected["Labels"] = {**(expected.get("Labels") or {}),
                              "org.opencontainers.image.source": "https://github.com/blazepower/rishik-homelab-rke2",
                              "io.homelab.derivative": DERIVATIVE}
        assert actual["Config"] == expected, "Factory runtime configuration changed"
        report["actual_runtime_identity"] = shared.runtime_identity(actual)
        before = http_owned.filesystem(lock["vendor"], scratch / "vendor-fs.tar")
        after = http_owned.filesystem(tag, scratch / "candidate-fs.tar")
        report["filesystem"] = compare_filesystems(before, after)
        baseline, candidate = scratch / "baseline", scratch / "candidate"
        baseline.mkdir()
        candidate.mkdir()
        report["vendor_binaries"] = binary_proof(lock["vendor"], sdk, lock, baseline, False)
        report["candidate_binaries"] = binary_proof(tag, sdk, lock, candidate, True)
        report["dependency_deltas"] = {
            name: dependency_delta(report["vendor_binaries"][name]["modules"],
                                   report["candidate_binaries"][name]["modules"], lock)
            for name in ("prometheus", "promtool")}
        report["native"] = native((lock["vendor"], tag), sdk, lock, scratch, ui)
        for label, image in (("vendor", lock["vendor"]), ("candidate", tag)):
            evidence = scratch / (label + "-rules.json")
            run("python3", "-B", str(Path(__file__).with_name("test-storage-health-alerts.py")),
                "--docker", "--local-image", image, "--evidence", str(evidence))
            report[label + "_actual_storage_rules"] = json.loads(evidence.read_text())
        output = ROOT / "prometheus-owned-image.tar"
        docker("save", "--output", str(output), tag)
        with output.open("rb") as stream:
            report["actual_image_archive_sha256"] = hashlib.file_digest(stream, "sha256").hexdigest()
        digest, config = saved_config(output)
        assert config["rootfs"]["diff_ids"] == actual["RootFS"]["Layers"], "Saved candidate rootfs differs"
        for field, value in config["config"].items():
            assert actual["Config"].get(field) == value, (field, "Saved candidate execution config differs")
        report["actual_image_archive_config_digest"] = digest
        report["state"] = "native_accepted_candidate_only"
        report["limitations"] = (
            "No publication or activation. Exact accepted public image archive retained for canonical "
            "all-severity package scanning; no whole-image or production security clearance yet. "
            "Default-root CA files preserved, not universal PKI/revocation proof. "
            "Current default UI only; synthetic WAL reopen is not real production recovery acceptance.")
        print(json.dumps(report, indent=2))
    finally:
        (ROOT / "prometheus-owned-evidence.json").write_text(json.dumps(report, indent=2))
        shutil.rmtree(scratch)


if __name__ == "__main__":
    main()
