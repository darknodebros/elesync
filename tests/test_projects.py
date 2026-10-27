from __future__ import annotations

import asyncio
import json
import os
import re
import tempfile
import unittest
from pathlib import Path
from unittest import mock

from elesync.models import MemoryItem
from elesync.service import MemoryService
from elesync.store import MemoryStore


class ProjectFakeEmbedder:
    vocab = ["work", "food"]
    synonyms = {"job": "work", "working": "work", "career": "work", "meal": "food"}

    def embed_one(self, text: str) -> list[float]:
        words = [self.synonyms.get(w, w) for w in re.findall(r"[a-z]+", text.lower())]
        return [float(words.count(term)) for term in self.vocab]

    def embed(self, texts: list[str]) -> list[list[float]]:
        return [self.embed_one(text) for text in texts]


class TestProjects(unittest.TestCase):
    def setUp(self) -> None:
        self.tmp = tempfile.TemporaryDirectory()
        self.store = MemoryStore(self.tmp.name)

    def tearDown(self) -> None:
        self.store.close()
        self.tmp.cleanup()

    def _add(self, content: str, source: str = "manual", mtype: str = "fact") -> MemoryItem:
        item = MemoryItem(content=content, source=source, type=mtype)
        self.store.upsert(item)
        return item

    def test_project_brief_is_scoped_and_evidence_backed(self) -> None:
        decision = self._add("We decided to book the Kyoto flight.", "chatgpt")
        question = self._add("Which hotel is close to Kyoto station?", "claude")
        unrelated = self._add("The garden needs watering.")
        project = self.store.create_project("Japan trip", "Plan Kyoto travel")
        self.store.add_to_project(project["id"], decision.id)
        self.store.add_to_project(project["id"], question.id)

        brief = self.store.project_brief(project["id"])

        self.assertEqual(brief["overview"]["memory_count"], 2)
        self.assertEqual(brief["overview"]["sources"], ["chatgpt", "claude"])
        self.assertEqual(brief["sections"]["decisions"][0]["id"], decision.id)
        self.assertEqual(brief["sections"]["open_questions"][0]["id"], question.id)
        timeline_ids = {row["id"] for row in brief["timeline"]}
        self.assertNotIn(unrelated.id, timeline_ids)

    def test_project_brief_leaves_out_superseded_memories(self) -> None:
        kept = self._add("Hotel in Kyoto is near Gion.", "claude")
        lost = self._add("Hotel in Kyoto is near Kyoto Station.", "chatgpt")
        project = self.store.create_project("Japan trip")
        self.store.add_to_project(project["id"], kept.id)
        self.store.add_to_project(project["id"], lost.id)
        self.store.supersede(lost.id, kept.id, "2026-09-24T12:00:00+00:00")

        brief = self.store.project_brief(project["id"])

        self.assertEqual(brief["overview"]["memory_count"], 1)
        self.assertEqual(brief["overview"]["sources"], ["claude"])
        self.assertEqual([row["id"] for row in brief["sections"]["key_facts"]], [kept.id])
        self.assertEqual([row["id"] for row in brief["timeline"]], [kept.id])

    def test_brief_keeps_chat_transcripts_out_of_its_sections(self) -> None:
        # Nearly every imported chat contains a "?", so each one used to be
        # listed, whole, as an open question.
        chat = self._add(
            "# Hotel planning\n\nhuman: which hotel should we book?\nassistant: We decided on Gion.",
            "chatgpt",
            "conversation",
        )
        fact = self._add("The Kyoto flight leaves at noon.", "gemini")
        project = self.store.create_project("Japan trip")
        self.store.add_to_project(project["id"], chat.id)
        self.store.add_to_project(project["id"], fact.id)

        brief = self.store.project_brief(project["id"])

        filed = {row["id"] for rows in brief["sections"].values() for row in rows}
        self.assertEqual(filed, {fact.id})
        self.assertIn(chat.id, {row["id"] for row in brief["timeline"]})

    def test_brief_open_question_needs_a_question_sentence(self) -> None:
        link = self._add("Booking page: https://example.com/hotels?city=kyoto&n=2")
        question = self._add("Which hotel is close to Kyoto station? Ask Ana.")
        project = self.store.create_project("Japan trip")
        for item in (link, question):
            self.store.add_to_project(project["id"], item.id)

        sections = self.store.project_brief(project["id"])["sections"]

        self.assertEqual([row["id"] for row in sections["open_questions"]], [question.id])
        self.assertEqual([row["id"] for row in sections["key_facts"]], [link.id])

    def test_brief_rows_carry_an_excerpt_not_the_whole_memory(self) -> None:
        note = self._add("Itinerary notes. " * 200, mtype="note")
        project = self.store.create_project("Japan trip")
        self.store.add_to_project(project["id"], note.id)

        brief = self.store.project_brief(project["id"])

        row = brief["sections"]["key_facts"][0]
        self.assertEqual(row["id"], note.id)
        self.assertLessEqual(len(row["content"]), 500)
        self.assertTrue(row["content"].endswith("…"))
        self.assertLessEqual(len(brief["timeline"][0]["content"]), 500)

    def test_suggestions_require_review_and_exclude_added_items(self) -> None:
        related = self._add("Compare Kyoto flight options", "gemini")
        self._add("Replace the kitchen light")
        project = self.store.create_project("Japan trip", "Kyoto flights")

        suggestions = self.store.project_suggestions(project["id"])
        self.assertEqual(suggestions[0]["item"].id, related.id)
        self.assertIn("kyoto", suggestions[0]["matched_terms"])

        self.store.add_to_project(project["id"], related.id)
        self.assertEqual(self.store.project_suggestions(project["id"]), [])

    def test_project_knowledge_documents_and_context_are_persistent(self) -> None:
        project = self.store.create_project("Japan trip", "Plan Kyoto travel")
        document = self.store.save_project_document(
            project["id"], "Decision", "Book the Kyoto flight.", "decision"
        )
        self.assertEqual(document["kind"], "decision")
        context = self.store.project_context(project["id"])
        self.assertEqual(context["documents"][0]["title"], "Decision")
        self.assertEqual(context["memories"], [])
        self.store.close()
        self.store = MemoryStore(self.tmp.name)
        context = self.store.project_context(project["id"])
        self.assertEqual(context["documents"][0]["body"], "Book the Kyoto flight.")

    def test_project_snapshot_skips_invalid_documents(self) -> None:
        project = self.store.create_project("Japan trip")
        snapshot = {
            "format_version": 1,
            "projects": [project],
            "memberships": [],
            "documents": [
                {"id": "", "project_id": project["id"], "title": "Bad", "body": "Body"},
                {
                    "id": "bad-kind",
                    "project_id": project["id"],
                    "title": "Bad",
                    "body": "Body",
                    "kind": "other",
                },
                {
                    "id": "valid",
                    "project_id": project["id"],
                    "title": "Good",
                    "body": "Body",
                    "kind": "knowledge",
                },
            ],
        }

        self.store.close()
        self.store = MemoryStore(self.tmp.name)
        self.store.restore_projects(snapshot)

        documents = self.store.project_documents(project["id"])
        self.assertEqual([document["id"] for document in documents], ["valid"])

    def test_project_search_requires_all_meaningful_terms_and_stems_questions(self) -> None:
        exact = self._add("We decided to book the Kyoto flight.")
        partial = self._add("Kyoto hotel recommendations.")
        other = self._add("Book the flight from Madrid.")
        project = self.store.create_project("Japan trip", "Plan Kyoto travel")
        for item in (exact, partial, other):
            self.store.add_to_project(project["id"], item.id)

        results = self.store.search_project(
            project["id"], "What did we decide about Kyoto flights?"
        )
        self.assertEqual([item.id for item in results], [exact.id])
        self.assertEqual(self.store.search_project(project["id"], "Kyoto hotel"), [partial])

    def test_project_bound_service_cannot_write_documents_outside_project(self) -> None:
        project = self.store.create_project("Allowed")
        other = self.store.create_project("Blocked")
        svc = MemoryService(self.store, project_id=project["id"], project_write=True)
        document = svc.save_project_document(project["id"], "Allowed note", "Body")
        self.assertEqual(document["project_id"], project["id"])
        with self.assertRaisesRegex(ValueError, "assigned project"):
            svc.save_project_document(other["id"], "Blocked note", "Body")

    def test_project_bound_service_cannot_recall_or_write_outside_project(self) -> None:
        inside = self._add("Inside project", "claude")
        self._add("Outside project", "grok")
        project = self.store.create_project("Focused work", "Only this context")
        self.store.add_to_project(project["id"], inside.id)
        svc = MemoryService(self.store, project_id=project["id"])
        self.assertIn("Inside project", svc.recall("Inside"))
        self.assertNotIn("Outside project", svc.recall("Outside"))
        saved = svc.remember("New project update", source="chatgpt")
        self.assertIn("Saved", saved)
        project_items = self.store.project_memories(project["id"])
        self.assertTrue(any(item.content == "New project update" for item in project_items))

    def test_project_lifecycle_never_deletes_memories(self) -> None:
        memory = self._add("Keep this source memory")
        project = self.store.create_project("Draft", "Old purpose")
        self.store.add_to_project(project["id"], memory.id)

        updated = self.store.update_project(project["id"], "Final", "New purpose")
        self.assertEqual(updated["name"], "Final")
        self.assertTrue(self.store.remove_from_project(project["id"], memory.id))
        self.assertIsNotNone(self.store.get(memory.id))

        self.store.add_to_project(project["id"], memory.id)
        self.assertTrue(self.store.delete_project(project["id"]))
        self.assertIsNone(self.store.project(project["id"]))
        self.assertIsNotNone(self.store.get(memory.id))

    def test_memory_update_preserves_project_membership(self) -> None:
        memory = self._add("Original project note")
        project = self.store.create_project("Stable membership")
        self.store.add_to_project(project["id"], memory.id)

        memory.content = "Edited project note"
        memory.content_hash = memory.compute_hash()
        self.assertTrue(self.store.update(memory))

        project_items = self.store.project_memories(project["id"])
        self.assertEqual([item.id for item in project_items], [memory.id])
        self.assertEqual(project_items[0].content, "Edited project note")

    def test_memory_update_collision_preserves_both_memories(self) -> None:
        first = self._add("First distinct note")
        second = self._add("Second distinct note")
        project = self.store.create_project("Collision safety")
        self.store.add_to_project(project["id"], first.id)

        first.content = second.content
        first.content_hash = first.compute_hash()
        with self.assertRaisesRegex(ValueError, "identical memory"):
            self.store.update(first)

        self.assertEqual(self.store.get(first.id).content, "First distinct note")
        self.assertEqual(self.store.get(second.id).content, "Second distinct note")
        self.assertEqual(
            [item.id for item in self.store.project_memories(project["id"])], [first.id]
        )

    def test_semantic_suggestion_finds_different_wording(self) -> None:
        self.store.close()
        self.store = MemoryStore(self.tmp.name, embedder=ProjectFakeEmbedder())
        related = self._add("Working remotely from Madrid", "claude")
        self._add("Plan the evening meal", "chatgpt")
        project = self.store.create_project("Career planning", "Find a new job")

        suggestions = self.store.project_suggestions(project["id"])

        self.assertEqual(suggestions[0]["item"].id, related.id)
        self.assertEqual(suggestions[0]["match_kind"], "semantic")
        self.assertEqual(suggestions[0]["matched_terms"], [])

    def test_markdown_export_keeps_evidence_references(self) -> None:
        decision = self._add("We decided to take the train.", "chatgpt")
        project = self.store.create_project("Japan trip", "Plan the holiday")
        self.store.add_to_project(project["id"], decision.id)

        markdown = self.store.project_brief_markdown(project["id"])

        self.assertIn("# Japan trip", markdown)
        self.assertIn("## Decisions", markdown)
        self.assertIn("We decided to take the train.", markdown)
        self.assertIn("chatgpt", markdown)
        self.assertIn(decision.id[:8], markdown)

    def test_ai_client_recall_is_project_scoped(self) -> None:
        project_memory = self._add("The Kyoto flight leaves at noon.", "chatgpt")
        self._add("The Madrid appointment is at noon.", "claude")
        project = self.store.create_project("Japan trip")
        self.store.add_to_project(project["id"], project_memory.id)
        service = MemoryService(self.store)

        projects = service.list_projects()
        result = service.recall_project(project["id"][:8], "noon")

        self.assertIn("Japan trip", projects)
        self.assertIn("Kyoto", result)
        self.assertNotIn("Madrid", result)

    def test_mcp_project_tools_accept_the_same_unique_short_id(self) -> None:
        project = self.store.create_project("Andromeda One")
        short_id = project["id"][:8]
        service = MemoryService(self.store, project_id=short_id)

        document = service.save_project_document(
            short_id,
            "Andromeda One Beta Plan",
            "The beta starts on October 21.",
        )
        context = service.project_context(short_id)

        self.assertEqual(document["project_id"], project["id"])
        self.assertEqual(context["project"]["id"], project["id"])
        self.assertIn("Saved", service.remember("Short-bound project memory"))
        self.assertEqual(
            self.store.project_memories(project["id"])[0].content, "Short-bound project memory"
        )

    def test_project_recall_finds_a_newly_saved_durable_document(self) -> None:
        project = self.store.create_project("Andromeda One")
        service = MemoryService(self.store)
        service.save_project_document(
            project["id"][:8],
            "Andromeda One Beta Plan",
            "The beta starts on October 21 and the onboarding target is 37 users.",
        )

        result = service.recall_project(project["id"][:8], "onboarding target 37 users")

        self.assertIn("Andromeda One Beta Plan", result)
        self.assertIn("October 21", result)

    def test_project_context_is_filtered_paginated_and_size_bounded(self) -> None:
        project = self.store.create_project("Large project")
        for index in range(6):
            marker = "needle" if index >= 2 else "unrelated"
            self.store.save_project_document(
                project["id"],
                f"Plan {index}",
                f"{marker} " + ("x" * 60_000),
                "knowledge" if index != 4 else "decision",
            )
        # Keep this pagination fixture independent of crossing a wall-clock
        # second: documents sort by updated_at before the title tiebreaker.
        self.store.db.execute(
            "UPDATE project_documents SET updated_at = ? WHERE project_id = ?",
            ("2026-10-27T00:00:00+00:00", project["id"]),
        )
        self.store.db.commit()
        service = MemoryService(self.store)

        context = service.project_context(
            project["id"][:8],
            query="needle",
            document_kind="knowledge",
            offset=1,
            limit=2,
            max_chars=2_000,
        )

        self.assertEqual([row["title"] for row in context["documents"]], ["Plan 3", "Plan 5"])
        self.assertEqual(context["pagination"]["documents"]["total"], 3)
        self.assertEqual(context["pagination"]["documents"]["returned"], 2)
        self.assertFalse(context["pagination"]["documents"]["has_more"])
        self.assertTrue(context["content_truncated"])
        self.assertLess(len(json.dumps(context).encode("utf-8")), 1_000_000)

    def test_default_project_context_stays_below_mcp_payload_limit(self) -> None:
        project = self.store.create_project("Oversized project")
        for index in range(30):
            self.store.save_project_document(
                project["id"], f"Document {index}", "z" * 60_000, "knowledge"
            )

        payload = json.dumps(MemoryService(self.store).project_context(project["id"]))

        self.assertLess(len(payload.encode("utf-8")), 1_000_000)
        self.assertTrue(json.loads(payload)["pagination"]["documents"]["has_more"])

    def test_ai_project_recall_respects_memory_scopes(self) -> None:
        visible = MemoryItem(content="Public project note", scopes=[])
        hidden = MemoryItem(content="Legal project secret", scopes=["legal"])
        self.store.upsert(visible)
        self.store.upsert(hidden)
        project = self.store.create_project("Mixed project")
        self.store.add_to_project(project["id"], visible.id)
        self.store.add_to_project(project["id"], hidden.id)
        service = MemoryService(self.store, grant=frozenset({"writing"}))

        result = service.recall_project(project["id"][:8], "project")

        self.assertIn("Public", result)
        self.assertNotIn("secret", result)

    def test_project_read_permission_can_exclude_conversations(self) -> None:
        note = self._add("Public project note", "chatgpt")
        conversation = MemoryItem(content="Private conversation text", type="conversation")
        self.store.upsert(conversation)
        project = self.store.create_project("Restricted project")
        self.store.add_to_project(project["id"], note.id)
        self.store.add_to_project(project["id"], conversation.id)
        service = MemoryService(
            self.store,
            project_id=project["id"],
            project_read_conversations=False,
        )

        recall = service.recall("project")
        context = service.project_context(project["id"])

        self.assertIn("Public", recall)
        self.assertNotIn("Private conversation", recall)
        self.assertEqual([item["id"] for item in context["memories"]], [note.id])

    def test_projects_survive_lost_index_and_reindex(self) -> None:
        memory = self._add("Durable project material", "claude")
        project = self.store.create_project("Durable project", "Must survive recovery")
        self.store.add_to_project(project["id"], memory.id)
        vault = Path(self.tmp.name)
        self.store.close()
        (vault / "index.db").unlink()

        self.store = MemoryStore(vault)
        self.store.reindex()

        restored = self.store.project(project["id"])
        self.assertIsNotNone(restored)
        self.assertEqual(restored["name"], "Durable project")
        self.assertEqual(self.store.project_memories(project["id"])[0].id, memory.id)

    def test_project_snapshot_restores_after_memories(self) -> None:
        memory = self._add("Backup project material", "gemini")
        project = self.store.create_project("Backup project")
        self.store.add_to_project(project["id"], memory.id)
        snapshot = self.store.project_snapshot()

        with tempfile.TemporaryDirectory() as destination:
            with MemoryStore(destination) as restored:
                restored.upsert(memory)
                self.assertEqual(restored.restore_projects(snapshot), 1)
                self.assertEqual(restored.project(project["id"])["name"], "Backup project")
                self.assertEqual(restored.project_memories(project["id"])[0].id, memory.id)


