import json
import tempfile
import unittest
from pathlib import Path

from elesync import demo, normalize
from elesync.audit import AuditLog
from elesync.project_service import ProjectService
from elesync.store import MemoryStore


class TestDemoStoryline(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        self.vault = Path(self.tmp.name)
        self.store = MemoryStore(self.vault)
        self.addCleanup(self.store.close)

    def _import_history(self):
        export = self.vault / "conversations.json"
        export.write_text(json.dumps(demo.history_export()), encoding="utf-8")
        items = normalize.normalize_file(export)
        self.store.import_many(items)
        return items

    def test_history_imports_as_claude_and_suggests_the_kyoto_trip(self):
        items = self._import_history()
        self.assertEqual(len(items), len(demo.HISTORY))
        self.assertTrue(all(item.source == "claude" for item in items))
        names = [s["name"] for s in ProjectService(self.store).discover()["suggestions"]]
        self.assertIn("Kyoto trip", names)

    def test_full_storyline_is_attributed_and_access_is_enforced(self):
        self._import_history()
        results = {}
        for step in demo.STEPS[1:]:
            results[step["id"]] = demo.run_step(self.store, str(self.vault), step["id"])

        self.assertIn("Saved", results["claude-saves"]["message"])
        self.assertIn("no longer active", results["chatgpt-blocked"]["message"])
        self.assertTrue(results["revoke-chatgpt"]["notice"])

        entries = list(reversed(AuditLog(self.vault).read()))
        seen = [(e["op"], e.get("ai")) for e in entries]
        self.assertEqual(
            seen,
            [
                ("REMEMBER", "claude"),
                ("PROJECT_RECALL", "chatgpt"),
                ("REMEMBER", "chatgpt"),
                ("CONFLICT", "chatgpt"),
                ("ACCESS_DENIED", "chatgpt"),
                ("PROJECT_CONTEXT", "grok"),
                ("CAPTURE", "phone"),
            ],
        )

        project_id = results["connect"]["project_id"]
        status = {row["provider"]: row["status"] for row in self.store.project_access(project_id)}
        self.assertEqual(status, {"claude": "active", "chatgpt": "revoked", "grok": "active"})

    def test_demo_projects_keep_their_id_across_runs(self):
        self.store.project_id_factory = demo.stable_project_id
        first = self.store.create_project("Kyoto trip")
        self.assertEqual(first["id"], demo.stable_project_id("kyoto trip"))

        with tempfile.TemporaryDirectory() as other:
            again = MemoryStore(Path(other))
            again.project_id_factory = demo.stable_project_id
            try:
                self.assertEqual(again.create_project("Kyoto trip")["id"], first["id"])
            finally:
                again.close()

        # A second project with the same name still gets a unique id.
        self.assertNotEqual(self.store.create_project("Kyoto trip")["id"], first["id"])

    def test_projects_outside_the_demo_get_random_ids(self):
        project = self.store.create_project("Kyoto trip")
        self.assertNotEqual(project["id"], demo.stable_project_id("Kyoto trip"))

    def test_demo_never_runs_inside_an_existing_vault(self):
        import argparse
        from unittest import mock

        from elesync import cli

        def run(folder):
            args = argparse.Namespace(dir=str(folder), port=7490, no_browser=True)
            with (
                mock.patch("elesync.web.serve") as serve,
                mock.patch.dict("os.environ", {"APPDATA": self.tmp.name}),
            ):
                code = cli.cmd_demo(args)
            return code, serve.called

        real = Path(self.tmp.name) / "home"
        (real / "EleSyncVault" / "notes").mkdir(parents=True)
        (real / "EleSyncVault" / "notes" / "mine.md").write_text("my real note", encoding="utf-8")
        self.assertEqual(run(real), (2, False))
        self.assertFalse((real / "EleSyncVault" / ".elesync-demo").exists())
        self.assertEqual(
            (real / "EleSyncVault" / "notes" / "mine.md").read_text(encoding="utf-8"),
            "my real note",
        )

        fresh = Path(self.tmp.name) / "fresh"
        self.assertEqual(run(fresh), (0, True))
        # A folder the demo created can be reused for the next take.
        self.assertEqual(run(fresh), (0, True))

    def test_a_real_vault_made_after_a_demo_is_still_protected(self):
        import argparse
        import shutil
        from unittest import mock

        from elesync import cli

        home = Path(self.tmp.name) / "home2"
        home.mkdir()

        def run():
            args = argparse.Namespace(dir=str(home), port=7490, no_browser=True)
            with (
                mock.patch("elesync.web.serve") as serve,
                mock.patch.dict("os.environ", {"APPDATA": self.tmp.name}),
            ):
                code = cli.cmd_demo(args)
            return code, serve.called

        self.assertEqual(run(), (0, True))
        # The demo vault is cleared away and the real vault is set up at the
        # default location in the same folder.
        shutil.rmtree(home / "EleSyncVault")
        (home / "EleSyncVault" / "notes").mkdir(parents=True)
        (home / "EleSyncVault" / "notes" / "mine.md").write_text("my real note", encoding="utf-8")
        self.assertEqual(run(), (2, False))

    def test_unknown_step_is_rejected(self):
        with self.assertRaises(ValueError):
            demo.run_step(self.store, str(self.vault), "launch-rockets")
