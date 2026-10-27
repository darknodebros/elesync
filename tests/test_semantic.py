"""Semantic-recall tests — stdlib only.

The vector-storage / cosine / hybrid-fusion / fallback plumbing is exercised with
a deterministic FakeEmbedder, so these run in CI without installing any model.
A final test uses the real model but is skipped unless the fastembed is
installed.
"""

import re
import tempfile
import unittest

from elesync import embeddings
from elesync.models import MemoryItem
from elesync.store import MemoryStore


class FakeEmbedder:
    """Tiny deterministic embedder over a fixed vocab, with a few synonyms so a
    query can land near a memory it shares no literal words with. Not remotely a
    real model — just enough to drive the store's vector paths in tests."""

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
        words = [self.SYN.get(w, w) for w in re.findall(r"[a-z]+", text.lower())]
        return [float(words.count(term)) for term in self.VOCAB]

    def embed_one(self, text):
        return self._vec(text)

    def embed(self, texts):
        return [self._vec(t) for t in texts]


class TestVectorHelpers(unittest.TestCase):
    def test_pack_unpack_roundtrip(self):
        vec = [0.5, -1.25, 3.0, 0.0]
        out = embeddings.unpack(embeddings.pack(vec))
        self.assertEqual(len(out), len(vec))
        for a, b in zip(out, vec, strict=True):
            self.assertAlmostEqual(a, b, places=5)

    def test_cosine(self):
        self.assertAlmostEqual(embeddings.cosine([1, 0], [1, 0]), 1.0, places=6)
        self.assertAlmostEqual(embeddings.cosine([1, 0], [0, 1]), 0.0, places=6)
        self.assertEqual(embeddings.cosine([0, 0], [1, 1]), 0.0)  # zero vector → 0


class TestSemanticStore(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.store = MemoryStore(self.tmp.name, embedder=FakeEmbedder())

    def tearDown(self):
        self.store.close()
        self.tmp.cleanup()

    def test_embed_on_write(self):
        mid = self.store.upsert(MemoryItem(content="Based in Westbrook", type="fact"))
        row = self.store.db.execute(
            "SELECT embedding FROM memories WHERE id = ?", (mid,)
        ).fetchone()
        self.assertIsNotNone(row["embedding"])
        self.assertEqual(len(embeddings.unpack(row["embedding"])), len(FakeEmbedder.VOCAB))
        self.assertEqual(self.store.stats()["embedded"], 1)

    def test_semantic_recall_without_keyword_overlap(self):
        # None of these share the literal words "where … live" with the query.
        self.store.upsert(MemoryItem(content="Based in Westbrook", type="fact"))
        self.store.upsert(MemoryItem(content="Enjoys cooking food on weekends", type="preference"))
        self.store.upsert(MemoryItem(content="Follows space-launch threads", type="preference"))
        hits = self.store.search("where does the user live")
        self.assertTrue(hits, "semantic search returned nothing")
        # The location memory should surface first despite zero shared keywords.
        self.assertIn("Westbrook", hits[0].content)

    def test_hybrid_still_finds_exact_keyword_hit(self):
        self.store.upsert(MemoryItem(content="The Helios project is active", type="project"))
        self.store.upsert(MemoryItem(content="Unrelated note about coffee", type="note"))
        hits = self.store.search("Helios")
        self.assertTrue(any("Helios" in h.content for h in hits))


class TestSemanticFallbackAndBackfill(unittest.TestCase):
    def test_no_embedder_is_keyword_only(self):
        with tempfile.TemporaryDirectory() as d:
            store = MemoryStore(d)  # no embedder
            mid = store.upsert(MemoryItem(content="The Helios project is active", type="project"))
            row = store.db.execute("SELECT embedding FROM memories WHERE id = ?", (mid,)).fetchone()
            self.assertIsNone(row["embedding"])
            self.assertEqual(store.stats()["embedded"], 0)
            self.assertEqual(len(store.search("Helios")), 1)  # FTS still works
            store.close()

    def test_embed_missing_backfills_existing_vault(self):
        with tempfile.TemporaryDirectory() as d:
            plain = MemoryStore(d)  # imported with semantic off
            for c in ("Based in Westbrook", "Enjoys cooking food", "Writes code daily"):
                plain.upsert(MemoryItem(content=c, type="fact"))
            plain.close()

            sem = MemoryStore(d, embedder=FakeEmbedder())
            self.assertEqual(sem.embed_missing(), 3)
            self.assertEqual(sem.stats()["embedded"], 3)
            self.assertEqual(sem.embed_missing(), 0)  # idempotent
            hits = sem.search("where does the user live")
            self.assertIn("Westbrook", hits[0].content)
            sem.close()


def _fastembed_available() -> bool:
    try:
        import fastembed  # noqa: F401

        return True
    except ImportError:
        return False


class TestRealModel(unittest.TestCase):
    @unittest.skipUnless(_fastembed_available(), "fastembed not installed")
    def test_real_semantic_recall(self):
        try:
            emb = embeddings.load_embedder()
        except (ValueError, OSError):
            self.skipTest("model download unavailable in this environment")
        with tempfile.TemporaryDirectory() as d:
            store = MemoryStore(d, embedder=emb)
            store.upsert(MemoryItem(content="Based in Westbrook, near the coast.", type="fact"))
            store.upsert(MemoryItem(content="Enjoys hiking on weekends.", type="preference"))
            hits = store.search("where does the user live")
            self.assertIn("Westbrook", hits[0].content)
            store.close()


if __name__ == "__main__":
    unittest.main()
