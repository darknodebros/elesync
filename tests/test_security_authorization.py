"""Security regressions at command, HTTP approval, and MCP boundaries."""

from __future__ import annotations

import base64
import hashlib
import io
import json
import os
import tempfile
import unittest
from contextlib import redirect_stderr, redirect_stdout
from pathlib import Path
from unittest import mock
from urllib.error import HTTPError
from urllib.parse import parse_qs, urlencode, urlparse
from urllib.request import HTTPRedirectHandler, Request, build_opener

from elesync import cli, mcp_server, oauth
from elesync.project_service import ProjectService
from elesync.store import MemoryStore
from tests.test_web_auth_integration import AuthEnabledWebServerTestCase


class TestStandaloneExposureGuard(unittest.TestCase):
    def test_standalone_command_refuses_public_http_and_sse_without_auth(self):
        for transport in ("http", "sse"):
            for exposure in (["--host", "0.0.0.0"], ["--allowed-host", "public.example"]):
                with (
                    self.subTest(transport=transport, exposure=exposure),
                    mock.patch.dict(os.environ, {}, clear=True),
                    mock.patch.object(cli, "_load_config", return_value={}),
                    mock.patch.object(mcp_server, "build_server") as build,
                    mock.patch("sys.argv", ["elesync", "--transport", transport, *exposure]),
                    redirect_stderr(io.StringIO()),
                    self.assertRaises(SystemExit) as stopped,
                ):
                    mcp_server.main()
                self.assertEqual(stopped.exception.code, 2)
                build.assert_not_called()

    def test_environment_tunnel_hostname_is_also_guarded(self):
        with (
            mock.patch.dict(
                os.environ,
                {"ELESYNC_TRANSPORT": "http", "ELESYNC_ALLOWED_HOSTS": "public.example"},
                clear=True,
            ),
            mock.patch.object(cli, "_load_config", return_value={}),
            mock.patch.object(mcp_server, "build_server") as build,
            mock.patch("sys.argv", ["elesync"]),
            redirect_stderr(io.StringIO()),
            self.assertRaises(SystemExit) as stopped,
        ):
            mcp_server.main()
        self.assertEqual(stopped.exception.code, 2)
        build.assert_not_called()

    def test_authenticated_network_and_private_transports_still_start(self):
        cases = [
            (
                ["--transport", "http", "--allowed-host", "public.example"],
                {"ELESYNC_TOKEN": "test"},
            ),
            (["--transport", "sse", "--allowed-host", "public.example"], {"ELESYNC_TOKEN": "test"}),
            (["--transport", "http"], {}),
            (["--transport", "stdio"], {}),
        ]
        for args, env in cases:
            with (
                self.subTest(args=args),
                mock.patch.dict(os.environ, env, clear=True),
                mock.patch.object(cli, "_load_config", return_value={}),
                mock.patch.object(mcp_server, "build_server") as build,
                mock.patch("sys.argv", ["elesync", *args]),
                redirect_stdout(io.StringIO()),
            ):
                mcp_server.main()
            build.return_value.run.assert_called_once()


class _NoRedirect(HTTPRedirectHandler):
    def redirect_request(self, req, fp, code, msg, headers, newurl):
        return None


