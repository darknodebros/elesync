# tests/test_cli_command_boundaries.py
"""Regression tests for CLI command-family extraction boundaries.

These tests intentionally exercise observable command behavior through
``elesync.cli.main``. They should remain green while USB and web-user command
implementations move out of ``cli.py`` into ``elesync.commands``.
"""

from __future__ import annotations

import tempfile
import unittest
from pathlib import Path
from unittest.mock import MagicMock, patch


class TestUsbCliContract(unittest.TestCase):
    """Protect the public ``ele usb`` behavior during command extraction."""

    def _run(self, argv: list[str]) -> int:
        from elesync.cli import main

        return main(argv)

    def test_list_without_drives_returns_success(self) -> None:
        with patch("elesync.usb.list_removable_drives", return_value=[]):
            result = self._run(["usb", "list"])

        self.assertEqual(result, 0)

    def test_install_failure_returns_error_and_reports_to_stderr(self) -> None:
        with (
            patch("elesync.cli._get_passphrase", return_value="secret"),
            patch(
                "elesync.usb.install",
                side_effect=FileNotFoundError("drive is missing"),
            ),
            patch("elesync.cli._err") as report_error,
        ):
            result = self._run(["usb", "install", "/missing-drive"])

        self.assertEqual(result, 1)
        report_error.assert_called_once()
        self.assertIn("drive is missing", str(report_error.call_args.args[0]))

    def test_attach_forwards_default_server_name(self) -> None:
        attach_result = {
            "server_key": "elesync-usb",
            "config_path": "/tmp/claude_desktop_config.json",
        }

        with (
            patch("elesync.cli._get_passphrase", return_value="secret"),
            patch("elesync.usb.attach", return_value=attach_result) as attach,
        ):
            result = self._run(["usb", "attach", "/media/usb"])

        self.assertEqual(result, 0)
        attach.assert_called_once_with(
            "/media/usb",
            "secret",
            name="elesync-usb",
        )

    def test_attach_failure_is_converted_to_cli_error(self) -> None:
        with (
            patch("elesync.cli._get_passphrase", return_value="secret"),
            patch(
                "elesync.usb.attach",
                side_effect=ValueError("invalid portable vault"),
            ),
            patch("elesync.cli._err") as report_error,
        ):
            result = self._run(["usb", "attach", "/media/usb"])

        self.assertEqual(result, 1)
        report_error.assert_called_once()
        self.assertIn("invalid portable vault", str(report_error.call_args.args[0]))

    def test_detach_with_eject_calls_both_operations(self) -> None:
        detach_result = {
            "removed": True,
            "config_path": "/tmp/claude_desktop_config.json",
        }

        with (
            patch("elesync.usb.detach", return_value=detach_result) as detach,
            patch("elesync.usb.eject", return_value="Eject sent.") as eject,
        ):
            result = self._run(["usb", "detach", "/media/usb", "--eject"])

        self.assertEqual(result, 0)
        detach.assert_called_once_with(
            "/media/usb",
            name="elesync-usb",
        )
        eject.assert_called_once_with("/media/usb")

    def test_status_forwards_drive_to_usb_backend(self) -> None:
        status = {
            "drive": "/media/usb",
            "vault_present": False,
            "encrypted": False,
            "note_count": 0,
            "launcher_present": False,
            "size_gb": 12.5,
        }

        with patch("elesync.usb.status", return_value=status) as get_status:
            result = self._run(["usb", "status", "/media/usb"])

        self.assertEqual(result, 0)
        get_status.assert_called_once_with("/media/usb")


class TestZeroCliLaunch(unittest.TestCase):
    """Running ``ele`` without a subcommand opens the dashboard."""

    def test_no_arguments_launches_web_ui(self) -> None:
        from elesync import cli

        vault = Path("C:/EleSync-Test-Vault")
        with (
            patch.object(cli, "_resolve_vault_dir", return_value=vault),
            patch.object(cli, "_load_config", return_value={}),
            patch("elesync.web.serve") as serve,
        ):
            result = cli.main([])

        self.assertEqual(result, 0)
        serve.assert_called_once_with(
            vault_dir=vault,
            port=7477,
            open_browser=True,
            lang=cli.get_language(),
        )


