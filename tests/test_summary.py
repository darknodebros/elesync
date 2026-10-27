"""Tests for elesync.summary — vault digest."""

import shutil
import tempfile
import unittest

from elesync.models import MemoryItem
from elesync.store import MemoryStore
from elesync.summary import build_summary


class TestBuildSummary(unittest.TestCase):
    def _tmp_vault_dir(self):
        d = tempfile.mkdtemp()
        self.addCleanup(shutil.rmtree, d, ignore_errors=True)
        return d

    def _store_with_items(self, items):
        d = self._tmp_vault_dir()
        store = MemoryStore(vault_dir=d)
        try:
            store.import_many(items)
        finally:
            store.close()
        return d

    def test_empty_vault(self):
        d = self._tmp_vault_dir()
        out = build_summary(vault_dir=d)
        self.assertIn("empty", out.lower())
        self.assertIn("ele import", out)

    def test_full_summary_contains_content(self):
        items = [
            MemoryItem(content="Lives in Madrid", type="fact", source="chatgpt"),
            MemoryItem(content="Prefers dark mode", type="preference", source="claude"),
            MemoryItem(content="Working on EleSync", type="project", source="gemini"),
        ]
        d = self._store_with_items(items)
        out = build_summary(vault_dir=d)
        self.assertIn("Lives in Madrid", out)
        self.assertIn("Prefers dark mode", out)
        self.assertIn("Working on EleSync", out)
        self.assertIn("chatgpt", out)
        self.assertIn("claude", out)
        self.assertIn("gemini", out)

    def test_short_mode(self):
        items = [
            MemoryItem(content="Lives in Madrid", type="fact", source="chatgpt"),
            MemoryItem(content="Loves coffee", type="fact", source="claude"),
        ]
        d = self._store_with_items(items)
        out = build_summary(vault_dir=d, short=True)
        # Short mode should show counts and top facts
        self.assertIn("Facts", out)
        self.assertIn("ele summary", out)

    def test_source_filter(self):
        items = [
            MemoryItem(content="From ChatGPT only", type="note", source="chatgpt"),
            MemoryItem(content="From Claude only", type="note", source="claude"),
        ]
        d = self._store_with_items(items)
        out = build_summary(vault_dir=d, source="chatgpt")
        self.assertIn("From ChatGPT only", out)
        self.assertNotIn("From Claude only", out)

    def test_source_filter_no_results(self):
        items = [MemoryItem(content="Something", type="note", source="claude")]
        d = self._store_with_items(items)
        out = build_summary(vault_dir=d, source="chatgpt")
        self.assertIn("No memories found", out)

    def test_header_shows_total_and_sources(self):
        items = [
            MemoryItem(content="Memory A", type="fact", source="chatgpt"),
            MemoryItem(content="Memory B", type="fact", source="claude"),
        ]
        d = self._store_with_items(items)
        out = build_summary(vault_dir=d)
        self.assertIn("2 memories", out)
        self.assertIn("chatgpt", out)
        self.assertIn("claude", out)

    def test_long_content_truncated(self):
        long_text = "A" * 120
        items = [MemoryItem(content=long_text, type="note", source="chatgpt")]
        d = self._store_with_items(items)
        out = build_summary(vault_dir=d)
        # Should be truncated with ellipsis, not the full 120 chars as one token
        self.assertIn("…", out)
