#!/usr/bin/env python3
"""Read-only Ubuntu observations for the existing node-exporter textfile collector."""
import argparse
import email.utils
import hashlib
import json
import math
import os
from pathlib import Path
import platform
import re
import socket
import stat
import subprocess
import sys
import tempfile
import time


KERNEL = re.compile(r"(\d+)\.(\d+)\.(\d+)-(\d+)-generic")
HELP = {
    "homelab_node_observer_last_attempt_seconds": "Unix time of this observer attempt, not its success.",
    "homelab_node_observer_check_success": "Whether this specific check completed with usable input.",
    "homelab_node_reboot_required": "Whether the regular Ubuntu reboot-required flag exists.",
    "homelab_node_kernel_abi_pending": "Whether a newer installed generic kernel ABI is not running.",
    "homelab_node_kernel_abi_info": "Running and newest installed generic ABI, not package revision compliance.",
    "homelab_node_apt_upgradable_packages": "Installed packages with a newer cached APT candidate.",
    "homelab_node_apt_security_updates_available": "Installed packages with a newer cached Ubuntu security version.",
    "homelab_node_apt_security_metadata_seconds": "Oldest active Ubuntu security InRelease publication time.",
}


class ObservationError(RuntimeError):
    pass


def require(condition, message):
    if not condition:
        raise ObservationError(message)


def reboot(root):
    require(stat.S_ISDIR((root / "run").stat().st_mode), "Ubuntu run directory is unavailable")
    try:
        metadata = (root / "run/reboot-required").lstat()
    except FileNotFoundError:
        return [("homelab_node_reboot_required", 0, {})]
    require(stat.S_ISREG(metadata.st_mode), "Reboot flag is not a regular file")
    return [("homelab_node_reboot_required", 1, {})]


def kernel_abi(running, output):
    current = KERNEL.fullmatch(running)
    require(current is not None, "Running kernel is not a supported generic ABI")
    installed = {}
    for line in output.splitlines():
        fields = line.split("\t")
        require(len(fields) == 2, "Malformed dpkg kernel status")
        package, status = fields
        if status != "installed":
            continue
        release = package.split(":")[0].removeprefix("linux-image-")
        match = KERNEL.fullmatch(release)
        require(match is not None, "Unexpected installed kernel package")
        installed[release] = tuple(map(int, match.groups()))
    require(running in installed, "Running ABI has no installed generic kernel package")
    newest = max(installed, key=installed.get)
    return [
        ("homelab_node_kernel_abi_pending", int(installed[newest] > installed[running]), {}),
        ("homelab_node_kernel_abi_info", 1, {"running_release": running, "newest_installed_release": newest}),
    ]


def kernel():
    result = subprocess.run(
        ["/usr/bin/dpkg-query", "-W", r"-f=${binary:Package}\t${db:Status-Status}\n", "linux-image-[0-9]*-generic"],
        capture_output=True, text=True, timeout=20, check=True, env={**os.environ, "LC_ALL": "C"},
    )
    return kernel_abi(platform.release(), result.stdout)


def release_input(path, archive, now):
    metadata = path.lstat()
    require(stat.S_ISREG(metadata.st_mode) and metadata.st_size <= 4 * 1024 * 1024,
            "Security InRelease is not a bounded regular file")
    with path.open("rb") as stream:
        raw = stream.read(4 * 1024 * 1024 + 1)
    require(len(raw) <= 4 * 1024 * 1024, "Security InRelease exceeded bounded input size")
    try:
        body = raw.decode("utf-8").split("\n-----BEGIN PGP SIGNATURE-----", 1)[0]
    except UnicodeDecodeError as error:
        raise ObservationError("Security InRelease is not UTF-8") from error
    fields = {}
    for key in ("Date", "Suite", "Valid-Until"):
        values = [line.split(":", 1)[1].strip() for line in body.splitlines() if line.startswith(key + ":")]
        require(len(values) <= 1 and (key == "Valid-Until" or len(values) == 1),
                "Missing or duplicate security release header")
        if values:
            fields[key] = values[0]
    require(fields["Suite"] == archive, "Security release suite differs from active cache")
    try:
        published = email.utils.parsedate_to_datetime(fields["Date"])
        expires = email.utils.parsedate_to_datetime(fields["Valid-Until"]) if "Valid-Until" in fields else None
    except (ValueError, TypeError) as error:
        raise ObservationError("Malformed security release date") from error
    require(published.tzinfo is not None, "Security release date has no timezone")
    epoch = published.timestamp()
    require(0 <= now - epoch <= 86400, "Security release publication is future-dated or older than 24 hours")
    if expires is not None:
        require(expires.tzinfo is not None and expires.timestamp() >= now, "Security release has expired")
    return epoch, hashlib.sha256(raw).hexdigest()


