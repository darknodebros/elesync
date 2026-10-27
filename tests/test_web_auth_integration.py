"""Integration tests for auth-gated behavior in elesync.web.

Follows the same pattern as test_web.py (real HTTP requests via urllib
against a background-threaded server), but with a vault that has users
configured, so the auth-required code paths actually get exercised. The
plain test_web.py suite continues to cover the no-auth-configured default
path unchanged.
"""

from __future__ import annotations

import http.cookiejar
import json
import tempfile
import threading
import unittest
import unittest.mock as mock
import urllib.request
from http.server import HTTPServer
from pathlib import Path
from urllib.error import HTTPError
from urllib.request import Request, urlopen


def _url(port: int, path: str) -> str:
    return f"http://127.0.0.1:{port}{path}"


def _get(port: int, path: str, opener=None) -> tuple[int, dict]:
    open_fn = opener.open if opener else urlopen
    try:
        with open_fn(_url(port, path), timeout=5) as r:
            return r.status, json.loads(r.read())
    except HTTPError as e:
        return e.code, json.loads(e.read())


def _post(port: int, path: str, body: dict, opener=None, headers=None) -> tuple[int, dict, object]:
    data = json.dumps(body).encode()
    request_headers = {"Content-Type": "application/json"}
    request_headers.update(headers or {})
    req = Request(_url(port, path), data=data, headers=request_headers, method="POST")
    open_fn = opener.open if opener else urlopen
    try:
        with open_fn(req, timeout=5) as r:
            return r.status, json.loads(r.read()), r.headers
    except HTTPError as e:
        return e.code, json.loads(e.read()), e.headers


def _put(port: int, path: str, body: dict, opener=None) -> tuple[int, dict]:
    data = json.dumps(body).encode()
    req = Request(
        _url(port, path), data=data, headers={"Content-Type": "application/json"}, method="PUT"
    )
    open_fn = opener.open if opener else urlopen
    try:
        with open_fn(req, timeout=5) as r:
            return r.status, json.loads(r.read())
    except HTTPError as e:
        return e.code, json.loads(e.read())


def _delete(port: int, path: str, opener=None) -> tuple[int, dict]:
    req = Request(_url(port, path), method="DELETE")
    open_fn = opener.open if opener else urlopen
    try:
        with open_fn(req, timeout=5) as r:
            return r.status, json.loads(r.read())
    except HTTPError as e:
        return e.code, json.loads(e.read())


def _cookie_opener() -> urllib.request.OpenerDirector:
    """An opener that remembers cookies across requests, like a browser -
    used to simulate a logged-in session across multiple calls."""
    jar = http.cookiejar.CookieJar()
    return urllib.request.build_opener(urllib.request.HTTPCookieProcessor(jar))


def _bcrypt_available() -> bool:
    try:
        import bcrypt  # noqa: F401

        return True
    except ImportError:
        return False


# setUpClass creates real users via add_user(), which hashes passwords, so
# these classes need bcrypt. unittest propagates a class-level skip to
# subclasses, so guarding the base covers all of them.
@unittest.skipUnless(_bcrypt_available(), "bcrypt not installed")
class AuthEnabledWebServerTestCase(unittest.TestCase):
    """Base: a vault with an admin and a viewer user configured, so every
    request in these tests goes through the auth-required code path."""

    port: int = 0
    server: HTTPServer | None = None
    thread: threading.Thread | None = None

    ADMIN_USER = "admin1"
    ADMIN_PASS = "adminpass123"
    VIEWER_USER = "viewer1"
    VIEWER_PASS = "viewerpass123"

    @classmethod
    def setUpClass(cls):
        import elesync.web as web_mod
        import elesync.web_auth as web_auth_mod
        from elesync.store import MemoryStore

        cls.vault_tmp = tempfile.TemporaryDirectory()
        vault_path = Path(cls.vault_tmp.name)
        web_mod._state.store = MemoryStore(vault_path)
        web_mod._state.vault_dir = str(vault_path)

        auth_dir = web_mod.web_auth_dir(vault_path)
        web_mod._state.user_mgr = web_auth_mod.UserManager(auth_dir)
        web_mod._state.session_mgr = web_auth_mod.SessionManager(
            auth_dir, require_existing_user=True
        )
        web_mod._state.user_mgr.add_user(cls.ADMIN_USER, cls.ADMIN_PASS, role="admin")
        web_mod._state.user_mgr.add_user(cls.VIEWER_USER, cls.VIEWER_PASS, role="viewer")

        cls.server = HTTPServer(("127.0.0.1", 0), web_mod._Handler)
        cls.port = cls.server.server_address[1]
        cls.thread = threading.Thread(target=cls.server.serve_forever, daemon=True)
        cls.thread.start()

    @classmethod
    def tearDownClass(cls):
        cls.server.shutdown()
        cls.server.server_close()
        import elesync.web as web_mod

        if web_mod._state.store:
            web_mod._state.store.close()
        web_mod._state.user_mgr = None
        web_mod._state.session_mgr = None
        cls.vault_tmp.cleanup()

    def _logged_in_opener(self, username: str, password: str):
        """Log in and return an opener that carries the resulting session
        cookie on every subsequent request, like a real browser tab."""
        opener = _cookie_opener()
        status, data, _headers = _post(
            self.port, "/api/login", {"username": username, "password": password}, opener=opener
        )
        assert status == 200, f"login failed in test setup: {status} {data}"
        return opener


