"""Edge case tests for issue #52.

Covers:
  - Large vault performance (1000+ memories)
  - Malformed import files (truncated/invalid JSON, empty files, binary)
  - Unicode edge cases (emoji, CJK, RTL, mixed scripts, zero-width chars)
  - Concurrent access (multiple MemoryStore instances sharing a vault)
"""

import json
import tempfile
import threading
import unittest
from pathlib import Path

from elesync.models import MemoryItem
from elesync.normalize import normalize_file
from elesync.service import MemoryService
from elesync.store import MemoryStore


def _cli_args(vault, path, source=None):
    """Minimal args stand-in for cmd_import, matching the pattern used in
    tests/test_paste.py's TestCmdImportStdin."""
    return type(
        "A",
        (),
        {
            "vault": vault,
            "path": path,
            "source": source,
            "passphrase": None,
            "semantic": False,
            "scopes": None,
            "lang": None,
        },
    )()


# ---------------------------------------------------------------------------
# 1. Large vault performance
# ---------------------------------------------------------------------------


class TestLargeVault(unittest.TestCase):
    """Ensure the store handles 1000+ memories without correctness or
    performance regressions.  Keeps total wall-time under 30 s."""

    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.store = MemoryStore(self.tmp.name)

    def tearDown(self):
        self.store.close()
        self.tmp.cleanup()

    def _populate(self, n: int) -> list[str]:
        """Insert *n* unique memories and return their ids."""
        ids = []
        for i in range(n):
            mid = self.store.upsert(
                MemoryItem(
                    content=f"Memory entry number {i}: fact about item-{i}",
                    source="claude" if i % 2 == 0 else "chatgpt",
                    type="fact" if i % 3 != 0 else "note",
                    tags=[f"batch-{i // 100}"],
                )
            )
            ids.append(mid)
        return ids

    def test_insert_1000_memories(self):
        """1000 inserts complete and the count is correct."""
        ids = self._populate(1000)
        self.assertEqual(len(set(ids)), 1000)
        self.assertEqual(self.store.count(), 1000)

    def test_search_in_large_vault(self):
        """Full-text search returns correct results from a large vault."""
        self._populate(1000)
        # Search for a term that only appears in one memory.
        hits = self.store.search("item-42", limit=10)
        self.assertTrue(any("item-42" in h.content for h in hits))

    def test_search_with_filters_in_large_vault(self):
        """Source/type filters still work correctly at scale."""
        self._populate(1000)
        hits = self.store.search("item-99", source="claude")
        # item-99 is odd, so source would be "chatgpt" -- should not appear.
        self.assertTrue(all(h.source == "claude" for h in hits))

    def test_stats_large_vault(self):
        """Stats aggregate correctly over many memories."""
        self._populate(1000)
        s = self.store.stats()
        self.assertEqual(s["total"], 1000)
        # Even-indexed -> claude (500), odd -> chatgpt (500).
        self.assertEqual(s["by_source"]["claude"], 500)
        self.assertEqual(s["by_source"]["chatgpt"], 500)

    def test_reindex_large_vault(self):
        """Reindex faithfully rebuilds a large index from markdown files."""
        ids = self._populate(500)
        # Wipe the index.
        self.store.db.execute("DELETE FROM memories")
        self.store.db.commit()
        self.assertEqual(self.store.count(), 0)
        res = self.store.reindex()
        self.assertEqual(res["reindexed"], 500)
        self.assertEqual(self.store.count(), 500)
        # Spot-check a known id survived the round-trip.
        self.assertIsNotNone(self.store.get(ids[0]))

    def test_sync_large_vault(self):
        """Sync detects external additions in a large vault."""
        self._populate(500)
        # Simulate an externally-added note.
        new = MemoryItem(content="added externally in sync test", type="note")
        note_path = Path(self.tmp.name) / "notes" / f"{new.id}.md"
        note_path.write_text(new.to_markdown(), encoding="utf-8")
        result = self.store.sync()
        self.assertEqual(result["added"], 1)
        self.assertEqual(result["total"], 501)

    def test_dedup_at_scale(self):
        """Dedup stays correct when re-inserting the same batch."""
        self._populate(500)
        self.assertEqual(self.store.count(), 500)
        # Re-insert the same content -- all should be deduped.
        for i in range(500):
            self.store.upsert(
                MemoryItem(
                    content=f"Memory entry number {i}: fact about item-{i}",
                    source="claude" if i % 2 == 0 else "chatgpt",
                    type="fact" if i % 3 != 0 else "note",
                    tags=[f"batch-{i // 100}"],
                )
            )
        self.assertEqual(self.store.count(), 500)

    def test_import_many_large_batch(self):
        """import_many handles a large batch correctly."""
        items = [
            MemoryItem(
                content=f"Bulk import item {i}",
                source="chatgpt",
                type="fact",
            )
            for i in range(1000)
        ]
        result = self.store.import_many(items)
        self.assertEqual(result["added"], 1000)
        self.assertEqual(result["skipped_duplicates"], 0)
        self.assertEqual(result["total"], 1000)

    def test_service_recall_large_vault(self):
        """MemoryService.recall works over a large vault."""
        svc = MemoryService(self.store)
        for i in range(500):
            svc.remember(
                f"Service fact {i}: detail about topic-{i}",
                type="fact",
                source="claude",
            )
        out = svc.recall("topic-123")
        self.assertIn("topic-123", out)
        self.assertNotEqual(out, "No matching memories.")


