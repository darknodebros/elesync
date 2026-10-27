"""Disaster-recovery tests for MemoryStore.

EleSync's durability promise rests on one invariant: the markdown notes are the
source of truth, and the SQLite index (`index.db`) is a rebuildable cache. These
tests deliberately damage the index / notes and assert the vault recovers with no
loss — the scenarios a user actually hits after a crash, a bad sync, or a corrupt
cache. They also cover large-scale rebuild, so recovery isn't only proven on toy
vaults.
"""

from __future__ import annotations

import tempfile
import unittest
from pathlib import Path

from elesync.models import MemoryItem
from elesync.store import MemoryStore


def _item(content: str, source: str = "claude", type: str = "note") -> MemoryItem:
    return MemoryItem(content=content, source=source, type=type)


class RecoveryTestCase(unittest.TestCase):
    def setUp(self):
        self._tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self._tmp.cleanup)
        self.vault = Path(self._tmp.name)

    def _open(self) -> MemoryStore:
        store = MemoryStore(self.vault)
        self.addCleanup(store.close)
        return store

    # -- index loss / corruption ------------------------------------------

    def test_deleted_index_rebuilds_from_notes(self):
        """The whole point of "notes are truth": delete index.db entirely, reopen,
        reindex — every memory comes back, content intact."""
        store = self._open()
        for i in range(30):
            store.upsert(_item(f"recover me {i}", type="fact"))
        before = {i.id: i.content for i in store.all()}
        store.close()

        (self.vault / "index.db").unlink()  # simulate a lost/deleted cache
        self.assertFalse((self.vault / "index.db").exists())

        store2 = self._open()
        self.assertEqual(store2.count(), 0)  # fresh empty index after loss
        res = store2.reindex()  # rebuild from the markdown that survived
        self.assertEqual(res["reindexed"], 30)
        after = {i.id: i.content for i in store2.all()}
        self.assertEqual(after, before)  # ids AND content fully restored

    def test_corrupt_index_recovered_by_delete_and_reindex(self):
        """A garbage index.db is the documented "rebuild the cache" recovery: remove
        the corrupt file, reopen, reindex. Notes are untouched, so nothing is lost."""
        store = self._open()
        for i in range(15):
            store.upsert(_item(f"corrupt-case {i}"))
        store.close()

        (self.vault / "index.db").write_bytes(b"\x00\x01 not a database \xff" * 64)

        # Recovery procedure: drop the corrupt cache and rebuild from notes.
        (self.vault / "index.db").unlink()
        store2 = self._open()
        store2.reindex()
        self.assertEqual(store2.count(), 15)

    # -- damaged notes -----------------------------------------------------

    def test_unparseable_note_is_skipped_not_fatal(self):
        """One corrupt/garbage note in notes/ must not abort the whole rebuild —
        the good notes still index, the bad one is skipped."""
        store = self._open()
        for i in range(10):
            store.upsert(_item(f"healthy note {i}"))
        store.close()

        # Drop a file that looks like a note but isn't parseable as one.
        (self.vault / "notes" / "garbage.md").write_bytes(b"\xff\xfe not valid frontmatter \x00")

        store2 = self._open()
        res = store2.reindex()  # must not raise
        self.assertEqual(res["reindexed"], 10)  # 10 good, garbage skipped
        self.assertEqual(store2.count(), 10)

    # -- index / notes drift ----------------------------------------------

    def test_sync_reconciles_when_a_note_is_removed_on_disk(self):
        """A note deleted directly on disk (bad merge, manual edit) leaves the index
        ahead of the truth. sync() rebuilds from notes and reports the removal."""
        store = self._open()
        ids = [store.upsert(_item(f"drift {i}", type="fact")) for i in range(12)]
        victim = ids[0]
        store.close()

        (self.vault / "notes" / f"{victim}.md").unlink()  # note vanishes on disk

        store2 = self._open()
        res = store2.sync()
        self.assertEqual(res["removed"], 1)
        self.assertEqual(res["total"], 11)
        self.assertIsNone(store2.get(victim))  # the orphaned index row is gone too

    # -- large-scale -------------------------------------------------------

    def test_large_scale_rebuild_is_lossless(self):
        """Recovery must hold at scale, not just on a handful of notes: import a
        large corpus, destroy the index, rebuild, and prove every memory returned."""
        n = 500
        store = self._open()
        store.import_many([_item(f"bulk memory {i} topic {i % 40}", type="fact") for i in range(n)])
        self.assertEqual(store.count(), n)
        before = {i.id: i.content for i in store.all()}
        store.close()

        (self.vault / "index.db").unlink()
        store2 = self._open()
        res = store2.reindex()
        self.assertEqual(res["reindexed"], n)
        after = {i.id: i.content for i in store2.all()}
        self.assertEqual(after, before)
        # And the rebuilt index is actually searchable, not just populated.
        self.assertTrue(store2.search("topic 7", limit=5))


if __name__ == "__main__":
    unittest.main()
