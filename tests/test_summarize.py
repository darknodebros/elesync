"""Tests for LLM-assisted summarization (#68).

The backend calls (Ollama/OpenAI/Anthropic) are mocked — no real API
keys or running Ollama server needed. Tests cover:
- Config resolution (provider, model, ollama_url)
- Ollama auto-detect
- Prompt building (format templates, memory joining)
- Backend calls (mocked urllib)
- Dry run
- Error handling (no items, unreachable backend, missing API key)
- CLI command dispatch
"""

from __future__ import annotations

import json
import os
import tempfile
import unittest
from unittest.mock import MagicMock, patch

from elesync import summarize
from elesync.models import MemoryItem


class TestConfigResolution(unittest.TestCase):
    """Provider/model/URL resolution from env and config."""

    def test_provider_from_env(self):
        with patch.dict(os.environ, {"ELESYNC_LLM_PROVIDER": "openai"}, clear=False):
            self.assertEqual(summarize.resolve_provider({}), "openai")

    def test_provider_from_config(self):
        with patch.dict(os.environ, {}, clear=True):
            self.assertEqual(
                summarize.resolve_provider({"llm": {"provider": "anthropic"}}),
                "anthropic",
            )

    def test_provider_defaults_to_none(self):
        with patch.dict(os.environ, {}, clear=True):
            self.assertEqual(summarize.resolve_provider({}), "none")
            self.assertEqual(summarize.resolve_provider(None), "none")

    def test_model_from_env(self):
        with patch.dict(os.environ, {"ELESYNC_LLM_MODEL": "custom-model"}, clear=False):
            self.assertEqual(summarize.resolve_model({}, "ollama"), "custom-model")

    def test_model_from_config(self):
        with patch.dict(os.environ, {}, clear=True):
            self.assertEqual(
                summarize.resolve_model({"llm": {"model": "cfg-model"}}, "openai"),
                "cfg-model",
            )

    def test_model_defaults_per_provider(self):
        with patch.dict(os.environ, {}, clear=True):
            self.assertEqual(summarize.resolve_model({}, "ollama"), "llama3")
            self.assertEqual(summarize.resolve_model({}, "openai"), "gpt-4o-mini")
            self.assertEqual(summarize.resolve_model({}, "anthropic"), "claude-3-5-haiku-latest")

    def test_ollama_url_from_env(self):
        with patch.dict(os.environ, {"ELESYNC_OLLAMA_URL": "http://myhost:11434"}, clear=False):
            self.assertEqual(summarize.resolve_ollama_url({}), "http://myhost:11434")

    def test_ollama_url_from_config(self):
        with patch.dict(os.environ, {}, clear=True):
            self.assertEqual(
                summarize.resolve_ollama_url({"llm": {"ollama_url": "http://cfg:11434"}}),
                "http://cfg:11434",
            )

    def test_ollama_url_default(self):
        with patch.dict(os.environ, {}, clear=True):
            self.assertEqual(summarize.resolve_ollama_url({}), "http://localhost:11434")

    def test_is_local_provider(self):
        self.assertTrue(summarize.is_local_provider("ollama"))
        self.assertFalse(summarize.is_local_provider("openai"))
        self.assertFalse(summarize.is_local_provider("anthropic"))


class TestOllamaDetect(unittest.TestCase):
    """Ollama auto-detect via /api/tags."""

    @patch("urllib.request.urlopen")
    def test_detect_returns_true_on_200(self, mock_urlopen):
        resp = MagicMock()
        resp.status = 200
        mock_urlopen.return_value.__enter__ = MagicMock(return_value=resp)
        mock_urlopen.return_value.__exit__ = MagicMock(return_value=False)
        self.assertTrue(summarize.detect_ollama())

    @patch("urllib.request.urlopen", side_effect=OSError("connection refused"))
    def test_detect_returns_false_on_error(self, _mock):
        self.assertFalse(summarize.detect_ollama())

    @patch("urllib.request.urlopen", side_effect=OSError("timeout"))
    def test_detect_with_custom_url(self, _mock):
        summarize.detect_ollama("http://myhost:11434")
        # Just verify it doesn't crash with a custom URL.


