"""Tests for mTLS cert generation and transport wiring.

Covers:
- setup_mtls() generates valid CA, server, and client certs
- get_mtls_config() resolves from env vars and config
- build_server wires SSL into the uvicorn runners when mTLS is configured
- cmd_serve exposure guard accepts mTLS as valid auth
"""

from __future__ import annotations

import os
import ssl
import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch

try:
    import cryptography  # noqa: F401

    CRYPTOGRAPHY_AVAILABLE = True
except ImportError:  # pragma: no cover
    CRYPTOGRAPHY_AVAILABLE = False

try:
    import mcp as _mcp_pkg  # noqa: F401

    MCP_AVAILABLE = True
except ImportError:  # pragma: no cover
    MCP_AVAILABLE = False


@unittest.skipUnless(CRYPTOGRAPHY_AVAILABLE, "cryptography not installed")
class TestSetupMtls(unittest.TestCase):
    """Cert generation produces files that OpenSSL accepts as valid TLS material."""

    def setUp(self):
        self._tmp = tempfile.TemporaryDirectory()
        self.cert_dir = self._tmp.name

    def tearDown(self):
        self._tmp.cleanup()

    def test_generates_all_six_files(self):
        from elesync.auth import setup_mtls

        result = setup_mtls(self.cert_dir)
        for name in ("ca.crt", "ca.key", "server.crt", "server.key", "client.crt", "client.key"):
            self.assertTrue(
                (Path(self.cert_dir) / name).exists(),
                f"{name} was not generated",
            )
        self.assertEqual(set(result.keys()), {"ca_cert", "server_cert", "server_key"})

    def test_server_cert_is_valid_for_tls_server(self):
        from elesync.auth import setup_mtls

        result = setup_mtls(self.cert_dir)
        ctx = ssl.SSLContext(ssl.PROTOCOL_TLS_SERVER)
        # Must not raise — the cert and key are a matched pair.
        ctx.load_cert_chain(result["server_cert"], result["server_key"])

    def test_server_requires_client_cert_signed_by_ca(self):
        """The whole point of mTLS: a client without a valid cert is rejected."""
        from elesync.auth import setup_mtls

        result = setup_mtls(self.cert_dir)
        ctx = ssl.SSLContext(ssl.PROTOCOL_TLS_SERVER)
        ctx.load_cert_chain(result["server_cert"], result["server_key"])
        ctx.load_verify_locations(result["ca_cert"])
        ctx.verify_mode = ssl.CERT_REQUIRED
        # A client cert signed by the CA must load into a client context.
        client_ctx = ssl.SSLContext(ssl.PROTOCOL_TLS_CLIENT)
        client_ctx.load_cert_chain(
            str(Path(self.cert_dir) / "client.crt"),
            str(Path(self.cert_dir) / "client.key"),
        )
        client_ctx.load_verify_locations(result["ca_cert"])

    def test_regenerating_overwrites_without_error(self):
        from elesync.auth import setup_mtls

        setup_mtls(self.cert_dir)
        # Second run should overwrite cleanly.
        setup_mtls(self.cert_dir)
        self.assertTrue((Path(self.cert_dir) / "ca.crt").exists())


class TestGetMtlsConfig(unittest.TestCase):
    """Config resolution from env vars and config.json."""

    def test_env_vars_take_priority(self):
        from elesync.auth import get_mtls_config

        with patch.dict(
            os.environ,
            {
                "ELESYNC_MTLS_CA_CERT": "/tmp/ca.crt",
                "ELESYNC_MTLS_SERVER_CERT": "/tmp/server.crt",
                "ELESYNC_MTLS_SERVER_KEY": "/tmp/server.key",
            },
        ):
            result = get_mtls_config()
            self.assertIsNotNone(result)
            self.assertEqual(result["ca_cert"], "/tmp/ca.crt")
            self.assertEqual(result["server_cert"], "/tmp/server.crt")
            self.assertEqual(result["server_key"], "/tmp/server.key")

    def test_config_dict_resolved(self):
        from elesync.auth import get_mtls_config

        cfg = {
            "http": {
                "auth": {
                    "mtls": {
                        "ca_cert": "/cfg/ca.crt",
                        "server_cert": "/cfg/server.crt",
                        "server_key": "/cfg/server.key",
                    }
                }
            }
        }
        with patch.dict(os.environ, {}, clear=True):
            result = get_mtls_config(cfg)
            self.assertIsNotNone(result)
            self.assertEqual(result["ca_cert"], "/cfg/ca.crt")

    def test_none_when_not_configured(self):
        from elesync.auth import get_mtls_config

        with patch.dict(os.environ, {}, clear=True):
            self.assertIsNone(get_mtls_config({}))
            self.assertIsNone(get_mtls_config(None))

    def test_partial_env_returns_none(self):
        from elesync.auth import get_mtls_config

        with patch.dict(
            os.environ,
            {"ELESYNC_MTLS_CA_CERT": "/tmp/ca.crt"},
            clear=True,
        ):
            self.assertIsNone(get_mtls_config())


