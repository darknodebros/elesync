"""Tests for the plain-text import feature (from_plaintext, `ele import -`, web API)."""

from __future__ import annotations

import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch  # noqa: F401  (used by TestCmdImportStdin)

from elesync.normalize import from_plaintext, normalize_file


class TestFromPlaintext(unittest.TestCase):
    def test_single_line(self):
        items = from_plaintext("I prefer dark mode")
        self.assertEqual(len(items), 1)
        self.assertEqual(items[0].content, "I prefer dark mode")
        self.assertEqual(items[0].source, "manual")
        self.assertEqual(items[0].type, "fact")

    def test_multi_line(self):
        text = "I prefer dark mode\nMy name is Alice\nI use Python"
        items = from_plaintext(text)
        self.assertEqual(len(items), 3)
        self.assertEqual(items[0].content, "I prefer dark mode")
        self.assertEqual(items[1].content, "My name is Alice")
        self.assertEqual(items[2].content, "I use Python")

    def test_paragraph_blocks(self):
        text = "I am a software engineer.\nI work on backend systems.\n\nMy favorite language is Rust.\nI also know Python."
        items = from_plaintext(text)
        self.assertEqual(len(items), 2)
        self.assertIn("software engineer", items[0].content)
        self.assertIn("Rust", items[1].content)

    def test_empty_string(self):
        self.assertEqual(from_plaintext(""), [])

    def test_whitespace_only(self):
        self.assertEqual(from_plaintext("   \n\n  \n  "), [])

    def test_custom_source(self):
        items = from_plaintext("test memory", source="chatgpt")
        self.assertEqual(items[0].source, "chatgpt")

    def test_custom_source_ref(self):
        items = from_plaintext("test memory", source_ref="my-file.txt")
        self.assertEqual(items[0].source_ref, "my-file.txt")

    def test_strips_lines(self):
        text = "  leading spaces  \n  trailing spaces  "
        items = from_plaintext(text)
        self.assertEqual(len(items), 2)
        self.assertEqual(items[0].content, "leading spaces")
        self.assertEqual(items[1].content, "trailing spaces")

    def test_skips_blank_lines(self):
        text = "line one\n\n\n\nline two"
        items = from_plaintext(text)
        self.assertEqual(len(items), 2)


class TestNormalizeFileTxt(unittest.TestCase):
    def test_txt_file(self):
        with tempfile.NamedTemporaryFile(
            mode="w", suffix=".txt", delete=False, encoding="utf-8"
        ) as f:
            f.write("I like Rust\nI use Linux\nI prefer vim")
            tmp = f.name
        try:
            items = normalize_file(tmp)
            self.assertEqual(len(items), 3)
            self.assertEqual(items[0].content, "I like Rust")
            self.assertEqual(items[0].source_ref, Path(tmp).name)
        finally:
            Path(tmp).unlink(missing_ok=True)


class TestCmdImportStdin(unittest.TestCase):
    """`ele import -` reads plain text from stdin and routes it through
    from_plaintext — the same path the old `ele paste` used and the web
    dashboard's paste box still uses."""

    def test_import_stdin(self):
        import io

        from elesync.cli import cmd_import

        with (
            tempfile.TemporaryDirectory() as vault,
            patch("sys.stdin", io.StringIO("I prefer dark mode")),
        ):
            args = type(
                "A",
                (),
                {
                    "path": "-",
                    "source": "manual",
                    "vault": vault,
                    "passphrase": None,
                    "semantic": False,
                    "scopes": None,
                    "lang": None,
                },
            )()
            result = cmd_import(args)
            self.assertEqual(result, 0)

    def test_import_stdin_empty_returns_error(self):
        import io

        from elesync.cli import cmd_import

        with tempfile.TemporaryDirectory() as vault, patch("sys.stdin", io.StringIO("")):
            args = type(
                "A",
                (),
                {
                    "path": "-",
                    "source": "manual",
                    "vault": vault,
                    "passphrase": None,
                    "semantic": False,
                    "scopes": None,
                    "lang": None,
                },
            )()
            result = cmd_import(args)
            self.assertEqual(result, 1)


if __name__ == "__main__":
    unittest.main()
