"""Media trust-overlay tests; SAB bootstrap networking requires a separate explicit opt-in."""

import argparse
import contextlib
import datetime
import hashlib
import importlib.util
import io
import json
from pathlib import Path
import re
import shutil
import ssl
import subprocess
import sys
import tarfile
import time
import unittest
from unittest import mock
import uuid

from cryptography import x509
from cryptography.hazmat.primitives import hashes, serialization
from cryptography.hazmat.primitives.asymmetric import ec, rsa
from cryptography.x509.oid import NameOID
import yaml


ROOT = Path(__file__).resolve().parents[2]
SHARED = ROOT / "apps" / "_shared" / "media-ca-hardening"
APPS = ("bazarr", "prowlarr", "sabnzbd")
REGISTRY = DOCKER = False
ALLOW_SAB_BOOTSTRAP_NETWORK = False
CRANE = "crane"
RUN_LABEL = "homelab.media-ca-test"
ACCEPTANCE = "/media-ca-acceptance"
PACKAGE_STAGE = "/etc/s6-overlay/s6-rc.d/init-mods-package-install/run"
APK_OPERATIONS = ("architecture_query", "ffmpeg_install", "bootstrap_curl_jq", "other")
PROXIES = {
    "/docker-mods": ("mod", "docker-mods"),
    "/sbin/apk": ("apk", "apk"),
    "/usr/sbin/update-ca-certificates": ("ca-update", "update-ca-certificates"),
    "/usr/bin/with-contenv": ("package", "with-contenv"),
}
STARTUP_ERROR_CLASSES = {
    "read_only_filesystem": rb"read-only file system",
    "permission_denied": rb"permission denied",
    "apk_package_selection_failed": rb"unable to select packages|unsatisfiable constraints",
    "apk_repository_fetch_failed": rb"WARNING: fetching|ERROR: fetching",
    "tls_verification_failed": rb"certificate verify failed|SSL certificate problem|unable to get local issuer certificate|curl: \(60\)",
    "dns_resolution_failed": rb"could not resolve host|temporary failure in name resolution",
    "network_connection_failed": rb"failed to connect|connection refused|network is unreachable",
    "network_request_timeout": rb"curl: \(28\)|operation timed out",
    "dependency_http_download_failed": rb"curl: \(22\)",
    "filesystem_space_exhausted": rb"no space left on device",
    "dynamic_loader_failed": rb"error loading shared library|error relocating",
    "startup_executable_unavailable": rb"not found|bad interpreter|exec format error|cannot execute",
    "python_import_failed": rb"ModuleNotFoundError:|ImportError:",
    "s6_service_start_failed": rb"s6-rc:[^\r\n]{0,80}unable to start service",
    "s6_fatal": rb"s6-[a-z0-9-]{1,40}: fatal:",
    "mod_offline_cache_missing": rb"\[mod-init\] \(ERROR\) OFFLINE:[^\r\n]{0,250}not found in modcache, skipping",
    "mod_manifest_unavailable": rb"\[mod-init\] \(ERROR\) No manifest available for arch",
    "mod_registry_no_response": rb"\[mod-init\] \(ERROR\) No response from",
    "mod_invalid_archive": rb"\[mod-init\] \(ERROR\) Invalid tarball for",
    "mod_download_lock_timeout": rb"timed out waiting for lock, skipping",
    "mod_jq_unavailable": rb"(?:/media-ca-acceptance/docker-mods|/docker-mods): line [0-9]+: jq: (?:command )?not found",
    "mod_curl_unavailable": rb"(?:/media-ca-acceptance/docker-mods|/docker-mods): line [0-9]+: curl: (?:command )?not found",
    "stock_s6_interpreter_unavailable": rb"/command/with-contenv:[^\r\n]{0,80}(?:not found|cannot execute)",
}
spec = importlib.util.spec_from_file_location("media_overlay", SHARED / "ca-overlay.py")
guard = importlib.util.module_from_spec(spec)
spec.loader.exec_module(guard)
CANONICAL = {
    entry["sha256"].upper()
    for entry in json.loads((ROOT / ".github" / "security" / "emudhra-2026.json").read_text())["blocked_spkis"]
}


def values(app):
    return yaml.safe_load((ROOT / "apps" / app / "helmrelease.yaml").read_text())["spec"]["values"]


def image(app, container="app"):
    controller = values(app)["controllers"][app]
    entry = controller["containers"][container] if container == "app" else controller["initContainers"][container]
    ref = entry["image"]
    return ref["repository"] + ":" + ref["tag"]


def bootstrap_network(app, allowed, network):
    if app == "sabnzbd" and allowed:
        if not network or network in ("none", "host", "bridge"):
            raise ValueError("SAB bootstrap requires its own disposable network")
        return network
    return "none"


def http_endpoint_ready(probe):
    return probe.returncode == 0 and probe.stdout in ("200", "301", "302", "303", "401", "403")


def public_environment(app):
    entries = values(app)["controllers"][app]["containers"]["app"]["env"]
    config = yaml.safe_load((ROOT / "apps" / app / "configmap.yaml").read_text())
    result = {}
    for entry in entries:
        name = entry["name"]
        if name not in ("PUID", "PGID", "TZ", "DOCKER_MODS", "INSTALL_PACKAGES"):
            raise ValueError("Unexpected environment entry; do not capture arbitrary app environment")
        if "value" in entry:
            result[name] = entry["value"]
        else:
            reference = entry["valueFrom"]["configMapKeyRef"]
            if reference["name"] != config["metadata"]["name"] or reference["key"] not in ("PUID", "PGID", "TZ"):
                raise ValueError("Only public identity/timezone ConfigMap references are supported")
            result[name] = config["data"][reference["key"]]
    return result


def main_command(app, name, volume, token, network, allow_sab_network):
    environment = public_environment(app)
    if app != "sabnzbd" or not allow_sab_network:
        environment.pop("DOCKER_MODS", None)
        environment.pop("INSTALL_PACKAGES", None)
    limits = values(app)["controllers"][app]["containers"]["app"]["resources"]["limits"]
    memory = limits["memory"]
    suffix = memory[-2:]
    if suffix not in ("Ki", "Mi", "Gi"):
        raise ValueError("Unsupported memory quantity")
    memory_bytes = int(memory[:-2]) * {"Ki": 1024, "Mi": 1024**2, "Gi": 1024**3}[suffix]
    cpu = str(limits["cpu"])
    cpus = str(int(cpu[:-1]) / 1000) if cpu.endswith("m") else cpu
    command = [
        "docker", "create", "--platform=linux/amd64", "--name", name, "--label", RUN_LABEL + "=" + token,
        "--network", bootstrap_network(app, allow_sab_network, network),
        "--memory", str(memory_bytes), "--cpus", cpus,
        "--tmpfs", "/config:rw,nosuid,nodev,size=67108864",
        "--volume", str(SHARED) + ":/ca-guard:ro",
    ]
    for key, value in environment.items():
        command += ["--env", key + "=" + str(value)]
    for path in guard.TREES + guard.PACKAGES[app]:
        command += ["--mount", f"type=volume,src={volume},dst=/{path},readonly,volume-subpath={path}"]
    return [*command, image(app)]


def proxy_script(stage, original):
    prelude = "#!/bin/sh\n"
    if stage == "package":
        prelude += (
            "target=false\nfor argument do\n"
            f'  if [ "$argument" = "{PACKAGE_STAGE}" ]; then target=true; fi\n'
            "done\n"
            f'if [ "$target" != true ]; then exec "{original}" "$@"; fi\n'
        )
    prelude += "ffmpeg_add=false\n"
    if stage == "apk":
        # Separate diagnostic classes never replace the strict transaction gate.
        prelude += (
            'if [ "$#" -eq 3 ] && [ "$1" = add ] && [ "$2" = --no-cache ] && [ "$3" = ffmpeg ]; '
            "then ffmpeg_add=true; fi\n"
            "operation=other\n"
            'if [ "$#" -eq 1 ] && [ "$1" = --print-arch ]; then operation=architecture_query\n'
            'elif [ "$ffmpeg_add" = true ]; then operation=ffmpeg_install\n'
            'elif [ "$#" -eq 4 ] && [ "$1" = add ] && [ "$2" = --no-cache ]; then\n'
            'case "$3/$4" in curl/jq|jq/curl) operation=bootstrap_curl_jq;; esac\nfi\n'
            f"""printf '{{"id":%s,"operation":"%s"}}\\n' "$$" "$operation" >> {ACCEPTANCE}/apk-operations.jsonl\n"""
        )
    # Record only fixed stage names, process IDs, a public-package predicate and
    # return codes. Never copy command arguments, output, environment or tokens.
    return (prelude + (
        f"""printf '{{"stage":"{stage}","event":"start","id":%s,"ffmpeg_add":%s}}\\n' "$$" "$ffmpeg_add" >> {ACCEPTANCE}/events.jsonl
"{original}" "$@"
status=$?
printf '{{"stage":"{stage}","event":"exit","id":%s,"ffmpeg_add":%s,"rc":%s}}\\n' "$$" "$ffmpeg_add" "$status" >> {ACCEPTANCE}/events.jsonl
exit "$status"
"""
    )).encode("ascii")


def tar_member(archive_bytes, limit=8 * 1024 * 1024):
    with tarfile.open(fileobj=io.BytesIO(archive_bytes), mode="r:*") as archive:
        members = [member for member in archive if member.isfile()]
        if len(members) != 1 or members[0].size > limit:
            raise ValueError("Expected one bounded public executable")
        return archive.extractfile(members[0]).read(), members[0].mode


