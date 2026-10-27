"""Versioned backup archive format (#91).

`ele backup` now writes a compressed, optionally-encrypted archive (`.eleb`) instead
of raw JSON, while `ele restore` still reads legacy plain-JSON backups/exports. These
tests pin: the archive round-trips losslessly, the plaintext header is readable without
a passphrase, encryption is opt-in and actually conceals content, a wrong passphrase
fails cleanly, an unsupported format version is rejected, and older JSON still restores.
"""

from __future__ import annotations

import gzip
import json
import os
import struct
import tempfile
import unittest
from pathlib import Path
from unittest import mock

from elesync import archive, cli, crypto
from elesync.service import MemoryService
from elesync.store import MemoryStore


def _seed(path: str) -> None:
    svc = MemoryService(MemoryStore(path))
    try:
        svc.remember("Based in Portland", type="fact", tags="loc")
        svc.remember("Prefers tea", type="preference")
    finally:
        svc.store.close()


def _total(path: str) -> int:
    store = MemoryStore(path)
    try:
        return store.stats()["total"]
    finally:
        store.close()


class TestArchiveRoundTrip(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.src = str(Path(self.tmp.name) / "src")
        _seed(self.src)

    def tearDown(self):
        self.tmp.cleanup()

    def test_default_backup_extension_is_eleb(self):
        self.assertTrue(cli._default_backup_name().endswith(".eleb"))

    def test_backup_writes_archive_and_restores_losslessly(self):
        bpath = str(Path(self.tmp.name) / "b.eleb")
        self.assertEqual(cli.main(["--vault", self.src, "backup", bpath]), 0)

        self.assertTrue(archive.is_archive(bpath))
        header = archive.header_of(bpath)  # readable with no key
        self.assertEqual(header["format"], "elesync-backup")
        self.assertEqual(header["format_version"], archive.FORMAT_VERSION)
        self.assertEqual(header["compression"], "gzip")
        self.assertIsNone(header["encryption"])
        self.assertEqual(header["count"], 2)

        dst = str(Path(self.tmp.name) / "dst")
        self.assertEqual(cli.main(["--vault", dst, "restore", bpath]), 0)
        self.assertEqual(_total(dst), 2)

    def test_restore_is_idempotent(self):
        bpath = str(Path(self.tmp.name) / "b.eleb")
        cli.main(["--vault", self.src, "backup", bpath])
        dst = str(Path(self.tmp.name) / "dst")
        self.assertEqual(cli.main(["--vault", dst, "restore", bpath]), 0)
        self.assertEqual(cli.main(["--vault", dst, "restore", bpath]), 0)  # re-run
        self.assertEqual(_total(dst), 2)  # no duplicates

    def test_legacy_json_export_still_restores(self):
        jpath = str(Path(self.tmp.name) / "legacy.json")
        self.assertEqual(cli.main(["--vault", self.src, "export", jpath]), 0)
        self.assertFalse(archive.is_archive(jpath))  # plain JSON, no magic
        dst = str(Path(self.tmp.name) / "dst")
        self.assertEqual(cli.main(["--vault", dst, "restore", jpath]), 0)
        self.assertEqual(_total(dst), 2)

    def test_unsupported_format_version_is_rejected(self):
        bpath = Path(self.tmp.name) / "bad.eleb"
        header = json.dumps(
            {"format": "elesync-backup", "format_version": 999, "encryption": None}
        ).encode("utf-8")
        with open(bpath, "wb") as fh:
            fh.write(archive.MAGIC)
            fh.write(struct.pack(">I", len(header)))
            fh.write(header)
            fh.write(b"body")
        with self.assertRaises(archive.ArchiveError):
            archive.header_of(bpath)
        dst = str(Path(self.tmp.name) / "dst")
        # restore surfaces it as a clean non-zero exit, not a traceback
        self.assertEqual(cli.main(["--vault", dst, "restore", str(bpath)]), 1)

    def test_oversized_header_is_rejected_before_allocation(self):
        bpath = Path(self.tmp.name) / "huge-header.eleb"
        with open(bpath, "wb") as fh:
            fh.write(archive.MAGIC)
            fh.write(struct.pack(">I", archive.MAX_HEADER_BYTES + 1))
        with self.assertRaisesRegex(archive.ArchiveError, "header exceeds"):
            archive.header_of(bpath)

    def test_compression_bomb_is_rejected(self):
        bpath = Path(self.tmp.name) / "bomb.eleb"
        header = json.dumps(
            {"format": "elesync-backup", "format_version": 1, "encryption": None}
        ).encode()
        body = gzip.compress(b"x" * 1024)
        with open(bpath, "wb") as fh:
            fh.write(archive.MAGIC)
            fh.write(struct.pack(">I", len(header)))
            fh.write(header)
            fh.write(body)
        with mock.patch.object(archive, "MAX_DECOMPRESSED_BYTES", 128):
            with self.assertRaisesRegex(archive.ArchiveError, "expands beyond"):
                archive.read(bpath)

    def test_encrypt_flag_without_extra_is_guarded(self):
        # Only meaningful when the extra is ABSENT; skip if it happens to be installed.
        if crypto.available():
            self.skipTest("PyNaCl is installed")
        bpath = str(Path(self.tmp.name) / "e.eleb")
        self.assertEqual(cli.main(["--vault", self.src, "backup", "--encrypt", bpath]), 2)


@unittest.skipUnless(crypto.available(), "PyNaCl not installed")
class TestEncryptedArchive(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.src = str(Path(self.tmp.name) / "src")
        _seed(self.src)
        self._prev = os.environ.get("ELESYNC_PASSPHRASE")
        os.environ["ELESYNC_PASSPHRASE"] = "correct horse battery staple"

    def tearDown(self):
        if self._prev is None:
            os.environ.pop("ELESYNC_PASSPHRASE", None)
        else:
            os.environ["ELESYNC_PASSPHRASE"] = self._prev
        self.tmp.cleanup()

    def test_encrypted_round_trip(self):
        bpath = str(Path(self.tmp.name) / "enc.eleb")
        self.assertEqual(cli.main(["--vault", self.src, "backup", "--encrypt", bpath]), 0)

        header = archive.header_of(bpath)  # metadata still readable without the passphrase
        self.assertIsNotNone(header["encryption"])
        self.assertEqual(header["encryption"]["cipher"], "xsalsa20poly1305")
        self.assertEqual(header["encryption"]["kdf"], "argon2id")
        self.assertEqual(header["count"], 2)

        dst = str(Path(self.tmp.name) / "dst")
        self.assertEqual(cli.main(["--vault", dst, "restore", bpath]), 0)
        self.assertEqual(_total(dst), 2)

    def test_body_does_not_leak_plaintext(self):
        bpath = Path(self.tmp.name) / "enc.eleb"
        cli.main(["--vault", self.src, "backup", "--encrypt", str(bpath)])
        raw = bpath.read_bytes()
        self.assertNotIn(b"Portland", raw)  # content is encrypted, not just compressed

    def test_wrong_passphrase_fails_cleanly(self):
        bpath = str(Path(self.tmp.name) / "enc.eleb")
        self.assertEqual(cli.main(["--vault", self.src, "backup", "--encrypt", bpath]), 0)
        os.environ["ELESYNC_PASSPHRASE"] = "the wrong passphrase"
        dst = str(Path(self.tmp.name) / "dst")
        self.assertEqual(cli.main(["--vault", dst, "restore", bpath]), 1)
        # nothing was written into the destination vault
        self.assertEqual(_total(dst), 0)


if __name__ == "__main__":
    unittest.main()
