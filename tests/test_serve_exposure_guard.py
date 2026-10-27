"""Guard against starting an unauthenticated MCP transport that is reachable
off this machine.

History: the original check keyed on the bind address alone, so
`ele serve --transport http --port 8000 --allowed-host xxxx.ngrok-free.dev`
-- loopback bind, public tunnel -- printed no warning at all and started an
endpoint exposing `remember` and `forget` to anyone who found the URL.
"""

from __future__ import annotations

import io
import json
import os
import tempfile
import threading
import unittest
from contextlib import redirect_stderr, redirect_stdout
from pathlib import Path
from unittest import mock

from elesync import cli


class TestPublicExposureReasons(unittest.TestCase):
    def test_loopback_bind_without_allowed_hosts_is_private(self):
        for host in ("127.0.0.1", "::1", "localhost", "LocalHost"):
            with self.subTest(host=host):
                self.assertEqual(cli.public_exposure_reasons(host, None), [])

    def test_non_loopback_bind_is_flagged(self):
        reasons = cli.public_exposure_reasons("0.0.0.0", None)
        self.assertEqual(len(reasons), 1)
        self.assertIn("not loopback", reasons[0])

    def test_tunnel_hostname_on_loopback_bind_is_flagged(self):
        """The regression: loopback bind + public --allowed-host."""
        reasons = cli.public_exposure_reasons(
            "127.0.0.1", ["canopener-stand-thirty.ngrok-free.dev"]
        )
        self.assertEqual(len(reasons), 1)
        self.assertIn("ngrok-free.dev", reasons[0])

    def test_loopback_allowed_hosts_are_not_flagged(self):
        self.assertEqual(
            cli.public_exposure_reasons("127.0.0.1", ["localhost", "127.0.0.1:8000", "[::1]"]),
            [],
        )

    def test_host_port_forms_are_normalized(self):
        self.assertEqual(cli._normalize_allowed_host("example.com:8000"), "example.com")
        self.assertEqual(cli._normalize_allowed_host("  Example.COM  "), "example.com")
        self.assertEqual(cli._normalize_allowed_host("[::1]:8000"), "[::1]")

    def test_both_signals_report_both_reasons(self):
        reasons = cli.public_exposure_reasons("0.0.0.0", ["tunnel.example.com"])
        self.assertEqual(len(reasons), 2)


class _ServeArgs:
    """Minimal stand-in for the argparse namespace cmd_serve consumes."""

    def __init__(self, **kw):
        self.transport = "http"
        self.host = "127.0.0.1"
        self.port = 8000
        self.allowed_host = None
        self.insecure = False
        self.vault = None
        self.__dict__.update(kw)