def install_sab_observers(name):
    stream, fingerprints = io.BytesIO(), {}
    with tarfile.open(fileobj=stream, mode="w") as archive:
        for destination, (stage, basename) in PROXIES.items():
            copied = subprocess.run(
                ["docker", "cp", "-L", name + ":" + destination, "-"],
                check=True, capture_output=True, timeout=30,
            )
            original, mode = tar_member(copied.stdout)
            for path, content, permissions in (
                (ACCEPTANCE.lstrip("/") + "/" + basename, original, mode),
                (destination.lstrip("/"), proxy_script(stage, ACCEPTANCE + "/" + basename), 0o755),
            ):
                member = tarfile.TarInfo(path)
                member.size, member.mode = len(content), permissions
                member.uid = member.gid = 0
                archive.addfile(member, io.BytesIO(content))
            fingerprints[destination] = hashlib.sha256(
                proxy_script(stage, ACCEPTANCE + "/" + basename)
            ).hexdigest()
    subprocess.run(["docker", "cp", "-", name + ":/"], input=stream.getvalue(),
                   check=True, capture_output=True, timeout=30)
    return fingerprints


def expected_sab_mod():
    environment = public_environment("sabnzbd")
    ref = environment["DOCKER_MODS"]
    prefix = "linuxserver/mods:universal-package-install@sha256:"
    if not ref.startswith(prefix) or not re.fullmatch(r"[a-f0-9]{64}", ref.removeprefix(prefix)) or environment["INSTALL_PACKAGES"] != "ffmpeg":
        raise ValueError("SAB acceptance must replay the existing exact pinned mod and ffmpeg request")
    metadata_ref = "lscr.io/" + ref
    digest = subprocess.run(
        [CRANE, "--platform=linux/amd64", "digest", metadata_ref],
        capture_output=True, text=True, check=True, timeout=120,
    ).stdout.strip()
    if not re.fullmatch(r"sha256:[a-f0-9]{64}", digest):
        raise ValueError("Invalid immutable mod platform digest")
    repository = metadata_ref.split("@", 1)[0].rsplit(":", 1)[0]
    manifest = json.loads(subprocess.run(
        [CRANE, "manifest", repository + "@" + digest],
        capture_output=True, text=True, check=True, timeout=120,
    ).stdout)
    if len(manifest["layers"]) != 1 or not re.fullmatch(r"sha256:[a-f0-9]{64}", manifest["layers"][0]["digest"]):
        raise ValueError("Stock Docker-mod acceptance expects exactly one immutable mod layer")
    return {
        "reference": ref, "manifest": repository + "@" + digest,
        "layer": manifest["layers"][0]["digest"],
        "marker": "/linuxserver.mods.universal-package-install." + ref.removeprefix(prefix),
    }


def validate_bootstrap_events(events):
    active, completed = {}, []
    for entry in events:
        stage, event, identity = entry["stage"], entry["event"], entry["id"]
        expected = {"stage", "event", "id", "ffmpeg_add"} | ({"rc"} if event == "exit" else set())
        if set(entry) != expected or stage not in ("mod", "package", "apk", "ca-update"):
            raise ValueError("Unexpected bootstrap journal schema")
        if type(identity) is not int or identity <= 0 or type(entry["ffmpeg_add"]) is not bool:
            raise ValueError("Invalid bootstrap event metadata")
        if stage != "apk" and entry["ffmpeg_add"]:
            raise ValueError("Only apk may record an ffmpeg transaction")
        key = (stage, identity)
        if event == "start" and key not in active:
            if stage == "package" and not any(e["stage"] == "mod" for e in completed):
                raise ValueError("Package stage preceded successful mod completion")
            if stage == "apk" and entry["ffmpeg_add"] and not any(k[0] == "package" for k in active):
                raise ValueError("ffmpeg installation was not inside the original package stage")
            active[key] = entry
        elif event == "exit" and key in active:
            start = active.pop(key)
            if start["ffmpeg_add"] != entry["ffmpeg_add"] or type(entry["rc"]) is not int or entry["rc"] != 0:
                raise ValueError(f"{stage} bootstrap failed; installer/CA errors cannot be ignored")
            completed.append(entry)
        else:
            raise ValueError("Unpaired bootstrap event")
    if active:
        raise ValueError("Bootstrap stage has not completed")
    for stage in ("mod", "package"):
        if not any(entry["stage"] == stage for entry in completed):
            raise ValueError("Missing successful " + stage + " execution")
    if not any(entry["stage"] == "apk" and entry["ffmpeg_add"] for entry in completed):
        raise ValueError("The exact stock apk add --no-cache ffmpeg transaction was not observed")
    return {"successful_exits": {stage: sum(e["stage"] == stage for e in completed)
                                for stage in ("mod", "package", "apk", "ca-update")}}


def verify_sab_bootstrap(name, mod, fingerprints):
    events_text = subprocess.run(
        ["docker", "exec", name, "cat", ACCEPTANCE + "/events.jsonl"],
        check=True, capture_output=True, text=True, timeout=30,
    ).stdout
    if len(events_text) > 65536:
        raise ValueError("Bootstrap journal exceeded its bound")
    summary = validate_bootstrap_events([json.loads(line) for line in events_text.splitlines()])
    marker = subprocess.run(["docker", "exec", name, "cat", mod["marker"]],
                            check=True, capture_output=True, text=True, timeout=30).stdout.strip()
    if marker != mod["layer"]:
        raise ValueError("Fresh mod installation marker does not match the pinned public mod layer")
    for path, expected in fingerprints.items():
        observed = subprocess.run(["docker", "cp", "-L", name + ":" + path, "-"],
                                  check=True, capture_output=True, timeout=30)
        if hashlib.sha256(tar_member(observed.stdout)[0]).hexdigest() != expected:
            raise ValueError("A vendor update replaced a test observer; exit coverage is not established")
    subprocess.run(["docker", "exec", name, ACCEPTANCE + "/apk", "info", "-e", "ffmpeg"],
                   check=True, capture_output=True, timeout=30)
    for program in ("ffmpeg", "ffprobe"):
        result = subprocess.run(["docker", "exec", name, program, "-version"],
                                check=True, capture_output=True, text=True, timeout=30)
        if not result.stdout.startswith(program + " version "):
            raise ValueError(program + " did not report a runnable installed binary")
    return {**summary, "mod_manifest": mod["manifest"], "mod_layer": mod["layer"],
            "ffmpeg_installed_and_runnable": True, "ffprobe_runnable": True}


def disconnect_bootstrap_network(name, network):
    subprocess.run(["docker", "network", "disconnect", network, name],
                   check=True, capture_output=True, timeout=30)
    networks = json.loads(subprocess.run(
        ["docker", "inspect", "--format", "{{json .NetworkSettings.Networks}}", name],
        check=True, capture_output=True, text=True, timeout=30,
    ).stdout)
    if networks:
        raise ValueError("SAB still has network attachments; refusing trust checks")


def public_bootstrap_journal(data):
    if len(data) > 65536:
        raise ValueError("Public bootstrap journal exceeded its bound")
    events, pending, unmatched, paired = [], {}, [], []
    for line in data.decode("ascii").splitlines():
        entry = json.loads(line)
        if not isinstance(entry, dict):
            raise ValueError("Invalid public bootstrap event")
        stage, event, identity = (entry.get(k) for k in ("stage", "event", "id"))
        keys = {"stage", "event", "id", "ffmpeg_add"} | ({"rc"} if event == "exit" else set())
        if set(entry) != keys or stage not in ("mod", "package", "apk", "ca-update") or event not in ("start", "exit"):
            raise ValueError("Invalid public bootstrap event schema")
        if type(identity) is not int or not 0 < identity < 2**31 or type(entry["ffmpeg_add"]) is not bool:
            raise ValueError("Invalid public bootstrap metadata")
        if stage != "apk" and entry["ffmpeg_add"]:
            raise ValueError("Invalid public package predicate")
        if event == "exit" and (type(entry["rc"]) is not int or not 0 <= entry["rc"] <= 255):
            raise ValueError("Invalid public bootstrap exit code")
        events.append(entry)
        key = (stage, identity)
        if event == "start" and key not in pending:
            pending[key] = entry
        elif event == "exit" and key in pending and pending[key]["ffmpeg_add"] == entry["ffmpeg_add"]:
            pending.pop(key)
            paired.append({"stage": stage, "id": identity, "rc": entry["rc"]})
        else:
            unmatched.append(entry)
    return {"events": events, "paired_exits": paired, "pending_starts": list(pending.values()),
            "unmatched_events": unmatched, "all_events_paired": not pending and not unmatched}


def public_startup_errors(stream):
    # Only fixed error-class identifiers leave this process, never log lines,
    # URLs, arguments, tracebacks, environment or generated authentication data.
    data = stream.read(65537)
    return {"error_classes": sorted(k for k, pattern in STARTUP_ERROR_CLASSES.items()
                                   if re.search(pattern, data[:65536], re.IGNORECASE)),
            "truncated": len(data) > 65536}


def public_apk_operations(data):
    if len(data) > 65536:
        raise ValueError("Public APK operation journal exceeded its bound")
    operations = []
    for line in data.decode("ascii").splitlines():
        entry = json.loads(line)
        if not isinstance(entry, dict) or set(entry) != {"id", "operation"}:
            raise ValueError("Invalid public APK operation schema")
        if type(entry["id"]) is not int or not 0 < entry["id"] < 2**31 or entry["operation"] not in APK_OPERATIONS:
            raise ValueError("Invalid public APK operation")
        operations.append(entry)
    return operations


