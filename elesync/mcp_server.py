"""MCP server — the connector.

This is the heart of the whole idea. It exposes your local vault over the Model
Context Protocol, which OpenAI, Google and Anthropic all support natively. Point
Claude, ChatGPT, Cursor, or any MCP client at this server and they can all
`recall` from and `remember` into the *same* vault, live. No pasting, no static
"memory chips", no switching apps.

The actual logic lives in service.py (and is unit-tested); this file is the thin
protocol wrapper around it.

Run:
    pip install -e .                 # from a repository clone
    ELESYNC_DIR=~/EleSyncVault python -m elesync.mcp_server

Easiest path:  `ele onboard`  wires this into your AI client for you.
"""

from __future__ import annotations

import contextvars
import os
from pathlib import Path
from typing import Protocol

from . import default_vault_dir
from .access import connection_permissions
from .audit import AuditLog, current_ai, normalize_ai_name
from .service import MemoryService
from .store import MemoryStore


class _McpWithStore(Protocol):
    """Structural type so mypy accepts the dynamic ``elesync_store`` attribute
    we attach to FastMCP instances (which don't declare it)."""

    elesync_store: MemoryStore


# `mcp` is an external dependency. Imported lazily so the rest of the package —
# and the test suite — work without it installed.
try:
    from mcp.server.fastmcp import FastMCP
except ImportError:  # pragma: no cover
    FastMCP = None  # type: ignore[assignment,misc]