class TestOAuthApprovalRoles(AuthEnabledWebServerTestCase):
    def setUp(self):
        from elesync import web

        self.project = ProjectService(web._state.store).create("OAuth role regression")
        web._state.store.set_project_access(self.project["id"], "chatgpt", True, True, True)
        self.client = oauth.register_client(
            web._state.vault_dir, "Audit client", ["https://client.example/callback"]
        )
        self.verifier = "regression-verifier-with-enough-entropy"
        self.fields = {
            "client_id": self.client["client_id"],
            "redirect_uri": "https://client.example/callback",
            "response_type": "code",
            "code_challenge_method": "S256",
            "code_challenge": oauth._pkce_challenge(self.verifier),
            "project_id": self.project["id"],
            "provider": "chatgpt",
        }

    def _request(self, username, method, *, valid_csrf=True):
        from elesync import web

        token = web._state.session_mgr.create_session(username)
        fields = {**self.fields, "csrf_token": web._csrf_token_for(token) if valid_csrf else "bad"}
        url = f"http://127.0.0.1:{self.port}/oauth/authorize"
        body = urlencode(fields).encode() if method == "POST" else None
        if method == "GET":
            url += "?" + urlencode(fields)
        request = Request(
            url,
            data=body,
            method=method,
            headers={
                "Cookie": f"{web.SESSION_COOKIE_NAME}={token}",
                "Content-Type": "application/x-www-form-urlencoded",
            },
        )
        try:
            response = build_opener(_NoRedirect).open(request, timeout=5)
        except HTTPError as error:
            response = error
        with response:
            return response.code, response.headers, response.read()

    def test_viewer_cannot_view_or_submit_approval_even_with_valid_csrf(self):
        from elesync import web

        before = oauth._load(web._state.vault_dir)
        for method in ("GET", "POST"):
            with self.subTest(method=method):
                status, _, _ = self._request(self.VIEWER_USER, method)
                self.assertEqual(status, 403)
        self.assertEqual(oauth._load(web._state.vault_dir), before)

    def test_guest_cannot_approve_existing_write_grant(self):
        from elesync import web

        web._state.user_mgr.add_user("guest", "guestpass123", guest=True)
        self.assertEqual(self._request("guest", "POST")[0], 403)

    def test_admin_approval_still_requires_csrf_and_issues_project_token(self):
        from elesync import web

        self.assertEqual(self._request(self.ADMIN_USER, "GET")[0], 200)
        self.assertEqual(self._request(self.ADMIN_USER, "POST", valid_csrf=False)[0], 403)
        status, headers, _ = self._request(self.ADMIN_USER, "POST")
        self.assertEqual(status, 302)
        code = parse_qs(urlparse(headers["Location"]).query)["code"][0]
        issued = oauth.exchange_code(
            web._state.vault_dir,
            code,
            self.client["client_id"],
            self.fields["redirect_uri"],
            self.verifier,
        )
        access = oauth.validate_token(web._state.vault_dir, issued["access_token"])
        self.assertEqual(access["project_id"], self.project["id"])
        self.assertTrue(access["permissions"]["can_write"])


