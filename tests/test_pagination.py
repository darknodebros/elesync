"""Tests for SQL-side pagination (`MemoryStore.page`).

Browse endpoints used to call `all()` and slice the result, loading and
rebuilding every MemoryItem in the vault to show thirty. `page()` pushes the
filtering, counting and slicing into SQLite.

Two risks come with that. The first is behavioural drift from the old
all()-then-slice path, so most of these are equivalence tests. The second is
pagination *stability*: `created_at` has second resolution, so a bulk import
gives many rows the same timestamp, and LIMIT/OFFSET over a non-deterministic
order can show one memory twice and never show another. The old code was immune
because it returned everything at once; the new code needs the id tiebreaker,
and the walk-every-page test is what proves it.
"""

from __future__ import annotations

import tempfile
import unittest
from pathlib import Path

from elesync.models import MemoryItem
from elesync.store import MemoryStore


class PageTestCase(unittest.TestCase):
    def setUp(self):
        self._tmp = tempfile.TemporaryDirectory()
        self.store = MemoryStore(Path(self._tmp.name))

    def tearDown(self):
        self.store.close()
        self._tmp.cleanup()

    def add(self, content, **kw):
        item = MemoryItem(content=content, **kw)
        self.store.upsert(item)
        return item


class TestPageEquivalence(PageTestCase):
    def seed(self, n=25):
        for i in range(n):
            self.add(f"memory {i} about topic-{i}", source="claude" if i % 2 else "manual")

    def test_first_page_matches_all_then_slice(self):
        self.seed()
        expected = self.store.all()[0:10]
        got, total = self.store.page(limit=10, offset=0)
        self.assertEqual([i.id for i in got], [i.id for i in expected])
        self.assertEqual(total, 25)

    def test_offset_page_matches_all_then_slice(self):
        self.seed()
        expected = self.store.all()[10:20]
        got, _ = self.store.page(limit=10, offset=10)
        self.assertEqual([i.id for i in got], [i.id for i in expected])

    def test_source_filter_matches(self):
        self.seed()
        expected = [i for i in self.store.all() if i.source == "claude"]
        got, total = self.store.page(limit=100, source="claude")
        self.assertEqual([i.id for i in got], [i.id for i in expected])
        self.assertEqual(total, len(expected))

    def test_type_filter_matches(self):
        self.add("a fact", type="fact")
        self.add("a note", type="note")
        got, total = self.store.page(limit=100, mtype="fact")
        self.assertEqual(total, 1)
        self.assertEqual(got[0].type, "fact")

    def test_combined_filters(self):
        self.add("x", type="fact", source="claude")
        self.add("y", type="fact", source="manual")
        self.add("z", type="note", source="claude")
        got, total = self.store.page(limit=100, source="claude", mtype="fact")
        self.assertEqual(total, 1)
        self.assertEqual(got[0].content, "x")

    def test_total_is_the_match_count_not_the_page_size(self):
        self.seed()
        _, total = self.store.page(limit=5)
        self.assertEqual(total, 25)


class TestSupersededHandling(PageTestCase):
    def test_superseded_hidden_by_default(self):
        old = self.add("lives in Portland", type="fact")
        new = self.add("lives in Seattle", type="fact")
        self.store.supersede(old.id, new.id, "2026-01-01T00:00:00+00:00")
        got, total = self.store.page(limit=100)
        self.assertEqual(total, 1)
        self.assertNotIn(old.id, [i.id for i in got])

    def test_superseded_included_on_request(self):
        old = self.add("lives in Portland", type="fact")
        new = self.add("lives in Seattle", type="fact")
        self.store.supersede(old.id, new.id, "2026-01-01T00:00:00+00:00")
        got, total = self.store.page(limit=100, include_superseded=True)
        self.assertEqual(total, 2)
        self.assertIn(old.id, [i.id for i in got])


class TestPaginationStability(PageTestCase):
    """The failure mode a naive ORDER BY created_at would introduce."""

    def test_every_memory_appears_exactly_once_across_pages(self):
        # All 60 share a timestamp — exactly what a bulk import produces, and
        # the case where an unstable sort duplicates and drops rows.
        stamp = "2026-07-25T12:00:00+00:00"
        for i in range(60):
            self.add(f"bulk import row {i}", created_at=stamp)
        seen = []
        offset = 0
        while True:
            got, total = self.store.page(limit=7, offset=offset)
            if not got:
                break
            seen.extend(i.id for i in got)
            offset += 7
        self.assertEqual(total, 60)
        self.assertEqual(len(seen), 60, "pages dropped or duplicated rows")
        self.assertEqual(len(set(seen)), 60, "a memory appeared on two pages")

    def test_page_order_is_repeatable(self):
        stamp = "2026-07-25T12:00:00+00:00"
        for i in range(20):
            self.add(f"row {i}", created_at=stamp)
        first = [i.id for i in self.store.page(limit=20)[0]]
        second = [i.id for i in self.store.page(limit=20)[0]]
        self.assertEqual(first, second)


class TestPageBoundaries(PageTestCase):
    def test_offset_past_the_end_is_empty_not_an_error(self):
        self.add("only one")
        got, total = self.store.page(limit=10, offset=500)
        self.assertEqual(got, [])
        self.assertEqual(total, 1)  # total still reports the real count

    def test_empty_vault(self):
        got, total = self.store.page(limit=10)
        self.assertEqual((got, total), ([], 0))

    def test_negative_offset_is_clamped(self):
        self.add("a")
        got, _ = self.store.page(limit=10, offset=-5)
        self.assertEqual(len(got), 1)

    def test_zero_limit_returns_nothing_but_still_counts(self):
        self.add("a")
        got, total = self.store.page(limit=0)
        self.assertEqual(got, [])
        self.assertEqual(total, 1)


class TestPageCostIsFlat(PageTestCase):
    """The guard that would have caught the original all()-then-slice cost."""

    def _rows_built_for_one_page(self, n):
        while self.store.count() < n:
            self.add(f"memory {self.store.count()}")
        calls = []
        real = MemoryStore._row_to_item

        def counting(row):
            calls.append(1)
            return real(row)

        MemoryStore._row_to_item = staticmethod(counting)
        try:
            self.store.page(limit=30, offset=0)
        finally:
            MemoryStore._row_to_item = staticmethod(real)
        return len(calls)

    def test_rows_built_does_not_grow_with_the_vault(self):
        small = self._rows_built_for_one_page(100)
        large = self._rows_built_for_one_page(400)
        self.assertEqual(small, 30)
        self.assertEqual(
            large,
            small,
            f"a 30-row page built {small} items at n=100 and {large} at n=400 — "
            "browse has gone back to loading the whole vault",
        )


if __name__ == "__main__":
    unittest.main()