def build_server(
    vault_dir: str | None = None,
    host: str = "127.0.0.1",
    port: int = 8000,
    allowed_hosts: list[str] | None = None,
    auth_token: str | None = None,
):
    if FastMCP is None:  # pragma: no cover
        raise ImportError('MCP SDK not found or incompatible. Run:  pip install "mcp>=1.2.0,<2"')

    vault_dir = default_vault_dir(vault_dir)
    # A project-bound connection must never create its vault: an empty stand-in
    # would hide that the real one (often a deleted demo folder) is gone.
    if os.environ.get("ELESYNC_PROJECT_ID") and not Path(vault_dir).expanduser().is_dir():
        raise RuntimeError(
            f"EleSync vault folder does not exist: {Path(vault_dir).expanduser()}. "
            f"This connection is bound to project {os.environ['ELESYNC_PROJECT_ID'][:8]}. "
            "Point ELESYNC_DIR at the vault that holds the project, or remove this "
            "server from your MCP client config."
        )
    # Opt into semantic recall when ELESYNC_SEMANTIC is set and the extra is present.
    embedder = None
    if os.environ.get("ELESYNC_SEMANTIC"):
        from . import embeddings

        embedder = embeddings.load_embedder()
    # Serve an encrypted vault by unlocking it with $ELESYNC_PASSPHRASE (held in memory
    # for the process). Refuse rather than silently fall back to plaintext.
    key = None
    from . import crypto

    if crypto.is_encrypted(vault_dir):
        import base64

        pw = os.environ.get("ELESYNC_PASSPHRASE")
        if not pw:
            server_key = os.environ.get("ELESYNC_SERVER_KEY")
            if server_key:
                try:
                    from . import credential_store

                    pw = credential_store.get_secret(f"vault:{server_key}")
                except Exception:
                    pw = None
        if not (pw and crypto.available()):
            raise RuntimeError(
                "vault is encrypted — set ELESYNC_PASSPHRASE (and install the "
                '"encryption" extra) so the server can unlock it.'
            )
        meta = crypto.read_meta(vault_dir)
        if meta is None:
            raise RuntimeError(
                "This vault looks encrypted, but its encryption details (vault.json) "
                "are missing or unreadable. If the vault was moved or restored, make "
                "sure vault.json sits in the vault folder next to the notes/ directory."
            )
        key = crypto.derive_key(
            pw, base64.b64decode(meta["salt"]), meta["opslimit"], meta["memlimit"]
        )
    # Scoped sharing: this client only sees the scopes it's granted (ELESYNC_SCOPES),
    # and records new memories into ELESYNC_WRITE_SCOPE by default. Unset == the full
    # vault, so existing single-client setups are unchanged.
    #
    # Read-only mode: ELESYNC_SCOPES=readonly:writing,legal — the client can
    # recall but cannot remember, forget, or re-scope memories.
    #
    # Audit logging is enabled by default. Set ELESYNC_AUDIT=0 to opt out;
    # enabled logging writes an append-only JSONL log inside the vault.
    from . import scopes as scopes_mod

    raw_scopes = os.environ.get("ELESYNC_SCOPES")
    readonly = scopes_mod.is_readonly(raw_scopes)
    scopes_value = scopes_mod.strip_readonly(raw_scopes) if raw_scopes else raw_scopes
    grant = scopes_mod.grant_from(scopes_value)
    write_scopes = scopes_mod.parse(os.environ.get("ELESYNC_WRITE_SCOPE", ""))

    audit_env = os.environ.get("ELESYNC_AUDIT")
    audit_setting = (audit_env if audit_env is not None else "1").strip().lower()
    audit_dir = vault_dir if audit_setting not in {"0", "false", "off", "no"} else None
    audit = AuditLog(audit_dir)

    project_id = os.environ.get("ELESYNC_PROJECT_ID") or None
    project_provider = os.environ.get("ELESYNC_PROJECT_PROVIDER", "").strip().lower()
    from .context import create_context

    ctx = create_context(
        vault_dir,
        config={},
        grant=grant,
        write_scopes=write_scopes,
        readonly=readonly,
        audit=audit,
        embedder=embedder,
        key=key,
        project_id=project_id,
    )
    store = ctx.store
    svc = ctx.service
    bound_project: dict[str, str] = {}

    def _bound_project_id() -> str | None:
        """The project this connection is bound to, looked up when a tool runs.

        An MCP client starts its servers when it opens, which can be before the
        project exists (created later from the dashboard), so a missing project
        is refused per call rather than failing the whole server. It never falls
        back to the unrestricted vault."""
        if not project_id:
            return None
        if "id" not in bound_project:
            matches = [p["id"] for p in store.projects() if p["id"].startswith(project_id)]
            if len(matches) != 1:
                raise PermissionError(
                    f"This connection is bound to EleSync project {project_id}, "
                    "which does not exist in this vault."
                )
            bound_project["id"] = matches[0]
        return bound_project["id"]

    oauth_context: contextvars.ContextVar[str | None] = contextvars.ContextVar(
        "elesync_oauth_context", default=None
    )

    def _request_ai_name(access: dict | None) -> str:
        provider = str((access or {}).get("provider") or "").strip()
        if provider:
            return normalize_ai_name(provider)
        if project_provider:
            return normalize_ai_name(project_provider)
        try:
            info = mcp.get_context().session.client_params.clientInfo
        except Exception:
            return ""
        return normalize_ai_name(getattr(info, "name", ""))

    def _active_service() -> MemoryService:
        oauth_token = oauth_context.get()
        access = None
        if oauth_token is not None:
            from . import oauth

            # Stateful MCP sessions can retain the context of their initial
            # request. Reload the token on each call so that session reuse
            # cannot retain reduced permissions or bypass expiry/revocation.
            access = oauth.validate_token(vault_dir, oauth_token)
            if access is None:
                raise PermissionError("This OAuth authorization has expired or been revoked.")
        current_ai.set(_request_ai_name(access))
        resolved_project_id = None if access else _bound_project_id()
        active_project_id = access.get("project_id") if access else resolved_project_id
        permissions = access.get("permissions", {}) if access else None
        if access:
            # A token is a ceiling on delegated access, not a replacement for
            # the current dashboard grant. Recheck on every tool/resource call,
            # including calls within an already authenticated MCP session.
            current = connection_permissions(
                store,
                str(access.get("project_id") or ""),
                str(access.get("provider") or "").strip().lower(),
                audit=audit,
                client_label=scopes_mod.describe(grant, readonly=readonly),
            )
            permissions = {
                name: bool(access.get("permissions", {}).get(name, False)) and allowed
                for name, allowed in current.items()
            }
        if permissions is None and resolved_project_id:
            provider = project_provider
            rows = store.project_access(resolved_project_id)
            # v1.24.26 dashboard-generated project connections did not include
            # provider metadata. A Claude grant is an unambiguous migration hint.
            if not provider and any(row["provider"] == "claude" for row in rows):
                provider = "claude"
            if provider:
                permissions = connection_permissions(
                    store,
                    resolved_project_id,
                    provider,
                    audit=audit,
                    client_label=scopes_mod.describe(grant, readonly=readonly),
                )
        if permissions is None:
            return svc
        return MemoryService(
            store,
            grant=grant,
            write_scopes=write_scopes,
            readonly=readonly,
            audit=audit,
            project_id=active_project_id,
            project_read=bool(permissions.get("can_read", False)),
            project_write=bool(permissions.get("can_write", False)),
            project_read_conversations=bool(permissions.get("can_read_conversations", False)),
        )

    transport_security = None
    if allowed_hosts:
        # Extend (not replace) DNS rebinding protection: keep localhost
        # allowed, plus explicitly trust the given public hostname(s) --
        # e.g. an ngrok domain or your own reverse-proxy host.
        from mcp.server.transport_security import TransportSecuritySettings

        host_patterns = ["127.0.0.1:*", "localhost:*", "[::1]:*"]
        origin_patterns = [
            "http://127.0.0.1:*",
            "http://localhost:*",
            "https://127.0.0.1:*",
            "https://localhost:*",
        ]
        for h in allowed_hosts:
            host_patterns += [h, f"{h}:*"]
            origin_patterns += [f"https://{h}", f"https://{h}:*", f"http://{h}", f"http://{h}:*"]
        transport_security = TransportSecuritySettings(
            enable_dns_rebinding_protection=True,
            allowed_hosts=host_patterns,
            allowed_origins=origin_patterns,
        )

    # ── Bearer token auth middleware ────────────────────────────────────
    # When a token is configured, every HTTP/SSE request must carry:
    #   Authorization: Bearer <token>
    # Rejections are logged WITHOUT including the token value.
    # Imported unconditionally: needed by the config-lookup path below *and*
    # by the Bearer-auth middleware further down when a caller passes
    # `auth_token` directly (skipping the config-lookup branch entirely).
    # A previous "lint cleanup" removed this import from the second site as
    # a seeming duplicate, which left `_auth_mod` undefined -> NameError the
    # first time a request hit the middleware on that code path.
    from . import auth as _auth_mod
    from .cli import _load_config as _lc

    _loaded_config = _lc()
    _auth_token = auth_token or _auth_mod.get_configured_token(_loaded_config)

    mcp = FastMCP("elesync", host=host, port=port, transport_security=transport_security)
    _oauth_file_exists = (Path(vault_dir) / ".oauth.json").exists()

    if _auth_token or _oauth_file_exists:
        import logging

        _auth_logger = logging.getLogger("elesync.auth")

        try:
            # FastMCP exposes the underlying Starlette app via .app for middleware.
            from starlette.middleware.base import BaseHTTPMiddleware
            from starlette.responses import JSONResponse

            _token_snapshot = _auth_token  # capture for the closure

            class _BearerAuthMiddleware(BaseHTTPMiddleware):
                async def dispatch(self, request, call_next):
                    auth_header = request.headers.get("Authorization", "")
                    if not auth_header.startswith("Bearer "):
                        _auth_logger.warning(
                            "Auth failure: missing or malformed Authorization header from %s %s",
                            request.client.host if request.client else "unknown",
                            request.url.path,
                        )
                        return JSONResponse({"error": "Unauthorized"}, status_code=401)
                    provided = auth_header[len("Bearer ") :]
                    access = None
                    if _token_snapshot and _auth_mod.verify_token(provided, _token_snapshot):
                        pass
                    else:
                        from . import oauth

                        access = oauth.validate_token(vault_dir, provided)
                        if access is None:
                            _auth_logger.warning(
                                "Auth failure: invalid token from %s %s",
                                request.client.host if request.client else "unknown",
                                request.url.path,
                            )
                            return JSONResponse({"error": "Unauthorized"}, status_code=401)
                    marker = oauth_context.set(provided if access is not None else None)
                    try:
                        return await call_next(request)
                    finally:
                        oauth_context.reset(marker)

            # Attach middleware to the actual Starlette app that gets served.
            #
            # IMPORTANT: FastMCP does NOT cache a Starlette app on `self.app`
            # or `self._app` -- streamable_http_app()/sse_app() each build and
            # return a brand-new Starlette instance on every call, and
            # run_streamable_http_async()/run_sse_async() pass that instance
            # straight to uvicorn without storing it anywhere on `self`. An
            # earlier version of this code tried to grab the app via
            # `getattr(mcp, "app", None) or getattr(mcp, "_app", None)` inside
            # a wrapped `mcp.run`, but those attributes never exist on any
            # released FastMCP version -- so `app` was always None and
            # `add_middleware` was silently never called. The Bearer-token
            # check was configured but never actually enforced.
            #
            # The fix: wrap the app-builder methods themselves so the
            # middleware is attached to whichever app instance is about to be
            # served, regardless of transport (streamable-http or sse).
            for _builder_name in ("streamable_http_app", "sse_app"):
                _orig_builder = getattr(mcp, _builder_name, None)
                if _orig_builder is None:
                    store.close()
                    raise RuntimeError("MCP SDK does not support authenticated HTTP transports")

                def _make_authenticated_builder(_orig_builder=_orig_builder):
                    def _authenticated_builder(*args, **kwargs):
                        built_app = _orig_builder(*args, **kwargs)
                        built_app.add_middleware(_BearerAuthMiddleware)
                        return built_app

                    return _authenticated_builder

                setattr(mcp, _builder_name, _make_authenticated_builder())
        except ImportError as exc:
            store.close()
            raise RuntimeError("Cannot start MCP server without authentication middleware") from exc

    # ── mTLS transport encryption ─────────────────────────────────────────
    # When mTLS is configured, override the uvicorn.Config that FastMCP
    # builds internally so it loads the server cert, requires client certs,
    # and verifies them against the CA. FastMCP's run_*_async methods build
    # the uvicorn.Config themselves without exposing ssl_* params, so we
    # wrap them to inject the SSL configuration after the Config is created.
    _mtls = _auth_mod.get_mtls_config(_loaded_config) if _loaded_config else None
    if not _mtls:
        # Also check env vars directly (for non-config setups like containers).
        _mtls = _auth_mod.get_mtls_config()
    if _mtls:
        import ssl as _ssl

        _mtls_paths = _mtls  # capture for closure
        _mtls_host, _mtls_port = host, port  # capture for closure

        def _make_ssl_runner(_app_builder_name: str):
            """Build a replacement async runner that adds mTLS to uvicorn.Config."""

            async def _ssl_async(*args, **kwargs):
                import uvicorn

                _app_builder = getattr(mcp, _app_builder_name)
                starlette_app = _app_builder()
                config = uvicorn.Config(
                    starlette_app,
                    host=_mtls_host,
                    port=_mtls_port,
                    log_level="info",
                    ssl_certfile=_mtls_paths["server_cert"],
                    ssl_keyfile=_mtls_paths["server_key"],
                    ssl_ca_certs=_mtls_paths["ca_cert"],
                    ssl_cert_reqs=_ssl.CERT_REQUIRED,
                )
                server = uvicorn.Server(config)
                await server.serve()

            return _ssl_async

        # Override both async runners so `server.run(transport=...)` picks
        # up the SSL-configured version regardless of which transport is used.
        for _runner_name, _app_builder_name in (
            ("run_streamable_http_async", "streamable_http_app"),
            ("run_sse_async", "sse_app"),
        ):
            if getattr(mcp, _runner_name, None) is not None:
                setattr(mcp, _runner_name, _make_ssl_runner(_app_builder_name))

    @mcp.tool()
    def recall(
        query: str,
        limit: int = 8,
        source: str = "",
        type: str = "",
        semantic_weight: float = -1.0,
    ) -> str:
        """Search the user's unified memory across all AIs. Use this at the start of
        a task to recover context. `source` filters to one AI (chatgpt/claude/gemini);
        `type` filters by fact/preference/project/relationship/conversation/note.
        `semantic_weight` (0.0–1.0) tunes hybrid search: 0.0 = pure keyword,
        1.0 = pure semantic, 0.5 = equal. Use -1.0 (default) for the config
        default. Results are limited to the memory scopes this client is
        allowed to see."""
        sw = None if semantic_weight < 0 else semantic_weight
        return _active_service().recall(
            query, limit=limit, source=source, type=type, semantic_weight=sw
        )

    @mcp.tool()
    def list_projects() -> str:
        """List the user's EleSync projects and their short ids. Use this before
        `recall_project` when the user refers to a project by name."""
        return _active_service().list_projects()

    @mcp.tool()
    def recall_project(project_id: str, query: str, limit: int = 8) -> str:
        """Search durable documents, chats, and memories in one EleSync project.
        `project_id` accepts the unique short id from `list_projects` or the full
        id. This is safer and more relevant than a whole-vault recall for
        project-specific work."""
        return _active_service().recall_project(project_id, query, limit=limit)

    @mcp.tool()
    def project_context(
        project_id: str,
        query: str = "",
        document_kind: str = "",
        offset: int = 0,
        limit: int = 20,
        max_chars: int = 100_000,
    ) -> str:
        """Return a bounded page of approved project documents and memories.

        `project_id` accepts the unique short id from `list_projects` or the full id.
        Use `query` to filter document title/body and memory content, and
        `document_kind` to select knowledge, task, decision, or handoff documents.
        `offset` and `limit` page both result collections. `max_chars` bounds the
        combined returned text so large projects remain safe for MCP clients.
        Pagination metadata reports totals and whether more results are available.
        """
        import json as _json

        return _json.dumps(
            _active_service().project_context(
                project_id,
                query=query,
                document_kind=document_kind,
                offset=offset,
                limit=limit,
                max_chars=max_chars,
            ),
            ensure_ascii=False,
            indent=2,
        )

    @mcp.tool()
    def save_project_document(
        project_id: str,
        title: str,
        body: str,
        kind: str = "knowledge",
    ) -> str:
        """Create a new durable document inside one EleSync project.

        This is a mutating tool: it writes the title and body to local vault storage
        and updates the project manifest. It does not send content to an AI provider,
        and it does not overwrite an existing document; each call creates a new
        document. `project_id` accepts the unique short id from `list_projects` or
        the full id, and `kind` must be one of `knowledge`, `task`, `decision`, or
        `handoff`. Call it only after the
        user has approved saving the content. A project-bound connection can use this
        only for its assigned project and only when it has project write permission.
        """
        document = _active_service().save_project_document(project_id, title, body, kind)
        return (
            f"Saved {kind} document '{document['title']}' to project {document['project_id'][:8]}."
        )

    @mcp.tool()
    def remember(
        content: str,
        type: str = "note",
        tags: str = "",
        source: str = "ai",
        scopes: str = "",
        supersedes: str = "",
        template: str = "",
        fields: str = "",
    ) -> str:
        """Save a new memory to the user's vault so every other AI can see it too.
        Use for durable facts, preferences, project state — not throwaway chatter.
        Pass your identity in `source` (e.g. 'claude', 'chatgpt', 'gemini') so
        provenance and conflict detection work correctly across AIs.
        `scopes` (comma-separated, e.g. "legal" or "writing,coding") puts the memory
        in a compartment only clients granted that scope can read; omit it to make
        the memory general (visible to all) unless this client has a default scope.
        `supersedes` (a memory id or 8-char prefix) marks that this memory replaces
        an older one when a fact *changes* rather than adds (a move, a rename, an
        updated preference): the old memory is kept as history but hidden from
        recall, and `history` can then show what changed and when.
        `template` names a structure from the `memory://templates` resource and
        applies its type/tags/scopes defaults; `fields` is a JSON object filling
        that template's placeholders, e.g. {"who": "Ana", "date": "2026-07-25"},
        which builds the content for you so repeated captures stay comparable."""
        return _active_service().remember(
            content,
            type=type,
            tags=tags,
            source=source,
            scopes=scopes,
            supersedes=supersedes,
            template=template,
            fields=fields,
        )

    @mcp.tool()
    def forget(memory_id: str) -> str:
        """Delete a memory by id (full or 8-char prefix shown by recall)."""
        return _active_service().forget(memory_id)

    @mcp.tool()
    def memory_status() -> str:
        """Summary of what's in the vault: totals by source and type (limited to the
        memories this client's scope can see)."""
        return _active_service().status()

    @mcp.tool()
    def list_scopes() -> str:
        """List the memory scope compartments this client can see, with how many
        memories each holds. `general` is the default, everyone-can-see bucket."""
        return _active_service().scope_summary()

    @mcp.tool()
    def find_conflicts() -> str:
        """Surface facts in the vault that look like they're about the same thing but
        disagree (e.g. two different stated locations), each with its source and date.
        Use this to spot stale/contradictory memory and confirm with the user."""
        return _active_service().conflicts()

    @mcp.tool()
    def history(memory_id: str) -> str:
        """Show the supersession history of a memory (id or 8-char prefix): every
        version that led to or replaced it, oldest first, with when each changed.
        This is the "what was true when" view — use it to see what a fact used to
        be after a memory was superseded (via `remember(..., supersedes=...)` or by
        resolving a conflict)."""
        return _active_service().history(memory_id)

    @mcp.tool()
    def link_memories(
        source_id: str, target_id: str, relation: str = "related", remove: bool = False
    ) -> str:
        """Record a direct, typed link from one memory to another (ids or 8-char
        prefixes), so the reasoning between memories is kept, not just the facts.
        `relation` is one of related, caused_by, depends_on, supports, contradicts
        or part_of, read as "source <relation> target" (e.g. a decision caused_by
        the constraint behind it). Set `remove` to true to delete that link. Use
        `remember(..., supersedes=...)` instead when a memory replaces another."""
        return _active_service().link_memories(source_id, target_id, relation, remove)

    @mcp.tool()
    def related_memories(memory_id: str) -> str:
        """List the memories directly linked to or from a memory (id or 8-char
        prefix), with each link's relation and direction. Use it after `recall`
        to follow why something was decided or what depends on it."""
        return _active_service().related_memories(memory_id)

    @mcp.tool()
    def audit_log(last: int = 50, operation: str = "", client: str = "") -> str:
        """View the audit trail of operations on this vault.  Shows who did what
        and when — useful for reviewing which AI clients are reading or writing
        memories.  Audit logging is enabled by default; set ELESYNC_AUDIT=0 to disable it.
        `last` limits the number of entries (newest first).
        `operation` filters to a specific op (e.g. REMEMBER, RECALL, FORGET, SCOPE,
        SUPERSEDE, LINK, UNLINK, RELATED, STATUS).
        `client` filters to a specific client label."""
        if not _active_service().audit.enabled:
            return (
                "Audit logging is not enabled. Remove ELESYNC_AUDIT=0 or set it to 1 to turn it on."
            )
        return _active_service().audit_summary(
            operation=operation or None, client=client or None, last=last
        )

    @mcp.resource("memory://recent")
    def recent() -> str:
        """The most recent memories, as a readable digest."""
        return _active_service().recent()

    # A resource, not a tool: templates are reference material the model reads
    # before calling `remember`, and adding another tool to describe them would
    # spend tool-list budget on something that never needs to *do* anything.
    @mcp.resource("memory://templates")
    def memory_templates() -> str:
        """Available capture templates, their fields, and the defaults they set."""
        return _active_service().templates()

    # Expose the store so callers that need a deterministic shutdown (tests
    # building ephemeral vaults, or any programmatic embedding) can close the
    # underlying SQLite connection explicitly rather than relying on garbage
    # collection timing. This matters cross-platform: POSIX lets you delete/
    # rmtree a file with an open handle (the dirent is unlinked immediately,
    # the inode is freed once the last fd closes), but Windows refuses with
    # PermissionError: [WinError 32] until every handle is closed -- so an
    # un-closed store silently "works" in a short-lived Linux test process
    # and reliably fails cleanup on Windows.
    mcp.elesync_store = svc.store  # type: ignore[attr-defined]
    return mcp


