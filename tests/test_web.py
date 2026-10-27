"""Tests for the local web UI server (elesync.web).

Spins up the HTTP server on a random port in a background thread,
hits the REST API with urllib (stdlib only — no requests), and
asserts expected responses. No browser is opened.
"""

from __future__ import annotations

import gzip
import io
import json
import os
import re
import tempfile
import threading
import time
import unittest
import unittest.mock as mock
from email.message import Message
from http.server import HTTPServer
from pathlib import Path
from urllib.error import HTTPError
from urllib.request import Request, urlopen


def _url(port: int, path: str) -> str:
    return f"http://127.0.0.1:{port}{path}"


def _get(port: int, path: str) -> tuple[int, dict]:
    try:
        with urlopen(_url(port, path), timeout=5) as r:
            return r.status, json.loads(r.read())
    except HTTPError as e:
        return e.code, json.loads(e.read())


def _post(port: int, path: str, body: dict) -> tuple[int, dict]:
    data = json.dumps(body).encode()
    req = Request(
        _url(port, path), data=data, headers={"Content-Type": "application/json"}, method="POST"
    )
    try:
        with urlopen(req, timeout=5) as r:
            return r.status, json.loads(r.read())
    except HTTPError as e:
        return e.code, json.loads(e.read())


class TestRequestBodyLimits(unittest.TestCase):
    def test_oversized_body_is_rejected_before_reading(self):
        import elesync.web as web_mod

        handler = object.__new__(web_mod._Handler)
        headers = Message()
        headers["Content-Length"] = str(web_mod.MAX_REQUEST_BODY_BYTES + 1)
        handler.headers = headers
        handler.rfile = io.BytesIO(b"")
        with self.assertRaises(web_mod.RequestTooLarge):
            handler._read_body()

    def _handler_with_body(self, body: bytes, rfile):
        import elesync.web as web_mod

        handler = object.__new__(web_mod._Handler)
        headers = Message()
        headers["Content-Length"] = str(len(body))
        handler.headers = headers
        handler.command = "POST"
        handler.rfile = rfile
        handler.wfile = io.BytesIO()
        return handler

    def test_unread_body_is_drained_before_the_connection_closes(self):
        # Routes like /api/tunnel/start never read their body. Closing the
        # socket with it unread makes Windows reset the connection, and the
        # client loses the response (WinError 10053).
        rfile = mock.Mock()
        rfile.read1.side_effect = [b"{}", b""]
        handler = self._handler_with_body(b"{}", rfile)
        handler.finish()
        rfile.read1.assert_called_once_with(2)

    def test_read_body_is_not_read_again_when_the_connection_closes(self):
        # On a real socket, reading past a consumed body blocks.
        rfile = mock.Mock()
        rfile.read1.side_effect = [b"{}"]
        handler = self._handler_with_body(b"{}", rfile)
        self.assertEqual(handler._read_body(), {})
        handler.finish()
        rfile.read1.assert_called_once_with(2)
        rfile.read.assert_not_called()


def _put(port: int, path: str, body: dict) -> tuple[int, dict]:
    data = json.dumps(body).encode()
    req = Request(
        _url(port, path), data=data, headers={"Content-Type": "application/json"}, method="PUT"
    )
    try:
        with urlopen(req, timeout=5) as r:
            return r.status, json.loads(r.read())
    except HTTPError as e:
        return e.code, json.loads(e.read())


def _delete(port: int, path: str) -> tuple[int, dict]:
    req = Request(_url(port, path), method="DELETE")
    try:
        with urlopen(req, timeout=5) as r:
            return r.status, json.loads(r.read())
    except HTTPError as e:
        return e.code, json.loads(e.read())


class WebServerTestCase(unittest.TestCase):
    """Base: starts a fresh in-memory vault + HTTP server for each test class."""

    port: int = 0
    server: HTTPServer | None = None
    thread: threading.Thread | None = None

    @classmethod
    def setUpClass(cls):
        import elesync.web as web_mod
        import elesync.web_auth as web_auth
        from elesync.store import MemoryStore

        cls.vault_tmp = tempfile.TemporaryDirectory()
        vault_path = Path(cls.vault_tmp.name)
        web_mod._state.store = MemoryStore(vault_path)
        web_mod._state.vault_dir = str(vault_path)
        web_mod._state.user_mgr = web_auth.UserManager(web_mod.web_auth_dir(vault_path))
        web_mod._state.session_mgr = web_auth.SessionManager(
            web_mod.web_auth_dir(vault_path), require_existing_user=True
        )

        # Bind to port 0 → OS picks a free port
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
        cls.vault_tmp.cleanup()


class TestRequestBodyValidation(WebServerTestCase):
    """Every route that takes a body enforces the size limit and a sane
    Content-Length, and a rejected request never waits on a body that is not
    coming."""

    def _status(
        self, path: str, content_length: int, body: bytes = b"", content_type="application/json"
    ):
        import socket

        with socket.create_connection(("127.0.0.1", self.port), timeout=5) as sock:
            sock.sendall(
                f"POST {path} HTTP/1.1\r\nHost: 127.0.0.1\r\nContent-Type: {content_type}\r\n"
                f"Content-Length: {content_length}\r\n\r\n".encode()
                + body
            )
            # Raises TimeoutError if the server is still waiting for the body.
            return int(sock.recv(64).split(b" ", 2)[1])

    def test_mcp_rejects_a_body_over_the_limit_without_reading_it(self):
        import elesync.web as web_mod

        self.assertEqual(self._status("/mcp", web_mod.MAX_REQUEST_BODY_BYTES + 1), 413)

    def test_mcp_rejects_a_negative_length(self):
        self.assertEqual(self._status("/mcp", -1), 400)

    def test_oauth_token_rejects_a_negative_length(self):
        self.assertEqual(
            self._status("/oauth/token", -1, content_type="application/x-www-form-urlencoded"),
            400,
        )

    def test_rejected_request_does_not_wait_for_a_missing_body(self):
        import elesync.web as web_mod

        web_mod._state.user_mgr.add_user("owner", "correct-horse-battery")
        self.addCleanup(web_mod._state.user_mgr.remove_user, "owner")
        # 100 bytes announced, 10 sent: the 401 must still arrive, bounded by
        # the drain timeout rather than by the client giving up.
        with mock.patch.object(web_mod, "DRAIN_TIMEOUT_SECONDS", 0.5):
            self.assertEqual(self._status("/api/memories", 100, b"x" * 10), 401)


# ---------------------------------------------------------------------------
# Static routes
# ---------------------------------------------------------------------------


