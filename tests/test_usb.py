"""Tests for the USB portable vault feature (elesync.usb).

These tests mock out crypto so they run without PyNaCl installed — the
same pattern used for other optional-dependency paths. Tests that exercise real
encryption are skipped when PyNaCl is unavailable.
"""

from __future__ import annotations

import json
import os
import subprocess
import sys
import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch


class FakeCrypto:
    """Minimal stub that makes usb.install() think encryption is available."""

    @staticmethod
    def available():
        return True

    @staticmethod
    def new_salt():
        return b"\x00" * 32

    @staticmethod
    def default_limits():
        return 3, 8192

    @staticmethod
    def derive_key(passphrase, salt, ops, mem):
        return b"\x42" * 32

    @staticmethod
    def write_meta(vault_dir, salt, ops, mem):
        meta = {
            "elesync_encryption": 1,
            "kdf": "argon2id",
            "cipher": "xsalsa20poly1305",
            "salt": "AAAA",
            "opslimit": ops,
            "memlimit": mem,
        }
        (Path(vault_dir) / "vault.json").write_text(json.dumps(meta), encoding="utf-8")

    @staticmethod
    def is_encrypted(vault_dir):
        return (Path(vault_dir) / "vault.json").exists()

    @staticmethod
    def read_meta(vault_dir):
        p = Path(vault_dir) / "vault.json"
        if not p.exists():
            return None
        return json.loads(p.read_text())

    @staticmethod
    def encrypt(key, data):
        return b"\x00" + data  # trivial "encryption" — prepend a null byte

    @staticmethod
    def decrypt(key, blob):
        return blob[1:]  # strip the prepended byte


class FakeStore:
    """Stub that replaces MemoryStore so no real DB or files are created in tests."""

    def __init__(self, *args, **kwargs):
        pass

    def close(self):
        pass


# ---------------------------------------------------------------------------
# install()
# ---------------------------------------------------------------------------


class TestUsbInstall(unittest.TestCase):
    def setUp(self):
        self.drive_tmp = tempfile.TemporaryDirectory()
        self.drive = Path(self.drive_tmp.name)

    def tearDown(self):
        self.drive_tmp.cleanup()

    def _install(self, passphrase="letmein"):
        import elesync.usb as usb_mod

        with (
            patch.object(usb_mod, "crypto", FakeCrypto()),
            patch("elesync.usb.MemoryStore", FakeStore),
        ):
            return usb_mod.install(self.drive, passphrase)

    def test_creates_vault_dir(self):
        self._install()
        self.assertTrue((self.drive / "EleSyncVault" / "notes").is_dir())

    def test_creates_launcher(self):
        self._install()
        launcher = self.drive / "RUN_ME.py"
        self.assertTrue(launcher.exists())
        content = launcher.read_text(encoding="utf-8")
        self.assertIn("EleSyncVault", content)
        self.assertIn("onboard", content)

    def test_launcher_does_not_pip_install_elesync(self):
        # EleSync is not on PyPI: `pip install elesync` always fails, so the
        # launcher must point at the private install paths instead.
        self._install()
        content = (self.drive / "RUN_ME.py").read_text(encoding="utf-8")
        self.assertNotIn('"install", "elesync"', content)
        self.assertIn("not on PyPI", content)
        self.assertIn("EleSync-master.zip", content)
        self.assertIn("EleSync-Setup.exe", content)
        compile(content, "RUN_ME.py", "exec")

    def test_launcher_without_elesync_exits_with_install_help(self):
        self._install()
        # -S skips site-packages, so `import elesync` fails the way it does
        # on a computer that doesn't have EleSync installed yet.
        r = subprocess.run(
            [sys.executable, "-S", str(self.drive / "RUN_ME.py")],
            capture_output=True,
            text=True,
            encoding="utf-8",
            env={**os.environ, "PYTHONIOENCODING": "utf-8"},
            timeout=60,
        )
        self.assertEqual(r.returncode, 1, r.stderr)
        self.assertEqual(r.stderr, "")
        self.assertIn("EleSync is not installed", r.stdout)
        self.assertIn('python -m pip install "<path to EleSync-master.zip>"', r.stdout)
        if os.name == "nt":
            self.assertIn("usb attach", r.stdout)
        self.assertNotIn("Connecting vault", r.stdout)

    def test_creates_readme(self):
        self._install()
        readme = self.drive / "ELESYNC_README.txt"
        self.assertTrue(readme.exists())
        content = readme.read_text(encoding="utf-8")
        self.assertIn("encrypted", content.lower())
        self.assertIn("ChatGPT", content)
        self.assertIn("Claude", content)
        self.assertIn("Gemini", content)

    def test_writes_vault_json(self):
        self._install()
        # FakeCrypto.write_meta writes vault.json
        meta_path = self.drive / "EleSyncVault" / "vault.json"
        self.assertTrue(meta_path.exists())
        meta = json.loads(meta_path.read_text())
        self.assertEqual(meta.get("elesync_encryption"), 1)

    def test_result_dict(self):
        result = self._install()
        self.assertIn("vault_dir", result)
        self.assertIn("launcher", result)
        self.assertIn("readme", result)
        self.assertTrue(result["encrypted"])

    def test_drive_not_found_raises(self):
        import elesync.usb as usb_mod

        with (
            patch.object(usb_mod, "crypto", FakeCrypto()),
            patch("elesync.usb.MemoryStore", FakeStore),
        ):
            with self.assertRaises(FileNotFoundError):
                usb_mod.install(self.drive / "nonexistent" / "xyz", "pw")

    def test_no_encryption_raises(self):
        import elesync.usb as usb_mod

        fake = FakeCrypto()
        fake.available = staticmethod(lambda: False)
        with patch.object(usb_mod, "crypto", fake):
            with self.assertRaises(RuntimeError):
                usb_mod.install(self.drive, "pw")


