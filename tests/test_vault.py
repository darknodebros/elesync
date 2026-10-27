"""Test suite — runs with stdlib only (no MCP SDK needed).

python -m unittest discover -s tests -v
"""

import contextlib
import io
import json
import tempfile
import unittest
from pathlib import Path

from elesync import cli, normalize
from elesync.models import MEMORY_TYPES, MemoryItem
from elesync.store import MemoryStore

SAMPLES = Path(__file__).parent / "sample_data"


class TestMemoryItem(unittest.TestCase):
    def test_defaults_and_id(self):
        m = MemoryItem(content="hello")
        self.assertEqual(m.source, "manual")
        self.assertEqual(m.type, "note")
        self.assertTrue(m.id)
        self.assertTrue(m.content_hash)

    def test_invalid_type_falls_back_to_note(self):
        self.assertEqual(MemoryItem(content="x", type="nonsense").type, "note")

    def test_confidence_clamped(self):
        self.assertEqual(MemoryItem(content="x", confidence=5).confidence, 1.0)
        self.assertEqual(MemoryItem(content="x", confidence=-1).confidence, 0.0)

    def test_hash_is_whitespace_and_case_insensitive(self):
        a = MemoryItem(content="Hello  World", source="x", type="fact")
        b = MemoryItem(content="hello world", source="x", type="fact")
        self.assertEqual(a.content_hash, b.content_hash)

    def test_hash_differs_by_source(self):
        a = MemoryItem(content="same", source="chatgpt", type="fact")
        b = MemoryItem(content="same", source="claude", type="fact")
        self.assertNotEqual(a.content_hash, b.content_hash)

    def test_markdown_roundtrip_fields(self):
        m = MemoryItem(content="body text", type="fact", tags=["a", "b"])
        md = m.to_markdown()
        self.assertIn("type: fact", md)
        self.assertIn("tags: [a, b]", md)
        self.assertIn("body text", md)

    def test_all_types_accepted(self):
        for t in MEMORY_TYPES:
            self.assertEqual(MemoryItem(content="x", type=t).type, t)

    def test_from_markdown_roundtrip_preserves_identity(self):
        m = MemoryItem(
            content="Alex lives in Westbrook",
            source="claude",
            type="fact",
            tags=["place", "home"],
            confidence=0.9,
            source_ref="claude-export",
        )
        back = MemoryItem.from_markdown(m.to_markdown())
        self.assertEqual(back.id, m.id)
        self.assertEqual(back.content, m.content)
        self.assertEqual(back.source, m.source)
        self.assertEqual(back.type, m.type)
        self.assertEqual(back.tags, m.tags)
        self.assertEqual(back.content_hash, m.content_hash)
        self.assertEqual(back.confidence, m.confidence)

    def test_from_markdown_without_frontmatter_raises(self):
        with self.assertRaises(ValueError):
            MemoryItem.from_markdown("just some text, no front-matter")


