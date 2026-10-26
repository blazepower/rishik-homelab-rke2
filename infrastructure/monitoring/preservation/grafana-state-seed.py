"""One-shot native SQLite seed; runs only in the coordinator-created helper."""

import hashlib
import json
import os
from contextlib import closing
from pathlib import Path
import shutil
import sqlite3
import stat
import time
import urllib.request


TABLES = (
    "user", "user_auth", "org", "org_user", "preferences", "dashboard",
    "data_source", "star", "playlist", "annotation", "library_element",
    "team", "team_member", "plugin_setting", "alert_rule",
)
SIDECARS = {"grafana.db-wal", "grafana.db-shm", "grafana.db-journal"}


def table_counts(connection):
    tables = {row[0] for row in connection.execute("SELECT name FROM sqlite_master WHERE type='table'")}
    return {name: connection.execute(f'SELECT count(*) FROM "{name}"').fetchone()[0]
            for name in TABLES if name in tables}


def backup_database(source, destination):
    if destination.exists():
        raise RuntimeError("Destination database already exists; refusing to overwrite saved state")
    fd = os.open(destination, os.O_WRONLY | os.O_CREAT | os.O_EXCL, 0o600)
    os.close(fd)
    with closing(sqlite3.connect(f"file:{source}?mode=ro", uri=True, timeout=30)) as src:
        src.execute("PRAGMA temp_store=MEMORY")
        src.execute("PRAGMA query_only=ON")
        src.execute("BEGIN")
        expected = table_counts(src)  # Establish and hold the source read snapshot.
        with closing(sqlite3.connect(destination)) as dst:
            dst.execute("PRAGMA temp_store=MEMORY")
            src.backup(dst, pages=128, sleep=0.05)
            if dst.execute("PRAGMA integrity_check").fetchall() != [("ok",)]:
                raise RuntimeError("Destination database integrity check failed")
            if table_counts(dst) != expected:
                raise RuntimeError("Destination counts differ from the held source snapshot")
        src.rollback()
    os.chmod(destination, 0o600)
    return expected


def copy_other_files(source, destination):
    count = size = 0
    for child in source.iterdir():
        if child.name == "grafana.db" or child.name in SIDECARS:
            continue
        if child.name == ".grafana-preservation":
            raise RuntimeError("Source unexpectedly already has preservation state")
        target = destination / child.name
        if target.exists() or target.is_symlink():
            raise RuntimeError("Destination contains existing non-database state")
        if child.is_symlink():
            target.symlink_to(os.readlink(child), target_is_directory=child.is_dir())
        elif child.is_dir():
            shutil.copytree(child, target, symlinks=True)
        else:
            shutil.copy2(child, target, follow_symlinks=False)
        paths = [target] if not target.is_dir() or target.is_symlink() else target.rglob("*")
        for path in paths:
            metadata = path.lstat()
            if stat.S_ISREG(metadata.st_mode):
                count += 1
                size += metadata.st_size
    return {"nonDatabaseFiles": count, "nonDatabaseBytes": size}


def save_silences(destination):
    url = "http://kube-prometheus-stack-alertmanager.monitoring.svc.cluster.local:9093/api/v2/silences"
    for attempt in range(3):
        try:
            with urllib.request.urlopen(url, timeout=20) as response:
                silences = json.load(response)
            if not isinstance(silences, list):
                raise RuntimeError("Unexpected Alertmanager response shape")
            with destination.open("x", encoding="utf-8") as output:
                json.dump(silences, output)
                output.flush()
                os.fsync(output.fileno())
            os.chmod(destination, 0o600)
            return len(silences)
        except (OSError, ValueError):
            if destination.exists() or attempt == 2:
                raise RuntimeError("Alertmanager export failed; no response body logged") from None
            time.sleep(2)


def main():
    os.umask(0o077)
    source, destination = Path("/source"), Path("/destination")
    source_uid = os.environ["EXPECTED_SOURCE_POD_UID"]
    claim_uid = os.environ["EXPECTED_DESTINATION_PVC_UID"]
    if claim_uid == "SET_BY_PRESERVATION_RUNNER" or not claim_uid:
        raise RuntimeError("Run the UID-checking preservation runner, not the bare Job")
    if not (source / "grafana.db").is_file():
        raise RuntimeError("Expected source database is missing")
    if any(path.name != "lost+found" for path in destination.iterdir()):
        raise RuntimeError("Destination is not empty; refusing to overwrite any existing state")
    recovery = destination / ".grafana-preservation"
    recovery.mkdir(mode=0o700)
    counts = backup_database(source / "grafana.db", destination / "grafana.db")
    shutil.copy2(destination / "grafana.db", recovery / "grafana-pre-upgrade.db")
    os.chmod(recovery / "grafana-pre-upgrade.db", 0o600)
    copied = copy_other_files(source, destination)
    silence_count = save_silences(recovery / "alertmanager-silences.json")
    digest = hashlib.sha256()
    with (destination / "grafana.db").open("rb") as database:
        for block in iter(lambda: database.read(1024 * 1024), b""):
            digest.update(block)
    receipt = {
        "sourcePodUID": source_uid, "destinationPVCUID": claim_uid,
        "sqliteIntegrity": "ok", "heldSnapshotTableCounts": counts,
        "databaseSHA256": digest.hexdigest(), "silenceCount": silence_count, **copied,
    }
    with (recovery / "seed-complete.json").open("x", encoding="utf-8") as output:
        json.dump(receipt, output)
        output.flush()
        os.fsync(output.fileno())
    # Flush only this destination, not every filesystem on the node.
    for path in destination.rglob("*"):
        if path.is_file() and not path.is_symlink():
            with path.open("rb") as output:
                os.fsync(output.fileno())
    for path in [recovery, destination]:
        descriptor = os.open(path, os.O_RDONLY | os.O_DIRECTORY)
        try:
            os.fsync(descriptor)
        finally:
            os.close(descriptor)
    print(json.dumps(receipt))


if __name__ == "__main__":
    try:
        main()
    except Exception as error:
        # Do not leak SQL rows, silence descriptions, or HTTP response bodies.
        print(json.dumps({"success": False, "errorType": type(error).__name__}))
        raise SystemExit(1)
