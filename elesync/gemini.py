"""Small Gemini API adapter used by the dashboard and summarizer.

The user creates the key in Google's AI Studio. EleSync never asks for a
Google password and never sends the key anywhere except Google's API.
"""

from __future__ import annotations

import json
import os
import urllib.error
import urllib.request

DEFAULT_MODEL = "gemini-2.5-flash"
API_ROOT = "https://generativelanguage.googleapis.com/v1beta/models"


class GeminiError(Exception):
    """A Gemini request failed or returned an unusable response."""


def resolve_api_key(config: dict | None = None) -> str:
    """Resolve the key from env, OS credential store, then legacy config."""
    env_key = os.environ.get("ELESYNC_GEMINI_API_KEY", "").strip()
    if env_key:
        return env_key
    try:
        from .credential_store import get_secret

        stored_key = get_secret("gemini-api-key")
        if stored_key:
            return stored_key.strip()
    except OSError:
        pass
    if config:
        return str(config.get("llm", {}).get("gemini_api_key", "")).strip()
    return ""


def resolve_model(config: dict | None = None) -> str:
    env_model = os.environ.get("ELESYNC_GEMINI_MODEL", "").strip()
    if env_model:
        return env_model
    if config:
        model = str(config.get("llm", {}).get("gemini_model", "")).strip()
        if model:
            return model
    return DEFAULT_MODEL


def _call(api_key: str, model: str, contents: list[dict], timeout: float = 60.0) -> str:
    if not api_key:
        raise GeminiError("Gemini API key is not configured.")
    url = f"{API_ROOT}/{model}:generateContent"
    payload = json.dumps({"contents": contents}).encode("utf-8")
    request = urllib.request.Request(
        url,
        data=payload,
        headers={"Content-Type": "application/json"},
        method="POST",
    )
    request.add_unredirected_header("x-goog-api-key", api_key)
    try:
        with urllib.request.urlopen(request, timeout=timeout) as response:
            data = json.loads(response.read().decode("utf-8"))
    except urllib.error.HTTPError as exc:
        body = exc.read().decode("utf-8", errors="replace")[:240]
        raise GeminiError(f"Gemini API error {exc.code}: {body}") from exc
    except (urllib.error.URLError, OSError) as exc:
        raise GeminiError(f"Cannot reach the Gemini API: {exc}") from exc
    except json.JSONDecodeError as exc:
        raise GeminiError("Gemini returned an invalid response.") from exc

    try:
        return data["candidates"][0]["content"]["parts"][0]["text"].strip()
    except (KeyError, IndexError, TypeError) as exc:
        raise GeminiError("Gemini returned no usable text response.") from exc


def verify_api_key(api_key: str, model: str = DEFAULT_MODEL) -> str:
    """Make a minimal real request and return Gemini's response text."""
    return _call(
        api_key,
        model,
        [{"role": "user", "parts": [{"text": "Reply with exactly ELE_SYNC_OK."}]}],
    )


def generate(api_key: str, model: str, system: str, user: str) -> str:
    """Generate text using the existing EleSync prompt pair."""
    return _call(
        api_key,
        model,
        [
            {"role": "user", "parts": [{"text": f"{system}\n\n{user}"}]},
        ],
    )