def public_mod_installation(name, mod):
    if not mod:
        return {"unavailable": "expected_mod_metadata_not_resolved"}
    marker, layer = mod["marker"], mod["layer"]
    if not re.fullmatch(r"/linuxserver\.mods\.universal-package-install\.[a-f0-9]{64}", marker) \
            or not re.fullmatch(r"sha256:[a-f0-9]{64}", layer):
        raise ValueError("Invalid public mod metadata")
    cache = "/modcache" + marker + ".tar.xz"
    command = (
        f'if [ -f "{marker}" ] && [ ! -L "{marker}" ]; then '
        f'printf "present\\n%s\\n" "$(head -c 80 "{marker}")"; else printf "absent\\n\\n"; fi; '
        f'if [ -f "{cache}" ] && [ ! -L "{cache}" ]; then '
        f'printf "present\\n"; sha256sum "{cache}" | cut -d " " -f 1; else printf "absent\\n\\n"; fi'
    )
    result = subprocess.run(["docker", "exec", name, "/bin/sh", "-ec", command],
                            stdout=subprocess.PIPE, stderr=subprocess.DEVNULL, timeout=10)
    if result.returncode:
        return {"unavailable": "docker_command_failed"}
    lines = result.stdout.decode("ascii").splitlines()
    if len(lines) != 4 or lines[0] not in ("present", "absent") or lines[2] not in ("present", "absent"):
        raise ValueError("Invalid public mod installation result")
    marker_valid = re.fullmatch(r"sha256:[a-f0-9]{64}", lines[1]) is not None
    cache_valid = re.fullmatch(r"[a-f0-9]{64}", lines[3]) is not None
    return {
        "regular_marker_present": lines[0] == "present",
        "marker_is_sha256_digest": marker_valid,
        "marker_matches_expected_layer": lines[0] == "present" and marker_valid and lines[1] == layer,
        "regular_cache_present": lines[2] == "present",
        "cache_is_sha256_digest": cache_valid,
        "cache_matches_expected_layer": lines[2] == "present" and cache_valid and "sha256:" + lines[3] == layer,
        "expected_layer": layer,
    }


def collect_public_startup_errors(name):
    with subprocess.Popen(
        [sys.executable, "-B", str(Path(__file__)), "--sanitize-sab-startup-errors"],
        stdin=subprocess.PIPE, stdout=subprocess.PIPE, stderr=subprocess.DEVNULL,
    ) as sanitizer:
        try:
            try:
                # Raw Docker output goes straight into the bounded sanitizer,
                # not a captured parent buffer, console, report or scratch file.
                logs = subprocess.run(["docker", "logs", "--tail", "200", name],
                                      stdout=sanitizer.stdin, stderr=subprocess.STDOUT, timeout=10)
            finally:
                sanitizer.stdin.close()
                sanitizer.stdin = None
            output, _ = sanitizer.communicate(timeout=5)
        except (subprocess.TimeoutExpired, OSError):
            sanitizer.kill()
            sanitizer.communicate()
            raise
        if sanitizer.returncode:
            raise ValueError("Public startup-error sanitizer failed")
    result = json.loads(output)
    if not isinstance(result, dict) or set(result) != {"error_classes", "truncated"}:
        raise ValueError("Invalid public startup-error result")
    if type(result["truncated"]) is not bool or not isinstance(result["error_classes"], list):
        raise ValueError("Invalid public startup-error metadata")
    if any(type(code) is not str or code not in STARTUP_ERROR_CLASSES for code in result["error_classes"]):
        raise ValueError("Unknown public startup-error class")
    return {**result, "docker_logs_command_succeeded": logs.returncode == 0}


def sab_failure_evidence(name, last_probe, mod=None):
    evidence = {"diagnostic_only": True, "root_cause_proven": False}
    if last_probe is not None:
        evidence["last_http_probe"] = {
            "exit_code": last_probe.returncode,
            "http_status": last_probe.stdout if re.fullmatch(r"[0-9]{3}", last_probe.stdout) else "unavailable",
        }
    state_format = (
        '{"status":{{json .State.Status}},"running":{{json .State.Running}},'
        '"exit_code":{{json .State.ExitCode}},"oom_killed":{{json .State.OOMKilled}},'
        '"dead":{{json .State.Dead}}}'
    )
    for source in ("termination_state", "bootstrap_journal", "apk_operations", "mod_installation", "startup_errors"):
        try:
            if source == "mod_installation":
                evidence[source] = public_mod_installation(name, mod)
                continue
            if source == "startup_errors":
                evidence[source] = collect_public_startup_errors(name)
                continue
            command = (
                ["docker", "inspect", "--format", state_format, name] if source == "termination_state"
                else ["docker", "cp", name + ":" + ACCEPTANCE + (
                    "/events.jsonl" if source == "bootstrap_journal" else "/apk-operations.jsonl"
                ), "-"]
            )
            result = subprocess.run(command, stdout=subprocess.PIPE, stderr=subprocess.DEVNULL, timeout=10)
            if result.returncode:
                evidence[source] = {"unavailable": "docker_command_failed"}
                continue
            if source in ("bootstrap_journal", "apk_operations"):
                data = tar_member(result.stdout, limit=65536)[0]
                evidence[source] = public_bootstrap_journal(data) if source == "bootstrap_journal" else public_apk_operations(data)
            else:
                state = json.loads(result.stdout)
                if not isinstance(state, dict) or set(state) != {"status", "running", "exit_code", "oom_killed", "dead"}:
                    raise ValueError("Invalid public container state")
                if state["status"] not in ("created", "running", "paused", "restarting", "removing", "exited", "dead"):
                    raise ValueError("Unknown public container status")
                if any(type(state[k]) is not bool for k in ("running", "oom_killed", "dead")):
                    raise ValueError("Invalid public container state flags")
                if type(state["exit_code"]) is not int or not 0 <= state["exit_code"] <= 255:
                    raise ValueError("Invalid public container exit code")
                evidence[source] = state
        except subprocess.TimeoutExpired:
            evidence[source] = {"unavailable": "diagnostic_timeout"}
        except (OSError, ValueError, UnicodeError, tarfile.TarError):
            evidence[source] = {"unavailable": "invalid_or_unavailable_public_evidence"}
    return evidence


def cleanup_owned(token):
    failures = []
    for kind in ("container", "network", "volume"):
        try:
            listing = subprocess.run(
                ["docker", kind, "ls", "--quiet", "--filter", "label=" + RUN_LABEL + "=" + token]
                + (["--all"] if kind == "container" else []),
                capture_output=True, text=True, timeout=30,
            )
        except subprocess.TimeoutExpired:
            failures.append(kind + " listing timed out")
            continue
        if listing.returncode:
            failures.append(kind + " listing failed")
            continue
        for identity in listing.stdout.splitlines():
            try:
                removal = subprocess.run(
                    ["docker", kind, "rm"] + (["--force", "--volumes"] if kind == "container" else []) + [identity],
                    capture_output=True, timeout=30,
                )
            except subprocess.TimeoutExpired:
                failures.append(kind + " cleanup timed out")
                continue
            if removal.returncode:
                failures.append(kind + " cleanup failed")
    if failures:
        raise RuntimeError("; ".join(failures))


class MediaOverlayWiringTests(unittest.TestCase):
    def test_preparation_metadata_has_portable_policy_and_exact_vendor_image_bindings(self):
        evidence = json.loads((ROOT / ".github" / "security" / "media-runtime-ca-hardening-20261026.json").read_text())
        policy = (ROOT / ".github" / "security" / "emudhra-2026.json").read_text(encoding="utf-8")
        self.assertEqual(hashlib.sha256(policy.encode("utf-8")).hexdigest(),
                         evidence["policy_binding"]["canonical_utf8_lf_sha256"])
        self.assertFalse(evidence["deployment_approved_by_this_report"])
        for app in APPS:
            ref = values(app)["controllers"][app]["containers"]["app"]["image"]
            self.assertEqual(ref["repository"] + "@" + ref["tag"].split("@", 1)[1],
                             evidence["immutable_platform_evidence"][app]["index"])
        acceptance = evidence["sab_complete_mod_acceptance"]
        self.assertEqual(acceptance["status"], "REQUIRED_NOT_RUN")
        self.assertTrue(acceptance["execution_networking_added"])
        self.assertFalse(acceptance["bootstrap_network_default"])
        env = {
            e["name"]: e.get("value")
            for e in values("sabnzbd")["controllers"]["sabnzbd"]["containers"]["app"]["env"]
        }
        for name, value in acceptance["preserve_environment"].items():
            self.assertEqual(env[name], value)

    def test_apps_use_one_shared_guard_and_exact_eleven_policy_keys(self):
        self.assertEqual(guard.policy_keys(SHARED / "blocked-spkis.sha256"), CANONICAL)
        for app in APPS:
            directory = ROOT / "apps" / app
            for name in ("ca-overlay.py", "copy-ca-overlay.sh", "blocked-spkis.sha256"):
                self.assertFalse((directory / name).exists())
                self.assertTrue((SHARED / name).is_file())
                self.assertFalse((SHARED / name).is_symlink())

    def test_kubernetes_init_failure_blocks_unmodified_original_application(self):
        for app in APPS:
            v = values(app)
            controller = v["controllers"][app]
            main = controller["containers"]["app"]
            self.assertNotIn("command", main)
            self.assertNotIn("args", main)
            init = controller["initContainers"]
            self.assertEqual(init["ca-copy"]["command"], ["/bin/sh", "/ca-guard/copy-ca-overlay.sh", app])
            self.assertEqual(init["ca-copy"]["image"], main["image"])
            self.assertEqual(init["ca-filter"]["command"], ["python3", "-B", "/ca-guard/ca-overlay.py"])
            self.assertEqual(init["ca-filter"]["args"], [
                "--root", "/ca-store", "--app", app, "--policy", "/ca-guard/blocked-spkis.sha256",
            ])
            for name in ("ca-copy", "ca-filter"):
                self.assertNotIn("restartPolicy", init[name])
                self.assertTrue(init[name]["securityContext"]["readOnlyRootFilesystem"])
                for volume in ("config", "media"):
                    entry = v["persistence"].get(volume, {})
                    self.assertNotIn("globalMounts", entry)
                    self.assertNotIn(name, entry.get("advancedMounts", {}).get(app, {}))
            self.assertEqual(image(app, "ca-filter"), image("bazarr") if app == "prowlarr" else image(app))

    def test_all_system_trees_and_entire_certifi_packages_are_read_only(self):
        for app in APPS:
            persistence = values(app)["persistence"]
            store = persistence["ca-store"]
            self.assertEqual(store["type"], "emptyDir")
            mounts = store["advancedMounts"][app]["app"]
            expected = set(guard.TREES + guard.PACKAGES[app])
            self.assertEqual({m["subPath"] for m in mounts}, expected)
            for mount in mounts:
                self.assertTrue(mount["readOnly"])
                self.assertEqual(mount["path"], "/" + mount["subPath"])
                self.assertFalse(mount["path"].endswith("cacert.pem"))
            if app == "sabnzbd":
                self.assertEqual(store["advancedMounts"][app]["codec-config"], mounts)

    def test_hashed_configmap_name_is_wired_into_helm_persistence(self):
        shared = yaml.safe_load((SHARED / "kustomization.yaml").read_text())
        generator, = shared["configMapGenerator"]
        self.assertEqual(generator["name"], "ca-guard")
        self.assertCountEqual(generator["files"], ["ca-overlay.py", "copy-ca-overlay.sh", "blocked-spkis.sha256"])
        self.assertNotIn("resources", shared)
        self.assertNotIn("namePrefix", shared)
        self.assertFalse(shared.get("generatorOptions", {}).get("disableNameSuffixHash", False))
        for app in APPS:
            directory = ROOT / "apps" / app
            config = yaml.safe_load((directory / "kustomization.yaml").read_text())
            self.assertIn("ca-guard", config["resources"])
            self.assertNotIn("namePrefix", config)
            nested = yaml.safe_load((directory / "ca-guard" / "kustomization.yaml").read_text())
            self.assertEqual(nested["namePrefix"], app + "-")
            self.assertEqual(nested["resources"], ["../../_shared/media-ca-hardening"])
            reference = json.loads((directory / "kustomize-ca.json").read_text())
            self.assertEqual(reference["nameReference"][0]["fieldSpecs"][0]["path"],
                             "spec/values/persistence/ca-guard/name")
            self.assertEqual(values(app)["persistence"]["ca-guard"]["name"], app + "-ca-guard")


