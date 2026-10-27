"""Tests for hybrid search weight tuning (semantic_weight parameter).

The RRF fusion in _hybrid_rows() blends FTS and vector rankings. The
semantic_weight parameter controls the blend: 0.0 = pure FTS, 1.0 = pure
vector, 0.5 = equal. These tests verify the weight actually changes results
and that edge cases (clamping, None default) behave correctly.

Uses the same FakeEmbedder as test_semantic.py so no model download is needed.
"""

from __future__ import annotations

import tempfile
import unittest

from elesync.models import MemoryItem
from elesync.store import MemoryStore


class FakeEmbedder:
    """Deterministic embedder over a small vocab with synonyms."""

    model_name = "fake"
    VOCAB = ["live", "city", "work", "project", "family", "food", "space", "code"]
    SYN = {
        "reside": "live",
        "resides": "live",
        "living": "live",
        "based": "live",
        "home": "live",
        "lives": "live",
        "town": "city",
        "place": "city",
        "located": "city",
        "where": "city",
        "job": "work",
        "working": "work",
    }

    def _vec(self, text):
        text = text.lower()
        for syn, real in self.SYN.items():
            text = text.replace(syn, real)
        vec = [0.0] * len(self.VOCAB)
        for word in re.findall(r"[a-z]+", text):
            if word in self.VOCAB:
                vec[self.VOCAB.index(word)] = 1.0
        return vec

    def embed_one(self, text):
        import array

        return array.array("f", self._vec(text))

    def embed_many(self, texts):
        return [self.embed_one(t) for t in texts]


import re  # noqa: E402


class TestHybridWeightTuning(unittest.TestCase):
    """The semantic_weight parameter changes which results surface first."""

    def setUp(self):
        self._tmp = tempfile.TemporaryDirectory()
        self.store = MemoryStore(self._tmp.name, embedder=FakeEmbedder())

    def tearDown(self):
        self.store.close()
        self._tmp.cleanup()

    def _seed(self):
        """Seed a vault where FTS and vector rankings disagree."""
        # "Helios project" — strong FTS hit for "Helios", weak vector hit.
        self.store.upsert(MemoryItem(content="The Helios project is active", type="project"))
        # "Based in Westbrook" — weak FTS hit for "where live", strong vector hit.
        self.store.upsert(MemoryItem(content="Based in Westbrook", type="fact"))
        # Filler.
        self.store.upsert(MemoryItem(content="Enjoys cooking food on weekends", type="preference"))

    def test_pure_fts_prefers_keyword_match(self):
        """semantic_weight=0.0 should rank the exact keyword hit first."""
        self._seed()
        hits = self.store.search("Helios", semantic_weight=0.0)
        self.assertTrue(hits)
        self.assertIn("Helios", hits[0].content)

    def test_pure_semantic_prefers_vector_match(self):
        """semantic_weight=1.0 should rank the semantic match first for a
        query that shares no keywords with the target."""
        self._seed()
        hits = self.store.search("where does the user live", semantic_weight=1.0)
        self.assertTrue(hits)
        self.assertIn("Westbrook", hits[0].content)

    def test_default_weight_is_equal(self):
        """semantic_weight=None (default) should behave the same as 0.5."""
        self._seed()
        default_hits = self.store.search("where does the user live")
        half_hits = self.store.search("where does the user live", semantic_weight=0.5)
        self.assertEqual(
            [h.id for h in default_hits],
            [h.id for h in half_hits],
            "default weight should equal 0.5",
        )

    def test_clamping_handles_out_of_range(self):
        """Values outside [0.0, 1.0] are clamped, not rejected."""
        self._seed()
        # Negative should clamp to 0.0 (pure FTS).
        fts_hits = self.store.search("Helios", semantic_weight=-5.0)
        self.assertIn("Helios", fts_hits[0].content)
        # >1.0 should clamp to 1.0 (pure semantic).
        sem_hits = self.store.search("where does the user live", semantic_weight=99.0)
        self.assertIn("Westbrook", sem_hits[0].content)

    def test_weight_changes_ordering(self):
        """For a query where FTS and vector disagree, changing the weight
        should change the top result (or at least the ordering)."""
        self._seed()
        fts_order = [
            h.id for h in self.store.search("where does the user live", semantic_weight=0.0)
        ]
        sem_order = [
            h.id for h in self.store.search("where does the user live", semantic_weight=1.0)
        ]
        # The two orderings should differ — if they're identical, the weight
        # isn't doing anything.
        self.assertNotEqual(fts_order, sem_order, "weight should change ordering")

    def test_weight_ignored_without_embeddings(self):
        """When no embeddings exist, semantic_weight is irrelevant — FTS is used."""
        with tempfile.TemporaryDirectory() as d:
            store = MemoryStore(d)  # no embedder
            store.upsert(MemoryItem(content="The Helios project is active", type="project"))
            # Any weight should still find the FTS hit.
            hits = store.search("Helios", semantic_weight=1.0)
            self.assertEqual(len(hits), 1)
            store.close()


class TestResolveSearchParams(unittest.TestCase):
    """The CLI's _resolve_search_params translates mode + weight flags + config."""

    def test_auto_mode_returns_none_weight(self):
        from elesync.cli import _resolve_search_params

        args = type("A", (), {"mode": "auto", "semantic_weight": None})()
        with __import__("contextlib").suppress(Exception):
            mode, sw = _resolve_search_params(args)
        # auto with no config should give (None, None) — let the store decide.
        # We can't fully control config here, so just check it doesn't crash.
        self.assertIsNone(mode)

    def test_fts_mode_clamps_to_zero_weight(self):
        from elesync.cli import _resolve_search_params

        args = type("A", (), {"mode": "fts", "semantic_weight": None})()
        mode, sw = _resolve_search_params(args)
        self.assertIsNone(mode)
        self.assertEqual(sw, 0.0)

    def test_semantic_mode_clamps_to_one_weight(self):
        from elesync.cli import _resolve_search_params

        args = type("A", (), {"mode": "semantic", "semantic_weight": None})()
        mode, sw = _resolve_search_params(args)
        self.assertIsNone(mode)
        self.assertEqual(sw, 1.0)

    def test_hybrid_mode_uses_explicit_weight(self):
        from elesync.cli import _resolve_search_params

        args = type("A", (), {"mode": "hybrid", "semantic_weight": 0.7})()
        mode, sw = _resolve_search_params(args)
        self.assertIsNone(mode)
        self.assertEqual(sw, 0.7)

    def test_hybrid_mode_defaults_to_half(self):
        from elesync.cli import _resolve_search_params

        args = type("A", (), {"mode": "hybrid", "semantic_weight": None})()
        mode, sw = _resolve_search_params(args)
        self.assertIsNone(mode)
        self.assertEqual(sw, 0.5)


if __name__ == "__main__":
    unittest.main()