class TestStaticRoutes(WebServerTestCase):
    def test_graph_route_and_assets(self):
        status, data = _get(self.port, "/api/graph")
        self.assertEqual(status, 200)
        self.assertIn("nodes", data)
        self.assertIn("edges", data)
        self.assertEqual(data["shown"], data["total"])
        self.assertFalse(data["truncated"])
        status, _ = _get(self.port, "/api/graph?project_id=missing")
        self.assertEqual(status, 400)
        for asset in (
            "dashboard-graph.js",
            "dashboard-graph-layout.js",
            "dashboard-graph-atlas.js",
            "dashboard-graph.css",
            "dashboard-theme.js",
            "dashboard-theme-cosmic.css",
        ):
            with urlopen(_url(self.port, "/static/" + asset), timeout=5) as response:
                self.assertEqual(response.status, 200)

    def test_root_returns_html(self):
        with urlopen(_url(self.port, "/"), timeout=5) as r:
            self.assertEqual(r.status, 200)
            ct = r.headers.get("Content-Type", "")
            self.assertIn("text/html", ct)
            body = r.read().decode()
            self.assertIn("EleSync", body)
            self.assertIn("<html", body.lower())
            self.assertIn('id="workspace-heading"', body)
            self.assertIn('id="overview-active-project"', body)
            self.assertIn('id="overview-collaborators"', body)
            self.assertIn('id="workspace-projects-heading"', body)
            self.assertIn('class="pairing-page"', body)
            self.assertNotIn('class="companion-screen"', body)
            self.assertNotIn("Good evening, John", body)
            self.assertNotIn("EleSync Redesign", body)
            self.assertNotIn("ChatGPT conversation imported", body)

    def test_index_html_alias(self):
        with urlopen(_url(self.port, "/index.html"), timeout=5) as r:
            self.assertEqual(r.status, 200)

    def test_dashboard_permission_labels_explain_access(self):
        with urlopen(_url(self.port, "/static/dashboard-projects.js"), timeout=5) as r:
            body = r.read().decode()
        self.assertIn("Read project", body)
        self.assertIn("Add updates", body)
        self.assertIn("Read conversations", body)
        self.assertIn("Access is revoked until restored.", body)

    def test_dashboard_loads_shared_api_module_before_features(self):
        with urlopen(_url(self.port, "/"), timeout=5) as r:
            html = r.read().decode()
        self.assertLess(html.index("/static/dashboard-api.js"), html.index("/static/dashboard.js"))
        self.assertLess(
            html.index("/static/dashboard-projects.js"), html.index("/static/dashboard.js")
        )
        self.assertLess(
            html.index("/static/dashboard-connect.js"), html.index("/static/dashboard.js")
        )
        self.assertLess(
            html.index("/static/dashboard-import.js"), html.index("/static/dashboard.js")
        )
        with urlopen(_url(self.port, "/static/dashboard-api.js"), timeout=5) as r:
            api_script = r.read().decode()
        self.assertIn("async function api(", api_script)

    def test_pwa_assets_are_served(self):
        for path in ("/static/manifest.json", "/service-worker.js", "/static/dashboard-pwa.js"):
            with self.subTest(path=path), urlopen(_url(self.port, path), timeout=5) as r:
                self.assertEqual(r.status, 200)

    def test_whatsapp_review_requires_an_explicit_dismissal(self):
        """An accidental outside click must not discard a pending chat import."""
        with urlopen(_url(self.port, "/"), timeout=5) as response:
            html = response.read().decode()

        self.assertIn('id="whatsapp-review-dialog"', html)
        self.assertNotIn(
            'id="whatsapp-review-dialog" onclick="if(event.target===this)closeWhatsAppReview()"',
            html,
        )
        self.assertIn('onclick="closeWhatsAppReview()">Dismiss import</button>', html)

    def test_file_picker_is_cleared_for_same_file_retries(self):
        with urlopen(_url(self.port, "/static/dashboard-import.js"), timeout=5) as response:
            script = response.read().decode()

        self.assertIn("const files = Array.from(fileList || []);", script)
        self.assertIn("if (picker) picker.value = '';", script)
        self.assertIn("if (files.length) importFiles(files);", script)

    def test_detail_panel_uses_reader_presentation(self):
        with urlopen(
            _url(self.port, "/static/dashboard-command-center.css"), timeout=5
        ) as response:
            css = response.read().decode()
        with urlopen(_url(self.port, "/static/dashboard.js"), timeout=5) as response:
            script = response.read().decode()
        self.assertIn("Reader, not drawer", css)
        self.assertIn("Conversation reader", script)
        self.assertIn("function conversationMessages(content)", script)
        self.assertIn("\\[\\d{1,4}", script)

    def test_offline_drafts_never_fall_back_to_plaintext(self):
        with urlopen(_url(self.port, "/static/dashboard.js"), timeout=5) as response:
            script = response.read().decode()
        self.assertIn("Offline draft encryption is unavailable", script)
        self.assertNotIn(
            "localStorage.setItem(OFFLINE_DRAFTS_KEY, JSON.stringify(drafts.slice(-50)))",
            script,
        )

    def test_sidebar_has_no_navigation_divider(self):
        with urlopen(_url(self.port, "/"), timeout=5) as response:
            html = response.read().decode()
        self.assertNotIn('<div class="nav-sep"></div>', html)

    def test_network_info_does_not_claim_loopback_is_phone_reachable(self):
        code, data = _get(self.port, "/api/network-info")
        self.assertEqual(code, 200)
        self.assertFalse(data["auth_enabled"])
        self.assertFalse(data["reachable_from_network"])

    def test_network_qr_rejects_loopback_pairing(self):
        code, _ = _get(self.port, "/api/network-qr")
        self.assertEqual(code, 409)

    def test_tunnel_start_requires_dashboard_authentication(self):
        import elesync.web as web_mod

        original_manager = web_mod._state.tunnel_manager
        manager = mock.Mock()
        manager.start.return_value = True
        manager.status.return_value = {"active": True, "url": "https://pair.example", "error": None}
        web_mod._state.tunnel_manager = manager
        try:
            code, data = _post(self.port, "/api/tunnel/start", {})
        finally:
            web_mod._state.tunnel_manager = original_manager

        self.assertEqual(code, 403)
        self.assertIn("Protect the dashboard", data["error"])
        manager.start.assert_not_called()

    def test_companion_ui_never_offers_loopback_as_a_phone_address(self):
        with urlopen(_url(self.port, "/static/dashboard-import.js"), timeout=5) as r:
            script = r.read().decode()
        with urlopen(_url(self.port, "/static/dashboard.js"), timeout=5) as r:
            dashboard = r.read().decode()
        self.assertIn("Secure pairing is not ready yet.", script)
        self.assertIn("Loopback addresses are never shown", script)
        self.assertIn("if (!info.auth_enabled)", script)
        self.assertIn("Protect dashboard first", script)
        self.assertIn("maybeShowSetup(true)", script)
        self.assertIn("pairingButton.onclick = enablePhonePairing", script)
        self.assertIn("pairingButton.hidden = true", script)
        self.assertNotIn("urlEl.textContent = window.location.origin", script)
        self.assertIn("loadOverview().then(loadRemote).catch(loadRemote)", dashboard)

    def test_dashboard_has_a_persistent_user_knowledge_block(self):
        with urlopen(_url(self.port, "/"), timeout=5) as r:
            html = r.read().decode()
        with urlopen(_url(self.port, "/static/dashboard.js"), timeout=5) as r:
            script = r.read().decode()
        self.assertIn('class="dashboard-card cc-knowledge"', html)
        self.assertIn('id="overview-knowledge-conversations"', html)
        self.assertIn("overview-knowledge-conversations", script)

    def test_home_renders_projects_without_waiting_for_stats(self):
        # /api/stats runs the whole-vault conflict scan. Home used to await it
        # alongside the projects and showed "Loading your projects…" and
        # 0 memories for 20 s on an 850-memory vault.
        with urlopen(_url(self.port, "/static/dashboard.js"), timeout=5) as r:
            script = r.read().decode()
        with urlopen(_url(self.port, "/"), timeout=5) as r:
            html = r.read().decode()
        body = script.split("async function loadOverview()", 1)[1].split("\nfunction ", 1)[0]
        self.assertNotIn("Promise.all([api('/api/stats')", body)
        self.assertLess(body.index("renderWorkspaceProjects("), body.index("await statsLoaded"))
        # Counts read as loading until they arrive, never as an empty vault.
        self.assertIn('<strong id="overview-memories">…</strong>', html)
        self.assertNotIn('<strong id="overview-memories">0</strong>', html)

    def test_new_note_reports_a_refused_save_instead_of_queueing_it(self):
        # A save the server refused ("project not found" for a stale active
        # project) was reported as "Saved offline" and retried forever.
        with urlopen(_url(self.port, "/static/dashboard-api.js"), timeout=5) as r:
            transport = r.read().decode()
        with urlopen(_url(self.port, "/static/dashboard.js"), timeout=5) as r:
            script = r.read().decode()
        self.assertIn("offline.network = true", transport)
        self.assertIn("refused.status = res.status", transport)
        add = script.split("async function addMemory()", 1)[1].split("\nfunction ", 1)[0]
        self.assertLess(
            add.index("if (!worthRetryingLater(e))"), add.index("saveOfflineDraft(body)")
        )
        # Drafts already queued for a project that is gone are saved unfiled.
        flush = script.split("async function flushOfflineDrafts()", 1)[1].split("\nfunction ", 1)[0]
        self.assertIn("project_id: null", flush)

    def test_new_note_shows_its_project_and_drops_a_stale_one(self):
        with urlopen(_url(self.port, "/"), timeout=5) as r:
            html = r.read().decode()
        with urlopen(_url(self.port, "/static/dashboard.js"), timeout=5) as r:
            script = r.read().decode()
        self.assertIn('<label class="add-label" for="add-project">Save to</label>', html)
        self.assertIn("else if (name === 'add') loadAddProjects();", script)
        add = script.split("async function addMemory()", 1)[1].split("\nfunction ", 1)[0]
        self.assertIn("document.getElementById('add-project')", add)
        overview = script.split("async function loadOverview()", 1)[1].split("\nfunction ", 1)[0]
        self.assertIn("forgetMissingActiveProject(", overview)

    def test_companion_page_keeps_the_sidebar_layout(self):
        # `display:block` here let the vault / Install app box float up to the
        # middle of the sidebar on the Companion page only.
        with urlopen(_url(self.port, "/static/dashboard.css"), timeout=5) as r:
            css = r.read().decode()
        self.assertIn("body:has(#page-remote.active) .sidebar { display:flex; }", css)
        self.assertNotIn("body:has(#page-remote.active) .sidebar { display:block; }", css)
        # Phones kept the page but hid every way off it.
        self.assertNotIn("body:has(#page-remote.active) .sidebar { display:none; }", css)

    def test_phones_reach_every_destination_through_more(self):
        # The phone tab bar hides AI collaborators, Companion and Vault; with no
        # "More" tab they could not be reached at all.
        with urlopen(_url(self.port, "/"), timeout=5) as r:
            html = r.read().decode()
        with urlopen(_url(self.port, "/static/dashboard.js"), timeout=5) as r:
            script = r.read().decode()
        self.assertIn('id="nav-mobile-more"', html)
        sheet = html.split('id="mobile-more-sheet"', 1)[1].split("</div>", 1)[0]
        for page in ("graph", "connect", "remote", "usb"):
            self.assertIn(f"go('{page}')", sheet)
        self.assertIn("const MORE_PAGES = ['graph', 'connect', 'remote', 'usb'];", script)

    def test_each_destination_has_one_name(self):
        # "New note", "Save something", "Save memory" and "New memory" were one
        # action; "Connect AI", "Invite AI" and "Manage access" one place; and
        # "Vault" opened a page titled "Move vault to USB".
        with urlopen(_url(self.port, "/"), timeout=5) as r:
            html = r.read().decode()
        with urlopen(_url(self.port, "/static/dashboard.js"), timeout=5) as r:
            script = r.read().decode()
        for old in ("New note", "Save something", "Connect AI", "Invite AI", "Move vault to USB"):
            self.assertNotIn(old, html + script, old)
        self.assertNotIn("<h2>Manage access</h2>", html)
        self.assertIn('<span class="nav-label nav-label-desktop">New memory</span>', html)
        self.assertIn("add:'New memory'", script)
        self.assertIn("connect:'AI collaborators'", script)
        self.assertIn("usb:'Vault & backups'", script)

    def test_home_says_each_thing_once(self):
        with urlopen(_url(self.port, "/"), timeout=5) as r:
            html = r.read().decode()
        with urlopen(_url(self.port, "/static/dashboard.js"), timeout=5) as r:
            script = r.read().decode()
        # The strip listed the active project again above its own card.
        strip = script.split("function renderWorkspaceProjects(projects)", 1)[1]
        strip = strip.split("\n}\n", 1)[0]
        self.assertIn("projects.filter(p => !active || p.id !== active.id)", strip)
        # 828 "Vault memories" beside 852 "items saved" read as two counts that
        # disagree; the card now adds the conversations instead.
        self.assertIn("<small>Memories</small>", html)
        self.assertNotIn("items saved", html)
        self.assertIn('<strong id="overview-knowledge-conversations">', html)
        # A short memory filled both the preview and the full text.
        edit = script.split("function editDetail(editing)", 1)[1].split("\n}\n", 1)[0]
        self.assertIn("document.getElementById('d-overview').hidden = true;", edit)

    def test_activity_feed_collapses_back_to_back_repeats(self):
        # Half the Home feed was one assistant's identical "status" checks.
        with urlopen(_url(self.port, "/static/dashboard.js"), timeout=5) as r:
            script = r.read().decode()
        audit = script.split("async function loadAudit()", 1)[1].split("\n}\n", 1)[0]
        self.assertIn("collapseRepeats(a.entries).slice(0, 8)", audit)
        self.assertIn("row.count > 1", audit)
        self.assertIn("function collapseRepeats(entries)", script)

    def test_memories_page_loads_more_instead_of_paging(self):
        # 852 memories were 29 pages behind prev/next arrows at the very bottom.
        with urlopen(_url(self.port, "/static/dashboard.js"), timeout=5) as r:
            script = r.read().decode()
        self.assertIn("async function loadMoreBrowse(button)", script)
        self.assertIn("renderLoadMore(data.total || 0);", script)
        self.assertIn("filter(item => !seen.has(item.id))", script)
        self.assertNotIn("function renderPagination(", script)

    def test_import_page_does_not_assume_a_phone(self):
        # A computer was told to "tap to browse" and "Choose files from phone".
        with urlopen(_url(self.port, "/"), timeout=5) as r:
            html = r.read().decode()
        with urlopen(_url(self.port, "/static/dashboard.css"), timeout=5) as r:
            css = r.read().decode()
        self.assertNotIn("tap to browse", html)
        self.assertNotIn("Choose files from phone", html)
        # By width: a touchscreen laptop reports no mouse but has no share sheet.
        self.assertIn("#share-fallback { display:none; }", css)
        self.assertIn("@media (max-width:700px) { #share-fallback { display:block; } }", css)
        self.assertNotIn("(pointer:fine) { #share-fallback", css)

    def test_companion_copies_only_a_real_address(self):
        # Copy was live while the box said "Protect dashboard first".
        with urlopen(_url(self.port, "/static/dashboard-import.js"), timeout=5) as r:
            script = r.read().decode()
        self.assertIn("if (copyButton) copyButton.disabled = !urlEl.dataset.address;", script)
        copy = script.split("function copyRemoteUrl()", 1)[1].split("\n}\n", 1)[0]
        self.assertIn(".dataset.address", copy)
        self.assertNotIn("textContent", copy)

    def test_cards_open_from_the_keyboard(self):
        # Memory and project cards were click-only <article>/<div>s.
        scripts = {}
        for name in ("dashboard.js", "dashboard-projects.js"):
            with urlopen(_url(self.port, f"/static/{name}"), timeout=5) as r:
                scripts[name] = r.read().decode()
        self.assertIn('<button type="button" class="memory-open"', scripts["dashboard.js"])
        self.assertIn(
            'class="mem-card project-list-card" data-project="${_gEsc(p.id)}" role="button" tabindex="0"',
            scripts["dashboard-projects.js"],
        )
        self.assertIn(
            'data-brief-memory="${_gEsc(r.id)}" role="button" tabindex="0"',
            scripts["dashboard-projects.js"],
        )
        self.assertIn("card.getAttribute('role') !== 'button'", scripts["dashboard.js"])

    def test_unchanged_static_files_revalidate_to_an_empty_304(self):
        # Every load re-downloaded every file: no ETag, so nothing to revalidate.
        with urlopen(_url(self.port, "/static/dashboard.css"), timeout=5) as r:
            etag = r.headers["ETag"]
        self.assertTrue(etag)
        request = Request(_url(self.port, "/static/dashboard.css"), headers={"If-None-Match": etag})
        with self.assertRaises(HTTPError) as caught:
            urlopen(request, timeout=5)
        self.assertEqual(caught.exception.code, 304)
        self.assertEqual(caught.exception.read(), b"")

    def test_text_is_gzipped_for_clients_that_accept_it(self):
        request = Request(
            _url(self.port, "/static/dashboard.js"), headers={"Accept-Encoding": "gzip"}
        )
        with urlopen(request, timeout=5) as r:
            self.assertEqual(r.headers["Content-Encoding"], "gzip")
            self.assertEqual(r.headers["Vary"], "Accept-Encoding")
            packed = r.read()
        with urlopen(_url(self.port, "/static/dashboard.js"), timeout=5) as r:
            plain = r.read()
        self.assertEqual(gzip.decompress(packed), plain)
        self.assertLess(len(packed), len(plain) // 3)
        # Images are compressed formats already.
        request = Request(
            _url(self.port, "/static/elesync-favicon.png"), headers={"Accept-Encoding": "gzip"}
        )
        with urlopen(request, timeout=5) as r:
            self.assertIsNone(r.headers["Content-Encoding"])

    def test_dashboard_images_are_sized_for_the_page(self):
        # The sidebar logo was a 1.66 MB, 1254px PNG shown at 124px, and the
        # same file doubled as the favicon every page fetched.
        import elesync.web as web_mod

        with urlopen(_url(self.port, "/"), timeout=5) as r:
            html = r.read().decode()
        self.assertIn('src="/static/elesync-main-logo.webp"', html)
        self.assertIn('<link rel="icon" href="/static/elesync-favicon-cosmic.png"', html)
        static = Path(web_mod.__file__).parent / "static"
        for name, limit in (
            ("elesync-main-logo.webp", 100_000),
            ("elesync-favicon.png", 20_000),
            ("elesync-ui-elephant-v2.webp", 200_000),
            ("elesync-main-logo.png", 400_000),
            ("elesync-main-logo-cosmic.webp", 100_000),
            ("elesync-favicon-cosmic.png", 20_000),
            ("elesync-main-logo-cosmic.png", 400_000),
        ):
            self.assertLess((static / name).stat().st_size, limit, name)

    def test_cosmic_theme_has_gold_copies_of_every_swapped_image(self):
        # A hue-rotate filter turned the violet elephant red and green, and tab and
        # home-screen icons cannot be filtered at all, so the theme swaps in gold files.
        import elesync.web as web_mod

        static = Path(web_mod.__file__).parent / "static"
        theme = (static / "dashboard-theme.js").read_text(encoding="utf-8")
        swapped = re.findall(r"'(/static/[\w.-]+-cosmic\.(?:png|webp))'", theme)
        self.assertEqual(len(swapped), 3, theme)
        for path in swapped:
            with urlopen(_url(self.port, path), timeout=5) as r:
                self.assertEqual(r.status, 200, path)
        cosmic_css = (static / "dashboard-theme-cosmic.css").read_text(encoding="utf-8")
        self.assertNotIn("hue-rotate", cosmic_css)
        self.assertIn("elesync-main-logo-cosmic.webp", cosmic_css)

    def test_no_dashboard_text_is_smaller_than_11px(self):
        # 65% of the Home screen's text was 8-10px at every window width.
        size = re.compile(r"(?:font-size\s*:\s*|font\s*:\s*(?:[^;{}]*?\s)?)(\d+(?:\.\d+)?)px")
        for name in ("dashboard.css", "dashboard-command-center.css"):
            with urlopen(_url(self.port, f"/static/{name}"), timeout=5) as r:
                css = r.read().decode()
            tiny = sorted({float(px) for px in size.findall(css) if float(px) < 11})
            self.assertEqual(tiny, [], f"{name} still sets text at {tiny} px")

    def test_sidebar_keeps_primary_destinations_visible_without_conflicts(self):
        with urlopen(_url(self.port, "/"), timeout=5) as r:
            html = r.read().decode()
        self.assertIn('id="nav-remote"', html)
        self.assertNotIn('id="nav-more"', html)
        self.assertNotIn('id="nav-conflicts"', html)

    def test_onboarding_guide_uses_three_user_outcomes(self):
        with urlopen(_url(self.port, "/"), timeout=5) as r:
            html = r.read().decode()
        with urlopen(_url(self.port, "/static/dashboard-onboarding.js"), timeout=5) as r:
            script = r.read().decode()
        self.assertIn("STEP 1 OF 3", html)
        self.assertIn("Create your workspace", script)
        self.assertIn("Add your context", script)
        self.assertIn("Connect an AI", script)
        self.assertNotIn("of 6 steps complete", script)

    def test_unknown_route_404(self):
        code, _ = _get(self.port, "/api/doesnotexist")
        self.assertEqual(code, 404)


class TestLargeChatImport(WebServerTestCase):
    def test_large_whatsapp_approval_persists_chunks_and_deduplicates(self):
        import elesync.web as web_mod

        transcript = "\n".join(f"12/31/23, 10:15 PM - Alex: Message {i}" for i in range(2149))
        _, project = _post(self.port, "/api/projects", {"name": "Large WhatsApp"})
        for save_full in (False, True):
            with self.subTest(save_full=save_full):
                code, preview = _post(
                    self.port,
                    "/api/whatsapp/preview",
                    {
                        "raw_text": transcript,
                        "filename": "WhatsApp.txt",
                    },
                )
                self.assertEqual(code, 200)
                body = {
                    "preview_token": preview["preview_token"],
                    "select_all": True,
                    "save_full": save_full,
                    "project_id": project["id"],
                }
                code, result = _post(self.port, "/api/whatsapp/approve", body)
                self.assertEqual(code, 200)
                self.assertEqual(result["added"] + result["skipped_duplicates"], 22)
        store = web_mod._get_store()
        rows = store.db.execute(
            "SELECT content FROM memories WHERE source = 'whatsapp' AND type = 'conversation'"
        ).fetchall()
        self.assertEqual(len(rows), 22)
        self.assertEqual(
            store.db.execute(
                "SELECT count(*) FROM project_memories WHERE project_id = ?",
                (project["id"],),
            ).fetchone()[0],
            44,
        )
        self.assertEqual(sum(row[0].count("Alex: Message ") for row in rows), 2149)
        code, result = _post(
            self.port,
            "/api/import",
            {
                "raw_text": transcript,
                "filename": "WhatsApp.txt",
            },
        )
        self.assertEqual(code, 200)
        self.assertEqual(result["added"], 0)
        self.assertEqual(result["skipped_duplicates"], 22)

    def test_whatsapp_chunking_preserves_selection_and_privacy(self):
        transcript = "\n".join(
            f"12/31/23, 10:15 PM - Alex: Message {i} https://example.com" for i in range(205)
        )
        import elesync.web as web_mod

        with mock.patch.object(web_mod._get_store(), "import_many", return_value={}) as save:
            code, _ = _post(
                self.port,
                "/api/whatsapp/approve",
                {
                    "raw_text": transcript,
                    "select_all": True,
                    "excluded_indices": [0, 100, 204],
                    "strip_links": True,
                },
            )
            self.assertEqual(code, 200)
            items = save.call_args.args[0]
            self.assertEqual(len(items), 3)
            content = "\n".join(item.content for item in items)
            self.assertEqual(content.count("Alex: Message "), 202)
            self.assertNotIn("https://", content)
            self.assertNotIn("Message 100 ", content)
            self.assertTrue(all(item.type == "note" for item in items))

    def test_whatsapp_preview_accepts_a_transcript_larger_than_four_megabytes(self):
        transcript = "12/31/23, 10:15 PM - Alex: " + ("x" * ((5 << 20) + 1))
        code, preview = _post(
            self.port,
            "/api/whatsapp/preview",
            {"raw_text": transcript, "filename": "WhatsApp Chat.txt"},
        )

        self.assertEqual(code, 200)
        self.assertEqual(preview["message_count"], 1)
        self.assertEqual(len(preview["messages"][0]["text"]), (5 << 20) + 1)

    def test_whatsapp_preview_pages_instead_of_rendering_every_message(self):
        transcript = "\n".join(
            f"12/31/23, 10:{index % 60:02d} PM - Alex: Message {index}" for index in range(450)
        )
        code, first = _post(
            self.port,
            "/api/whatsapp/preview",
            {"raw_text": transcript, "filename": "WhatsApp Chat.txt"},
        )
        self.assertEqual(code, 200)
        self.assertEqual(first["message_count"], 450)
        self.assertEqual(len(first["messages"]), 200)

        code, last = _post(
            self.port,
            "/api/whatsapp/preview",
            {"preview_token": first["preview_token"], "offset": 400, "limit": 200},
        )
        self.assertEqual(code, 200)
        self.assertEqual(len(last["messages"]), 50)
        self.assertEqual(last["messages"][0]["index"], 400)


# ---------------------------------------------------------------------------
# /api/vault
# ---------------------------------------------------------------------------


class TestVaultEndpoint(WebServerTestCase):
    def test_vault_returns_path(self):
        code, data = _get(self.port, "/api/vault")
        self.assertEqual(code, 200)
        self.assertIn("vault_dir", data)
        self.assertIsInstance(data["vault_dir"], str)


# ---------------------------------------------------------------------------
# /api/stats
# ---------------------------------------------------------------------------


class TestStatsEndpoint(WebServerTestCase):
    def test_stats_shape(self):
        code, data = _get(self.port, "/api/stats")
        self.assertEqual(code, 200)
        self.assertIn("total", data)
        self.assertIn("by_source", data)
        self.assertIn("by_type", data)

    def test_stats_total_is_int(self):
        _, data = _get(self.port, "/api/stats")
        self.assertIsInstance(data["total"], int)

    def test_stats_reports_last_backup_field(self):
        _, data = _get(self.port, "/api/stats")
        self.assertIn("last_backup_at", data)


class TestHandoffEndpoint(WebServerTestCase):
    def test_returns_prompt_for_a_project_and_404_otherwise(self):
        code, project = _post(self.port, "/api/projects", {"name": "Handoff trip"})
        self.assertEqual(code, 201)
        code, data = _get(self.port, f"/api/projects/{project['id']}/handoff")
        self.assertEqual(code, 200)
        self.assertIn("Project: Handoff trip", data["prompt"])
        self.assertEqual(data["characters"], len(data["prompt"]))
        self.assertEqual(_get(self.port, "/api/projects/missing-project/handoff")[0], 404)


class TestPhoneCaptures(WebServerTestCase):
    PHONE_UA = "Mozilla/5.0 (Linux; Android 14; Pixel 8) AppleWebKit/537.36 Mobile Safari/537.36"
    DESKTOP_UA = (
        "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 Chrome/130 Safari/537.36"
    )

    def _post_as(self, path, body, user_agent):
        req = Request(
            _url(self.port, path),
            data=json.dumps(body).encode(),
            headers={"Content-Type": "application/json", "User-Agent": user_agent},
            method="POST",
        )
        with urlopen(req, timeout=5) as r:
            return r.status, json.loads(r.read())

    def _captures(self):
        import elesync.web as web_mod
        from elesync.audit import AuditLog

        return [e for e in AuditLog(web_mod._state.vault_dir).read() if e["op"] == "CAPTURE"]

    def test_companion_note_is_recorded_as_a_phone_capture(self):
        before = len(self._captures())
        body = {
            "text": "Phone note about the Kyoto ryokan checkout time",
            "source": "EleSync companion",
        }
        code, result = self._post_as("/api/import", body, self.DESKTOP_UA)
        self.assertEqual(code, 200)
        self.assertEqual(len(result["memory_ids"]), 1)
        captures = self._captures()
        self.assertEqual(len(captures), before + 1)
        self.assertEqual(captures[0]["ai"], "phone")
        self.assertEqual(captures[0]["mem"], result["memory_ids"][0][:8])

    def test_saves_from_a_phone_browser_are_captures_and_desktop_saves_are_not(self):
        before = len(self._captures())
        self._post_as("/api/memories", {"content": "Saved on the laptop"}, self.DESKTOP_UA)
        self.assertEqual(len(self._captures()), before)
        _, item = self._post_as("/api/memories", {"content": "Saved on the phone"}, self.PHONE_UA)
        captures = self._captures()
        self.assertEqual(len(captures), before + 1)
        self.assertEqual(captures[0]["mem"], item["id"][:8])


class TestReadinessEndpoint(WebServerTestCase):
    def test_reports_last_seen_per_assistant_and_setup_state(self):
        import contextvars

        import elesync.web as web_mod
        from elesync.audit import AuditLog, current_ai

        log = AuditLog(web_mod._state.vault_dir)

        def as_ai(name, op):
            def run():
                current_ai.set(name)
                log.log(op, client="x")

            contextvars.copy_context().run(run)

        as_ai("claude", "RECALL")
        as_ai("chatgpt", "REMEMBER")
        as_ai("claude", "STATUS")
        log.log("RECALL", client="x")

        code, data = _get(self.port, "/api/readiness")
        self.assertEqual(code, 200)
        self.assertEqual(data["activity"]["claude"]["op"], "STATUS")
        self.assertEqual(data["activity"]["chatgpt"]["op"], "REMEMBER")
        self.assertEqual(data["activity"][""]["op"], "RECALL")
        self.assertEqual(set(data["tunnel"]), {"active", "url"})
        self.assertIn("last_backup_at", data["backup"])
        self.assertTrue(all("ai" in c for c in data["remote_clients"] + data["local_clients"]))


class TestDemoEndpointsOffByDefault(WebServerTestCase):
    def test_demo_is_disabled_outside_ele_demo(self):
        code, data = _get(self.port, "/api/demo")
        self.assertEqual((code, data), (200, {"enabled": False, "steps": []}))
        self.assertEqual(_get(self.port, "/api/demo/history")[0], 404)
        self.assertEqual(_post(self.port, "/api/demo/step", {"step": "connect"})[0], 404)


class TestProjectDiscoveryEndpoints(WebServerTestCase):
    def setUp(self):
        import elesync.web as web_mod
        from elesync.models import MemoryItem

        texts = [
            "# Kyoto trip budget\nuser: budget for our Kyoto trip",
            "# Planning a Kyoto trip\nuser: plan five days in Kyoto",
            "# Helios launch plan\nuser: launch plan for Helios",
            "# Helios pricing\nuser: Helios launch pricing",
        ]
        for text in texts:
            web_mod._state.store.upsert(
                MemoryItem(content=text, source="chatgpt", type="conversation")
            )

    def test_discover_then_accept_creates_filled_projects(self):
        code, data = _get(self.port, "/api/project-discovery")
        self.assertEqual(code, 200)
        by_name = {s["name"]: s for s in data["suggestions"]}
        self.assertIn("Kyoto trip", by_name)
        self.assertIn("Helios launch", by_name)
        body = {
            "projects": [
                {"name": "Kyoto trip", "memory_ids": by_name["Kyoto trip"]["memory_ids"]},
                {"name": "Helios", "memory_ids": by_name["Helios launch"]["memory_ids"]},
            ]
        }
        code, created = _post(self.port, "/api/project-discovery/accept", body)
        self.assertEqual(code, 201)
        self.assertEqual([p["name"] for p in created["created"]], ["Kyoto trip", "Helios"])
        self.assertEqual([p["memory_count"] for p in created["created"]], [2, 2])
        _, again = _get(self.port, "/api/project-discovery")
        self.assertEqual(again["suggestions"], [])

    def test_accept_rejects_bad_bodies_before_creating_anything(self):
        _, before = _get(self.port, "/api/projects")
        for body in (
            {},
            {"projects": []},
            {"projects": [{"name": "Ok", "memory_ids": []}, {"name": " ", "memory_ids": []}]},
            {"projects": [{"name": "Ok", "memory_ids": "abc"}]},
        ):
            code, _ = _post(self.port, "/api/project-discovery/accept", body)
            self.assertEqual(code, 400, body)
        _, after = _get(self.port, "/api/projects")
        self.assertEqual(len(after["projects"]), len(before["projects"]))

    def test_discover_rejects_non_integer_limit(self):
        code, _ = _get(self.port, "/api/project-discovery?limit=lots")
        self.assertEqual(code, 400)


class TestAuditEnrichment(unittest.TestCase):
    def setUp(self):
        import elesync.web as web_mod
        from elesync.audit import AuditLog
        from elesync.models import MemoryItem
        from elesync.store import MemoryStore

        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        saved = (web_mod._state.store, web_mod._state.vault_dir)
        self.store = MemoryStore(Path(self.tmp.name))
        web_mod._state.store = self.store
        web_mod._state.vault_dir = self.tmp.name

        def restore():
            self.store.close()
            web_mod._state.store, web_mod._state.vault_dir = saved

        self.addCleanup(restore)
        self.item = MemoryItem(content="Flight leaves at noon.", source="claude", type="fact")
        self.store.upsert(self.item)
        self.project = self.store.create_project("Japan trip")
        self.store.add_to_project(self.project["id"], self.item.id)
        self.audit = AuditLog(self.tmp.name)

    def test_entries_carry_memory_and_project(self):
        import elesync.web as web_mod

        self.audit.log("REMEMBER", client="x", memory_id=self.item.id[:8])
        self.audit.log("PROJECT_RECALL", client="x", detail=f"project={self.project['id'][:8]} q")
        recall, remember = web_mod._api_audit({})["entries"]
        self.assertEqual(remember["memory"]["preview"], "Flight leaves at noon.")
        self.assertEqual(remember["memory"]["source"], "claude")
        self.assertEqual(remember["memory"]["projects"], ["Japan trip"])
        self.assertEqual(recall["project_name"], "Japan trip")

    def _conflicting_pair(self):
        from elesync.models import MemoryItem

        later = MemoryItem(
            content="Flight leaves at 3pm.",
            source="chatgpt",
            type="fact",
            created_at="2099-01-01T00:00:00+00:00",
        )
        self.store.upsert(later)
        self.audit.log(
            "CONFLICT", client="x", memory_id=later.id[:8], detail=f"with={self.item.id[:8]}"
        )
        return later

    def test_open_conflict_lists_every_version_oldest_first(self):
        import elesync.web as web_mod

        later = self._conflicting_pair()
        (entry,) = web_mod._api_audit({})["entries"]
        conflict = entry["conflict"]
        self.assertFalse(conflict["resolved"])
        self.assertTrue(conflict["cluster_id"])
        self.assertEqual([i["id"] for i in conflict["items"]], [self.item.id, later.id])
        self.assertEqual([i["source"] for i in conflict["items"]], ["claude", "chatgpt"])

    def test_resolved_conflict_is_marked_resolved(self):
        import elesync.web as web_mod
        from elesync.service import MemoryService

        later = self._conflicting_pair()
        cid = web_mod._api_audit({})["entries"][0]["conflict"]["cluster_id"]
        MemoryService(self.store).resolve_conflict(cid, choose_id=later.id)
        conflict = web_mod._api_audit({})["entries"][0]["conflict"]
        self.assertTrue(conflict["resolved"])
        self.assertIsNone(conflict["cluster_id"])
        superseded = {i["id"]: i["superseded"] for i in conflict["items"]}
        self.assertEqual(superseded, {self.item.id: True, later.id: False})

    def test_supersede_entry_carries_replacement(self):
        import elesync.web as web_mod
        from elesync.models import MemoryItem

        newer = MemoryItem(content="Flight leaves at 3pm.", source="chatgpt", type="fact")
        self.store.upsert(newer)
        self.audit.log(
            "SUPERSEDE", client="x", memory_id=self.item.id[:8], detail=f"by={newer.id[:8]} at=x"
        )
        (entry,) = web_mod._api_audit({})["entries"]
        self.assertEqual(entry["memory"]["id"], self.item.id)
        self.assertEqual(entry["replacement"]["preview"], "Flight leaves at 3pm.")

    def test_forgotten_memory_is_not_attached(self):
        import elesync.web as web_mod

        self.audit.log("FORGET", client="x", memory_id=self.item.id[:8])
        self.store.delete(self.item.id)
        (entry,) = web_mod._api_audit({})["entries"]
        self.assertNotIn("memory", entry)

    def test_non_integer_last_is_rejected(self):
        import elesync.web as web_mod

        with self.assertRaises(ValueError):
            web_mod._api_audit({"last": ["many"]})


class TestLastBackupAt(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.vault = Path(self.tmp.name)
        self.backups = self.vault / "backups"
        patcher_env = mock.patch.dict(os.environ, {}, clear=False)
        patcher_env.start()
        os.environ.pop("ELESYNC_BACKUP_DIR", None)
        patcher_cfg = mock.patch("elesync.web._load_config", return_value={})
        patcher_cfg.start()
        self.addCleanup(patcher_env.stop)
        self.addCleanup(patcher_cfg.stop)
        self.addCleanup(self.tmp.cleanup)

    def test_no_backups_is_none(self):
        import elesync.web as web_mod

        self.assertIsNone(web_mod._last_backup_at(self.vault))

    def test_newest_backup_on_disk_wins(self):
        import elesync.web as web_mod
        from elesync import archive

        self.backups.mkdir()
        archive.write(
            self.backups / "old.eleb", {"exported_at": "2026-01-01T00:00:00Z", "count": 1}
        )
        archive.write(
            self.backups / "new.eleb", {"exported_at": "2026-02-01T00:00:00Z", "count": 2}
        )
        self.assertEqual(web_mod._last_backup_at(self.vault), "2026-02-01T00:00:00Z")

    def test_manifest_entry_without_file_is_ignored(self):
        import elesync.web as web_mod
        from elesync import backups

        self.backups.mkdir()
        backups.save(
            self.backups,
            {"backups": [{"filename": "gone.eleb", "created_at": "2099-01-01T00:00:00Z"}]},
        )
        self.assertIsNone(web_mod._last_backup_at(self.vault))


# ---------------------------------------------------------------------------
# POST /api/memories  (add)
# ---------------------------------------------------------------------------


class TestAddMemory(WebServerTestCase):
    def test_add_returns_201(self):
        code, data = _post(
            self.port,
            "/api/memories",
            {
                "content": "Test memory from web test",
                "source": "manual",
                "type": "note",
                "tags": ["test"],
            },
        )
        self.assertEqual(code, 201)
        self.assertIn("id", data)
        self.assertEqual(data["content"], "Test memory from web test")
        self.assertEqual(data["source"], "manual")

    def test_add_memory_can_be_bound_to_a_project(self):
        code, project = _post(self.port, "/api/projects", {"name": "Bound project"})
        self.assertEqual(code, 201)
        code, memory = _post(
            self.port,
            "/api/memories",
            {"content": "Project-bound note", "project_id": project["id"]},
        )
        self.assertEqual(code, 201)
        code, context = _get(self.port, f"/api/projects/{project['id']}/context")
        self.assertEqual(code, 200)
        self.assertEqual([item["id"] for item in context["memories"]], [memory["id"]])

    def test_retrying_same_memory_is_idempotent_and_keeps_project_binding(self):
        code, project = _post(self.port, "/api/projects", {"name": "Offline retry project"})
        self.assertEqual(code, 201)
        payload = {"content": "Retry-safe offline note", "project_id": project["id"]}
        code, first = _post(self.port, "/api/memories", payload)
        self.assertEqual(code, 201)
        code, second = _post(self.port, "/api/memories", payload)
        self.assertEqual(code, 201)
        self.assertEqual(second["id"], first["id"])
        code, context = _get(self.port, f"/api/projects/{project['id']}/context")
        self.assertEqual(code, 200)
        self.assertEqual(len(context["memories"]), 1)
        self.assertEqual(context["memories"][0]["id"], first["id"])

    def test_add_memory_rejects_unknown_project_without_writing(self):
        code, _ = _post(
            self.port,
            "/api/memories",
            {"content": "Should not be saved", "project_id": "missing-project"},
        )
        self.assertEqual(code, 400)
        code, data = _get(self.port, "/api/memories?q=Should%20not%20be%20saved")
        self.assertEqual(code, 200)
        self.assertEqual(data["items"], [])

    def test_edit_keeps_memory_bound_to_project(self):
        code, project = _post(self.port, "/api/projects", {"name": "Edit-safe project"})
        self.assertEqual(code, 201)
        code, memory = _post(
            self.port,
            "/api/memories",
            {"content": "Before edit", "project_id": project["id"]},
        )
        self.assertEqual(code, 201)

        code, updated = _put(self.port, f"/api/memories/{memory['id']}", {"content": "After edit"})
        self.assertEqual(code, 200)
        self.assertEqual(updated["content"], "After edit")
        code, context = _get(self.port, f"/api/projects/{project['id']}/context")
        self.assertEqual(code, 200)
        self.assertEqual([item["id"] for item in context["memories"]], [memory["id"]])

    def test_add_empty_content_returns_400(self):
        code, data = _post(self.port, "/api/memories", {"content": ""})
        self.assertEqual(code, 400)

    def test_add_missing_content_returns_400(self):
        code, _ = _post(self.port, "/api/memories", {"source": "manual"})
        self.assertEqual(code, 400)


class TestStoreLease(unittest.TestCase):
    def test_retired_store_closes_only_after_request_releases_it(self):
        import elesync.web as web_mod

        fake_store = mock.Mock()
        with web_mod._state.lock:
            previous = web_mod._state.store
            previous_leases = web_mod._state.store_leases
            previous_retired = web_mod._state.retired_stores
            web_mod._state.store = fake_store
            web_mod._state.store_leases = {}
            web_mod._state.retired_stores = set()
        try:
            leased = web_mod._lease_store()
            web_mod._retire_store(fake_store)
            fake_store.close.assert_not_called()
            web_mod._release_store(leased)
            fake_store.close.assert_called_once_with()
        finally:
            with web_mod._state.lock:
                web_mod._state.store = previous
                web_mod._state.store_leases = previous_leases
                web_mod._state.retired_stores = previous_retired


class TestOAuthConsent(WebServerTestCase):
    def test_registered_name_cannot_choose_the_project_grant(self):
        import elesync.web as web_mod
        from elesync import oauth

        code, project = _post(self.port, "/api/projects", {"name": "OAuth identity"})
        self.assertEqual(code, 201)
        code, _access = _post(
            self.port,
            f"/api/projects/{project['id']}/access",
            {"provider": "chatgpt", "can_read": True},
        )
        self.assertEqual(code, 200)
        client = oauth.register_client(
            web_mod._state.vault_dir,
            "ChatGPT",
            ["https://attacker.example/callback"],
        )
        fields = {
            "response_type": "code",
            "code_challenge_method": "S256",
            "code_challenge": "challenge",
            "client_id": client["client_id"],
            "redirect_uri": "https://attacker.example/callback",
            "project_id": project["id"],
        }

        with self.assertRaisesRegex(ValueError, "Choose which project grant"):
            web_mod._oauth_validate_request(fields)
        fields["provider"] = "chatgpt"
        registered, permissions = web_mod._oauth_validate_request(fields)
        self.assertEqual(registered["name"], "ChatGPT")
        self.assertTrue(permissions["can_read"])
        html = web_mod._oauth_authorize_html(
            "Review this request",
            fields,
            registered,
            web_mod._oauth_active_providers(project["id"]),
        )
        self.assertIn("Unverified client name", html)
        self.assertIn("https://attacker.example", html)
        self.assertEqual(html.count('name="provider"'), 1)


class TestProjectDocuments(WebServerTestCase):
    def test_create_and_read_project_document_context(self):
        code, project = _post(self.port, "/api/projects", {"name": "Japan trip"})
        self.assertEqual(code, 201)

        code, document = _post(
            self.port,
            f"/api/projects/{project['id']}/documents",
            {"title": "Decision", "body": "Book Kyoto flight.", "kind": "decision"},
        )
        self.assertEqual(code, 201)
        self.assertEqual(document["kind"], "decision")

        code, context = _get(self.port, f"/api/projects/{project['id']}/context")
        self.assertEqual(code, 200)
        self.assertEqual(context["documents"][0]["title"], "Decision")

    def test_create_project_document_rejects_non_object_json(self):
        code, project = _post(self.port, "/api/projects", {"name": "Japan trip"})
        self.assertEqual(code, 201)

        code, response = _post(
            self.port,
            f"/api/projects/{project['id']}/documents",
            ["not", "an", "object"],
        )
        self.assertEqual(code, 400)
        self.assertIn("JSON object", response["error"])

    def test_create_project_document_rejects_non_string_fields(self):
        code, project = _post(self.port, "/api/projects", {"name": "Japan trip"})
        self.assertEqual(code, 201)

        code, response = _post(
            self.port,
            f"/api/projects/{project['id']}/documents",
            {"title": None, "body": "Body"},
        )
        self.assertEqual(code, 400)
        self.assertIn("must be strings", response["error"])

    def test_project_activity_returns_persisted_project_events(self):
        code, project = _post(self.port, "/api/projects", {"name": "Japan trip"})
        self.assertEqual(code, 201)
        code, _ = _post(
            self.port,
            f"/api/projects/{project['id']}/documents",
            {"title": "Decision", "body": "Book Kyoto flight.", "kind": "decision"},
        )
        self.assertEqual(code, 201)
        code, memory = _post(
            self.port,
            "/api/memories",
            {"content": "Compare flight prices.", "project_id": project["id"]},
        )
        self.assertEqual(code, 201)

        code, activity = _get(self.port, f"/api/projects/{project['id']}/activity")
        self.assertEqual(code, 200)
        self.assertEqual(
            {event["type"] for event in activity["events"]},
            {"project_created", "document_created", "memory_added"},
        )
        self.assertTrue(
            any(event["title"] == "Added memory to project" for event in activity["events"])
        )

    def test_project_activity_records_task_completion_and_decision_revision(self):
        code, project = _post(self.port, "/api/projects", {"name": "Japan trip"})
        self.assertEqual(code, 201)
        code, task = _post(
            self.port,
            f"/api/projects/{project['id']}/documents",
            {"title": "Book tickets", "body": "Reserve Kyoto rail tickets.", "kind": "task"},
        )
        self.assertEqual(code, 201)
        code, completed = _post(
            self.port,
            f"/api/projects/{project['id']}/documents/{task['id']}/complete",
            {},
        )
        self.assertEqual(code, 200)
        self.assertEqual(completed["status"], "completed")
        code, decision = _post(
            self.port,
            f"/api/projects/{project['id']}/documents",
            {"title": "Transport", "body": "Take the train.", "kind": "decision"},
        )
        self.assertEqual(code, 201)
        code, revised = _put(
            self.port,
            f"/api/projects/{project['id']}/documents/{decision['id']}",
            {"title": "Transport", "body": "Take the night train.", "kind": "decision"},
        )
        self.assertEqual(code, 200)
        self.assertEqual(revised["body"], "Take the night train.")

        code, activity = _get(self.port, f"/api/projects/{project['id']}/activity")
        self.assertEqual(code, 200)
        self.assertIn("task_completed", {event["type"] for event in activity["events"]})
        self.assertIn("decision_revised", {event["type"] for event in activity["events"]})
        self.assertTrue(all(event["actor"] == "Dashboard" for event in activity["events"]))


# ---------------------------------------------------------------------------
# GET /api/memories
# ---------------------------------------------------------------------------


class TestListMemories(WebServerTestCase):
    @classmethod
    def setUpClass(cls):
        super().setUpClass()
        # Seed a few memories
        for i in range(3):
            _post(
                cls.port,
                "/api/memories",
                {
                    "content": f"Seeded memory number {i}",
                    "source": "chatgpt" if i % 2 == 0 else "claude",
                    "type": "fact",
                },
            )

    def test_list_returns_items(self):
        code, data = _get(self.port, "/api/memories")
        self.assertEqual(code, 200)
        self.assertIn("items", data)
        self.assertIn("total", data)
        self.assertGreaterEqual(data["total"], 3)

    def test_list_limit(self):
        _, data = _get(self.port, "/api/memories?limit=2")
        self.assertLessEqual(len(data["items"]), 2)

    def test_summary_list_keeps_large_conversations_compact(self):
        content = "conversation line\n" * 1_000
        _, created = _post(
            self.port,
            "/api/memories",
            {"content": content, "source": "whatsapp", "type": "conversation"},
        )
        _, data = _get(self.port, "/api/memories?summary=1&limit=50")
        item = next(item for item in data["items"] if item["id"] == created["id"])
        self.assertTrue(item["content_truncated"])
        self.assertLess(len(item["content"]), len(content))

    def test_list_filter_source(self):
        _, data = _get(self.port, "/api/memories?source=chatgpt")
        for item in data["items"]:
            self.assertEqual(item["source"], "chatgpt")

    def test_search_query(self):
        _, data = _get(self.port, "/api/memories?q=Seeded")
        self.assertGreater(len(data["items"]), 0)


# ---------------------------------------------------------------------------
# GET /api/memories/<id>
# ---------------------------------------------------------------------------


class TestGetMemory(WebServerTestCase):
    @classmethod
    def setUpClass(cls):
        super().setUpClass()
        _, added = _post(
            cls.port,
            "/api/memories",
            {
                "content": "Memory for get test",
                "source": "manual",
                "type": "note",
            },
        )
        cls.mem_id = added["id"]

    def test_get_existing(self):
        code, data = _get(self.port, f"/api/memories/{self.mem_id}")
        self.assertEqual(code, 200)
        self.assertEqual(data["id"], self.mem_id)
        self.assertEqual(data["content"], "Memory for get test")

    def test_get_includes_only_explicit_project_membership(self):
        _, project = _post(self.port, "/api/projects", {"name": "Inspector project"})
        project_id = project["id"]
        _post(self.port, f"/api/projects/{project_id}/memories", {"memory_id": self.mem_id})
        code, data = _get(self.port, f"/api/memories/{self.mem_id}")
        self.assertEqual(code, 200)
        self.assertIn({"id": project_id, "name": "Inspector project"}, data["projects"])
        _, other = _post(self.port, "/api/memories", {"content": "Unassigned inspector note"})
        _, data = _get(self.port, f"/api/memories/{other['id']}")
        self.assertEqual(data["projects"], [])

    def test_get_nonexistent_404(self):
        code, _ = _get(self.port, "/api/memories/doesnotexist0000")
        self.assertEqual(code, 404)


# ---------------------------------------------------------------------------
# PUT /api/memories/<id>  (update)
# ---------------------------------------------------------------------------


class TestUpdateMemory(WebServerTestCase):
    @classmethod
    def setUpClass(cls):
        super().setUpClass()
        _, added = _post(
            cls.port,
            "/api/memories",
            {
                "content": "Original content",
                "source": "manual",
                "type": "note",
            },
        )
        cls.mem_id = added["id"]

    def test_update_content(self):
        code, data = _put(self.port, f"/api/memories/{self.mem_id}", {"content": "Updated content"})
        self.assertEqual(code, 200)
        self.assertEqual(data["content"], "Updated content")

    def test_update_tags(self):
        _, data = _put(self.port, f"/api/memories/{self.mem_id}", {"tags": ["newtag", "another"]})
        self.assertIn("newtag", data["tags"])

    def test_update_nonexistent_404(self):
        code, _ = _put(self.port, "/api/memories/nope0000nope0000", {"content": "x"})
        self.assertEqual(code, 404)


# ---------------------------------------------------------------------------
# DELETE /api/memories/<id>
# ---------------------------------------------------------------------------


class TestDeleteMemory(WebServerTestCase):
    def test_delete_existing(self):
        _, added = _post(
            self.port,
            "/api/memories",
            {
                "content": "Memory to be deleted",
                "source": "manual",
                "type": "note",
            },
        )
        mem_id = added["id"]
        code, data = _delete(self.port, f"/api/memories/{mem_id}")
        self.assertEqual(code, 200)
        self.assertTrue(data["deleted"])
        # Confirm it's gone
        code2, _ = _get(self.port, f"/api/memories/{mem_id}")
        self.assertEqual(code2, 404)

    def test_delete_nonexistent_404(self):
        code, _ = _delete(self.port, "/api/memories/ghost0000ghost000")
        self.assertEqual(code, 404)


# ---------------------------------------------------------------------------
# POST /api/import
# ---------------------------------------------------------------------------


class TestImport(WebServerTestCase):
    def test_import_chatgpt_format(self):
        payload = {
            "data": {"memories": ["I prefer Python over Java", "My name is Alex"]},
            "source": "chatgpt",
        }
        code, data = _post(self.port, "/api/import", payload)
        self.assertEqual(code, 200)
        self.assertIn("added", data)
        self.assertGreaterEqual(data["added"], 2)

    def test_import_empty_returns_zero(self):
        payload = {"data": {"memories": []}, "source": "chatgpt"}
        code, data = _post(self.port, "/api/import", payload)
        self.assertEqual(code, 200)
        self.assertEqual(data["added"], 0)

    def test_import_missing_data_400(self):
        code, _ = _post(self.port, "/api/import", {"source": "chatgpt"})
        self.assertEqual(code, 400)

    def test_import_raw_text_csv_copilot_shape(self):
        csv_text = (
            "Conversation,Time,Author,Message\r\n"
            "Trip planning,2026-01-05T10:02:00Z,User,What's a good week in Japan?\r\n"
            "Trip planning,2026-01-05T10:01:00Z,Bot,Here's a 7-day Japan itinerary.\r\n"
        )
        code, data = _post(
            self.port,
            "/api/import",
            {"raw_text": csv_text, "source": "copilot", "filename": "copilot-export.csv"},
        )
        self.assertEqual(code, 200)
        self.assertGreaterEqual(data["added"], 1)

    def test_import_preview_detects_copilot_columns(self):
        payload = {
            "raw_text": "ChatName,CreatedAt,Author,MessageContent\nTrip,2026-01-01,User,Hello\n",
            "filename": "copilot-export.csv",
        }
        code, data = _post(self.port, "/api/import/preview", payload)
        self.assertEqual(code, 200)
        self.assertTrue(data["supported"])
        self.assertEqual(data["format"], "copilot")

    def test_import_raw_text_plaintext(self):
        code, data = _post(
            self.port,
            "/api/import",
            {"raw_text": "I like hiking.", "filename": "notes.txt"},
        )
        self.assertEqual(code, 200)
        self.assertGreaterEqual(data["added"], 1)

    def test_import_pasted_text(self):
        code, data = _post(
            self.port,
            "/api/import",
            {"text": "I prefer concise answers.", "source": "manual"},
        )
        self.assertEqual(code, 200)
        self.assertGreaterEqual(data["added"], 1)


# ---------------------------------------------------------------------------
# GET /api/stats  (enhanced — v1.7.x fields)
# ---------------------------------------------------------------------------


class TestStatsEnhanced(WebServerTestCase):
    """Stats endpoint now surfaces conflict count, scope counts, encryption
    status, and auth status — all the v1.6.0+ features the dashboard needs."""

    def test_stats_has_conflict_count(self):
        code, data = _get(self.port, "/api/stats")
        self.assertEqual(code, 200)
        self.assertIn("conflicts", data)
        self.assertIsInstance(data["conflicts"], int)

    def test_stats_has_scope_counts(self):
        _, data = _get(self.port, "/api/stats")
        self.assertIn("scope_counts", data)
        self.assertIsInstance(data["scope_counts"], dict)

    def test_stats_has_encryption_status(self):
        _, data = _get(self.port, "/api/stats")
        self.assertIn("encrypted", data)
        self.assertIsInstance(data["encrypted"], bool)

    def test_stats_has_auth_status(self):
        _, data = _get(self.port, "/api/stats")
        self.assertIn("auth_enabled", data)
        self.assertIsInstance(data["auth_enabled"], bool)


# ---------------------------------------------------------------------------
# GET /api/conflicts
# ---------------------------------------------------------------------------


class TestConflictsEndpoint(WebServerTestCase):
    @classmethod
    def setUpClass(cls):
        super().setUpClass()
        # Seed conflicting memories — same topic, different values
        for content, source in [
            ("User lives in Barcelona", "chatgpt"),
            ("User lives in Madrid", "claude"),
        ]:
            _post(
                cls.port,
                "/api/memories",
                {"content": content, "source": source, "type": "fact"},
            )

    def test_conflicts_shape(self):
        code, data = _get(self.port, "/api/conflicts")
        self.assertEqual(code, 200)
        self.assertIn("conflicts", data)
        self.assertIn("total", data)
        self.assertIsInstance(data["conflicts"], list)
        self.assertIsInstance(data["total"], int)

    def test_conflict_cluster_has_items(self):
        _, data = _get(self.port, "/api/conflicts")
        if data["total"] > 0:
            cluster = data["conflicts"][0]
            self.assertIn("cluster_id", cluster)
            self.assertIn("items", cluster)
            self.assertGreaterEqual(len(cluster["items"]), 2)
            item = cluster["items"][0]
            for key in ("id", "content", "source", "created_at", "type"):
                self.assertIn(key, item, f"conflict item missing '{key}'")


# ---------------------------------------------------------------------------
# POST /api/conflicts/resolve
# ---------------------------------------------------------------------------


class TestConflictResolve(WebServerTestCase):
    @classmethod
    def setUpClass(cls):
        super().setUpClass()
        for content, source in [
            ("User's dog is named Max", "chatgpt"),
            ("User's dog is named Rex", "claude"),
        ]:
            _post(
                cls.port,
                "/api/memories",
                {"content": content, "source": source, "type": "fact"},
            )

    def test_resolve_with_strategy(self):
        _, data = _get(self.port, "/api/conflicts")
        if data["total"] == 0:
            self.skipTest("no conflicts to resolve (embedder-dependent)")
        cluster = data["conflicts"][0]
        code, result = _post(
            self.port,
            "/api/conflicts/resolve",
            {"cluster_id": cluster["cluster_id"], "strategy": "newest"},
        )
        self.assertEqual(code, 200)
        self.assertIn("result", result)

    def test_resolve_missing_params_400(self):
        code, _ = _post(self.port, "/api/conflicts/resolve", {"cluster_id": "abc"})
        self.assertEqual(code, 200)  # returns error message in result, not HTTP 400
        # The service returns a helpful string, not an exception


# ---------------------------------------------------------------------------
# GET /api/scopes
# ---------------------------------------------------------------------------


class TestScopesEndpoint(WebServerTestCase):
    @classmethod
    def setUpClass(cls):
        super().setUpClass()
        # Add scoped memories
        _post(
            cls.port,
            "/api/memories",
            {"content": "Scoped to work", "source": "manual", "scopes": "work"},
        )
        _post(
            cls.port,
            "/api/memories",
            {"content": "General memory", "source": "manual"},
        )

    def test_scopes_shape(self):
        code, data = _get(self.port, "/api/scopes")
        self.assertEqual(code, 200)
        self.assertIn("scopes", data)
        self.assertIsInstance(data["scopes"], dict)

    def test_scopes_reflect_added_memories(self):
        _, data = _get(self.port, "/api/scopes")
        scopes = data["scopes"]
        # Should have at least 'general' and 'work'
        self.assertIn("general", scopes)
        self.assertGreaterEqual(scopes["general"], 1)


# ---------------------------------------------------------------------------
# GET /api/export
# ---------------------------------------------------------------------------


class TestExportEndpoint(WebServerTestCase):
    @classmethod
    def setUpClass(cls):
        super().setUpClass()
        for i in range(3):
            _post(
                cls.port,
                "/api/memories",
                {"content": f"Export test memory {i}", "source": "manual"},
            )

    def test_export_shape(self):
        code, data = _get(self.port, "/api/export")
        self.assertEqual(code, 200)
        self.assertIn("memories", data)
        self.assertIn("total", data)
        self.assertIn("exported_at", data)
        self.assertIn("vault_dir", data)

    def test_export_includes_all_memories(self):
        _, stats = _get(self.port, "/api/stats")
        _, export = _get(self.port, "/api/export")
        self.assertEqual(export["total"], stats["total"])

    def test_export_memory_keys(self):
        _, data = _get(self.port, "/api/export")
        if data["total"] > 0:
            mem = data["memories"][0]
            for key in ("id", "content", "source", "type", "tags", "created_at", "scopes"):
                self.assertIn(key, mem, f"exported memory missing '{key}'")


if __name__ == "__main__":
    unittest.main()


# ---------------------------------------------------------------------------
# /api/connect/auto  (one-click local client wiring)
# ---------------------------------------------------------------------------


class TestConnectAuto(WebServerTestCase):
    """POST /api/connect/auto detects installed local MCP clients and writes
    the EleSync server entry into each one's config — no command to copy."""

    def test_auto_connect_returns_wired_list(self):
        # detect_local_clients probes real config dirs on the test machine, so
        # we monkeypatch it to a controlled fake so the test is deterministic.
        import unittest.mock as mock

        import elesync.onboarding as onb

        with tempfile.TemporaryDirectory() as d:
            fake_cfg = Path(d) / "claude_desktop_config.json"
            fake_detected = [
                {
                    "target": "claude-desktop",
                    "label": "Claude Desktop",
                    "config_path": str(fake_cfg),
                    "installed": True,
                }
            ]
            with mock.patch.object(onb, "detect_local_clients", return_value=fake_detected):
                code, data = _post(self.port, "/api/connect/auto", {})
            self.assertEqual(code, 200)
            self.assertEqual(data["count"], 1)
            self.assertEqual(len(data["wired"]), 1)
            self.assertEqual(data["wired"][0]["client"], "Claude Desktop")
            self.assertEqual(data["wired"][0]["target"], "claude-desktop")
            self.assertFalse(data["wired"][0]["already_wired"])  # first wire
            # The config file was actually written by wire_client_config.
            self.assertTrue(fake_cfg.exists())

    def test_project_connection_command_binds_claude_to_project(self):
        code, project = _post(self.port, "/api/projects", {"name": "Claude project"})
        self.assertEqual(code, 201)
        code, plan = _post(
            self.port,
            "/api/connect",
            {"client": "claude", "project_id": project["id"]},
        )
        self.assertEqual(code, 200)
        self.assertIn("--project", plan["command"])
        self.assertIn(project["id"], plan["command"])
        self.assertIn(f"elesync-project-{project['id'][:8]}", plan["command"])
        self.assertIn("Claude entry name", " ".join(plan["steps"]))

    def test_auto_connect_empty_when_no_clients_found(self):
        import unittest.mock as mock

        import elesync.onboarding as onb

        with mock.patch.object(onb, "detect_local_clients", return_value=[]):
            code, data = _post(self.port, "/api/connect/auto", {})
        self.assertEqual(code, 200)
        self.assertEqual(data["count"], 0)
        self.assertEqual(data["wired"], [])

    def test_auto_connect_marks_already_wired_on_second_run(self):
        import unittest.mock as mock

        import elesync.onboarding as onb

        with tempfile.TemporaryDirectory() as d:
            fake_cfg = Path(d) / "claude_desktop_config.json"
            fake_detected = [
                {
                    "target": "claude-desktop",
                    "label": "Claude Desktop",
                    "config_path": str(fake_cfg),
                    "installed": True,
                }
            ]
            with mock.patch.object(onb, "detect_local_clients", return_value=fake_detected):
                _post(self.port, "/api/connect/auto", {})
                code2, data2 = _post(self.port, "/api/connect/auto", {})
            self.assertEqual(code2, 200)
            self.assertTrue(data2["wired"][0]["already_wired"])


# ---------------------------------------------------------------------------
# /api/vault/switch  (regression: key mismatch + atomic swap)
# ---------------------------------------------------------------------------


class TestVaultSwitch(WebServerTestCase):
    """POST /api/vault/switch accepts the canonical ``vault_dir`` key (and a
    ``path`` fallback), swaps the live store atomically, and leaves other read
    endpoints working — guarding the bug where the switch closed the live store
    out from under concurrent requests and hung the dashboard."""

    def tearDown(self):
        # Restore the class's original vault so a per-test target directory can
        # be cleaned up without a lingering open DB handle (matters on Windows).
        import elesync.web as web_mod
        from elesync.store import MemoryStore

        origin = str(Path(self.vault_tmp.name))
        if web_mod._state.vault_dir != origin:
            if web_mod._state.store:
                web_mod._state.store.close()
            web_mod._state.store = MemoryStore(Path(origin))
            web_mod._state.vault_dir = origin

    def _make_vault(self) -> str:
        from elesync.store import MemoryStore

        tmp = tempfile.TemporaryDirectory()
        self.addCleanup(tmp.cleanup)
        p = Path(tmp.name)
        MemoryStore(p).close()  # materialise the vault (notes/, index.db, meta)
        return str(p)

    def test_switch_requires_target(self):
        code, data = _post(self.port, "/api/vault/switch", {})
        self.assertEqual(code, 400)
        self.assertIn("vault_dir", data.get("error", "").lower())

    def test_switch_missing_vault_errors(self):
        code, data = _post(
            self.port, "/api/vault/switch", {"vault_dir": "/no/such/elesync-vault-xyz"}
        )
        self.assertEqual(code, 400)
        self.assertTrue(data.get("error"))

    def test_switch_with_vault_dir_key(self):
        target = self._make_vault()
        code, data = _post(self.port, "/api/vault/switch", {"vault_dir": target})
        self.assertEqual(code, 200)
        self.assertEqual(data["vault_dir"], target)
        self.assertIn("stats", data)
        # The live vault endpoint reflects the swap...
        _, v = _get(self.port, "/api/vault")
        self.assertEqual(v["vault_dir"], target)
        # ...and reads still succeed against the new store (no closed/half-open).
        code_stats, _ = _get(self.port, "/api/stats")
        self.assertEqual(code_stats, 200)

    def test_switch_with_path_fallback_key(self):
        target = self._make_vault()
        code, data = _post(self.port, "/api/vault/switch", {"path": target})
        self.assertEqual(code, 200)
        self.assertEqual(data["vault_dir"], target)


# ---------------------------------------------------------------------------
# /api/backup  ("Back up now")
# ---------------------------------------------------------------------------


class TestBackupNow(WebServerTestCase):
    """The dashboard's only advice was "run ele backup", and installer users have
    no terminal and no `ele` on their PATH."""

    def setUp(self):
        import os
        from unittest.mock import patch

        appdata = tempfile.TemporaryDirectory()
        self.addCleanup(appdata.cleanup)
        patcher = patch.dict(os.environ, {"APPDATA": appdata.name})
        patcher.start()
        self.addCleanup(patcher.stop)
        os.environ.pop("ELESYNC_BACKUP_DIR", None)

    def test_back_up_now_writes_the_managed_backup_the_dashboard_counts(self):
        _post(self.port, "/api/memories", {"content": "Backup me", "type": "note"})

        code, result = _post(self.port, "/api/backup", {})

        self.assertEqual(code, 201)
        path = Path(result["path"])
        self.assertTrue(path.is_file())
        self.assertEqual(path.parent.resolve(), (Path(self.vault_tmp.name) / "backups").resolve())
        self.assertGreaterEqual(result["memories"], 1)
        _, stats = _get(self.port, "/api/stats")
        self.assertEqual(stats["last_backup_at"], result["created_at"])

    def test_a_copy_saved_from_the_vault_page_counts_as_a_backup(self):
        import elesync.web as web_mod

        vault = Path(self.vault_tmp.name)
        web_mod._record_vault_copy(vault)

        copied = web_mod._load_config()["backup"]["copies"][str(vault.resolve())]
        self.assertEqual(web_mod._last_backup_at(vault), copied)

    def test_dashboard_offers_the_button_instead_of_the_command(self):
        with urlopen(_url(self.port, "/"), timeout=5) as r:
            html = r.read().decode()
        scripts = {}
        for name in ("dashboard.js", "dashboard-connect.js"):
            with urlopen(_url(self.port, f"/static/{name}"), timeout=5) as r:
                scripts[name] = r.read().decode()
        self.assertIn('onclick="backupNow(this)">Back up now</button>', html)
        self.assertNotIn("from a terminal: ele backup", scripts["dashboard.js"])
        self.assertNotIn("Create a new one with: ele backup", scripts["dashboard.js"])
        self.assertNotIn("run ele backup", scripts["dashboard-connect.js"])
        self.assertIn("api('/api/backup', { method:'POST'", scripts["dashboard.js"])


# ---------------------------------------------------------------------------
# /api/setup  (installer first-run handoff)
# ---------------------------------------------------------------------------


class TestFirstRunSetup(WebServerTestCase):
    def setUp(self):
        import shutil

        import elesync.web as web_mod
        import elesync.web_auth as web_auth

        auth_dir = web_mod.web_auth_dir(self.vault_tmp.name)
        shutil.rmtree(auth_dir, ignore_errors=True)
        web_mod._state.user_mgr = web_auth.UserManager(auth_dir)
        web_mod._state.session_mgr = web_auth.SessionManager(auth_dir, require_existing_user=True)

    def test_empty_config_is_treated_as_first_run(self):
        import os
        from unittest.mock import patch

        with tempfile.TemporaryDirectory() as appdata:
            with patch.dict(os.environ, {"APPDATA": appdata}, clear=False):
                code, setup = _get(self.port, "/api/setup")

        self.assertEqual(code, 200)
        self.assertTrue(setup["first_run"])

    def test_update_check_cache_alone_is_still_a_first_run(self):
        # The dashboard caches its update check in config.json on first load,
        # which used to hide the setup wizard from every source install.
        import elesync.web as web_mod

        cached = {"update_check": {"last_check": "2026-09-28T21:31:52+00:00"}}
        with mock.patch.object(web_mod, "_load_config", return_value=cached):
            self.assertTrue(web_mod._api_setup_status()["first_run"])
        # Anything the user configured still means setup already happened.
        configured = {**cached, "vault_dir": web_mod._state.vault_dir}
        with mock.patch.object(web_mod, "_load_config", return_value=configured):
            self.assertFalse(web_mod._api_setup_status()["first_run"])

    def test_protected_vault_with_empty_config_does_not_reopen_setup(self):
        import elesync.web as web_mod

        web_mod._state.user_mgr.add_user("owner", "correct-horse", role="admin")
        with mock.patch.object(web_mod, "_load_config", return_value={}):
            setup = web_mod._api_setup_status()

        self.assertTrue(setup["auth_enabled"])
        self.assertFalse(setup["first_run"])

    def test_existing_protection_can_skip_setup_and_records_completion(self):
        import elesync.web as web_mod

        web_mod._state.user_mgr.add_user("owner", "correct-horse", role="admin")
        saved: dict = {}
        with (
            mock.patch.object(web_mod, "_load_config", return_value=saved),
            mock.patch.object(web_mod, "_save_config") as save_config,
        ):
            data = web_mod._api_setup_complete(
                {"vault_dir": web_mod._state.vault_dir, "protect_dashboard": False}
            )

        self.assertTrue(data["auth_enabled"])
        self.assertFalse(saved["first_run_setup"])
        save_config.assert_called_once_with(saved)
        self.assertTrue(web_mod._state.user_mgr.authenticate("owner", "correct-horse"))

    def test_setup_complete_can_enable_security_after_first_run(self):
        import os
        from unittest.mock import patch

        import elesync.web as web_mod

        with tempfile.TemporaryDirectory() as appdata:
            cfg = Path(appdata) / "EleSync" / "config.json"
            cfg.parent.mkdir(parents=True)
            cfg.write_text(json.dumps({"vault_dir": web_mod._state.vault_dir}), encoding="utf-8")
            with patch.dict(os.environ, {"APPDATA": appdata}, clear=False):
                code, data = _post(
                    self.port,
                    "/api/setup/complete",
                    {"protect_dashboard": True, "passcode": "correct-horse"},
                )

        self.assertEqual(code, 200)
        self.assertTrue(data["auth_enabled"])

    def test_setup_complete_creates_owner_passcode_and_clears_installer_marker(self):
        import os
        from unittest.mock import patch

        import elesync.web as web_mod

        with tempfile.TemporaryDirectory() as appdata:
            cfg = Path(appdata) / "EleSync" / "config.json"
            cfg.parent.mkdir(parents=True)
            cfg.write_text(
                json.dumps({"vault_dir": web_mod._state.vault_dir, "first_run_setup": True}),
                encoding="utf-8",
            )
            with patch.dict(os.environ, {"APPDATA": appdata}, clear=False):
                code, setup = _get(self.port, "/api/setup")
                self.assertEqual(code, 200)
                self.assertTrue(setup["first_run"])

                code, data = _post(
                    self.port,
                    "/api/setup/complete",
                    {"protect_dashboard": True, "passcode": "correct-horse"},
                )
                self.assertEqual(code, 200)
                self.assertTrue(data["auth_enabled"])

                setup = web_mod._api_setup_status()
                self.assertFalse(setup["first_run"])

                saved = json.loads(cfg.read_text(encoding="utf-8"))
                self.assertFalse(saved["first_run_setup"])
                self.assertTrue(web_mod._state.user_mgr.authenticate("owner", "correct-horse"))

    def test_setup_complete_refuses_to_claim_success_when_config_cannot_persist(self):
        import elesync.web as web_mod

        with (
            mock.patch.object(web_mod, "_load_config", return_value={}),
            mock.patch.object(web_mod, "_save_config", return_value=False),
        ):
            with self.assertRaisesRegex(OSError, "could not save setup"):
                web_mod._api_setup_complete({"protect_dashboard": False})


# ---------------------------------------------------------------------------
# /api/connect  (dashboard AI-client setup planner)
# ---------------------------------------------------------------------------


class TestConnectAPI(unittest.TestCase):
    def setUp(self):
        from unittest.mock import patch

        import elesync.config as config_mod
        import elesync.web as web_mod

        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        self.cfg_path = Path(self.tmp.name) / "config.json"
        self.old_vault = web_mod._state.vault_dir
        web_mod._state.vault_dir = str(Path(self.tmp.name) / "vault")
        patcher = patch.object(config_mod, "config_path", return_value=self.cfg_path)
        self.addCleanup(patcher.stop)
        patcher.start()
        self.addCleanup(setattr, web_mod._state, "vault_dir", self.old_vault)
        self._saved_env = os.environ.copy()
        os.environ.pop("ELESYNC_TOKEN", None)

    def tearDown(self):
        os.environ.clear()
        os.environ.update(self._saved_env)

    def test_remote_connect_plan_generates_token_and_commands(self):
        from unittest.mock import patch

        import elesync.web as web_mod

        with patch.dict(os.environ, {"ELESYNC_TOKEN": ""}, clear=False):
            plan = web_mod._api_connect_client(
                {"client": "grok", "allowed_host": "grok.example.test", "port": 9000}
            )
        self.assertEqual(plan["kind"], "remote")
        self.assertEqual(plan["server_url"], "https://grok.example.test/mcp")
        self.assertIn("--transport http --port 9000", plan["serve_command"])
        self.assertIn("Authorization", " ".join(plan["steps"]))
        cfg = json.loads(self.cfg_path.read_text(encoding="utf-8"))
        self.assertEqual(plan["token"], cfg["http"]["auth"]["token"])

    def test_remote_connect_force_token_rejected_when_env_token_is_set(self):
        from unittest.mock import patch

        import elesync.web as web_mod

        with patch.dict(os.environ, {"ELESYNC_TOKEN": "from-env"}, clear=False):
            with self.assertRaisesRegex(
                ValueError, "Cannot replace token while ELESYNC_TOKEN is set"
            ):
                web_mod._api_connect_client(
                    {
                        "client": "grok",
                        "allowed_host": "grok.example.test",
                        "port": 9000,
                        "force_token": True,
                    }
                )

    def test_remote_connect_plan_shell_quotes_metacharacters(self):
        import elesync.web as web_mod

        host = "grok$(touch pwned).example.test"
        plan = web_mod._api_connect_client({"client": "grok", "allowed_host": host, "port": 9000})
        self.assertIn(f"--allowed-host {web_mod._quote_cli_arg(host)}", plan["serve_command"])

    def test_remote_connect_plan_windows_quotes_metacharacters_for_powershell(self):
        from unittest.mock import patch

        import elesync.web as web_mod

        with patch.object(web_mod.os, "name", "nt"):
            plan = web_mod._api_connect_client(
                {"client": "grok", "allowed_host": "grok&calc.example.test", "port": 9000}
            )
        self.assertIn("--allowed-host 'grok&calc.example.test'", plan["serve_command"])

    def test_local_connect_plan_uses_onboard_target(self):
        import elesync.web as web_mod

        plan = web_mod._api_connect_client({"client": "claude"})
        self.assertEqual(plan["kind"], "local")
        self.assertIn("onboard --target claude-desktop", plan["command"])

    def test_gemini_key_is_verified_before_it_is_saved(self):
        import elesync.web as web_mod

        with (
            mock.patch("elesync.gemini.verify_api_key", return_value="ELE_SYNC_OK"),
            mock.patch("elesync.credential_store.set_secret") as set_secret,
        ):
            result = web_mod._api_connect_gemini({"api_key": "a" * 24})
        self.assertTrue(result["connected"])
        self.assertEqual(result["provider"], "gemini")
        set_secret.assert_called_once_with("gemini-api-key", "a" * 24)
        saved = json.loads(self.cfg_path.read_text(encoding="utf-8"))
        self.assertNotIn("gemini_api_key", saved["llm"])
        self.assertTrue(saved["llm"]["gemini_verified_at"])


# ---------------------------------------------------------------------------
# Served dashboard JS integrity (regression: escapes eaten by triple-quoted _HTML)
# ---------------------------------------------------------------------------


class TestDashboardJsIntegrity(unittest.TestCase):
    """The dashboard JS is served from ``static/dashboard.js``. It used to live
    inside the triple-quoted ``_HTML`` string, where a backslash escape written
    as ``\\n`` was consumed by Python and became a real newline in the served
    script — aborting the whole <script> and killing every handler (the
    CSV-export ``join`` hit exactly this and blanked the UI). Moving it into a
    real file removes that hazard by construction; these checks run against the
    served script content wherever it lives."""

    def _script(self) -> str:
        from pathlib import Path

        import elesync.web as web

        static = Path(web.__file__).parent / "static"
        return "\n\n".join(
            (static / name).read_text(encoding="utf-8")
            for name in (
                "dashboard-api.js",
                "dashboard-onboarding.js",
                "dashboard-projects.js",
                "dashboard-connect.js",
                "dashboard-import.js",
                "dashboard.js",
            )
        )

    def _js_functions(self) -> dict:
        """Map every JS function name in the served script to its body."""
        import re

        js = self._script()
        out = {}
        for m in re.finditer(r"(?:async\s+)?function\s+(\w+)\s*\([^)]*\)\s*\{", js):
            i = m.end() - 1
            depth = 0
            for j in range(i, len(js)):
                if js[j] == "{":
                    depth += 1
                elif js[j] == "}":
                    depth -= 1
                    if depth == 0:
                        break
            out[m.group(1)] = js[i : j + 1]
        return out

    def test_every_skeleton_reaches_a_terminal_state(self):
        """A skeleton loader with no error path spins forever on a failed fetch.

        ``api()`` throws on network error, non-2xx and timeout, and there is no
        global unhandledrejection handler — so an unguarded ``await api()``
        after a skeleton leaves the user staring at pulsing bars that *look*
        like progress. Every function that raises a skeleton must therefore
        either catch, or delegate to ``withLoader`` (which catches for it).
        """
        offenders = []
        for name, body in self._js_functions().items():
            if name in ("skel", "withLoader"):
                continue
            raises_skeleton = 'class="skel"' in body or "skel(" in body or "withLoader(" in body
            if not raises_skeleton:
                continue
            if "catch" not in body and "withLoader(" not in body:
                offenders.append(name)
        self.assertEqual(
            offenders,
            [],
            f"skeleton loaders with no terminal error state: {offenders}",
        )

    def test_csv_export_join_is_escaped(self):
        js = self._script()

        self.assertIn(".join('\\n')", js)  # correct: JS newline escape
        self.assertNotIn(".join('\n')", js)  # broken: real newline

    def test_dashboard_assets_are_external_not_inlined(self):
        """The JS and CSS must be referenced, never inlined. Inlining is what let
        a Python-eaten backslash escape kill the script and, separately, let
        attacker-controlled data reach an executable context (the USB-name XSS).
        Keeping them in real files served verbatim removes both bug classes."""
        import elesync.web as web

        self.assertNotIn("<style>", web._HTML)
        self.assertNotIn("<script>", web._HTML)
        self.assertIn('<script src="/static/dashboard.js"></script>', web._HTML)
        self.assertIn('<script src="/static/dashboard-onboarding.js"></script>', web._HTML)
        self.assertIn('<script src="/static/dashboard-projects.js"></script>', web._HTML)
        self.assertIn('<script src="/static/dashboard-connect.js"></script>', web._HTML)
        self.assertIn('<script src="/static/dashboard-import.js"></script>', web._HTML)
        self.assertIn('<link rel="stylesheet" href="/static/dashboard.css">', web._HTML)

    def test_usb_install_collects_and_sends_passphrase(self):
        """USB vaults are always encrypted, so the dashboard install flow must
        collect a passphrase and POST it under the keys the server reads.

        Regression: ``usbInstall`` previously fired ``/api/usb/install`` with
        just ``{path}`` — no passphrase, and the wrong key (server reads
        ``drive``/``passphrase``). The request always failed with 'drive and
        passphrase are required', and there was no passphrase field in the UI
        at all, so a USB vault could not be encrypted from the dashboard."""
        import elesync.web as web

        fns = self._js_functions()
        self.assertIn("usbInstallConfirm", fns, "USB install confirm handler is missing")
        confirm = fns["usbInstallConfirm"]
        self.assertIn("/api/usb/install", confirm)
        self.assertIn("drive:", confirm, "install POST must send the 'drive' key the server reads")
        self.assertIn("passphrase:", confirm, "install POST must send a passphrase")
        # The passphrase + confirm inputs must exist in the served markup.
        self.assertIn('id="usb-install-pass"', web._HTML)
        self.assertIn('id="usb-install-pass2"', web._HTML)

    def test_first_run_setup_modal_wires_passcode_and_usb_next_step(self):
        import elesync.web as web

        fns = self._js_functions()
        self.assertIn("maybeShowSetup", fns)
        self.assertIn("setupSubmit", fns)
        self.assertIn("/api/setup", fns["maybeShowSetup"])
        self.assertIn("/api/setup/complete", fns["setupSubmit"])
        self.assertIn("protect_dashboard", fns["setupSubmit"])
        self.assertIn("go('usb')", fns["setupSubmit"])
        self.assertIn("Retry setup", fns["setupSubmit"])
        self.assertIn("timeoutMs:15000", fns["setupSubmit"])
        self.assertIn("setup-skip-btn", fns["setupSubmit"])
        self.assertIn("setupShowError", fns["maybeShowSetup"])
        self.assertIn('id="setup-dialog"', web._HTML)
        self.assertIn('id="setup-protect"', web._HTML)
        self.assertIn('id="setup-usb"', web._HTML)

    def test_connect_ai_page_wires_supported_clients(self):
        import elesync.web as web

        fns = self._js_functions()
        self.assertIn("loadConnect", fns)
        self.assertIn("connectGenerate", fns)
        self.assertIn("/api/connect", fns["connectGenerate"])
        self.assertIn('id="page-connect"', web._HTML)
        self.assertIn('id="connect-grid"', web._HTML)
        for token in ("chatgpt", "grok", "gemini", "cursor", "claude"):
            self.assertIn(token, self._script())
        self.assertIn("AI collaborators", web._HTML)

    def test_served_js_parses(self):
        """Full parse of the served dashboard script. Skips if node is absent."""
        import os
        import shutil
        import subprocess
        import tempfile

        node = shutil.which("node")
        if not node:
            self.skipTest("node not available to parse-check JS")
        with tempfile.NamedTemporaryFile("w", suffix=".js", delete=False, encoding="utf-8") as f:
            f.write(self._script())
            path = f.name
        try:
            r = subprocess.run([node, "--check", path], capture_output=True, text=True)
            self.assertEqual(r.returncode, 0, f"served dashboard JS failed to parse:\n{r.stderr}")
        finally:
            os.unlink(path)


# ---------------------------------------------------------------------------
# ---------------------------------------------------------------------------
# /api/audit  (Overview activity stream)
# ---------------------------------------------------------------------------


class TestAuditEndpoint(WebServerTestCase):
    def test_audit_disabled_when_no_log(self):
        code, data = _get(self.port, "/api/audit")
        self.assertEqual(code, 200)
        self.assertIn("enabled", data)
        self.assertIn("entries", data)
        self.assertEqual(data["entries"], [])

    def test_audit_reads_existing_log(self):
        import elesync.web as web_mod
        from elesync.audit import AuditLog

        audit = AuditLog(web_mod._state.vault_dir)
        audit.log(operation="REMEMBER", client="claude", memory_id="m1", detail="note")
        audit.log(operation="RECALL", client="chatgpt", memory_id="", detail="q")
        code, data = _get(self.port, "/api/audit?last=5")
        self.assertEqual(code, 200)
        self.assertTrue(data["enabled"])
        self.assertGreaterEqual(len(data["entries"]), 2)
        self.assertEqual(data["entries"][0]["op"], "RECALL")  # newest first
        for e in data["entries"]:
            self.assertIn("ts", e)
            self.assertIn("client", e)


class TestRelayConflictGroupingCache(unittest.TestCase):
    """The relay asks for open conflict groups every few seconds; grouping scans
    every fact pairwise, so it must only be recomputed when the vault changes."""

    def setUp(self):
        from elesync.store import MemoryStore

        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        self.store = MemoryStore(self.tmp.name)
        self.addCleanup(self.store.close)

    def test_groups_are_reused_until_the_vault_changes(self):
        import elesync.web as web_mod
        from elesync.models import MemoryItem
        from elesync.service import MemoryService

        self.store.upsert(MemoryItem(content="The flight leaves at noon.", type="fact"))
        self.store.upsert(MemoryItem(content="The flight leaves at 3pm.", type="fact"))
        real = MemoryService._get_clusters
        with mock.patch.object(
            MemoryService, "_get_clusters", autospec=True, side_effect=real
        ) as grouping:
            first = web_mod._open_conflict_clusters(self.store)
            for _ in range(5):
                self.assertEqual(web_mod._open_conflict_clusters(self.store), first)
            self.assertEqual(grouping.call_count, 1)
            self.assertEqual(len(first), 1)

            self.store.upsert(MemoryItem(content="The flight leaves at 5pm.", type="fact"))
            changed = web_mod._open_conflict_clusters(self.store)
            self.assertEqual(grouping.call_count, 2)
            self.assertEqual(len(changed[0][1]), 3)

    def test_an_edit_that_keeps_the_length_still_regroups(self):
        import elesync.web as web_mod
        from elesync.models import MemoryItem
        from elesync.service import MemoryService

        self.store.upsert(MemoryItem(content="Based in Oakland", type="fact", source="claude"))
        other = MemoryItem(content="Based in Seattle", type="fact", source="chatgpt")
        self.store.upsert(other)
        self.assertEqual(len(web_mod._open_conflict_clusters(self.store)), 1)

        # The owner settles it by editing one side; same length, same timestamps.
        other.content = "Has a pet turtle"
        other.content_hash = other.compute_hash()
        self.assertTrue(self.store.update(other))
        self.assertEqual(MemoryService(self.store)._get_clusters(), [])
        self.assertEqual(web_mod._open_conflict_clusters(self.store), [])

    def test_a_deleted_and_recreated_memory_regroups_under_its_new_id(self):
        import elesync.web as web_mod
        from elesync.models import MemoryItem

        self.store.upsert(MemoryItem(content="Hotel booked near Gion", type="fact"))
        old = self.store.upsert(MemoryItem(content="Hotel booked near the station", type="fact"))
        self.assertEqual(len(web_mod._open_conflict_clusters(self.store)), 1)

        # Same text, type and count as before: only the id differs.
        self.store.delete(old)
        new = self.store.upsert(MemoryItem(content="Hotel booked near the station", type="fact"))
        self.assertNotEqual(new, old)
        ids = {
            item.id for _, group in web_mod._open_conflict_clusters(self.store) for item in group
        }
        self.assertIn(new, ids)
        self.assertNotIn(old, ids)


class TestOnboardingImportedStep(unittest.TestCase):
    def test_a_live_assistant_save_is_not_an_import(self):
        import contextvars
        from unittest import mock

        import elesync.web as web_mod
        from elesync.audit import current_ai
        from elesync.models import MemoryItem
        from elesync.service import MemoryService
        from elesync.store import MemoryStore

        tmp = tempfile.TemporaryDirectory()
        self.addCleanup(tmp.cleanup)
        store = MemoryStore(tmp.name)
        self.addCleanup(store.close)
        saved = (web_mod._state.store, web_mod._state.vault_dir)
        self.addCleanup(lambda: setattr(web_mod._state, "store", saved[0]))
        self.addCleanup(lambda: setattr(web_mod._state, "vault_dir", saved[1]))
        web_mod._state.store, web_mod._state.vault_dir = store, tmp.name

        def progress():
            with (
                mock.patch.object(web_mod, "_api_readiness", return_value={"local_clients": []}),
                mock.patch.object(
                    web_mod, "_gemini_connection_status", return_value={"connected": False}
                ),
                mock.patch("elesync.oauth.has_active_authorization", return_value=False),
            ):
                return web_mod._api_onboarding_progress()

        svc = MemoryService(store)
        contextvars.copy_context().run(
            lambda: (current_ai.set("claude"), svc.remember("Prefers tabs over spaces"))
        )
        self.assertEqual(store.stats()["by_source"], {"claude": 1})
        self.assertFalse(progress()["imported"])

        store.upsert(
            MemoryItem(content="Lives in Kyoto", source="claude", source_ref="claude-export")
        )
        self.assertTrue(progress()["imported"])


class TestRelayPhoneDetection(unittest.TestCase):
    def test_a_narrow_desktop_window_is_not_treated_as_the_phone(self):
        from pathlib import Path

        import elesync

        script = (Path(elesync.__file__).parent / "static" / "dashboard.js").read_text(
            encoding="utf-8"
        )
        start = script.index("const RELAY_ON_PHONE")
        definition = script[start : script.index(";", start)]
        self.assertIn("navigator.userAgent", definition)
        self.assertNotIn("max-width", definition)


# ---------------------------------------------------------------------------
# Request guards: other websites, foreign host names, password guessing
# ---------------------------------------------------------------------------


def _raw(port: int, method: str, path: str, headers: dict | None = None, body=None):
    import http.client

    connection = http.client.HTTPConnection("127.0.0.1", port, timeout=5)
    try:
        connection.request(method, path, body=body, headers=headers or {})
        response = connection.getresponse()
        return response.status, response.read()
    finally:
        connection.close()


class TestRequestGuards(WebServerTestCase):
    """A dashboard without a password answered any website and any host name."""

    def test_another_website_cannot_write_through_the_browser(self):
        # A plain-text POST needs no permission from the browser, so any page
        # could plant memories that connected AIs then read.
        body = json.dumps({"content": "planted by another site", "type": "note"})
        for headers in (
            {"Content-Type": "text/plain", "Origin": "https://evil.example"},
            {"Content-Type": "text/plain", "Sec-Fetch-Site": "cross-site"},
            {"Content-Type": "text/plain", "Origin": "null"},
        ):
            status, _ = _raw(self.port, "POST", "/api/memories", headers, body)
            self.assertEqual(status, 403, headers)
        _, found = _get(self.port, "/api/memories?q=planted")
        self.assertEqual(found["total"], 0)

    def test_the_dashboard_and_apps_can_still_write(self):
        body = json.dumps({"content": "same-origin guard probe", "type": "note"})
        for headers in (
            {"Content-Type": "application/json", "Origin": f"http://127.0.0.1:{self.port}"},
            {"Content-Type": "application/json", "Sec-Fetch-Site": "same-origin"},
            # AI clients and scripts send neither header.
            {"Content-Type": "application/json"},
        ):
            status, _ = _raw(self.port, "POST", "/api/memories", headers, body)
            self.assertEqual(status, 201, headers)

    def test_foreign_host_names_are_refused(self):
        # DNS rebinding reaches 127.0.0.1 under the attacker's own domain name.
        status, _ = _raw(self.port, "GET", "/api/export", {"Host": f"evil.example:{self.port}"})
        self.assertEqual(status, 403)
        for host in (
            f"localhost:{self.port}",
            f"127.0.0.1:{self.port}",
            "192.168.1.20:7477",
            "[::1]:7477",
        ):
            status, _ = _raw(self.port, "GET", "/api/vault", {"Host": host})
            self.assertEqual(status, 200, host)

    def test_listed_names_and_the_active_phone_link_are_allowed(self):
        import os
        from unittest.mock import patch

        import elesync.web as web_mod

        with patch.dict(os.environ, {"ELESYNC_ALLOWED_HOSTS": "vault.example.org"}):
            status, _ = _raw(self.port, "GET", "/api/vault", {"Host": "vault.example.org"})
        self.assertEqual(status, 200)
        tunnel = {"active": True, "url": "https://abc123.lhr.life", "error": None}
        with patch.object(web_mod._state.tunnel_manager, "status", return_value=tunnel):
            status, _ = _raw(self.port, "GET", "/api/vault", {"Host": "abc123.lhr.life"})
        self.assertEqual(status, 200)
        status, _ = _raw(self.port, "GET", "/api/vault", {"Host": "abc123.lhr.life"})
        self.assertEqual(status, 403)


class TestSignInThrottle(WebServerTestCase):
    """Unlimited password guesses once the phone link put the dashboard online."""

    def setUp(self):
        import shutil

        import elesync.web as web_mod
        import elesync.web_auth as web_auth

        auth_dir = web_mod.web_auth_dir(self.vault_tmp.name)
        shutil.rmtree(auth_dir, ignore_errors=True)
        web_mod._state.user_mgr = web_auth.UserManager(auth_dir)
        web_mod._state.session_mgr = web_auth.SessionManager(auth_dir, require_existing_user=True)
        web_mod._state.user_mgr.add_user("owner", "correct-horse", role="admin")
        web_mod._sign_in_throttle = web_mod._SignInThrottle()
        self.throttle = web_mod._sign_in_throttle

    def _login(self, password: str) -> int:
        code, _ = _post(self.port, "/api/login", {"username": "owner", "password": password})
        return code

    def test_wrong_passwords_lock_sign_in_for_a_while(self):
        for _ in range(5):
            self.assertEqual(self._login("wrong"), 401)
        # Locked: even the right password waits, or guessing would continue.
        self.assertEqual(self._login("correct-horse"), 429)
        count, _ = self.throttle._failures["owner"]
        self.throttle._failures["owner"] = (count, time.monotonic() - 31)
        self.assertEqual(self._login("correct-horse"), 200)
        # A success starts the count again.
        self.assertEqual(self._login("wrong"), 401)


class TestSecondReviewFixes(WebServerTestCase):
    """Items 21-23 of the second dashboard review."""

    def test_no_update_check_is_made(self):
        # A private repo's releases are invisible to GitHub's public API, so the
        # check could never report an update, yet it called GitHub every day.
        code, _ = _get(self.port, "/api/update-check")
        self.assertEqual(code, 404)
        with urlopen(_url(self.port, "/static/dashboard.js"), timeout=5) as r:
            self.assertNotIn("update-check", r.read().decode())

    def test_project_detail_sends_previews_not_whole_memories(self):
        # One 13-memory project was a 371 KB response; the inspector fetches a
        # memory in full when it is opened.
        _, project = _post(self.port, "/api/projects", {"name": "Preview probe"})
        _, memory = _post(
            self.port,
            "/api/memories",
            {"content": "Long transcript. " * 400, "type": "conversation"},
        )
        _post(self.port, f"/api/projects/{project['id']}/memories", {"memory_id": memory["id"]})
        _, detail = _get(self.port, f"/api/projects/{project['id']}")
        listed = next(m for m in detail["memories"] if m["id"] == memory["id"])
        self.assertTrue(listed["content_truncated"])
        self.assertLessEqual(len(listed["content"]), 1200)

    def test_project_tabs_hold_what_they_say(self):
        with urlopen(_url(self.port, "/static/dashboard-projects.js"), timeout=5) as r:
            script = r.read().decode()
        self.assertIn("['files','Memories']", script)
        self.assertNotIn("['files','Files']", script)
        materials = script.split("function renderProjectMaterials(items, projectId) {", 1)[1]
        materials = materials.split("\n}\n", 1)[0]
        self.assertIn("items.filter(item => item.type === 'conversation')", materials)
        self.assertIn('id="project-conversations"', materials)

    def test_status_chips_only_offer_what_this_install_can_do(self):
        # The installed app is built without semantic search, and has no
        # terminal to run `ele embed` or `ele encrypt` in.
        _, stats = _get(self.port, "/api/stats")
        self.assertIsInstance(stats["semantic_available"], bool)
        with urlopen(_url(self.port, "/static/dashboard.js"), timeout=5) as r:
            script = r.read().decode()
        health = script.split("function renderVaultHealth(s) {", 1)[1].split("\n}\n", 1)[0]
        self.assertIn("if (total && s.semantic_available) {", health)
        self.assertNotIn("ele encrypt", health)
        self.assertIn("text:'Not encrypted'", health)
        self.assertIn("go:'usb'", health)


class TestSecondReviewPolish(WebServerTestCase):
    """Items 26-29 of the second dashboard review."""

    def test_unused_images_are_not_shipped(self):
        # Nothing referenced them, yet every installer carried ~2.4 MB of them.
        import elesync.web as web_mod

        static = Path(web_mod.__file__).parent / "static"
        for name in ("elesync-official-logo.png", "logo.png", "elesync-ui-elephant-v1.webp"):
            self.assertFalse((static / name).exists(), name)

    def test_switch_vault_is_a_button(self):
        with urlopen(_url(self.port, "/"), timeout=5) as r:
            html = r.read().decode()
        self.assertIn('<button type="button" class="vault-switch" onclick="vaultDialog()">', html)
        self.assertNotIn('<span class="vault-switch"', html)

    def test_pairing_qr_is_only_requested_when_a_phone_can_connect(self):
        # Home asked for it on every visit and logged the server's 409 refusal.
        with urlopen(_url(self.port, "/static/dashboard-import.js"), timeout=5) as r:
            script = r.read().decode()
        loader = script.split("async function loadPairingQr(info) {", 1)[1].split("\n}\n", 1)[0]
        self.assertLess(
            loader.index("if (!network.reachable_from_network)"),
            loader.index("fetch('/api/network-qr'"),
        )
        remote = script.split("async function loadRemote() {", 1)[1].split("\n}\n", 1)[0]
        self.assertEqual(remote.count("await loadPairingQr(info);"), 4)

    def test_phone_rows_wrap_instead_of_scrolling_out_of_sight(self):
        with urlopen(_url(self.port, "/static/dashboard-command-center.css"), timeout=5) as r:
            command_center = r.read().decode()
        with urlopen(_url(self.port, "/static/dashboard.css"), timeout=5) as r:
            dashboard = r.read().decode()
        self.assertNotIn(".cc-health { flex-wrap:nowrap;", command_center)
        self.assertIn(
            "@media (max-width:700px) { .project-tabs { flex-wrap:wrap; overflow-x:visible; } }",
            dashboard,
        )
