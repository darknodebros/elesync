"""Supersession: the first step toward a temporal model.

A memory can be marked as *superseded by* a newer one — retained as recoverable
history but hidden from live recall, with the two linked so `history` can answer
"what was true when". Supersession enters through two paths:

  * `remember(..., supersedes=<id>)` — an explicit "this replaces that".
  * `resolve_conflict(...)` — the losers are superseded by the kept memory
    rather than destroyed, so resolution becomes recoverable history.

These tests pin both paths, the markdown round-trip (the vault is files-first,
so supersession must survive a reindex), and the scope-safety of the history view.
"""

from __future__ import annotations

import json
import tempfile
import unittest
from pathlib import Path

from elesync import conflicts as conflicts_mod
from elesync.models import MemoryItem
from elesync.service import MemoryService
from elesync.store import MemoryStore


def _svc(tmp: str, **kw) -> tuple[MemoryStore, MemoryService]:
    store = MemoryStore(vault_dir=tmp)
    return store, MemoryService(store, **kw)


def _full_id(store: MemoryStore, remember_result: str) -> str:
    # "Saved (id=1bcfa0ec)...." -> the full 32-char id (store.get needs a full id;
    # the printed value is only an 8-char prefix).
    prefix = remember_result.split("id=", 1)[1].split(")", 1)[0]
    return store.prefix_lookup(prefix)[0]


class TestModelRoundTrip(unittest.TestCase):
    def test_superseded_item_round_trips_through_markdown(self):
        it = MemoryItem(
            content="User lives in Seattle",
            superseded_by="abc123",
            superseded_at="2026-01-02T00:00:00+00:00",
        )
        md = it.to_markdown()
        self.assertIn("superseded_by: abc123", md)
        self.assertIn("superseded_at: 2026-01-02T00:00:00+00:00", md)
        back = MemoryItem.from_markdown(md)
        self.assertEqual(back.superseded_by, "abc123")
        self.assertEqual(back.superseded_at, it.superseded_at)

    def test_live_item_omits_supersession_frontmatter(self):
        # A live note's front-matter must be unchanged from earlier versions, so
        # upgrading doesn't churn every note in the vault.
        md = MemoryItem(content="User lives in Boston").to_markdown()
        self.assertNotIn("superseded_by", md)
        self.assertNotIn("superseded_at", md)


class TestRememberSupersedes(unittest.TestCase):
    def test_supersede_hides_old_and_keeps_new_live(self):
        with tempfile.TemporaryDirectory() as d:
            store, svc = _svc(d)
            try:
                old = _full_id(
                    store, svc.remember("User lives in Boston", type="fact", source="claude")
                )
                res = svc.remember(
                    "User lives in Seattle", type="fact", source="claude", supersedes=old
                )
                self.assertIn(f"Superseded id={old[:8]}", res)

                live = svc.recall("where does the user live")
                self.assertIn("Seattle", live)
                self.assertNotIn("Boston", live)

                # Retained, not deleted.
                self.assertIsNotNone(store.get(old))
                self.assertTrue(store.get(old).superseded_by)
                # Hidden from the live view, present in the full view.
                self.assertNotIn(old, [i.id for i in store.all()])
                self.assertIn(old, [i.id for i in store.all(include_superseded=True)])
            finally:
                store.close()

    def test_self_supersede_is_ignored(self):
        with tempfile.TemporaryDirectory() as d:
            store, svc = _svc(d)
            try:
                new = _full_id(store, svc.remember("A fact", type="fact"))
                # Feeding the just-created id as `supersedes` must not tombstone it.
                msg = svc._supersede(new, new)
                self.assertIn("can't supersede itself", msg)
                self.assertFalse(store.get(new).superseded_by)
            finally:
                store.close()

    def test_bad_supersedes_ref_still_saves_the_memory(self):
        with tempfile.TemporaryDirectory() as d:
            store, svc = _svc(d)
            try:
                res = svc.remember("A new fact", type="fact", supersedes="doesnotexist")
                self.assertIn("Saved", res)
                self.assertIn("couldn't supersede", res)
                self.assertEqual(len(store.all()), 1)
            finally:
                store.close()