class MediaSabBootstrapHarnessTests(unittest.TestCase):
    @staticmethod
    def events():
        return [
            {"stage": "mod", "event": "start", "id": 1, "ffmpeg_add": False},
            {"stage": "mod", "event": "exit", "id": 1, "ffmpeg_add": False, "rc": 0},
            {"stage": "package", "event": "start", "id": 2, "ffmpeg_add": False},
            {"stage": "apk", "event": "start", "id": 3, "ffmpeg_add": True},
            {"stage": "ca-update", "event": "start", "id": 4, "ffmpeg_add": False},
            {"stage": "ca-update", "event": "exit", "id": 4, "ffmpeg_add": False, "rc": 0},
            {"stage": "apk", "event": "exit", "id": 3, "ffmpeg_add": True, "rc": 0},
            {"stage": "package", "event": "exit", "id": 2, "ffmpeg_add": False, "rc": 0},
        ]

    @staticmethod
    def archive(content):
        stream = io.BytesIO()
        with tarfile.open(fileobj=stream, mode="w") as archive:
            member = tarfile.TarInfo("public-program")
            member.size, member.mode = len(content), 0o755
            archive.addfile(member, io.BytesIO(content))
        return stream.getvalue()

    def test_sab_303_is_ready_but_the_observed_no_ffmpeg_bootstrap_still_fails(self):
        self.assertTrue(http_endpoint_ready(subprocess.CompletedProcess([], 0, "303")))
        for code, status in ((7, "303"), (0, "000"), (0, "304"), (0, "500")):
            self.assertFalse(http_endpoint_ready(subprocess.CompletedProcess([], code, status)))
        events = [
            {"stage": "mod", "event": "start", "id": 26, "ffmpeg_add": False},
            {"stage": "apk", "event": "start", "id": 54, "ffmpeg_add": False},
            {"stage": "apk", "event": "exit", "id": 54, "ffmpeg_add": False, "rc": 0},
            {"stage": "mod", "event": "exit", "id": 26, "ffmpeg_add": False, "rc": 0},
            {"stage": "package", "event": "start", "id": 169, "ffmpeg_add": False},
            {"stage": "package", "event": "exit", "id": 169, "ffmpeg_add": False, "rc": 0},
        ]
        self.assertTrue(public_bootstrap_journal("\n".join(json.dumps(e) for e in events).encode())["all_events_paired"])
        with self.assertRaisesRegex(ValueError, "exact stock apk add --no-cache ffmpeg transaction"):
            validate_bootstrap_events(events)

    def test_apk_observer_preserves_command_substitution_output_arguments_and_exit_status(self):
        bash = Path("/bin/bash") if sys.platform.startswith("linux") else Path(r"C:\Program Files\Git\bin\bash.exe")
        if not bash.is_file():
            self.skipTest("Synthetic shell fixture requires Bash; no image execution")
        directory = ROOT / (".media-apk-observer-" + uuid.uuid4().hex)
        directory.mkdir()
        self.addCleanup(shutil.rmtree, directory)
        original, proxy = directory / "original-apk", directory / "observed-apk"
        original.write_bytes(
            b'#!/bin/sh\ncase "$*" in\n'
            b'"--print-arch") printf "x86_64\\n";;\n'
            b'"add --no-cache ffmpeg") printf "ffmpeg fixture\\n";;\n'
            b'"add --no-cache curl jq") printf "tools fixture\\n";;\n'
            b'*) exit 17;;\nesac\n'
        )
        original.chmod(0o755)
        with mock.patch.dict(globals(), {"ACCEPTANCE": directory.as_posix()}):
            proxy.write_bytes(proxy_script("apk", original.as_posix()))
        proxy.chmod(0o755)
        arguments = (["--print-arch"], ["add", "--no-cache", "ffmpeg"],
                     ["add", "--no-cache", "curl", "jq"], ["unsupported-public-fixture"])
        for args in arguments:
            direct = subprocess.run([str(bash), str(original), *args], capture_output=True, timeout=10)
            observed = subprocess.run([str(bash), str(proxy), *args], capture_output=True, timeout=10)
            self.assertEqual((observed.returncode, observed.stdout, observed.stderr),
                             (direct.returncode, direct.stdout, direct.stderr))
        subprocess.run([
            str(bash), "-c", 'architecture="$("$1" --print-arch)"; test "$architecture" = x86_64',
            "public-fixture", proxy.as_posix(),
        ], check=True, capture_output=True, timeout=10)
        operations = public_apk_operations((directory / "apk-operations.jsonl").read_bytes())
        self.assertEqual([e["operation"] for e in operations],
                         ["architecture_query", "ffmpeg_install", "bootstrap_curl_jq", "other", "architecture_query"])
        self.assertNotIn(b"unsupported-public-fixture", (directory / "apk-operations.jsonl").read_bytes())

    def test_only_explicitly_authorized_sab_main_gets_disposable_network(self):
        for app in APPS:
            for allowed in (False, True):
                command = main_command(app, "test-main", "test-volume", "test-token", "test-bootstrap", allowed)
                expected = "test-bootstrap" if app == "sabnzbd" and allowed else "none"
                self.assertEqual(command[command.index("--network") + 1], expected)
                self.assertEqual(command[-1], image(app))
                self.assertIn("--platform=linux/amd64", command)
                for forbidden in ("--privileged", "--cap-add", "--cap-drop", "--entrypoint", "--publish", "--user", "--device"):
                    self.assertNotIn(forbidden, command)
                self.assertNotIn("/var/run/docker.sock", " ".join(command))
                self.assertNotIn("host", command)
                observed = dict(command[i + 1].split("=", 1) for i, value in enumerate(command) if value == "--env")
                for key in ("PUID", "PGID", "TZ"):
                    self.assertEqual(observed[key], public_environment(app)[key])
                self.assertEqual("DOCKER_MODS" in observed, app == "sabnzbd" and allowed)
                self.assertEqual("INSTALL_PACKAGES" in observed, app == "sabnzbd" and allowed)
                if app == "sabnzbd" and allowed:
                    self.assertEqual(observed["DOCKER_MODS"], public_environment(app)["DOCKER_MODS"])
                    self.assertEqual(observed["INSTALL_PACKAGES"], "ffmpeg")
                    self.assertEqual(command[command.index("--memory") + 1], "2147483648")
                    self.assertEqual(float(command[command.index("--cpus") + 1]), 2)
        with self.assertRaises(ValueError):
            bootstrap_network("sabnzbd", True, "host")

    def test_bootstrap_success_requires_mod_package_and_exact_ffmpeg_transaction(self):
        summary = validate_bootstrap_events(self.events())
        self.assertEqual(summary["successful_exits"], {"mod": 1, "package": 1, "apk": 1, "ca-update": 1})
        for index in (1, 5, 6, 7):
            events = self.events()
            events[index]["rc"] = 1
            with self.subTest(stage=events[index]["stage"]), self.assertRaises(ValueError):
                validate_bootstrap_events(events)
        for stage in ("mod", "package", "apk"):
            with self.subTest(missing=stage), self.assertRaises(ValueError):
                validate_bootstrap_events([e for e in self.events() if e["stage"] != stage])
        with self.assertRaises(ValueError):
            validate_bootstrap_events(self.events()[:-1])
        events = self.events()
        events[3]["ffmpeg_add"] = events[6]["ffmpeg_add"] = False
        with self.assertRaises(ValueError):
            validate_bootstrap_events(events)
        events = self.events()
        events[0]["arguments"] = "must never be captured"
        with self.assertRaises(ValueError):
            validate_bootstrap_events(events)

    def test_observers_copy_public_originals_and_preserve_exit_and_nonpackage_exec(self):
        original = b"#!/bin/sh\nexit 0\n"
        calls = []

        def run(command, **kwargs):
            calls.append((command, kwargs))
            return subprocess.CompletedProcess(command, 0, self.archive(original) if "-L" in command else b"", b"")

        with mock.patch.object(subprocess, "run", side_effect=run):
            fingerprints = install_sab_observers("owned-main")
        self.assertEqual(set(fingerprints), set(PROXIES))
        with tarfile.open(fileobj=io.BytesIO(calls[-1][1]["input"]), mode="r:") as archive:
            self.assertEqual(len(archive.getmembers()), 8)
            for destination, (stage, basename) in PROXIES.items():
                self.assertEqual(archive.extractfile(ACCEPTANCE.lstrip("/") + "/" + basename).read(), original)
                script = archive.extractfile(destination.lstrip("/")).read().decode()
                self.assertIn('status=$?', script)
                self.assertTrue(script.endswith('exit "$status"\n'))
                self.assertNotIn("set -e", script)
                self.assertNotIn("env >", script)
                if stage == "package":
                    self.assertIn('then exec "' + ACCEPTANCE + '/with-contenv" "$@"; fi', script)

    def test_exact_pinned_mod_metadata_resolves_platform_and_binds_one_layer(self):
        digest, layer = "sha256:" + "a" * 64, "sha256:" + "b" * 64
        with mock.patch.object(subprocess, "run", side_effect=[
            subprocess.CompletedProcess([], 0, digest + "\n", ""),
            subprocess.CompletedProcess([], 0, json.dumps({"layers": [{"digest": layer}]}), ""),
        ]) as run:
            result = expected_sab_mod()
        self.assertEqual(result["layer"], layer)
        self.assertTrue(result["manifest"].startswith("lscr.io/linuxserver/mods@sha256:"))
        self.assertIn("--platform=linux/amd64", run.call_args_list[0].args[0])
        self.assertIn(public_environment("sabnzbd")["DOCKER_MODS"], run.call_args_list[0].args[0][-1])

    def test_disconnect_is_verified_and_residual_endpoints_fail_closed(self):
        for endpoints in ({}, {"still-connected": {}}):
            with self.subTest(endpoints=endpoints), mock.patch.object(subprocess, "run", side_effect=[
                subprocess.CompletedProcess([], 0, "", ""),
                subprocess.CompletedProcess([], 0, json.dumps(endpoints), ""),
            ]) as run:
                if endpoints:
                    with self.assertRaises(ValueError):
                        disconnect_bootstrap_network("owned-main", "owned-bootstrap")
                else:
                    disconnect_bootstrap_network("owned-main", "owned-bootstrap")
                self.assertEqual(run.call_args_list[0].args[0],
                                 ["docker", "network", "disconnect", "owned-bootstrap", "owned-main"])

    def test_bootstrap_rejects_changed_marker_observers_missing_package_or_nonworking_binaries(self):
        wrapper = b"public-observer"
        fingerprints = {"/docker-mods": hashlib.sha256(wrapper).hexdigest()}
        mod = {"marker": "/public-marker", "layer": "public-layer", "manifest": "public-manifest"}
        for failure in ("marker", "observer", "apk", "ffmpeg", "ffprobe"):
            def run(command, **kwargs):
                if command[-1] == ACCEPTANCE + "/events.jsonl":
                    output = "\n".join(json.dumps(e) for e in self.events())
                elif command[-1] == mod["marker"]:
                    output = "changed" if failure == "marker" else mod["layer"]
                elif command[:2] == ["docker", "cp"]:
                    output = self.archive(b"changed" if failure == "observer" else wrapper)
                elif command[-3:] == ["info", "-e", "ffmpeg"]:
                    if failure == "apk":
                        raise subprocess.CalledProcessError(1, command)
                    output = ""
                else:
                    program = command[-2]
                    output = "not runnable" if failure == program else program + " version fixture"
                return subprocess.CompletedProcess(command, 0, output, "")

            with self.subTest(failure=failure), mock.patch.object(subprocess, "run", side_effect=run), \
                    self.assertRaises((ValueError, subprocess.CalledProcessError)):
                verify_sab_bootstrap("owned-main", mod, fingerprints)

    def test_cleanup_attempts_all_owned_kinds_and_reports_failures(self):
        calls = []

        def run(command, **kwargs):
            calls.append(command)
            if command[2] == "ls":
                self.assertIn("label=" + RUN_LABEL + "=unique-token", command)
                return subprocess.CompletedProcess(command, 0, "owned-" + command[1] + "\n", "")
            return subprocess.CompletedProcess(command, 1 if command[1] == "container" else 0, b"", b"")

        with mock.patch.object(subprocess, "run", side_effect=run), self.assertRaises(RuntimeError):
            cleanup_owned("unique-token")
        self.assertEqual([c[1] for c in calls if c[2] == "rm"], ["container", "network", "volume"])
        self.assertIn("--volumes", next(c for c in calls if c[1:3] == ["container", "rm"]))

    def test_cleanup_timeouts_do_not_skip_remaining_owned_resource_kinds(self):
        calls = []

        def run(command, **kwargs):
            calls.append(command)
            if command[1:3] in (["container", "ls"], ["network", "rm"]):
                raise subprocess.TimeoutExpired(command, 30)
            return subprocess.CompletedProcess(command, 0, "owned-resource\n" if command[2] == "ls" else "", "")

        with mock.patch.object(subprocess, "run", side_effect=run), self.assertRaises(RuntimeError):
            cleanup_owned("unique-token")
        self.assertTrue(any(c[1:3] == ["volume", "rm"] for c in calls))

    def test_sab_network_opt_in_requires_docker_instead_of_silently_ignoring_flag(self):
        result = subprocess.run([
            sys.executable, "-B", str(Path(__file__)), "--allow-sab-bootstrap-network",
        ], capture_output=True, text=True, timeout=30)
        self.assertNotEqual(result.returncode, 0)
        self.assertIn("requires --docker", result.stderr)

    def test_orchestrator_disconnects_before_all_trust_checks_even_with_vendor_init(self):
        calls, detached = [], False

        def run(command, **kwargs):
            nonlocal detached
            calls.append(command)
            output, error, code = "", "", 0
            if command[:2] == ["docker", "version"]:
                output = "linux/amd64"
            elif "media-ca-filter-" in " ".join(command) and command[:2] == ["docker", "run"]:
                output = '{"removed_certificate_copies": 1}'
            elif command[:3] == ["docker", "network", "disconnect"]:
                detached = True
            elif command[:2] == ["docker", "inspect"]:
                output = "{}" if detached else '{"bootstrap":{}}'
            elif "curl" in command:
                output = "303"
            elif command[:2] == ["docker", "exec"] and "/bin/sh" in command:
                self.assertTrue(detached)
                code, error = 1, "Read-only file system"
            elif command[:2] == ["docker", "exec"] and "python3" in command:
                self.assertTrue(detached)
                code = 1 if "import os; os.rename(" in " ".join(command) else 0
            elif "--verify-only" in command:
                self.assertTrue(detached)
            return subprocess.CompletedProcess(command, code, output, error)

        with mock.patch.dict(globals(), {"DOCKER": True, "ALLOW_SAB_BOOTSTRAP_NETWORK": True, "APPS": ("sabnzbd",)}), \
                mock.patch.object(subprocess, "run", side_effect=run), \
                mock.patch.object(time, "monotonic", side_effect=[0, 0]), \
                mock.patch.object(time, "sleep"), \
                contextlib.redirect_stdout(io.StringIO()), \
                mock.patch.dict(globals(), {
                    "expected_sab_mod": lambda: {"manifest": "public-manifest", "layer": "public-layer"},
                    "install_sab_observers": lambda name: {},
                    "verify_sab_bootstrap": lambda name, mod, observers: {"successful_exits": "mock-unit-only"},
                }):
            MediaRealVendorTests("test_real_linux_docker_init_and_readonly_overlay").test_real_linux_docker_init_and_readonly_overlay()
        main = next(c for c in calls if c[:2] == ["docker", "create"])
        self.assertTrue(main[main.index("--network") + 1].startswith("media-ca-bootstrap-"))
        for helper in (c for c in calls if c[:2] == ["docker", "run"]):
            self.assertEqual(helper[helper.index("--network") + 1], "none")
            self.assertIn("--platform=linux/amd64", helper)
        preflight = next(c for c in calls if any("media-ca-preflight-" in a for a in c))
        self.assertIn("command -v ffmpeg", preflight[-1])
        self.assertIn("command -v ffprobe", preflight[-1])
        self.assertTrue(detached)
        self.assertEqual({c[1] for c in calls if len(c) > 2 and c[2] == "ls"},
                         {"container", "network", "volume"})


