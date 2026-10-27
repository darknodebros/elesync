"""Backup / restore is the trust-critical path for a memory vault: a user must be
able to snapshot the whole vault and rebuild it *losslessly* — same ids, same
supersession history, superseded facts still hidden from live recall. The store-level
round-trips (scopes, reindex, supersession) are pinned elsewhere; these tests pin the
user-facing `ele backup` / `ele restore` (and the underlying export/import) end to end,
so a future refactor can't silently turn the backup format lossy.
"""

from __future__ import annotations

import json
import os
import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch

from elesync import archive, cli
from elesync.normalize import normalize_file
from elesync.service import MemoryService
from elesync.store import MemoryStore


def _superseded_vault(path: str) -> tuple[str, str]:
    """Populate a vault at `path` with a superseded pair; return (old_id, new_id)."""
    svc = MemoryService(MemoryStore(path))
    try:
        svc.remember("Based in Westbrook", type="fact", tags="loc")
        svc.remember("Based in Portland", type="fact", tags="loc")
        items = {i.content: i.id for i in svc.store.all(include_superseded=True)}
        old, new = items["Based in Westbrook"], items["Based in Portland"]
        # explicit supersession so the link is deterministic regardless of auto-gate
        svc.supersede(old, new)
        return old, new
    finally:
        svc.store.close()


class TestBackupRestoreRoundTrip(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.src = str(Path(self.tmp.name) / "src")
        self.dst = str(Path(self.tmp.name) / "dst")
        self.backup = str(Path(self.tmp.name) / "backup.json")

    def tearDown(self):
        self.tmp.cleanup()

    def test_export_import_is_lossless(self):
        old, new = _superseded_vault(self.src)

        # export (via the store, the same payload `backup`/`export` write)
        src = MemoryStore(self.src)
        try:
            payload = cli._export_payload(src.all(include_superseded=True))
        finally:
            src.close()
        Path(self.backup).write_text(json.dumps(payload), encoding="utf-8")

        # the backup carries history, not just live facts
        self.assertEqual(payload["count"], 2)
        self.assertTrue(any(m.get("superseded_by") for m in payload["memories"]))

        # restore into a *fresh* vault
        dst = MemoryStore(self.dst)
        try:
            dst.import_many(normalize_file(self.backup))

            all_ids = {i.id for i in dst.all(include_superseded=True)}
            live_ids = {i.id for i in dst.all()}
            self.assertEqual(all_ids, {old, new})  # ids preserved, not re-minted
            self.assertEqual(live_ids, {new})  # superseded one hidden from live view

            # supersession link survived: old points at new, and history walks back
            restored_old = next(i for i in dst.all(include_superseded=True) if i.id == old)
            self.assertEqual(restored_old.superseded_by, new)
            self.assertTrue(restored_old.superseded_at)
            self.assertEqual([p.id for p in dst.predecessors(new)], [old])
        finally:
            dst.close()

    def test_restore_is_idempotent(self):
        _superseded_vault(self.src)
        src = MemoryStore(self.src)
        try:
            Path(self.backup).write_text(
                json.dumps(cli._export_payload(src.all(include_superseded=True))),
                encoding="utf-8",
            )
        finally:
            src.close()

        dst = MemoryStore(self.dst)
        try:
            first = dst.import_many(normalize_file(self.backup))
            second = dst.import_many(normalize_file(self.backup))
            self.assertEqual(first["added"], 2)
            self.assertEqual(second["added"], 0)  # nothing new on re-restore
            self.assertEqual(second["skipped_duplicates"], 2)
        finally:
            dst.close()

    def test_scopes_survive_round_trip(self):
        svc = MemoryService(MemoryStore(self.src))
        try:
            svc.remember("Work laptop is a Surface", type="fact", scopes="work")
            src_scoped = svc.store.all()[0].scopes
        finally:
            svc.store.close()

        src = MemoryStore(self.src)
        try:
            Path(self.backup).write_text(
                json.dumps(cli._export_payload(src.all(include_superseded=True))),
                encoding="utf-8",
            )
        finally:
            src.close()

        dst = MemoryStore(self.dst)
        try:
            dst.import_many(normalize_file(self.backup))
            self.assertEqual(dst.all()[0].scopes, src_scoped)
        finally:
            dst.close()


class TestBackupRestoreCli(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.src = str(Path(self.tmp.name) / "src")
        self.dst = str(Path(self.tmp.name) / "dst")

    def tearDown(self):
        self.tmp.cleanup()

    def test_backup_default_path_writes_timestamped_file(self):
        _superseded_vault(self.src)
        # No path arg -> a timestamped archive lands in the *managed* backup
        # directory (#92), not the current working directory as in <=1.15.
        managed = Path(self.tmp.name) / "managed"
        with patch.dict(os.environ, {"ELESYNC_BACKUP_DIR": str(managed)}):
            rc = cli.main(["--vault", self.src, "backup"])
        self.assertEqual(rc, 0)
        made = list(managed.glob("elesync-backup-*.eleb"))
        self.assertEqual(len(made), 1)
        header = archive.header_of(made[0])
        self.assertEqual(header["format"], "elesync-backup")
        self.assertEqual(header["count"], 2)
        self.assertIsNone(header["encryption"])  # unencrypted by default

    def test_backup_then_restore_cli(self):
        old, new = _superseded_vault(self.src)
        backup = str(Path(self.tmp.name) / "b.json")
        with MemoryStore(self.src) as src:
            project = src.create_project("Move project", "Track the relocation")
            src.add_to_project(project["id"], new)

        self.assertEqual(cli.main(["--vault", self.src, "backup", backup]), 0)
        self.assertEqual(cli.main(["--vault", self.dst, "restore", backup]), 0)

        with MemoryStore(self.dst) as dst:
            self.assertEqual({i.id for i in dst.all(include_superseded=True)}, {old, new})
            self.assertEqual({i.id for i in dst.all()}, {new})
            self.assertEqual(dst.project(project["id"])["name"], "Move project")
            self.assertEqual(dst.project_memories(project["id"])[0].id, new)


if __name__ == "__main__":
    unittest.main()
