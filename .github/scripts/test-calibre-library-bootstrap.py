"""Regression tests for the Calibre-Web empty-library cutover guard."""

from contextlib import closing, redirect_stdout
import io
from pathlib import Path
import shutil
import sqlite3
import unittest
from unittest import mock
import uuid

import yaml


ROOT = Path(__file__).resolve().parents[2]
RELEASE = yaml.safe_load((ROOT / "apps/calibre-web/helmrelease.yaml").read_text())
VALUES = RELEASE["spec"]["values"]
CONTROLLER = VALUES["controllers"]["calibre-web"]
BOOTSTRAP = CONTROLLER["initContainers"]["bootstrap-library"]
MODULE = {"__name__": "bootstrap_test"}
exec(compile(BOOTSTRAP["args"][0], "bootstrap-library", "exec"), MODULE)


class BootstrapTests(unittest.TestCase):
    def setUp(self):
        self.workspace = ROOT / (".calibre-bootstrap-test-" + uuid.uuid4().hex)
        self.source = self.workspace / "legacy"
        self.library = self.workspace / "library"
        self.source.mkdir(parents=True)
        self.library.mkdir()
        self.addCleanup(shutil.rmtree, self.workspace)

    def database(self, directory, books=0, formats=0):
        path = directory / "metadata.db"
        with closing(sqlite3.connect(path)) as connection:
            connection.execute("CREATE TABLE books (id INTEGER PRIMARY KEY)")
            connection.execute("CREATE TABLE data (id INTEGER PRIMARY KEY)")
            connection.executemany("INSERT INTO books VALUES (?)", [(i,) for i in range(books)])
            connection.executemany("INSERT INTO data VALUES (?)", [(i,) for i in range(formats)])
            connection.commit()
        return path

    def bootstrap(self):
        with redirect_stdout(io.StringIO()):
            MODULE["bootstrap"](str(self.source), str(self.library))

    def test_empty_source_migrated_without_modification(self):
        source = self.database(self.source)
        before = source.read_bytes()
        self.bootstrap()
        self.assertEqual(source.read_bytes(), before)
        with closing(sqlite3.connect(self.library / "metadata.db")) as connection:
            self.assertEqual(MODULE["validate"](connection), (0, 0))
        self.assertFalse((self.library / ".metadata.db.bootstrap").exists())

    def test_existing_populated_destination_preserved_without_source(self):
        target = self.database(self.library, books=2, formats=2)
        before = target.read_bytes()
        self.bootstrap()
        self.assertEqual(target.read_bytes(), before)

    def test_missing_source_fails_without_creating_database(self):
        with self.assertRaisesRegex(RuntimeError, "Missing legacy"):
            self.bootstrap()
        self.assertFalse((self.library / "metadata.db").exists())

    def test_nonempty_source_fails_without_partial_destination(self):
        for books, formats in ((1, 0), (0, 1)):
            with self.subTest(books=books, formats=formats):
                source = self.database(self.source, books, formats)
                with self.assertRaisesRegex(RuntimeError, "not empty"):
                    self.bootstrap()
                self.assertFalse((self.library / "metadata.db").exists())
                self.assertFalse((self.library / ".metadata.db.bootstrap").exists())
                source.unlink()

    def test_corrupt_source_or_destination_fails_closed(self):
        for directory in (self.source, self.library):
            with self.subTest(directory=directory.name):
                database = directory / "metadata.db"
                database.write_bytes(b"not a sqlite database")
                with self.assertRaises(sqlite3.DatabaseError):
                    self.bootstrap()
                self.assertEqual(database.read_bytes(), b"not a sqlite database")
                database.unlink()
        self.assertFalse((self.library / ".metadata.db.bootstrap").exists())

    def test_incomplete_staging_file_is_replaced_on_retry(self):
        self.database(self.source)
        staging = self.library / ".metadata.db.bootstrap"
        staging.write_bytes(b"incomplete previous attempt")
        self.bootstrap()
        self.assertFalse(staging.exists())
        with closing(sqlite3.connect(self.library / "metadata.db")) as connection:
            self.assertEqual(MODULE["validate"](connection), (0, 0))

    def test_missing_calibre_schema_fails_closed(self):
        with closing(sqlite3.connect(self.source / "metadata.db")) as connection:
            connection.execute("CREATE TABLE unrelated (id INTEGER)")
        with self.assertRaises(sqlite3.OperationalError):
            self.bootstrap()
        self.assertFalse((self.library / "metadata.db").exists())
        self.assertFalse((self.library / ".metadata.db.bootstrap").exists())

    def test_source_sidecars_require_operator_review(self):
        self.database(self.source)
        for suffix in ("-wal", "-journal"):
            sidecar = self.source / ("metadata.db" + suffix)
            sidecar.touch()
            with self.assertRaisesRegex(RuntimeError, "sidecar"):
                self.bootstrap()
            sidecar.unlink()
        self.assertFalse((self.library / "metadata.db").exists())

    def test_backup_deadline_leaves_no_destination(self):
        self.database(self.source)
        with mock.patch.object(MODULE["time"], "monotonic", side_effect=[0, 61]):
            with self.assertRaises(TimeoutError):
                self.bootstrap()
        self.assertFalse((self.library / "metadata.db").exists())
        self.assertFalse((self.library / ".metadata.db.bootstrap").exists())

    def test_wiring_is_single_writer_and_keeps_source_out_of_app(self):
        self.assertEqual(CONTROLLER["replicas"], 1)
        self.assertEqual(CONTROLLER["strategy"], "Recreate")
        self.assertEqual(BOOTSTRAP["image"], CONTROLLER["containers"]["app"]["image"])
        mounts = VALUES["persistence"]["legacy-library"]["advancedMounts"]["calibre-web"]
        self.assertNotIn("app", mounts)
        self.assertTrue(mounts["bootstrap-library"][0]["readOnly"])
        scaler = yaml.safe_load((ROOT / "apps/calibre-web/scaledobject.yaml").read_text())
        self.assertEqual(scaler["spec"]["minReplicaCount"], 1)
        self.assertEqual(scaler["spec"]["maxReplicaCount"], 1)


if __name__ == "__main__":
    unittest.main()