class TestWebUserCliContract(unittest.TestCase):
    """Protect web-user administration behavior during command extraction."""

    def setUp(self) -> None:
        self.temp_dir = tempfile.TemporaryDirectory()
        self.vault = Path(self.temp_dir.name)
        (self.vault / "notes").mkdir()

    def tearDown(self) -> None:
        self.temp_dir.cleanup()

    def _run(self, argv: list[str]) -> int:
        from elesync.cli import main

        return main(["--vault", str(self.vault), *argv])

    def _manager(self) -> MagicMock:
        manager = MagicMock()
        manager.list_users.return_value = []
        return manager

    def test_adduser_missing_vault_fails_before_password_prompt(self) -> None:
        missing_vault = self.vault / "does-not-exist"

        from elesync.cli import main

        with (
            patch("elesync.cli._get_web_password") as get_password,
            patch("elesync.cli._err") as report_error,
        ):
            result = main(
                [
                    "--vault",
                    str(missing_vault),
                    "web",
                    "adduser",
                    "alice",
                ]
            )

        self.assertEqual(result, 1)
        get_password.assert_not_called()
        report_error.assert_called_once()

    def test_adduser_first_admin_uses_password_and_role(self) -> None:
        manager = self._manager()

        with (
            patch("elesync.cli._get_web_password", return_value="secret-pass"),
            patch("elesync.web_auth.UserManager", return_value=manager),
        ):
            result = self._run(["web", "adduser", "alice"])

        self.assertEqual(result, 0)
        manager.add_user.assert_called_once_with(
            "alice",
            "secret-pass",
            role="admin",
            guest=False,
        )

    def test_adduser_guest_forwards_guest_flag(self) -> None:
        manager = self._manager()

        with (
            patch("elesync.cli._get_web_password", return_value="secret-pass"),
            patch("elesync.web_auth.UserManager", return_value=manager),
        ):
            result = self._run(["web", "adduser", "guest1", "--guest"])

        self.assertEqual(result, 0)
        manager.add_user.assert_called_once_with(
            "guest1",
            "secret-pass",
            role="admin",
            guest=True,
        )

    def test_adduser_validation_error_becomes_cli_error(self) -> None:
        manager = self._manager()
        manager.add_user.side_effect = ValueError("username already exists")

        with (
            patch("elesync.cli._get_web_password", return_value="secret-pass"),
            patch("elesync.web_auth.UserManager", return_value=manager),
            patch("elesync.cli._err") as report_error,
        ):
            result = self._run(["web", "adduser", "alice"])

        self.assertEqual(result, 1)
        report_error.assert_called_once()
        self.assertIn(
            "username already exists",
            str(report_error.call_args.args[0]),
        )

    def test_removeuser_returns_success_and_removes_requested_user(self) -> None:
        manager = self._manager()
        manager.list_users.return_value = []

        with patch("elesync.web_auth.UserManager", return_value=manager):
            result = self._run(["web", "removeuser", "alice"])

        self.assertEqual(result, 0)
        manager.remove_user.assert_called_once_with("alice")

    def test_removeuser_unknown_user_becomes_cli_error(self) -> None:
        manager = self._manager()
        manager.remove_user.side_effect = ValueError("unknown user")

        with (
            patch("elesync.web_auth.UserManager", return_value=manager),
            patch("elesync.cli._err") as report_error,
        ):
            result = self._run(["web", "removeuser", "ghost"])

        self.assertEqual(result, 1)
        report_error.assert_called_once()
        self.assertIn("unknown user", str(report_error.call_args.args[0]))

    def test_listusers_returns_success_for_empty_user_database(self) -> None:
        manager = self._manager()

        with patch("elesync.web_auth.UserManager", return_value=manager):
            result = self._run(["web", "listusers"])

        self.assertEqual(result, 0)
        manager.list_users.assert_called_once()


class TestWebPasswordContract(unittest.TestCase):
    """Protect dashboard-password lookup independently of vault encryption."""

    def test_environment_password_avoids_prompt(self) -> None:
        from elesync.cli import _get_web_password

        with (
            patch.dict(
                "os.environ",
                {"ELESYNC_WEB_PASSWORD": "from-env"},
                clear=False,
            ),
            patch("getpass.getpass") as prompt,
        ):
            password = _get_web_password(confirm=True)

        self.assertEqual(password, "from-env")
        prompt.assert_not_called()

    def test_empty_password_is_rejected(self) -> None:
        from elesync.cli import _get_web_password

        with (
            patch.dict("os.environ", {}, clear=True),
            patch("getpass.getpass", return_value=""),
            patch("elesync.cli._err") as report_error,
        ):
            with self.assertRaises(SystemExit) as raised:
                _get_web_password(confirm=False)

        self.assertEqual(raised.exception.code, 1)
        report_error.assert_called_once_with("Password cannot be empty.")

    def test_confirmation_mismatch_is_rejected(self) -> None:
        from elesync.cli import _get_web_password

        with (
            patch.dict("os.environ", {}, clear=True),
            patch(
                "getpass.getpass",
                side_effect=["first-password", "different-password"],
            ),
            patch("elesync.cli._err") as report_error,
        ):
            with self.assertRaises(SystemExit) as raised:
                _get_web_password(confirm=True)

        self.assertEqual(raised.exception.code, 1)
        report_error.assert_called_once_with("Passwords do not match.")


if __name__ == "__main__":
    unittest.main()