# ---------------------------------------------------------------------------
# 2. Malformed import files
# ---------------------------------------------------------------------------


class TestMalformedImports(unittest.TestCase):
    """normalize_file should reject bad input gracefully with ValueError,
    not crash with an unhandled exception."""

    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()

    def tearDown(self):
        self.tmp.cleanup()

    def _write(self, name: str, content: str | bytes) -> Path:
        p = Path(self.tmp.name) / name
        if isinstance(content, bytes):
            p.write_bytes(content)
        else:
            p.write_text(content, encoding="utf-8")
        return p

    def test_empty_json_file(self):
        """An empty file is not valid JSON."""
        p = self._write("empty.json", "")
        with self.assertRaises(ValueError):
            normalize_file(p)

    def test_truncated_json(self):
        """A JSON file cut off mid-object."""
        p = self._write("truncated.json", '{"memories": ["hello", "world"')
        with self.assertRaises(ValueError):
            normalize_file(p)

    def test_invalid_json_syntax(self):
        """Garbled JSON."""
        p = self._write("bad.json", "{not json at all!!!}")
        with self.assertRaises(ValueError):
            normalize_file(p)

    def test_binary_file_as_json(self):
        """A binary file should not parse as JSON."""
        p = self._write("binary.json", b"\x89PNG\r\n\x1a\n\x00\x00\x00\rIHDR")
        with self.assertRaises((ValueError, UnicodeDecodeError)):
            normalize_file(p)

    def test_valid_json_wrong_structure(self):
        """Valid JSON, but not an expected export shape -- returns empty list."""
        p = self._write("wrong.json", json.dumps({"unrelated": "data", "count": 42}))
        result = normalize_file(p)
        self.assertEqual(result, [])

    def test_json_array_of_numbers(self):
        """A JSON array of non-string/non-dict entries -- returns empty list."""
        p = self._write("numbers.json", json.dumps([1, 2, 3, 4, 5]))
        result = normalize_file(p)
        self.assertEqual(result, [])

    def test_json_null_top_level(self):
        """Top-level null is valid JSON but not an export."""
        p = self._write("null.json", "null")
        result = normalize_file(p)
        self.assertEqual(result, [])

    def test_json_string_top_level(self):
        """A bare JSON string is iterable, so normalize treats each character
        as a potential entry.  The important thing is it doesn't crash."""
        p = self._write("string.json", '"just a string"')
        # Should not raise -- the adapter iterates the string's characters and
        # coerces single-char strings into MemoryItems.  Not very useful, but
        # it must not blow up.
        result = normalize_file(p)
        self.assertIsInstance(result, list)

    def test_empty_memories_array(self):
        """An export with an empty memories array yields zero items."""
        p = self._write("empty_memories.json", json.dumps({"memories": []}))
        result = normalize_file(p)
        self.assertEqual(result, [])

    def test_memories_with_empty_strings(self):
        """Entries that are empty or whitespace-only strings are skipped."""
        p = self._write(
            "blank_entries.json",
            json.dumps({"memories": ["", "   ", "\n", "valid fact"]}),
        )
        result = normalize_file(p)
        self.assertEqual(len(result), 1)
        self.assertEqual(result[0].content, "valid fact")

    def test_csv_empty(self):
        """An empty CSV file produces zero items."""
        p = self._write("empty.csv", "")
        result = normalize_file(p)
        self.assertEqual(result, [])

    def test_txt_empty(self):
        """An empty text file produces zero items."""
        p = self._write("empty.txt", "")
        result = normalize_file(p)
        self.assertEqual(result, [])


