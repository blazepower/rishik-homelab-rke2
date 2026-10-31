"""Configure native CWA ingestion without rewriting canonical reader files."""
from contextlib import closing
import os
from pathlib import Path
import sqlite3
import sys


SETTINGS = {
    "default_settings": 0,
    "auto_convert": 0,
    "kindle_epub_fixer": 0,
    "kindle_epub_fixer_aggressive": 0,
    "auto_metadata_enforcement": 0,
    "auto_metadata_fetch_enabled": 0,
    "auto_metadata_smart_application": 0,
    "duplicate_auto_resolve_enabled": 0,
    "archived_cleanup_enabled": 0,
    "koreader_sync_enabled": 0,
    "hardcover_auto_fetch_enabled": 0,
    "auto_backup_imports": 0,
    "auto_backup_conversions": 0,
    "auto_zip_backups": 0,
    "auto_ingest_automerge": "new_record",
}


def configure(config, library, bridge, scripts):
    config, library, bridge = map(Path, (config, library, bridge))
    database = library / "metadata.db"
    if not database.is_file():
        raise RuntimeError("Missing existing library; refusing automatic empty-library creation")
    with closing(sqlite3.connect(database.as_uri() + "?mode=ro", uri=True)) as connection:
        if connection.execute("PRAGMA quick_check").fetchall() != [("ok",)]:
            raise RuntimeError("Library integrity check failed")
    for name in ("queue", "state"):
        (bridge / name).mkdir(parents=True, exist_ok=True)
    sys.path.insert(0, str(scripts))
    from cwa_db import CWA_DB

    os.environ["CWA_DB_PATH"] = str(config)
    cwa = CWA_DB()
    try:
        fields = {row[1] for row in cwa.cur.execute("PRAGMA table_info(cwa_settings)")}
        if not set(SETTINGS) <= fields:
            raise RuntimeError("Pinned CWA settings schema changed")
        assignments = ", ".join(f"{name}=?" for name in SETTINGS)
        cwa.cur.execute(f"UPDATE cwa_settings SET {assignments}", tuple(SETTINGS.values()))
        cwa.con.commit()
        actual = cwa.cur.execute(
            "SELECT " + ", ".join(SETTINGS) + " FROM cwa_settings"
        ).fetchall()
        if actual != [tuple(SETTINGS.values())]:
            raise RuntimeError("Byte-preserving ingest settings were not saved")
    finally:
        cwa.con.close()
    settings = config / "app.db"
    if not settings.is_file():
        raise RuntimeError("Missing existing Calibre-Web configuration")
    with closing(sqlite3.connect(settings)) as connection:
        users = connection.execute("SELECT COUNT(*) FROM user").fetchone()[0]
        connection.execute(
            "UPDATE settings SET config_calibre_dir=?, config_calibre_split=0, config_embed_metadata=0",
            ("/calibre-library",),
        )
        fields = {row[1] for row in connection.execute("PRAGMA table_info(user)")}
        if "auto_send_enabled" in fields:
            connection.execute("UPDATE user SET auto_send_enabled=0")
        connection.commit()
        if connection.execute("SELECT COUNT(*) FROM user").fetchone()[0] != users:
            raise RuntimeError("Account count changed")
    print("Existing accounts retained; native ingest configured to preserve EPUB bytes", flush=True)


if __name__ == "__main__":
    configure("/config", "/library", "/bridge", "/app/calibre-web-automated/scripts")