class TestPromptBuilding(unittest.TestCase):
    """The prompt builder joins memories with metadata."""

    def test_bullet_format(self):
        items = [
            MemoryItem(content="Likes hiking", type="preference", source="chatgpt"),
            MemoryItem(content="Works at Acme", type="fact", source="claude"),
        ]
        system, user = summarize._build_prompt(items, "bullet")
        self.assertIn("bullet-point list", system)
        self.assertIn("[preference/chatgpt] Likes hiking", user)
        self.assertIn("[fact/claude] Works at Acme", user)

    def test_narrative_format(self):
        items = [MemoryItem(content="Test", type="note", source="manual")]
        system, _ = summarize._build_prompt(items, "narrative")
        self.assertIn("narrative paragraph", system)

    def test_tldr_format(self):
        items = [MemoryItem(content="Test", type="note", source="manual")]
        system, _ = summarize._build_prompt(items, "tldr")
        self.assertIn("single sentence", system)

    def test_unknown_format_falls_back_to_default(self):
        items = [MemoryItem(content="Test", type="note", source="manual")]
        system, _ = summarize._build_prompt(items, "nonexistent")
        self.assertIn("bullet-point list", system)


class TestSummarizeCall(unittest.TestCase):
    """The summarize() function dispatches to the right backend."""

    def test_no_items_raises(self):
        with self.assertRaises(summarize.SummarizeError):
            summarize.summarize([], provider="ollama")

    def test_dry_run_returns_prompt(self):
        items = [MemoryItem(content="Test memory", type="fact", source="chatgpt")]
        result = summarize.summarize(items, provider="ollama", dry_run=True)
        self.assertIn("[System prompt]", result)
        self.assertIn("[User prompt", result)
        self.assertIn("Test memory", result)

    @patch("elesync.summarize._call_ollama", return_value="Summary text")
    def test_ollama_dispatch(self, _mock):
        items = [MemoryItem(content="Test", type="fact", source="chatgpt")]
        result = summarize.summarize(items, provider="ollama", model="llama3")
        self.assertEqual(result, "Summary text")

    @patch("elesync.summarize._call_openai", return_value="OpenAI summary")
    def test_openai_dispatch(self, _mock):
        items = [MemoryItem(content="Test", type="fact", source="chatgpt")]
        with patch.dict(os.environ, {"OPENAI_API_KEY": "sk-test"}, clear=False):
            result = summarize.summarize(items, provider="openai", model="gpt-4o-mini")
            self.assertEqual(result, "OpenAI summary")

    @patch("elesync.summarize._call_anthropic", return_value="Anthropic summary")
    def test_anthropic_dispatch(self, _mock):
        items = [MemoryItem(content="Test", type="fact", source="chatgpt")]
        with patch.dict(os.environ, {"ANTHROPIC_API_KEY": "sk-test"}, clear=False):
            result = summarize.summarize(items, provider="anthropic", model="claude-3-5-sonnet")
            self.assertEqual(result, "Anthropic summary")

    def test_openai_without_key_raises(self):
        items = [MemoryItem(content="Test", type="fact", source="chatgpt")]
        with patch.dict(os.environ, {}, clear=True):
            with self.assertRaises(summarize.SummarizeError) as ctx:
                summarize.summarize(items, provider="openai")
            self.assertIn("API key not found", str(ctx.exception))

    def test_anthropic_without_key_raises(self):
        items = [MemoryItem(content="Test", type="fact", source="chatgpt")]
        with patch.dict(os.environ, {}, clear=True):
            with self.assertRaises(summarize.SummarizeError) as ctx:
                summarize.summarize(items, provider="anthropic")
            self.assertIn("API key not found", str(ctx.exception))

    def test_unknown_provider_raises(self):
        items = [MemoryItem(content="Test", type="fact", source="chatgpt")]
        with self.assertRaises(summarize.SummarizeError):
            summarize.summarize(items, provider="nonexistent")