class TestCmdImportErrorHandling(unittest.TestCase):
    """`normalize_file()` correctly raises ValueError/OSError on bad input
    (covered above) -- but the CLI layer (`ele import`) has its own job: turn
    that into a clean, non-crashing error message instead of a Python
    traceback. These exercise cmd_import directly, the way a user actually
    hits this path."""

    def setUp(self):
        self.tmpdir = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmpdir.cleanup)

    def _write(self, name, content):
        p = Path(self.tmpdir.name) / name
        p.write_text(content, encoding="utf-8")
        return p

    def _run_import(self, path):
        from elesync.cli import cmd_import

        vault = Path(self.tmpdir.name) / "vault"
        vault.mkdir(exist_ok=True)
        args = _cli_args(str(vault), str(path))
        return cmd_import(args)

    def test_truncated_json_does_not_crash(self):
        p = self._write("truncated.json", '{"memories": ["hello", "world"')
        result = self._run_import(p)
        self.assertEqual(result, 1)

    def test_invalid_json_syntax_does_not_crash(self):
        p = self._write("bad.json", "{not json at all!!!}")
        result = self._run_import(p)
        self.assertEqual(result, 1)

    def test_missing_file_does_not_crash(self):
        missing = Path(self.tmpdir.name) / "does_not_exist.json"
        result = self._run_import(missing)
        self.assertEqual(result, 1)

    def test_unrecognized_csv_shape_does_not_crash(self):
        p = self._write("bad_shape.csv", "foo,bar\n1,2\n")
        result = self._run_import(p)
        self.assertEqual(result, 1)

    def test_valid_import_still_succeeds(self):
        """The error handling must not swallow the success path."""
        p = self._write("good.json", json.dumps({"memories": ["a real fact"]}))
        result = self._run_import(p)
        self.assertEqual(result, 0)


# ---------------------------------------------------------------------------
# 3. Unicode edge cases
# ---------------------------------------------------------------------------


