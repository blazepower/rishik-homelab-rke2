"""Copy stable NAS EPUBs to CWA's consumer queue, never changing source books."""
import argparse
from collections import Counter
from contextlib import closing
import fcntl
import hashlib
import json
import os
from pathlib import Path, PurePosixPath
import shutil
import sqlite3
import stat
import time
import unicodedata
import xml.etree.ElementTree as ET
import zipfile


def signature(path):
    info = path.stat(follow_symlinks=False)
    if not stat.S_ISREG(info.st_mode):
        raise ValueError("Not a regular file")
    return [info.st_size, info.st_mtime_ns]


def digest(path):
    result = hashlib.sha256()
    with path.open("rb") as stream:
        for block in iter(lambda: stream.read(1024 * 1024), b""):
            result.update(block)
    return result.hexdigest()


def normalized(value):
    return " ".join(unicodedata.normalize("NFKC", value).casefold().split())


def identity(path):
    with zipfile.ZipFile(path) as book:
        if book.read("mimetype") != b"application/epub+zip":
            raise ValueError("Invalid EPUB mimetype")
        container_info = book.getinfo("META-INF/container.xml")
        if container_info.file_size > 1024 * 1024:
            raise ValueError("Oversized EPUB container")
        container = ET.fromstring(book.read(container_info))
        package = container.find(".//{*}rootfile")
        if package is None:
            raise ValueError("Missing EPUB package")
        metadata_info = book.getinfo(package.attrib["full-path"])
        if metadata_info.file_size > 2 * 1024 * 1024:
            raise ValueError("Oversized EPUB metadata")
        metadata = ET.fromstring(book.read(metadata_info))
        namespace = "{http://purl.org/dc/elements/1.1/}"
        titles = [node.text or "" for node in metadata.findall(".//" + namespace + "title")]
        authors = [node.text or "" for node in metadata.findall(".//" + namespace + "creator")]
        if not titles or not authors:
            raise ValueError("Missing EPUB title or author")
        return [normalized(titles[0]), sorted(normalized(author) for author in authors)]


def library_inventory(library, cache):
    hashes, identities = set(), set()
    with closing(sqlite3.connect(
        (library / "metadata.db").as_uri() + "?mode=ro", uri=True, timeout=15,
    )) as connection:
        records = connection.execute(
            "SELECT books.path, data.name FROM books JOIN data ON books.id=data.book "
            "WHERE data.format='EPUB'"
        ).fetchall()
    updated = {}
    for directory, name in records:
        relative = PurePosixPath(directory) / (name + ".epub")
        if relative.is_absolute() or ".." in relative.parts:
            raise ValueError("Unsafe managed-library path")
        path = library.joinpath(*relative.parts)
        if not path.resolve().is_relative_to(library.resolve()):
            raise ValueError("Managed-library path escapes mount")
        current = signature(path)
        key = str(relative)
        previous = cache.get(key)
        if previous and previous["signature"] == current:
            item = previous
        else:
            item = {"signature": current, "sha256": digest(path), "identity": identity(path)}
            if signature(path) != current:
                raise ValueError("Library file changed during inspection")
        updated[key] = item
        hashes.add(item["sha256"])
        identities.add(json.dumps(item["identity"]))
    return hashes, identities, updated


def save_state(path, state):
    temporary = path.with_suffix(".tmp")
    with temporary.open("w", encoding="utf-8") as stream:
        json.dump(state, stream, ensure_ascii=True)
        stream.flush()
        os.fsync(stream.fileno())
    os.replace(temporary, path)


