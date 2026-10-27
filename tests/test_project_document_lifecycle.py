"""Task lifecycle fields belong to tasks only, and project listings count documents.

Regression coverage for two defects found through the MCP surface:

* every project document was saved with ``status: "open"`` / ``completed_at``,
  so knowledge, decision, and handoff documents looked like unfinished tasks;
* ``list_projects`` and ``project_context`` reported memories only, so a
  project holding documents read as empty.
"""

from __future__ import annotations

import json
import sqlite3
import tempfile
import unittest
from pathlib import Path

from elesync.models import MemoryItem
from elesync.service import MemoryService
from elesync.store import MemoryStore
from elesync.store_projects import document_lifecycle

NON_TASK_KINDS = ("knowledge", "decision", "handoff")


class TestDocumentLifecycleRule(unittest.TestCase):
    def test_non_task_kinds_never_carry_lifecycle(self) -> None:
        for kind in NON_TASK_KINDS:
            self.assertEqual(document_lifecycle(kind, "open", "2026-01-01"), (None, None))
            self.assertEqual(document_lifecycle(kind, "completed", "2026-01-01"), (None, None))

    def test_task_defaults_open_and_keeps_completion(self) -> None:
        self.assertEqual(document_lifecycle("task"), ("open", None))
        self.assertEqual(document_lifecycle("task", "bogus", "x"), ("open", None))
        self.assertEqual(document_lifecycle("task", "open", "2026-01-01"), ("open", None))
        self.assertEqual(
            document_lifecycle("task", "completed", "2026-01-01"), ("completed", "2026-01-01")
        )


class TestProjectDocumentLifecycle(unittest.TestCase):
    def setUp(self) -> None:
        self.tmp = tempfile.TemporaryDirectory()
        self.store = MemoryStore(self.tmp.name)
        self.project = self.store.create_project("pro")

    def tearDown(self) -> None:
        self.store.close()
        self.tmp.cleanup()

    def _save(self, kind: str) -> dict:
        return self.store.save_project_document(self.project["id"], f"{kind} doc", "body", kind)

    def test_saved_non_task_documents_have_no_lifecycle(self) -> None:
        for kind in NON_TASK_KINDS:
            with self.subTest(kind=kind):
                saved = self._save(kind)
                self.assertIsNone(saved["status"])
                self.assertIsNone(saved["completed_at"])
                stored = self.store.project_document(saved["id"])
                self.assertIsNone(stored["status"])
                self.assertIsNone(stored["completed_at"])

    def test_saved_task_starts_open(self) -> None:
        task = self.store.project_document(self._save("task")["id"])
        self.assertEqual(task["status"], "open")
        self.assertIsNone(task["completed_at"])

    def test_completing_a_task_still_works(self) -> None:
        task = self._save("task")
        done = self.store.complete_project_task(self.project["id"], task["id"])
        self.assertEqual(done["status"], "completed")
        self.assertTrue(done["completed_at"])

    def test_non_task_cannot_be_completed(self) -> None:
        note = self._save("knowledge")
        self.assertIsNone(self.store.complete_project_task(self.project["id"], note["id"]))
        self.assertIsNone(self.store.project_document(note["id"])["status"])

    def test_editing_a_completed_task_keeps_its_completion(self) -> None:
        task = self._save("task")
        done = self.store.complete_project_task(self.project["id"], task["id"])
        edited = self.store.update_project_document(task["id"], "renamed", "new body", "task")
        self.assertEqual(edited["status"], "completed")
        self.assertEqual(edited["completed_at"], done["completed_at"])

    def test_turning_a_task_into_knowledge_clears_lifecycle(self) -> None:
        task = self._save("task")
        self.store.complete_project_task(self.project["id"], task["id"])
        edited = self.store.update_project_document(task["id"], "note", "body", "knowledge")
        self.assertIsNone(edited["status"])
        self.assertIsNone(edited["completed_at"])

    def test_turning_knowledge_into_a_task_opens_it(self) -> None:
        note = self._save("knowledge")
        edited = self.store.update_project_document(note["id"], "todo", "body", "task")
        self.assertEqual(edited["status"], "open")
        self.assertIsNone(edited["completed_at"])

    def test_restore_normalizes_legacy_snapshot(self) -> None:
        with self.store._lock:
            snapshot = self.store._project_snapshot_unsafe()
        snapshot["documents"] = [
            {
                "id": "legacy-knowledge",
                "project_id": self.project["id"],
                "title": "Old note",
                "body": "body",
                "kind": "knowledge",
                "status": "open",
                "completed_at": None,
                "created_at": "2026-01-01T00:00:00+00:00",
                "updated_at": "2026-01-01T00:00:00+00:00",
            },
            {
                "id": "legacy-task",
                "project_id": self.project["id"],
                "title": "Old task",
                "body": "body",
                "kind": "task",
                "created_at": "2026-01-01T00:00:00+00:00",
                "updated_at": "2026-01-01T00:00:00+00:00",
            },
        ]
        self.store.restore_projects(snapshot)
        self.assertIsNone(self.store.project_document("legacy-knowledge")["status"])
        self.assertEqual(self.store.project_document("legacy-task")["status"], "open")


