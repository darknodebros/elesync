"""Ingest adapters.

The hard, unglamorous part of this whole system: provider exports are a mess and
none of them share a format. As of 2026 you get ChatGPT JSON, Gemini via Google
Takeout, and Claude's structured export — all different. These adapters sniff the
shape of a JSON export and turn it into normalized MemoryItems.

Design choice: be *tolerant*. Accept the documented shapes below, but also fall
back gracefully on unexpected structure rather than crashing, because export
formats change without notice.
"""

from __future__ import annotations

import csv as _csv
import io
import json
import re
import tempfile
import zipfile
from datetime import datetime, timezone
from pathlib import Path, PurePosixPath

from .models import MemoryItem


def _coerce_memory_entries(entries, source: str, source_ref: str) -> list[MemoryItem]:
    """Turn a list of strings or {content,type,tags} dicts into MemoryItems."""
    out: list[MemoryItem] = []
    for e in entries:
        if isinstance(e, str):
            text = e.strip()
            if text:
                out.append(
                    MemoryItem(content=text, source=source, type="fact", source_ref=source_ref)
                )
        elif isinstance(e, dict):
            text = (e.get("content") or e.get("text") or e.get("memory") or "").strip()
            if not text:
                continue
            out.append(
                MemoryItem(
                    content=text,
                    source=source,
                    type=e.get("type", "fact"),
                    tags=e.get("tags", []),
                    confidence=e.get("confidence", 1.0),
                    source_ref=e.get("id", source_ref),
                )
            )
    return out


def from_chatgpt(data, source_ref: str = "chatgpt-export") -> list[MemoryItem]:
    """ChatGPT memory export: {"memories": [...]} or a bare list of facts."""
    if isinstance(data, dict):
        entries = data.get("memories") or data.get("memory") or data.get("facts") or []
    else:
        entries = data or []
    return _coerce_memory_entries(entries, "chatgpt", source_ref)


def from_claude(data, source_ref: str = "claude-export") -> list[MemoryItem]:
    """Claude memory exports come in two real-world shapes:

    1. A simple flat fact list: {"memories": [...]} or {"userMemories": [...]}.
    2. Claude.ai's actual 2026 data-export "memories.json" shape: a list of
       account records, each with `conversations_memory` (one big narrative
       summary spanning all conversations) and `project_memories` (a dict of
       per-project narrative summaries, keyed by project UUID). Neither key
       is a flat fact list, so the generic coercion below doesn't see them
       unless we extract them explicitly first.
    """
    if isinstance(data, dict):
        entries = data.get("memories") or data.get("userMemories") or []
        return _coerce_memory_entries(entries, "claude", source_ref)

    out: list[MemoryItem] = []
    plain_entries: list = []
    for record in data or []:
        if isinstance(record, dict) and (
            "conversations_memory" in record or "project_memories" in record
        ):
            conv_mem = (record.get("conversations_memory") or "").strip()
            if conv_mem:
                out.append(
                    MemoryItem(
                        content=conv_mem,
                        source="claude",
                        type="note",
                        source_ref=source_ref or "claude-memories",
                    )
                )
            proj_mems = record.get("project_memories") or {}
            if isinstance(proj_mems, dict):
                for project_uuid, proj_text in proj_mems.items():
                    proj_text = (proj_text or "").strip()
                    if not proj_text:
                        continue
                    out.append(
                        MemoryItem(
                            content=proj_text,
                            source="claude",
                            type="project",
                            tags=[str(project_uuid)],
                            source_ref=source_ref or "claude-memories",
                        )
                    )
        else:
            plain_entries.append(record)
    out.extend(_coerce_memory_entries(plain_entries, "claude", source_ref))
    return out


def _looks_like_claude_project(data) -> bool:
    """Claude.ai 'Project' metadata export: {uuid, name, docs: [...]}.

    Most of this object is just metadata (name, description, timestamps) --
    nothing to extract. But `docs` holds actual project-knowledge files the
    user attached, often the most substantive content in the whole export.
    """
    return (
        isinstance(data, dict)
        and "uuid" in data
        and "docs" in data
        and isinstance(data.get("docs"), list)
    )


