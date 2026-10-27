"""Tests for the rclone remote backup adapter (#93).

rclone is an external binary, so these tests mock subprocess.run to
exercise the push/pull/list/prune logic without requiring rclone to be
installed or a real remote to be configured. The mock returns canned
outputs matching what rclone would produce.
"""

from __future__ import annotations

import json
import subprocess
import tempfile
import unittest
from pathlib import Path
from unittest.mock import MagicMock, patch

from elesync import backups, remote


class TestRemoteAdapter(unittest.TestCase):
    """The rclone adapter wraps subprocess calls — mock them and verify
    the right rclone commands are issued."""

    def setUp(self):
        self._tmp = tempfile.TemporaryDirectory()
        self.local_dir = Path(self._tmp.name)

    def tearDown(self):
        self._tmp.cleanup()

    def _make_backup(self, name: str = "elesync-backup-20260728-120000.eleb") -> Path:
        """Create a dummy .eleb file and a manifest entry."""
        path = self.local_dir / name
        path.write_bytes(b"dummy archive content")
        # Write a manifest with this entry.
        manifest = {
            "manifest_version": 1,
            "backups": [
                {
                    "filename": name,
                    "created_at": "2026-07-28T12:00:00+00:00",
                    "size": len(path.read_bytes()),
                    "format_version": 1,
                    "encrypted": False,
                    "sha256": "abc123",
                    "recorded_at": "2026-07-28T12:00:01+00:00",
                }
            ],
        }
        backups.save(self.local_dir, manifest)
        return path

    def _mock_run(self, returncode=0, stdout="", stderr=""):
        """Create a mock subprocess.run that returns a CompletedProcess."""
        result = MagicMock(spec=subprocess.CompletedProcess)
        result.returncode = returncode
        result.stdout = stdout
        result.stderr = stderr
        return MagicMock(return_value=result)

    @patch("shutil.which", return_value="/usr/bin/rclone")
    def test_push_copies_file_and_manifest(self, _mock_which):
        """push() issues rclone copyto for the file and the manifest."""
        path = self._make_backup()
        mock_run = self._mock_run()
        with patch("subprocess.run", mock_run):
            dest = remote.push(path, "s3:my-bucket/elesync")
        self.assertIn("elesync-backup-20260728-120000.eleb", dest)
        # Two calls: one for the file, one for the manifest.
        self.assertEqual(mock_run.call_count, 2)
        # First call should be copyto with the file.
        first_args = mock_run.call_args_list[0][0][0]
        self.assertIn("copyto", first_args)
        self.assertIn(str(path), first_args)

    @patch("shutil.which", return_value="/usr/bin/rclone")
    def test_push_nonexistent_file_raises(self, _mock_which):
        """push() raises if the local file doesn't exist."""
        with self.assertRaises(remote.RemoteError):
            remote.push(self.local_dir / "nonexistent.eleb", "s3:bucket")

    @patch("shutil.which", return_value=None)
    def test_push_without_rclone_raises(self, _mock_which):
        """push() raises a clear error if rclone isn't installed."""
        path = self._make_backup()
        with self.assertRaises(remote.RemoteError) as ctx:
            remote.push(path, "s3:bucket")
        self.assertIn("rclone not found", str(ctx.exception))

    @patch("shutil.which", return_value="/usr/bin/rclone")
    def test_pull_newest_downloads_first_file(self, _mock_which):
        """pull() without a name pulls the newest backup."""
        manifest_json = json.dumps(
            {
                "manifest_version": 1,
                "backups": [
                    {
                        "filename": "elesync-backup-20260728-120000.eleb",
                        "created_at": "2026-07-28T12:00:00+00:00",
                    },
                    {
                        "filename": "elesync-backup-20260727-090000.eleb",
                        "created_at": "2026-07-27T09:00:00+00:00",
                    },
                ],
            }
        )
        mock_run = self._mock_run(stdout=manifest_json)
        with patch("subprocess.run", mock_run):
            local_path = remote.pull("s3:bucket/elesync", self.local_dir)
        self.assertEqual(local_path.name, "elesync-backup-20260728-120000.eleb")

    @patch("shutil.which", return_value="/usr/bin/rclone")
    def test_pull_specific_name(self, _mock_which):
        """pull() with a name downloads that specific file."""
        mock_run = self._mock_run()
        with patch("subprocess.run", mock_run):
            local_path = remote.pull("s3:bucket/elesync", self.local_dir, name="specific.eleb")
        self.assertEqual(local_path.name, "specific.eleb")
        # The rclone command should reference the specific file in the path.
        args = mock_run.call_args[0][0]
        self.assertIn("s3:bucket/elesync/specific.eleb", args)

    @patch("shutil.which", return_value="/usr/bin/rclone")
    def test_pull_rejects_path_traversal(self, _mock_which):
        """pull() rejects names with path separators (security)."""
        with self.assertRaises(remote.RemoteError):
            remote.pull("s3:bucket", self.local_dir, name="../etc/passwd")
        with self.assertRaises(remote.RemoteError):
            remote.pull("s3:bucket", self.local_dir, name="subdir/file.eleb")

    @patch("shutil.which", return_value="/usr/bin/rclone")
    def test_list_reads_manifest_first(self, _mock_which):
        """list_remote() reads the manifest when available."""
        manifest_json = json.dumps(
            {
                "manifest_version": 1,
                "backups": [
                    {"filename": "b.eleb", "created_at": "2026-07-28T12:00:00+00:00"},
                    {"filename": "a.eleb", "created_at": "2026-07-27T09:00:00+00:00"},
                ],
            }
        )
        mock_run = self._mock_run(stdout=manifest_json)
        with patch("subprocess.run", mock_run):
            entries = remote.list_remote("s3:bucket/elesync")
        self.assertEqual(len(entries), 2)
        self.assertEqual(entries[0]["filename"], "b.eleb")  # newest first

    @patch("shutil.which", return_value="/usr/bin/rclone")
    def test_list_falls_back_to_lsf_when_manifest_missing(self, _mock_which):
        """list_remote() falls back to `rclone lsf` when the manifest is absent."""
        # First call (cat manifest) fails; second (lsf) succeeds.
        fail_result = MagicMock(spec=subprocess.CompletedProcess)
        fail_result.returncode = 1
        fail_result.stdout = ""
        fail_result.stderr = "object not found"

        ok_result = MagicMock(spec=subprocess.CompletedProcess)
        ok_result.returncode = 0
        ok_result.stdout = "b.eleb\na.eleb\n"
        ok_result.stderr = ""

        mock_run = MagicMock(side_effect=[fail_result, ok_result])
        with patch("subprocess.run", mock_run):
            entries = remote.list_remote("s3:bucket/elesync")
        self.assertEqual(len(entries), 2)
        self.assertEqual(entries[0]["filename"], "b.eleb")  # sorted newest-first
        self.assertEqual(entries[0]["status"], backups.UNRECORDED)

    @patch("shutil.which", return_value="/usr/bin/rclone")
    def test_prune_deletes_oldest_beyond_keep(self, _mock_which):
        """prune() deletes backups beyond the keep count, newest first."""
        entries = [
            {"filename": f"backup-{i}.eleb", "created_at": f"2026-07-{i:02d}T00:00:00+00:00"}
            for i in range(5, 0, -1)  # 5 newest to 1 oldest
        ]
        with patch.object(remote, "list_remote", return_value=entries):
            mock_run = self._mock_run()
            with patch("subprocess.run", mock_run):
                deleted = remote.prune("s3:bucket", keep=2)
        # Should delete the 3 oldest (backups 3, 2, 1).
        self.assertEqual(len(deleted), 3)

    @patch("shutil.which", return_value="/usr/bin/rclone")
    def test_prune_protects_named_file(self, _mock_which):
        """prune() never deletes the protected file (just-pushed backup).

        With keep=1 and 3 entries, the 2 oldest would normally be deleted.
        If the protected file is the 2nd-newest, it must survive."""
        entries = [
            {"filename": "newest.eleb", "created_at": "2026-07-29T12:00:00+00:00"},
            {"filename": "protected.eleb", "created_at": "2026-07-28T12:00:00+00:00"},
            {"filename": "oldest.eleb", "created_at": "2026-07-27T09:00:00+00:00"},
        ]
        with patch.object(remote, "list_remote", return_value=entries):
            mock_run = self._mock_run()
            with patch("subprocess.run", mock_run):
                deleted = remote.prune("s3:bucket", keep=1, protect="protected.eleb")
        # Without protection, entries[1:] = [protected, oldest] would both be deleted.
        # With protection, only "oldest" is deleted.
        self.assertEqual(deleted, ["oldest.eleb"])

    @patch("shutil.which", return_value="/usr/bin/rclone")
    def test_prune_keep_zero_deletes_nothing(self, _mock_which):
        """keep=0 means no retention — don't delete anything."""
        entries = [{"filename": "a.eleb", "created_at": "2026-01-01T00:00:00+00:00"}]
        with patch.object(remote, "list_remote", return_value=entries):
            deleted = remote.prune("s3:bucket", keep=0)
        self.assertEqual(deleted, [])

    @patch("shutil.which", return_value=None)
    def test_is_available_false_when_not_installed(self, _mock_which):
        self.assertFalse(remote.is_available())

    def test_prune_rejects_every_unsafe_candidate_before_any_deletion(self):
        unsafe = (
            "../outside.eleb",
            "/outside.eleb",
            "subdir/backup.eleb",
            "..\\outside.eleb",
            "C:outside.eleb",
            "backup.eleb:stream.eleb",
            "backup\x00.eleb",
            "backup\n.eleb",
            "config.json",
            "",
            None,
            123,
        )
        for name in unsafe:
            with (
                self.subTest(name=name),
                patch.object(
                    remote,
                    "list_remote",
                    return_value=[
                        {"filename": "newest.eleb"},
                        {"filename": "valid-old.eleb"},
                        {"filename": name},
                    ],
                ),
                patch.object(remote, "_run") as run,
                self.assertRaises(remote.RemoteError),
            ):
                remote.prune("s3:bucket/backups", keep=1)
            run.assert_not_called()

    def test_prune_also_validates_retained_and_protected_entries(self):
        for entries, protect in (
            ([{"filename": "../retained.eleb"}, {"filename": "old.eleb"}], None),
            ([{"filename": "new.eleb"}, {"filename": "../protected.eleb"}], "../protected.eleb"),
        ):
            with (
                self.subTest(protect=protect),
                patch.object(remote, "list_remote", return_value=entries),
                patch.object(remote, "_run") as run,
                self.assertRaises(remote.RemoteError),
            ):
                remote.prune("s3:bucket/backups", keep=1, protect=protect)
            run.assert_not_called()

    def test_tampered_manifest_aborts_prune_without_fallback_or_deletion(self):
        manifest = json.dumps(
            {"backups": [{"filename": "new.eleb"}, {"filename": "../outside.eleb"}]}
        )
        with (
            patch.object(remote, "_run", return_value=self._mock_run(stdout=manifest)()) as run,
            self.assertRaises(remote.RemoteError),
        ):
            remote.prune("s3:bucket/backups", keep=1)
        self.assertEqual(run.call_count, 1)
        self.assertEqual(run.call_args.args[0][0], "cat")

    def test_fallback_listing_cannot_supply_a_traversal_target(self):
        results = [
            self._mock_run(returncode=1)(),
            self._mock_run(stdout="new.eleb\n../outside.eleb\n")(),
        ]
        with (
            patch.object(remote, "_run", side_effect=results) as run,
            self.assertRaises(remote.RemoteError),
        ):
            remote.prune("s3:bucket/backups", keep=1)
        self.assertEqual([call.args[0][0] for call in run.call_args_list], ["cat", "lsf"])

    def test_pull_rejects_drive_and_stream_names_before_downloading(self):
        for name in ("C:outside.eleb", "backup.eleb:stream.eleb", "../outside.eleb", 123):
            with (
                self.subTest(name=name),
                patch.object(remote, "_run") as run,
                self.assertRaises(remote.RemoteError),
            ):
                remote.pull("s3:bucket/backups", self.local_dir, name=name)
            run.assert_not_called()

    def test_safe_custom_names_stay_inside_the_remote_prefix(self):
        name = "résumé backup 2026.eleb"
        with (
            patch.object(
                remote, "list_remote", return_value=[{"filename": "new.eleb"}, {"filename": name}]
            ),
            patch.object(remote, "_run", return_value=self._mock_run()()) as run,
        ):
            self.assertEqual(remote.prune("s3:bucket/backups/", keep=1), [name])
        run.assert_called_once_with(["deletefile", "s3:bucket/backups/" + name], check=False)
        with patch.object(remote, "_run", return_value=self._mock_run()()) as run:
            self.assertEqual(
                remote.pull("s3:bucket/backups", self.local_dir, name), self.local_dir / name
            )
        self.assertEqual(run.call_args.args[0][1], "s3:bucket/backups/" + name)

    @patch("shutil.which", return_value="/usr/bin/rclone")
    def test_is_available_true_when_installed(self, _mock_which):
        self.assertTrue(remote.is_available())


