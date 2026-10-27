"""Encryption-at-rest tests.

The metadata/guard tests are stdlib-only (they run in CI). The full
encrypt/decrypt round-trip needs real crypto, so it's skipped unless
PyNaCl is installed.
"""

import base64
import contextlib
import io
import json
import os
import tempfile
import unittest
from pathlib import Path

from elesync import cli, crypto
from elesync.models import MemoryItem
from elesync.store import MemoryStore


def _key(vault, passphrase):
    meta = crypto.read_meta(vault)
    return crypto.derive_key(
        passphrase, base64.b64decode(meta["salt"]), meta["opslimit"], meta["memlimit"]
    )


def _run(argv, passphrase=None) -> int:
    """Run the CLI with stdout/stderr swallowed; optionally provide the passphrase
    via the env var so prompts never block."""
    if passphrase is not None:
        os.environ["ELESYNC_PASSPHRASE"] = passphrase
    try:
        with contextlib.redirect_stdout(io.StringIO()), contextlib.redirect_stderr(io.StringIO()):
            try:
                return cli.main(argv)
            except SystemExit as e:  # normalise exit() into a return code for assertions
                return e.code if isinstance(e.code, int) else 1
    finally:
        os.environ.pop("ELESYNC_PASSPHRASE", None)


class TestEncryptionMetadata(unittest.TestCase):
    """No crypto library required — detection + the no-key safety guard."""

    def test_is_encrypted_detects_marker(self):
        with tempfile.TemporaryDirectory() as d:
            self.assertFalse(crypto.is_encrypted(d))
            (Path(d) / "vault.json").write_text(json.dumps({"elesync_encryption": 1}))
            self.assertTrue(crypto.is_encrypted(d))

    def test_json_without_marker_is_not_encrypted(self):
        with tempfile.TemporaryDirectory() as d:
            (Path(d) / "vault.json").write_text(json.dumps({"something": "else"}))
            self.assertFalse(crypto.is_encrypted(d))

    def test_opening_encrypted_vault_without_key_is_refused(self):
        # Safety: the store must never silently operate (and write plaintext) on an
        # encrypted vault without a key. Needs no crypto lib — the guard is up front.
        with tempfile.TemporaryDirectory() as d:
            (Path(d) / "vault.json").write_text(json.dumps({"elesync_encryption": 1}))
            with self.assertRaises(PermissionError):
                MemoryStore(d)


@unittest.skipUnless(crypto.available(), "PyNaCl not installed")
class TestEncryptionRoundTrip(unittest.TestCase):
    PASS = "correct horse battery staple"

    def _seed(self, d):
        store = MemoryStore(d)
        for c in ("Alex lives in Westbrook", "Has a younger sister", "Runs the Helios project"):
            store.upsert(MemoryItem(content=c, source="claude", type="fact"))
        store.close()

    def test_encrypt_then_decrypt_restores_everything(self):
        with tempfile.TemporaryDirectory() as d:
            self._seed(d)
            notes = Path(d) / "notes"
            self.assertEqual(len(list(notes.glob("*.md"))), 3)

            self.assertEqual(_run(["--vault", d, "encrypt"], self.PASS), 0)
            # at rest: only ciphertext — no plaintext notes, no plaintext index
            self.assertEqual(len(list(notes.glob("*.md"))), 0)
            self.assertEqual(len(list(notes.glob("*.md.enc"))), 3)
            self.assertFalse((Path(d) / "index.db").exists())
            self.assertTrue(crypto.is_encrypted(d))

            self.assertEqual(_run(["--vault", d, "decrypt"], self.PASS), 0)
            self.assertEqual(len(list(notes.glob("*.md"))), 3)
            self.assertEqual(len(list(notes.glob("*.md.enc"))), 0)
            self.assertFalse(crypto.is_encrypted(d))

            # index rebuilt, content intact, searchable again
            store = MemoryStore(d)
            self.assertEqual(store.count(), 3)
            self.assertEqual(len(store.search("Helios")), 1)
            store.close()

    def test_wrong_passphrase_changes_nothing(self):
        with tempfile.TemporaryDirectory() as d:
            self._seed(d)
            self.assertEqual(_run(["--vault", d, "encrypt"], self.PASS), 0)
            before = sorted(p.read_bytes() for p in (Path(d) / "notes").glob("*.md.enc"))

            self.assertEqual(_run(["--vault", d, "decrypt"], "the wrong passphrase"), 1)

            self.assertTrue(crypto.is_encrypted(d))  # still encrypted
            self.assertEqual(len(list((Path(d) / "notes").glob("*.md"))), 0)  # nothing leaked
            after = sorted(p.read_bytes() for p in (Path(d) / "notes").glob("*.md.enc"))
            self.assertEqual(before, after)  # ciphertext untouched

    def test_project_manifest_is_encrypted_and_restored(self):
        with tempfile.TemporaryDirectory() as d:
            store = MemoryStore(d)
            memory = MemoryItem(content="Private acquisition plan", source="claude")
            store.upsert(memory)
            project = store.create_project("Private project", "Sensitive planning")
            store.add_to_project(project["id"], memory.id)
            store.close()

            self.assertEqual(_run(["--vault", d, "encrypt"], self.PASS), 0)
            vault = Path(d)
            self.assertFalse((vault / "projects.json").exists())
            encrypted = vault / "projects.json.enc"
            self.assertTrue(encrypted.exists())
            self.assertNotIn(b"Private project", encrypted.read_bytes())

            locked = MemoryStore(d, key=_key(d, self.PASS))
            self.assertEqual(locked.project(project["id"])["name"], "Private project")
            self.assertEqual(locked.project_memories(project["id"])[0].id, memory.id)
            locked.close()

            self.assertEqual(_run(["--vault", d, "decrypt"], self.PASS), 0)
            self.assertTrue((vault / "projects.json").exists())
            self.assertFalse(encrypted.exists())
            reopened = MemoryStore(d)
            self.assertEqual(reopened.project(project["id"])["name"], "Private project")
            reopened.close()

    def test_crypto_primitives(self):
        from nacl import pwhash
        from nacl.exceptions import CryptoError

        # Fast limits for the unit test (the CLI uses MODERATE in practice).
        ops, mem = pwhash.argon2id.OPSLIMIT_INTERACTIVE, pwhash.argon2id.MEMLIMIT_INTERACTIVE
        salt = crypto.new_salt()
        key = crypto.derive_key("pw", salt, ops, mem)
        self.assertEqual(key, crypto.derive_key("pw", salt, ops, mem))  # deterministic
        other = crypto.derive_key("different", salt, ops, mem)
        self.assertNotEqual(key, other)
        blob = crypto.encrypt(key, b"secret bytes")
        self.assertNotIn(b"secret bytes", blob)  # actually encrypted
        self.assertEqual(crypto.decrypt(key, blob), b"secret bytes")
        with self.assertRaises(CryptoError):
            crypto.decrypt(other, blob)  # wrong key fails auth