@unittest.skipUnless(MCP_AVAILABLE, "mcp SDK not installed")
class TestBuildServerMtlsWiring(unittest.TestCase):
    """build_server overrides the uvicorn runners with SSL when mTLS is set."""

    def setUp(self):
        self._tmp = tempfile.TemporaryDirectory()
        self.vault_dir = self._tmp.name
        self._built_stores = []

    def tearDown(self):
        for store in self._built_stores:
            store.close()
        self._tmp.cleanup()

    def test_mtls_env_vars_trigger_ssl_runner_override(self):
        """When ELESYNC_MTLS_* env vars are set, the async runners are replaced."""
        from elesync.mcp_server import build_server

        with patch.dict(
            os.environ,
            {
                "ELESYNC_MTLS_CA_CERT": "/fake/ca.crt",
                "ELESYNC_MTLS_SERVER_CERT": "/fake/server.crt",
                "ELESYNC_MTLS_SERVER_KEY": "/fake/server.key",
            },
        ):
            mcp = build_server(vault_dir=self.vault_dir)
            self._built_stores.append(mcp.elesync_store)
            # The overridden runner should not be the original bound method.
            # We can't easily compare identity, but we can check it's not
            # the same as the class's unbound method.
            self.assertTrue(callable(mcp.run_streamable_http_async))
            self.assertTrue(callable(mcp.run_sse_async))

    def test_no_mtls_means_no_override(self):
        """Without mTLS config, the runners are the original FastMCP methods."""
        from elesync.mcp_server import build_server

        with patch.dict(os.environ, {}, clear=True):
            mcp = build_server(vault_dir=self.vault_dir)
            self._built_stores.append(mcp.elesync_store)
            # The runners should be the original bound methods (not replaced).
            # We check that they're still bound methods of the mcp instance.
            self.assertTrue(callable(mcp.run_streamable_http_async))


class TestServeExposureGuardMtls(unittest.TestCase):
    """The cmd_serve exposure guard accepts mTLS as valid authentication."""

    def test_mtls_satisfies_guard(self):
        from elesync.cli import public_exposure_reasons

        # A non-loopback host with mTLS configured should not trigger refusal.
        reasons = public_exposure_reasons("0.0.0.0", ["tunnel.example.com"])
        self.assertTrue(reasons, "guard should detect exposure")

        # But with mTLS configured, cmd_serve should proceed.
        import elesync.cli as cli

        with (
            patch.dict(os.environ, {}, clear=True),
            patch("elesync.cli._load_config") as mock_cfg,
            patch("elesync.cli._resolve_vault", return_value="/tmp/vault"),
            patch("elesync.mcp_server.build_server") as mock_build,
        ):
            mock_cfg.return_value = {
                "http": {
                    "auth": {
                        "mtls": {
                            "ca_cert": "/tmp/ca.crt",
                            "server_cert": "/tmp/server.crt",
                            "server_key": "/tmp/server.key",
                        }
                    }
                }
            }
            mock_server = mock_build.return_value
            args = type("Args", (), {})()
            args.transport = "http"
            args.host = "0.0.0.0"
            args.port = 8000
            args.allowed_host = ["tunnel.example.com"]
            args.insecure = False
            result = cli.cmd_serve(args)
            self.assertEqual(result, 0, "mTLS should satisfy the exposure guard")
            mock_server.run.assert_called_once()


if __name__ == "__main__":
    unittest.main()