def main() -> None:  # pragma: no cover
    import argparse

    parser = argparse.ArgumentParser(prog="elesync", description="EleSync MCP server")
    parser.add_argument(
        "--transport",
        choices=["stdio", "http", "sse"],
        default=os.environ.get("ELESYNC_TRANSPORT", "stdio"),
        help=(
            "Transport to serve over. 'stdio' (default) is for Claude Desktop and compatible local clients. "
            "'http' (Streamable HTTP) is for remote AI services "
            "like ChatGPT's Developer Mode connector. 'sse' is legacy, kept for "
            "older remote clients."
        ),
    )
    parser.add_argument(
        "--host",
        default=os.environ.get("ELESYNC_HOST", "127.0.0.1"),
        help="Host to bind for http/sse transport (ignored for stdio). Default: 127.0.0.1",
    )
    parser.add_argument(
        "--port",
        type=int,
        default=int(os.environ.get("ELESYNC_PORT", "8000")),
        help="Port to bind for http/sse transport (ignored for stdio). Default: 8000",
    )
    parser.add_argument(
        "--allowed-host",
        action="append",
        default=None,
        help=(
            "Public hostname to trust for http/sse transport (repeatable). "
            "Required when serving through a tunnel (ngrok, Cloudflare Tunnel, "
            "a reverse proxy) -- e.g. --allowed-host xxxx.ngrok-free.dev. "
            "Keeps DNS rebinding protection on; just extends the allow-list."
        ),
    )
    args = parser.parse_args()

    allowed_hosts = args.allowed_host or (
        os.environ.get("ELESYNC_ALLOWED_HOSTS", "").split(",")
        if os.environ.get("ELESYNC_ALLOWED_HOSTS")
        else None
    )
    # Both installed commands must enforce the same public-exposure guard.
    # Keep stdio and private loopback operation available without credentials.
    from .cli import cmd_serve

    args.allowed_host = allowed_hosts
    args.vault = None
    args.insecure = False
    try:
        status = cmd_serve(args)
    except RuntimeError as exc:
        import sys

        print(f"EleSync MCP server could not start: {exc}", file=sys.stderr)
        sys.exit(1)
    if status:
        raise SystemExit(status)


if __name__ == "__main__":  # pragma: no cover
    main()