class TestBackendCalls(unittest.TestCase):
    """The _call_* functions issue the right HTTP requests (mocked)."""

    @patch("urllib.request.urlopen")
    def test_ollama_call_parses_response(self, mock_urlopen):
        resp = MagicMock()
        resp.read.return_value = json.dumps(
            {
                "message": {"content": "  Summary here  "},
            }
        ).encode("utf-8")
        mock_urlopen.return_value.__enter__ = MagicMock(return_value=resp)
        mock_urlopen.return_value.__exit__ = MagicMock(return_value=False)
        result = summarize._call_ollama("http://localhost:11434", "llama3", "sys", "user")
        self.assertEqual(result, "Summary here")

    @patch("urllib.request.urlopen")
    def test_openai_call_parses_response(self, mock_urlopen):
        resp = MagicMock()
        resp.read.return_value = json.dumps(
            {
                "choices": [{"message": {"content": "OpenAI result"}}],
            }
        ).encode("utf-8")
        mock_urlopen.return_value.__enter__ = MagicMock(return_value=resp)
        mock_urlopen.return_value.__exit__ = MagicMock(return_value=False)
        result = summarize._call_openai("sk-key", "gpt-4o-mini", "sys", "user")
        self.assertEqual(result, "OpenAI result")

    @patch("urllib.request.urlopen")
    def test_anthropic_call_parses_response(self, mock_urlopen):
        resp = MagicMock()
        resp.read.return_value = json.dumps(
            {
                "content": [{"text": "Anthropic result"}],
            }
        ).encode("utf-8")
        mock_urlopen.return_value.__enter__ = MagicMock(return_value=resp)
        mock_urlopen.return_value.__exit__ = MagicMock(return_value=False)
        result = summarize._call_anthropic("sk-key", "claude-3-5-sonnet", "sys", "user")
        self.assertEqual(result, "Anthropic result")

    @patch(
        "urllib.request.urlopen",
        side_effect=__import__("urllib.error", fromlist=["HTTPError"]).HTTPError(
            "", 401, "Unauthorized", {}, __import__("io").BytesIO(b'{"error": "bad key"}')
        ),
    )
    def test_ollama_http_error_raises(self, _mock):
        with self.assertRaises(summarize.SummarizeError) as ctx:
            summarize._call_ollama("http://localhost:11434", "llama3", "sys", "user")
        self.assertIn("Ollama API error 401", str(ctx.exception))

    @patch("urllib.request.urlopen", side_effect=OSError("connection refused"))
    def test_ollama_connection_error_raises(self, _mock):
        with self.assertRaises(summarize.SummarizeError) as ctx:
            summarize._call_ollama("http://localhost:11434", "llama3", "sys", "user")
        self.assertIn("Cannot reach Ollama", str(ctx.exception))


class TestSummarizeCli(unittest.TestCase):
    """The CLI command dispatches correctly."""

    def test_no_provider_and_no_ollama_returns_error(self):
        from elesync.cli import cmd_summarize

        args = type("A", (), {})()
        args.tag = None
        args.source = None
        args.type = None
        args.since = None
        args.format = "bullet"
        args.provider = None
        args.save_as = False
        args.dry_run = False
        args.vault = None
        with (
            patch.dict(os.environ, {}, clear=True),
            patch("elesync.cli._load_config", return_value={}),
            patch("elesync.summarize.detect_ollama", return_value=False),
        ):
            result = cmd_summarize(args)
        self.assertEqual(result, 1)

    def test_dry_run_outputs_prompt(self):
        from elesync.cli import cmd_summarize

        with tempfile.TemporaryDirectory() as vault_dir:
            from elesync.store import MemoryStore

            store = MemoryStore(vault_dir)
            store.upsert(
                MemoryItem(content="Test memory for dry run", type="fact", source="chatgpt")
            )
            store.close()

            args = type("A", (), {})()
            args.tag = None
            args.source = None
            args.type = None
            args.since = None
            args.format = "bullet"
            args.provider = "ollama"
            args.save_as = False
            args.dry_run = True
            args.vault = vault_dir
            with (
                patch.dict(os.environ, {}, clear=True),
                patch("elesync.cli._load_config", return_value={}),
            ):
                result = cmd_summarize(args)
            self.assertEqual(result, 0)


if __name__ == "__main__":
    unittest.main()