class TestBackupPushCli(unittest.TestCase):
    """The CLI push/pull commands dispatch to the remote module."""

    def test_push_without_destination_returns_error(self):
        from elesync.cli import cmd_backup_push

        args = type("A", (), {"destination": None, "dir": None, "vault": None})()
        with (
            patch.dict("os.environ", {}, clear=True),
            patch("elesync.cli._load_config", return_value={}),
        ):
            result = cmd_backup_push(args)
        self.assertEqual(result, 1)

    def test_pull_without_destination_returns_error(self):
        from elesync.cli import cmd_backup_pull

        args = type("A", (), {"destination": None, "name": None, "dir": None, "vault": None})()
        with (
            patch.dict("os.environ", {}, clear=True),
            patch("elesync.cli._load_config", return_value={}),
        ):
            result = cmd_backup_pull(args)
        self.assertEqual(result, 1)

    def test_argv_rewrite_handles_push_and_pull(self):
        from elesync.cli import _rewrite_backup_list

        # _rewrite_backup_list operates on argv without the program name
        # (as main() passes sys.argv[1:]).
        self.assertEqual(
            _rewrite_backup_list(["backup", "push", "--destination", "s3:b"]),
            ["backup-push", "--destination", "s3:b"],
        )
        self.assertEqual(
            _rewrite_backup_list(["backup", "pull"]),
            ["backup-pull"],
        )
        # `backup list` still rewrites.
        self.assertEqual(
            _rewrite_backup_list(["backup", "list"]),
            ["backup-list"],
        )
        # `backup <path>` is not rewritten.
        self.assertEqual(
            _rewrite_backup_list(["backup", "/some/path.eleb"]),
            ["backup", "/some/path.eleb"],
        )


if __name__ == "__main__":
    unittest.main()