class TestServeRefusesUnauthenticatedExposure(unittest.TestCase):
    def setUp(self):
        self._env = dict(os.environ)
        os.environ.pop("ELESYNC_TOKEN", None)
        patcher = mock.patch.object(cli, "_load_config", return_value={})
        self.addCleanup(patcher.stop)
        patcher.start()

    def tearDown(self):
        os.environ.clear()
        os.environ.update(self._env)

    def _run(self, args):
        err, out = io.StringIO(), io.StringIO()
        with redirect_stderr(err), redirect_stdout(out):
            rc = cli.cmd_serve(args)
        return rc, err.getvalue()

    def test_refuses_the_exact_tunnel_invocation(self):
        args = _ServeArgs(allowed_host=["canopener-stand-thirty.ngrok-free.dev"])
        with mock.patch("elesync.mcp_server.build_server") as build:
            rc, err = self._run(args)
        self.assertEqual(rc, 2)
        build.assert_not_called()
        self.assertIn("Refusing to start", err)
        self.assertIn("ele auth generate-token", err)

    def test_refuses_non_loopback_bind(self):
        args = _ServeArgs(host="0.0.0.0")
        with mock.patch("elesync.mcp_server.build_server") as build:
            rc, _ = self._run(args)
        self.assertEqual(rc, 2)
        build.assert_not_called()

    def test_insecure_override_starts_but_warns(self):
        args = _ServeArgs(allowed_host=["tunnel.example.com"], insecure=True)
        with mock.patch("elesync.mcp_server.build_server") as build:
            rc, err = self._run(args)
        self.assertEqual(rc, 0)
        build.assert_called_once()
        self.assertIn("NO authentication", err)

    def test_configured_token_allows_exposure_without_override(self):
        cli._load_config.return_value = {"http": {"auth": {"token": "s3cret"}}}
        args = _ServeArgs(allowed_host=["tunnel.example.com"])
        with mock.patch("elesync.mcp_server.build_server") as build:
            rc, err = self._run(args)
        self.assertEqual(rc, 0)
        build.assert_called_once()
        self.assertNotIn("Refusing to start", err)

    def test_env_token_also_satisfies_the_guard(self):
        os.environ["ELESYNC_TOKEN"] = "from-env"
        args = _ServeArgs(allowed_host=["tunnel.example.com"])
        with mock.patch("elesync.mcp_server.build_server") as build:
            rc, err = self._run(args)
        self.assertEqual(rc, 0)
        build.assert_called_once()
        self.assertNotIn("Refusing to start", err)

    def test_local_only_http_is_untouched(self):
        args = _ServeArgs()
        with mock.patch("elesync.mcp_server.build_server") as build:
            rc, err = self._run(args)
        self.assertEqual(rc, 0)
        build.assert_called_once()
        self.assertNotIn("Refusing to start", err)

    def test_stdio_transport_is_never_gated(self):
        args = _ServeArgs(transport="stdio", host="0.0.0.0")
        with mock.patch("elesync.mcp_server.build_server") as build:
            rc, err = self._run(args)
        self.assertEqual(rc, 0)
        build.assert_called_once()
        self.assertNotIn("Refusing to start", err)


class TestAuthCommandExists(unittest.TestCase):
    """The refusal message names `ele auth generate-token`; it must resolve."""

    def setUp(self):
        self._env = dict(os.environ)
        os.environ.pop("ELESYNC_TOKEN", None)
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        self.cfg_path = Path(self.tmp.name) / "config.json"
        import elesync.config as config_mod

        patcher = mock.patch.object(config_mod, "config_path", return_value=self.cfg_path)
        self.addCleanup(patcher.stop)
        patcher.start()

    def tearDown(self):
        os.environ.clear()
        os.environ.update(self._env)

    def test_auth_is_a_registered_subcommand(self):
        parser = cli.build_parser() if hasattr(cli, "build_parser") else None
        if parser is None:  # parser built inline in main(); fall back to argv parse
            with self.assertRaises(SystemExit) as ctx:
                cli.main(["auth", "--help"])
            self.assertEqual(ctx.exception.code, 0)

    def test_generate_token_persists_and_status_reports_it(self):
        out = io.StringIO()
        with redirect_stdout(out):
            rc = cli.cmd_auth_generate_token(_ServeArgs(force=False))
        self.assertEqual(rc, 0)

        cfg = json.loads(self.cfg_path.read_text(encoding="utf-8"))
        token = cfg["http"]["auth"]["token"]
        self.assertEqual(cfg["http"]["auth"]["mode"], "token")
        self.assertGreaterEqual(len(token), 32)
        self.assertIn(token, out.getvalue())

        status = io.StringIO()
        with redirect_stdout(status):
            cli.cmd_auth_status(_ServeArgs())
        self.assertIn("ENABLED", status.getvalue())

    def test_generate_token_refuses_to_clobber_without_force(self):
        with redirect_stdout(io.StringIO()):
            cli.cmd_auth_generate_token(_ServeArgs(force=False))
        first = json.loads(self.cfg_path.read_text(encoding="utf-8"))["http"]["auth"]["token"]

        err = io.StringIO()
        with redirect_stderr(err), redirect_stdout(io.StringIO()):
            rc = cli.cmd_auth_generate_token(_ServeArgs(force=False))
        self.assertEqual(rc, 1)
        self.assertIn("--force", err.getvalue())

        unchanged = json.loads(self.cfg_path.read_text(encoding="utf-8"))["http"]["auth"]["token"]
        self.assertEqual(first, unchanged)

    def test_revoke_clears_the_token(self):
        with redirect_stdout(io.StringIO()):
            cli.cmd_auth_generate_token(_ServeArgs(force=False))
            rc = cli.cmd_auth_revoke(_ServeArgs())
        self.assertEqual(rc, 0)
        cfg = json.loads(self.cfg_path.read_text(encoding="utf-8"))
        self.assertNotIn("token", cfg["http"]["auth"])
        self.assertEqual(cfg["http"]["auth"]["mode"], "none")

    def test_status_reports_disabled_when_nothing_configured(self):
        out = io.StringIO()
        with redirect_stdout(out):
            cli.cmd_auth_status(_ServeArgs())
        self.assertIn("DISABLED", out.getvalue())


