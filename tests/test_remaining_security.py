"""Regression coverage for transport, KDF, credential, and deployment hardening."""

from __future__ import annotations

import argparse
import importlib.util
import io
import json
import os
import tempfile
import types
import unittest
import urllib.request
from contextlib import redirect_stderr
from pathlib import Path
from unittest import mock

from elesync import cli, crypto, embeddings, gemini, mcp_server, summarize, web


class TestRemainingSecurity(unittest.TestCase):
    def test_excessive_or_invalid_kdf_metadata_never_reaches_native_code(self):
        from nacl import pwhash

        salt = b"s" * pwhash.argon2id.SALTBYTES
        invalid = (
            (crypto.MAX_KDF_OPSLIMIT + 1, 64 << 20),
            (2, crypto.MAX_KDF_MEMLIMIT + 1),
            (True, 64 << 20),
            (2, -1),
            ("2", 64 << 20),
        )
        for ops, mem in invalid:
            with (
                self.subTest(ops=ops, mem=mem),
                mock.patch("nacl.pwhash.argon2id.kdf") as native,
                self.assertRaises(ValueError),
            ):
                crypto.derive_key("password", salt, ops, mem)
            native.assert_not_called()

    def test_default_kdf_profile_remains_supported(self):

        with mock.patch("nacl.pwhash.argon2id.kdf", return_value=b"k" * 32) as native:
            key = crypto.derive_key("password", crypto.new_salt(), *crypto.default_limits())
        self.assertEqual(key, b"k" * 32)
        native.assert_called_once()

    def test_invalid_salt_is_rejected_before_native_kdf(self):
        with mock.patch("nacl.pwhash.argon2id.kdf") as native, self.assertRaises(ValueError):
            crypto.derive_key("password", b"bad", 2, 64 << 20)
        native.assert_not_called()

    def test_authenticated_web_binding_still_requires_tls(self):
        with (
            mock.patch.object(web, "_auth_enabled", return_value=True),
            mock.patch.object(web, "BoundedHTTPServer") as listener,
            self.assertRaisesRegex(ValueError, "unencrypted HTTP"),
        ):
            web.serve(host="0.0.0.0", port=0, open_browser=False)
        listener.assert_not_called()

    def test_partial_tls_configuration_cannot_silently_start_http(self):
        with mock.patch.object(web, "BoundedHTTPServer") as listener:
            for args in ({"tls_cert": "cert.pem"}, {"tls_key": "key.pem"}):
                with self.subTest(args=args), self.assertRaisesRegex(ValueError, "Both"):
                    web.serve(open_browser=False, **args)
        listener.assert_not_called()

    def test_direct_mcp_bearer_binding_requires_tls(self):
        args = argparse.Namespace(transport="http", host="0.0.0.0", port=8000, insecure=False)
        with (
            mock.patch.dict(os.environ, {}, clear=True),
            mock.patch.object(
                cli, "_load_config", return_value={"http": {"auth": {"token": "test"}}}
            ),
            mock.patch.object(mcp_server, "build_server") as build,
            redirect_stderr(io.StringIO()),
        ):
            self.assertEqual(cli.cmd_serve(args), cli.EXIT_PRECONDITION)
        build.assert_not_called()

    def test_forwarded_scheme_is_trusted_only_from_loopback(self):
        for peer, expected in (("127.0.0.1", "https"), ("::1", "https"), ("192.0.2.10", None)):
            handler = types.SimpleNamespace(
                client_address=(peer, 1234), headers={"X-Forwarded-Proto": "https"}
            )
            self.assertEqual(web._forwarded_proto(handler), expected)

    def test_semantic_search_does_not_install_packages_implicitly(self):
        with (
            mock.patch("importlib.import_module", side_effect=ImportError),
            mock.patch.object(embeddings, "_try_install_fastembed") as install,
        ):
            self.assertIsNone(embeddings.load_embedder())
        install.assert_not_called()

    def test_missing_authentication_middleware_prevents_startup(self):
        with (
            tempfile.TemporaryDirectory() as vault,
            mock.patch.dict("sys.modules", {"starlette.middleware.base": None}),
            self.assertRaisesRegex(RuntimeError, "authentication middleware"),
        ):
            mcp_server.build_server(vault, auth_token="test")

    def test_provider_credentials_are_not_forwarded_by_redirects(self):
        requests = []

        def response(request, **kwargs):
            requests.append(request)
            result = mock.MagicMock()
            result.__enter__.return_value.read.return_value = json.dumps(
                {
                    "choices": [{"message": {"content": "ok"}}],
                    "content": [{"text": "ok"}],
                    "candidates": [{"content": {"parts": [{"text": "ok"}]}}],
                }
            ).encode()
            return result

        with mock.patch("urllib.request.urlopen", side_effect=response):
            summarize._call_openai("openai-test", "model", "system", "user")
            summarize._call_anthropic("anthropic-test", "model", "system", "user")
            gemini.verify_api_key("gemini-test")
        for request in requests:
            redirected = urllib.request.HTTPRedirectHandler().redirect_request(
                request, None, 302, "Found", {}, "https://other.example/"
            )
            for header in ("Authorization", "X-api-key", "X-goog-api-key"):
                self.assertIsNone(redirected.get_header(header))
            self.assertNotIn("gemini-test", request.full_url)

    def test_configured_mtls_is_not_lost_when_bearer_is_explicit(self):
        config = {
            "http": {
                "auth": {"mtls": {"ca_cert": "ca", "server_cert": "cert", "server_key": "key"}}
            }
        }
        with (
            tempfile.TemporaryDirectory() as vault,
            mock.patch.object(cli, "_load_config", return_value=config),
        ):
            server = mcp_server.build_server(vault, auth_token="test")
            try:
                self.assertEqual(server.run_streamable_http_async.__name__, "_ssl_async")
            finally:
                server.elesync_store.close()


class TestSiteVerification(unittest.TestCase):
    def setUp(self):
        root = Path(__file__).resolve().parent.parent
        spec = importlib.util.spec_from_file_location(
            "verify_site", root / "scripts/verify_site.py"
        )
        self.module = importlib.util.module_from_spec(spec)
        spec.loader.exec_module(self.module)
        self.body = (root / "index.html").read_bytes()
        headers = json.loads((root / "vercel.json").read_text())["headers"][0]["headers"]
        self.headers = "HTTP/2 200\n" + "\n".join(
            item["key"] + ": " + item["value"] for item in headers
        )

    def test_site_must_match_both_source_and_headers(self):
        self.module.verify(self.body, self.headers)
        with self.assertRaisesRegex(ValueError, "HTML"):
            self.module.verify(b"older production page", self.headers)
        with self.assertRaisesRegex(ValueError, "header"):
            self.module.verify(self.body, "HTTP/2 200\n")


if __name__ == "__main__":
    unittest.main()
