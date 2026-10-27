"""Regression tests for Bearer-token auth on the HTTP/SSE MCP transport.

These exist because the middleware was previously *configured* but never
actually attached to the served Starlette app (build_server tried to reach
it through `mcp.app` / `mcp._app`, neither of which FastMCP ever sets) --
so ELESYNC_TOKEN silently protected nothing. See CHANGELOG [1.9.1].

The assertions here don't try to speak full MCP JSON-RPC; they only need to
prove the middleware itself is on the request path: an unauthenticated
request must never reach routing (always 401, even for a path that doesn't
exist), and a correctly authenticated request must be let through past the
middleware (i.e. must NOT be 401 -- what happens after that is routing's
job, not auth's).
"""

from __future__ import annotations

import os
import tempfile
import unittest
from unittest.mock import patch

try:
    from starlette.testclient import TestClient

    STARLETTE_AVAILABLE = True
except ImportError:  # pragma: no cover
    STARLETTE_AVAILABLE = False

try:
    import mcp as _mcp_pkg  # noqa: F401

    MCP_AVAILABLE = True
except ImportError:  # pragma: no cover
    MCP_AVAILABLE = False


_DEFAULT_TOKEN = object()  # sentinel: "use self.token" vs. explicitly-passed None


@unittest.skipUnless(STARLETTE_AVAILABLE and MCP_AVAILABLE, "mcp SDK not installed")
class TestBearerAuthEnforced(unittest.TestCase):
    def setUp(self):
        self._tmp = tempfile.TemporaryDirectory()
        self.vault_dir = self._tmp.name
        self.token = "s3cr3t-test-token"
        self._built_stores = []

    def tearDown(self):
        # Close every store this test built *before* cleaning up the temp
        # directory. On POSIX this is harmless either way (you can unlink a
        # file while a handle to it is still open), but on Windows an open
        # SQLite connection blocks deletion outright: PermissionError:
        # [WinError 32] "The process cannot access the file because it is
        # being used by another process" on index.db. Closing explicitly
        # here makes this test suite behave identically on both platforms
        # instead of "happening to work" on POSIX by accident.
        for store in self._built_stores:
            store.close()
        self._tmp.cleanup()

    def _build(self, auth_token=_DEFAULT_TOKEN):
        from elesync.mcp_server import build_server

        token = self.token if auth_token is _DEFAULT_TOKEN else auth_token
        mcp = build_server(vault_dir=self.vault_dir, auth_token=token)
        self._built_stores.append(mcp.elesync_store)
        return mcp

    def test_streamable_http_rejects_missing_auth(self):
        mcp = self._build()
        app = mcp.streamable_http_app()
        client = TestClient(app, raise_server_exceptions=False)
        resp = client.get("/definitely-not-a-real-path")
        self.assertEqual(resp.status_code, 401, resp.text)

    def test_streamable_http_rejects_wrong_token(self):
        mcp = self._build()
        app = mcp.streamable_http_app()
        client = TestClient(app, raise_server_exceptions=False)
        resp = client.get(
            "/definitely-not-a-real-path",
            headers={"Authorization": "Bearer wrong-token"},
        )
        self.assertEqual(resp.status_code, 401, resp.text)

    def test_streamable_http_allows_correct_token_past_middleware(self):
        mcp = self._build()
        app = mcp.streamable_http_app()
        client = TestClient(app, raise_server_exceptions=False)
        resp = client.get(
            "/definitely-not-a-real-path",
            headers={"Authorization": f"Bearer {self.token}"},
        )
        # Middleware must let it through -- routing then 404s on the bogus
        # path, which is fine; a 401 here would mean auth is still broken.
        self.assertNotEqual(resp.status_code, 401, resp.text)

    def test_sse_app_also_enforces_auth(self):
        # The bug affected both streamable_http_app() and sse_app() --
        # regression-guard both transports, not just the default one.
        mcp = self._build()
        app = mcp.sse_app()
        client = TestClient(app, raise_server_exceptions=False)
        resp = client.get("/definitely-not-a-real-path")
        self.assertEqual(resp.status_code, 401, resp.text)

    def test_no_token_configured_means_no_middleware_gate(self):
        # Sanity check on the other branch: when no token is configured at
        # all (the common local/stdio case), requests must not be gated --
        # confirms this test suite isn't just checking for permanent 401.
        #
        # auth_token=None makes build_server fall back to resolving a token
        # from ELESYNC_TOKEN and then from the real user config.json. So this
        # test has to isolate both, or it fails on any machine where the user
        # has actually run `ele auth generate-token` -- which is exactly what
        # the #94 remediation tells them to do. An assertion that breaks
        # because the developer followed the docs is worse than no assertion.
        with (
            patch.dict(os.environ, {}, clear=False) as env,
            patch("elesync.cli._load_config", return_value={}),
        ):
            env.pop("ELESYNC_TOKEN", None)
            mcp = self._build(auth_token=None)
            app = mcp.streamable_http_app()
            client = TestClient(app, raise_server_exceptions=False)
            resp = client.get("/definitely-not-a-real-path")
            self.assertNotEqual(resp.status_code, 401, resp.text)

    def test_a_configured_token_gates_even_without_an_explicit_argument(self):
        """The resolution path itself: config token -> middleware active.

        Complements the test above -- together they prove build_server honours
        a resolved token rather than only an explicitly passed one.
        """
        with (
            patch.dict(os.environ, {}, clear=False) as env,
            patch(
                "elesync.cli._load_config",
                return_value={"http": {"auth": {"token": "from-config"}}},
            ),
        ):
            env.pop("ELESYNC_TOKEN", None)
            mcp = self._build(auth_token=None)
            client = TestClient(mcp.streamable_http_app(), raise_server_exceptions=False)
            self.assertEqual(client.get("/nope").status_code, 401)
            ok = client.get("/nope", headers={"Authorization": "Bearer from-config"})
            self.assertNotEqual(ok.status_code, 401, ok.text)


if __name__ == "__main__":
    unittest.main()