class MediaSabFailureDiagnosticsTests(unittest.TestCase):
    def test_journal_preserves_failed_exits_and_unfinished_stages_without_acceptance(self):
        events = MediaSabBootstrapHarnessTests.events()[:-1]
        events[5]["rc"] = 1
        journal = public_bootstrap_journal("\n".join(json.dumps(e) for e in events).encode())
        self.assertEqual(journal["events"], events)
        self.assertFalse(journal["all_events_paired"])
        self.assertEqual(journal["pending_starts"], [events[2]])
        self.assertIn({"stage": "ca-update", "id": 4, "rc": 1}, journal["paired_exits"])
        with self.assertRaises(ValueError):
            validate_bootstrap_events(events)
        for unsafe in (
            b'{"stage":"mod","event":"start","id":1,"ffmpeg_add":false,"token":"canary-secret"}',
            b'{"stage":"canary-secret","event":"start","id":1,"ffmpeg_add":false}',
            b"canary-secret",
            b"x" * 65537,
        ):
            with self.subTest(payload_size=len(unsafe)), self.assertRaises(ValueError):
                public_bootstrap_journal(unsafe)

    def test_sanitizer_cli_never_emits_log_lines_authentication_data_urls_or_environment(self):
        canary = "canary-generated-auth-do-not-emit"
        fixture = (
            f"SAB API key: {canary}\nPASSWORD={canary}\n"  # gitleaks:allow
            f"WARNING: fetching https://user:{canary}@repository.invalid/main: error\n"
            f"update-ca-certificates: Read-only file system /etc/ssl/certs/{canary}\n"
            "s6-rc: warning: unable to start service init-mods-package-install\n"
        ).encode()
        result = subprocess.run([
            sys.executable, "-B", str(Path(__file__)), "--sanitize-sab-startup-errors",
        ], input=fixture, capture_output=True, timeout=30, check=True)
        self.assertEqual(json.loads(result.stdout), {
            "error_classes": ["apk_repository_fetch_failed", "read_only_filesystem", "s6_service_start_failed"],
            "truncated": False,
        })
        self.assertEqual(result.stderr, b"")
        for forbidden in (canary.encode(), b"https://", b"PASSWORD", b"API key", b"/etc/ssl"):
            self.assertNotIn(forbidden, result.stdout)
        truncated = public_startup_errors(io.BytesIO(b"x" * 65537 + b"Permission denied"))
        self.assertEqual(truncated, {"error_classes": [], "truncated": True})

    def test_only_sanitized_output_is_captured_by_parent_and_raw_stream_is_not_printed(self):
        worker = mock.MagicMock()
        input_pipe = worker.stdin
        worker.returncode = 0
        worker.communicate.return_value = (b'{"error_classes":["permission_denied"],"truncated":false}', None)
        worker.__enter__.return_value = worker
        with mock.patch.object(subprocess, "Popen", return_value=worker), \
                mock.patch.object(subprocess, "run", return_value=subprocess.CompletedProcess([], 0)) as run:
            result = collect_public_startup_errors("owned-main")
        self.assertEqual(run.call_args.args[0], ["docker", "logs", "--tail", "200", "owned-main"])
        self.assertIs(run.call_args.kwargs["stdout"], input_pipe)
        self.assertIs(run.call_args.kwargs["stderr"], subprocess.STDOUT)
        self.assertNotIn("capture_output", run.call_args.kwargs)
        input_pipe.close.assert_called_once()
        self.assertTrue(result["docker_logs_command_succeeded"])
        with mock.patch.object(subprocess, "Popen", return_value=worker), \
                mock.patch.object(subprocess, "run", side_effect=subprocess.TimeoutExpired("docker", 10)), \
                self.assertRaises(subprocess.TimeoutExpired):
            worker.stdin = mock.Mock()
            collect_public_startup_errors("owned-main")
        worker.kill.assert_called_once()

    def test_stopped_container_evidence_uses_only_public_state_and_cp_not_exec(self):
        state = {"status": "exited", "running": False, "exit_code": 111, "oom_killed": False, "dead": False}
        events = MediaSabBootstrapHarnessTests.events()
        events[6]["rc"] = 2
        archive = MediaSabBootstrapHarnessTests.archive("\n".join(json.dumps(e) for e in events).encode())
        probe = subprocess.CompletedProcess([], 7, "generated-auth-canary", "never-print-stderr")
        with mock.patch.object(subprocess, "run", side_effect=[
            subprocess.CompletedProcess([], 0, json.dumps(state).encode()),
            subprocess.CompletedProcess([], 0, archive),
            subprocess.CompletedProcess([], 0, MediaSabBootstrapHarnessTests.archive(
                b'{"id":54,"operation":"architecture_query"}\n')),
        ]) as run, mock.patch.dict(globals(), {
            "collect_public_startup_errors": lambda name: {"error_classes": ["read_only_filesystem"]},
        }):
            evidence = sab_failure_evidence("owned-main", probe)
        self.assertEqual(evidence["termination_state"], state)
        self.assertEqual(evidence["last_http_probe"], {"exit_code": 7, "http_status": "unavailable"})
        self.assertFalse(evidence["root_cause_proven"])
        self.assertEqual(evidence["bootstrap_journal"]["events"], events)
        self.assertEqual(evidence["apk_operations"], [{"id": 54, "operation": "architecture_query"}])
        self.assertEqual(run.call_args_list[1].args[0],
                         ["docker", "cp", "owned-main:" + ACCEPTANCE + "/events.jsonl", "-"])
        self.assertNotIn("generated-auth-canary", json.dumps(evidence))
        inspect = run.call_args_list[0].args[0]
        for forbidden in (".Config", ".Env", ".Error", ".Health"):
            self.assertNotIn(forbidden, inspect[3])

    def test_diagnostic_failures_are_explicit_and_never_echo_malformed_data_or_skip_other_sources(self):
        for failure in ("malformed", "timeout"):
            with self.subTest(failure=failure), mock.patch.object(subprocess, "run", side_effect=(
                [subprocess.CompletedProcess([], 0, b'{"token":"canary-secret"}'),
                 subprocess.CompletedProcess([], 0, MediaSabBootstrapHarnessTests.archive(b"canary-secret")),
                 subprocess.CompletedProcess([], 0, MediaSabBootstrapHarnessTests.archive(b"canary-secret"))]
                if failure == "malformed"
                else [subprocess.TimeoutExpired("docker", 10)] * 3
            )), mock.patch.dict(globals(), {
                "collect_public_startup_errors": mock.Mock(side_effect=OSError("canary-secret")),
            }):
                evidence = sab_failure_evidence("owned-main", None)
            for source in ("termination_state", "bootstrap_journal", "apk_operations", "mod_installation", "startup_errors"):
                self.assertIn("unavailable", evidence[source])
            self.assertNotIn("canary-secret", json.dumps(evidence))

    def test_mod_marker_and_cached_layer_evidence_never_echo_invalid_marker_contents(self):
        mod = {"marker": "/linuxserver.mods.universal-package-install." + "a" * 64, "layer": "sha256:" + "b" * 64}
        for marker, cache, expected in (("", "", False), (mod["layer"], "b" * 64, True),
                                         ("private-canary-never-emit", "c" * 64, False)):
            output = (("present" if marker else "absent") + "\n" + marker + "\n"
                      + ("present" if cache else "absent") + "\n" + cache + "\n").encode()
            with self.subTest(matches=expected), mock.patch.object(subprocess, "run", return_value=
                    subprocess.CompletedProcess([], 0, output)) as run:
                evidence = public_mod_installation("owned-main", mod)
            self.assertEqual(evidence["marker_matches_expected_layer"], expected)
            self.assertEqual(evidence["cache_matches_expected_layer"], expected)
            self.assertNotIn("private-canary-never-emit", json.dumps(evidence))
            self.assertIn("head -c 80", run.call_args.args[0][-1])
            self.assertIn('[ ! -L "', run.call_args.args[0][-1])
        for bad in (b'{"id":54,"operation":"private-canary"}',
                    b'{"id":54,"operation":"architecture_query","arguments":"private-canary"}'):
            with self.assertRaises(ValueError):
                public_apk_operations(bad)

    def test_captured_vendor_skip_and_executable_errors_emit_only_fixed_classifications(self):
        fixture = (
            b"/media-ca-acceptance/docker-mods: line 177: jq: command not found\n"
            b"[mod-init] (ERROR) OFFLINE: private-canary not found in modcache, skipping\n"
            b"[mod-init] (ERROR) No manifest available for arch amd64, cannot fetch mod\n"
            b"[mod-init] (ERROR) Invalid tarball for private-canary, skipping\n"
        )
        evidence = public_startup_errors(io.BytesIO(fixture))
        for code in ("mod_jq_unavailable", "mod_offline_cache_missing", "mod_manifest_unavailable", "mod_invalid_archive"):
            self.assertIn(code, evidence["error_classes"])
        self.assertNotIn("private-canary", json.dumps(evidence))

    def test_readiness_failure_collects_before_cleanup_without_weakening_failure_or_running_trust_checks(self):
        order, calls = [], []

        def run(command, **kwargs):
            calls.append(command)
            output, code = "", 0
            if command[:2] == ["docker", "version"]:
                output = "linux/amd64"
            elif any("media-ca-filter-" in a for a in command):
                output = '{"removed_certificate_copies":1}'
            elif "curl" in command:
                output, code = "000", 7
            return subprocess.CompletedProcess(command, code, output, "")

        def diagnostics(name, probe, mod=None):
            order.append("diagnostics")
            self.assertEqual(probe.stdout, "000")
            return {"diagnostic_only": True, "root_cause_proven": False}

        captured = io.StringIO()
        with mock.patch.dict(globals(), {
            "DOCKER": True, "ALLOW_SAB_BOOTSTRAP_NETWORK": True, "APPS": ("sabnzbd",),
            "expected_sab_mod": lambda: {}, "install_sab_observers": lambda name: {},
            "sab_failure_evidence": diagnostics, "cleanup_owned": lambda token: order.append("cleanup"),
        }), mock.patch.object(subprocess, "run", side_effect=run), \
                mock.patch.object(time, "monotonic", side_effect=[0, 0, 601]), \
                mock.patch.object(time, "sleep"), contextlib.redirect_stdout(captured), \
                self.assertRaisesRegex(AssertionError, "original /init did not reach"):
            MediaRealVendorTests("test_real_linux_docker_init_and_readonly_overlay").test_real_linux_docker_init_and_readonly_overlay()
        self.assertEqual(order, ["diagnostics", "cleanup"])
        self.assertIn('"root_cause_proven": false', captured.getvalue())
        self.assertNotIn("accepted", captured.getvalue())
        self.assertFalse(any("--verify-only" in c or ".ca-write-probe" in " ".join(c) for c in calls))
        self.assertFalse(any(c[:3] == ["docker", "network", "disconnect"] for c in calls))


class MediaOverlayFilterTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.keys, cls.pem, cls.spki = [], [], []
        for index in range(11):
            key = rsa.generate_private_key(public_exponent=65537, key_size=2048) if index == 0 else ec.generate_private_key(ec.SECP256R1())
            subject = x509.Name([x509.NameAttribute(NameOID.COMMON_NAME, "Same fixture subject")])
            cert = (
                x509.CertificateBuilder().subject_name(subject).issuer_name(subject)
                .public_key(key.public_key()).serial_number(x509.random_serial_number())
                .not_valid_before(datetime.datetime(2025, 1, 1, tzinfo=datetime.timezone.utc))
                .not_valid_after(datetime.datetime(2035, 1, 1, tzinfo=datetime.timezone.utc))
                .add_extension(x509.BasicConstraints(ca=True, path_length=None), critical=True)
                .add_extension(x509.SubjectKeyIdentifier.from_public_key(key.public_key()), critical=False)
                .add_extension(x509.AuthorityKeyIdentifier.from_issuer_public_key(key.public_key()), critical=False)
                .add_extension(x509.SubjectAlternativeName([x509.DNSName("localhost")]), critical=False)
                .sign(key, hashes.SHA256())
            )
            cls.keys.append(key)
            cls.pem.append(cert.public_bytes(serialization.Encoding.PEM))
            cls.spki.append(hashlib.sha256(key.public_key().public_bytes(
                serialization.Encoding.DER, serialization.PublicFormat.SubjectPublicKeyInfo,
            )).hexdigest().upper())

    def setUp(self):
        self.base = ROOT / (".media-ca-test-" + uuid.uuid4().hex)
        self.addCleanup(shutil.rmtree, self.base)
        self.root = self.base / "snapshot"
        for directory in guard.TREES + guard.PACKAGES["bazarr"] + guard.PACKAGES["sabnzbd"]:
            (self.root / directory).mkdir(parents=True, exist_ok=True)
        self.bundle = self.root / "etc" / "ssl" / "certs" / "ca-certificates.crt"
        self.bundle.write_bytes(b"".join(self.pem))
        for app in ("bazarr", "sabnzbd"):
            for directory in guard.PACKAGES[app]:
                (self.root / directory / "cacert.pem").write_bytes(b"".join(self.pem))
        self.sources = self.root / "usr" / "share" / "ca-certificates"
        for index, cert in enumerate(self.pem):
            (self.sources / f"root-{index}.crt").write_bytes(cert)
        self.denied = {self.spki[0]}

    def test_stdlib_spki_der_parser_matches_independent_crypto_for_rsa_and_ec(self):
        for pem, expected in zip(self.pem, self.spki, strict=True):
            der = x509.load_pem_x509_certificate(pem).public_bytes(serialization.Encoding.DER)
            self.assertEqual(guard.spki_digest(der), expected)
            self.assertEqual(guard.certificates(pem)[0][1], expected)
        for broken in (b"", b"\x30\x80", b"\x30\x01\x00", b"\x30\x81\x01\x00"):
            with self.assertRaises((ValueError, IndexError)):
                guard.spki_digest(broken)

    def test_removes_exact_key_from_os_sources_bundle_and_application_stores(self):
        for app in ("bazarr", "sabnzbd"):
            result = guard.harden(self.root, app, self.denied)
            self.assertIn("not certified", result["coverage"])
            self.assertFalse((self.sources / "root-0.crt").exists())
            for path in [self.bundle] + [self.root / d / "cacert.pem" for d in guard.PACKAGES[app]]:
                certs = guard.certificates(path.read_bytes())
                self.assertEqual({key for _, key, _ in certs}, set(self.spki[1:]))
            guard.harden(self.root, app, self.denied, verify_only=True)
        for index in range(1, 11):
            self.assertEqual((self.sources / f"root-{index}.crt").read_bytes(), self.pem[index])

    def test_hash_collision_sequence_is_compacted_without_losing_an_allowed_ca(self):
        directory = self.bundle.parent
        (directory / "deadbeef.0").write_bytes(self.pem[0])
        (directory / "deadbeef.1").write_bytes(self.pem[1])
        guard.harden(self.root, "prowlarr", self.denied)
        self.assertEqual((directory / "deadbeef.0").read_bytes(), self.pem[1])
        self.assertFalse((directory / "deadbeef.1").exists())

    @unittest.skipUnless(sys.platform.startswith("linux"), "Linux absolute CA-directory aliases")
    def test_absolute_hash_aliases_are_removed_and_compacted_inside_snapshot(self):
        directory = self.bundle.parent
        (directory / "deadbeef.0").symlink_to("/usr/share/ca-certificates/root-0.crt")
        (directory / "deadbeef.1").symlink_to("/usr/share/ca-certificates/root-1.crt")
        guard.harden(self.root, "prowlarr", self.denied)
        self.assertEqual((directory / "deadbeef.0").readlink().as_posix(), "/usr/share/ca-certificates/root-1.crt")
        self.assertFalse((directory / "deadbeef.1").is_symlink())
        outside = self.base / "outside.crt"
        outside.write_bytes(self.pem[0])
        (self.sources / "escape.crt").symlink_to(outside)
        with self.assertRaises(ValueError):
            guard.harden(self.root, "prowlarr", self.denied)
        self.assertEqual(outside.read_bytes(), self.pem[0])

    def test_bad_policy_missing_malformed_or_partial_bundles_fail_closed(self):
        policy = self.base / "policy"
        for content in ("", "bad\n", self.spki[0] + "\n", (self.spki[0] + "\n") * 11):
            policy.write_text(content)
            with self.assertRaises(ValueError):
                guard.policy_keys(policy)
        policy.write_bytes(("\r\n".join([self.spki[0]] + [f"{n:064X}" for n in range(10)]) + "\r\n").encode())
        self.assertEqual(len(guard.policy_keys(policy)), 11)
        malformed_key_header = b"-----BEGIN " + b"PRIVATE KEY-----"
        for content in (b"", self.pem[0], b"-----BEGIN CERTIFICATE-----\nbroken\n", self.pem[0] + malformed_key_header):
            self.bundle.write_bytes(content)
            with self.assertRaises((ValueError, ssl.SSLError)):
                guard.harden(self.root, "prowlarr", self.denied)
        self.bundle.unlink()
        with self.assertRaises(ValueError):
            guard.harden(self.root, "prowlarr", self.denied)

    def test_missing_application_store_and_cli_policy_failure_cannot_report_success(self):
        (self.root / guard.PACKAGES["bazarr"][0] / "cacert.pem").unlink()
        with self.assertRaises(ValueError):
            guard.harden(self.root, "bazarr", self.denied)
        policy = self.base / "invalid-policy"
        policy.write_text("invalid\n")
        original = self.bundle.read_bytes()
        result = subprocess.run([
            sys.executable, "-B", str(SHARED / "ca-overlay.py"),
            "--root", str(self.root), "--app", "prowlarr", "--policy", str(policy),
        ], capture_output=True, text=True, timeout=30)
        self.assertNotEqual(result.returncode, 0)
        self.assertEqual(result.stdout, "")
        self.assertEqual(self.bundle.read_bytes(), original)

    def handshake(self, index, bundle):
        cert, key = self.base / "tls-cert.pem", self.base / "tls-key.pem"
        cert.write_bytes(self.pem[index])
        key.write_bytes(self.keys[index].private_bytes(
            serialization.Encoding.PEM, serialization.PrivateFormat.PKCS8, serialization.NoEncryption(),
        ))
        server_context = ssl.SSLContext(ssl.PROTOCOL_TLS_SERVER)
        server_context.load_cert_chain(cert, key)
        client_context = ssl.SSLContext(ssl.PROTOCOL_TLS_CLIENT)
        client_context.load_verify_locations(cafile=str(bundle))
        cin, cout, sin, sout = (ssl.MemoryBIO() for _ in range(4))
        client = client_context.wrap_bio(cin, cout, server_hostname="localhost")
        server = server_context.wrap_bio(sin, sout, server_side=True)
        done = set()
        for _ in range(20):
            for name, endpoint in (("client", client), ("server", server)):
                try:
                    endpoint.do_handshake()
                    done.add(name)
                except ssl.SSLWantReadError:
                    pass
            if cout.pending:
                sin.write(cout.read())
            if sout.pending:
                cin.write(sout.read())
            if len(done) == 2:
                return
        self.fail("TLS memory-BIO handshake failed to converge")

    def test_native_python_tls_positive_control_survives_and_denied_fixture_rejects(self):
        self.handshake(0, self.bundle)
        guard.harden(self.root, "prowlarr", self.denied)
        self.handshake(1, self.bundle)
        with self.assertRaises(ssl.SSLCertVerificationError):
            self.handshake(0, self.bundle)


