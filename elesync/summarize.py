"""LLM-assisted summarization via Ollama or remote APIs (#68).

Optional feature: generate digests or summaries of vault subsets without
leaving the CLI. Three backends:

- **Ollama** (local, recommended for privacy): calls a local Ollama server
  at ``http://localhost:11434`` via its REST API. No content leaves the
  machine.
- **OpenAI**: calls ``https://api.openai.com/v1/chat/completions``. Requires
  an API key in the environment.
- **Anthropic**: calls ``https://api.anthropic.com/v1/messages``. Requires
  an API key in the environment.
- **Gemini**: calls Google's Generative Language API. Requires a Gemini API
  key from the user's Google AI Studio account.

Uses stdlib ``urllib.request`` — no HTTP client dependency. The base install
stays dependency-light; summarization is an optional convenience, not a
core feature.

Privacy: a clear warning is printed before sending vault content to a
remote API. ``--dry-run`` shows what would be sent without making any call.
"""

from __future__ import annotations

import json
import os
import urllib.error
import urllib.request
from typing import TYPE_CHECKING

if TYPE_CHECKING:
    from .models import MemoryItem

# ── Backend configuration ─────────────────────────────────────────────────

OLLAMA_DEFAULT_URL = "http://localhost:11434"
OPENAI_API_URL = "https://api.openai.com/v1/chat/completions"
ANTHROPIC_API_URL = "https://api.anthropic.com/v1/messages"

#: Provider names accepted by config / --provider.
PROVIDERS = ("ollama", "openai", "anthropic", "gemini", "none")

#: Format templates. Each is a system prompt that shapes the output.
FORMAT_PROMPTS = {
    "bullet": (
        "Summarize the following memories as a concise bullet-point list. "
        "Group related items together. Each bullet should be one line, "
        "starting with '- '. Do not include a preamble or closing remark."
    ),
    "narrative": (
        "Summarize the following memories as a flowing narrative paragraph. "
        "Synthesize connections between items. Keep it under 300 words. "
        "Do not include a preamble or closing remark."
    ),
    "tldr": (
        "Summarize the following memories in a single sentence (TL;DR). "
        "Capture the most important point. Do not include any other text."
    ),
}

DEFAULT_FORMAT = "bullet"
DEFAULT_MODEL_OLLAMA = "llama3"
DEFAULT_MODEL_OPENAI = "gpt-4o-mini"
DEFAULT_MODEL_ANTHROPIC = "claude-3-5-haiku-latest"
DEFAULT_MODEL_GEMINI = "gemini-2.5-flash"


class SummarizeError(Exception):
    """A summarization request failed — backend unreachable, API error,
    or no content to summarize."""


# ── Config resolution ────────────────────────────────────────────────────


def resolve_provider(config: dict | None = None) -> str:
    """Resolve the LLM provider from env or config.

    Priority: $ELESYNC_LLM_PROVIDER > config["llm"]["provider"] > "none".
    """
    env = os.environ.get("ELESYNC_LLM_PROVIDER")
    if env:
        return env
    if config:
        return config.get("llm", {}).get("provider", "none")
    return "none"


def resolve_model(config: dict | None = None, provider: str | None = None) -> str:
    """Resolve the model name for the given provider."""
    env_model = os.environ.get("ELESYNC_LLM_MODEL")
    if env_model:
        return env_model
    if config:
        cfg_model = config.get("llm", {}).get("model")
        if cfg_model:
            return cfg_model
    # Provider-specific defaults.
    if provider == "ollama":
        return DEFAULT_MODEL_OLLAMA
    if provider == "openai":
        return DEFAULT_MODEL_OPENAI
    if provider == "anthropic":
        return DEFAULT_MODEL_ANTHROPIC
    if provider == "gemini":
        return DEFAULT_MODEL_GEMINI
    return DEFAULT_MODEL_OLLAMA