class TestOAuthGrantReductions(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.addCleanup(self.temp.cleanup)
        self.vault = Path(self.temp.name)
        self.store = MemoryStore(self.vault)
        self.addCleanup(self.store.close)
        self.projects = ProjectService(self.store)
        self.project = self.projects.create("Scoped project")
        self.other = self.projects.create("Other project")
        self.projects.set_access(self.project["id"], "chatgpt", True, True, True)
        self.client = oauth.register_client(self.vault, "Client", ["https://client.example/cb"])
        self.verifier = "grant-reduction-verifier"
        self.pending = self._code()
        self.issued = self._exchange(self._code())

    def _code(self, *, project=None, provider="chatgpt", can_write=True):
        challenge = base64.urlsafe_b64encode(hashlib.sha256(self.verifier.encode()).digest())
        return oauth.create_code(
            self.vault,
            self.client["client_id"],
            "https://client.example/cb",
            challenge.rstrip(b"=").decode(),
            project or self.project["id"],
            {"can_read": True, "can_write": can_write, "can_read_conversations": True},
            provider=provider,
        )

    def _exchange(self, code):
        return oauth.exchange_code(
            self.vault, code, self.client["client_id"], "https://client.example/cb", self.verifier
        )

    def _permissions(self, issued):
        return oauth.validate_token(self.vault, issued["access_token"])["permissions"]

    def test_reductions_cover_access_refresh_pending_codes_and_do_not_expand_again(self):
        self.projects.set_access(self.project["id"], "chatgpt", True, False, False)
        reduced = {"can_read": True, "can_write": False, "can_read_conversations": False}
        self.assertEqual(self._permissions(self.issued), reduced)
        pending = self._exchange(self.pending)
        self.assertEqual(self._permissions(pending), reduced)
        self.projects.set_access(self.project["id"], "chatgpt", True, True, True)
        refreshed = oauth.refresh_access_token(
            self.vault, self.issued["refresh_token"], self.client["client_id"]
        )
        self.assertEqual(self._permissions(refreshed), reduced)
        self.assertEqual(self._permissions(self.issued), reduced)

    def test_read_reduction_does_not_affect_other_projects_or_providers(self):
        other_project = self._exchange(self._code(project=self.other["id"]))
        other_provider = self._exchange(self._code(provider="claude"))
        self.store.set_project_access(self.project["id"], "chatgpt", False, False, False)
        self.assertFalse(any(self._permissions(self.issued).values()))
        self.assertTrue(all(self._permissions(other_project).values()))
        self.assertTrue(all(self._permissions(other_provider).values()))

    def test_storage_failure_does_not_report_a_successful_grant_reduction(self):
        with (
            mock.patch("elesync.oauth._save_unlocked", side_effect=OSError("disk full")),
            self.assertRaises(OSError),
        ):
            self.projects.set_access(self.project["id"], "chatgpt", True, False, False)
        self.assertTrue(self.store.project_access(self.project["id"])[0]["can_write"])

    def test_mcp_session_checks_live_grants_and_never_expands_token_permissions(self):
        from starlette.testclient import TestClient

        with mock.patch.dict(os.environ, {}, clear=True):
            server = mcp_server.build_server(str(self.vault), auth_token="owner-test-token")
        self.addCleanup(server.elesync_store.close)
        with TestClient(server.streamable_http_app(), base_url="http://127.0.0.1:8000") as client:
            headers = {
                "Authorization": "Bearer " + self.issued["access_token"],
                "Accept": "application/json, text/event-stream",
            }

            def rpc(method, params, request_id=1):
                response = client.post(
                    "/mcp",
                    headers=headers,
                    json={"jsonrpc": "2.0", "id": request_id, "method": method, "params": params},
                )
                self.assertEqual(response.status_code, 200, response.text)
                if response.headers.get("mcp-session-id"):
                    headers["mcp-session-id"] = response.headers["mcp-session-id"]
                if response.headers.get("content-type", "").startswith("text/event-stream"):
                    return json.loads(
                        next(
                            line[6:]
                            for line in response.text.splitlines()
                            if line.startswith("data: ")
                        )
                    )
                return response.json()

            rpc(
                "initialize",
                {
                    "protocolVersion": "2025-03-26",
                    "capabilities": {},
                    "clientInfo": {"name": "regression", "version": "1"},
                },
            )
            response = rpc(
                "tools/call",
                {"name": "remember", "arguments": {"content": "allowed before reduction"}},
                2,
            )
            self.assertFalse(response["result"].get("isError"), response)
            # Bypass the normal saved-token reduction to exercise the per-call
            # live check independently, on an already established MCP session.
            with mock.patch("elesync.oauth.restrict_project_authorizations"):
                self.projects.set_access(self.project["id"], "chatgpt", True, False, False)
            response = rpc(
                "tools/call", {"name": "remember", "arguments": {"content": "must be denied"}}, 3
            )
            self.assertIn("read-only access", response["result"]["content"][0]["text"])
            self.assertNotIn("must be denied", [item.content for item in self.store.all()])
            # A later grant increase cannot widen an older delegation.
            self.projects.set_access(self.project["id"], "chatgpt", True, False, False)
            self.projects.set_access(self.project["id"], "chatgpt", True, True, True)
            response = rpc(
                "tools/call",
                {"name": "remember", "arguments": {"content": "still denied after increase"}},
                4,
            )
            self.assertIn("read-only access", response["result"]["content"][0]["text"])
            self.assertNotIn(
                "still denied after increase", [item.content for item in self.store.all()]
            )
            # Even a different valid header must not revive an established
            # session whose original OAuth authorization has been revoked.
            oauth.revoke_token(self.vault, self.issued["access_token"])
            headers["Authorization"] = "Bearer owner-test-token"
            response = rpc("tools/call", {"name": "memory_status", "arguments": {}}, 5)
            self.assertTrue(response["result"].get("isError"), response)
            self.assertIn("revoked", response["result"]["content"][0]["text"])


if __name__ == "__main__":
    unittest.main()
