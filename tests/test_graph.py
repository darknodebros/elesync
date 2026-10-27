from __future__ import annotations

import tempfile
import unittest

from elesync.graph import memory_graph
from elesync.models import MemoryItem
from elesync.store import MemoryStore


class TestMemoryGraph(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.store = MemoryStore(self.tmp.name)

    def tearDown(self):
        self.store.close()
        self.tmp.cleanup()

    def add(self, content, tags=()):
        item = MemoryItem(content=content, tags=list(tags))
        self.store.upsert(item)
        return item

    def test_shared_tags_and_isolated_memories(self):
        first = self.add("First", ["topic", "unique"])
        second = self.add("Second", ["topic"])
        isolated = self.add("Isolated")
        graph = memory_graph(self.store)
        self.assertEqual(
            {n["id"] for n in graph["nodes"]}, {first.id, second.id, isolated.id, "tag:topic"}
        )
        self.assertEqual(len(graph["edges"]), 2)
        self.assertEqual({e["target"] for e in graph["edges"]}, {"tag:topic"})

    def test_project_filter_cannot_leak_other_memories_or_tags(self):
        member = self.add("Inside", ["shared"])
        outside = self.add("Outside", ["shared", "private"])
        project = self.store.create_project("Project")
        self.store.add_to_project(project["id"], member.id)
        graph = memory_graph(self.store, project["id"])
        self.assertEqual([n["id"] for n in graph["nodes"]], [member.id, "project:" + project["id"]])
        self.assertEqual(graph["nodes"][0]["project_ids"], [project["id"]])
        self.assertEqual(
            graph["edges"],
            [{"source": member.id, "target": "project:" + project["id"], "kind": "project"}],
        )
        self.assertNotIn(outside.id, str(graph))
        self.assertNotIn("private", str(graph))
        self.assertEqual(graph["total"], 1)
        with self.assertRaises(ValueError):
            memory_graph(self.store, "missing")

    def test_complete_graph_and_read_only_behavior(self):
        items = [MemoryItem(content=f"Memory {i}", tags=["common"]) for i in range(1205)]
        self.store.import_many(items)
        before = self.store.count()
        graph = memory_graph(self.store)
        self.assertEqual(graph["shown"], 1205)
        self.assertEqual(graph["total"], 1205)
        self.assertFalse(graph["truncated"])
        self.assertEqual(len(graph["edges"]), 1205)
        self.assertEqual(
            {n["id"] for n in graph["nodes"] if n["kind"] == "memory"}, {item.id for item in items}
        )
        self.assertEqual(self.store.count(), before)
        # The tag graph must not silently discard hubs either.
        for i in range(45):
            self.add(f"Tagged pair {i} A", [f"tag-{i}"])
            self.add(f"Tagged pair {i} B", [f"tag-{i}"])
        graph = memory_graph(self.store)
        self.assertEqual(sum(n["kind"] == "tag" for n in graph["nodes"]), 46)

    def test_empty_graph(self):
        graph = memory_graph(self.store)
        self.assertEqual(graph["nodes"], [])
        self.assertEqual(graph["edges"], [])
        self.assertFalse(graph["truncated"])

    def test_graph_is_capped_and_reports_truncation(self):
        self.store.import_many(
            [MemoryItem(content=f"Memory {i}", tags=["common"]) for i in range(12)]
        )
        graph = memory_graph(self.store, limit=5)
        self.assertEqual(graph["shown"], 5)
        self.assertEqual(graph["total"], 12)
        self.assertTrue(graph["truncated"])
        self.assertEqual(sum(n["kind"] == "memory" for n in graph["nodes"]), 5)

    def test_projects_are_hubs_with_their_name(self):
        first = self.add("One")
        second = self.add("Two")
        loose = self.add("Loose")
        project = self.store.create_project("Roadmap")
        self.store.add_to_project(project["id"], first.id)
        self.store.add_to_project(project["id"], second.id)
        graph = memory_graph(self.store)
        hub = next(n for n in graph["nodes"] if n["kind"] == "project")
        self.assertEqual(hub["label"], "Roadmap")
        targets = {e["source"] for e in graph["edges"] if e["target"] == hub["id"]}
        self.assertEqual(targets, {first.id, second.id})
        self.assertNotIn(loose.id, targets)
