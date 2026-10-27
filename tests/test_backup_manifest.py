"""Backup manifest, listing, and retention (#92).

`ele backup` (#91) writes a `.eleb` archive; #92 gives those archives a managed home,
a manifest recording what each one is, `ele backup list`, and `backup.keep` retention.

These tests pin the parts that are dangerous to get wrong: retention deletes files, so
it must never run ahead of a confirmed-good new backup, never delete the backup that
justified it, and never touch a directory the user named explicitly. They also pin the
degradation contract — a missing, corrupt, or incomplete manifest must cost metadata,
never visibility.
"""

from __future__ import annotations

import json
import os
import unittest
from contextlib import contextmanager
from pathlib import Path
from tempfile import TemporaryDirectory
from unittest import mock

from elesync import archive, backups, cli
from elesync.service import MemoryService
from elesync.store import MemoryStore


def _seed(vault: str) -> None:
    svc = MemoryService(MemoryStore(vault))
    try:
        svc.remember("Based in Westbrook", type="fact", tags="loc")
        svc.remember("Prefers dark mode", type="preference")
    finally:
        svc.store.close()


@contextmanager
def _env(**kwargs):
    """Set/clear env vars for the duration of a block."""
    previous = {k: os.environ.get(k) for k in kwargs}
    try:
        for k, v in kwargs.items():
            if v is None:
                os.environ.pop(k, None)
            else:
                os.environ[k] = v
        yield
    finally:
        for k, v in previous.items():
            if v is None:
                os.environ.pop(k, None)
            else:
                os.environ[k] = v


class _VaultCase(unittest.TestCase):
    """A seeded vault plus a clean, isolated backup directory."""

    def setUp(self):
        self.tmp = TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        root = Path(self.tmp.name)
        self.vault = str(root / "vault")
        _seed(self.vault)
        self.backup_dir = root / "backups"
        # Neutralise ambient config/env so a developer's real settings can't
        # steer these tests.
        patched = _env(ELESYNC_BACKUP_DIR=str(self.backup_dir), ELESYNC_BACKUP_KEEP=None)
        patched.__enter__()
        self.addCleanup(patched.__exit__, None, None, None)

    def backup(self, *extra: str) -> int:
        return cli.main(["--vault", self.vault, "backup", *extra])

    def archives(self) -> list[str]:
        return sorted(p.name for p in self.backup_dir.glob("*.eleb"))

    def place(self, created_at: str, name: str | None = None) -> Path:
        """Put an archive in the managed dir with a chosen `created_at`.

        Ordering and retention key off the archive *header*, not the filename, and
        `created_at` is whole-second — so backups taken by the CLI inside one test
        would tie. Writing the header directly makes the age of each backup explicit.
        """
        self.backup_dir.mkdir(parents=True, exist_ok=True)
        # NTFS forbids ":" in filenames -- keep the default name Windows-safe
        # (ordering and retention key off the archive header, not the name).
        stamp = created_at[:19].replace(":", "-")
        path = self.backup_dir / (name or f"elesync-backup-{stamp}.eleb")
        archive.write(
            path, {"elesync_export": 1, "exported_at": created_at, "count": 0, "memories": []}
        )
        backups.record(path)
        return path

    def manifest(self) -> dict:
        return json.loads(backups.manifest_path(self.backup_dir).read_text(encoding="utf-8"))


