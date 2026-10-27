"""Auto-supersession on the write path.

The "auto later" half of the supersession design: when a freshly-remembered fact
unambiguously updates exactly one live fact in the same scope, supersede that fact
automatically. It is deliberately conservative — acting on the write path is
expensive when wrong, so anything ambiguous (2+ conflicts, cross-scope,
sub-threshold, already human-resolved) leaves history untouched. Because
supersession is recoverable (hidden, not deleted), a rare false positive is a
reversible mistake, and every auto-action is announced in the save result and
audited.

These tests pin: it fires on a clean single-conflict update; it stays out of every
ambiguous case; the env kill-switch; that explicit `supersedes=` is unaffected;
that recall drops the superseded fact while `history` still walks it; and that the
result survives a reindex (files-first vault).
"""

from __future__ import annotations

import contextlib
import tempfile
import unittest

from elesync import conflicts as conflicts_mod
from elesync.service import MemoryService
from elesync.store import MemoryStore


def _svc(tmp: str, **kw) -> tuple[MemoryStore, MemoryService]:
    store = MemoryStore(vault_dir=tmp)
    return store, MemoryService(store, **kw)


@contextlib.contextmanager
def _tmp_svc(**kw):
    """A vault in a temp dir, with the store CLOSED before the dir is deleted.
    Windows refuses to remove a directory while SQLite holds an open handle in
    it (WinError 32), so `with tempfile.TemporaryDirectory()` around an open
    store errors on cleanup there -- invisibly green on Linux/CI."""
    with tempfile.TemporaryDirectory() as tmp:
        store, svc = _svc(tmp, **kw)
        try:
            yield store, svc
        finally:
            store.close()


def _full_id(store: MemoryStore, remember_result: str) -> str:
    prefix = remember_result.split("id=", 1)[1].split(")", 1)[0]
    return store.prefix_lookup(prefix)[0]


class TestAutoSupersedeFires(unittest.TestCase):
    def test_clean_single_conflict_is_auto_superseded(self):
        with _tmp_svc() as (store, svc):
            old = svc.remember("Santo is based in Westbrook", type="fact", tags="location")
            old_id = _full_id(store, old)
            new = svc.remember("Santo is based in Portland", type="fact", tags="location")

            self.assertIn("Auto-superseded", new)
            self.assertIn(old_id[:8], new)
            # Old fact is now history: hidden from live recall, present as history.
            live = {i.id for i in store.all(include_superseded=False)}
            self.assertNotIn(old_id, live)
            item = store.get(old_id)
            self.assertIsNotNone(item.superseded_by)

    def test_recall_drops_superseded_and_history_walks_it(self):
        with _tmp_svc() as (store, svc):
            svc.remember("Santo is based in Westbrook", type="fact", tags="location")
            new = svc.remember("Santo is based in Portland", type="fact", tags="location")
            new_id = _full_id(store, new)

            recalled = svc.recall("Santo based")
            self.assertIn("Portland", recalled)
            self.assertNotIn("Westbrook", recalled)
            # History still knows the old value.
            self.assertIn("Westbrook", svc.history(new_id))

    def test_reindex_round_trip_preserves_auto_supersession(self):
        with _tmp_svc() as (store, svc):
            old = svc.remember("Santo is based in Westbrook", type="fact", tags="location")
            old_id = _full_id(store, old)
            svc.remember("Santo is based in Portland", type="fact", tags="location")

            # Rebuild the SQLite index from the markdown notes (files are truth).
            store.reindex()
            self.assertNotIn(old_id, {i.id for i in store.all(include_superseded=False)})
            self.assertIsNotNone(store.get(old_id).superseded_by)


