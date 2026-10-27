"""Adapter template — copy this to add support for a new AI source.

Adding a new source is the single best first contribution to this project. It's
self-contained, testable, and immediately useful. Here's the whole process:

  1. Copy this file's `from_<source>` function into `elesync/normalize.py`.
  2. Register it in the `_MEMORY_ADAPTERS` dict in normalize.py.
  3. Teach `_guess_source` how to recognise the export (filename / structure).
  4. Drop a real (anonymised) sample export in tests/sample_data/.
  5. Add a test in tests/test_vault.py mirroring the existing adapter tests.
  6. `python -m unittest discover -s tests -v`  → all green → open a PR.

The only contract: your function takes the parsed JSON (a dict or list) plus a
`source_ref` string, and returns a list[MemoryItem]. Be tolerant — provider
export formats change without notice, so degrade gracefully instead of raising.
"""

from __future__ import annotations

from elesync.models import MemoryItem

# Use the canonical source slug: "grok", "deepseek", "manus", "perplexity", ...
SOURCE = "yoursource"


def from_yoursource(data, source_ref: str = "yoursource-export") -> list[MemoryItem]:
    """Normalise a <YourSource> export into MemoryItems.

    Replace the body below with logic for your provider's actual shape. The two
    common shapes you'll meet:
      - a memory list:        {"memories": ["fact", {"content": "...", "type": "..."}]}
      - conversation history: {"conversations": [{"title", "messages": [...]}]}
    For the conversation shape, prefer reusing `from_conversations` in normalize.py.
    """
    if isinstance(data, dict):
        entries = data.get("memories") or data.get("items") or []
    else:
        entries = data or []

    out: list[MemoryItem] = []
    for e in entries:
        if isinstance(e, str):
            text = e.strip()
            mtype = "fact"
            tags: list[str] = []
        elif isinstance(e, dict):
            text = (e.get("content") or e.get("text") or "").strip()
            mtype = e.get("type", "fact")
            tags = e.get("tags", [])
        else:
            continue
        if not text:
            continue
        out.append(
            MemoryItem(content=text, source=SOURCE, type=mtype, tags=tags, source_ref=source_ref)
        )
    return out


# --- to register, add to normalize.py: -----------------------------------
# _MEMORY_ADAPTERS = {..., "yoursource": from_yoursource}
# and in _guess_source(), map the filename/structure hint to "yoursource".
