"""Tests for TLS-enabled web server (ele web --tls-cert --tls-key).

Verifies that:
- serve() accepts tls_cert/tls_key parameters
- The TLS socket wrapping logic works (a TLS client can connect)
- The non-loopback warning mentions TLS when it's missing
"""

from __future__ import annotations

import http.client
import socket
import ssl
import tempfile
import threading
import time
import unittest
from unittest.mock import patch

try:
    import cryptography  # noqa: F401

    CRYPTOGRAPHY_AVAILABLE = True
except ImportError:  # pragma: no cover
    CRYPTOGRAPHY_AVAILABLE = False


@unittest.skipUnless(CRYPTOGRAPHY_AVAILABLE, "cryptography not installed")
class TestWebTLSSocket(unittest.TestCase):
    """The TLS socket wrapping produces a server that accepts HTTPS."""

    def setUp(self):
        self._tmp = tempfile.TemporaryDirectory()
        from elesync.auth import setup_mtls

        self.paths = setup_mtls(self._tmp.name)
        self.cert = self.paths["server_cert"]
        self.key = self.paths["server_key"]

    def tearDown(self):
        self._tmp.cleanup()

    def test_tls_wrapped_socket_accepts_https_client(self):
        """A TLS-wrapped ThreadingHTTPServer accepts HTTPS connections."""
        from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer

        # Minimal handler that just returns 200.
        class _Handler(BaseHTTPRequestHandler):
            def do_GET(self):
                self.send_response(200)
                self.send_header("Content-Length", "2")
                self.end_headers()
                self.wfile.write(b"ok")

            def log_message(self, *args, **kwargs):
                pass  # suppress stderr noise

        # Pick an ephemeral port.
        s = socket.socket()
        s.bind(("127.0.0.1", 0))
        port = s.getsockname()[1]
        s.close()

        server = ThreadingHTTPServer(("127.0.0.1", port), _Handler)

        # Replicate the TLS wrapping from web.py serve().
        ctx = ssl.SSLContext(ssl.PROTOCOL_TLS_SERVER)
        ctx.load_cert_chain(self.cert, self.key)
        server.socket = ctx.wrap_socket(server.socket, server_side=True)

        t = threading.Thread(target=server.serve_forever, daemon=True)
        t.start()
        try:
            time.sleep(0.3)

            # Connect with a TLS client that trusts our self-signed cert.
            client_ctx = ssl.create_default_context()
            client_ctx.check_hostname = False
            client_ctx.verify_mode = ssl.CERT_NONE

            conn = http.client.HTTPSConnection("127.0.0.1", port, context=client_ctx, timeout=5)
            conn.request("GET", "/")
            resp = conn.getresponse()
            self.assertEqual(resp.status, 200)
            self.assertEqual(resp.read(), b"ok")
            conn.close()
        finally:
            server.shutdown()
            server.server_close()

    def test_plain_http_fails_on_tls_server(self):
        """A plain HTTP request to a TLS server should fail (not silently
        succeed) — confirms the socket is actually wrapped."""
        from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer

        class _Handler(BaseHTTPRequestHandler):
            def do_GET(self):
                self.send_response(200)
                self.end_headers()

            def log_message(self, *args, **kwargs):
                pass

        s = socket.socket()
        s.bind(("127.0.0.1", 0))
        port = s.getsockname()[1]
        s.close()

        server = ThreadingHTTPServer(("127.0.0.1", port), _Handler)
        ctx = ssl.SSLContext(ssl.PROTOCOL_TLS_SERVER)
        ctx.load_cert_chain(self.cert, self.key)
        server.socket = ctx.wrap_socket(server.socket, server_side=True)

        t = threading.Thread(target=server.serve_forever, daemon=True)
        t.start()
        try:
            time.sleep(0.3)
            # Plain HTTP should get a connection reset or SSL error, not a
            # clean HTTP response.
            with self.assertRaises((ConnectionError, OSError, ssl.SSLError)):
                conn = http.client.HTTPConnection("127.0.0.1", port, timeout=3)
                conn.request("GET", "/")
                conn.getresponse()
                conn.close()
        finally:
            server.shutdown()
            server.server_close()


class TestWebServeSignature(unittest.TestCase):
    """serve() accepts and passes through TLS parameters."""

    def test_serve_accepts_tls_params(self):
        import inspect

        from elesync.web import serve

        sig = inspect.signature(serve)
        self.assertIn("tls_cert", sig.parameters)
        self.assertIn("tls_key", sig.parameters)
        self.assertIn("allow_insecure_network", sig.parameters)
        self.assertIsNone(sig.parameters["tls_cert"].default)
        self.assertIsNone(sig.parameters["tls_key"].default)

    def test_cmd_web_passes_tls_args(self):
        """cmd_web reads tls_cert/tls_key from args and passes to serve()."""
        from elesync.cli import cmd_web

        # cmd_web calls serve() — we mock it to capture the call.
        with patch("elesync.web.serve") as mock_serve:
            mock_serve.return_value = None
            args = type("A", (), {})()
            args.vault = None
            args.host = "127.0.0.1"
            args.port = 7477
            args.no_browser = True
            args.tls_cert = "/path/to/cert.pem"
            args.tls_key = "/path/to/key.pem"
            args.allow_insecure_network = False
            args.web_cmd = None
            cmd_web(args)
            mock_serve.assert_called_once()
            kwargs = mock_serve.call_args.kwargs
            self.assertEqual(kwargs.get("tls_cert"), "/path/to/cert.pem")
            self.assertEqual(kwargs.get("tls_key"), "/path/to/key.pem")
            self.assertFalse(kwargs.get("allow_insecure_network"))

    def test_non_loopback_without_auth_is_refused(self):
        from elesync.web import serve

        with tempfile.TemporaryDirectory() as vault:
            with self.assertRaisesRegex(ValueError, "Refusing to expose"):
                serve(
                    vault_dir=vault,
                    host="0.0.0.0",
                    port=0,
                    open_browser=False,
                )


class TestWebTLSWarning(unittest.TestCase):
    """The non-loopback warning mentions TLS when it's missing."""

    def test_warning_mentions_tls(self):
        from pathlib import Path

        import elesync.web as web_mod

        source = Path(web_mod.__file__).read_text(encoding="utf-8")
        self.assertIn("session cookies travel in cleartext", source)
        self.assertIn("--tls-cert/--tls-key", source)


if __name__ == "__main__":
    unittest.main()