def from_claude_project(data, source_ref: str = "") -> list[MemoryItem]:
    """Claude.ai Project export: {uuid, name, description, docs: [{filename, content}]}.

    The project metadata itself isn't a memory, but each doc the user attached
    to the project is real content worth capturing.
    """
    project_name = data.get("name") or "Untitled project"
    out: list[MemoryItem] = []
    for doc in data.get("docs") or []:
        if not isinstance(doc, dict):
            continue
        content = (doc.get("content") or "").strip()
        if not content:
            continue
        filename = doc.get("filename") or "untitled"
        body = f"# {project_name} — {filename}\n\n{content}"
        out.append(
            MemoryItem(
                content=body,
                source="claude",
                type="project",
                tags=[project_name],
                source_ref=doc.get("uuid") or source_ref or filename,
            )
        )
    return out


def from_gemini(data, source_ref: str = "gemini-takeout") -> list[MemoryItem]:
    """Gemini (Takeout) memory/conversation import.

    Handles three shapes:
      1. Memory export:  {"memories": [...]} or {"savedInfo": [...]}
      2. Takeout MyActivity.json:  {"listItems": [{header, payload: {conversationId,
         title, messages: [{role, content}]}}]}
      3. Flat list of conversations:  [{conversationId, title, messages}]

    For shape (2) and (3) the messages are converted into one MemoryItem per
    conversation via from_conversations(). Shape (1) keeps the simpler
    _coerce_memory_entries path.
    """
    if not isinstance(data, dict):
        # flat list — try as conversations first
        if isinstance(data, list) and data and isinstance(data[0], dict):
            first = data[0]
            if "messages" in first or "payload" in first:
                return from_conversations(
                    {"conversations": _extract_gemini_takeout_conversations(data)},
                    source="gemini",
                    source_ref=source_ref,
                )
        return _coerce_memory_entries(data or [], "gemini", source_ref)

    # Shape 2: Takeout MyActivity.json with listItems
    if "listItems" in data:
        convs = _extract_gemini_takeout_conversations(data.get("listItems", []))
        if convs:
            return from_conversations(
                {"conversations": convs},
                source="gemini",
                source_ref=source_ref,
            )
        # listItems present but no conversations extracted — possibly activity
        # data (steps, fitness).  Fall through to the memory path which will
        # return [] harmlessly.

    # Shape 1: simple memory export
    entries = data.get("memories") or data.get("savedInfo") or data.get("saved_info") or []
    return _coerce_memory_entries(entries, "gemini", source_ref)


def _extract_gemini_takeout_conversations(items: list) -> list[dict]:
    """Flatten Takeout listItems into the generic conversation shape
    expected by from_conversations(): [{title, messages: [{role, content}]}].

    Silently skips items that aren't conversations (e.g. activity/fitness data).
    """
    out: list[dict] = []
    for item in items:
        if not isinstance(item, dict):
            continue
        payload = item.get("payload") or item
        messages = payload.get("messages")
        if not isinstance(messages, list) or not messages:
            continue
        # Normalise Gemini's "model" role to "assistant" so downstream code
        # that looks for user/assistant pairs works correctly.
        normalised_msgs = []
        for m in messages:
            if not isinstance(m, dict):
                continue
            role = (m.get("role") or "user").lower()
            if role == "model":
                role = "assistant"
            content = m.get("content") or m.get("text") or ""
            if isinstance(content, list):  # multi-part
                content = " ".join(
                    p.get("text", str(p)) if isinstance(p, dict) else str(p) for p in content
                )
            if content:
                normalised_msgs.append({"role": role, "content": str(content)})
        if normalised_msgs:
            out.append(
                {
                    "id": payload.get("conversationId") or payload.get("id", ""),
                    "title": payload.get("title") or "Gemini conversation",
                    "messages": normalised_msgs,
                }
            )
    return out


