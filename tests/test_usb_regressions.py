import base64
import json
import tempfile
import unittest
from pathlib import Path
from unittest.mock import Mock, patch

from elesync import archive, crypto, usb


class DriveDiscoveryTests(unittest.TestCase):
    def test_windows_refresh_replug_and_letter_change(self):
        kernel = Mock()
        kernel.GetLogicalDrives.side_effect = [(1 << 2) | (1 << 4), 1 << 2, (1 << 2) | (1 << 5)]
        kernel.GetDriveTypeW.side_effect = lambda root: 3 if root == "C:\\" else 2

        def volume(root, label, *args):
            label.value = "USB, café"
            return True

        kernel.GetVolumeInformationW.side_effect = volume
        with (
            patch.object(usb, "SYSTEM", "Windows"),
            patch.object(usb, "_windows_kernel", return_value=kernel),
            patch.object(usb, "_windows_usb_bus", return_value=False),
        ):
            self.assertEqual(usb.list_removable_drives()[0]["path"], "E:\\")
            self.assertEqual(usb.list_removable_drives(), [])
            result = usb.list_removable_drives()[0]
            self.assertEqual(result["path"], "F:\\")
            self.assertEqual(result["label"], "USB, café")
            self.assertEqual(kernel.GetLogicalDrives.call_count, 3)

    def test_fixed_usb_disk_included_internal_disk_excluded(self):
        kernel = Mock()
        kernel.GetLogicalDrives.return_value = (1 << 2) | (1 << 4)
        kernel.GetDriveTypeW.return_value = 3
        with (
            patch.object(usb, "_windows_kernel", return_value=kernel),
            patch.object(usb, "_windows_usb_bus", side_effect=lambda k, root: root == "E:\\"),
        ):
            self.assertEqual([d["path"] for d in usb._list_drives_windows()], ["E:\\"])

    def test_discovery_failure_is_nonfatal(self):
        for error in (OSError(), PermissionError()):
            with (
                patch.object(usb, "SYSTEM", "Windows"),
                patch.object(usb, "_windows_kernel", side_effect=error),
            ):
                self.assertEqual(usb.list_removable_drives(), [])
                with self.assertRaises(OSError):
                    usb.list_removable_drives(strict=True)


class DestinationTests(unittest.TestCase):
    def test_folder_browser_only_lists_directories_and_rejects_files(self):
        with tempfile.TemporaryDirectory() as root:
            folder = Path(root) / "folder with spaces"
            folder.mkdir()
            file = Path(root) / "private.txt"
            file.write_text("not exposed")
            self.assertEqual(usb.destination_folders(root)["folders"], [str(folder)])
            with self.assertRaises(ValueError):
                usb.validate_destination(file)

    def test_relative_and_missing_paths_rejected(self):
        with self.assertRaises(ValueError):
            usb.validate_destination("relative")
        with tempfile.TemporaryDirectory() as root:
            missing = Path(root) / "unplugged"
            with self.assertRaises(FileNotFoundError):
                usb.validate_destination(missing)
            self.assertFalse(missing.exists())

    def test_existing_vault_never_rekeyed(self):
        with tempfile.TemporaryDirectory() as root:
            vault = Path(root) / "EleSyncVault"
            vault.mkdir()
            meta = vault / "vault.json"
            meta.write_text("original key metadata")
            with patch.object(usb.crypto, "available", return_value=True):
                with self.assertRaises(FileExistsError):
                    usb.install(root, "different password")
            self.assertEqual(meta.read_text(), "original key metadata")


