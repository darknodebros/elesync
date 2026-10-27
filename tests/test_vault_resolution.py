"""Vault-path resolution is a single code path with one precedence order.

Regression tests for a real footgun: the CLI used to carry THREE resolvers with
different precedence -- `_resolve_vault` (--vault > $ELESYNC_DIR > config >
default), `_vault_path` (--vault > config > $ELESYNC_DIR > default) and
`_resolve_vault_dir` (config > $ELESYNC_DIR > default) -- and `ele serve`
bypassed config entirely. With $ELESYNC_DIR and a configured `vault_dir` both
set, `ele encrypt` could encrypt a different vault than the one `ele search`
was reading. Everything now routes through `_resolve_vault`.
"""

import argparse
import json
import os
import tempfile
import unittest
from pathlib import Path
from unittest import mock

from elesync import cli


def _args(vault=None):
    return argparse.Namespace(vault=vault)


class _ResolutionCase(unittest.TestCase):
    """Isolate config + env so the developer's real setup can't leak in."""

    def setUp(self):
        self._tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self._tmp.cleanup)
        base = Path(self._tmp.name)
        self.config_dir = base / "appdata"
        self.env_vault = base / "env-vault"
        self.cfg_vault = base / "cfg-vault"
        self.flag_vault = base / "flag-vault"
        patcher = mock.patch.dict(os.environ, {"APPDATA": str(self.config_dir)})
        patcher.start()
        self.addCleanup(patcher.stop)
        os.environ.pop("ELESYNC_DIR", None)

    def _write_config(self, vault_dir):
        p = self.config_dir / "EleSync" / "config.json"
        p.parent.mkdir(parents=True, exist_ok=True)
        p.write_text(json.dumps({"vault_dir": str(vault_dir)}), encoding="utf-8")


class TestResolveVaultPrecedence(_ResolutionCase):
    def test_flag_beats_env_and_config(self):
        os.environ["ELESYNC_DIR"] = str(self.env_vault)
        self._write_config(self.cfg_vault)
        self.assertEqual(cli._resolve_vault(_args(str(self.flag_vault))), self.flag_vault)

    def test_env_beats_config(self):
        """The historical divergence: `_vault_path` put config first here."""
        os.environ["ELESYNC_DIR"] = str(self.env_vault)
        self._write_config(self.cfg_vault)
        self.assertEqual(cli._resolve_vault(_args()), self.env_vault)

    def test_config_when_no_env(self):
        self._write_config(self.cfg_vault)
        self.assertEqual(cli._resolve_vault(_args()), self.cfg_vault)

    def test_default_when_nothing_set(self):
        expected = (Path("~") / "EleSyncVault").expanduser()
        self.assertEqual(cli._resolve_vault(_args()), expected)

    def test_accepts_no_args_namespace(self):
        os.environ["ELESYNC_DIR"] = str(self.env_vault)
        self.assertEqual(cli._resolve_vault(), self.env_vault)


class TestResolveVaultDirAgrees(_ResolutionCase):
    def test_env_beats_config(self):
        """`_resolve_vault_dir` (the `ele` welcome path) used to put config first."""
        os.environ["ELESYNC_DIR"] = str(self.env_vault)
        self._write_config(self.cfg_vault)
        self.assertEqual(cli._resolve_vault_dir(), self.env_vault.resolve())


class TestSingleResolver(_ResolutionCase):
    def test_vault_path_is_gone(self):
        """`_vault_path` must not come back -- it had a different precedence order."""
        self.assertFalse(hasattr(cli, "_vault_path"))

    def test_serve_resolves_through_resolve_vault(self):
        """`ele serve` used to hand `args.vault` (often None) straight to
        build_server, which skipped the configured vault_dir entirely."""
        self._write_config(self.cfg_vault)
        args = argparse.Namespace(
            vault=None,
            transport="stdio",
            host="127.0.0.1",
            port=8000,
            allowed_host=None,
            insecure=False,
        )
        with mock.patch("elesync.mcp_server.build_server") as build:
            build.return_value = mock.Mock()
            cli.cmd_serve(args)
        build.assert_called_once()
        self.assertEqual(build.call_args.args[0], str(self.cfg_vault))


if __name__ == "__main__":
    unittest.main()