def from_grok(data, source_ref: str = "grok-export") -> list[MemoryItem]:
    """Grok (xAI) memory or exported conversation data.

    Grok exports commonly arrive inside a ZIP as ``prod-grok-backend.json``.
    EleSync accepts that extracted JSON directly, including the wrapped
    ``conversations: [{conversation, responses}]`` shape, and ignores account,
    billing, and media files that are not conversation content.
    """
    if isinstance(data, dict):
        entries = data.get("grok_memories") or data.get("memories") or data.get("saved") or []
        if entries:
            return _coerce_memory_entries(entries, "grok", source_ref)
        conversations = data.get("conversations") or []
        if conversations and isinstance(conversations[0], dict) and "responses" in conversations[0]:
            return from_conversations(data, source="grok", source_ref=source_ref)
    else:
        entries = data or []
    return _coerce_memory_entries(entries, "grok", source_ref)


def from_deepseek(data, source_ref: str = "deepseek-export") -> list[MemoryItem]:
    """DeepSeek memory export: {"memories": [...]} or {"deepseek_memories": [...]} —
    a list of fact strings or {content, type, tags} dicts. DeepSeek *conversation*
    exports are caught earlier by the shared from_conversations path."""
    if isinstance(data, dict):
        entries = data.get("deepseek_memories") or data.get("memories") or data.get("saved") or []
    else:
        entries = data or []
    return _coerce_memory_entries(entries, "deepseek", source_ref)


def from_perplexity(data, source_ref: str = "perplexity-export") -> list[MemoryItem]:
    """Perplexity memory export: {"memories": [...]} or {"perplexity_memories": [...]} —
    a list of fact strings or {content, type, tags} dicts. Conversation exports are
    caught earlier by the shared from_conversations path."""
    if isinstance(data, dict):
        entries = data.get("perplexity_memories") or data.get("memories") or data.get("saved") or []
    else:
        entries = data or []
    return _coerce_memory_entries(entries, "perplexity", source_ref)


def from_copilot(data, source_ref: str = "copilot-export") -> list[MemoryItem]:
    """Microsoft Copilot memory export: {"memories": [...]} or {"copilot_memories": [...]} —
    a list of fact strings or {content, type, tags} dicts. Conversation exports are
    caught earlier by the shared from_conversations path."""
    if isinstance(data, dict):
        entries = data.get("copilot_memories") or data.get("memories") or data.get("saved") or []
    else:
        entries = data or []
    return _coerce_memory_entries(entries, "copilot", source_ref)


def from_copilot_csv(rows: list[dict], source_ref: str = "copilot-export.csv") -> list[MemoryItem]:
    """Microsoft Copilot's Privacy Dashboard CSV export.

    Verified against a real 405-row export with the header
    `Conversation,Time,Author,Message` -- one row per turn, no thread ID
    beyond the repeated conversation title. Rows for a given conversation
    appear in *reverse*-chronological order (newest first) and reliably so
    -- verified across all 12 real conversations in the sample export.
    We restore chronological order by reversing each group's row order
    rather than re-sorting on the Time string: Time only has second-level
    precision, so re-sorting can flip same-second turns (e.g. a Response
    landing before its Prompt) where simply reversing the original file
    order does not, since the file's own ordering already disambiguates
    same-second ties correctly.

    Also tolerates a second column-naming seen from a different export
    button (`ChatName,CreatedAt,Author,MessageContent`) -- same fields,
    different names. That variant is UNVERIFIED at the row-content level
    (the sample we have is header-only, zero data rows): the column mapping
    is unambiguous, but we haven't confirmed the same reverse-chronological
    ordering holds for it, so this assumes it does.
    """
    by_title: dict[str, list[dict]] = {}
    for row in rows:
        title = (row.get("Conversation") or row.get("ChatName") or "").strip()
        if not title:
            continue
        by_title.setdefault(title, []).append(row)

    out: list[MemoryItem] = []
    for title, group in by_title.items():
        group = list(reversed(group))  # file order is newest-first; restore chronological
        lines = []
        for row in group:
            message = (row.get("Message") or row.get("MessageContent") or "").strip()
            if not message:
                continue
            author = (row.get("Author") or "?").strip()
            lines.append(f"{author}: {message}")
        if not lines:
            continue
        body = f"# {title}\n\n" + "\n".join(lines)
        out.append(
            MemoryItem(
                content=body,
                source="copilot",
                type="conversation",
                source_ref=source_ref or title,
            )
        )
    return out


