"""Append-only audit log for MCP client operations.

Every ``remember``, ``recall``, ``forget``, ``scope`` and ``status`` call that
hits the service layer can be logged here — so a vault owner can see exactly who
did what and when, across all connected AI clients.

Design choices:

  * **JSON-lines** (one JSON object per line, ``audit.jsonl``).  Easier to parse
    than TSV, handles embedded commas/quotes without escaping headaches, and
    streams cleanly — ``tail -f audit.jsonl | jq`` is instant visibility.
  * **Append-only, thread-safe.**  Multiple threads (e.g. FastMCP workers) can
    log concurrently; a reentrant lock serialises writes.  No rotation or
    truncation — the owner manages the file themselves (or ``ele audit clear``).
  * **Fail-open.**  If the log can't be written (permissions, disk full), the
    vault operation still succeeds — audit is observability, not a gate.
  * **On by default for connected AI clients.** The MCP server enables the log
    unless ``ELESYNC_AUDIT=0`` (or another explicit false value) is set. The
    CLI also supports ``--no-audit`` for an explicit opt-out.
  * ``AuditLog(None)`` remains a silent no-op for internal callers that need to
    disable logging explicitly.
"""

from __future__ import annotations

import contextvars
import json
import re
import threading
from datetime import datetime, timezone
from pathlib import Path

AUDIT_FILE = "audit.jsonl"

# Which AI assistant the current request comes from (e.g. "claude"). The MCP
# server sets it per tool call; every entry logged in that context records it,
# so callers don't have to thread the name through each log() call.
current_ai: contextvars.ContextVar[str] = contextvars.ContextVar("elesync_current_ai", default="")

_AI_ALIASES = (
    ("claude", "claude"),
    ("anthropic", "claude"),
    ("chatgpt", "chatgpt"),
    ("openai", "chatgpt"),
    ("gemini", "gemini"),
    ("google", "gemini"),
    ("grok", "grok"),
    ("xai", "grok"),
    ("perplexity", "perplexity"),
    ("copilot", "copilot"),
    ("deepseek", "deepseek"),
    ("cursor", "cursor"),
    ("windsurf", "windsurf"),
)


def audit_enabled() -> bool:
    """Audit logging is on unless ELESYNC_AUDIT is set to a false value."""
    import os

    return os.environ.get("ELESYNC_AUDIT", "1").strip().lower() not in {"0", "false", "off", "no"}


def record_capture(vault_dir, memory_ids, *, device: str = "phone") -> None:
    """Log memories saved from a device (e.g. the phone app) so every open
    dashboard shows them live, attributed to that device."""
    ids = [m for m in memory_ids if m]
    if not ids or not audit_enabled():
        return

    def run() -> None:
        current_ai.set(device)
        log = AuditLog(vault_dir)
        for memory_id in ids:
            log.log("CAPTURE", client="dashboard", memory_id=memory_id[:8])

    contextvars.copy_context().run(run)


def normalize_ai_name(name: object) -> str:
    """Map an MCP client name or provider id to a short assistant label."""
    low = str(name or "").strip().lower()
    for needle, label in _AI_ALIASES:
        if needle in low:
            return label
    return re.sub(r"[^a-z0-9 ._-]", "", low)[:40]


# Operations logged.
OP_REMEMBER = "REMEMBER"
OP_RECALL = "RECALL"
OP_FORGET = "FORGET"
OP_SCOPE = "SCOPE"
OP_STATUS = "STATUS"
OP_CONFLICTS = "CONFLICTS"

ALL_OPS = frozenset({OP_REMEMBER, OP_RECALL, OP_FORGET, OP_SCOPE, OP_STATUS, OP_CONFLICTS})