class TestMCPProjectTools(unittest.TestCase):
    def setUp(self) -> None:
        try:
            from mcp.server.fastmcp import FastMCP  # noqa: F401
        except ImportError:
            self.skipTest("MCP SDK not installed")
        from elesync.mcp_server import build_server

        self.tmp = tempfile.TemporaryDirectory()
        self.server = build_server(self.tmp.name)
        self.store = self.server.elesync_store

    def tearDown(self) -> None:
        if hasattr(self, "store"):
            self.store.close()
        if hasattr(self, "tmp"):
            self.tmp.cleanup()

    @staticmethod
    def _text(result) -> str:
        content = result[0] if isinstance(result, tuple) else result
        return "".join(block.text for block in content if hasattr(block, "text"))

    def test_short_id_save_recall_and_bounded_context_work_over_mcp(self) -> None:
        project = self.store.create_project("Andromeda One")
        short_id = project["id"][:8]

        async def run() -> tuple[str, str, str]:
            saved = await self.server.call_tool(
                "save_project_document",
                {
                    "project_id": short_id,
                    "title": "Andromeda One Beta Plan",
                    "body": "The onboarding target is 37 users.",
                },
            )
            recalled = await self.server.call_tool(
                "recall_project", {"project_id": short_id, "query": "onboarding 37 users"}
            )
            context = await self.server.call_tool(
                "project_context", {"project_id": short_id, "limit": 1, "max_chars": 2_000}
            )
            return self._text(saved), self._text(recalled), self._text(context)

        saved, recalled, context_text = asyncio.run(run())
        context = json.loads(context_text)

        self.assertIn("Saved knowledge document", saved)
        self.assertIn("Andromeda One Beta Plan", recalled)
        self.assertEqual(context["documents"][0]["title"], "Andromeda One Beta Plan")
        self.assertEqual(context["pagination"]["limit"], 1)

    def test_legacy_claude_project_mcp_rechecks_grant_after_revocation(self) -> None:
        from elesync.mcp_server import build_server

        project = self.store.create_project("Revocable project")
        self.store.set_project_access(project["id"], "claude", True, True, False)

        with mock.patch.dict(
            os.environ,
            {
                "ELESYNC_PROJECT_ID": project["id"],
            },
        ):
            server = build_server(self.tmp.name)
        self.store.close()
        self.store = server.elesync_store

        async def remember(content: str) -> str:
            result = await server.call_tool("remember", {"content": content})
            return self._text(result)

        self.assertIn("Saved", asyncio.run(remember("Before revocation")))
        self.store.revoke_project_access(project["id"], "claude")
        with self.assertRaisesRegex(Exception, "no longer active"):
            asyncio.run(remember("After revocation"))
        with self.assertRaisesRegex(Exception, "no longer active"):
            asyncio.run(server.call_tool("memory_status", {}))
        self.assertFalse(
            any(
                item.content == "After revocation"
                for item in self.store.project_memories(project["id"])
            )
        )

    def test_project_mcp_starts_before_its_project_exists(self) -> None:
        from elesync.mcp_server import build_server

        self.store.upsert(MemoryItem(content="Unrelated vault memory"))
        project_id = "0123456789abcdef0123456789abcdef"
        with mock.patch.dict(
            os.environ,
            {"ELESYNC_PROJECT_ID": project_id, "ELESYNC_PROJECT_PROVIDER": "claude"},
        ):
            server = build_server(self.tmp.name)
        self.store.close()
        self.store = server.elesync_store

        async def call(tool: str, args: dict) -> str:
            return self._text(await server.call_tool(tool, args))

        # Until the project exists every call is refused; it never falls back to
        # the whole vault.
        with self.assertRaisesRegex(Exception, "does not exist in this vault"):
            asyncio.run(call("recall", {"query": "Unrelated"}))

        self.store.project_id_factory = lambda name: project_id
        self.store.create_project("Late project")
        self.store.set_project_access(project_id, "claude", True, True, False)
        self.assertIn("Saved", asyncio.run(call("remember", {"content": "Filed after start"})))
        self.assertTrue(
            any(i.content == "Filed after start" for i in self.store.project_memories(project_id))
        )

    def test_project_mcp_refuses_missing_vault_folder(self) -> None:
        from elesync.mcp_server import build_server

        missing = Path(self.tmp.name) / "deleted-demo" / "EleSyncVault"
        with mock.patch.dict(
            os.environ,
            {"ELESYNC_PROJECT_ID": "0123456789abcdef0123456789abcdef"},
        ):
            with self.assertRaisesRegex(RuntimeError, "vault folder does not exist"):
                build_server(str(missing))
        self.assertFalse(missing.exists())


if __name__ == "__main__":
    unittest.main()