_WHATSAPP_LINE = re.compile(
    r"^(?:\[)?(?P<date>\d{1,4}[/-]\d{1,2}[/-]\d{1,4}),?\s+"
    r"(?P<time>\d{1,2}:\d{2}(?::\d{2})?\s*(?:[AP]M)?)(?:\]\s*|\s+-\s+|\s*:\s*)"
    r"(?P<sender>[^:]+):\s?(?P<message>.*)$",
    re.IGNORECASE,
)
_WHATSAPP_DATE_FORMATS = (
    "%m/%d/%y, %I:%M %p",
    "%d/%m/%y, %H:%M",
    "%m/%d/%y, %H:%M",
    "%d/%m/%Y, %H:%M",
    "%m/%d/%Y, %H:%M",
)


def _parse_whatsapp_timestamp(date_text: str, time_text: str) -> str | None:
    value = f"{date_text}, {time_text.upper()}".replace("  ", " ").strip()
    for fmt in _WHATSAPP_DATE_FORMATS:
        try:
            return datetime.strptime(value, fmt).replace(tzinfo=timezone.utc).isoformat()
        except ValueError:
            continue
    return None


# What WhatsApp writes in place of a deleted message, in the languages EleSync
# ships (compared lower-case, without a final full stop).
_WHATSAPP_DELETED = frozenset(
    {
        "you deleted this message",
        "this message was deleted",
        "eliminaste este mensaje",
        "se eliminó este mensaje",
        "este mensaje fue eliminado",
        "você apagou esta mensagem",
        "mensagem apagada",
        "esta mensagem foi apagada",
        "vous avez supprimé ce message",
        "ce message a été supprimé",
        "du hast diese nachricht gelöscht",
        "diese nachricht wurde gelöscht",
    }
)
_WHATSAPP_EDITED = re.compile(r"\s*<This message was edited>$", re.IGNORECASE)


def _whatsapp_message_text(text: str) -> str | None:
    """The message as worth remembering: None for a deleted-message stand-in,
    and without the "<This message was edited>" tag."""
    text = text.replace("\u200e", "").replace("\u200f", "").strip()
    if text.lower().rstrip(".") in _WHATSAPP_DELETED:
        return None
    return _WHATSAPP_EDITED.sub("", text)


def _looks_like_whatsapp_text(text: str) -> bool:
    return sum(1 for line in text.splitlines() if _WHATSAPP_LINE.match(line)) >= 1


def parse_whatsapp_messages(text: str) -> list[dict[str, str]]:
    """Parse a WhatsApp export into reviewable messages without saving anything."""
    messages: list[dict[str, str]] = []
    current: dict[str, str] | None = None

    def finish(message: dict[str, str]) -> None:
        kept = _whatsapp_message_text(message["text"])
        if kept is not None:
            messages.append({**message, "text": kept})

    for raw_line in text.splitlines():
        match = _WHATSAPP_LINE.match(raw_line.strip())
        if match:
            if current:
                finish(current)
            timestamp = _parse_whatsapp_timestamp(match.group("date"), match.group("time"))
            current = {
                "timestamp": timestamp or "",
                "display_time": f"{match.group('date')}, {match.group('time').strip()}",
                "sender": match.group("sender").strip(),
                "text": match.group("message").strip(),
            }
        elif current and raw_line.strip():
            current["text"] += f"\n{raw_line.strip()}"
    if current:
        finish(current)
    return messages


