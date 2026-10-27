"""Service layer.

The recall / remember / forget / status operations live here as plain, testable
methods. The MCP server (mcp_server.py) is a thin wrapper that exposes these over
the protocol. Keeping the logic here means the behaviour an AI client actually
sees is covered by the test suite — not hidden inside an untested server closure.

Scoped sharing lives here too: a `MemoryService` is constructed with the calling
client's `grant` (the scopes it may see) and `write_scopes` (what new memories it
records get tagged with by default). Every read path filters by the grant, so a
scoped client cannot see — or even infer the existence of — memories outside its
compartment.
"""

from __future__ import annotations

import json
import os
import re
from dataclasses import replace

from . import conflicts as conflicts_mod
from . import scopes as scopes_mod
from .access import AccessGuard
from .audit import AuditLog
from .models import MemoryItem, _now, normalize_tags, split_link
from .results import RecallResult
from .service_links import LinkServiceMixin
from .store import MemoryStore

READONLY_MSG = "This client has read-only access — write operations are disabled."
_PROJECT_DOCUMENT_KINDS = {"knowledge", "task", "decision", "handoff"}
_PROJECT_CONTEXT_DEFAULT_LIMIT = 20
_PROJECT_CONTEXT_MAX_LIMIT = 50
_PROJECT_CONTEXT_DEFAULT_CHARS = 100_000
_PROJECT_CONTEXT_MAX_CHARS = 100_000


def _auto_supersede_enabled() -> bool:
    """Auto-supersession is on by default; set ELESYNC_AUTO_SUPERSEDE=0 (or false/no/off)
    to disable it without a code change."""
    return os.environ.get("ELESYNC_AUTO_SUPERSEDE", "1").strip().lower() not in {
        "0",
        "false",
        "no",
        "off",
    }


def _provenance_line(it) -> str:
    return f"   - [{it.source}, {it.created_at[:10]}] {it.content}  (id={it.id[:8]})"


def _supersede_proposal(grp) -> list[str]:
    """For an *isolated pair* (exactly two conflicting facts), offer to supersede
    one by the other — recoverable history, not a `forget`. No direction is assumed:
    both commands are shown so the user picks which fact is the update. Larger
    clusters get no proposal (they keep the choose-one `resolve-conflict` flow, since
    there's no clean "this updates that" pair to suggest)."""
    if len(grp) != 2:
        return []
    a, b = sorted(grp, key=lambda i: i.created_at)
    return [
        "   → if one updates the other, supersede (kept as history; either direction):",
        f"     ele supersede {a.id[:8]} --by {b.id[:8]}",
        f"     ele supersede {b.id[:8]} --by {a.id[:8]}",
    ]


def _parse_fields(raw: str) -> dict:
    """Parse the MCP `fields` argument: a JSON object of placeholder values.

    JSON rather than "k=v,k=v" because template values routinely contain commas
    and equals signs (a decision's rationale, a meeting topic), and a delimiter
    format would mangle exactly the fields worth capturing.
    """
    try:
        parsed = json.loads(raw)
    except (TypeError, ValueError) as e:
        raise ValueError(f'`fields` must be a JSON object, e.g. {{"who": "Ana"}} ({e}).') from e
    if not isinstance(parsed, dict):
        raise ValueError(f"`fields` must be a JSON object, got {type(parsed).__name__}.")
    return parsed