# ---------------------------------------------------------------------------
# is_usb_vault()
# ---------------------------------------------------------------------------


class TestUsbIsVault(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.drive = Path(self.tmp.name)

    def tearDown(self):
        self.tmp.cleanup()

    def test_empty_drive_is_not_vault(self):
        from elesync.usb import is_usb_vault

        self.assertFalse(is_usb_vault(self.drive))

    def test_notes_dir_makes_it_a_vault(self):
        from elesync.usb import is_usb_vault

        (self.drive / "EleSyncVault" / "notes").mkdir(parents=True)
        self.assertTrue(is_usb_vault(self.drive))

    def test_vault_json_alone_makes_it_a_vault(self):
        from elesync.usb import is_usb_vault

        (self.drive / "EleSyncVault").mkdir(parents=True)
        (self.drive / "EleSyncVault" / "vault.json").write_text("{}", encoding="utf-8")
        self.assertTrue(is_usb_vault(self.drive))


# ---------------------------------------------------------------------------
# status()
# ---------------------------------------------------------------------------


class TestUsbStatus(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.drive = Path(self.tmp.name)

    def tearDown(self):
        self.tmp.cleanup()

    def test_status_empty_drive(self):
        import elesync.usb as usb_mod

        with patch.object(usb_mod, "crypto", FakeCrypto()):
            s = usb_mod.status(self.drive)
        self.assertFalse(s["vault_present"])
        self.assertEqual(s["note_count"], 0)

    def test_status_with_vault(self):
        import elesync.usb as usb_mod

        vp = self.drive / "EleSyncVault"
        notes = vp / "notes"
        notes.mkdir(parents=True)
        (vp / "vault.json").write_text(
            '{"elesync_encryption": 1, "kdf": "argon2id"}', encoding="utf-8"
        )
        # Write a fake encrypted note
        (notes / "abc123.md.enc").write_bytes(b"\x00test")

        with patch.object(usb_mod, "crypto", FakeCrypto()):
            s = usb_mod.status(self.drive)
        self.assertTrue(s["vault_present"])
        self.assertTrue(s["encrypted"])
        self.assertEqual(s["note_count"], 1)


# ---------------------------------------------------------------------------
# detach()
# ---------------------------------------------------------------------------


class TestUsbDetach(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.cfg = Path(self.tmp.name) / "claude_desktop_config.json"

    def tearDown(self):
        self.tmp.cleanup()

    def _write_cfg(self, servers: dict):
        self.cfg.write_text(json.dumps({"mcpServers": servers}, indent=2), encoding="utf-8")

    def test_detach_removes_server_entry(self):
        import elesync.usb as usb_mod

        self._write_cfg(
            {
                "elesync-usb": {"command": "python", "args": ["-m", "elesync.mcp_server"]},
                "something-else": {"command": "node"},
            }
        )
        with patch("elesync.usb.onboarding") as mock_ob:
            mock_ob.default_client_config_path.return_value = self.cfg
            result = usb_mod.detach("/fake/drive", name="elesync-usb")

        self.assertTrue(result["removed"])
        cfg = json.loads(self.cfg.read_text())
        self.assertNotIn("elesync-usb", cfg["mcpServers"])
        self.assertIn("something-else", cfg["mcpServers"])  # other entries preserved

    def test_detach_missing_server_key(self):
        import elesync.usb as usb_mod

        self._write_cfg({"something-else": {}})
        with patch("elesync.usb.onboarding") as mock_ob:
            mock_ob.default_client_config_path.return_value = self.cfg
            result = usb_mod.detach("/fake/drive", name="elesync-usb")
        self.assertFalse(result["removed"])

    def test_detach_missing_config(self):
        import elesync.usb as usb_mod

        with patch("elesync.usb.onboarding") as mock_ob:
            mock_ob.default_client_config_path.return_value = Path("/nonexistent/config.json")
            result = usb_mod.detach("/fake/drive")
        self.assertFalse(result["removed"])


# ---------------------------------------------------------------------------
# attach() — passphrase patching in config
# ---------------------------------------------------------------------------


class TestPatchPassphrase(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.cfg = Path(self.tmp.name) / "claude_desktop_config.json"

    def tearDown(self):
        self.tmp.cleanup()

    def test_passphrase_stored_in_keychain_not_config(self):
        # When the OS credential store is available, the passphrase must not be
        # written to the MCP config JSON — only the non-secret server key is.
        from unittest import mock

        import elesync.usb as usb_mod

        self.cfg.write_text(
            json.dumps({"mcpServers": {"elesync-usb": {"command": "py", "env": {}}}}),
            encoding="utf-8",
        )
        with mock.patch("elesync.credential_store.set_secret") as set_secret:
            usb_mod._patch_passphrase_in_config(self.cfg, "elesync-usb", "s3cr3t")
        set_secret.assert_called_once_with("vault:elesync-usb", "s3cr3t")
        env = json.loads(self.cfg.read_text())["mcpServers"]["elesync-usb"]["env"]
        self.assertNotIn("ELESYNC_PASSPHRASE", env)
        self.assertEqual(env["ELESYNC_SERVER_KEY"], "elesync-usb")

    def test_passphrase_falls_back_to_env_when_no_keychain(self):
        # No OS credential store — write the passphrase to config env as a
        # last resort so the MCP server can still auto-unlock the vault.
        from unittest import mock

        import elesync.usb as usb_mod

        self.cfg.write_text(
            json.dumps({"mcpServers": {"elesync-usb": {"command": "py"}}}),
            encoding="utf-8",
        )
        with mock.patch(
            "elesync.credential_store.set_secret",
            side_effect=OSError("no store"),
        ):
            usb_mod._patch_passphrase_in_config(self.cfg, "elesync-usb", "pw2")
        env = json.loads(self.cfg.read_text())["mcpServers"]["elesync-usb"]["env"]
        self.assertEqual(env["ELESYNC_PASSPHRASE"], "pw2")


# ---------------------------------------------------------------------------
# CLI integration — smoke tests
# ---------------------------------------------------------------------------


class TestUsbCLI(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.drive = Path(self.tmp.name)

    def tearDown(self):
        self.tmp.cleanup()

    def _run(self, argv):
        from elesync.cli import main

        return main(argv)

    def test_usb_list_no_crash(self):
        """ele usb list should exit 0 even if no removable drives are found."""
        import elesync.usb as usb_mod

        with patch.object(usb_mod, "list_removable_drives", return_value=[]):
            rc = self._run(["usb", "list"])
        self.assertEqual(rc, 0)

    def test_usb_status_no_vault(self):
        import elesync.usb as usb_mod

        with patch.object(usb_mod, "crypto", FakeCrypto()):
            rc = self._run(["usb", "status", str(self.drive)])
        self.assertEqual(rc, 0)

    def test_usb_install_missing_drive(self):
        import elesync.usb as usb_mod

        with (
            patch.object(usb_mod, "crypto", FakeCrypto()),
            patch("elesync.usb.MemoryStore", FakeStore),
            patch("elesync.cli._get_passphrase", return_value="pw"),
        ):
            rc = self._run(
                ["usb", "install", str(Path(tempfile.gettempdir()) / "nonexistent-drive-xyz123")]
            )
        self.assertEqual(rc, 1)

    def test_usb_install_success(self):
        import elesync.usb as usb_mod

        with (
            patch.object(usb_mod, "crypto", FakeCrypto()),
            patch("elesync.usb.MemoryStore", FakeStore),
            patch("elesync.cli._get_passphrase", return_value="pw"),
        ):
            rc = self._run(["usb", "install", str(self.drive)])
        self.assertEqual(rc, 0)
        self.assertTrue((self.drive / "RUN_ME.py").exists())
        self.assertTrue((self.drive / "ELESYNC_README.txt").exists())

    def test_usb_eject_no_crash(self):
        import elesync.usb as usb_mod

        with patch.object(usb_mod, "eject", return_value="Eject sent."):
            rc = self._run(["usb", "eject", str(self.drive)])
        self.assertEqual(rc, 0)


if __name__ == "__main__":
    unittest.main()
