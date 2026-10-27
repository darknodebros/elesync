"""Direct links between memories (storage side).

A link is stored on its source memory: in the note's front-matter (the source
of truth) and mirrored in the ``memories.links`` column, so reindex rebuilds it
and every read-modify-write of a ``MemoryItem`` carries it along unchanged.
"""

from __future__ import annotations

import logging
from typing import TYPE_CHECKING

from .models import normalize_links, split_link

_log = logging.getLogger("elesync.store")

if TYPE_CHECKING:
    import sqlite3
    import threading
    from collections.abc import Callable

    from .models import MemoryItem


class LinkMixin:
    # Host-provided surface: ``MemoryStore`` in store.py defines every one of these.
    db: sqlite3.Connection
    _lock: threading.RLock
    get: Callable[[str], MemoryItem | None]
    _write_note: Callable[[MemoryItem], None]

    def set_links(self, mem_id: str, links) -> bool:
        """Replace a memory's outgoing links. Updates the index and the note in
        lock-step, like ``set_scopes``. Returns False if there is no such memory."""
        with self._lock:
            item = self.get(mem_id)
            if item is None:
                return False
            self._write_links_unsafe(item, normalize_links(links))
            self.db.commit()
            return True

    def incoming_links(self, mem_id: str) -> list[tuple[str, str]]:
        """``(source_id, relation)`` for every memory that links to ``mem_id``."""
        with self._lock:
            rows = self.db.execute(
                "SELECT id, links FROM memories WHERE links LIKE ?", (f"%:{mem_id}%",)
            ).fetchall()
        found = []
        for row in rows:
            for link in normalize_links(row["links"]):
                relation, target = split_link(link)
                if target == mem_id:
                    found.append((row["id"], relation))
        return found

    def _write_links_unsafe(self, item: MemoryItem, links: list[str]) -> None:
        """Caller holds the lock and commits. Rolls back if the note write fails,
        so the index never claims a link the note does not have."""
        item.links = links
        self.db.execute(
            "UPDATE memories SET links = ? WHERE id = ?", (", ".join(links) or None, item.id)
        )
        try:
            self._write_note(item)
        except Exception:
            self.db.rollback()
            raise

    def _drop_links_to_unsafe(self, mem_id: str) -> None:
        """Remove links pointing at a deleted memory from the notes that held them.

        Runs after the delete has committed and is best-effort per note: a link
        left dangling is harmless (reads skip missing targets), so one failed
        note write must never fail or undo the delete itself."""
        for source_id, _ in self.incoming_links(mem_id):
            item = self.get(source_id)
            if item is None:
                continue
            kept = [link for link in item.links if split_link(link)[1] != mem_id]
            try:
                self._write_links_unsafe(item, kept)
                self.db.commit()
            except Exception as exc:
                _log.warning(
                    "could not drop link to %s from %s: %s: %s",
                    mem_id[:8],
                    source_id[:8],
                    type(exc).__name__,
                    exc,
                )