def whatsapp_messages_to_item(
    messages: list[dict[str, str]],
    source_ref: str = "whatsapp-export",
    *,
    redact_phone_numbers: bool = False,
    strip_links: bool = False,
    strip_media: bool = False,
    type: str = "conversation",
    tags: list[str] | None = None,
) -> MemoryItem | None:
    """Build an approved WhatsApp item after privacy filters are applied."""
    phone = re.compile(r"(?<!\w)\+?\d[\d\s().-]{6,}\d(?!\w)")
    link = re.compile(r"https?://\S+", re.IGNORECASE)
    kept: list[dict[str, str]] = []
    for original in messages:
        message = dict(original)
        text = message["text"]
        if strip_media and "<Media omitted>" in text:
            text = text.replace("<Media omitted>", "").strip()
        if redact_phone_numbers:
            text = phone.sub("[phone redacted]", text)
        if strip_links:
            text = link.sub("[link removed]", text)
        if text:
            message["text"] = text
            kept.append(message)
    if not kept:
        return None
    lines = [f"[{m['display_time']}] {m['sender']}: {m['text']}" for m in kept]
    latest_timestamp = next(
        (message["timestamp"] for message in reversed(kept) if message["timestamp"]), None
    )
    return MemoryItem(
        content="# WhatsApp conversation\n\n" + "\n".join(lines),
        source="whatsapp",
        type=type,
        tags=tags or [],
        source_ref=source_ref,
        created_at=latest_timestamp
        or datetime.now(timezone.utc).replace(microsecond=0).isoformat(),
    )


def whatsapp_messages_to_items(
    messages: list[dict[str, str]],
    source_ref: str = "whatsapp-export",
    **options,
) -> list[MemoryItem]:
    """Save bounded groups without splitting individual or multiline messages.

    A single message may exceed the character target; keep it intact in its
    own item. Privacy filtering and metadata use the same rules as short chats.
    """
    items: list[MemoryItem] = []
    batch: list[dict[str, str]] = []
    size = 0
    for message in messages:
        message_size = len(f"[{message['display_time']}] {message['sender']}: {message['text']}\n")
        if batch and (len(batch) >= 100 or size + message_size > 16_000):
            item = whatsapp_messages_to_item(batch, source_ref, **options)
            if item is not None:
                items.append(item)
            batch = []
            size = 0
        batch.append(message)
        size += message_size
    if batch:
        item = whatsapp_messages_to_item(batch, source_ref, **options)
        if item is not None:
            items.append(item)
    return items


def from_whatsapp(text: str, source_ref: str = "whatsapp-export") -> list[MemoryItem]:
    """Turn a WhatsApp export into bounded, searchable conversation items."""
    return whatsapp_messages_to_items(parse_whatsapp_messages(text), source_ref)


def from_telegram(data, source_ref: str = "telegram-export") -> list[MemoryItem]:
    """Turn a Telegram Desktop JSON export into one searchable conversation."""
    if not isinstance(data, dict) or not isinstance(data.get("messages"), list):
        return []
    title = data.get("name") or data.get("title") or "Telegram conversation"
    lines: list[str] = []
    for message in data["messages"]:
        if not isinstance(message, dict) or message.get("type") not in (None, "message"):
            continue
        text = message.get("text", "")
        if isinstance(text, list):
            text = "".join(
                part.get("text", "") if isinstance(part, dict) else str(part) for part in text
            )
        text = str(text).strip()
        if not text:
            continue
        sender = message.get("from") or message.get("actor") or "Unknown"
        date = message.get("date") or message.get("date_unixtime") or ""
        lines.append(f"{date} · {sender}: {text}" if date else f"{sender}: {text}")
    if not lines:
        return []
    return [
        MemoryItem(
            content=f"# {title}\n\n" + "\n".join(lines),
            source="telegram",
            type="conversation",
            source_ref=str(data.get("id") or source_ref),
        )
    ]


