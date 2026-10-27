"""Direct links between memories (service side): link, unlink and traverse.

Every id goes through the client's grant, so a scoped or project-bound client
can neither link to nor learn about a memory it cannot see.
"""

from __future__ import annotations

from typing import TYPE_CHECKING

from .models import LINK_RELATIONS, MAX_LINKS, MemoryItem, normalize_links, split_link

if TYPE_CHECKING:
    from collections.abc import Callable

    from .access import AccessGuard
    from .audit import AuditLog
    from .store import MemoryStore

_PREVIEW_CHARS = 160


def _preview(item: MemoryItem) -> str:
    text = " ".join(item.content.split())
    return text if len(text) <= _PREVIEW_CHARS else text[: _PREVIEW_CHARS - 1] + "…"


class LinkServiceMixin:
    # Host-provided surface: ``MemoryService`` in service.py defines every one of these.
    store: MemoryStore
    access: AccessGuard
    audit: AuditLog
    project_id: str | None
    _client_label: str
    _resolve: Callable[[str], tuple[str | None, str]]
    _visible: Callable[[list[MemoryItem]], list[MemoryItem]]
    _project_items: Callable[[], list[MemoryItem]]

    def link_memories(
        self, source_ref: str, target_ref: str, relation: str = "related", remove: bool = False
    ) -> str:
        """Add (or with ``remove``, delete) a ``relation`` link from one memory
        to another. Never raises: a bad reference degrades to a message."""
        from .service import READONLY_MSG

        op = "UNLINK" if remove else "LINK"
        try:
            self.access.require_write()
        except PermissionError:
            self.audit.log(op, client=self._client_label, detail="DENIED (read-only)")
            return READONLY_MSG
        relation = (relation or "related").strip().lower()
        if relation not in LINK_RELATIONS:
            return (
                f"Unknown relation '{relation}'. Use one of: {', '.join(LINK_RELATIONS)}. "
                "To mark a memory as replaced by another, use supersede instead."
            )
        source_id, err = self._resolve(source_ref)
        if source_id is None:
            return f"(couldn't link from '{source_ref}': {err})"
        target_id, err = self._resolve(target_ref)
        if target_id is None:
            return f"(couldn't link to '{target_ref}': {err})"
        if source_id == target_id:
            return "(link ignored: a memory can't link to itself)"
        # `_resolve` already limits both ids to the client's grant and project.
        item = self.store.get(source_id)
        if item is None:
            return "No such memory."
        link = f"{relation}:{target_id}"
        if not normalize_links([link]):
            return f"(couldn't link to id={target_id[:8]}: its id is not one links can store)"
        pair = f"id={source_id[:8]} {relation} id={target_id[:8]}"
        if remove:
            if link not in item.links:
                return f"No {relation} link from id={source_id[:8]} to id={target_id[:8]}."
            links = [existing for existing in item.links if existing != link]
        else:
            if link in item.links:
                return f"Link already exists: {pair}."
            if len(item.links) >= MAX_LINKS:
                return f"id={source_id[:8]} already has {MAX_LINKS} links, the limit."
            links = [*item.links, link]
        self.store.set_links(source_id, links)
        self.audit.log(
            op,
            client=self._client_label,
            memory_id=source_id[:8],
            detail=f"{relation}→{target_id[:8]}",
        )
        return f"Removed link: {pair}." if remove else f"Linked: {pair}."

    def related_memories(self, memory_ref: str) -> str:
        """Memories directly linked to or from ``memory_ref``, both directions."""
        self.audit.log("RELATED", client=self._client_label, detail=f"id={memory_ref!r}")
        memory_id, err = self._resolve(memory_ref)
        if memory_id is None:
            return err
        item = self.store.get(memory_id)
        if item is None:
            return "No such memory."
        edges = [(*split_link(link), "→") for link in item.links]
        edges += [
            (relation, source, "←") for source, relation in self.store.incoming_links(memory_id)
        ]
        found = [self.store.get(other) for _, other, _ in edges]
        neighbours = {i.id: i for i in self._visible([i for i in found if i is not None])}
        if self.project_id:
            in_project = {i.id for i in self._project_items()}
            neighbours = {k: v for k, v in neighbours.items() if k in in_project}
        lines = [
            f"{relation} {arrow} id={other[:8]} ({neighbours[other].source}, "
            f"{neighbours[other].created_at[:10]}): {_preview(neighbours[other])}"
            for relation, other, arrow in edges
            if other in neighbours
        ]
        if not lines:
            return f"No linked memories for id={memory_id[:8]}."
        head = f"Links for id={memory_id[:8]} (→ outgoing from it, ← incoming to it):"
        return "\n".join([head, *lines])