class TestLegacyVaultMigration(unittest.TestCase):
    """A vault created before this fix has a NOT NULL status column and stale rows."""

    def setUp(self) -> None:
        self.tmp = tempfile.TemporaryDirectory()
        store = MemoryStore(self.tmp.name)
        project = store.create_project("legacy")
        self.project_id = project["id"]
        store.save_project_document(self.project_id, "Note", "body", "knowledge")
        task = store.save_project_document(self.project_id, "Task", "body", "task")
        store.complete_project_task(self.project_id, task["id"])
        self.task_id = task["id"]
        self.db_path = store.db_path
        store.close()
        self._downgrade_to_legacy_schema()

    def tearDown(self) -> None:
        self.tmp.cleanup()

    def _downgrade_to_legacy_schema(self) -> None:
        """Recreate the pre-fix table and data, in the DB and in the manifest."""
        db = sqlite3.connect(self.db_path)
        db.executescript(
            """
            CREATE TABLE legacy AS SELECT * FROM project_documents;
            DROP TABLE project_documents;
            CREATE TABLE project_documents (
                id TEXT PRIMARY KEY, project_id TEXT NOT NULL, title TEXT NOT NULL,
                body TEXT NOT NULL, kind TEXT NOT NULL DEFAULT 'knowledge',
                status TEXT NOT NULL DEFAULT 'open', completed_at TEXT,
                created_at TEXT NOT NULL, updated_at TEXT NOT NULL,
                FOREIGN KEY (project_id) REFERENCES projects(id) ON DELETE CASCADE
            );
            INSERT INTO project_documents
              SELECT id, project_id, title, body, kind, COALESCE(status, 'open'),
                     completed_at, created_at, updated_at FROM legacy;
            DROP TABLE legacy;
            """
        )
        db.commit()
        db.close()
        manifest = self._manifest_path()
        payload = json.loads(manifest.read_text(encoding="utf-8"))
        for document in payload["documents"]:
            if document["kind"] != "task":
                document["status"] = "open"
        manifest.write_text(json.dumps(payload), encoding="utf-8")

    def _manifest_path(self) -> Path:
        candidates = [
            p for p in Path(self.tmp.name).rglob("*.json") if "documents" in p.read_text("utf-8")
        ]
        self.assertEqual(len(candidates), 1, candidates)
        return candidates[0]

    def test_reopening_migrates_schema_rows_and_manifest(self) -> None:
        store = MemoryStore(self.tmp.name)
        try:
            columns = {
                row[1]: row for row in store.db.execute("PRAGMA table_info(project_documents)")
            }
            self.assertEqual(columns["status"][3], 0, "status column should be nullable")
            documents = {d["kind"]: d for d in store.project_documents(self.project_id)}
            self.assertIsNone(documents["knowledge"]["status"])
            self.assertEqual(documents["task"]["status"], "completed")
            self.assertTrue(documents["task"]["completed_at"])
        finally:
            store.close()
        payload = json.loads(self._manifest_path().read_text(encoding="utf-8"))
        statuses = {d["kind"]: d["status"] for d in payload["documents"]}
        self.assertIsNone(statuses["knowledge"])
        self.assertEqual(statuses["task"], "completed")

    def test_migration_is_idempotent(self) -> None:
        for _ in range(2):
            store = MemoryStore(self.tmp.name)
            try:
                self.assertEqual(len(store.project_documents(self.project_id)), 2)
            finally:
                store.close()