def from_plaintext(
    text: str, source: str = "manual", source_ref: str = "pasted-text"
) -> list[MemoryItem]:
    """Plain-text import: split on double-newline into paragraph blocks, or
    fall back to one memory per non-empty line. Designed for the copy-paste
    memory exports that ChatGPT, Claude, and Gemini use in place of file-based
    exports."""
    text = text.strip()
    if not text:
        return []
    blocks = [b.strip() for b in text.split("\n\n") if b.strip()]
    if len(blocks) >= 2:
        entries = blocks
    else:
        entries = [line.strip() for line in text.split("\n") if line.strip()]
    return _coerce_memory_entries(entries, source, source_ref)


_CSV_COLUMN_SHAPES = (
    {"conversation", "time", "author", "message"},
    {"chatname", "createdat", "author", "messagecontent"},
)


def preview_csv(text: str) -> dict:
    """Describe a CSV's headers and sample rows without importing it."""
    reader = _csv.DictReader(io.StringIO(text, newline=""))
    raw_headers = reader.fieldnames or []
    headers = {(header or "").strip().lower() for header in raw_headers}
    matched = next((shape for shape in _CSV_COLUMN_SHAPES if shape <= headers), None)
    rows = list(reader)
    return {
        "supported": matched is not None,
        "format": "copilot" if matched is not None else "unknown",
        "columns": [header for header in raw_headers if header],
        "sample": [{key: value for key, value in row.items() if key and value} for row in rows[:3]],
    }


def _normalize_csv_file(path: Path, source_ref: str) -> list[MemoryItem]:
    with open(path, encoding="utf-8-sig", newline="") as f:
        reader = _csv.DictReader(f)
        rows = list(reader)
    if not rows:
        return []
    headers = {(h or "").strip().lower() for h in rows[0].keys()}
    if any(shape <= headers for shape in _CSV_COLUMN_SHAPES):
        return from_copilot_csv(rows, source_ref=source_ref)
    raise ValueError(f"Could not recognize CSV shape in '{path.name}' (columns: {sorted(headers)})")


def from_elesync(data, source_ref: str = "elesync-export") -> list[MemoryItem]:
    """EleSync's own export format (see `ele export`): a dict with a
    `memories` list of full MemoryItem dicts. Each item keeps its original
    source/type/content and content_hash, so re-importing an export is a no-op —
    the dedup recognises every memory and adds zero new ones (idempotent round-trip)."""
    entries = data.get("memories", []) if isinstance(data, dict) else (data or [])
    out: list[MemoryItem] = []
    for e in entries:
        if isinstance(e, dict) and (e.get("content") or "").strip():
            out.append(MemoryItem.from_dict(e))
    return out