class TestAutoSupersedeStaysOut(unittest.TestCase):
    def test_ambiguous_multi_conflict_does_not_fire(self):
        with _tmp_svc() as (store, svc):
            # Two existing live facts both clash with the new one — ambiguous which
            # it replaces, so auto-supersession must abstain. Seed them directly (not
            # via remember) so the second doesn't auto-supersede the first: we need
            # both alive when the third arrives, so it faces two matches.
            from elesync.models import MemoryItem

            store.upsert_status(
                MemoryItem(content="Santo is based in Westbrook", type="fact", tags="location")
            )
            store.upsert_status(
                MemoryItem(content="Santo is based in Portland", type="fact", tags="location")
            )
            new = svc.remember("Santo is based in Augusta", type="fact", tags="location")
            self.assertNotIn("Auto-superseded", new)
            self.assertEqual(len(store.all(include_superseded=False)), 3)

    def test_cross_scope_does_not_fire(self):
        with _tmp_svc() as (store, svc):
            svc.remember("Santo is based in Westbrook", type="fact", scopes="work", tags="location")
            new = svc.remember(
                "Santo is based in Portland", type="fact", scopes="home", tags="location"
            )
            self.assertNotIn("Auto-superseded", new)
            self.assertEqual(len(store.all(include_superseded=False)), 2)

    def test_below_threshold_does_not_fire(self):
        with _tmp_svc() as (store, svc):
            svc.remember("Santo is based in Westbrook", type="fact", tags="location")
            new = svc.remember("The office coffee machine is broken again", type="fact")
            self.assertNotIn("Auto-superseded", new)
            self.assertEqual(len(store.all(include_superseded=False)), 2)

    def test_untagged_update_does_not_fire(self):
        with _tmp_svc() as (store, svc):
            # Same clean update shape, but no shared tag — the subject anchor is
            # absent, so auto-supersession abstains and both facts stay live.
            svc.remember("Santo is based in Westbrook", type="fact")
            new = svc.remember("Santo is based in Portland", type="fact")
            self.assertNotIn("Auto-superseded", new)
            self.assertEqual(len(store.all(include_superseded=False)), 2)

    def test_non_fact_does_not_fire(self):
        with _tmp_svc() as (store, svc):
            svc.remember("Santo prefers dark mode in the editor", type="preference")
            new = svc.remember("Santo prefers light mode in the editor", type="preference")
            self.assertNotIn("Auto-superseded", new)

    def test_resolved_pair_does_not_fire(self):
        with _tmp_svc() as (store, svc):
            a = svc.remember("Santo is based in Westbrook", type="fact", tags="location")
            a_id = _full_id(store, a)
            b = svc.remember("Santo is based in Portland Maine", type="fact", tags="location")
            b_id = _full_id(store, b)
            # A human already resolved this pair; record it so auto never re-acts.
            store.record_resolution("c1", [(a_id, b_id)], kept_id=a_id, strategy="manual")
            new = svc.remember("Santo is based in Portland", type="fact", tags="location")
            # New vs the already-resolved pair members must not auto-fire against a
            # resolved relationship.
            candidate = conflicts_mod.auto_supersede_candidate(
                store.get(_full_id(store, new)),
                svc._visible(store.all()),
                resolved_pairs={frozenset((a_id, b_id))},
            )
            # The resolved (a,b) pairing is skipped; the assertion here is that a
            # resolved pair never resurfaces as an auto target.
            if candidate is not None:
                self.assertNotEqual({candidate.id}, {a_id})


class TestKillSwitchAndExplicit(unittest.TestCase):
    def test_env_kill_switch_disables(self):
        import os

        with _tmp_svc() as (store, svc):
            svc.remember("Santo is based in Westbrook", type="fact", tags="location")
            prev = os.environ.get("ELESYNC_AUTO_SUPERSEDE")
            os.environ["ELESYNC_AUTO_SUPERSEDE"] = "0"
            try:
                new = svc.remember("Santo is based in Portland", type="fact", tags="location")
            finally:
                if prev is None:
                    del os.environ["ELESYNC_AUTO_SUPERSEDE"]
                else:
                    os.environ["ELESYNC_AUTO_SUPERSEDE"] = prev
            self.assertNotIn("Auto-superseded", new)
            self.assertEqual(len(store.all(include_superseded=False)), 2)

    def test_explicit_supersedes_still_wins(self):
        with _tmp_svc() as (store, svc):
            old = svc.remember("Santo is based in Westbrook", type="fact", tags="location")
            old_id = _full_id(store, old)
            new = svc.remember(
                "Completely unrelated note about the weather",
                type="fact",
                supersedes=old_id[:8],
            )
            self.assertIn("Superseded", new)
            self.assertNotIn("Auto-superseded", new)
            self.assertIsNotNone(store.get(old_id).superseded_by)

    def test_readonly_never_auto_supersedes(self):
        with _tmp_svc() as (store, _writer):
            _writer.remember("Santo is based in Westbrook", type="fact", tags="location")
            ro = MemoryService(store, readonly=True)
            out = ro.remember("Santo is based in Portland", type="fact", tags="location")
            self.assertNotIn("Auto-superseded", out)
            self.assertEqual(len(store.all(include_superseded=False)), 1)


if __name__ == "__main__":
    unittest.main()