class MediaRealVendorTests(unittest.TestCase):
    def test_registry_public_certificates_match_independent_spki_hashes(self):
        if not REGISTRY:
            self.skipTest("Explicit --registry required; no image execution is implied")
        for app in APPS:
            with self.subTest(app=app):
                log = ROOT / ("media-ca-export-" + uuid.uuid4().hex + ".log")
                observed, matches = set(), 0
                try:
                    with log.open("w+b") as errors:
                        process = subprocess.Popen([CRANE, "--platform=linux/amd64", "export", image(app), "-"],
                                                   stdout=subprocess.PIPE, stderr=errors)
                        try:
                            with tarfile.open(fileobj=process.stdout, mode="r|") as archive:
                                for member in archive:
                                    name = member.name.removeprefix("./")
                                    if not member.isfile() or not (
                                        name.endswith("/cacert.pem")
                                        or name == "etc/ssl/certs/ca-certificates.crt"
                                        or name.startswith(("usr/share/ca-certificates/", "usr/local/share/ca-certificates/"))
                                    ):
                                        continue
                                    data = archive.extractfile(member).read()
                                    certs = guard.certificates(data)
                                    independent = x509.load_pem_x509_certificates(data)
                                    self.assertEqual(len(certs), len(independent))
                                    for (_, key, _), cert in zip(certs, independent, strict=True):
                                        expected = hashlib.sha256(cert.public_key().public_bytes(
                                            serialization.Encoding.DER, serialization.PublicFormat.SubjectPublicKeyInfo,
                                        )).hexdigest().upper()
                                        self.assertEqual(key, expected)
                                        matches += key in CANONICAL
                                    observed.add(name)
                        finally:
                            while process.stdout.read(1024 * 1024):
                                pass
                            process.stdout.close()
                            code = process.wait(timeout=300)
                        errors.seek(0)
                        self.assertEqual(code, 0, errors.read().decode(errors="replace"))
                    self.assertGreater(matches, 0, "Historical vendor stores must not be relabeled as deployed clear")
                    for package in guard.PACKAGES[app]:
                        self.assertIn(package + "/cacert.pem", observed)
                    print(f"\n{app}: {len(observed)} public vendor stores parsed; {matches} baseline denied copies; no image execution")
                finally:
                    log.unlink(missing_ok=True)

    def test_real_linux_docker_init_and_readonly_overlay(self):
        if not DOCKER:
            self.skipTest("Explicit --docker required; real Linux/Docker validation remains outstanding")
        server = subprocess.run(["docker", "version", "--format", "{{.Server.Os}}/{{.Server.Arch}}"],
                                check=True, capture_output=True, text=True).stdout.strip()
        self.assertEqual(server, "linux/amd64")
        for app in APPS:
            with self.subTest(app=app):
                token = uuid.uuid4().hex[:12]
                volume, name = "media-ca-" + token, "media-ca-app-" + token
                network = "media-ca-bootstrap-" + token
                allow_network = app == "sabnzbd" and ALLOW_SAB_BOOTSTRAP_NETWORK
                last_probe = None
                mod = None
                directory = SHARED
                subprocess.run(["docker", "pull", "--platform=linux/amd64", image(app)],
                               check=True, capture_output=True, timeout=300)
                subprocess.run(["docker", "pull", "--platform=linux/amd64", image(app, "ca-filter")],
                               check=True, capture_output=True, timeout=300)
                try:
                    subprocess.run(["docker", "volume", "create", "--label", RUN_LABEL + "=" + token, volume],
                                   check=True, capture_output=True, timeout=30)
                    if allow_network:
                        subprocess.run([
                            "docker", "network", "create", "--driver", "bridge",
                            "--label", RUN_LABEL + "=" + token, network,
                        ], check=True, capture_output=True, timeout=30)
                        subprocess.run([
                            "docker", "run", "--platform=linux/amd64", "--rm", "--name", "media-ca-preflight-" + token,
                            "--label", RUN_LABEL + "=" + token, "--network", "none", "--read-only",
                            "--cap-drop", "ALL", "--security-opt", "no-new-privileges",
                            "--tmpfs", "/config:rw,nosuid,nodev,size=1048576",
                            "--entrypoint", "/bin/sh", image(app), "-ec",
                            "if command -v ffmpeg >/dev/null 2>&1 || command -v ffprobe >/dev/null 2>&1; "
                            "then exit 1; fi",
                        ], check=True, capture_output=True, timeout=30)
                    mount = ["--volume", str(directory) + ":/ca-guard:ro", "--volume", volume + ":/ca-store"]
                    subprocess.run([
                        "docker", "run", "--platform=linux/amd64", "--rm", "--name", "media-ca-copy-" + token,
                        "--label", RUN_LABEL + "=" + token, "--network", "none", "--read-only",
                        "--tmpfs", "/config:rw,nosuid,nodev,size=1048576",
                        "--cap-drop", "ALL", "--security-opt", "no-new-privileges", *mount,
                        "--entrypoint", "/bin/sh", image(app), "/ca-guard/copy-ca-overlay.sh", app,
                    ], check=True, capture_output=True, timeout=90)
                    filtered = subprocess.run([
                        "docker", "run", "--platform=linux/amd64", "--rm", "--name", "media-ca-filter-" + token,
                        "--label", RUN_LABEL + "=" + token, "--network", "none", "--read-only",
                        "--tmpfs", "/config:rw,nosuid,nodev,size=1048576",
                        "--cap-drop", "ALL", "--security-opt", "no-new-privileges", *mount,
                        "--entrypoint", "python3", image(app, "ca-filter"),
                        "-B", "/ca-guard/ca-overlay.py", "--root", "/ca-store", "--app", app,
                        "--policy", "/ca-guard/blocked-spkis.sha256",
                    ], check=True, capture_output=True, text=True, timeout=90)
                    self.assertGreater(json.loads(filtered.stdout)["removed_certificate_copies"], 0)
                    subprocess.run(main_command(app, name, volume, token, network, allow_network),
                                   check=True, capture_output=True, timeout=90)
                    if allow_network:
                        mod = expected_sab_mod()
                        observers = install_sab_observers(name)
                    subprocess.run(["docker", "start", name], check=True, capture_output=True, timeout=30)
                    port = {"bazarr": 6767, "prowlarr": 9696, "sabnzbd": 8080}[app]
                    ready = False
                    deadline = time.monotonic() + (600 if allow_network else 180)
                    while time.monotonic() < deadline:
                        probe = subprocess.run([
                            "docker", "exec", name, "curl", "--silent", "--max-time", "2",
                            "--output", "/dev/null", "--write-out", "%{http_code}", f"http://127.0.0.1:{port}/",
                        ], capture_output=True, text=True, timeout=10)
                        last_probe = probe
                        if http_endpoint_ready(probe):
                            ready = True
                            break
                        time.sleep(2)
                    self.assertTrue(ready, app + " original /init did not reach an HTTP/auth endpoint")
                    if allow_network:
                        bootstrap = verify_sab_bootstrap(name, mod, observers)
                        disconnect_bootstrap_network(name, network)
                        print("\nSAB bootstrap accepted before offline trust checks: " + json.dumps(bootstrap))
                    for path in guard.TREES + guard.PACKAGES[app]:
                        write = subprocess.run([
                            "docker", "exec", name, "/bin/sh", "-ec",
                            'LC_ALL=C; export LC_ALL; : > "/' + path + '/.ca-write-probe"',
                        ], capture_output=True, text=True, timeout=30)
                        self.assertNotEqual(write.returncode, 0, "Trust overlay unexpectedly writable")
                        self.assertIn("read-only file system", write.stderr.lower())
                    if app != "prowlarr":
                        native = (
                            "import runpy,ssl,sys; from pathlib import Path; "
                            "g=runpy.run_path('/ca-guard/ca-overlay.py'); "
                            "d=g['policy_keys'](Path('/ca-guard/blocked-spkis.sha256')); "
                            "contexts=[ssl.create_default_context()]; "
                            + ("sys.path.insert(0,'/app/bazarr/bin/libs'); " if app == "bazarr" else "")
                            + "import certifi; contexts.append(ssl.create_default_context(cafile=certifi.where())); "
                            "roots=[c.get_ca_certs(binary_form=True) for c in contexts]; "
                            "assert all(len(r)>=10 and all(g['spki_digest'](x) not in d for x in r) for r in roots); "
                            "print('Native Python and application certifi contexts have no denied root keys; no custom TLS claim')"
                        )
                        subprocess.run(["docker", "exec", name, "python3", "-B", "-c", native],
                                       check=True, capture_output=True, timeout=30)
                        package = "/" + guard.PACKAGES[app][0]
                        replacement = subprocess.run([
                            "docker", "exec", name, "python3", "-B", "-c",
                            "import os; os.rename(" + repr(package) + "," + repr(package + "-replaced") + ")",
                        ], capture_output=True, timeout=30)
                        self.assertNotEqual(replacement.returncode, 0, "Entire certifi package mount could be replaced")
                    subprocess.run([
                        "docker", "run", "--platform=linux/amd64", "--rm", "--name", "media-ca-verify-" + token,
                        "--label", RUN_LABEL + "=" + token, "--network", "none", "--read-only",
                        "--tmpfs", "/config:rw,nosuid,nodev,size=1048576",
                        "--cap-drop", "ALL", "--security-opt", "no-new-privileges", *mount,
                        "--entrypoint", "python3", image(app, "ca-filter"),
                        "-B", "/ca-guard/ca-overlay.py", "--root", "/ca-store", "--app", app,
                        "--policy", "/ca-guard/blocked-spkis.sha256", "--verify-only",
                    ], check=True, capture_output=True, timeout=90)
                    print(f"\n{app}: stock /init HTTP/auth ready; public overlays clear and read-only; compiled/custom TLS not certified")
                    if app == "sabnzbd" and not allow_network:
                        print("SAB base-image test only: complete mod/package acceptance requires --allow-sab-bootstrap-network")
                finally:
                    try:
                        if allow_network and sys.exc_info()[0] is not None:
                            print("\nSAB safe failure evidence: " + json.dumps(sab_failure_evidence(name, last_probe, mod)))
                    finally:
                        cleanup_owned(token)


if __name__ == "__main__":
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--registry", action="store_true")
    parser.add_argument("--crane", default="crane")
    parser.add_argument("--docker", action="store_true")
    parser.add_argument("--sanitize-sab-startup-errors", action="store_true", help=argparse.SUPPRESS)
    parser.add_argument(
        "--allow-sab-bootstrap-network", action="store_true",
        help="Explicitly allow only disposable SAB main outbound during its existing pinned mod/package bootstrap; disconnect before trust checks",
    )
    options, remaining = parser.parse_known_args()
    if options.sanitize_sab_startup_errors:
        if options.registry or options.docker or options.allow_sab_bootstrap_network or remaining:
            parser.error("Public startup-error sanitizer cannot be combined with test options")
        print(json.dumps(public_startup_errors(sys.stdin.buffer)))
        sys.exit(0)
    if options.allow_sab_bootstrap_network and not options.docker:
        parser.error("--allow-sab-bootstrap-network requires --docker")
    REGISTRY, CRANE, DOCKER = options.registry, options.crane, options.docker
    ALLOW_SAB_BOOTSTRAP_NETWORK = options.allow_sab_bootstrap_network
    unittest.main(argv=[sys.argv[0], *remaining])