# ---------------------------------------------------------------------------
# Unauthenticated access - everything mutating/reading data should 401
# ---------------------------------------------------------------------------


class TestUnauthenticatedAccess(AuthEnabledWebServerTestCase):
    def test_stats_requires_auth(self):
        code, _ = _get(self.port, "/api/stats")
        self.assertEqual(code, 401)

    def test_memories_list_requires_auth(self):
        code, _ = _get(self.port, "/api/memories")
        self.assertEqual(code, 401)

    def test_add_memory_requires_auth(self):
        code, _, _headers = _post(self.port, "/api/memories", {"content": "x"})
        self.assertEqual(code, 401)

    def test_update_requires_auth(self):
        code, _ = _put(self.port, "/api/memories/whatever", {"content": "x"})
        self.assertEqual(code, 401)

    def test_delete_requires_auth(self):
        code, _ = _delete(self.port, "/api/memories/whatever")
        self.assertEqual(code, 401)

    def test_root_redirects_to_login(self):
        req = Request(_url(self.port, "/"))

        class NoRedirect(urllib.request.HTTPRedirectHandler):
            def redirect_request(self, *args, **kwargs):
                return None

        opener = urllib.request.build_opener(NoRedirect)
        try:
            with opener.open(req, timeout=5) as r:
                status, location = r.status, r.headers.get("Location")
        except HTTPError as e:
            # urllib raises HTTPError(302) when redirect_request() opts out
            # of following it - the redirect itself is still correct, this
            # is just how urllib surfaces "I chose not to follow it".
            status, location = e.code, e.headers.get("Location")
        self.assertEqual(status, 302)
        self.assertEqual(location, "/login")

    def test_login_page_itself_does_not_require_auth(self):
        with urlopen(_url(self.port, "/login"), timeout=5) as r:
            self.assertEqual(r.status, 200)
            self.assertIn("Sign in", r.read().decode())

    def test_login_page_follows_the_dashboard_theme_without_auth(self):
        with urlopen(_url(self.port, "/login"), timeout=5) as r:
            html = r.read().decode()
        self.assertIn('<script src="/static/dashboard-theme.js"></script>', html)
        for path in ("/static/dashboard-theme.js", "/static/elesync-favicon-cosmic.png"):
            with urlopen(_url(self.port, path), timeout=5) as r:
                self.assertEqual(r.status, 200, path)

    def test_me_endpoint_reports_unauthenticated(self):
        code, data = _get(self.port, "/api/me")
        self.assertEqual(code, 200)
        self.assertFalse(data["authenticated"])
        self.assertTrue(data["auth_required"])

    def test_wrong_password_rejected(self):
        code, data, _headers = _post(
            self.port, "/api/login", {"username": self.ADMIN_USER, "password": "wrongpass"}
        )
        self.assertEqual(code, 401)

    def test_unknown_username_rejected(self):
        code, data, _headers = _post(
            self.port, "/api/login", {"username": "ghost", "password": "whatever123"}
        )
        self.assertEqual(code, 401)


# ---------------------------------------------------------------------------
# Authenticated admin - full read/write access
# ---------------------------------------------------------------------------