class TestStore(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        self.store = MemoryStore(self.tmp.name)
        self.addCleanup(self.store.close)

    def tearDown(self):
        pass

    def test_upsert_and_get(self):
        m = MemoryItem(content="Alex lives in Westbrook", source="claude", type="fact")
        mid = self.store.upsert(m)
        got = self.store.get(mid)
        self.assertEqual(got.content, "Alex lives in Westbrook")
        self.assertEqual(got.source, "claude")

    def test_markdown_file_written(self):
        m = MemoryItem(content="written to disk", type="note")
        mid = self.store.upsert(m)
        self.assertTrue((Path(self.tmp.name) / "notes" / f"{mid}.md").exists())

    def test_dedup_is_idempotent(self):
        m1 = MemoryItem(content="Repeated fact", source="chatgpt", type="fact")
        m2 = MemoryItem(content="repeated   fact", source="chatgpt", type="fact")
        id1 = self.store.upsert(m1)
        id2 = self.store.upsert(m2)
        self.assertEqual(id1, id2)
        self.assertEqual(self.store.count(), 1)

    def test_search_finds_content(self):
        self.store.upsert(MemoryItem(content="The Helios project is active", type="project"))
        self.store.upsert(MemoryItem(content="Unrelated note about coffee", type="note"))
        hits = self.store.search("Helios")
        self.assertEqual(len(hits), 1)
        self.assertIn("Helios", hits[0].content)

    def test_search_filters(self):
        self.store.upsert(MemoryItem(content="alpha topic", source="chatgpt", type="fact"))
        self.store.upsert(MemoryItem(content="alpha topic", source="claude", type="fact"))
        self.assertEqual(len(self.store.search("alpha", source="claude")), 1)

    def test_delete(self):
        mid = self.store.upsert(MemoryItem(content="to be deleted"))
        self.assertTrue(self.store.delete(mid))
        self.assertIsNone(self.store.get(mid))
        self.assertFalse((Path(self.tmp.name) / "notes" / f"{mid}.md").exists())
        self.assertFalse(self.store.delete("nonexistent"))

    def test_stats(self):
        self.store.upsert(MemoryItem(content="a", source="chatgpt", type="fact"))
        self.store.upsert(MemoryItem(content="b", source="claude", type="project"))
        s = self.store.stats()
        self.assertEqual(s["total"], 2)
        self.assertEqual(s["by_source"]["chatgpt"], 1)
        self.assertEqual(s["by_type"]["project"], 1)

    def test_empty_query_returns_recent(self):
        # Explicit, well-separated timestamps rather than relying on
        # real-time capture: created_at has second-level precision (_now()
        # truncates microseconds), so three upserts in a tight loop can
        # easily land in the same second, which would make an ordering
        # assertion pass trivially regardless of whether ORDER BY actually
        # works. Inserted out of chronological order too, so the test can't
        # pass by coincidentally matching insertion order instead of
        # actually sorting by created_at.
        self.store.upsert(MemoryItem(content="oldest", created_at="2026-01-01T00:00:00+00:00"))
        self.store.upsert(MemoryItem(content="newest", created_at="2026-01-01T02:00:00+00:00"))
        self.store.upsert(MemoryItem(content="middle", created_at="2026-01-01T01:00:00+00:00"))
        hits = self.store.search("")
        self.assertEqual(len(hits), 3)
        self.assertEqual([h.content for h in hits], ["newest", "middle", "oldest"])

    def test_upsert_status_reports_insert_then_dedup(self):
        m = MemoryItem(content="distinct fact", source="chatgpt", type="fact")
        mid, inserted = self.store.upsert_status(m)
        self.assertTrue(inserted)
        same_id, inserted2 = self.store.upsert_status(
            MemoryItem(content="distinct fact", source="chatgpt", type="fact")
        )
        self.assertFalse(inserted2)
        self.assertEqual(same_id, mid)
        self.assertEqual(self.store.count(), 1)

    def test_reindex_rebuilds_index_from_markdown(self):
        ids = [
            self.store.upsert(MemoryItem(content=c, source="claude", type="fact"))
            for c in ("Lives in Westbrook", "Has a younger sister", "Runs the Helios project")
        ]
        # Simulate a lost/stale index (e.g. fresh machine after a git pull of notes/).
        self.store.db.execute("DELETE FROM memories")
        self.store.db.commit()
        self.assertEqual(self.store.count(), 0)

        res = self.store.reindex()
        self.assertEqual(res["reindexed"], 3)
        self.assertEqual(self.store.count(), 3)
        # Ids preserved and search works again off the rebuilt index.
        self.assertIsNotNone(self.store.get(ids[0]))
        self.assertEqual(len(self.store.search("Helios")), 1)

    def test_sync_reports_added_changed_removed(self):
        keep = self.store.upsert(MemoryItem(content="keep me", type="note"))
        delete = self.store.upsert(MemoryItem(content="delete me", type="note"))
        edit_id = self.store.upsert(MemoryItem(content="original body", type="note"))
        notes = Path(self.tmp.name) / "notes"

        # Simulate a folder sync from another device: one added, one removed, one edited.
        new = MemoryItem(content="added on another device", type="note")
        (notes / f"{new.id}.md").write_text(new.to_markdown(), encoding="utf-8")
        (notes / f"{delete}.md").unlink()
        edited = MemoryItem(content="edited body now", type="note", id=edit_id)
        (notes / f"{edit_id}.md").write_text(edited.to_markdown(), encoding="utf-8")

        res = self.store.sync()
        self.assertEqual((res["added"], res["changed"], res["removed"]), (1, 1, 1))
        self.assertEqual(res["total"], 3)
        # Index now matches the files on disk.
        self.assertEqual(len(self.store.search("edited body now")), 1)
        self.assertIsNone(self.store.get(delete))  # the removed note is gone
        self.assertIsNotNone(self.store.get(keep))


def _run_cli(argv) -> int:
    """Run the CLI, swallowing its stdout so test logs stay clean."""
    with contextlib.redirect_stdout(io.StringIO()):
        return cli.main(argv)


def _run_cli_capture_stderr(argv):
    """Run the CLI, capturing stderr text alongside the return code.
    Some code paths (e.g. _store()'s vault-not-found check) raise SystemExit
    directly rather than returning an int, matching how the real entry point
    (sys.exit(main())) is meant to consume it -- so we catch that here too."""
    buf = io.StringIO()
    with contextlib.redirect_stdout(io.StringIO()), contextlib.redirect_stderr(buf):
        try:
            rc = cli.main(argv)
        except SystemExit as e:
            rc = e.code
    return rc, buf.getvalue()


class TestCLI(unittest.TestCase):
    def test_doctor_fix_wires_config(self):
        with tempfile.TemporaryDirectory() as d:
            vault = Path(d) / "vault"
            config = Path(d) / "config.json"
            rc = _run_cli(["--vault", str(vault), "doctor", "--fix", "--config-path", str(config)])
            self.assertEqual(rc, 0)
            self.assertTrue(vault.exists())
            self.assertTrue((vault / "notes").exists())
            self.assertTrue(config.exists())
            data = json.loads(config.read_text(encoding="utf-8"))
            self.assertIn("elesync", data["mcpServers"])
            self.assertEqual(data["mcpServers"]["elesync"]["args"], ["-m", "elesync.mcp_server"])

    def test_missing_vault_error_uses_clean_em_dash(self):
        """Regression test: the 'Vault not found' message used to embed raw
        hex-escape bytes (\\xe2\\x80\\x94) instead of a real em-dash, which
        Python renders as mojibake ('â' plus two invisible control chars)
        rather than the intended '—'. Guards against that regressing."""
        with tempfile.TemporaryDirectory() as d:
            missing = Path(d) / "never-created"
            rc, err = _run_cli_capture_stderr(["--vault", str(missing), "add", "test"])
            self.assertEqual(rc, 1)
            self.assertIn("—", err)
            self.assertNotIn("\xe2\x80\x94", err)
            self.assertNotIn("\x80", err)
            self.assertNotIn("\x94", err)

    def test_cli_source_contains_no_committed_mojibake(self):
        source = (Path(__file__).parents[1] / "elesync" / "cli.py").read_text(encoding="utf-8")
        controls = [char for char in source if 0x80 <= ord(char) < 0xA0]
        self.assertEqual(controls, [])
        for marker in ("Ã", "Â", "â", "ð", "ï", "�"):
            self.assertNotIn(marker, source)


class TestExportRoundTrip(unittest.TestCase):
    def test_export_then_import_is_idempotent_across_sources(self):
        with (
            tempfile.TemporaryDirectory() as src_dir,
            tempfile.TemporaryDirectory() as dst_dir,
            tempfile.TemporaryDirectory() as out_dir,
        ):
            # Build a mixed-source vault.
            store = MemoryStore(src_dir)
            store.import_many(
                normalize.normalize_file(SAMPLES / "chatgpt_memory.json", source="chatgpt")
            )
            store.import_many(
                normalize.normalize_file(SAMPLES / "claude_memory.json", source="claude")
            )
            original = store.count()
            store.close()

            out = Path(out_dir) / "backup.json"
            self.assertEqual(_run_cli(["--vault", src_dir, "export", str(out)]), 0)

            payload = json.loads(out.read_text(encoding="utf-8"))
            self.assertTrue(payload["elesync_export"])
            self.assertEqual(payload["count"], original)

            # Import into a fresh vault: every memory lands, none lost or duplicated.
            self.assertEqual(_run_cli(["--vault", dst_dir, "import", str(out)]), 0)
            dst = MemoryStore(dst_dir)
            self.assertEqual(dst.count(), original)
            # Re-importing the same export is a no-op (the round-trip is idempotent).
            again = dst.import_many(normalize.normalize_file(out))
            self.assertEqual(again["added"], 0)
            self.assertEqual(dst.count(), original)
            dst.close()


class TestForgetCLI(unittest.TestCase):
    def test_forget_by_prefix_deletes_and_exits_zero(self):
        with tempfile.TemporaryDirectory() as d:
            self.assertEqual(_run_cli(["--vault", d, "add", "delete me", "--type", "note"]), 0)
            store = MemoryStore(d)
            memories = store.all()
            self.assertGreater(len(memories), 0)
            mem_id = memories[0].id
            store.close()
            self.assertEqual(_run_cli(["--vault", d, "forget", mem_id[:8]]), 0)
            store2 = MemoryStore(d)
            self.assertEqual(store2.count(), 0)
            store2.close()

    def test_forget_missing_id_exits_nonzero(self):
        with tempfile.TemporaryDirectory() as d:
            rc = _run_cli(["--vault", d, "forget", "deadbeef"])
            self.assertEqual(rc, 1)

    def test_forget_ambiguous_prefix_exits_nonzero(self):
        with tempfile.TemporaryDirectory() as d:
            # Use explicit ids with a known shared prefix so the ambiguous-match
            # path is guaranteed rather than relying on random UUID collisions.
            shared_prefix = "abcd1234"
            target_id_length = len(MemoryItem(content="length probe").id)
            suffix_len = target_id_length - len(shared_prefix)
            self.assertGreater(suffix_len, 0)
            store = MemoryStore(d)
            store.upsert(MemoryItem(content="first memory", id=shared_prefix + "0" * suffix_len))
            store.upsert(MemoryItem(content="second memory", id=shared_prefix + "1" * suffix_len))
            original_count = store.count()
            store.close()

            self.assertEqual(_run_cli(["--vault", d, "forget", shared_prefix]), 1)

            verify = MemoryStore(d)
            self.assertEqual(verify.count(), original_count)
            verify.close()


class TestSyncCLI(unittest.TestCase):
    def test_sync_writes_gitignore_for_the_index(self):
        with tempfile.TemporaryDirectory() as d:
            self.assertEqual(_run_cli(["--vault", d, "add", "a memory", "--type", "note"]), 0)
            self.assertEqual(_run_cli(["--vault", d, "sync"]), 0)
            gitignore = (Path(d) / ".gitignore").read_text(encoding="utf-8")
            self.assertIn("index.db", gitignore)
            # Idempotent: running sync again doesn't duplicate the entry.
            self.assertEqual(_run_cli(["--vault", d, "sync"]), 0)
            self.assertEqual((Path(d) / ".gitignore").read_text(encoding="utf-8"), gitignore)


class TestServeWithoutSDK(unittest.TestCase):
    def test_serve_fails_cleanly_when_mcp_sdk_missing(self):
        """`ele serve` without the MCP SDK prints one clear line — no traceback."""
        import unittest.mock as mock

        from elesync import mcp_server

        err = io.StringIO()
        with (
            tempfile.TemporaryDirectory() as d,
            mock.patch.object(mcp_server, "FastMCP", None),
            contextlib.redirect_stderr(err),
        ):
            rc = _run_cli(["--vault", d, "serve"])
        self.assertEqual(rc, 1)
        self.assertIn("MCP SDK", err.getvalue())
        self.assertIn('pip install "mcp>=1.2.0,<2"', err.getvalue())
        self.assertNotIn("Traceback", err.getvalue())


class TestAdapters(unittest.TestCase):
    def test_chatgpt_memory_with_dedup_within_file(self):
        items = normalize.normalize_file(SAMPLES / "chatgpt_memory.json", source="chatgpt")
        # 4 entries but two are identical -> store dedups; adapter itself yields 4
        self.assertEqual(len(items), 4)
        self.assertTrue(all(i.source == "chatgpt" for i in items))
        prefs = [i for i in items if i.type == "preference"]
        self.assertEqual(len(prefs), 1)

    def test_claude_memory_types_preserved(self):
        items = normalize.normalize_file(SAMPLES / "claude_memory.json", source="claude")
        self.assertEqual(len(items), 3)
        types = {i.type for i in items}
        self.assertEqual(types, {"fact", "project", "relationship"})

    def test_grok_memory_with_dedup_within_file(self):
        items = normalize.normalize_file(SAMPLES / "grok_memory.json", source="grok")
        # 4 entries, two identical -> store would dedup; the adapter itself yields 4
        self.assertEqual(len(items), 4)
        self.assertTrue(all(i.source == "grok" for i in items))
        prefs = [i for i in items if i.type == "preference"]
        self.assertEqual(len(prefs), 1)
        # importing dedups the repeated fact: 4 entries -> 3 stored
        with tempfile.TemporaryDirectory() as d:
            store = MemoryStore(d)
            self.assertEqual(store.import_many(items)["added"], 3)
            store.close()

    def test_claude_conversations_json_is_not_labelled_chatgpt(self):
        # Both vendors export conversations.json; Claude's shape must win over the default.
        data = json.loads((SAMPLES / "claude_conversations.json").read_text(encoding="utf-8"))
        self.assertEqual(normalize._guess_source(Path("conversations.json"), data), "claude")
        chatgpt_shape = [{"title": "t", "mapping": {}}]
        self.assertEqual(
            normalize._guess_source(Path("conversations.json"), chatgpt_shape), "chatgpt"
        )

    def test_grok_autodetected_by_structure(self):
        # No --source and a name without "grok": the grok_memories key routes it.
        self.assertEqual(
            normalize._guess_source(Path("export.json"), {"grok_memories": []}), "grok"
        )
        items = normalize.normalize_file(SAMPLES / "grok_memory.json")
        self.assertTrue(all(i.source == "grok" for i in items))

    def test_deepseek_memory_with_dedup_within_file(self):
        items = normalize.normalize_file(SAMPLES / "deepseek_memory.json", source="deepseek")
        # 4 entries, two identical -> store would dedup; the adapter itself yields 4
        self.assertEqual(len(items), 4)
        self.assertTrue(all(i.source == "deepseek" for i in items))
        prefs = [i for i in items if i.type == "preference"]
        self.assertEqual(len(prefs), 1)
        # importing dedups the repeated fact: 4 entries -> 3 stored
        with tempfile.TemporaryDirectory() as d:
            store = MemoryStore(d)
            self.assertEqual(store.import_many(items)["added"], 3)
            store.close()

    def test_deepseek_autodetected_by_structure(self):
        # No --source and a name without "deepseek": the deepseek_memories key routes it.
        self.assertEqual(
            normalize._guess_source(Path("export.json"), {"deepseek_memories": []}), "deepseek"
        )
        items = normalize.normalize_file(SAMPLES / "deepseek_memory.json")
        self.assertTrue(all(i.source == "deepseek" for i in items))

    def test_perplexity_memory_with_dedup_within_file(self):
        items = normalize.normalize_file(SAMPLES / "perplexity_memory.json", source="perplexity")
        self.assertEqual(len(items), 4)
        self.assertTrue(all(i.source == "perplexity" for i in items))
        self.assertEqual(len([i for i in items if i.type == "preference"]), 1)
        with tempfile.TemporaryDirectory() as d:
            store = MemoryStore(d)
            self.assertEqual(store.import_many(items)["added"], 3)
            store.close()

    def test_copilot_autodetected_by_structure(self):
        self.assertEqual(
            normalize._guess_source(Path("export.json"), {"copilot_memories": []}), "copilot"
        )
        items = normalize.normalize_file(SAMPLES / "copilot_memory.json")
        self.assertEqual(len(items), 4)
        self.assertTrue(all(i.source == "copilot" for i in items))

    def test_conversations_auto_detected(self):
        items = normalize.normalize_file(SAMPLES / "gemini_conversations.json")
        self.assertEqual(len(items), 1)
        self.assertEqual(items[0].type, "conversation")
        self.assertIn("investor outreach plan", items[0].content)
        self.assertIn("user:", items[0].content)

    def test_chatgpt_mapping_uses_active_branch_in_parent_child_order(self):
        data = [
            {
                "id": "conversation-1",
                "title": "Branched conversation",
                "current_node": "answer-final",
                "mapping": {
                    "answer-final": {
                        "parent": "question",
                        "message": {
                            "author": {"role": "assistant"},
                            "content": {"parts": ["Final answer"]},
                        },
                    },
                    "root": {"parent": None, "message": None},
                    "question": {
                        "parent": "root",
                        "message": {
                            "author": {"role": "user"},
                            "content": {"parts": ["Question first"]},
                        },
                    },
                    "answer-old": {
                        "parent": "question",
                        "message": {
                            "author": {"role": "assistant"},
                            "content": {"parts": ["Superseded answer"]},
                        },
                    },
                },
            }
        ]

        items = normalize.from_conversations(data, source="chatgpt")

        self.assertEqual(len(items), 1)
        self.assertLess(
            items[0].content.index("Question first"), items[0].content.index("Final answer")
        )
        self.assertNotIn("Superseded answer", items[0].content)

    # ------------------------------------------------------------------
    # Gemini Takeout (MyActivity.json with listItems) — added by
    # install_gemini_adapter.py
    # ------------------------------------------------------------------

    def test_gemini_takeout_myactivity_conversations(self):
        """Gemini Takeout MyActivity.json with listItems/payload/messages
        is parsed into conversation MemoryItems."""
        items = normalize.normalize_file(
            SAMPLES / "gemini_takeout_myactivity.json", source="gemini"
        )
        self.assertGreaterEqual(len(items), 1)
        self.assertTrue(all(i.type == "conversation" for i in items))
        self.assertTrue(all(i.source == "gemini" for i in items))
        # First conversation content should be present
        body = items[0].content
        self.assertIn("investor outreach plan", body.lower())
        self.assertIn("user:", body)

    def test_gemini_takeout_auto_detected(self):
        """listItems key in data triggers gemini source detection."""
        import json

        data = json.loads((SAMPLES / "gemini_takeout_myactivity.json").read_text(encoding="utf-8"))
        self.assertEqual(normalize._guess_source(Path("MyActivity.json"), data), "gemini")

    def test_gemini_takeout_role_normalisation(self):
        """Gemini uses 'model' role — verify it becomes 'assistant:' in output."""
        items = normalize.normalize_file(
            SAMPLES / "gemini_takeout_myactivity.json", source="gemini"
        )
        self.assertTrue(len(items) >= 1)
        body = items[0].content
        self.assertIn("assistant:", body)
        self.assertNotIn("model:", body)

    def test_gemini_takeout_multiple_conversations(self):
        """Two conversations in the fixture should produce two MemoryItems."""
        items = normalize.normalize_file(
            SAMPLES / "gemini_takeout_myactivity.json", source="gemini"
        )
        self.assertEqual(len(items), 2)
        titles = [i.content.split("\n")[0] for i in items]
        self.assertTrue(any("investor" in t.lower() for t in titles))
        self.assertTrue(any("python" in t.lower() or "debug" in t.lower() for t in titles))

    def test_gemini_activity_data_yields_nothing(self):
        """Non-conversation activity data (steps, fitness) should produce
        zero MemoryItems without crashing."""
        items = normalize.normalize_file(
            SAMPLES / "gemini_activity_not_conversations.json", source="gemini"
        )
        self.assertEqual(len(items), 0)

    def test_claude_data_export_conversations_parsed(self):
        """Claude.ai's real 'Export Data' shape: bare list of
        {name, chat_messages: [{sender, text, content: [blocks]}]} --
        different from the generic {messages: [{role, content}]} shape."""
        items = normalize.normalize_file(SAMPLES / "claude_conversations.json", source="claude")
        # Only the conversation with real text content should produce a memory;
        # the empty-chat_messages and empty-content ones are correctly skipped.
        self.assertEqual(len(items), 1)
        self.assertEqual(items[0].type, "conversation")
        self.assertEqual(items[0].source, "claude")
        self.assertIn("Trip planning", items[0].content)
        self.assertIn("human:", items[0].content)
        self.assertIn("mountains", items[0].content)
        self.assertIn("cabin", items[0].content)

    def test_claude_project_docs_imported(self):
        """Claude.ai Project export ({uuid, name, docs: [...]}) -- the docs
        array holds real attached content, not just metadata."""
        items = normalize.normalize_file(SAMPLES / "claude_project.json", source="claude")
        self.assertEqual(len(items), 2)
        self.assertTrue(all(i.type == "project" for i in items))
        self.assertTrue(all(i.tags == ["Sample Project"] for i in items))
        bodies = " ".join(i.content for i in items)
        self.assertIn("ship the thing by Friday", bodies)
        self.assertIn("single static HTML file", bodies)

    def test_claude_project_with_no_docs_imports_nothing(self):
        """An empty docs: [] is correctly 0 -- nothing to extract, not a bug."""
        items = normalize.normalize_file(SAMPLES / "claude_project_empty.json", source="claude")
        self.assertEqual(len(items), 0)

    def test_claude_memories_export_conversations_and_project(self):
        """Claude.ai's real memories.json shape: conversations_memory (one
        narrative) + project_memories (per-project narratives), not a flat
        fact list."""
        items = normalize.normalize_file(SAMPLES / "claude_memories_export.json", source="claude")
        self.assertEqual(len(items), 2)
        notes = [i for i in items if i.type == "note"]
        projects = [i for i in items if i.type == "project"]
        self.assertEqual(len(notes), 1)
        self.assertEqual(len(projects), 1)
        self.assertIn("concise code review", notes[0].content)
        self.assertIn("SQLite", projects[0].content)
        self.assertEqual(projects[0].tags, ["00000000-0000-0000-0000-000000000001"])

    def test_copilot_csv_groups_by_conversation_and_restores_order(self):
        """Microsoft Copilot's Privacy Dashboard CSV export: rows are
        reverse-chronological within each conversation title; we should
        restore correct (oldest-first) order and skip rows/threads with no
        real message content."""
        items = normalize.normalize_file(SAMPLES / "copilot_activity.csv", source="copilot")
        # "Empty Thread" has no real messages -> 0 memories from it.
        # The empty-title row contributes nothing.
        # Only "Weekend Trip Planning" has real content -> 1 memory.
        self.assertEqual(len(items), 1)
        item = items[0]
        self.assertEqual(item.source, "copilot")
        self.assertEqual(item.type, "conversation")
        self.assertIn("Weekend Trip Planning", item.content)
        # Human's message (10:04) should come before AI's reply (10:05),
        # even though the file lists AI's row first (reverse-chronological).
        human_pos = item.content.index("Human: I want to plan")
        ai_pos = item.content.index("AI: Sounds fun")
        self.assertLess(human_pos, ai_pos)

    def test_copilot_csv_unrecognized_columns_raises(self):
        import tempfile

        with tempfile.TemporaryDirectory() as d:
            bad_csv = Path(d) / "bad.csv"
            bad_csv.write_text("Foo,Bar\n1,2\n", encoding="utf-8")
            with self.assertRaises(ValueError):
                normalize.normalize_file(bad_csv, source="copilot")

    def test_source_autodetect_from_filename(self):
        items = normalize.normalize_file(SAMPLES / "claude_memory.json")
        self.assertTrue(all(i.source == "claude" for i in items))

    def test_end_to_end_import_dedups_across_sources_correctly(self):
        with tempfile.TemporaryDirectory() as d:
            store = MemoryStore(d)
            for f, src in [
                ("chatgpt_memory.json", "chatgpt"),
                ("claude_memory.json", "claude"),
                ("gemini_conversations.json", None),
            ]:
                store.import_many(normalize.normalize_file(SAMPLES / f, source=src))
            # 3 unique chatgpt + 3 claude + 1 conversation = 7
            self.assertEqual(store.count(), 7)
            # re-import is a no-op (idempotent)
            again = store.import_many(
                normalize.normalize_file(SAMPLES / "chatgpt_memory.json", source="chatgpt")
            )
            self.assertEqual(again["added"], 0)
            self.assertEqual(store.count(), 7)
            store.close()


if __name__ == "__main__":
    unittest.main()