class TestHistory(unittest.TestCase):
    def test_history_shows_chain_oldest_first(self):
        with tempfile.TemporaryDirectory() as d:
            store, svc = _svc(d)
            try:
                a = _full_id(
                    store, svc.remember("Codename is Helios", type="project", source="claude")
                )
                b = _full_id(
                    store,
                    svc.remember(
                        "Codename is Aurora", type="project", source="claude", supersedes=a
                    ),
                )
                out = svc.history(a)
                self.assertIn("2 versions", out)
                # Oldest first: Helios (superseded) appears before Aurora (current).
                self.assertLess(out.index("Helios"), out.index("Aurora"))
                self.assertIn("current", out)
                self.assertIn(b[:8], out)
            finally:
                store.close()

    def test_history_of_a_lone_memory_reports_no_history(self):
        with tempfile.TemporaryDirectory() as d:
            store, svc = _svc(d)
            try:
                a = _full_id(store, svc.remember("Only version", type="fact"))
                out = svc.history(a)
                self.assertIn("no supersession history", out)
            finally:
                store.close()


class TestReindexRoundTrip(unittest.TestCase):
    def test_reindex_preserves_supersession(self):
        # Regression: _insert originally omitted the supersession columns, so a
        # rebuild from notes/*.md silently resurrected superseded facts as live.
        with tempfile.TemporaryDirectory() as d:
            store, svc = _svc(d)
            try:
                a = _full_id(store, svc.remember("Codename is Helios", type="project"))
                svc.remember("Codename is Aurora", type="project", supersedes=a)
                store.reindex()
                superseded = [i for i in store.all(include_superseded=True) if i.superseded_by]
                self.assertEqual(len(superseded), 1)
                self.assertEqual(superseded[0].content, "Codename is Helios")
                self.assertIn("Aurora", svc.recall("codename"))
                self.assertNotIn("Helios", svc.recall("codename"))
            finally:
                store.close()


class TestResolveConflictSupersedes(unittest.TestCase):
    def test_losers_are_superseded_not_deleted(self):
        with tempfile.TemporaryDirectory() as d:
            store, svc = _svc(d)
            try:
                keep = _full_id(
                    store, svc.remember("Based in Westbrook", type="fact", source="claude")
                )
                lose = _full_id(
                    store, svc.remember("Based in Portland", type="fact", source="chatgpt")
                )

                clusters = conflicts_mod.find_conflicts(store.all())
                self.assertEqual(len(clusters), 1)
                cid = conflicts_mod.cluster_id(clusters[0])

                res = svc.resolve_conflict(cid, choose_id=keep[:8])
                self.assertIn("superseded", res)
                self.assertIn("kept as history", res)

                # Loser retained as history, linked to the kept memory.
                loser = store.get(lose)
                self.assertIsNotNone(loser)
                self.assertEqual(loser.superseded_by, keep)
                # Gone from the live view and no longer an open conflict.
                self.assertNotIn(lose, [i.id for i in store.all()])
                self.assertEqual(svc.conflicts(), "No conflicting memories found.")
                # Recoverable through history.
                self.assertIn("Portland", svc.history(keep))
            finally:
                store.close()


class TestStatusAndExport(unittest.TestCase):
    def test_status_reports_superseded_count(self):
        with tempfile.TemporaryDirectory() as d:
            store, svc = _svc(d)
            try:
                a = _full_id(store, svc.remember("Old fact", type="fact"))
                svc.remember("New fact", type="fact", supersedes=a)
                status = svc.status()
                self.assertIn("1 memories", status)  # live count excludes superseded
                self.assertIn("1 superseded", status)
            finally:
                store.close()

    def test_export_includes_superseded_history(self):
        with tempfile.TemporaryDirectory() as d:
            store, svc = _svc(d)
            try:
                a = _full_id(store, svc.remember("Old fact", type="fact"))
                svc.remember("New fact", type="fact", supersedes=a)
                # cmd_export uses store.all(include_superseded=True); assert that
                # contract directly so history is never dropped on export.
                exported = store.all(include_superseded=True)
                self.assertEqual(len(exported), 2)
                path = Path(d) / "export.json"
                payload = {"memories": [i.to_dict() for i in exported]}
                path.write_text(json.dumps(payload), encoding="utf-8")
                back = json.loads(path.read_text(encoding="utf-8"))
                superseded = [m for m in back["memories"] if m.get("superseded_by")]
                self.assertEqual(len(superseded), 1)
            finally:
                store.close()


if __name__ == "__main__":
    unittest.main()
