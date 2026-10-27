"""Transport-specific formatting for domain results."""

from __future__ import annotations

from . import scopes as scopes_mod
from .results import RecallResult


def format_recall_text(result: RecallResult) -> str:
    if result.access_denied:
        return "This project connection does not have read access."
    if not result.memories:
        return "No matching memories."

    def header(memory) -> str:
        scope = "" if not memory.scopes else f" {scopes_mod.label_of(memory.scopes)}"
        return f"[{memory.type}/{memory.source}]{scope} (id={memory.id[:8]})"

    body = "\n\n".join(f"{header(memory)}\n{memory.content}" for memory in result.memories)
    if result.conflicts:
        note = [
            "",
            "Note: some of these memories conflict — confirm with the user instead of assuming one is correct:",
        ]
        for group in result.conflicts:
            note.extend(
                f"   - [{memory.source}, {memory.created_at[:10]}] {memory.content}  (id={memory.id[:8]})"
                for memory in sorted(group, key=lambda item: item.created_at)
            )
        body += "\n" + "\n".join(note)
    return body
