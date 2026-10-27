"""meta.json lifecycle tests for MemoryStore.

Every MCP server / CLI process opens the vault, and Claude Desktop starts one MCP
server per session pool. Rewriting an unchanged meta.json on every open adds a
pointless synchronous write to the startup path (a stall there delays the MCP
`initialize` reply past the client's timeout) and makes concurrent processes
contend for the same file. The store must therefore write meta.json only when it
is missing, unreadable, or differs from what would be written.
"""

from __future__ import annotations

import contextlib
import io
import json
import tempfile
import unittest
from pathlib import Path
from unittest import mock

from elesync.store import META_FILE, VAULT_SCHEMA_VERSION, MemoryStore


def _open_and_close(vault: Path) -> None:
    store = MemoryStore(vault)
    store.db.close()


def _meta_writes_during(vault: Path) -> int:
    """Open the vault once and count write_text calls that target meta.json."""
    real_write_text = Path.write_text
    meta_writes: list[Path] = []

    def spy(self: Path, *args, **kwargs):
        if self.name == META_FILE:
            meta_writes.append(self)
        return real_write_text(self, *args, **kwargs)

    with mock.patch.object(Path, "write_text", spy):
        _open_and_close(vault)
    return len(meta_writes)


class TestVaultMeta(unittest.TestCase):
    def setUp(self) -> None:
        self._tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self._tmp.cleanup)
        self.vault = Path(self._tmp.name) / "vault"
        self.meta_path = self.vault / META_FILE

    def test_fresh_vault_creates_meta_with_current_version(self) -> None:
        _open_and_close(self.vault)

        meta = json.loads(self.meta_path.read_text(encoding="utf-8"))
        self.assertEqual(meta, {"elesync_version": VAULT_SCHEMA_VERSION})

    def test_reopening_vault_with_unchanged_meta_does_not_rewrite_it(self) -> None:
        _open_and_close(self.vault)

        self.assertEqual(_meta_writes_during(self.vault), 0)

    def test_missing_meta_is_recreated(self) -> None:
        _open_and_close(self.vault)
        self.meta_path.unlink()

        self.assertEqual(_meta_writes_during(self.vault), 1)
        meta = json.loads(self.meta_path.read_text(encoding="utf-8"))
        self.assertEqual(meta["elesync_version"], VAULT_SCHEMA_VERSION)

    def test_corrupt_meta_is_repaired(self) -> None:
        _open_and_close(self.vault)
        self.meta_path.write_text("{not json", encoding="utf-8")

        self.assertEqual(_meta_writes_during(self.vault), 1)
        meta = json.loads(self.meta_path.read_text(encoding="utf-8"))
        self.assertEqual(meta, {"elesync_version": VAULT_SCHEMA_VERSION})

    def test_noncanonical_meta_is_normalized_and_keeps_unknown_keys(self) -> None:
        _open_and_close(self.vault)
        self.meta_path.write_text(
            json.dumps({"elesync_version": VAULT_SCHEMA_VERSION, "note": "keep me"}),
            encoding="utf-8",
        )

        self.assertEqual(_meta_writes_during(self.vault), 1)
        meta = json.loads(self.meta_path.read_text(encoding="utf-8"))
        self.assertEqual(meta["note"], "keep me")
        # Once canonical, the next open leaves it alone.
        self.assertEqual(_meta_writes_during(self.vault), 0)

    def test_newer_schema_version_warns_and_is_not_downgraded(self) -> None:
        _open_and_close(self.vault)
        newer = VAULT_SCHEMA_VERSION + 1
        self.meta_path.write_text(
            json.dumps({"elesync_version": newer}, indent=2) + "\n", encoding="utf-8"
        )

        stderr = io.StringIO()
        with contextlib.redirect_stderr(stderr):
            _open_and_close(self.vault)

        self.assertIn("newer EleSync version", stderr.getvalue())
        meta = json.loads(self.meta_path.read_text(encoding="utf-8"))
        self.assertEqual(meta["elesync_version"], newer)


if __name__ == "__main__":
    unittest.main()