class TestUnicodeEdgeCases(unittest.TestCase):
    """MemoryItem and MemoryStore must handle the full Unicode range
    without data loss or errors."""

    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.store = MemoryStore(self.tmp.name)

    def tearDown(self):
        self.store.close()
        self.tmp.cleanup()

    def _roundtrip(self, content: str, **kwargs) -> MemoryItem:
        """Insert a memory and read it back from the store."""
        mid = self.store.upsert(MemoryItem(content=content, **kwargs))
        return self.store.get(mid)

    # -- emoji --
    def test_emoji_basic(self):
        got = self._roundtrip("Meeting went great! \U0001f600\U0001f389\U0001f680")
        self.assertIn("\U0001f600", got.content)

    def test_emoji_zwj_sequence(self):
        """Zero-width joiner sequences (e.g. family emoji)."""
        family = "\U0001f468‍\U0001f469‍\U0001f467‍\U0001f466"
        got = self._roundtrip(f"Family: {family}")
        self.assertIn(family, got.content)

    def test_emoji_flags(self):
        flag_jp = "\U0001f1ef\U0001f1f5"
        got = self._roundtrip(f"Country: {flag_jp}")
        self.assertIn(flag_jp, got.content)

    # -- CJK --
    def test_cjk_chinese(self):
        text = "今天天气很好，我很开心"
        got = self._roundtrip(text)
        self.assertEqual(got.content, text)

    def test_cjk_japanese(self):
        text = "東京は美しい都市です"
        got = self._roundtrip(text)
        self.assertEqual(got.content, text)

    def test_cjk_korean(self):
        text = "안녕하세요, 세계!"
        got = self._roundtrip(text)
        self.assertEqual(got.content, text)

    def test_cjk_search(self):
        """Search with CJK content.  SQLite FTS5's default tokenizer doesn't
        segment CJK text, so substring matches may only work via the LIKE
        fallback.  We test that the full content string matches."""
        self.store.upsert(MemoryItem(content="東京は美しい都市です"))
        # FTS5 may not tokenize CJK into searchable terms, but the full string
        # or an empty query (recent-items fallback) should still surface it.
        hits = self.store.search("東京は美しい都市です")
        self.assertTrue(len(hits) >= 1)
        self.assertIn("東京", hits[0].content)

    # -- RTL --
    def test_arabic(self):
        text = "مرحبًا بالعالم"
        got = self._roundtrip(text)
        self.assertEqual(got.content, text)

    def test_hebrew(self):
        text = "שלום עולם"
        got = self._roundtrip(text)
        self.assertEqual(got.content, text)

    # -- mixed scripts --
    def test_mixed_scripts(self):
        text = "English 中文 АБВ مرحبا 123"
        got = self._roundtrip(text)
        self.assertEqual(got.content, text)

    # -- zero-width and special characters --
    def test_zero_width_space(self):
        text = "hello​world"
        got = self._roundtrip(text)
        self.assertIn("​", got.content)

    def test_zero_width_non_joiner(self):
        text = "soft‌hyphen"
        got = self._roundtrip(text)
        self.assertIn("‌", got.content)

    def test_bom_in_content(self):
        """A BOM character embedded in content should survive the round-trip."""
        text = "﻿Leading BOM marker"
        got = self._roundtrip(text)
        self.assertIn("BOM", got.content)

    # -- supplementary plane --
    def test_supplementary_plane_math(self):
        """Mathematical symbols from the SMP (U+1D400+)."""
        text = "Proof: \U0001d400\U0001d401\U0001d402 = \U0001d715f(x)"
        got = self._roundtrip(text)
        self.assertIn("\U0001d400", got.content)

    def test_musical_symbols(self):
        text = "Music: \U0001d11e \U0001d122"
        got = self._roundtrip(text)
        self.assertIn("\U0001d11e", got.content)

    # -- dedup with unicode --
    def test_dedup_normalizes_unicode_whitespace(self):
        """Content differing only in whitespace normalisation should dedup."""
        id1 = self.store.upsert(MemoryItem(content="今天  天气好", source="claude", type="fact"))
        id2 = self.store.upsert(MemoryItem(content="今天 天气好", source="claude", type="fact"))
        self.assertEqual(id1, id2)
        self.assertEqual(self.store.count(), 1)

    # -- markdown roundtrip --
    def test_markdown_roundtrip_unicode(self):
        """MemoryItem.to_markdown -> from_markdown preserves unicode content."""
        content = "User likes \U0001f363 and lives in 東京 (שלום)"
        m = MemoryItem(content=content, source="claude", type="fact", tags=["food", "日本"])
        md = m.to_markdown()
        back = MemoryItem.from_markdown(md)
        self.assertEqual(back.content, content)
        self.assertEqual(back.tags, ["food", "日本"])

    # -- import with unicode --
    def test_normalize_file_with_unicode(self):
        """normalize_file handles JSON with unicode content."""
        tmp = Path(self.tmp.name) / "unicode.json"
        data = {"memories": ["今天は良い天気です", "User likes \U0001f600"]}
        tmp.write_text(json.dumps(data, ensure_ascii=False), encoding="utf-8")
        items = normalize_file(tmp)
        self.assertEqual(len(items), 2)
        self.assertIn("\U0001f600", items[1].content)


# ---------------------------------------------------------------------------
# 4. Concurrent access
# ---------------------------------------------------------------------------