class TestManifestRecording(_VaultCase):
    def test_backup_lands_in_managed_dir_and_is_recorded(self):
        self.assertEqual(self.backup(), 0)
        names = self.archives()
        self.assertEqual(len(names), 1)

        entries = self.manifest()["backups"]
        self.assertEqual(len(entries), 1)
        entry = entries[0]

        # Acceptance criterion: timestamp, size, format version, encryption state,
        # and checksum are all recorded.
        self.assertEqual(entry["filename"], names[0])
        self.assertTrue(entry["created_at"])
        self.assertEqual(entry["size"], (self.backup_dir / names[0]).stat().st_size)
        self.assertEqual(entry["format_version"], archive.FORMAT_VERSION)
        self.assertFalse(entry["encrypted"])
        self.assertEqual(entry["sha256"], backups.checksum(self.backup_dir / names[0]))

    def test_recorded_checksum_is_of_the_real_bytes(self):
        self.backup()
        path = self.backup_dir / self.archives()[0]
        entry = backups.entries(self.backup_dir)[0]
        self.assertIs(backups.verify(self.backup_dir, entry), True)

        with open(path, "r+b") as fh:  # flip a byte in the body
            fh.seek(-1, os.SEEK_END)
            last = fh.read(1)
            fh.seek(-1, os.SEEK_END)
            fh.write(bytes([last[0] ^ 0xFF]))

        self.assertIs(backups.verify(self.backup_dir, backups.entries(self.backup_dir)[0]), False)

    def test_explicit_path_is_recorded_in_its_own_directory(self):
        elsewhere = Path(self.tmp.name) / "elsewhere"
        elsewhere.mkdir()
        target = elsewhere / "mine.eleb"
        self.assertEqual(self.backup(str(target)), 0)

        self.assertTrue(target.exists())
        self.assertFalse(self.backup_dir.exists())  # managed dir untouched
        recorded = json.loads(backups.manifest_path(elsewhere).read_text(encoding="utf-8"))[
            "backups"
        ]
        self.assertEqual([e["filename"] for e in recorded], ["mine.eleb"])

    def test_rewriting_the_same_filename_replaces_its_entry(self):
        target = self.backup_dir / "fixed-name.eleb"
        self.assertEqual(self.backup(str(target)), 0)
        first = self.manifest()["backups"][0]["sha256"]

        store = MemoryStore(self.vault)
        try:
            MemoryService(store).remember("A third memory", type="fact")
        finally:
            store.close()
        self.assertEqual(self.backup(str(target)), 0)

        entries = self.manifest()["backups"]
        self.assertEqual(len(entries), 1, "the same filename must not accumulate entries")
        self.assertNotEqual(entries[0]["sha256"], first)
        self.assertEqual(entries[0]["sha256"], backups.checksum(target))


class TestListing(_VaultCase):
    def test_list_surfaces_manifest_metadata(self):
        self.backup()
        rows = backups.entries(self.backup_dir)
        self.assertEqual(len(rows), 1)
        self.assertEqual(rows[0]["status"], backups.OK)
        self.assertEqual(rows[0]["count"], 2)
        self.assertEqual(rows[0]["format_version"], archive.FORMAT_VERSION)
        self.assertEqual(cli.main(["--vault", self.vault, "backup", "list"]), 0)

    def test_newest_first(self):
        for day in ("01", "03", "02"):  # deliberately out of order on disk
            self.place(f"2026-01-{day}T00:00:00+00:00")
        created = [e["created_at"] for e in backups.entries(self.backup_dir)]
        self.assertEqual(
            created,
            ["2026-01-03T00:00:00+00:00", "2026-01-02T00:00:00+00:00", "2026-01-01T00:00:00+00:00"],
        )

    def test_same_second_backups_order_deterministically(self):
        """Whole-second `created_at` means ties are possible; the filename breaks them
        so retention never deletes an arbitrary one of two same-second backups."""
        stamp = "2026-01-01T00:00:00+00:00"
        for name in ("b.eleb", "a.eleb", "c.eleb"):
            self.place(stamp, name=name)
        self.assertEqual(
            [e["filename"] for e in backups.entries(self.backup_dir)],
            ["c.eleb", "b.eleb", "a.eleb"],
        )

    def test_missing_manifest_degrades_to_reading_headers(self):
        self.backup()
        name = self.archives()[0]
        backups.manifest_path(self.backup_dir).unlink()

        rows = backups.entries(self.backup_dir)
        self.assertEqual([r["filename"] for r in rows], [name])
        self.assertEqual(rows[0]["status"], backups.UNRECORDED)
        self.assertEqual(rows[0]["count"], 2)  # still read from the archive header
        self.assertIsNone(rows[0]["sha256"])  # nothing to verify against
        self.assertIsNone(backups.verify(self.backup_dir, rows[0]))
        self.assertEqual(cli.main(["--vault", self.vault, "backup", "list"]), 0)

    def test_corrupt_manifest_is_moved_aside_not_silently_overwritten(self):
        self.backup()
        manifest = backups.manifest_path(self.backup_dir)
        manifest.write_text("{ this is not json", encoding="utf-8")

        rows = backups.entries(self.backup_dir)  # must not raise
        self.assertEqual(len(rows), 1)
        self.assertTrue(manifest.with_suffix(manifest.suffix + ".corrupt").exists())

    def test_manifest_entry_without_its_file_is_reported_missing(self):
        self.backup()
        (self.backup_dir / self.archives()[0]).unlink()
        rows = backups.entries(self.backup_dir)
        self.assertEqual([r["status"] for r in rows], [backups.MISSING])

    def test_unreadable_eleb_is_listed_not_crashed_on(self):
        self.backup()
        (self.backup_dir / "junk.eleb").write_bytes(b"not an archive at all")
        rows = backups.entries(self.backup_dir)
        junk = next(r for r in rows if r["filename"] == "junk.eleb")
        self.assertTrue(junk["unreadable"])
        self.assertEqual(cli.main(["--vault", self.vault, "backup", "list", "--verify"]), 0)

    def test_empty_directory_lists_cleanly(self):
        self.assertEqual(backups.entries(self.backup_dir), [])
        self.assertEqual(cli.main(["--vault", self.vault, "backup", "list"]), 0)