class TestAuthenticatedAdminAccess(AuthEnabledWebServerTestCase):
    def test_login_sets_cookie_and_succeeds(self):
        opener = _cookie_opener()
        status, data, headers = _post(
            self.port,
            "/api/login",
            {"username": self.ADMIN_USER, "password": self.ADMIN_PASS},
            opener=opener,
        )
        self.assertEqual(status, 200)
        self.assertTrue(data["ok"])
        self.assertEqual(data["role"], "admin")
        self.assertIn("Set-Cookie", dict(headers.items()))

    def test_tls_login_cookie_is_secure(self):
        import elesync.web as web_mod

        web_mod._state.tls_enabled = True
        try:
            status, _data, headers = _post(
                self.port,
                "/api/login",
                {"username": self.ADMIN_USER, "password": self.ADMIN_PASS},
            )
        finally:
            web_mod._state.tls_enabled = False
        self.assertEqual(status, 200)
        self.assertIn("Secure", headers["Set-Cookie"])

    def test_forwarded_https_login_cookie_is_secure(self):
        status, _data, headers = _post(
            self.port,
            "/api/login",
            {"username": self.ADMIN_USER, "password": self.ADMIN_PASS},
            headers={"X-Forwarded-Proto": "https"},
        )
        self.assertEqual(status, 200)
        self.assertIn("Secure", headers["Set-Cookie"])

    def test_authenticated_read_succeeds(self):
        opener = self._logged_in_opener(self.ADMIN_USER, self.ADMIN_PASS)
        code, data = _get(self.port, "/api/stats", opener=opener)
        self.assertEqual(code, 200)
        self.assertIn("total", data)

    def test_network_info_reports_dashboard_authentication(self):
        opener = self._logged_in_opener(self.ADMIN_USER, self.ADMIN_PASS)
        code, data = _get(self.port, "/api/network-info", opener=opener)
        self.assertEqual(code, 200)
        self.assertTrue(data["auth_enabled"])

    def test_authenticated_write_succeeds(self):
        opener = self._logged_in_opener(self.ADMIN_USER, self.ADMIN_PASS)
        code, data, _headers = _post(
            self.port,
            "/api/memories",
            {"content": "admin-added memory", "source": "manual", "type": "note"},
            opener=opener,
        )
        self.assertEqual(code, 201)
        self.assertEqual(data["content"], "admin-added memory")

    def test_authenticated_admin_can_start_pairing_tunnel(self):
        import elesync.web as web_mod

        opener = self._logged_in_opener(self.ADMIN_USER, self.ADMIN_PASS)
        original_manager = web_mod._state.tunnel_manager
        manager = mock.Mock()
        manager.start.return_value = True
        manager.status.return_value = {"active": True, "url": "https://pair.example", "error": None}
        web_mod._state.tunnel_manager = manager
        try:
            code, data, _headers = _post(self.port, "/api/tunnel/start", {}, opener=opener)
        finally:
            web_mod._state.tunnel_manager = original_manager

        self.assertEqual(code, 200)
        self.assertTrue(data["success"])
        manager.start.assert_called_once_with(web_mod._state.port)

    def test_me_endpoint_reports_authenticated_admin(self):
        opener = self._logged_in_opener(self.ADMIN_USER, self.ADMIN_PASS)
        code, data = _get(self.port, "/api/me", opener=opener)
        self.assertEqual(code, 200)
        self.assertTrue(data["authenticated"])
        self.assertEqual(data["username"], self.ADMIN_USER)
        self.assertEqual(data["role"], "admin")

    def test_logout_invalidates_session(self):
        opener = self._logged_in_opener(self.ADMIN_USER, self.ADMIN_PASS)
        code, _ = _get(self.port, "/api/stats", opener=opener)
        self.assertEqual(code, 200)

        status, data, _headers = _post(self.port, "/api/logout", {}, opener=opener)
        self.assertEqual(status, 200)

        code, _ = _get(self.port, "/api/stats", opener=opener)
        self.assertEqual(code, 401)

    def test_invalid_cookie_value_is_rejected(self):
        # Seed a garbage cookie directly rather than logging in.
        req = Request(_url(self.port, "/api/stats"))
        req.add_header("Cookie", "ele_session=not-a-real-token")
        try:
            with urlopen(req, timeout=5) as r:
                status = r.status
        except HTTPError as e:
            status = e.code
        self.assertEqual(status, 401)


# ---------------------------------------------------------------------------
# Authenticated viewer - read-only, writes must 403
# ---------------------------------------------------------------------------