class TestConcurrentAccess(unittest.TestCase):
    """Multiple threads (simulating concurrent MCP clients) should be able
    to read and write the same vault without data corruption."""

    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()

    def tearDown(self):
        self.tmp.cleanup()

    def test_concurrent_writes_single_store(self):
        """Multiple threads writing through a single MemoryStore instance."""
        store = MemoryStore(self.tmp.name)
        errors = []
        n_per_thread = 50
        n_threads = 4

        def writer(thread_id):
            try:
                for i in range(n_per_thread):
                    store.upsert(
                        MemoryItem(
                            content=f"Thread {thread_id} memory {i}",
                            source="claude",
                            type="fact",
                        )
                    )
            except Exception as exc:
                errors.append(exc)

        threads = [threading.Thread(target=writer, args=(t,)) for t in range(n_threads)]
        for t in threads:
            t.start()
        for t in threads:
            t.join(timeout=30)

        store.close()
        self.assertEqual(errors, [])
        # Reopen and verify count.
        store2 = MemoryStore(self.tmp.name)
        self.assertEqual(store2.count(), n_per_thread * n_threads)
        store2.close()

    def test_concurrent_reads_and_writes(self):
        """Readers and writers operating simultaneously on one store."""
        store = MemoryStore(self.tmp.name)
        # Pre-populate so readers have something to find.
        for i in range(50):
            store.upsert(MemoryItem(content=f"Seed memory {i}", source="claude", type="fact"))

        errors = []
        read_results = []

        def writer():
            try:
                for i in range(50):
                    store.upsert(
                        MemoryItem(
                            content=f"Concurrent write {i}",
                            source="chatgpt",
                            type="note",
                        )
                    )
            except Exception as exc:
                errors.append(exc)

        def reader():
            try:
                for _ in range(50):
                    hits = store.search("Seed")
                    read_results.append(len(hits))
                    store.count()
                    store.stats()
            except Exception as exc:
                errors.append(exc)

        threads = [threading.Thread(target=writer) for _ in range(2)] + [
            threading.Thread(target=reader) for _ in range(2)
        ]
        for t in threads:
            t.start()
        for t in threads:
            t.join(timeout=30)

        store.close()
        self.assertEqual(errors, [])
        # All reads should have found at least some results.
        self.assertTrue(all(r > 0 for r in read_results))

    def test_concurrent_store_instances(self):
        """Two separate MemoryStore instances sharing the same vault directory.
        This simulates two processes (e.g. CLI + MCP server) hitting the same
        vault. SQLite WAL mode + busy_timeout should handle this."""
        vault = self.tmp.name
        # Pre-initialise the vault so competing MemoryStore constructors don't
        # race on schema creation (hits Windows SQLite WAL deadlocks).
        bootstrap = MemoryStore(vault)
        bootstrap.close()

        errors = []
        n_per_store = 50

        def store_writer(writer_id):
            s = None
            try:
                s = MemoryStore(vault)
                for i in range(n_per_store):
                    s.upsert(
                        MemoryItem(
                            content=f"Store-{writer_id} mem-{i}",
                            source="claude",
                            type="fact",
                        )
                    )
            except Exception as exc:
                errors.append(exc)
            finally:
                if s is not None:
                    s.close()

        threads = [threading.Thread(target=store_writer, args=(w,)) for w in range(3)]
        for t in threads:
            t.start()
        for t in threads:
            t.join(timeout=30)

        self.assertEqual(errors, [])
        # Verify all writes landed.
        final = MemoryStore(vault)
        self.assertEqual(final.count(), n_per_store * 3)
        final.close()

    def test_concurrent_service_remember_recall(self):
        """Multiple MemoryService instances (same store) doing remember+recall."""
        store = MemoryStore(self.tmp.name)
        svc = MemoryService(store)
        errors = []

        def worker(worker_id):
            try:
                for i in range(30):
                    svc.remember(
                        f"Worker-{worker_id} fact-{i}",
                        type="fact",
                        source="claude",
                    )
                    svc.recall(f"Worker-{worker_id}")
            except Exception as exc:
                errors.append(exc)

        threads = [threading.Thread(target=worker, args=(w,)) for w in range(3)]
        for t in threads:
            t.start()
        for t in threads:
            t.join(timeout=30)

        store.close()
        self.assertEqual(errors, [])

    def test_concurrent_delete_and_read(self):
        """Deleting memories while another thread reads should not crash."""
        store = MemoryStore(self.tmp.name)
        ids = []
        for i in range(100):
            ids.append(
                store.upsert(MemoryItem(content=f"Deletable {i}", source="claude", type="fact"))
            )
        errors = []

        def deleter():
            try:
                for mid in ids[:50]:
                    store.delete(mid)
            except Exception as exc:
                errors.append(exc)

        def reader():
            try:
                for mid in ids:
                    store.get(mid)  # may return None for deleted items -- that's fine
                    store.search("Deletable")
            except Exception as exc:
                errors.append(exc)

        threads = [
            threading.Thread(target=deleter),
            threading.Thread(target=reader),
            threading.Thread(target=reader),
        ]
        for t in threads:
            t.start()
        for t in threads:
            t.join(timeout=30)

        store.close()
        self.assertEqual(errors, [])


if __name__ == "__main__":
    unittest.main()
