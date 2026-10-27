"""Direct, typed links between memories.

A link lives on its source memory's note (the source of truth), so it survives
reindex, sync, backup and encryption with the note itself, and is mirrored into
the index for queries.
"""

from __future__ import annotations

import asyncio
import tempfile
import unittest
from unittest import mock

from elesync import scopes as scopes_mod
from elesync.graph import memory_graph
from elesync.models import MAX_LINKS, MemoryItem, normalize_links
from elesync.service import MemoryService
from elesync.store import MemoryStore

OTHER = "b" * 32


class TestNormalizeLinks(unittest.TestCase):
    def test_keeps_valid_links_in_order_without_duplicates(self):
        raw = [f"caused_by:{OTHER}", f"caused_by:{OTHER}", f"related:{'c' * 32}"]
        self.assertEqual(normalize_links(raw), [f"caused_by:{OTHER}", f"related:{'c' * 32}"])

    def test_drops_unknown_relations_and_malformed_targets(self):
        raw = f"replaces:{OTHER}, related:, related:bad id!, nonsense, supports:{OTHER}"
        self.assertEqual(normalize_links(raw), [f"supports:{OTHER}"])

    def test_relation_is_case_insensitive(self):
        self.assertEqual(normalize_links([f"Depends_On:{OTHER}"]), [f"depends_on:{OTHER}"])

    def test_is_capped(self):
        raw = [f"related:{i:032x}" for i in range(MAX_LINKS + 5)]
        self.assertEqual(len(normalize_links(raw)), MAX_LINKS)

    def test_item_drops_a_link_to_itself(self):
        item = MemoryItem(content="x", id="a" * 32, links=[f"related:{'a' * 32}"])
        self.assertEqual(item.links, [])


class TestLinkMarkdown(unittest.TestCase):
    def test_links_round_trip_through_markdown(self):
        item = MemoryItem(content="Body", links=[f"caused_by:{OTHER}"])
        text = item.to_markdown()
        self.assertIn(f"links: [caused_by:{OTHER}]\n", text)
        self.assertEqual(MemoryItem.from_markdown(text).links, [f"caused_by:{OTHER}"])

    def test_unlinked_note_frontmatter_is_unchanged(self):
        self.assertNotIn("links:", MemoryItem(content="Body").to_markdown())


