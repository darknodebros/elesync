"""Path-traversal defence for the bundled-file routes (`/assets/`, `/static/`).

Regression tests for an unauthenticated arbitrary-file-read: `/assets/` joined
the request path straight onto the assets directory and served anything that
`exists()`, so `GET /assets/../../../../etc/passwd` returned the file with HTTP
200. `/static/` had a correct containment check but built its path by joining
request text, which kept it on CodeQL's taint path.

Both now go through `_lookup_bundled_file`, which requires a bare filename and
returns an entry taken from the directory listing rather than a path built from
request text. These tests pin both halves: real assets still serve, and every
traversal shape 404s.
"""

from __future__ import annotations

import unittest
from pathlib import Path

from elesync.web import _lookup_bundled_file

ASSETS = Path(__file__).resolve().parent.parent / "assets"
STATIC = Path(__file__).resolve().parent.parent / "elesync" / "static"


class TestLookupBundledFileAllows(unittest.TestCase):
    def test_real_asset_resolves(self):
        got = _lookup_bundled_file(ASSETS, "logo-small.png")
        self.assertIsNotNone(got)
        self.assertTrue(got.is_file())
        self.assertEqual(got.name, "logo-small.png")
        self.assertEqual(got.parent, ASSETS.resolve())

    def test_real_static_file_resolves(self):
        got = _lookup_bundled_file(STATIC, "dashboard.js")
        self.assertIsNotNone(got)
        self.assertEqual(got.name, "dashboard.js")
        self.assertEqual(got.parent, STATIC.resolve())


class TestLookupBundledFileRejects(unittest.TestCase):
    def test_parent_traversal_is_rejected(self):
        for probe in (
            "../../../../etc/passwd",
            "../" * 8 + "etc/passwd",
            "..",
            "../assets",
            "subdir/../../etc/passwd",
        ):
            with self.subTest(probe=probe):
                self.assertIsNone(_lookup_bundled_file(ASSETS, probe))

    def test_separators_are_rejected(self):
        for probe in ("a/b", "a\\b", "/etc/passwd", "nested/logo.png", "/"):
            with self.subTest(probe=probe):
                self.assertIsNone(_lookup_bundled_file(ASSETS, probe))

    def test_dotfiles_and_empty_are_rejected(self):
        for probe in ("", ".", "..", ".hidden", ".git"):
            with self.subTest(probe=probe):
                self.assertIsNone(_lookup_bundled_file(ASSETS, probe))

    def test_absolute_path_is_rejected(self):
        self.assertIsNone(_lookup_bundled_file(ASSETS, "/etc/passwd"))

    def test_missing_file_is_rejected(self):
        self.assertIsNone(_lookup_bundled_file(ASSETS, "does-not-exist.png"))

    def test_symlink_escaping_base_is_rejected(self):
        """A symlink inside the directory pointing outside it must not be served
        -- the resolved parent has to still be the base directory."""
        import tempfile

        with tempfile.TemporaryDirectory() as tmp:
            base = Path(tmp) / "bundled"
            base.mkdir()
            outside = Path(tmp) / "secret.txt"
            outside.write_text("secret")
            link = base / "innocent.txt"
            try:
                link.symlink_to(outside)
            except (OSError, NotImplementedError):
                self.skipTest("symlinks not supported here")
            self.assertIsNone(_lookup_bundled_file(base, "innocent.txt"))

    def test_nonexistent_base_dir_does_not_raise(self):
        self.assertIsNone(_lookup_bundled_file(Path("/no/such/dir"), "x.png"))


class TestRoutesEndToEnd(unittest.TestCase):
    """Drive the real HTTP handler: the shape the vulnerability actually took."""

    @classmethod
    def setUpClass(cls):
        import tempfile
        import threading
        import time

        from elesync.web import serve

        cls._tmp = tempfile.TemporaryDirectory()
        cls.port = 8917
        threading.Thread(
            target=lambda: serve(vault_dir=cls._tmp.name, port=cls.port, open_browser=False),
            daemon=True,
        ).start()
        time.sleep(2)

    @classmethod
    def tearDownClass(cls):
        # Close the server thread's store before deleting the temp dir --
        # Windows refuses to remove index.db while SQLite holds it open
        # (WinError 32); same pattern as test_web's tearDownClass.
        from elesync import web as web_mod

        if web_mod._state.store:
            web_mod._state.store.close()
            web_mod._state.store = None
        cls._tmp.cleanup()

    def _get(self, path):
        import urllib.error
        import urllib.request

        try:
            with urllib.request.urlopen(f"http://127.0.0.1:{self.port}{path}", timeout=5) as resp:
                return resp.status, resp.read()
        except urllib.error.HTTPError as exc:
            return exc.code, b""

    def test_legitimate_assets_still_served(self):
        for path in ("/static/dashboard.js", "/static/login.css", "/assets/logo-small.png"):
            with self.subTest(path=path):
                status, body = self._get(path)
                self.assertEqual(status, 200)
                self.assertTrue(body)

    def test_traversal_requests_404_and_leak_nothing(self):
        for path in (
            "/assets/../../../../etc/passwd",
            "/static/../../../../etc/passwd",
            "/assets/..%2f..%2f..%2f..%2fetc%2fpasswd",
            "/assets/....//....//etc/passwd",
            "/assets//etc/passwd",
            "/assets/.",
            "/assets/..",
        ):
            with self.subTest(path=path):
                status, body = self._get(path)
                self.assertEqual(status, 404)
                self.assertNotIn(b"root:x:", body)


if __name__ == "__main__":
    unittest.main()