class TestAuthenticatedViewerAccess(AuthEnabledWebServerTestCase):
    def test_viewer_can_read(self):
        opener = self._logged_in_opener(self.VIEWER_USER, self.VIEWER_PASS)
        code, data = _get(self.port, "/api/stats", opener=opener)
        self.assertEqual(code, 200)

    def test_viewer_cannot_add_memory(self):
        opener = self._logged_in_opener(self.VIEWER_USER, self.VIEWER_PASS)
        code, _data, _headers = _post(
            self.port,
            "/api/memories",
            {"content": "viewer should not be able to add this"},
            opener=opener,
        )
        self.assertEqual(code, 403)

    def test_viewer_cannot_update_memory(self):
        opener = self._logged_in_opener(self.VIEWER_USER, self.VIEWER_PASS)
        code, _data = _put(self.port, "/api/memories/whatever", {"content": "x"}, opener=opener)
        self.assertEqual(code, 403)

    def test_viewer_put_with_large_body_gets_clean_403(self):
        """Regression for WinError 10053: the 403 gate fires before the
        request body is read, and closing the connection with unread
        data makes Windows clients see a connection abort instead of
        the status code. A large body maximizes that race; the server
        must drain it before responding."""
        opener = self._logged_in_opener(self.VIEWER_USER, self.VIEWER_PASS)
        big = {"content": "x" * 262144}  # 256 KiB, well under the 1 MiB drain cap
        code, _data = _put(self.port, "/api/memories/whatever", big, opener=opener)
        self.assertEqual(code, 403)

    def test_viewer_cannot_delete_memory(self):
        opener = self._logged_in_opener(self.VIEWER_USER, self.VIEWER_PASS)
        code, _data = _delete(self.port, "/api/memories/whatever", opener=opener)
        self.assertEqual(code, 403)

    def test_me_endpoint_reports_viewer_role(self):
        opener = self._logged_in_opener(self.VIEWER_USER, self.VIEWER_PASS)
        code, data = _get(self.port, "/api/me", opener=opener)
        self.assertEqual(code, 200)
        self.assertEqual(data["role"], "viewer")

    def test_viewer_write_attempt_does_not_actually_write(self):
        # Belt-and-suspenders: confirm the 403 isn't just a status code
        # while the write silently went through anyway.
        admin_opener = self._logged_in_opener(self.ADMIN_USER, self.ADMIN_PASS)
        _, before = _get(self.port, "/api/stats", opener=admin_opener)

        viewer_opener = self._logged_in_opener(self.VIEWER_USER, self.VIEWER_PASS)
        _post(self.port, "/api/memories", {"content": "should be rejected"}, opener=viewer_opener)

        _, after = _get(self.port, "/api/stats", opener=admin_opener)
        self.assertEqual(before["total"], after["total"])


# ---------------------------------------------------------------------------
# No-auth-configured vault sharing the same server code - confirms the two
# modes don't bleed into each other within a single test run.
# ---------------------------------------------------------------------------


class TestNoAuthConfiguredStillOpen(unittest.TestCase):
    """A separate vault with zero users - server should behave exactly as
    it did before web_auth existed, matching test_web.py's assumptions."""

    @classmethod
    def setUpClass(cls):
        import elesync.web as web_mod
        import elesync.web_auth as web_auth_mod
        from elesync.store import MemoryStore

        cls.vault_tmp = tempfile.TemporaryDirectory()
        vault_path = Path(cls.vault_tmp.name)
        web_mod._state.store = MemoryStore(vault_path)
        web_mod._state.vault_dir = str(vault_path)
        # UserManager with zero users configured - _auth_enabled() must be False.
        web_mod._state.user_mgr = web_auth_mod.UserManager(web_mod.web_auth_dir(vault_path))
        web_mod._state.session_mgr = web_auth_mod.SessionManager(
            web_mod.web_auth_dir(vault_path), require_existing_user=True
        )

        cls.server = HTTPServer(("127.0.0.1", 0), web_mod._Handler)
        cls.port = cls.server.server_address[1]
        cls.thread = threading.Thread(target=cls.server.serve_forever, daemon=True)
        cls.thread.start()

    @classmethod
    def tearDownClass(cls):
        cls.server.shutdown()
        cls.server.server_close()
        import elesync.web as web_mod

        if web_mod._state.store:
            web_mod._state.store.close()
        web_mod._state.user_mgr = None
        web_mod._state.session_mgr = None
        cls.vault_tmp.cleanup()

    def test_stats_open_without_login(self):
        code, data = _get(self.port, "/api/stats")
        self.assertEqual(code, 200)

    def test_root_serves_dashboard_directly_no_redirect(self):
        with urlopen(_url(self.port, "/"), timeout=5) as r:
            self.assertEqual(r.status, 200)
            self.assertIn("EleSync", r.read().decode())

    def test_me_endpoint_reports_no_auth_required(self):
        code, data = _get(self.port, "/api/me")
        self.assertEqual(code, 200)
        self.assertFalse(data["auth_required"])


if __name__ == "__main__":
    unittest.main()