@unittest.skipUnless(crypto.available(), "PyNaCl required")
class PortableInstallTests(unittest.TestCase):
    def test_populated_encrypted_portable_install_survives_drive_letter_change(self):
        from elesync.models import MemoryItem

        with tempfile.TemporaryDirectory() as root:
            base = Path(root)
            source = base / "installed-app"
            source.mkdir()
            (source / "EleSync.exe").write_bytes(b"exe")
            (source / "_internal").mkdir()
            (source / "_internal" / "payload.dat").write_bytes(b"runtime")
            drive = base / "E"
            drive.mkdir()
            item = MemoryItem(content="portable secret", source="test")
            payload = {"memories": [item.to_dict()], "projects": {}, "count": 1}

            result = usb.install_portable(drive, "correct password", payload, app_source=source)
            self.assertTrue(result["portable"])
            self.assertTrue((drive / "EleSync" / "EleSync.exe").is_file())
            note_bytes = b"".join(
                p.read_bytes() for p in (drive / "EleSyncVault" / "notes").iterdir()
            )
            self.assertNotIn(b"portable secret", note_bytes)

            moved = base / "F"
            drive.rename(moved)
            marker = json.loads((moved / "EleSync" / "portable.json").read_text())
            self.assertEqual(
                (moved / "EleSync" / marker["vault_dir"]).resolve(),
                (moved / "EleSyncVault").resolve(),
            )

    def test_existing_files_are_preserved(self):
        with tempfile.TemporaryDirectory() as root:
            drive = Path(root) / "drive"
            source = Path(root) / "app"
            drive.mkdir()
            source.mkdir()
            (source / "EleSync.exe").write_bytes(b"exe")
            (drive / "EleSync").mkdir()
            kept = drive / "EleSync" / "keep.txt"
            kept.write_text("keep")
            with self.assertRaises(FileExistsError):
                usb.install_portable(drive, "pw", {"memories": []}, app_source=source)
            self.assertEqual(kept.read_text(), "keep")


@unittest.skipUnless(crypto.available(), "PyNaCl required")
class EncryptedBackupTests(unittest.TestCase):
    def test_dashboard_export_preserves_history_projects_and_templates(self):
        from elesync import templates, web
        from elesync.service import MemoryService
        from elesync.store import MemoryStore

        with tempfile.TemporaryDirectory() as root:
            source = MemoryStore(Path(root) / "source")
            destination = Path(root) / "destination"
            destination.mkdir()
            try:
                service = MemoryService(source)
                service.remember("Earlier fact", type="fact")
                service.remember("Updated fact", type="fact")
                items = source.all()
                service.supersede(items[0].id, items[1].id)
                project = source.create_project("Portable project")
                source.add_to_project(project["id"], items[1].id)
                source.save_project_document(project["id"], "Handoff", "Preserve this document")
                templates.save(
                    source.vault_dir, templates.Template(name="personal", body="Private template")
                )
                with patch.object(web, "_get_store", return_value=source):
                    result = web._api_usb_install(
                        {"drive": str(destination), "passphrase": "test password", "mode": "export"}
                    )
                    with self.assertRaises(ValueError):
                        web._api_usb_install(
                            {
                                "drive": str(source.vault_dir),
                                "passphrase": "test password",
                                "mode": "export",
                            }
                        )
                metadata = archive.header_of(result["backup_path"])["encryption"]
                key = crypto.derive_key(
                    "test password",
                    base64.b64decode(metadata["salt"]),
                    metadata["opslimit"],
                    metadata["memlimit"],
                )
                payload = archive.read(result["backup_path"], key=key)
                self.assertEqual(payload["count"], 2)
                self.assertTrue(any(item["superseded_by"] for item in payload["memories"]))
                self.assertEqual(payload["projects"], source.project_snapshot())
                self.assertEqual(payload["templates"]["personal"]["body"], "Private template")
            finally:
                source.close()

    def test_empty_and_nonempty_destinations_and_encrypted_roundtrip(self):
        payload = {
            "count": 1,
            "memories": [{"content": "secret memory"}],
            "projects": {"documents": [{"body": "secret project"}]},
            "templates": {"private": {"content": "secret template"}},
        }
        with tempfile.TemporaryDirectory() as root:
            destination = Path(root)
            for index in range(2):
                result = usb.export_backup(destination, "correct password", payload)
                target = Path(result["backup_path"])
                self.assertTrue(target.is_file())
                self.assertEqual(len(list(destination.glob("*.eleb"))), index + 1)
                self.assertNotIn(b"secret", target.read_bytes())
                meta = archive.header_of(target)["encryption"]
                key = crypto.derive_key(
                    "correct password",
                    base64.b64decode(meta["salt"]),
                    meta["opslimit"],
                    meta["memlimit"],
                )
                self.assertEqual(archive.read(target, key=key), payload)
                with self.assertRaises(archive.DecryptError):
                    archive.read(target, key=b"x" * 32)
            self.assertFalse(list(destination.glob("*.tmp")))

    def test_write_failure_leaves_no_successful_backup(self):
        with tempfile.TemporaryDirectory() as root:
            with patch.object(archive, "write", side_effect=PermissionError("read-only drive")):
                with self.assertRaises(PermissionError):
                    usb.export_backup(root, "password", {"count": 0})
            self.assertEqual(list(Path(root).iterdir()), [])


if __name__ == "__main__":
    unittest.main()