@unittest.skipUnless(crypto.available(), "PyNaCl not installed")
class TestLockedModeServe(unittest.TestCase):
    """Phase 2 — use an encrypted vault live, without ever decrypting it to disk."""

    PASS = "correct horse battery staple"

    def _seed_and_encrypt(self, d):
        store = MemoryStore(d)
        for c in ("Alex lives in Westbrook", "Has a younger sister", "Runs the Helios project"):
            store.upsert(MemoryItem(content=c, source="claude", type="fact"))
        store.close()
        _run(["--vault", d, "encrypt"], self.PASS)

    def test_search_and_write_without_decrypting(self):
        with tempfile.TemporaryDirectory() as d:
            self._seed_and_encrypt(d)
            notes = Path(d) / "notes"

            store = MemoryStore(d, key=_key(d, self.PASS))  # locked mode
            self.assertTrue(store.encrypted)
            self.assertEqual(store.db_path, ":memory:")  # index never on disk
            self.assertEqual(store.count(), 3)  # decrypted into memory
            self.assertEqual(len(store.search("Helios")), 1)  # queryable live
            store.upsert(MemoryItem(content="Enjoys hiking on weekends", type="preference"))
            self.assertEqual(store.count(), 4)
            store.close()

            # Nothing plaintext ever hit the disk:
            self.assertEqual(len(list(notes.glob("*.md"))), 0)
            self.assertEqual(len(list(notes.glob("*.md.enc"))), 4)  # new memory encrypted too
            self.assertFalse((Path(d) / "index.db").exists())

            # The new memory persisted (encrypted) and is searchable on reopen:
            store2 = MemoryStore(d, key=_key(d, self.PASS))
            self.assertEqual(store2.count(), 4)
            self.assertEqual(len(store2.search("hiking")), 1)
            store2.close()

    def test_cli_unlocks_with_passphrase_and_rejects_wrong_one(self):
        with tempfile.TemporaryDirectory() as d:
            self._seed_and_encrypt(d)
            self.assertEqual(_run(["--vault", d, "search", "Helios"], self.PASS), 0)  # unlocks
            self.assertEqual(_run(["--vault", d, "search", "Helios"], "nope"), 1)  # wrong pass
            # the searches left no plaintext behind
            self.assertEqual(len(list((Path(d) / "notes").glob("*.md"))), 0)
            self.assertFalse((Path(d) / "index.db").exists())


if __name__ == "__main__":
    unittest.main()