class TestConnectClients(unittest.TestCase):
    def setUp(self):
        self._env = dict(os.environ)
        os.environ.pop("ELESYNC_TOKEN", None)
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        self.cfg_path = Path(self.tmp.name) / "config.json"
        self.vault = Path(self.tmp.name) / "vault"
        import elesync.config as config_mod

        patcher = mock.patch.object(config_mod, "config_path", return_value=self.cfg_path)
        self.addCleanup(patcher.stop)
        patcher.start()

    def tearDown(self):
        os.environ.clear()
        os.environ.update(self._env)

    def _run(self, argv):
        out, err = io.StringIO(), io.StringIO()
        with redirect_stdout(out), redirect_stderr(err):
            rc = cli.main(["--vault", str(self.vault), *argv])
        return rc, out.getvalue(), err.getvalue()

    def test_connect_chatgpt_requires_a_public_url_or_allowed_host(self):
        rc, _out, err = self._run(["connect", "chatgpt"])
        self.assertEqual(rc, 1)
        self.assertIn("ChatGPT needs a reachable HTTPS URL", err)

    def test_connect_chatgpt_generates_token_and_prints_safe_setup(self):
        rc, out, err = self._run(
            ["connect", "chatgpt", "--allowed-host", "demo.ngrok-free.dev", "--port", "9000"]
        )
        self.assertEqual(rc, 0, err)
        self.assertIn("ChatGPT connection setup", out)
        self.assertIn("https://demo.ngrok-free.dev/mcp", out)
        self.assertIn("Authorization: Bearer", out)
        self.assertIn(
            f"serve --transport http --port 9000 --allowed-host {cli._quote_cli_arg('demo.ngrok-free.dev')}",
            out,
        )

        cfg = json.loads(self.cfg_path.read_text(encoding="utf-8"))
        token = cfg["http"]["auth"]["token"]
        self.assertGreaterEqual(len(token), 32)
        self.assertIn(token, out)

    def test_connect_chatgpt_reuses_existing_token(self):
        self.cfg_path.parent.mkdir(parents=True, exist_ok=True)
        self.cfg_path.write_text(
            json.dumps({"http": {"auth": {"mode": "token", "token": "existing-token"}}}),
            encoding="utf-8",
        )

        rc, out, err = self._run(["connect", "chatgpt", "--url", "https://example.com/mcp"])
        self.assertEqual(rc, 0, err)
        self.assertIn("Existing Bearer token", out)
        self.assertIn("existing-token", out)
        self.assertIn("https://example.com/mcp", out)

    def test_connect_chatgpt_force_token_rejected_when_env_token_is_set(self):
        os.environ["ELESYNC_TOKEN"] = "from-env"
        rc, _out, err = self._run(
            ["connect", "chatgpt", "--allowed-host", "demo.ngrok-free.dev", "--force-token"]
        )
        self.assertEqual(rc, 1)
        self.assertIn("Cannot replace token while ELESYNC_TOKEN is set", err)

    def test_connect_chatgpt_shell_quotes_metacharacters(self):
        host = "demo$(touch pwned).ngrok-free.dev"
        rc, out, err = self._run(["connect", "chatgpt", "--allowed-host", host])
        self.assertEqual(rc, 0, err)
        self.assertIn(f"--allowed-host {cli._quote_cli_arg(host)}", out)

    def test_windows_quote_cli_arg_uses_powershell_literal(self):
        with mock.patch.object(cli.os, "name", "nt"):
            self.assertEqual(cli._quote_cli_arg("host&calc"), "'host&calc'")
            self.assertEqual(cli._quote_cli_arg("C:\\O'Neil\\Vault"), "'C:\\O''Neil\\Vault'")

    def test_configured_or_new_token_serializes_generation(self):
        calls = []
        saved_cfg = {}
        first_started = threading.Event()
        second_loaded = threading.Event()

        def fake_load_config():
            if first_started.is_set() and not saved_cfg:
                second_loaded.set()
            return json.loads(json.dumps(saved_cfg))

        def fake_save_config(cfg):
            saved_cfg.clear()
            saved_cfg.update(json.loads(json.dumps(cfg)))

        def fake_generate_token():
            calls.append("generate")
            if len(calls) == 1:
                first_started.set()
                second_loaded.wait(0.2)
                return "token-1"
            return "token-2"

        results = []

        def worker():
            results.append(cli._configured_or_new_token())

        with (
            mock.patch.object(cli, "_load_config", side_effect=fake_load_config),
            mock.patch.object(cli, "_save_config", side_effect=fake_save_config),
            mock.patch("elesync.auth.generate_token", side_effect=fake_generate_token),
        ):
            t1 = threading.Thread(target=worker)
            t2 = threading.Thread(target=worker)
            t1.start()
            t2.start()
            t1.join()
            t2.join()

        self.assertEqual(calls, ["generate"])
        self.assertCountEqual(results, [("token-1", True), ("token-1", False)])
        self.assertEqual(saved_cfg["http"]["auth"]["token"], "token-1")

    def test_connect_chatgpt_rejects_http_url_unless_insecure(self):
        rc, _out, err = self._run(["connect", "chatgpt", "--url", "http://example.test/mcp"])
        self.assertEqual(rc, 1)
        self.assertIn("should use HTTPS", err)

        rc, out, err = self._run(
            ["connect", "chatgpt", "--url", "http://example.test/mcp", "--insecure"]
        )
        self.assertEqual(rc, 0, err)
        self.assertIn("http://example.test/mcp", out)

    def test_remote_connect_clients_share_the_secure_connector_flow(self):
        for client, label in (("grok", "Grok"), ("gemini", "Gemini")):
            with self.subTest(client=client):
                rc, out, err = self._run(
                    ["connect", client, "--allowed-host", f"{client}.example.test"]
                )
                self.assertEqual(rc, 0, err)
                self.assertIn(f"{label} connection setup", out)
                self.assertIn(f"https://{client}.example.test/mcp", out)
                self.assertIn("Authorization: Bearer", out)

    def test_local_connect_clients_print_onboard_commands(self):
        for client, label, target in (
            ("cursor", "Cursor", "cursor"),
            ("claude", "Claude Desktop", "claude-desktop"),
        ):
            with self.subTest(client=client):
                rc, out, err = self._run(["connect", client])
                self.assertEqual(rc, 0, err)
                self.assertIn(f"{label} connection setup", out)
                self.assertIn(f"onboard --target {target}", out)

    def test_connect_claude_uses_the_detected_config_path(self):
        store_config = (
            self.vault
            / "Packages"
            / "Claude_pzs8sxrjxfjjc"
            / "LocalCache"
            / "Roaming"
            / "Claude"
            / "claude_desktop_config.json"
        )
        with mock.patch(
            "elesync.onboarding.default_config_path_for_target", return_value=store_config
        ):
            rc, out, err = self._run(["connect", "claude"])

        self.assertEqual(rc, 0, err)
        self.assertIn(f"--config-path {cli._quote_cli_arg(str(store_config))}", out)


if __name__ == "__main__":
    unittest.main()