class TestProjectDocumentCounts(unittest.TestCase):
    def setUp(self) -> None:
        self.tmp = tempfile.TemporaryDirectory()
        self.store = MemoryStore(self.tmp.name)
        self.project = self.store.create_project("pro")

    def tearDown(self) -> None:
        self.store.close()
        self.tmp.cleanup()

    def test_store_reports_document_count(self) -> None:
        self.assertEqual(self.store.project(self.project["id"])["document_count"], 0)
        self.store.save_project_document(self.project["id"], "Dummy", "body")
        self.store.save_project_document(self.project["id"], "Todo", "body", "task")
        self.assertEqual(self.store.project(self.project["id"])["document_count"], 2)
        listed = {p["id"]: p for p in self.store.projects()}
        self.assertEqual(listed[self.project["id"]]["document_count"], 2)
        self.assertEqual(listed[self.project["id"]]["memory_count"], 0)

    def test_document_count_does_not_inflate_memory_count(self) -> None:
        for text in ("one", "two", "three"):
            item = MemoryItem(content=f"memory {text}")
            self.store.upsert(item)
            self.store.add_to_project(self.project["id"], item.id)
        for title in ("a", "b"):
            self.store.save_project_document(self.project["id"], title, "body")
        project = self.store.project(self.project["id"])
        self.assertEqual(project["memory_count"], 3)
        self.assertEqual(project["document_count"], 2)

    def test_list_projects_shows_documents(self) -> None:
        service = MemoryService(self.store)
        self.assertIn("0 visible memories, 0 documents", service.list_projects())
        self.store.save_project_document(self.project["id"], "Dummy", "body")
        self.assertIn(
            f"- pro (id={self.project['id'][:8]}, 0 visible memories, 1 document)",
            service.list_projects(),
        )

    def test_list_projects_singular_memory(self) -> None:
        item = MemoryItem(content="only memory")
        self.store.upsert(item)
        self.store.add_to_project(self.project["id"], item.id)
        self.assertIn("1 visible memory, 0 documents", MemoryService(self.store).list_projects())

    def test_project_context_counts_match_what_client_sees(self) -> None:
        visible = MemoryItem(content="Public note", scopes=[])
        hidden = MemoryItem(content="Legal secret", scopes=["legal"])
        self.store.upsert(visible)
        self.store.upsert(hidden)
        self.store.add_to_project(self.project["id"], visible.id)
        self.store.add_to_project(self.project["id"], hidden.id)
        self.store.save_project_document(self.project["id"], "Dummy", "body")

        scoped = MemoryService(self.store, grant=frozenset({"writing"}))
        context = scoped.project_context(self.project["id"][:8])
        self.assertEqual(context["project"]["memory_count"], 1)
        self.assertEqual(len(context["memories"]), 1)
        self.assertEqual(context["project"]["document_count"], 1)

        full = MemoryService(self.store).project_context(self.project["id"][:8])
        self.assertEqual(full["project"]["memory_count"], 2)
        self.assertEqual(full["project"]["document_count"], 1)


if __name__ == "__main__":
    unittest.main()
