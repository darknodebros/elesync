"""Public REST API (/api/v1) with API keys: keys, permissions, validation and HTTP."""

from __future__ import annotations

import json
import tempfile
import threading
import unittest
from http.server import HTTPServer
from pathlib import Path
from urllib.error import HTTPError
from urllib.request import Request, urlopen

from elesync import rest_api
from elesync.project_service import ProjectService
from elesync.store import MemoryStore


class TestApiKeyStore(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        self.keys = rest_api.ApiKeyStore(self.tmp.name)

    def test_secret_is_returned_once_and_only_its_hash_is_stored(self):
        public, secret = self.keys.create("n8n flows", "write")
        self.assertTrue(secret.startswith("esk_"))
        on_disk = (Path(self.tmp.name) / rest_api.KEYS_FILE).read_text(encoding="utf-8")
        self.assertNotIn(secret, on_disk)
        self.assertNotIn("hash", public)
        self.assertEqual(self.keys.list(), [public])
        self.assertEqual(self.keys.verify(secret)["id"], public["id"])

    def test_wrong_malformed_and_revoked_keys_are_refused(self):
        public, secret = self.keys.create("script", "read")
        self.assertIsNone(self.keys.verify(secret[:-1] + ("A" if secret[-1] != "A" else "B")))
        self.assertIsNone(self.keys.verify("not-a-key"))
        self.assertIsNone(self.keys.verify(""))
        self.assertTrue(self.keys.revoke(public["id"]))
        self.assertIsNone(self.keys.verify(secret))
        self.assertFalse(self.keys.revoke(public["id"]))

    def test_names_access_and_project_ids_are_validated(self):
        for name, access, project in (
            ("", "read", None),
            ("x", "admin", None),
            ("x", "read", "../etc"),
        ):
            with self.subTest(name=name, access=access, project=project):
                with self.assertRaises(ValueError):
                    self.keys.create(name, access, project)

    def test_an_unreadable_key_file_is_never_overwritten(self):
        path = Path(self.tmp.name) / rest_api.KEYS_FILE
        path.write_text("{ not json", encoding="utf-8")
        self.assertIsNone(self.keys.verify("esk_" + "a" * 43))
        with self.assertRaises(OSError):
            self.keys.create("app", "read")
        self.assertEqual(path.read_text(encoding="utf-8"), "{ not json")

    def test_bearer_header_parsing(self):
        key = "esk_" + "b" * 43
        self.assertEqual(rest_api.bearer_token(f"Bearer {key}"), key)
        self.assertEqual(rest_api.bearer_token(f"bearer   {key}"), key)
        self.assertEqual(rest_api.bearer_token(f"Basic {key}"), "")
        self.assertEqual(rest_api.bearer_token("Bearer esk_short"), "")
        self.assertEqual(rest_api.bearer_token(None), "")


class TestRateLimiter(unittest.TestCase):
    def test_limits_per_key_and_recovers_after_the_window(self):
        limiter = rest_api.RateLimiter(limit=2, window=60)
        self.assertEqual(limiter.retry_after("a", now=0), 0)
        self.assertEqual(limiter.retry_after("a", now=1), 0)
        self.assertGreater(limiter.retry_after("a", now=2), 0)
        self.assertEqual(limiter.retry_after("b", now=2), 0)  # other keys are unaffected
        self.assertEqual(limiter.retry_after("a", now=61), 0)


class TestRestApi(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        self.store = MemoryStore(self.tmp.name)
        self.addCleanup(self.store.close)
        self.projects = ProjectService(self.store)
        self.api = rest_api.RestApi(self.store, self.projects)
        self.writer = {"id": "w", "name": "writer", "access": "write", "project_id": None}
        self.reader = {"id": "r", "name": "reader", "access": "read", "project_id": None}

    def call(self, method, path, key, body=None, query=None):
        def read_body():
            if isinstance(body, str):
                return json.loads(body)
            return body if body is not None else {}

        return self.api.handle(method, path, query or {}, read_body, key)

    def save(self, content, key=None, **extra):
        return self.call(
            "POST", "/api/v1/memories", key or self.writer, {"content": content, **extra}
        )

    def test_save_search_get_and_delete(self):
        status, body = self.save("Deploys go out on Thursdays", tags=["ops"])
        self.assertEqual(status, 201)
        memory_id = body["data"]["id"]
        self.assertEqual(body["data"]["source"], "api")

        status, body = self.save("Deploys go out on Thursdays")
        self.assertEqual((status, body["meta"]["created"]), (200, False))

        status, body = self.call("GET", "/api/v1/memories", self.reader, query={"q": ["Thursdays"]})
        self.assertEqual(status, 200)
        self.assertEqual([m["id"] for m in body["data"]], [memory_id])

        status, body = self.call("GET", "/api/v1/memories", self.reader)
        self.assertEqual((status, body["meta"]["total"]), (200, 1))

        status, body = self.call("GET", f"/api/v1/memories/{memory_id}", self.reader)
        self.assertEqual(body["data"]["tags"], ["ops"])

        self.assertEqual(self.call("DELETE", f"/api/v1/memories/{memory_id}", self.writer)[0], 200)
        self.assertEqual(self.call("GET", f"/api/v1/memories/{memory_id}", self.reader)[0], 404)

    def test_read_only_keys_cannot_change_anything(self):
        status, body = self.save("x", key=self.reader)
        self.assertEqual((status, body["error"]["code"]), (403, "read_only_key"))
        memory_id = self.save("kept")[1]["data"]["id"]
        self.assertEqual(self.call("DELETE", f"/api/v1/memories/{memory_id}", self.reader)[0], 403)
        self.assertIsNotNone(self.store.get(memory_id))

    def test_a_project_key_sees_only_its_project(self):
        mine = self.projects.create("Mine", "")["id"]
        other = self.projects.create("Other", "")["id"]
        outside = self.save("outside the project")[1]["data"]["id"]
        bound = {"id": "p", "name": "bound", "access": "write", "project_id": mine}

        status, body = self.save("inside the project", key=bound)
        self.assertEqual(status, 201)
        inside = body["data"]["id"]
        self.assertIn(mine, {p["id"] for p in self.store.memory_projects(inside)})

        listed = self.call("GET", "/api/v1/memories", bound)[1]["data"]
        self.assertEqual([m["id"] for m in listed], [inside])
        # Outside memories and other projects look exactly like missing ones.
        self.assertEqual(self.call("GET", f"/api/v1/memories/{outside}", bound)[0], 404)
        self.assertEqual(self.call("DELETE", f"/api/v1/memories/{outside}", bound)[0], 404)
        self.assertEqual(self.call("GET", f"/api/v1/projects/{other}", bound)[0], 404)
        self.assertEqual(self.save("x", key=bound, project_id=other)[0], 404)
        # Saving the exact text of an outside memory must not pull it into the project
        # (security review, 2026-10-08): 409, no id, and it stays invisible to the key.
        status, body = self.save("outside the project", key=bound)
        self.assertEqual((status, body["error"]["code"]), (409, "already_exists"))
        self.assertNotIn(outside, json.dumps(body))
        self.assertNotIn(mine, {p["id"] for p in self.store.memory_projects(outside)})
        self.assertEqual(self.call("GET", f"/api/v1/memories/{outside}", bound)[0], 404)
        self.assertEqual(self.call("DELETE", f"/api/v1/memories/{outside}", bound)[0], 404)
        self.assertIsNotNone(self.store.get(outside))
        # Re-saving something already in the project is still a plain duplicate.
        status, body = self.save("inside the project", key=bound)
        self.assertEqual(
            (status, body["data"]["id"], body["meta"]["created"]), (200, inside, False)
        )

        projects = self.call("GET", "/api/v1/projects", bound)[1]["data"]
        self.assertEqual([p["id"] for p in projects], [mine])
        self.assertEqual(self.call("GET", f"/api/v1/projects/{mine}/context", bound)[0], 200)

    def test_bad_input_is_refused_with_a_clear_code(self):
        cases = [
            (self.save("   "), 400, "invalid_field"),
            (self.save("x", tags="ops"), 400, "invalid_field"),
            (self.save("x", tags=["t"] * 21), 400, "invalid_field"),
            (self.save("x", source="Bad Source!"), 400, "invalid_field"),
            (self.save("x" * (rest_api.MAX_CONTENT_CHARS + 1)), 400, "invalid_field"),
            (self.call("POST", "/api/v1/memories", self.writer, "[1, 2]"), 400, "invalid_json"),
            (self.call("POST", "/api/v1/memories", self.writer, "{oops"), 400, "invalid_json"),
            (
                self.call("GET", "/api/v1/memories", self.reader, query={"limit": ["0"]}),
                400,
                "invalid_parameter",
            ),
            (
                self.call("GET", "/api/v1/memories", self.reader, query={"limit": ["x"]}),
                400,
                "invalid_parameter",
            ),
            (
                self.call("GET", "/api/v1/memories", self.reader, query={"q": ["q" * 501]}),
                400,
                "invalid_parameter",
            ),
            (self.call("GET", "/api/v1/nothing", self.reader), 404, "not_found"),
            (self.call("GET", "/api/v1/memories/..%2Fx", self.reader), 404, "not_found"),
            (self.call("PUT", "/api/v1/memories", self.writer), 405, "method_not_allowed"),
            (self.call("POST", "/api/v1/projects", self.writer), 405, "method_not_allowed"),
        ]
        for (status, body), want_status, want_code in cases:
            with self.subTest(want_code=want_code, body=body):
                self.assertEqual(status, want_status)
                self.assertFalse(body["ok"])
                self.assertEqual(body["error"]["code"], want_code)

    def test_info_describes_the_key(self):
        status, body = self.call("GET", "/api/v1", self.reader)
        self.assertEqual(status, 200)
        self.assertEqual(
            body["data"]["key"], {"name": "reader", "access": "read", "project_id": None}
        )


class TestRestApiOverHttp(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        import elesync.web as web_mod
        import elesync.web_auth as web_auth

        cls.web = web_mod
        cls.tmp = tempfile.TemporaryDirectory()
        vault = Path(cls.tmp.name)
        web_mod._state.store = MemoryStore(vault)
        web_mod._state.vault_dir = str(vault)
        # No dashboard users: the dashboard itself is open, but the public API must not be.
        web_mod._state.user_mgr = web_auth.UserManager(web_mod.web_auth_dir(vault))
        web_mod._state.session_mgr = web_auth.SessionManager(
            web_mod.web_auth_dir(vault), require_existing_user=True
        )
        cls.server = HTTPServer(("127.0.0.1", 0), web_mod._Handler)
        cls.port = cls.server.server_address[1]
        threading.Thread(target=cls.server.serve_forever, daemon=True).start()

    @classmethod
    def tearDownClass(cls):
        cls.server.shutdown()
        cls.server.server_close()
        if cls.web._state.store:
            cls.web._state.store.close()
        cls.tmp.cleanup()

    def request(self, method, path, body=None, key=None, headers=None):
        all_headers = {"Content-Type": "application/json", **(headers or {})}
        if key:
            all_headers["Authorization"] = f"Bearer {key}"
        data = json.dumps(body).encode() if body is not None else None
        req = Request(
            f"http://127.0.0.1:{self.port}{path}", data=data, headers=all_headers, method=method
        )
        try:
            with urlopen(req, timeout=5) as r:
                return r.status, json.loads(r.read()), r.headers
        except HTTPError as e:
            return e.code, json.loads(e.read()), e.headers

    def new_key(self, access="write"):
        status, body, _ = self.request("POST", "/api/api-keys", {"name": "tests", "access": access})
        self.assertEqual(status, 201)
        return body["key"], body["secret"]

    def test_every_request_needs_a_key_even_without_a_dashboard_password(self):
        status, body, headers = self.request("GET", "/api/v1/memories")
        self.assertEqual((status, body["error"]["code"]), (401, "unauthorized"))
        self.assertIn("Bearer", headers["WWW-Authenticate"])
        status, _, _ = self.request("GET", "/api/v1", key="esk_" + "z" * 43)
        self.assertEqual(status, 401)

    def test_key_lifecycle_from_the_dashboard(self):
        public, secret = self.new_key()
        listing = self.request("GET", "/api/api-keys")[1]["keys"]
        self.assertIn(public["id"], [k["id"] for k in listing])
        self.assertNotIn(secret, json.dumps(listing))
        self.assertTrue(all("hash" not in k for k in listing))

        status, body, headers = self.request(
            "POST", "/api/v1/memories", {"content": "via http"}, key=secret
        )
        self.assertEqual(status, 201)
        self.assertEqual(headers["Cache-Control"], "no-store")
        status, body, _ = self.request("GET", "/api/v1/memories?q=via", key=secret)
        self.assertEqual(status, 200)
        self.assertEqual(body["data"][0]["content"], "via http")

        self.assertEqual(self.request("DELETE", f"/api/api-keys/{public['id']}")[0], 200)
        self.assertEqual(self.request("GET", "/api/v1", key=secret)[0], 401)

    def test_a_too_large_body_is_refused(self):
        _, secret = self.new_key()
        huge = {"content": "x" * (rest_api.MAX_BODY_BYTES + 10)}
        status, body, _ = self.request("POST", "/api/v1/memories", huge, key=secret)
        self.assertEqual((status, body["error"]["code"]), (413, "too_large"))

    def test_rate_limit_answers_429_with_retry_after(self):
        public, secret = self.new_key(access="read")
        original = rest_api.RATE_LIMITER
        rest_api.RATE_LIMITER = rest_api.RateLimiter(limit=1, window=60)
        self.addCleanup(setattr, rest_api, "RATE_LIMITER", original)
        self.assertEqual(self.request("GET", "/api/v1", key=secret)[0], 200)
        status, body, headers = self.request("GET", "/api/v1", key=secret)
        self.assertEqual((status, body["error"]["code"]), (429, "rate_limited"))
        self.assertGreater(int(headers["Retry-After"]), 0)

    def test_creating_a_key_for_a_missing_project_fails(self):
        status, body, _ = self.request(
            "POST", "/api/api-keys", {"name": "x", "access": "read", "project_id": "f" * 32}
        )
        self.assertEqual(status, 400)
        self.assertIn("project not found", body["error"])


if __name__ == "__main__":
    unittest.main()
