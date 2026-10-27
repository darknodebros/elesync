"""
elesync.summary — human-readable digest of everything in the vault.

Usage:
    ele summary            # full digest grouped by type
    ele summary --short    # just the headline counts + top facts
    ele summary --source chatgpt   # filter to one AI
"""

from __future__ import annotations

import logging
from collections import defaultdict
from datetime import datetime, timezone

logger = logging.getLogger(__name__)

# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------


def _age(ts: str | None) -> str:
    """Return a friendly relative-age string for an ISO timestamp."""
    if not ts:
        return "unknown date"
    try:
        dt = datetime.fromisoformat(ts.rstrip("Z")).replace(tzinfo=timezone.utc)
        delta = datetime.now(timezone.utc) - dt
        days = delta.days
        if days == 0:
            return "today"
        if days == 1:
            return "yesterday"
        if days < 7:
            return f"{days} days ago"
        if days < 30:
            weeks = days // 7
            return f"{weeks} week{'s' if weeks > 1 else ''} ago"
        if days < 365:
            months = days // 30
            return f"{months} month{'s' if months > 1 else ''} ago"
        years = days // 365
        return f"{years} year{'s' if years > 1 else ''} ago"
    except Exception:
        logger.debug("failed to parse timestamp %r", ts)
        return ts[:10] if ts else "unknown date"


def _truncate(text: str, width: int = 80) -> str:
    return text if len(text) <= width else text[: width - 1] + "…"


_TYPE_LABELS: dict[str, str] = {
    "fact": "📌  Facts about you",
    "preference": "⭐  Your preferences",
    "project": "🚀  Projects",
    "note": "📝  Notes",
    "instruction": "📋  Instructions",
    "event": "📅  Events",
    "other": "🗂   Other",
}

_TYPE_ORDER = ["fact", "preference", "project", "instruction", "event", "note", "other"]


# ---------------------------------------------------------------------------
# Public API
# ---------------------------------------------------------------------------


def build_summary(
    vault_dir: str | None = None,
    source: str | None = None,
    short: bool = False,
) -> str:
    """Return the formatted summary as a string."""
    from elesync import default_vault_dir
    from elesync.store import MemoryStore

    store = MemoryStore(vault_dir=default_vault_dir(vault_dir))
    try:
        all_items = store.all()
    finally:
        store.close()

    if source:
        all_items = [i for i in all_items if i.source == source]

    if not all_items:
        if source:
            return f"No memories found from source '{source}'."
        return (
            "Your vault is empty.\n"
            "Run `ele import <file>` to load memories from ChatGPT, Claude, or Gemini."
        )

    # Group by type
    by_type: dict[str, list] = defaultdict(list)
    sources_seen: set[str] = set()
    for item in all_items:
        t = item.type if item.type in _TYPE_LABELS else "other"
        by_type[t].append(item)
        if item.source:
            sources_seen.add(item.source)

    # Sort each group newest-first
    for group in by_type.values():
        group.sort(key=lambda i: i.created_at or "", reverse=True)

    lines: list[str] = []

    # ── Header ──────────────────────────────────────────────────────────────
    total = len(all_items)
    source_str = ", ".join(sorted(sources_seen)) if sources_seen else "unknown"
    lines.append("━" * 60)
    lines.append("  🐘  EleSync — your AI memory")
    lines.append(f"  {total} {'memory' if total == 1 else 'memories'}  ·  sources: {source_str}")
    lines.append("━" * 60)

    if short:
        # Short mode: just counts per type + top 5 facts
        for t in _TYPE_ORDER:
            if t not in by_type:
                continue
            label = _TYPE_LABELS[t]
            count = len(by_type[t])
            lines.append(f"  {label}: {count}")
        lines.append("")
        facts = by_type.get("fact", [])[:5]
        if facts:
            lines.append("Top facts about you:")
            for item in facts:
                lines.append(f"  • {_truncate(item.content)}")
        lines.append("━" * 60)
        lines.append("  Run `ele summary` for the full digest.")
        return "\n".join(lines)

    # ── Full mode ────────────────────────────────────────────────────────────
    for t in _TYPE_ORDER:
        if t not in by_type:
            continue
        label = _TYPE_LABELS[t]
        group = by_type[t]
        lines.append("")
        lines.append(f"  {label}  ({len(group)})")
        lines.append("  " + "─" * 56)
        for item in group:
            age = _age(item.created_at)
            src = f"[{item.source}]" if item.source else ""
            tags = f"  #{' #'.join(item.tags)}" if item.tags else ""
            content = _truncate(item.content, 72)
            lines.append(f"  • {content}")
            lines.append(f"    {src}  {age}{tags}")

    # ── Footer ───────────────────────────────────────────────────────────────
    lines.append("")
    lines.append("━" * 60)
    lines.append("  tip: `ele search <words>` to find specific memories")
    lines.append("       `ele forget <id>` to remove one")
    lines.append("━" * 60)

    return "\n".join(lines)