class TestRetention(_VaultCase):
    def _aged(self, *days: str) -> list[Path]:
        return [self.place(f"2026-01-{d}T00:00:00+00:00") for d in days]

    def test_keep_prunes_oldest_and_keeps_newest(self):
        old, mid, new = self._aged("01", "02", "03")
        with _env(ELESYNC_BACKUP_KEEP="2"):
            self.assertEqual(self.backup(), 0)

        remaining = self.archives()
        self.assertEqual(len(remaining), 2)
        self.assertNotIn(old.name, remaining)
        self.assertNotIn(mid.name, remaining)
        self.assertIn(new.name, remaining, "the newest pre-existing backup survives")
        # The manifest is pruned in step with the files it describes.
        self.assertEqual(sorted(e["filename"] for e in self.manifest()["backups"]), remaining)

    def test_the_new_backup_is_never_the_one_pruned(self):
        # A future-dated backup sorts *newest*, so a naive "keep the top N" would
        # keep it and delete the backup just written. It must go instead.
        future = self.place("2099-01-01T00:00:00+00:00")
        with _env(ELESYNC_BACKUP_KEEP="1"):
            self.assertEqual(self.backup(), 0)

        remaining = self.archives()
        self.assertEqual(len(remaining), 1)
        self.assertNotIn(future.name, remaining)
        self.assertEqual(
            backups.entries(self.backup_dir)[0]["status"],
            backups.OK,
            "the surviving backup must be the freshly written, recorded one",
        )

    def test_unrecorded_archives_count_against_keep(self):
        """Santo's call: retention manages the directory, not just the manifest — a
        hand-copied archive counts toward `keep` instead of accumulating forever."""
        self._aged("01", "02")
        backups.manifest_path(self.backup_dir).unlink()  # both now unrecorded
        with _env(ELESYNC_BACKUP_KEEP="1"):
            self.assertEqual(self.backup(), 0)
        self.assertEqual(len(self.archives()), 1)

    def test_retention_never_touches_an_explicit_destination(self):
        elsewhere = Path(self.tmp.name) / "elsewhere"
        elsewhere.mkdir()
        for stamp in ("a", "b", "c"):
            self.backup(str(elsewhere / f"{stamp}.eleb"))
        with _env(ELESYNC_BACKUP_KEEP="1"):
            self.assertEqual(self.backup(str(elsewhere / "d.eleb")), 0)
        self.assertEqual(len(list(elsewhere.glob("*.eleb"))), 4)

    def test_no_pruning_when_keep_is_unset_or_invalid(self):
        self._aged("01", "02")
        for value in (None, "0", "-3", "banana", ""):
            with _env(ELESYNC_BACKUP_KEEP=value):
                self.assertEqual(self.backup(), 0)
        self.assertGreaterEqual(len(self.archives()), 3)

    def test_prune_is_skipped_when_the_manifest_update_fails(self):
        """Create-then-prune: retention runs only after the new backup *and* its
        manifest entry are both safely on disk. If recording fails, nothing is
        deleted — the alternative trades away the only confirmed-good copy."""
        self._aged("01", "02")
        before = self.archives()

        with (
            _env(ELESYNC_BACKUP_KEEP="1"),
            mock.patch.object(backups, "record", side_effect=OSError("disk full")),
            mock.patch.object(backups, "prune") as pruned,
        ):
            self.assertEqual(self.backup(), 0)

        pruned.assert_not_called()
        self.assertTrue(set(before).issubset(set(self.archives())))

    def test_keep_resolution_prefers_env_over_config(self):
        with _env(ELESYNC_BACKUP_KEEP="5"):
            self.assertEqual(backups.resolve_keep({"backup": {"keep": 2}}), 5)
        with _env(ELESYNC_BACKUP_KEEP=None):
            self.assertEqual(backups.resolve_keep({"backup": {"keep": 2}}), 2)
            self.assertIsNone(backups.resolve_keep({}))

    def test_dir_resolution_prefers_env_then_config_then_vault(self):
        with _env(ELESYNC_BACKUP_DIR="/tmp/from-env"):
            self.assertEqual(
                backups.resolve_dir("/v", {"backup": {"dir": "/c"}}), Path("/tmp/from-env")
            )
        with _env(ELESYNC_BACKUP_DIR=None):
            self.assertEqual(backups.resolve_dir("/v", {"backup": {"dir": "/c"}}), Path("/c"))
            self.assertEqual(backups.resolve_dir("/v", {}), Path("/v/backups"))


