"""Focused tests for the real Gemini API-key connector."""

from __future__ import annotations

import json
import os
import unittest
from unittest.mock import MagicMock, patch

from elesync import gemini, summarize
from elesync.models import MemoryItem


class TestGeminiAdapter(unittest.TestCase):
    @patch("urllib.request.urlopen")
    def test_verify_makes_real_generation_request(self, mock_urlopen):
        response = MagicMock()
        response.read.return_value = json.dumps(
            {"candidates": [{"content": {"parts": [{"text": "ELE_SYNC_OK"}]}}]}
        ).encode("utf-8")
        mock_urlopen.return_value.__enter__ = MagicMock(return_value=response)
        mock_urlopen.return_value.__exit__ = MagicMock(return_value=False)

        self.assertEqual(gemini.verify_api_key("a" * 24), "ELE_SYNC_OK")
        request = mock_urlopen.call_args.args[0]
        self.assertIn("generativelanguage.googleapis.com", request.full_url)
        self.assertNotIn("key=", request.full_url)
        self.assertEqual(request.get_header("X-goog-api-key"), "a" * 24)
        self.assertEqual(
            json.loads(request.data)["contents"][0]["parts"][0]["text"],
            "Reply with exactly ELE_SYNC_OK.",
        )

    @patch("elesync.gemini.generate", return_value="Gemini summary")
    def test_summarize_dispatches_to_gemini(self, generate):
        items = [MemoryItem(content="A test memory", type="note", source="manual")]
        with patch.dict(os.environ, {"ELESYNC_GEMINI_API_KEY": "a" * 24}, clear=False):
            result = summarize.summarize(items, provider="gemini")
        self.assertEqual(result, "Gemini summary")
        self.assertEqual(generate.call_args.args[0], "a" * 24)

    def test_missing_key_is_clear(self):
        items = [MemoryItem(content="A test memory", type="note", source="manual")]
        with patch.dict(os.environ, {}, clear=True):
            with self.assertRaises(summarize.SummarizeError) as error:
                summarize.summarize(items, provider="gemini")
        self.assertIn("Connect Gemini in the dashboard", str(error.exception))