def resolve_ollama_url(config: dict | None = None) -> str:
    """Resolve the Ollama server URL."""
    env = os.environ.get("ELESYNC_OLLAMA_URL")
    if env:
        return env
    if config:
        url = config.get("llm", {}).get("ollama_url")
        if url:
            return url
    return OLLAMA_DEFAULT_URL


def is_local_provider(provider: str) -> bool:
    """True if the provider runs locally (no content leaves the machine)."""
    return provider == "ollama"


# ── Ollama auto-detect ──────────────────────────────────────────────────


def detect_ollama(url: str | None = None, timeout: float = 2.0) -> bool:
    """Check if an Ollama server is running at the given URL (or default).

    A quick GET to /api/tags — returns True if it responds with 200.
    Times out after ``timeout`` seconds so the CLI doesn't hang.
    """
    base = url or OLLAMA_DEFAULT_URL
    try:
        req = urllib.request.Request(f"{base}/api/tags", method="GET")
        with urllib.request.urlopen(req, timeout=timeout) as resp:
            return resp.status == 200
    except (urllib.error.URLError, OSError, ValueError):
        return False


# ── Backend calls ────────────────────────────────────────────────────────


def _build_prompt(items: list[MemoryItem], fmt: str) -> tuple[str, str]:
    """Build (system_prompt, user_prompt) for the given items and format.

    The user prompt is the memories joined by separators, with type/source
    metadata so the model can group by context.
    """
    system = FORMAT_PROMPTS.get(fmt, FORMAT_PROMPTS[DEFAULT_FORMAT])
    lines = []
    for item in items:
        meta = f"[{item.type}/{item.source}]"
        lines.append(f"{meta} {item.content}")
    user = "\n\n".join(lines)
    return system, user


def _call_ollama(url: str, model: str, system: str, user: str, timeout: float = 120.0) -> str:
    """Call the Ollama /api/chat endpoint and return the response text."""
    payload = json.dumps(
        {
            "model": model,
            "messages": [
                {"role": "system", "content": system},
                {"role": "user", "content": user},
            ],
            "stream": False,
        }
    ).encode("utf-8")
    req = urllib.request.Request(
        f"{url}/api/chat",
        data=payload,
        headers={"Content-Type": "application/json"},
        method="POST",
    )
    try:
        with urllib.request.urlopen(req, timeout=timeout) as resp:
            data = json.loads(resp.read().decode("utf-8"))
            return data.get("message", {}).get("content", "").strip()
    except urllib.error.HTTPError as e:
        body = e.read().decode("utf-8", errors="replace")[:200]
        raise SummarizeError(f"Ollama API error {e.code}: {body}") from e
    except (urllib.error.URLError, OSError) as e:
        raise SummarizeError(
            f"Cannot reach Ollama at {url}. Is it running? Start it with `ollama serve`."
        ) from e


def _call_openai(api_key: str, model: str, system: str, user: str, timeout: float = 60.0) -> str:
    """Call the OpenAI chat completions API."""
    payload = json.dumps(
        {
            "model": model,
            "messages": [
                {"role": "system", "content": system},
                {"role": "user", "content": user},
            ],
        }
    ).encode("utf-8")
    req = urllib.request.Request(
        OPENAI_API_URL,
        data=payload,
        headers={
            "Content-Type": "application/json",
        },
        method="POST",
    )
    req.add_unredirected_header("Authorization", f"Bearer {api_key}")
    try:
        with urllib.request.urlopen(req, timeout=timeout) as resp:
            data = json.loads(resp.read().decode("utf-8"))
            return data["choices"][0]["message"]["content"].strip()
    except urllib.error.HTTPError as e:
        body = e.read().decode("utf-8", errors="replace")[:200]
        raise SummarizeError(f"OpenAI API error {e.code}: {body}") from e
    except (urllib.error.URLError, OSError) as e:
        raise SummarizeError(f"Cannot reach OpenAI API: {e}") from e
    except (KeyError, json.JSONDecodeError) as e:
        raise SummarizeError(f"Unexpected OpenAI response: {e}") from e


