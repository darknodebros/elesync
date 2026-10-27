"""Regression tests for the `remember()` write-path scaling fix.

`remember()` used to run a whole-vault `find_conflicts` on every save — twice,
once for the auto-supersede gate and again for the hint — so per-save cost grew
with vault size and building a vault was roughly cubic. Building 500 memories
took double-digit minutes.

The fix walks outward from the saved item instead of clustering everything. The
risk that introduces is *behavioural drift*: a component walk that disagrees
with union-find would silently change which facts get superseded. Most of these
tests are equivalence tests for that reason. The scaling guard counts
similarity comparisons rather than timing anything, so it fails for the right
reason on a slow machine.
"""

from __future__ import annotations

import tempfile
import unittest
from pathlib import Path

from elesync import conflicts as conflicts_mod
from elesync.models import MemoryItem
from elesync.service import MemoryService
from elesync.store import MemoryStore


def _fact(content: str, tags=None, scopes=None) -> MemoryItem:
    return MemoryItem(content=content, type="fact", tags=tags or [], scopes=scopes or [])


class TestClusterContainingEquivalence(unittest.TestCase):
    """cluster_containing must return exactly the component union-find would."""

    def _component_via_find_conflicts(self, item, items):
        for cluster in conflicts_mod.find_conflicts(items):
            if any(m.id == item.id for m in cluster):
                return sorted(m.id for m in cluster)
        return []

    def assert_agrees(self, item, items):
        expected = self._component_via_find_conflicts(item, items)
        got = sorted(m.id for m in conflicts_mod.cluster_containing(item, items))
        self.assertEqual(got, expected)

    def test_isolated_pair(self):
        a = _fact("Lives in Portland Oregon")
        b = _fact("Lives in Seattle Oregon")
        c = _fact("Completely unrelated statement about penguins")
        self.assert_agrees(a, [a, b, c])

    def test_transitive_chain_is_one_component(self):
        # A~B and B~C but not A~C: union-find merges all three, and the walk
        # must too, or the hint would fire on a chain it should stay out of.
        a = _fact("Based in Portland with the platform team")
        b = _fact("Based in Seattle with the platform team")
        c = _fact("Based in Seattle with the research team")
        self.assert_agrees(a, [a, b, c])
        self.assertEqual(len(conflicts_mod.cluster_containing(a, [a, b, c])), 3)

    def test_no_conflict_returns_empty(self):
        a = _fact("Alpha statement about one topic")
        b = _fact("Totally different words entirely unrelated")
        self.assertEqual(conflicts_mod.cluster_containing(a, [a, b]), [])
        self.assert_agrees(a, [a, b])

    def test_duplicate_is_not_a_conflict(self):
        a = _fact("Lives in Portland Oregon")
        b = _fact("Lives in Portland Oregon")  # same content -> same hash
        self.assertEqual(conflicts_mod.cluster_containing(a, [a, b]), [])

    def test_resolved_pair_is_excluded(self):
        a = _fact("Lives in Portland Oregon")
        b = _fact("Lives in Seattle Oregon")
        resolved = {frozenset((a.id, b.id))}
        self.assertEqual(conflicts_mod.cluster_containing(a, [a, b], resolved_pairs=resolved), [])

    def test_non_fact_item_has_no_cluster(self):
        note = MemoryItem(content="Lives in Portland Oregon", type="note")
        b = _fact("Lives in Seattle Oregon")
        self.assertEqual(conflicts_mod.cluster_containing(note, [note, b]), [])

    def test_max_size_stops_early_and_reports_at_least(self):
        a = _fact("Based in Portland with the platform team")
        b = _fact("Based in Seattle with the platform team")
        c = _fact("Based in Seattle with the research team")
        got = conflicts_mod.cluster_containing(a, [a, b, c], max_size=2)
        self.assertGreater(len(got), 2)  # "at least this big", walk stopped early

    def test_agreement_across_sources_is_not_a_conflict(self):
        a = _fact("Lives in Portland Oregon")
        b = _fact("lives  in   portland oregon")  # normalises equal
        self.assertEqual(conflicts_mod.cluster_containing(a, [a, b]), [])


class TestWritePathScaling(unittest.TestCase):
    """The guard that would have caught the original defect."""

    def setUp(self):
        self._tmp = tempfile.TemporaryDirectory()
        self.store = MemoryStore(Path(self._tmp.name))
        self.svc = MemoryService(self.store)

    def tearDown(self):
        self.store.close()
        self._tmp.cleanup()

    def _comparisons_for_one_save(self, existing: int) -> int:
        for i in range(existing):
            self.svc.remember(f"Fact number {i} about topic-{i}", type="fact", source="t")
        calls = []
        real = conflicts_mod._similar

        def counting(a, b, threshold):
            calls.append(1)
            return real(a, b, threshold)

        conflicts_mod._similar = counting
        try:
            self.svc.remember("A brand new fact about something else", type="fact", source="t")
        finally:
            conflicts_mod._similar = real
        return len(calls)

    def test_single_save_is_linear_not_quadratic(self):
        # Doubling the vault must not quadruple the work of one save. The old
        # code clustered every pair, so this ratio was ~4x; a linear walk keeps
        # it near 2x. 3x is the generous bar that still fails the old behaviour.
        small = self._comparisons_for_one_save(40)
        self.tearDown()
        self.setUp()
        large = self._comparisons_for_one_save(80)
        self.assertLess(
            large,
            small * 3,
            f"one save cost {small} comparisons at n=40 and {large} at n=80 — "
            "write-path conflict scanning has gone superlinear again",
        )


class TestWritePathBehaviourUnchanged(unittest.TestCase):
    """The fix must not change what remember() actually does."""

    def setUp(self):
        self._tmp = tempfile.TemporaryDirectory()
        self.store = MemoryStore(Path(self._tmp.name))
        self.svc = MemoryService(self.store)

    def tearDown(self):
        self.store.close()
        self._tmp.cleanup()

    def test_isolated_pair_still_hints(self):
        self.svc.remember("Santo lives in Portland Oregon", type="fact", source="t")
        out = self.svc.remember("Santo lives in Seattle Oregon", type="fact", source="t")
        self.assertIn("may update", out)

    def test_unrelated_save_gets_no_hint(self):
        self.svc.remember("Santo lives in Portland Oregon", type="fact", source="t")
        out = self.svc.remember("Penguins are birds that cannot fly", type="fact", source="t")
        self.assertNotIn("may update", out)

    def test_chain_suppresses_the_hint(self):
        # Three mutually-similar facts collapse into one cluster, so no clean
        # pair exists and the hint must stay silent.
        self.svc.remember("Based in Portland with the platform team", type="fact", source="t")
        self.svc.remember("Based in Seattle with the platform team", type="fact", source="t")
        out = self.svc.remember("Based in Seattle with the research team", type="fact", source="t")
        self.assertNotIn("may update", out)

    def test_auto_supersede_still_fires_on_tagged_update(self):
        self.svc.remember("Santo lives in Portland Oregon", type="fact", tags="home", source="t")
        out = self.svc.remember(
            "Santo lives in Seattle Oregon", type="fact", tags="home", source="t"
        )
        self.assertIn("Auto-superseded", out)


if __name__ == "__main__":
    unittest.main()
