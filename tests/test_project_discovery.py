import random
import tempfile
import unittest
from pathlib import Path

from elesync.models import MemoryItem
from elesync.project_discovery import suggest_projects
from elesync.project_service import ProjectService
from elesync.store import MemoryStore

HISTORY = [
    (
        "chatgpt",
        "conversation",
        "# Planning a Kyoto trip\nuser: help me plan 5 days in Kyoto\nassistant: Fushimi Inari, Arashiyama...",
    ),
    (
        "chatgpt",
        "conversation",
        "# Kyoto trip budget\nuser: a realistic budget for a Kyoto trip for two?\nassistant: Around 150 per day.",
    ),
    (
        "claude",
        "conversation",
        "# Ryokan options in Kyoto\nuser: compare ryokan near Gion for our Kyoto trip\nassistant: Three ryokan with onsen...",
    ),
    ("claude", "fact", "The Kyoto flight leaves at noon on October 12."),
    (
        "chatgpt",
        "conversation",
        "# Helios launch plan\nuser: outline a launch plan for Helios\nassistant: Private beta, then Product Hunt...",
    ),
    (
        "claude",
        "conversation",
        "# Helios pricing\nuser: should Helios pricing be monthly or annual?\nassistant: Offer both.",
    ),
    (
        "chatgpt",
        "conversation",
        "# Helios landing page copy\nuser: write landing page copy for the Helios launch\nassistant: Capture every idea.",
    ),
    ("claude", "fact", "Helios private beta opens to 50 users on November 3."),
    (
        "chatgpt",
        "conversation",
        "# Resume review\nuser: review my resume for a product manager role\nassistant: Lead with outcomes.",
    ),
    (
        "gemini",
        "conversation",
        "# Interview prep product manager\nuser: mock interview for a product manager role\nassistant: Tell me about a product you shipped.",
    ),
    (
        "chatgpt",
        "conversation",
        "# Cover letter\nuser: draft a cover letter for the product manager role at Stripe\nassistant: Dear hiring team...",
    ),
    ("claude", "preference", "Prefers concise answers with bullet points."),
    ("grok", "fact", "Has a younger sister."),
    (
        "chatgpt",
        "conversation",
        "# Sourdough starter\nuser: my sourdough starter smells like acetone\nassistant: Feed it more often.",
    ),
]


def _history():
    return [MemoryItem(content=text, source=source, type=kind) for source, kind, text in HISTORY]


class TestSuggestProjects(unittest.TestCase):
    def test_groups_history_into_named_projects(self):
        items = _history()
        by_id = {item.id: item for item in items}
        suggestions = suggest_projects(items)
        self.assertEqual(
            sorted(s["name"] for s in suggestions),
            ["Helios launch", "Kyoto trip", "Product manager"],
        )
        kyoto = next(s for s in suggestions if s["name"] == "Kyoto trip")
        self.assertEqual(kyoto["count"], 4)
        self.assertEqual(kyoto["sources"], ["chatgpt", "claude"])
        self.assertTrue(all("kyoto" in by_id[i].content.lower() for i in kyoto["memory_ids"]))
        self.assertIn("Planning a Kyoto trip", kyoto["samples"])

    def test_unrelated_memories_stay_unassigned(self):
        suggestions = suggest_projects(_history())
        grouped = {i for s in suggestions for i in s["memory_ids"]}
        loners = {
            item.id
            for item in _history()
            if "sourdough" in item.content or "sister" in item.content
        }
        self.assertFalse(grouped & loners)

    def test_each_memory_joins_at_most_one_project(self):
        ids = [i for s in suggest_projects(_history()) for i in s["memory_ids"]]
        self.assertEqual(len(ids), len(set(ids)))

    def test_existing_project_name_is_not_suggested_again(self):
        names = [s["name"] for s in suggest_projects(_history(), existing_names=["Kyoto trip"])]
        self.assertNotIn("Kyoto trip", names)

    def test_result_does_not_depend_on_input_order(self):
        items = _history()
        shuffled = items[:]
        random.Random(7).shuffle(shuffled)
        self.assertEqual(suggest_projects(items), suggest_projects(shuffled))

    def test_dominant_topic_is_still_suggested(self):
        # Once other projects are filed, what is left can be mostly one topic.
        rest = [
            item
            for item in _history()
            if "helios" in item.content.lower() or "sourdough" in item.content
        ]
        (only,) = suggest_projects(rest)
        self.assertEqual(only["name"], "Helios launch")
        self.assertEqual(only["count"], 4)

    def test_too_little_history_suggests_nothing(self):
        self.assertEqual(suggest_projects(_history()[:1]), [])


class TestDiscoveryStore(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        self.store = MemoryStore(Path(self.tmp.name))
        self.addCleanup(self.store.close)
        self.items = _history()
        for item in self.items:
            self.store.upsert(item)

    def test_unfiled_memories_exclude_filed_and_superseded(self):
        project = self.store.create_project("Existing")
        self.store.add_to_project(project["id"], self.items[0].id)
        self.store.supersede(self.items[1].id, self.items[2].id, "2026-01-01T00:00:00+00:00")
        unfiled = {item.id for item in self.store.unfiled_memories()}
        self.assertNotIn(self.items[0].id, unfiled)
        self.assertNotIn(self.items[1].id, unfiled)
        self.assertIn(self.items[2].id, unfiled)

    def test_bulk_add_skips_unknown_and_duplicate_ids(self):
        project = self.store.create_project("Bulk")
        ids = [self.items[0].id, self.items[1].id, self.items[0].id, "missing"]
        self.assertEqual(self.store.add_memories_to_project(project["id"], ids), 2)
        self.assertEqual(self.store.add_memories_to_project(project["id"], ids), 0)
        self.assertEqual(len(self.store.project_memories(project["id"])), 2)
        events = [
            e for e in self.store.project_timeline(project["id"]) if "Added 2" in e.get("title", "")
        ]
        self.assertEqual(len(events), 1)

    def test_accepting_a_suggestion_creates_a_filled_project(self):
        service = ProjectService(self.store)
        suggestion = service.discover()["suggestions"][0]
        project = service.create_from_suggestion(suggestion["name"], "", suggestion["memory_ids"])
        self.assertEqual(project["memory_count"], suggestion["count"])
        names = [s["name"] for s in service.discover()["suggestions"]]
        self.assertNotIn(suggestion["name"], names)