def _call_anthropic(api_key: str, model: str, system: str, user: str, timeout: float = 60.0) -> str:
    """Call the Anthropic messages API."""
    payload = json.dumps(
        {
            "model": model,
            "max_tokens": 1024,
            "system": system,
            "messages": [{"role": "user", "content": user}],
        }
    ).encode("utf-8")
    req = urllib.request.Request(
        ANTHROPIC_API_URL,
        data=payload,
        headers={
            "Content-Type": "application/json",
            "anthropic-version": "2023-06-01",
        },
        method="POST",
    )
    req.add_unredirected_header("x-api-key", api_key)
    try:
        with urllib.request.urlopen(req, timeout=timeout) as resp:
            data = json.loads(resp.read().decode("utf-8"))
            return data["content"][0]["text"].strip()
    except urllib.error.HTTPError as e:
        body = e.read().decode("utf-8", errors="replace")[:200]
        raise SummarizeError(f"Anthropic API error {e.code}: {body}") from e
    except (urllib.error.URLError, OSError) as e:
        raise SummarizeError(f"Cannot reach Anthropic API: {e}") from e
    except (KeyError, json.JSONDecodeError, IndexError) as e:
        raise SummarizeError(f"Unexpected Anthropic response: {e}") from e


# ── Public API ───────────────────────────────────────────────────────────


def summarize(
    items: list[MemoryItem],
    *,
    provider: str = "ollama",
    model: str | None = None,
    fmt: str = DEFAULT_FORMAT,
    config: dict | None = None,
    dry_run: bool = False,
) -> str:
    """Summarize a list of memories using the configured LLM backend.

    Args:
        items: The memories to summarize.
        provider: "ollama", "openai", or "anthropic".
        model: Model name (defaults to provider-specific default).
        fmt: Output format — "bullet", "narrative", or "tldr".
        config: The EleSync config dict (for resolving ollama_url, etc.).
        dry_run: If True, return the prompt that would be sent without
            making an API call.

    Returns:
        The summary text, or the prompt if dry_run=True.

    Raises:
        SummarizeError: If the backend is unreachable, the API returns an
            error, or there are no items to summarize.
    """
    if not items:
        raise SummarizeError("No memories to summarize.")

    resolved_model = model or resolve_model(config, provider)
    system, user = _build_prompt(items, fmt)

    if dry_run:
        return f"[System prompt]\n{system}\n\n[User prompt ({len(items)} memories)]\n{user}"

    if provider == "ollama":
        url = resolve_ollama_url(config)
        return _call_ollama(url, resolved_model, system, user)

    if provider == "openai":
        key_env = "OPENAI_API_KEY"
        if config:
            key_env = config.get("llm", {}).get("openai_api_key_env", key_env)
        api_key = os.environ.get(key_env, "")
        if not api_key:
            raise SummarizeError(
                f"OpenAI API key not found. Set ${key_env} or configure "
                f"llm.openai_api_key_env in config."
            )
        return _call_openai(api_key, resolved_model, system, user)

    if provider == "anthropic":
        key_env = "ANTHROPIC_API_KEY"
        if config:
            key_env = config.get("llm", {}).get("anthropic_api_key_env", key_env)
        api_key = os.environ.get(key_env, "")
        if not api_key:
            raise SummarizeError(
                f"Anthropic API key not found. Set ${key_env} or configure "
                f"llm.anthropic_api_key_env in config."
            )
        return _call_anthropic(api_key, resolved_model, system, user)

    if provider == "gemini":
        from . import gemini

        api_key = gemini.resolve_api_key(config)
        if not api_key:
            raise SummarizeError(
                "Gemini API key not found. Connect Gemini in the dashboard or set "
                "$ELESYNC_GEMINI_API_KEY."
            )
        try:
            return gemini.generate(api_key, resolved_model, system, user)
        except gemini.GeminiError as exc:
            raise SummarizeError(str(exc)) from exc

    raise SummarizeError(f"Unknown provider {provider!r}. Use one of: {', '.join(PROVIDERS)}.")