def from_conversations(data, source: str, source_ref: str = "") -> list[MemoryItem]:
    """Generic conversation history → one 'conversation' memory per thread.

    Accepts {"conversations": [{title, messages: [{role, content}]}]} — the common
    shape across ChatGPT/Gemini chat-history exports — as well as Claude.ai's data
    export shape: a bare list of {name, chat_messages: [{sender, text, content}]},
    where `content` is a list of Anthropic-style blocks ([{"type": "text", "text": ...}])
    rather than a plain string. We don't summarise (that needs a model); we capture
    the thread verbatim so it's searchable and an AI can summarise on demand via MCP.
    """
    convs = data.get("conversations", data) if isinstance(data, dict) else data
    out: list[MemoryItem] = []
    for c in convs or []:
        if not isinstance(c, dict):
            continue
        grok_meta = c.get("conversation") or {}
        if not isinstance(grok_meta, dict):
            grok_meta = {}
        title = c.get("title") or c.get("name") or grok_meta.get("title") or "Untitled conversation"
        msgs = (
            c.get("messages")
            or c.get("mapping")
            or c.get("chat_messages")
            or c.get("responses")
            or []
        )
        if isinstance(msgs, dict):  # ChatGPT-style mapping object
            current_node = c.get("current_node")
            if isinstance(current_node, str) and current_node in msgs:
                active_messages = []
                seen = set()
                node_id: str | None = current_node
                while isinstance(node_id, str) and node_id and node_id not in seen:
                    seen.add(node_id)
                    node = msgs.get(node_id)
                    if not isinstance(node, dict):
                        break
                    message = node.get("message")
                    if isinstance(message, dict):
                        active_messages.append(message)
                    node_id = node.get("parent")
                msgs = list(reversed(active_messages))
            else:
                msgs = [v.get("message", {}) for v in msgs.values() if isinstance(v, dict)]
        lines = []
        for m in msgs:
            if not isinstance(m, dict):
                continue
            response: dict = {}
            if isinstance(m.get("response"), dict):
                response = m["response"]
            author: dict = {}
            if isinstance(m.get("author"), dict):
                author = m["author"]
            role = (
                m.get("role")
                or author.get("role")
                or m.get("sender")
                or response.get("sender")
                or "?"
            )
            content = m.get("content")
            if content is None and response:
                content = response.get("message") or response.get("content")
            if isinstance(content, dict):  # ChatGPT parts: {"parts": [...]}
                content = " ".join(str(p) for p in content.get("parts", []))
            elif isinstance(content, list):  # Claude-style blocks: [{"type": "text", "text": ...}]
                content = " ".join(
                    block.get("text", "")
                    for block in content
                    if isinstance(block, dict) and block.get("type") == "text"
                )
            if not content:
                content = m.get("text")  # some exports (Claude) also carry a flat text field
            content = (content or "").strip()
            if content:
                lines.append(f"{role}: {content}")
        if not lines:
            continue
        body = f"# {title}\n\n" + "\n".join(lines)
        out.append(
            MemoryItem(
                content=body,
                source=source,
                type="conversation",
                source_ref=c.get("id") or c.get("uuid") or source_ref or title,
            )
        )
    return out


# -- top-level sniffing dispatcher ----------------------------------------
_MEMORY_ADAPTERS = {
    "chatgpt": from_chatgpt,
    "claude": from_claude,
    "gemini": from_gemini,
    "grok": from_grok,
    "deepseek": from_deepseek,
    "perplexity": from_perplexity,
    "copilot": from_copilot,
    "telegram": from_telegram,
}


MAX_ARCHIVE_MEMBER_BYTES = 25 * 1024 * 1024
MAX_ARCHIVE_TOTAL_BYTES = 100 * 1024 * 1024
_ARCHIVE_IMPORT_SUFFIXES = {".json", ".csv", ".txt", ".md", ".markdown"}


def _normalize_archive(path: Path, source: str | None) -> list[MemoryItem]:
    """Import recognized files from a ZIP without extracting outside a temp dir."""
    items: list[MemoryItem] = []
    total = 0
    with zipfile.ZipFile(path) as archive:
        members = [member for member in archive.infolist() if not member.is_dir()]
        for member in members:
            name = PurePosixPath(member.filename)
            if name.is_absolute() or ".." in name.parts:
                continue
            if name.suffix.lower() not in _ARCHIVE_IMPORT_SUFFIXES:
                continue
            if member.file_size > MAX_ARCHIVE_MEMBER_BYTES:
                raise ValueError(f"Archive member is too large: {member.filename}")
            total += member.file_size
            if total > MAX_ARCHIVE_TOTAL_BYTES:
                raise ValueError("Archive contains too much importable data")
            with tempfile.TemporaryDirectory() as directory:
                target = Path(directory) / name.name
                target.write_bytes(archive.read(member))
                try:
                    nested = normalize_file(target, source=source)
                except (ValueError, UnicodeDecodeError):
                    continue
            for item in nested:
                item.source_ref = f"{path.name}/{member.filename}"
            items.extend(nested)
    return items


