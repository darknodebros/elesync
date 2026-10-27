"""Tests for remote MCP transport support (--transport http/sse, --allowed-host).

These cover the build_server() wiring added for ChatGPT Developer Mode /
other remote MCP clients that need Streamable HTTP instead of stdio.
Verified manually end-to-end against a real ngrok tunnel + ChatGPT connector
(see CHANGELOG); these tests lock in the wiring so it doesn't regress.

Note on temp-dir cleanup: build_server() opens a MemoryStore, which keeps a
live sqlite3 connection for the lifetime of the returned server object (by
design -- the MCP server runs for the life of the process). On Windows this
means the index.db file handle is still open when cleanup runs, raising
PermissionError: [WinError 32] (POSIX tolerates deleting a file that still
has an open handle; Windows does not). build_server() exposes the store it
opened as `.elesync_store` specifically so callers like this mixin can close
it deterministically before cleanup, rather than papering over the symptom
with rmtree(ignore_errors=True) -- which "works" but silently leaves orphaned
temp directories behind on every Windows test run.
"""

from __future__ import annotations

import tempfile
import unittest

try:
    from mcp.server.fastmcp import FastMCP as _FastMCP  # noqa: F401

    _HAS_MCP_SDK = True
except ImportError:
    _HAS_MCP_SDK = False

_skip_without_mcp_sdk = unittest.skipUnless(
    _HAS_MCP_SDK,
    'requires the mcp SDK (pip install "mcp[cli]>=1.2.0,<2")',
)


class _TempVaultMixin:
    """Creates a temp vault dir and tracks every server built via self._build()
    so tearDown can close its MemoryStore before the directory is removed."""

    def setUp(self):
        self._tmp = tempfile.TemporaryDirectory()
        self.vault_dir = self._tmp.name
        self._built_servers = []

    def tearDown(self):
        for server in self._built_servers:
            store = getattr(server, "elesync_store", None)
            if store is not None:
                store.close()
        self._tmp.cleanup()

    def _build(self, **kwargs):
        from elesync import mcp_server

        server = mcp_server.build_server(self.vault_dir, **kwargs)
        self._built_servers.append(server)
        return server


@_skip_without_mcp_sdk
class TestBuildServerTransportDefaults(_TempVaultMixin, unittest.TestCase):
    def test_default_host_port_unchanged(self):
        """No args -> same stdio-friendly defaults as before this feature existed."""
        server = self._build()
        self.assertEqual(server.settings.host, "127.0.0.1")
        self.assertEqual(server.settings.port, 8000)

    def test_custom_host_port_applied(self):
        server = self._build(host="0.0.0.0", port=9999)
        self.assertEqual(server.settings.host, "0.0.0.0")
        self.assertEqual(server.settings.port, 9999)

    def test_no_allowed_hosts_leaves_sdk_default_protection(self):
        """Without --allowed-host, we don't touch transport_security at all --
        the SDK's own localhost-only DNS-rebinding default still applies."""
        server = self._build()
        ts = server.settings.transport_security
        if ts is not None:
            self.assertTrue(ts.enable_dns_rebinding_protection)


@_skip_without_mcp_sdk
class TestBuildServerAllowedHosts(_TempVaultMixin, unittest.TestCase):
    def test_allowed_hosts_extends_not_replaces_localhost(self):
        """Passing allowed_hosts keeps localhost/127.0.0.1 trusted AND adds
        the new host -- doesn't lock out local testing."""
        server = self._build(host="127.0.0.1", port=8000, allowed_hosts=["xxxx.ngrok-free.dev"])
        ts = server.settings.transport_security
        self.assertIsNotNone(ts)
        self.assertTrue(ts.enable_dns_rebinding_protection)
        self.assertIn("127.0.0.1:*", ts.allowed_hosts)
        self.assertIn("localhost:*", ts.allowed_hosts)
        self.assertIn("xxxx.ngrok-free.dev", ts.allowed_hosts)
        self.assertIn("xxxx.ngrok-free.dev:*", ts.allowed_hosts)

    def test_allowed_origins_cover_http_and_https(self):
        server = self._build(allowed_hosts=["example.ngrok-free.dev"])
        origins = server.settings.transport_security.allowed_origins
        self.assertIn("https://example.ngrok-free.dev", origins)
        self.assertIn("https://example.ngrok-free.dev:*", origins)
        self.assertIn("http://example.ngrok-free.dev", origins)

    def test_multiple_allowed_hosts(self):
        server = self._build(allowed_hosts=["one.ngrok-free.dev", "two.example.com"])
        hosts = server.settings.transport_security.allowed_hosts
        self.assertIn("one.ngrok-free.dev", hosts)
        self.assertIn("two.example.com", hosts)

    def test_empty_allowed_hosts_list_behaves_like_none(self):
        """An empty list (e.g. from an unset env var split) shouldn't blow up
        or force transport_security to be set."""
        server = self._build(allowed_hosts=[])
        ts = server.settings.transport_security
        if ts is not None:
            self.assertTrue(ts.enable_dns_rebinding_protection)


class TestServeCliTransportFlags(unittest.TestCase):
    """`ele serve --transport ... --allowed-host ...` argparse wiring.

    No vault/store involved here, so no temp-dir cleanup concerns.
    """

    def _build_parser(self):
        from elesync.cli import build_parser

        return build_parser()

    def test_serve_default_transport_is_stdio(self):
        parser = self._build_parser()
        args = parser.parse_args(["serve"])
        self.assertEqual(args.transport, "stdio")
        self.assertEqual(args.host, "127.0.0.1")
        self.assertEqual(args.port, 8000)
        self.assertIsNone(args.allowed_host)

    def test_serve_accepts_http_transport_and_allowed_host(self):
        parser = self._build_parser()
        args = parser.parse_args(
            [
                "serve",
                "--transport",
                "http",
                "--port",
                "9000",
                "--allowed-host",
                "xxxx.ngrok-free.dev",
            ]
        )
        self.assertEqual(args.transport, "http")
        self.assertEqual(args.port, 9000)
        self.assertEqual(args.allowed_host, ["xxxx.ngrok-free.dev"])

    def test_serve_allowed_host_is_repeatable(self):
        parser = self._build_parser()
        args = parser.parse_args(
            [
                "serve",
                "--allowed-host",
                "one.example.com",
                "--allowed-host",
                "two.example.com",
            ]
        )
        self.assertEqual(args.allowed_host, ["one.example.com", "two.example.com"])

    def test_serve_rejects_unknown_transport(self):
        parser = self._build_parser()
        with self.assertRaises(SystemExit):
            parser.parse_args(["serve", "--transport", "carrier-pigeon"])


if __name__ == "__main__":  # pragma: no cover
    unittest.main()