def security_updates(root, now, cache, candidate_version, compare):
    directory = root / "var/lib/apt/lists"
    releases = {}
    for item in cache.file_list:
        if item.origin != "Ubuntu" or not item.archive or not item.archive.endswith("-security"):
            continue
        path = Path(item.filename)
        marker = "_dists_" + item.archive + "_"
        require(path.parent == directory and path.name.count(marker) == 1, "Unsupported security cache layout")
        release = directory / (path.name.split(marker)[0] + marker + "InRelease")
        releases[release] = item.archive
    require(bool(releases), "No active Ubuntu security cache inputs")
    before = {path: release_input(path, archive, now) for path, archive in releases.items()}
    security = upgradable = 0
    for package in cache.packages:
        if package.current_ver is None:
            continue
        candidate = candidate_version(package)
        upgradable += int(candidate is not None and compare(candidate.ver_str, package.current_ver.ver_str) > 0)
        # A newer -updates candidate must not hide an older, still unapplied security version.
        if any(compare(version.ver_str, package.current_ver.ver_str) > 0
               and any(origin.origin == "Ubuntu" and origin.archive and origin.archive.endswith("-security")
                       for origin, _ in version.file_list) for version in package.version_list):
            security += 1
    require(before == {path: release_input(path, archive, now) for path, archive in releases.items()},
            "Security release metadata changed during observation")
    return [
        ("homelab_node_apt_upgradable_packages", upgradable, {}),
        ("homelab_node_apt_security_updates_available", security, {}),
        ("homelab_node_apt_security_metadata_seconds", min(value[0] for value in before.values()), {}),
    ]


def apt_security(root, now):
    import apt_pkg
    from apt.progress.base import OpProgress
    before = apt_input_identity(root)
    try:
        apt_pkg.init()
        apt_pkg.config.set("Dir::Cache::pkgcache", "")
        apt_pkg.config.set("Dir::Cache::srcpkgcache", "")
        cache = apt_pkg.Cache(OpProgress())
        candidates = apt_pkg.DepCache(cache)
        result = security_updates(root, now, cache, candidates.get_candidate_ver, apt_pkg.version_compare)
    except apt_pkg.Error as error:
        raise ObservationError("Read-only Python-APT cache observation failed") from error
    require(before == apt_input_identity(root), "APT inputs or installed package state changed during observation")
    return result


def apt_input_identity(root):
    paths = [root / "var/lib/dpkg/status"]
    paths.extend(path for path in (root / "var/lib/apt/lists").iterdir()
                 if not stat.S_ISDIR(path.lstat().st_mode))
    result = {}
    for path in paths:
        metadata = path.lstat()
        require(stat.S_ISREG(metadata.st_mode), "APT input is not a regular file")
        result[str(path)] = (metadata.st_dev, metadata.st_ino, metadata.st_size, metadata.st_mtime_ns)
    return result


def collect(node, now, checks):
    require(re.fullmatch(r"[a-z0-9](?:[a-z0-9.-]{0,251}[a-z0-9])?", node) is not None,
            "Hostname is not a supported Kubernetes node name")
    samples = [("homelab_node_observer_last_attempt_seconds", now, {})]
    errors = []
    for name, check in checks.items():
        try:
            result = check()
        except (ObservationError, OSError, ImportError, subprocess.SubprocessError) as error:
            if isinstance(error, ObservationError):
                message = str(error)
            elif isinstance(error, ImportError):
                message = "System Python python3-apt dependency unavailable"
            elif isinstance(error, subprocess.CalledProcessError):
                message = "Probe command failed with exit " + str(error.returncode)
            elif isinstance(error, subprocess.TimeoutExpired):
                message = "Probe command timed out"
            else:
                message = type(error).__name__ + " (errno " + str(getattr(error, "errno", None)) + ")"
            errors.append(name + ": " + message)
            success = 0
        else:
            samples.extend(result)
            success = 1
        samples.append(("homelab_node_observer_check_success", success, {"check": name}))
    lines = []
    for metric in sorted({sample[0] for sample in samples}):
        lines += ["# HELP " + metric + " " + HELP[metric], "# TYPE " + metric + " gauge"]
        for name, value, labels in samples:
            if name != metric:
                continue
            require(math.isfinite(value) and value >= 0, "Invalid observer sample")
            fields = ",".join(key + "=" + json.dumps(text) for key, text in sorted({"node": node, **labels}.items()))
            lines.append(metric + "{" + fields + "} " + str(value))
    return "\n".join(lines) + "\n", errors


def publish(path, text):
    temporary = None
    try:
        with tempfile.NamedTemporaryFile(mode="w", encoding="utf-8", newline="\n",
                                         prefix="." + path.name + "-", dir=path.parent, delete=False) as stream:
            temporary = Path(stream.name)
            stream.write(text)
            stream.flush()
            os.fsync(stream.fileno())
        temporary.chmod(0o644)
        os.replace(temporary, path)
    finally:
        if temporary is not None:
            temporary.unlink(missing_ok=True)


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    output = parser.add_mutually_exclusive_group(required=True)
    output.add_argument("--stdout", action="store_true")
    output.add_argument("--output", type=Path)
    args = parser.parse_args()
    root, now = Path("/"), time.time()
    text, errors = collect(socket.gethostname(), now, {
        "reboot": lambda: reboot(root), "kernel": kernel, "security": lambda: apt_security(root, now),
    })
    if args.stdout:
        print(text, end="")
    else:
        publish(args.output, text)
    for error in errors:
        print("Node observer check failed: " + error, file=sys.stderr)
    return int(bool(errors))


if __name__ == "__main__":
    sys.exit(main())
