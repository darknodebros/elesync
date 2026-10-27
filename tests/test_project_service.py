from __future__ import annotations

import tempfile
import unittest
from pathlib import Path
from unittest import mock

from elesync.models import MemoryItem
from elesync.project_service import ProjectService
from elesync.store import MemoryStore


class TestProjectService(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.store = MemoryStore(Path(self.temp.name))
        self.projects = ProjectService(self.store)

    def tearDown(self):
        self.store.close()
        self.temp.cleanup()

    def test_get_assembles_project_context_for_web_consumers(self):
        project = self.projects.create("Research", "A focused workspace")
        self.assertEqual(self.projects.list_projects()[0]["id"], project["id"])
        loaded = self.projects.get(project["id"])
        self.assertEqual(loaded["name"], "Research")
        self.assertEqual(loaded["memories"], [])
        self.assertEqual(loaded["documents"], [])

    def test_project_access_lifecycle_stays_inside_service(self):
        project = self.projects.create("Shared work")
        access = self.projects.set_access(project["id"], "claude", True, False, False)
        self.assertEqual(access["provider"], "claude")
        self.assertEqual(self.projects.access(project["id"])[0]["can_read"], 1)
        with mock.patch("elesync.oauth.revoke_project_authorizations") as revoke_tokens:
            self.assertTrue(self.projects.revoke_access(project["id"], "claude"))
        revoke_tokens.assert_called_once_with(self.store.vault_dir, project["id"], "claude")
        self.assertEqual(self.projects.access(project["id"])[0]["status"], "revoked")

    def test_timeline_derives_project_events_from_persisted_records(self):
        project = self.projects.create("Launch planning")
        memory = MemoryItem(content="Keep the release notes short.")
        self.store.upsert(memory)
        self.assertTrue(self.projects.add_memory(project["id"], memory.id))
        self.projects.save_document(
            project["id"], "Go-live decision", "Launch on Monday.", "decision"
        )
        self.projects.set_access(project["id"], "claude", True, False, False)
        self.assertTrue(self.projects.revoke_access(project["id"], "claude"))

        events = self.projects.timeline(project["id"])
        self.assertEqual(
            {event["type"] for event in events},
            {
                "project_created",
                "memory_added",
                "document_created",
                "access_granted",
                "access_revoked",
            },
        )

    def test_ledger_survives_reopening_the_vault(self):
        project = self.projects.create("Durable activity")
        task = self.projects.save_document(project["id"], "Ship release", "Publish it.", "task")
        self.assertIsNotNone(self.projects.complete_task(project["id"], task["id"]))
        vault_path = Path(self.temp.name)
        self.store.close()
        self.store = MemoryStore(vault_path)
        self.projects = ProjectService(self.store)

        events = self.projects.timeline(project["id"])
        self.assertEqual(
            {event["type"] for event in events},
            {"project_created", "task_created", "task_completed"},
        )


if __name__ == "__main__":
    unittest.main()