class TestRoundTrip(_VaultCase):
    def test_managed_backup_restores_into_a_fresh_vault(self):
        self.backup()
        name = self.archives()[0]
        dst = str(Path(self.tmp.name) / "restored")
        self.assertEqual(cli.main(["--vault", dst, "restore", str(self.backup_dir / name)]), 0)
        store = MemoryStore(dst)
        try:
            self.assertEqual(store.stats()["total"], 2)
        finally:
            store.close()

    def test_restore_warns_when_the_recorded_checksum_no_longer_matches(self):
        self.backup()
        path = self.backup_dir / self.archives()[0]
        payload = archive.read(path)
        payload["memories"] = payload["memories"][:1]
        archive.write(path, payload)  # rewritten behind the manifest's back

        dst = str(Path(self.tmp.name) / "restored")
        with mock.patch("builtins.print") as printed:
            self.assertEqual(cli.main(["--vault", dst, "restore", str(path)]), 0)
        said = " ".join(str(c.args[0]) for c in printed.call_args_list if c.args)
        self.assertIn(path.name, said)
        self.assertTrue(
            any("checksum" in str(c.args[0]).lower() for c in printed.call_args_list if c.args),
            f"expected a checksum warning, got: {said}",
        )


class TestBackupListDispatch(unittest.TestCase):
    def test_bare_list_is_rewritten_after_global_options(self):
        self.assertEqual(cli._rewrite_backup_list(["backup", "list"]), ["backup-list"])
        self.assertEqual(
            cli._rewrite_backup_list(["--vault", "/v", "backup", "list", "--verify"]),
            ["--vault", "/v", "backup-list", "--verify"],
        )
        self.assertEqual(
            cli._rewrite_backup_list(["--vault=/v", "--semantic", "backup", "list"]),
            ["--vault=/v", "--semantic", "backup-list"],
        )

    def test_a_path_named_list_still_reaches_backup(self):
        self.assertEqual(cli._rewrite_backup_list(["backup", "./list"]), ["backup", "./list"])
        self.assertEqual(cli._rewrite_backup_list(["backup"]), ["backup"])
        self.assertEqual(cli._rewrite_backup_list(["list"]), ["list"])
        # A vault directory literally named "backup" must not be mistaken for the
        # command: the command token is found by skipping valued globals.
        self.assertEqual(
            cli._rewrite_backup_list(["--vault", "backup", "list"]),
            ["--vault", "backup", "list"],
        )

    def test_value_globals_matches_parser(self):
        """Drift guard: `_command_index` skips valued global options by name. If a new
        one is added to the parser without being listed, `ele backup list` would break
        in that position — fail here instead."""
        parser = cli.build_parser()
        valued = set()
        for action in parser._actions:
            if action.option_strings and action.nargs != 0 and action.dest != "help":
                if not isinstance(action, __import__("argparse")._VersionAction):
                    valued.update(action.option_strings)
        self.assertEqual(valued, set(cli._VALUE_GLOBALS))


if __name__ == "__main__":
    unittest.main()