class StoreCase(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.store = MemoryStore(self.tmp.name)

    def tearDown(self):
        self.store.close()
        self.tmp.cleanup()

    def add(self, content, **kwargs):
        item = MemoryItem(content=content, **kwargs)
        self.store.upsert(item)
        return item


class TestStoreLinks(StoreCase):
    def test_set_links_writes_index_and_note(self):
        a, b = self.add("A"), self.add("B")
        self.assertTrue(self.store.set_links(a.id, [f"caused_by:{b.id}"]))
        self.assertEqual(self.store.get(a.id).links, [f"caused_by:{b.id}"])
        note = self.store._read_note(self.store._note_path(a.id))
        self.assertIn(f"caused_by:{b.id}", note)
        self.assertFalse(self.store.set_links("missing", []))

    def test_links_survive_reindex(self):
        a, b = self.add("A"), self.add("B")
        self.store.set_links(a.id, [f"supports:{b.id}"])
        self.store.reindex()
        self.assertEqual(self.store.get(a.id).links, [f"supports:{b.id}"])

    def test_other_writes_keep_links(self):
        a, b, c = self.add("A"), self.add("B"), self.add("C")
        self.store.set_links(a.id, [f"related:{b.id}"])
        self.store.set_scopes(a.id, ["legal"])
        self.store.supersede(a.id, c.id, "2026-10-04T00:00:00+00:00")
        item = self.store.get(a.id)
        item.content = "A, edited"
        item.content_hash = item.compute_hash()
        self.store.update(item)
        self.store.reindex()
        self.assertEqual(self.store.get(a.id).links, [f"related:{b.id}"])

    def test_incoming_links(self):
        a, b, c = self.add("A"), self.add("B"), self.add("C")
        self.store.set_links(a.id, [f"depends_on:{c.id}"])
        self.store.set_links(b.id, [f"contradicts:{c.id}", f"related:{a.id}"])
        self.assertEqual(
            sorted(self.store.incoming_links(c.id)),
            sorted([(a.id, "depends_on"), (b.id, "contradicts")]),
        )
        self.assertEqual(self.store.incoming_links(b.id), [])

    def test_delete_succeeds_when_a_linking_note_cannot_be_rewritten(self):
        a, b = self.add("A"), self.add("B")
        self.store.set_links(a.id, [f"related:{b.id}"])
        with mock.patch.object(self.store, "_write_note", side_effect=OSError("disk full")):
            with self.assertLogs("elesync.store", level="WARNING"):
                self.assertTrue(self.store.delete(b.id))
        self.assertIsNone(self.store.get(b.id))
        self.assertEqual(self.store.get(a.id).links, [f"related:{b.id}"])

    def test_deleting_a_memory_removes_links_pointing_at_it(self):
        a, b = self.add("A"), self.add("B")
        self.store.set_links(a.id, [f"related:{b.id}"])
        self.store.delete(b.id)
        self.assertEqual(self.store.get(a.id).links, [])
        self.store.reindex()
        self.assertEqual(self.store.get(a.id).links, [])


class TestLinkService(StoreCase):
    def svc(self, **kwargs):
        return MemoryService(self.store, **kwargs)

    def test_link_and_unlink(self):
        a, b = self.add("Chose SQLite"), self.add("Needs to run offline")
        msg = self.svc().link_memories(a.id[:8], b.id[:8], "caused_by")
        self.assertIn("Linked", msg)
        self.assertEqual(self.store.get(a.id).links, [f"caused_by:{b.id}"])
        self.assertIn("already", self.svc().link_memories(a.id, b.id, "caused_by"))
        msg = self.svc().link_memories(a.id, b.id, "caused_by", remove=True)
        self.assertIn("Removed", msg)
        self.assertEqual(self.store.get(a.id).links, [])
        self.assertIn("No caused_by link", self.svc().link_memories(a.id, b.id, "caused_by", True))

    def test_relation_defaults_to_related_and_is_validated(self):
        a, b = self.add("A"), self.add("B")
        self.svc().link_memories(a.id, b.id)
        self.assertEqual(self.store.get(a.id).links, [f"related:{b.id}"])
        msg = self.svc().link_memories(a.id, b.id, "replaces")
        self.assertIn("Unknown relation", msg)
        self.assertIn("supersede", msg)

    def test_cannot_link_to_itself(self):
        a = self.add("A")
        self.assertIn("itself", self.svc().link_memories(a.id, a.id))

    def test_read_only_client_cannot_link(self):
        a, b = self.add("A"), self.add("B")
        msg = self.svc(readonly=True).link_memories(a.id, b.id)
        self.assertIn("read-only", msg.lower())
        self.assertEqual(self.store.get(a.id).links, [])

    def test_link_limit_is_reported(self):
        a = self.add("A")
        self.store.set_links(a.id, [f"related:{i:032x}" for i in range(MAX_LINKS)])
        b = self.add("B")
        self.assertIn("limit", self.svc().link_memories(a.id, b.id))

    def test_scoped_client_cannot_link_to_hidden_memory(self):
        a = self.add("Public")
        secret = self.add("Secret", scopes=["legal"])
        grant = scopes_mod.grant_from("coding")
        msg = self.svc(grant=grant).link_memories(a.id, secret.id)
        self.assertIn("No such memory", msg)
        self.assertEqual(self.store.get(a.id).links, [])

    def test_related_memories_lists_both_directions(self):
        a, b, c = self.add("Decision A"), self.add("Reason B"), self.add("Rule C")
        self.svc().link_memories(a.id, b.id, "caused_by")
        self.svc().link_memories(c.id, a.id, "depends_on")
        out = self.svc().related_memories(a.id[:8])
        self.assertIn(f"caused_by → id={b.id[:8]}", out)
        self.assertIn("Reason B", out)
        self.assertIn(f"depends_on ← id={c.id[:8]}", out)
        self.assertIn("Rule C", out)

    def test_related_memories_hides_out_of_scope_neighbours(self):
        a = self.add("Public")
        secret = self.add("Secret plan", scopes=["legal"])
        self.store.set_links(a.id, [f"related:{secret.id}"])
        self.store.set_links(secret.id, [f"supports:{a.id}"])
        out = self.svc(grant=scopes_mod.grant_from("coding")).related_memories(a.id)
        self.assertNotIn(secret.id[:8], out)
        self.assertNotIn("Secret", out)
        self.assertIn("No linked memories", out)

    def test_recent_digest_hides_links_to_out_of_scope_memories(self):
        a, b = self.add("Public"), self.add("Also public")
        secret = self.add("Secret", scopes=["legal"])
        self.store.set_links(a.id, [f"caused_by:{secret.id}", f"related:{b.id}"])
        out = self.svc(grant=scopes_mod.grant_from("coding")).recent()
        self.assertNotIn(secret.id, out)
        self.assertIn(f"related:{b.id}", out)
        self.assertIn(f"caused_by:{secret.id}", self.svc().recent())

    def test_link_to_an_id_links_cannot_store_is_refused(self):
        a = self.add("A")
        odd = self.add("Imported", id="x" * 70)
        msg = self.svc().link_memories(a.id, odd.id)
        self.assertNotIn("Linked", msg)
        self.assertEqual(self.store.get(a.id).links, [])

    def test_related_memories_unknown_id(self):
        self.assertIn("No such memory", self.svc().related_memories("deadbeef"))


class TestGraphLinks(StoreCase):
    def test_links_become_memory_to_memory_edges(self):
        a, b = self.add("A"), self.add("B")
        self.store.set_links(a.id, [f"caused_by:{b.id}"])
        graph = memory_graph(self.store)
        self.assertIn(
            {"source": a.id, "target": b.id, "kind": "link", "relation": "caused_by"},
            graph["edges"],
        )

    def test_links_to_memories_outside_the_view_are_left_out(self):
        inside, outside = self.add("Inside"), self.add("Outside")
        self.store.set_links(inside.id, [f"related:{outside.id}"])
        project = self.store.create_project("P")
        self.store.add_to_project(project["id"], inside.id)
        graph = memory_graph(self.store, project["id"])
        self.assertNotIn(outside.id, str(graph))
        self.assertFalse([e for e in graph["edges"] if e["kind"] == "link"])


class TestLinkTools(unittest.TestCase):
    """The two tools end to end through the MCP server, as a client calls them."""

    def setUp(self):
        try:
            from mcp.server.fastmcp import FastMCP  # noqa: F401
        except ImportError:
            self.skipTest("MCP SDK not installed")
        from elesync.mcp_server import build_server

        self.tmp = tempfile.TemporaryDirectory()
        self.server = build_server(self.tmp.name)
        self.store = self.server.elesync_store

    def tearDown(self):
        if hasattr(self, "store"):
            self.store.close()
        if hasattr(self, "tmp"):
            self.tmp.cleanup()

    def call(self, tool, **arguments):
        result = asyncio.run(self.server.call_tool(tool, arguments))
        blocks = result[0] if isinstance(result, tuple) else result
        return "".join(getattr(block, "text", "") for block in blocks)

    def test_link_follow_and_remove(self):
        a, b = MemoryItem(content="Chose SQLite"), MemoryItem(content="Must work offline")
        self.store.upsert(a)
        self.store.upsert(b)
        self.assertIn(
            "Linked",
            self.call("link_memories", source_id=a.id, target_id=b.id, relation="caused_by"),
        )
        self.assertIn("Must work offline", self.call("related_memories", memory_id=a.id[:8]))
        self.assertIn(
            "Removed",
            self.call(
                "link_memories", source_id=a.id, target_id=b.id, relation="caused_by", remove=True
            ),
        )
        self.assertEqual(self.store.get(a.id).links, [])


if __name__ == "__main__":
    unittest.main()