def normalize_file(path: str | Path, source: str | None = None) -> list[MemoryItem]:
    """Load JSON, CSV, TXT, Markdown, or safe ZIP exports into MemoryItems."""
    path = Path(path)
    if path.suffix.lower() == ".zip":
        return _normalize_archive(path, source)
    if path.suffix.lower() in {".md", ".markdown"}:
        text = path.read_text(encoding="utf-8-sig").strip()
        if not text:
            return []
        return [
            MemoryItem(
                content=text,
                source=source or "manual",
                type="note",
                source_ref=path.name,
            )
        ]
    if path.suffix.lower() == ".txt":
        text = path.read_text(encoding="utf-8-sig")
        if (
            (source or "").lower() == "whatsapp"
            or "whatsapp" in path.name.lower()
            or _looks_like_whatsapp_text(text)
        ):
            return from_whatsapp(text, source_ref=path.name)
        return from_plaintext(text, source=source or "manual", source_ref=path.name)
    if path.suffix.lower() == ".csv":
        return _normalize_csv_file(path, source_ref=path.name)
    try:
        data = json.loads(path.read_text(encoding="utf-8"))
    except json.JSONDecodeError as exc:
        raise ValueError(
            f"Could not parse '{path.name}': not valid JSON ({exc.msg} at line {exc.lineno})"
        ) from exc
    src = source or _guess_source(path, data)

    # Our own export format round-trips losslessly through its dedicated adapter.
    if isinstance(data, dict) and data.get("elesync_export"):
        return from_elesync(data, source_ref=path.name)

    # Claude.ai "Project" metadata export (uuid/name/docs) -- route regardless
    # of source, same as the conversation-shape check below.
    if _looks_like_claude_project(data):
        return from_claude_project(data, source_ref=path.name)

    # If it's clearly conversation history, route there regardless of source.
    if _looks_like_conversations(data):
        return from_conversations(data, source=src, source_ref=path.name)

    adapter = _MEMORY_ADAPTERS.get(src, from_chatgpt)
    items = adapter(data, source_ref=path.name)
    # last-resort fallback: if nothing parsed, try conversation shape
    return items or from_conversations(data, source=src, source_ref=path.name)


def _guess_source(path: Path, data) -> str:
    name = path.name.lower()
    for s in (
        "chatgpt",
        "openai",
        "claude",
        "anthropic",
        "gemini",
        "google",
        "grok",
        "xai",
        "deepseek",
        "perplexity",
        "copilot",
        "whatsapp",
        "telegram",
    ):
        if s in name:
            return {
                "openai": "chatgpt",
                "anthropic": "claude",
                "google": "gemini",
                "xai": "grok",
            }.get(s, s)
    if isinstance(data, dict) and "userMemories" in data:
        return "claude"
    # Claude and ChatGPT both name their export conversations.json, so the file
    # name can't tell them apart; Claude's chats carry `chat_messages`.
    if isinstance(data, list) and data and isinstance(data[0], dict) and "chat_messages" in data[0]:
        return "claude"
    if isinstance(data, dict) and "listItems" in data:
        return "gemini"
    if isinstance(data, dict) and "savedInfo" in data:
        return "gemini"
    if isinstance(data, dict) and "grok_memories" in data:
        return "grok"
    if _looks_like_telegram_export(data):
        return "telegram"
    if isinstance(data, dict) and isinstance(data.get("conversations"), list):
        conversations = data["conversations"]
        if conversations and isinstance(conversations[0], dict) and "responses" in conversations[0]:
            return "grok"
    if isinstance(data, dict) and "deepseek_memories" in data:
        return "deepseek"
    if isinstance(data, dict) and "perplexity_memories" in data:
        return "perplexity"
    if isinstance(data, dict) and "copilot_memories" in data:
        return "copilot"
    return "chatgpt"


def _looks_like_telegram_export(data) -> bool:
    messages = data.get("messages") if isinstance(data, dict) else None
    if not isinstance(messages, list) or not messages:
        return False
    sample = next((item for item in messages if isinstance(item, dict)), None)
    return bool(sample and ("from" in sample or "from_id" in sample) and "text" in sample)


def _looks_like_conversations(data) -> bool:
    convs = data.get("conversations") if isinstance(data, dict) else data
    if isinstance(convs, list) and convs and isinstance(convs[0], dict):
        return "messages" in convs[0] or "mapping" in convs[0] or "chat_messages" in convs[0]
    return False
