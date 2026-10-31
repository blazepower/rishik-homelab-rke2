"""Regression tests for safe NAS-to-native-CWA staging."""
import hashlib
import importlib.util
import json
import os
from pathlib import Path
import sqlite3
import tempfile
import time
import unittest
import zipfile


APP = (Path(os.environ["STAGING_APP_PATH"]) if "STAGING_APP_PATH" in os.environ
       else Path(__file__).resolve().parents[2] / "apps" / "calibre-web")
spec = importlib.util.spec_from_file_location("stage_books", APP / "stage-books.py")
stage = importlib.util.module_from_spec(spec)
spec.loader.exec_module(stage)


def epub(path, title="Bridge Verification", extra=""):
    with zipfile.ZipFile(path, "w") as archive:
        archive.writestr("mimetype", "application/epub+zip")
        archive.writestr("META-INF/container.xml", (
            '<container xmlns="urn:oasis:names:tc:opendocument:xmlns:container" version="1.0">'
            '<rootfiles><rootfile full-path="content.opf" '
            'media-type="application/oebps-package+xml"/></rootfiles></container>'
        ))
        archive.writestr("content.opf", (
            '<package xmlns="http://www.idpf.org/2007/opf" version="3.0" unique-identifier="id">'
            '<metadata xmlns:dc="http://purl.org/dc/elements/1.1/">'
            f'<dc:title>{title}</dc:title><dc:creator>Bridge Test</dc:creator>'
            '<dc:identifier id="id">urn:uuid:bridge-verification</dc:identifier>'
            '<dc:language>en</dc:language></metadata>'
            '<manifest><item id="chapter" href="chapter.xhtml" media-type="application/xhtml+xml"/>'
            '</manifest><spine><itemref idref="chapter"/></spine></package>'
        ))
        archive.writestr("chapter.xhtml", (
            '<html xmlns="http://www.w3.org/1999/xhtml"><head><title>Verification</title>'
            f"</head><body><p>Reader bridge verification. {extra}</p></body></html>"
        ))


class StagingTests(unittest.TestCase):
    def setUp(self):
        self.temporary = tempfile.TemporaryDirectory()
        self.addCleanup(self.temporary.cleanup)
        self.root = Path(self.temporary.name)
        self.source, self.library, self.bridge = (
            self.root / "source", self.root / "library", self.root / "bridge",
        )
        self.source.mkdir()
        self.library.mkdir()
        with sqlite3.connect(self.library / "metadata.db") as connection:
            connection.executescript(
                "CREATE TABLE books(id INTEGER PRIMARY KEY, path TEXT);"
                "CREATE TABLE data(book INTEGER, name TEXT, format TEXT);"
            )
        self.now = time.time() + 3600

    def scan(self, seconds=0):
        return stage.run_once(
            self.source, self.library, self.bridge, now=self.now + seconds,
        )

    def register(self, book, book_id=1):
        directory = self.library / str(book_id)
        directory.mkdir()
        target = directory / "book.epub"
        target.write_bytes(book.read_bytes())
        with sqlite3.connect(self.library / "metadata.db") as connection:
            connection.execute("INSERT INTO books VALUES(?,?)", (book_id, str(book_id)))
            connection.execute("INSERT INTO data VALUES(?,?,?)", (book_id, "book", "EPUB"))

    def test_stable_copy_preserves_source_and_acknowledged_import_does_not_repeat(self):
        book = self.source / "book.epub"
        epub(book)
        original = book.read_bytes()
        self.assertEqual(self.scan()["waiting_for_stable_source"], 1)
        self.assertEqual(self.scan(61)["queued"], 1)
        queued = list((self.bridge / "queue").glob("*.epub"))
        self.assertEqual(len(queued), 1)
        self.assertEqual(queued[0].read_bytes(), original)
        queued[0].unlink()
        self.register(book)
        self.assertEqual(self.scan(122)["already_in_library"], 1)
        self.assertEqual(list((self.bridge / "queue").glob("*.epub")), [])
        self.assertEqual(book.read_bytes(), original)

    def test_changing_download_must_stabilize_before_publication(self):
        book = self.source / "book.epub"
        epub(book)
        self.scan()
        epub(book, extra="Still downloading")
        self.assertEqual(self.scan(61)["waiting_for_stable_source"], 1)
        self.assertEqual(self.scan(122)["queued"], 1)

    def test_existing_bytes_and_differing_copy_with_same_metadata_are_not_reimported(self):
        existing = self.source / "original.epub"
        epub(existing)
        self.register(existing)
        variant = self.source / "variant.epub"
        epub(variant, extra="Different bytes; do not overwrite the established edition")
        self.scan()
        result = self.scan(61)
        self.assertEqual(result["already_in_library"], 1)
        self.assertEqual(result["metadata_duplicate"], 1)
        self.assertEqual(list((self.bridge / "queue").glob("*.epub")), [])
        self.assertTrue(variant.exists())

    def test_invalid_epub_and_symlinks_never_reach_consumer(self):
        bad = self.source / "bad.epub"
        bad.write_bytes(b"An incomplete download")
        outside = self.root / "outside.epub"
        epub(outside)
        (self.source / "linked.epub").symlink_to(outside)
        self.scan()
        self.assertEqual(self.scan(61)["invalid_or_unstable"], 1)
        self.assertEqual(list((self.bridge / "queue").glob("*.epub")), [])
        self.assertEqual(bad.read_bytes(), b"An incomplete download")
        self.assertTrue(outside.exists())

    def test_consumption_without_library_acknowledgement_has_bounded_retries(self):
        book = self.source / "book.epub"
        epub(book)
        self.scan()
        self.scan(61)
        for seconds in (962, 1863):
            next((self.bridge / "queue").glob("*.epub")).unlink()
            self.assertEqual(self.scan(seconds)["queued"], 1)
        next((self.bridge / "queue").glob("*.epub")).unlink()
        self.assertEqual(self.scan(2764)["failed"], 1)
        self.assertEqual(list((self.bridge / "queue").glob("*.epub")), [])
        state = json.loads((self.bridge / "state" / "staging.json").read_text())
        checksum = hashlib.sha256(book.read_bytes()).hexdigest()
        self.assertEqual(state["ledger"][checksum]["attempts"], 3)
        self.assertTrue(book.exists())


if __name__ == "__main__":
    unittest.main()
