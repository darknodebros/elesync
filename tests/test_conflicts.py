"""Provenance & conflict-detection tests — stdlib only."""

import tempfile
import unittest

from elesync import conflicts
from elesync.models import MemoryItem
from elesync.service import MemoryService
from elesync.store import MemoryStore


def F(content, source="claude", type="fact"):
    return MemoryItem(content=content, source=source, type=type)


class TestFindConflicts(unittest.TestCase):
    def test_detects_location_conflict_across_sources(self):
        items = [
            F("Based in Westbrook", "claude"),
            F("Based in Portland", "chatgpt"),
            F("Likes hiking on weekends", "gemini", type="preference"),
        ]
        clusters = conflicts.find_conflicts(items)
        self.assertEqual(len(clusters), 1)
        self.assertEqual(
            {i.content for i in clusters[0]}, {"Based in Westbrook", "Based in Portland"}
        )

    def test_unrelated_facts_not_flagged(self):
        items = [F("Based in Westbrook"), F("Has a younger sister"), F("Works as a teacher")]
        self.assertEqual(conflicts.find_conflicts(items), [])

    def test_same_fact_from_two_sources_is_agreement(self):
        items = [F("Based in Westbrook", "claude"), F("Based in Westbrook", "chatgpt")]
        self.assertEqual(conflicts.find_conflicts(items), [])

    def test_identical_duplicate_not_flagged(self):
        items = [F("Based in Westbrook", "claude"), F("Based in Westbrook", "claude")]
        self.assertEqual(conflicts.find_conflicts(items), [])

    def test_only_facts_considered_by_default(self):
        # Similar-but-different preferences can coexist — they're not a contradiction.
        items = [
            F("Prefers dark roast coffee", type="preference"),
            F("Prefers light roast coffee", type="preference"),
        ]
        self.assertEqual(conflicts.find_conflicts(items), [])

    def test_three_way_cluster_merges(self):
        items = [
            F("User's name is Alex", "claude"),
            F("User's name is Sam", "chatgpt"),
            F("User's name is Jordan", "gemini"),
        ]
        clusters = conflicts.find_conflicts(items)
        self.assertEqual(len(clusters), 1)
        self.assertEqual(len(clusters[0]), 3)


class TestWholeVaultScanCost(unittest.TestCase):
    """The dashboard's Home load waited ~20 s on an 850-memory vault because the
    whole-vault scan re-tokenised both texts of every pair, on every load."""

    def setUp(self):
        conflicts._scan_cache.clear()

    def _count_calls(self, name):
        calls = []
        real = getattr(conflicts, name)

        def counting(*args):
            calls.append(1)
            return real(*args)

        setattr(conflicts, name, counting)
        self.addCleanup(setattr, conflicts, name, real)
        return calls

    def _facts(self, n):
        # Unrelated filler: one shared word in seven keeps every pair far below 0.5.
        return [F(f"note {i}a {i}b {i}c") for i in range(n)]

    def test_scan_tokenises_each_memory_once(self):
        calls = self._count_calls("_tokens")
        conflicts.find_conflicts(self._facts(30))
        self.assertEqual(len(calls), 30)

    def test_unchanged_vault_reuses_the_last_scan(self):
        items = self._facts(conflicts._CACHE_MIN_ITEMS) + [
            F("Based in Westbrook"),
            F("Based in Portland"),
        ]
        first = conflicts.find_conflicts(items)
        calls = self._count_calls("_similar")
        # Same memories, freshly loaded: the result comes back without a rescan,
        # and it is built from the objects of this call, not the cached ones.
        reloaded = [
            MemoryItem(content=i.content, type=i.type, id=i.id, content_hash=i.content_hash)
            for i in items
        ]
        again = conflicts.find_conflicts(reloaded)
        self.assertEqual(calls, [])
        self.assertEqual([[m.id for m in c] for c in again], [[m.id for m in c] for c in first])
        self.assertTrue(all(m in reloaded for c in again for m in c))

    def test_hand_edit_that_keeps_the_stored_hash_rescans(self):
        items = self._facts(conflicts._CACHE_MIN_ITEMS)
        conflicts.find_conflicts(items)
        calls = self._count_calls("_similar")
        edited = items[0]
        # A reindex keeps the front-matter hash, so a note edited in Obsidian
        # arrives with new text under its old hash.
        items[0] = MemoryItem(
            content="Based in Portland", type="fact", id=edited.id, content_hash=edited.content_hash
        )
        conflicts.find_conflicts(items)
        self.assertGreater(len(calls), 0)

    def test_new_resolution_rescans(self):
        items = self._facts(conflicts._CACHE_MIN_ITEMS) + [
            F("Based in Westbrook"),
            F("Based in Portland"),
        ]
        self.assertEqual(len(conflicts.find_conflicts(items)), 1)
        resolved = {frozenset((items[-2].id, items[-1].id))}
        self.assertEqual(conflicts.find_conflicts(items, resolved_pairs=resolved), [])

    def test_size_shortcut_keeps_pairs_exactly_at_the_threshold(self):
        # Two words against four: the size ceiling and the overlap are both
        # exactly 0.5, so the shortcut must not reject the pair.
        items = [F("alpha beta"), F("alpha beta gamma delta")]
        self.assertEqual(len(conflicts.find_conflicts(items, threshold=0.5)), 1)
        self.assertEqual(conflicts.find_conflicts(items, threshold=0.51), [])


class TestServiceConflicts(unittest.TestCase):
    def test_report_and_recall_note_with_provenance(self):
        with tempfile.TemporaryDirectory() as d:
            svc = MemoryService(MemoryStore(d))
            svc.remember("Based in Westbrook", type="fact", source="claude")
            svc.remember("Based in Portland", type="fact", source="chatgpt")

            report = svc.conflicts()
            self.assertIn("possible conflict", report.lower())
            for token in ("Westbrook", "Portland", "claude", "chatgpt"):
                self.assertIn(token, report)  # both values + provenance shown

            recalled = svc.recall("Based in")
            self.assertIn("conflict", recalled.lower())  # surfaced during recall too
            svc.store.close()

    def test_no_conflicts_message(self):
        with tempfile.TemporaryDirectory() as d:
            svc = MemoryService(MemoryStore(d))
            svc.remember("Based in Westbrook", type="fact", source="claude")
            self.assertIn("No conflicting", svc.conflicts())
            svc.store.close()


if __name__ == "__main__":
    unittest.main()