def run_once(source, library, bridge, now=None, stable_seconds=60, max_pending=4):
    now = time.time() if now is None else now
    source, library, bridge = map(Path, (source, library, bridge))
    if source.resolve() == library.resolve() or source.resolve() == bridge.resolve():
        raise ValueError("Acquisition, library, and ingest paths must be separate")
    if not source.is_dir():
        raise ValueError("Acquisition mount is unavailable")
    queue, state_dir = bridge / "queue", bridge / "state"
    queue.mkdir(parents=True, exist_ok=True)
    state_dir.mkdir(parents=True, exist_ok=True)
    with (state_dir / "staging.lock").open("a") as lock:
        fcntl.flock(lock, fcntl.LOCK_EX | fcntl.LOCK_NB)
        path = state_dir / "staging.json"
        state = json.loads(path.read_text()) if path.exists() else {}
        observations = state.get("observations", {})
        ledger = state.get("ledger", {})
        hashes, identities, cache = library_inventory(library, state.get("library_files", {}))
        counts = Counter()
        for checksum, item in ledger.items():
            if checksum in hashes:
                item["status"] = "imported"
            elif item["status"] == "pending" and now - item["queued_at"] > 900:
                item["status"] = "retry" if item["attempts"] < 3 else "failed"
        pending = sum(item["status"] == "pending" for item in ledger.values())
        current_observations = {}
        for directory, subdirectories, filenames in os.walk(source, followlinks=False):
            subdirectories[:] = sorted(
                name for name in subdirectories
                if not name.startswith(".") and name != "calibre-library"
                and not (Path(directory) / name).is_symlink()
            )
            for name in sorted(filenames):
                if name.startswith(".") or not name.lower().endswith(".epub"):
                    if Path(name).suffix.lower() in (".mobi", ".azw", ".azw3", ".kfx", ".pdf", ".fb2"):
                        counts["unsupported_format"] += 1
                    continue
                book = Path(directory) / name
                if book.is_symlink():
                    continue
                try:
                    before = signature(book)
                    if not book.resolve().is_relative_to(source.resolve()):
                        raise ValueError("Source escapes acquisition mount")
                    key = str(book.relative_to(source))
                    previous = observations.get(key)
                    observed = {"signature": before, "first_seen": now}
                    if previous and previous["signature"] == before:
                        observed = previous
                    current_observations[key] = observed
                    if now - observed["first_seen"] < stable_seconds or now - before[1] / 1e9 < stable_seconds:
                        counts["waiting_for_stable_source"] += 1
                        continue
                    checksum = observed.get("sha256") or digest(book)
                    if signature(book) != before:
                        raise ValueError("Source changed during hashing")
                    observed["sha256"] = checksum
                    if checksum in hashes:
                        counts["already_in_library"] += 1
                        continue
                    item = ledger.get(checksum)
                    if item and item["status"] in ("pending", "failed", "metadata_duplicate", "invalid"):
                        counts[item["status"]] += 1
                        continue
                    book_identity = identity(book)
                    if json.dumps(book_identity) in identities:
                        ledger[checksum] = {"status": "metadata_duplicate", "source": key}
                        counts["metadata_duplicate"] += 1
                        continue
                    if pending >= max_pending:
                        counts["queue_limited"] += 1
                        continue
                    if shutil.disk_usage(bridge).free < before[0] + 64 * 1024 * 1024:
                        raise RuntimeError("Insufficient ingest free space")
                    if shutil.disk_usage(library).free < before[0] + 128 * 1024 * 1024:
                        raise RuntimeError("Insufficient managed-library free space")
                    destination = queue / (checksum + ".epub")
                    if destination.exists():
                        if digest(destination) != checksum:
                            raise RuntimeError("Existing ingest file differs")
                    else:
                        temporary = queue / ("." + checksum + ".partial")
                        if temporary.exists():
                            temporary.unlink()
                        shutil.copyfile(book, temporary)
                        if signature(book) != before or digest(temporary) != checksum:
                            temporary.unlink()
                            raise ValueError("Source changed during staging")
                        os.replace(temporary, destination)
                    ledger[checksum] = {
                        "status": "pending", "source": key, "queued_at": now,
                        "attempts": item.get("attempts", 0) + 1 if item else 1,
                    }
                    pending += 1
                    counts["queued"] += 1
                except (ValueError, KeyError, zipfile.BadZipFile, ET.ParseError) as error:
                    if "sha256" in current_observations.get(str(book.relative_to(source)), {}):
                        checksum = current_observations[str(book.relative_to(source))]["sha256"]
                        ledger[checksum] = {"status": "invalid", "source": str(book.relative_to(source))}
                    counts["invalid_or_unstable"] += 1
                    print(f"Skipped EPUB: {book.relative_to(source)} ({type(error).__name__})", flush=True)
        save_state(path, {
            "observations": current_observations, "ledger": ledger, "library_files": cache,
        })
        counts["library_epubs"] = len(hashes)
        print(json.dumps(dict(counts), sort_keys=True), flush=True)
        return dict(counts)


def main():
    parser = argparse.ArgumentParser()
    parser.add_argument("--source", type=Path, default=Path("/source/books"))
    parser.add_argument("--library", type=Path, default=Path("/library"))
    parser.add_argument("--bridge", type=Path, default=Path("/bridge"))
    parser.add_argument("--interval", type=int, default=120)
    parser.add_argument("--once", action="store_true")
    args = parser.parse_args()
    while True:
        try:
            run_once(args.source, args.library, args.bridge)
        except (OSError, sqlite3.Error, RuntimeError, ValueError) as error:
            print(f"EPUB staging failed closed: {type(error).__name__}: {error}", flush=True)
            if args.once:
                raise
        if args.once:
            return
        time.sleep(args.interval)


if __name__ == "__main__":
    main()