class MemoryService(LinkServiceMixin):
    def __init__(
        self,
        store: MemoryStore,
        grant: scopes_mod.Grant | None = None,
        write_scopes: list[str] | None = None,
        readonly: bool = False,
        audit: AuditLog | None = None,
        project_id: str | None = None,
        project_read: bool = True,
        project_write: bool = True,
        project_read_conversations: bool = True,
    ):
        """`grant` is the set of scopes this client may see (None = unrestricted,
        the default). `write_scopes` is what `remember` tags new memories with when
        the caller doesn't say otherwise — so e.g. a legal-scoped client records
        into the legal compartment automatically.

        `readonly` blocks all mutating operations (remember, forget, scope).

        `audit` receives an append-only log entry for every operation.  Pass
        ``AuditLog(None)`` (or ``None``) to disable."""
        self.store = store
        self.grant = grant
        self.write_scopes = scopes_mod.parse(write_scopes or [])
        self.readonly = readonly
        self.audit = audit or AuditLog(None)
        self.access = AccessGuard(
            project_id=project_id,
            readonly=readonly,
            project_read=project_read,
            project_write=project_write,
            project_read_conversations=project_read_conversations,
        )
        self.project_id = self.access.project_id
        self.project_read = self.access.project_read
        self.project_write = self.access.project_write
        self.project_read_conversations = self.access.project_read_conversations
        self._client_label = scopes_mod.describe(grant, readonly=readonly)

    def _resolve_project(self, project_id: str) -> dict:
        """Resolve a full project id or unique prefix to its canonical row."""
        prefix = project_id.strip() if isinstance(project_id, str) else ""
        matches = [project for project in self.store.projects() if project["id"].startswith(prefix)]
        if not prefix or not matches:
            raise ValueError("Project not found. Use list_projects to see available project ids.")
        if len(matches) > 1:
            raise ValueError("Project id is ambiguous. Use more characters from list_projects.")
        return matches[0]

    def _bound_project_id(self) -> str | None:
        if not self.project_id:
            return None
        return self._resolve_project(self.project_id)["id"]

    @staticmethod
    def _project_query_terms(query: str) -> list[str]:
        stop = {
            "a",
            "about",
            "an",
            "and",
            "are",
            "did",
            "do",
            "does",
            "for",
            "from",
            "in",
            "is",
            "of",
            "on",
            "or",
            "the",
            "to",
            "what",
            "when",
            "where",
            "which",
            "who",
            "with",
        }
        raw = re.findall(r"[\w'-]+", query.casefold())
        terms = [term for term in raw if len(term) >= 2 and term not in stop]
        return terms or [term for term in raw if len(term) >= 2]

    @classmethod
    def _matches_project_query(cls, text: str, query: str) -> bool:
        terms = cls._project_query_terms(query)
        haystack = text.casefold()
        return not terms or all(term in haystack for term in terms)

    def _search_project_documents(self, project_id: str, query: str, limit: int) -> list[dict]:
        documents = self.store.project_documents(project_id)
        matches = [
            document
            for document in documents
            if self._matches_project_query(
                f"{document.get('title', '')} {document.get('body', '')} ", query
            )
        ]
        return matches[:limit]

    def _visible(self, items: list[MemoryItem]) -> list[MemoryItem]:
        visible = items
        if self.grant is not None:
            visible = [i for i in visible if scopes_mod.is_visible(i.scopes, self.grant)]
        if not self.project_read_conversations:
            visible = [i for i in visible if i.type != "conversation"]
        return visible

    def _project_items(self) -> list[MemoryItem]:
        try:
            project_id = self._bound_project_id()
        except ValueError:
            return []
        items = self.store.project_memories(project_id) if project_id else self.store.all()
        return self._visible(items)

    def _project_allows(self, memory_id: str) -> bool:
        return not self.project_id or any(item.id == memory_id for item in self._project_items())

    def _project_actor(self) -> str:
        """A truthful provenance label when an MCP client changes a project."""
        return f"AI client ({self._client_label})"

    def recall_result(
        self,
        query: str,
        limit: int = 8,
        source: str = "",
        type: str = "",
        semantic_weight: float | None = None,
    ) -> RecallResult:
        try:
            self.access.require_read()
        except PermissionError:
            return RecallResult(access_denied=True)
        self.audit.log("RECALL", client=self._client_label, detail=f"query={query!r} limit={limit}")
        if self.project_id:
            try:
                project_id = self._bound_project_id()
            except ValueError:
                return RecallResult()
            assert project_id is not None
            hits = self.store.search_project(project_id, query, limit=limit, grant=self.grant)
            if source:
                hits = [item for item in hits if item.source == source]
            if type:
                hits = [item for item in hits if item.type == type]
        else:
            hits = self.store.search(
                query,
                limit=limit,
                source=source or None,
                mtype=type or None,
                grant=self.grant,
                semantic_weight=semantic_weight,
            )
        hits = self._visible(hits)
        resolved = self.store.resolved_pairs()
        conflicts = conflicts_mod.find_conflicts(
            hits, resolved_pairs=resolved, vectors=self._vectors()
        )
        return RecallResult(memories=hits, conflicts=conflicts)

    def recall(
        self,
        query: str,
        limit: int = 8,
        source: str = "",
        type: str = "",
        semantic_weight: float | None = None,
    ) -> str:
        from .formatters import format_recall_text

        return format_recall_text(
            self.recall_result(
                query, limit=limit, source=source, type=type, semantic_weight=semantic_weight
            )
        )

    def _vectors(self) -> dict[str, list[float]] | None:
        """Embedding dict for conflict detection, or None when unavailable.

        Pulled fresh from the store on each conflict scan so it reflects the
        current vault state (a stale dict would miss just-saved memories).
        Returns None when no embeddings exist — callers fall back to the
        lexical heuristic. Wrapped so a store error never breaks recall/conflicts.
        """
        try:
            return self.store.embeddings_dict()
        except Exception:
            return None

    def _get_clusters(self) -> list[list]:
        """Return conflict clusters, skipping already-resolved pairs."""
        resolved = self.store.resolved_pairs()
        return conflicts_mod.find_conflicts(
            self._visible(self.store.all()),
            resolved_pairs=resolved,
            vectors=self._vectors(),
        )

    def conflicts(self) -> str:
        """Surface facts that look like they're about the same thing but disagree,
        with provenance — so the user can resolve them (e.g. `forget` the stale one).
        Only memories this client is allowed to see are considered, so conflict
        output never leaks a memory from outside the client's scope."""
        clusters = self._get_clusters()
        if not clusters:
            return "No conflicting memories found."
        out = [f"Found {len(clusters)} possible conflict(s) — review and `forget` the wrong one:"]
        for n, grp in enumerate(clusters, 1):
            out.append(f"\n{n}. Same topic, different values:")
            out += [_provenance_line(it) for it in sorted(grp, key=lambda i: i.created_at)]
            out += _supersede_proposal(grp)
        return "\n".join(out)

    def conflicts_list(self) -> str:
        """List unresolved conflicts with stable cluster IDs for use with resolve-conflict."""
        clusters = self._get_clusters()
        if not clusters:
            return "No conflicting memories found."
        out = [
            f"Found {len(clusters)} unresolved conflict(s).",
            "Use `ele resolve-conflict <cluster-id> --choose <memory-id>` to resolve.",
        ]
        for n, grp in enumerate(clusters, 1):
            cid = conflicts_mod.cluster_id(grp)
            out.append(f"\n{n}. Cluster {cid}  (same topic, different values):")
            for it in sorted(grp, key=lambda i: i.created_at):
                out.append(
                    f"   id={it.id[:8]}  source={it.source}  date={it.created_at[:10]}"
                    f"  \u2192 {it.content}"
                )
            out += _supersede_proposal(grp)
        return "\n".join(out)

    def resolve_conflict(
        self,
        cluster_id_prefix: str,
        choose_id: str | None = None,
        strategy: str | None = None,
    ) -> str:
        """Resolve a conflict cluster by either choosing a specific memory to keep
        or applying an auto-resolution strategy (newest/oldest/longest).

        The chosen memory is kept; all others in the cluster are superseded by it
        — retained as recoverable history (hidden from recall, walkable via
        `history`) rather than deleted. The resolution is recorded so the cluster
        won't resurface.
        """
        try:
            self.access.require_write()
        except PermissionError:
            return READONLY_MSG
        if not choose_id and not strategy:
            return "Provide --choose <memory-id> or --strategy newest|oldest|longest."
        if choose_id and strategy:
            return "Provide either --choose or --strategy, not both."
        if strategy and strategy not in ("newest", "oldest", "longest"):
            return "Strategy must be one of: newest, oldest, longest."

        clusters = self._get_clusters()
        if not clusters:
            return "No unresolved conflicts found."

        # Find the cluster whose ID starts with the given prefix.
        matching = []
        for grp in clusters:
            cid = conflicts_mod.cluster_id(grp)
            if cid.startswith(cluster_id_prefix):
                matching.append((cid, grp))

        if not matching:
            return f"No conflict cluster found with id starting with '{cluster_id_prefix}'."
        if len(matching) > 1:
            ids = ", ".join(cid for cid, _ in matching)
            return f"Ambiguous cluster prefix '{cluster_id_prefix}' — matches: {ids}."

        cid, grp = matching[0]

        # Determine which memory to keep.
        if choose_id:
            # Resolve by prefix against cluster members.
            candidates = [m for m in grp if m.id.startswith(choose_id)]
            if not candidates:
                member_ids = ", ".join(m.id[:8] for m in grp)
                return (
                    f"Memory id '{choose_id}' not found in cluster {cid}. "
                    f"Cluster members: {member_ids}."
                )
            if len(candidates) > 1:
                return f"Ambiguous id prefix '{choose_id}' matches multiple cluster members."
            kept = candidates[0]
            used_strategy = None
        else:
            # Auto-resolve by strategy.
            if strategy == "newest":
                kept = max(grp, key=lambda m: m.created_at)
            elif strategy == "oldest":
                kept = min(grp, key=lambda m: m.created_at)
            else:  # longest
                kept = max(grp, key=lambda m: len(m.content))
            used_strategy = strategy

        # Supersede the losers by the kept memory — retain, don't destroy.
        at = _now()
        superseded = []
        for m in grp:
            if m.id != kept.id:
                self.store.supersede(m.id, kept.id, at)
                superseded.append(m.id[:8])

        # Record the resolution so this cluster is skipped in future scans.
        pairs = conflicts_mod.all_pairs(grp)
        self.store.record_resolution(cid, pairs, kept.id, used_strategy)

        superseded_str = ", ".join(superseded) if superseded else "none"
        return (
            f"Resolved cluster {cid}: kept id={kept.id[:8]} ({kept.source}), "
            f"superseded [{superseded_str}] (kept as history)."
        )

    def history(self, memory_id: str) -> str:
        """Show the supersession chain a memory belongs to — every version that
        led to or replaced it, oldest first — so an AI can see what a fact used
        to be and when it changed. This is the "what was true when" view."""
        self.audit.log("HISTORY", client=self._client_label, detail=f"id={memory_id!r}")
        target, err = self._resolve(memory_id)
        if target is None:
            return err
        # Gather the whole connected supersession component: walk forward along
        # `superseded_by` to the live head and backward through predecessors.
        seen: dict[str, MemoryItem] = {}
        frontier = [target]
        while frontier:
            mid = frontier.pop()
            if mid in seen:
                continue
            it = self.store.get(mid)
            if it is None:
                continue
            seen[mid] = it
            if it.superseded_by:
                frontier.append(it.superseded_by)
            frontier += [p.id for p in self.store.predecessors(mid)]
        # Scope safety: never reveal a version the client isn't allowed to see.
        chain = self._visible(list(seen.values()))
        if not chain:
            return "No such memory."
        chain.sort(key=lambda i: i.created_at)
        if len(chain) == 1 and not chain[0].superseded_by:
            it = chain[0]
            return (
                f"id={it.id[:8]} ({it.source}, {it.created_at[:10]}) — current version, "
                f"no supersession history.\n{it.content}"
            )
        lines = [f"Supersession history ({len(chain)} versions, oldest first):"]
        for it in chain:
            if it.superseded_by:
                state = f"superseded {it.superseded_at[:10]} → {it.superseded_by[:8]}"
            else:
                state = "current"
            lines.append(
                f"\n[{it.created_at[:10]}] id={it.id[:8]} ({it.source}) — {state}\n  {it.content}"
            )
        return "\n".join(lines)

    def list_projects(self) -> str:
        """List projects that contain at least one memory visible to this client."""
        try:
            self.access.require_read()
        except PermissionError as error:
            return str(error)
        rows = []
        projects = self.store.projects()
        if self.project_id:
            projects = [p for p in projects if p["id"].startswith(self.project_id)]
        for project in projects:
            visible = self._visible(self.store.project_memories(project["id"]))
            if visible or self.grant is None:
                rows.append((project, len(visible)))
        if not rows:
            return "No projects available to this client."
        lines = ["Projects:"]
        for project, count in rows:
            documents = int(project.get("document_count") or 0)
            lines.append(
                f"- {project['name']} (id={project['id'][:8]}, "
                f"{count} visible {'memory' if count == 1 else 'memories'}, "
                f"{documents} {'document' if documents == 1 else 'documents'})"
            )
        return "\n".join(lines)

    def recall_project(self, project_id: str, query: str, limit: int = 8) -> str:
        """Retrieve context from one project without searching the rest of the vault."""
        try:
            project = self._resolve_project(project_id)
        except ValueError as error:
            return str(error)
        try:
            self.access.require_read(project["id"])
        except (PermissionError, ValueError) as error:
            return str(error)
        limit = max(1, min(int(limit), 50))
        documents = self._search_project_documents(project["id"], query, limit)
        hits = self._visible(
            self.store.search_project(
                project["id"], query, limit=max(0, limit - len(documents)), grant=self.grant
            )
        )
        self.audit.log(
            "PROJECT_RECALL",
            client=self._client_label,
            detail=f"project={project['id'][:8]} query={query!r} limit={limit}",
        )
        if not documents and not hits:
            return f"No matching project material in project '{project['name']}'."
        lines = [f"Project: {project['name']}"]
        lines.extend(
            f"   - [document/{document['kind']}, {document['updated_at'][:10]}] "
            f"{document['title']}: {document['body'][:8_000]}  (id={document['id'][:8]})"
            for document in documents
        )
        lines.extend(_provenance_line(item) for item in hits)
        return "\n".join(lines)

    def project_context(
        self,
        project_id: str,
        query: str = "",
        document_kind: str = "",
        offset: int = 0,
        limit: int = _PROJECT_CONTEXT_DEFAULT_LIMIT,
        max_chars: int = _PROJECT_CONTEXT_DEFAULT_CHARS,
    ) -> dict:
        project = self._resolve_project(project_id)
        self.access.require_read(project["id"])
        self.audit.log(
            "PROJECT_CONTEXT",
            client=self._client_label,
            detail=f"project={project['id'][:8]}" + (f" query={query!r}" if query.strip() else ""),
        )
        kind = document_kind.strip().lower()
        if kind and kind not in _PROJECT_DOCUMENT_KINDS:
            raise ValueError("document_kind must be knowledge, task, decision, or handoff")
        offset = max(0, int(offset))
        limit = max(1, min(int(limit), _PROJECT_CONTEXT_MAX_LIMIT))
        max_chars = max(1_000, min(int(max_chars), _PROJECT_CONTEXT_MAX_CHARS))

        context = self.store.project_context(project["id"])
        allowed_ids = {item.id for item in self._project_items()}
        memories = [item for item in context["memories"] if item.get("id") in allowed_ids]
        documents = context["documents"]
        # Report what this client can see: the stored memory_count includes
        # memories outside its scope, which the memories list below excludes.
        visible_memory_count = len(memories)
        document_count = len(documents)
        if kind:
            documents = [document for document in documents if document.get("kind") == kind]
        if query.strip():
            documents = [
                document
                for document in documents
                if self._matches_project_query(
                    f"{document.get('title', '')} {document.get('body', '')}", query
                )
            ]
            memories = [
                memory
                for memory in memories
                if self._matches_project_query(memory.get("content", ""), query)
            ]

        document_total = len(documents)
        memory_total = len(memories)
        documents = [dict(document) for document in documents[offset : offset + limit]]
        memories = [dict(memory) for memory in memories[offset : offset + limit]]

        remaining = max_chars
        content_truncated = False

        def bounded(value: str) -> str:
            nonlocal remaining, content_truncated
            text = str(value)
            if len(text) > remaining:
                text = text[:remaining]
                content_truncated = True
            remaining -= len(text)
            return text

        project_data = dict(context["project"])
        project_data["memory_count"] = visible_memory_count
        project_data["document_count"] = document_count
        for key, value in project_data.items():
            if isinstance(value, str):
                project_data[key] = bounded(value)
        for document in documents:
            for key in ("id", "project_id", "title", "kind", "created_at", "updated_at"):
                if isinstance(document.get(key), str):
                    document[key] = bounded(document[key])
        for memory in memories:
            for key in ("id", "source", "type", "created_at"):
                if isinstance(memory.get(key), str):
                    memory[key] = bounded(memory[key])
        for document in documents:
            if isinstance(document.get("body"), str):
                document["body"] = bounded(document["body"])
        for memory in memories:
            if isinstance(memory.get("content"), str):
                memory["content"] = bounded(memory["content"])

        return {
            "project": project_data,
            "documents": documents,
            "memories": memories,
            "pagination": {
                "offset": offset,
                "limit": limit,
                "max_chars": max_chars,
                "documents": {
                    "total": document_total,
                    "returned": len(documents),
                    "has_more": offset + len(documents) < document_total,
                    "next_offset": (
                        offset + len(documents)
                        if offset + len(documents) < document_total
                        else None
                    ),
                },
                "memories": {
                    "total": memory_total,
                    "returned": len(memories),
                    "has_more": offset + len(memories) < memory_total,
                    "next_offset": (
                        offset + len(memories) if offset + len(memories) < memory_total else None
                    ),
                },
            },
            "content_truncated": content_truncated,
        }

    def save_project_document(
        self, project_id: str, title: str, body: str, kind: str = "knowledge"
    ) -> dict:
        project = self._resolve_project(project_id)
        self.access.require_write(project["id"])
        document = self.store.save_project_document(
            project["id"], title, body, kind, actor=self._project_actor()
        )
        self.audit.log(
            "PROJECT_DOCUMENT",
            client=self._client_label,
            detail=f"project={project['id'][:8]} kind={kind} title={title!r}",
        )
        return document

    def remember(
        self,
        content: str,
        type: str = "note",
        tags: str = "",
        source: str = "ai",
        scopes: str = "",
        supersedes: str = "",
        template: str = "",
        fields: str = "",
    ) -> str:
        """Save a durable memory. `scopes` (comma-separated) tags which compartment
        it belongs to; when omitted it falls back to this client's default
        write-scopes (often empty — i.e. `general`, visible to all).

        `supersedes` (a memory id or 8-char prefix) records that this new memory
        replaces an older one: the old memory is kept as recoverable history but
        hidden from recall, and the two are linked so `history` can show what
        changed and when. Use it when a fact updates rather than adds — e.g. a
        move, a rename, a changed preference.

        `template` (a name from the `memory://templates` resource) applies that
        template's type/tags/scopes defaults. Anything you pass explicitly wins
        over the template; tags union rather than replace.

        `fields` is a JSON object filling the template's placeholders, e.g.
        {"who": "Ana", "date": "2026-07-25"}. Supply it and the content is built
        from the template body, so captures of the same kind come out
        comparable. Get a placeholder wrong and the save is refused with a
        message naming the problem — you can correct it in the same turn, which
        is cheaper than a vault full of near-misses. Omit `fields` to write the
        content yourself and take only the template's metadata defaults."""
        try:
            self.access.require_write()
        except PermissionError:
            self.audit.log(
                "REMEMBER",
                client=self._client_label,
                detail="DENIED (read-only or project permission)",
            )
            return READONLY_MSG
        # Emptiness is checked *after* the template block: with `fields`, the
        # template builds the content, so an empty `content` is the normal case
        # rather than an error.
        content = (content or "").strip()
        if fields and not template:
            return "`fields` needs a `template` to fill — name one from memory://templates."
        if template:
            from . import templates as templates_mod

            try:
                tpl = templates_mod.get(self.store.vault_dir, template)
                if fields:
                    if content:
                        return (
                            f"Template {tpl.name!r} builds the content from `fields`; "
                            "pass one or the other, not both."
                        )
                    content = tpl.render(_parse_fields(fields))
            except templates_mod.TemplateError as e:
                return str(e)
            except ValueError as e:
                return str(e)
            merged = templates_mod.apply_defaults(
                tpl,
                type=type if type != "note" else None,
                tags=tags,
                scopes=scopes_mod.parse(scopes) if scopes else None,
                source=source if source != "ai" else None,
            )
            type, source = merged["type"], merged["source"]
            tags = ",".join(merged["tags"])
            if merged["scopes"]:
                scopes = ",".join(merged["scopes"])
        if not content.strip():
            return "Nothing to save: content was empty."
        if (source or "ai") == "ai":
            # Most assistants don't pass `source`. The connection already knows
            # which assistant is calling, so record that instead of the generic
            # "ai" (it is what briefs and handoff prompts attribute the fact to).
            from .audit import current_ai

            source = current_ai.get() or "ai"
        chosen = scopes_mod.parse(scopes) if scopes else list(self.write_scopes)
        item = MemoryItem(
            content=content,
            type=type,
            tags=normalize_tags(tags),  # also re-normalised in __post_init__
            source=source,
            scopes=chosen,
        )
        try:
            project_id = self._bound_project_id()
        except ValueError:
            return "This connection is limited to a project that no longer exists."
        mem_id, inserted = self.store.upsert_status(item)
        if not inserted:
            return f"Already in your vault (id={mem_id[:8]}) — nothing to add."
        if project_id:
            self.store.add_to_project(project_id, mem_id, actor=self._project_actor())
        where = "" if not chosen else f" in scope {scopes_mod.label_of(chosen)}"
        self.audit.log(
            "REMEMBER", client=self._client_label, memory_id=mem_id[:8], detail=f"type={type}"
        )
        saved = f"Saved (id={mem_id[:8]}){where}."
        if supersedes:
            saved += " " + self._supersede(mem_id, supersedes)
            return saved
        # Both the auto-supersede gate and the hint need the same two reads. Doing
        # them once and passing them down turns four vault loads per save into one;
        # they were the other half of why `remember()` slowed as the vault grew.
        live_facts = self._visible(self.store.all())
        resolved_pairs = self.store.resolved_pairs()
        acted = (
            self._auto_supersede(item, live_facts, resolved_pairs)
            if _auto_supersede_enabled()
            else ""
        )
        saved += acted
        if not acted:
            # Auto-supersession stayed its hand (e.g. an untagged update, which the
            # write-path gate deliberately won't act on). Offer a non-destructive
            # nudge instead — surface the pair, let the user accept it explicitly.
            self._flag_conflict(item, live_facts, resolved_pairs)
            saved += self._supersede_hint(item, live_facts, resolved_pairs)
        return saved

    def _flag_conflict(self, new_item: MemoryItem, live_facts, resolved_pairs) -> None:
        """Record a CONFLICT audit entry when a newly saved fact disagrees with
        existing facts, so the dashboard can surface it the moment it happens.
        Writes nothing to the vault and never raises."""
        import logging

        try:
            cluster = conflicts_mod.cluster_containing(
                new_item,
                live_facts,
                resolved_pairs=resolved_pairs,
                max_size=6,
                vectors=self._vectors(),
            )
        except Exception:
            logging.getLogger("elesync.service").exception(
                "conflict check suppressed for id=%s", new_item.id[:8]
            )
            return
        others = [m.id[:8] for m in cluster if m.id != new_item.id]
        if others:
            self.audit.log(
                "CONFLICT",
                client=self._client_label,
                memory_id=new_item.id[:8],
                detail="with=" + ",".join(others),
            )

    def _supersede_hint(self, new_item: MemoryItem, live_facts=None, resolved_pairs=None) -> str:
        """Non-destructive: if the freshly-saved fact lands in an *isolated pair*
        (a two-member `find_conflicts` cluster) that auto-supersession didn't act on,
        suggest an explicit `ele supersede` — without writing anything. Returns a
        leading-space fragment, or "" when there's no clean pair. Uses the permissive
        review bar (surfacing is cheap; only *acting* demands the strict gate). Never
        raises: any failure leaves a clean save result — but the reason is logged so
        a silently dropped hint isn't invisible."""
        import logging

        try:
            visible = self._visible(self.store.all()) if live_facts is None else live_facts
            resolved = self.store.resolved_pairs() if resolved_pairs is None else resolved_pairs
            # Only this item's component matters, and only if it is an isolated
            # pair — so walk outward from it and stop at three rather than
            # clustering every fact in the vault on every save.
            cluster = conflicts_mod.cluster_containing(
                new_item,
                visible,
                resolved_pairs=resolved,
                max_size=2,
                vectors=self._vectors(),
            )
            if len(cluster) == 2:
                other = next(m for m in cluster if m.id != new_item.id)
                return (
                    f" Looks like it may update id={other.id[:8]}; if so: "
                    f"`ele supersede {other.id[:8]} --by {new_item.id[:8]}`."
                )
        except Exception:
            logging.getLogger("elesync.service").exception(
                "supersede hint suppressed for id=%s", new_item.id[:8]
            )
            return ""
        return ""

    def _auto_supersede(self, new_item: MemoryItem, live_facts=None, resolved_pairs=None) -> str:
        """When a freshly-saved fact unambiguously updates exactly one live fact in
        the same scope, supersede that fact automatically (recoverable — hidden from
        recall, kept as history). Returns a status fragment (leading space) for the
        save result, or "" when nothing qualifies. Never raises: any failure leaves
        the save intact and history untouched."""
        candidate = conflicts_mod.auto_supersede_candidate(
            new_item,
            self._visible(self.store.all()) if live_facts is None else live_facts,
            resolved_pairs=self.store.resolved_pairs()
            if resolved_pairs is None
            else resolved_pairs,
            vectors=self._vectors(),
        )
        if candidate is None:
            return ""
        at = _now()
        if not self.store.supersede(candidate.id, new_item.id, at):
            return ""
        self.audit.log(
            "SUPERSEDE",
            client=self._client_label,
            memory_id=candidate.id[:8],
            detail=f"by={new_item.id[:8]} at={at} auto=1",
        )
        return (
            f" Auto-superseded id={candidate.id[:8]} (looked like an update; "
            f"kept as history — `history {new_item.id[:8]}` to review)."
        )

    def supersede(self, old_ref: str, by_ref: str) -> str:
        """User-invoked supersession: mark an existing memory `old_ref` as replaced
        by an existing memory `by_ref` (both ids or 8-char prefixes). The old memory
        is kept as recoverable history, hidden from recall — never deleted. This is
        the accept step for a proposal surfaced by `ele conflicts`. Never raises: a
        bad reference degrades to a message rather than a traceback."""
        try:
            self.access.require_write()
        except PermissionError:
            self.audit.log("SUPERSEDE", client=self._client_label, detail="DENIED (read-only)")
            return READONLY_MSG
        old_id, err = self._resolve(old_ref)
        if old_id is None:
            return f"(couldn't supersede '{old_ref}': {err})"
        new_id, err = self._resolve(by_ref)
        if new_id is None:
            return f"(couldn't supersede by '{by_ref}': {err})"
        if old_id == new_id:
            return "(supersede ignored: a memory can't supersede itself)"
        at = _now()
        if not self.store.supersede(old_id, new_id, at):
            return f"(couldn't supersede id={old_ref}: no such memory)"
        self.audit.log(
            "SUPERSEDE",
            client=self._client_label,
            memory_id=old_id[:8],
            detail=f"by={new_id[:8]} at={at}",
        )
        return f"Superseded id={old_id[:8]} by id={new_id[:8]} (kept as history)."

    def _supersede(self, new_id: str, old_ref: str) -> str:
        """Link an existing memory as superseded by `new_id`. Returns a status
        fragment appended to the remember result — never raises, so a bad
        `supersedes` reference degrades to a note rather than losing the save."""
        old_id, err = self._resolve(old_ref)
        if old_id is None:
            return f"(couldn't supersede '{old_ref}': {err})"
        if old_id == new_id:
            return "(supersedes ignored: a memory can't supersede itself)"
        at = _now()
        if not self.store.supersede(old_id, new_id, at):
            return f"(couldn't supersede id={old_ref}: no such memory)"
        self.audit.log(
            "SUPERSEDE",
            client=self._client_label,
            memory_id=old_id[:8],
            detail=f"by={new_id[:8]} at={at}",
        )
        return f"Superseded id={old_id[:8]} (kept as history)."

    def _resolve(self, memory_id: str) -> tuple[str | None, str]:
        """Resolve a full id or unambiguous 8-char prefix to a full id, honouring
        the client's grant (a scoped client can only address memories it can see).
        Returns (id, error_message); id is None on failure."""
        memory_id = (memory_id or "").strip()
        if not memory_id:
            return None, "No id given."
        # SQL prefix lookup avoids scanning every memory in Python (the 1.0.6 fix).
        if len(memory_id) >= 32:
            candidate_ids = [memory_id]
        else:
            candidate_ids = self.store.prefix_lookup(memory_id)
        # Scoped clients can only address memories their grant lets them see, so
        # the lookup never reveals the existence of out-of-scope ids.
        if self.grant is not None or self.project_id:
            visible_ids = {i.id for i in self._project_items()}
            candidate_ids = [cid for cid in candidate_ids if cid in visible_ids]
        if len(candidate_ids) == 1:
            return candidate_ids[0], ""
        if not candidate_ids:
            return None, "No such memory."
        return None, f"{len(candidate_ids)} memories match '{memory_id}'. Be more specific."

    def forget(self, memory_id: str) -> str:
        try:
            self.access.require_write()
        except PermissionError:
            self.audit.log("FORGET", client=self._client_label, detail="DENIED (read-only)")
            return READONLY_MSG
        target, err = self._resolve(memory_id)
        if target is None:
            return err
        deleted = self.store.delete(target)
        if deleted:
            self.audit.log("FORGET", client=self._client_label, memory_id=target[:8])
        return "Deleted." if deleted else "No such memory."

    def scope(self, memory_id: str, scopes: str) -> str:
        """Re-assign the scopes of an existing memory (full id or 8-char prefix).
        Empty `scopes` moves it back to `general` (visible to all)."""
        try:
            self.access.require_write()
        except PermissionError:
            self.audit.log("SCOPE", client=self._client_label, detail="DENIED (read-only)")
            return READONLY_MSG
        target, err = self._resolve(memory_id)
        if target is None:
            return err
        if self.project_id and not self._project_allows(target):
            return "This connection is limited to its assigned project."
        labels = scopes_mod.parse(scopes)
        if not self.store.set_scopes(target, labels):
            return "No such memory."
        self.audit.log(
            "SCOPE",
            client=self._client_label,
            memory_id=target[:8],
            detail=f"→ {scopes_mod.label_of(labels)}",
        )
        return f"Scoped (id={target[:8]}) → {scopes_mod.label_of(labels)}."

    def status(self) -> str:
        self.audit.log("STATUS", client=self._client_label)
        if self.grant is None and not self.project_id:
            s = self.store.stats()
            total = s["total"]
            superseded = s["superseded"]
            by_src = ", ".join(f"{k}:{v}" for k, v in s["by_source"].items()) or "empty"
            by_typ = ", ".join(f"{k}:{v}" for k, v in s["by_type"].items()) or "empty"
        else:
            # Scoped or project-bound client: report only what it can see, so totals
            # do not leak the rest of the vault.
            visible = self._project_items()
            total = len(visible)
            superseded = 0  # scoped clients get the live view only
            src: dict[str, int] = {}
            typ: dict[str, int] = {}
            for it in visible:
                src[it.source] = src.get(it.source, 0) + 1
                typ[it.type] = typ.get(it.type, 0) + 1
            by_src = ", ".join(f"{k}:{v}" for k, v in src.items()) or "empty"
            by_typ = ", ".join(f"{k}:{v}" for k, v in typ.items()) or "empty"
        scope_note = "" if self.grant is None else f" Scope: {scopes_mod.describe(self.grant)}."
        sup_note = f" ({superseded} superseded, kept as history)" if superseded else ""
        return f"{total} memories{sup_note}. By source: {by_src}. By type: {by_typ}.{scope_note}"

    def scope_summary(self) -> str:
        """List the scope compartments visible to this client, with counts."""
        counts = self.store.scope_counts(grant=self.grant)
        if not counts:
            return "Vault empty."
        # general first, then the rest alphabetically.
        ordered = sorted(counts.items(), key=lambda kv: (kv[0] != scopes_mod.GENERAL, kv[0]))
        lines = [f"  {label:<16} {n}" for label, n in ordered]
        return "Scopes ({}):\n{}".format(scopes_mod.describe(self.grant), "\n".join(lines))

    def recent(self, n: int = 25) -> str:
        items = self._visible(self.store.all())[:n]
        if self.grant is not None or self.project_id:
            # A note's links name other memories; keep only the ones this client can see.
            visible = {i.id for i in self._project_items()}
            items = [
                replace(i, links=[link for link in i.links if split_link(link)[1] in visible])
                for i in items
            ]
        return "\n\n".join(i.to_markdown() for i in items) or "Vault empty."

    def templates(self) -> str:
        """Available capture templates, as a digest a model can act on.

        Fields are listed in the order `remember`'s `fields` argument expects
        them, and the body is shown verbatim so the model can see the shape it
        is filling rather than infer it from the field names.
        """
        from . import templates as templates_mod

        available, errors = templates_mod.load_all(self.store.vault_dir)
        if not available:
            return "No templates available."
        blocks = []
        for name in sorted(available):
            tpl = available[name]
            blocks.append(
                f"## {name}\n"
                f"{tpl.description}\n"
                f"- sets: type={tpl.type}"
                + (f", tags={','.join(tpl.tags)}" if tpl.tags else "")
                + (f", scopes={','.join(tpl.scopes)}" if tpl.scopes else "")
                + "\n"
                + f"- fields: {', '.join(tpl.fields) or 'none'}\n"
                + (f"- body: {tpl.body}" if tpl.body else "- body: (none — write content yourself)")
            )
        out = "\n\n".join(blocks)
        if errors:
            # A template the user wrote but that won't parse is worth saying out
            # loud here; the model is otherwise told it doesn't exist.
            broken = ", ".join(sorted(errors))
            out += f"\n\nUnreadable template file(s): {broken}"
        return out

    def audit_summary(
        self, *, operation: str | None = None, client: str | None = None, last: int = 50
    ) -> str:
        """Return a formatted summary of the audit log."""
        entries = self.audit.read(operation=operation, client=client, last=last)
        return self.audit.format_entries(entries)