class AuditLog:
    """Append-only JSON-lines audit log.

    Pass ``log_dir=None`` to get a silent no-op instance (no file created, every
    method returns immediately).  This lets callers unconditionally call
    ``self.audit.log(...)`` without checking whether auditing is enabled.
    """

    def __init__(self, log_dir: str | Path | None):
        if log_dir is None:
            self.log_file: Path | None = None
        else:
            d = Path(log_dir).expanduser()
            d.mkdir(parents=True, exist_ok=True)
            self.log_file = d / AUDIT_FILE
        self._lock = threading.RLock()

    @property
    def enabled(self) -> bool:
        return self.log_file is not None

    # ── write ────────────────────────────────────────────────────────────

    def log(
        self,
        operation: str,
        *,
        client: str = "",
        memory_id: str = "",
        detail: str = "",
    ) -> None:
        """Append a single audit entry.  Silently no-ops when disabled or on
        write failure (fail-open: never block the vault operation)."""
        if self.log_file is None:
            return
        entry = {
            "ts": datetime.now(timezone.utc).replace(microsecond=0).isoformat(),
            "op": operation,
            "client": client,
            "mem": memory_id,
            "detail": detail,
        }
        ai = current_ai.get()
        if ai:
            entry["ai"] = ai
        with self._lock:
            try:
                with self.log_file.open("a", encoding="utf-8") as f:
                    f.write(json.dumps(entry, separators=(",", ":")) + "\n")
            except OSError:
                pass  # fail-open

    # ── read / query ─────────────────────────────────────────────────────

    def read(
        self,
        *,
        operation: str | None = None,
        client: str | None = None,
        last: int = 0,
    ) -> list[dict]:
        """Return audit entries, newest first, with optional filters.

        ``last`` limits the number of entries returned (0 = all). With a limit,
        the file is read backwards from the end and only as far as needed, so
        polling the newest entries costs the same however long the log grows.
        """
        if self.log_file is None or not self.log_file.exists():
            return []
        entries: list[dict] = []
        try:
            for raw in self._lines_newest_first():
                line = raw.strip()
                if not line:
                    continue
                try:
                    entry = json.loads(line)
                except json.JSONDecodeError:
                    continue
                if operation and entry.get("op") != operation:
                    continue
                if client and entry.get("client") != client:
                    continue
                entries.append(entry)
                if 0 < last <= len(entries):
                    break
        except OSError:
            return []
        return entries

    def _lines_newest_first(self, block: int = 64 * 1024):
        """Yield the log's lines from the last to the first, reading the file
        backwards in blocks. A line split across blocks is joined before it is
        yielded, and undecodable bytes are replaced rather than raising."""
        assert self.log_file is not None
        with self.log_file.open("rb") as f:
            f.seek(0, 2)
            pos = f.tell()
            pending = b""
            while pos > 0:
                step = min(block, pos)
                pos -= step
                f.seek(pos)
                chunk = f.read(step) + pending
                lines = chunk.split(b"\n")
                # The first piece may continue in the previous block.
                pending = lines.pop(0)
                for line in reversed(lines):
                    yield line.decode("utf-8", errors="replace")
            if pending:
                yield pending.decode("utf-8", errors="replace")

    def format_entries(self, entries: list[dict]) -> str:
        """Human-readable multi-line rendering for CLI / MCP output."""
        if not entries:
            return "No audit entries."
        lines: list[str] = []
        for e in entries:
            ts = e.get("ts", "?")
            op = e.get("op", "?")
            client = e.get("client", "")
            mem = e.get("mem", "")
            detail = e.get("detail", "")
            parts = [f"[{ts}] {op}"]
            if client:
                parts.append(f"client={client}")
            if mem:
                parts.append(f"mem={mem}")
            if detail:
                parts.append(detail)
            lines.append("  ".join(parts))
        return "\n".join(lines)

    # ── maintenance ──────────────────────────────────────────────────────

    def clear(self) -> bool:
        """Delete the audit log file.  Returns True if a file was removed."""
        if self.log_file is None:
            return False
        with self._lock:
            if self.log_file.exists():
                self.log_file.unlink()
                return True
        return False

    def count(self) -> int:
        """Number of entries in the log (O(n) — reads the whole file)."""
        if self.log_file is None or not self.log_file.exists():
            return 0
        try:
            with self.log_file.open("r", encoding="utf-8") as f:
                return sum(1 for line in f if line.strip())
        except OSError:
            return 0
