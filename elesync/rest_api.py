"""Public REST API, version 1, authenticated with API keys.

Any app, script or integration can use EleSync over plain HTTP: search and save
memories, read projects and a project's context. It is served by the dashboard
server under ``/api/v1/`` (``web.py`` owns the transport; this module owns keys,
validation, permissions and the response shapes, so it is testable on its own).
Apps that speak MCP keep using the MCP server; this is the door for everything
else.

Keys are created and revoked in the dashboard. Only a SHA-256 hash of each key is
stored, in ``api_keys.json`` inside the vault, so a key opens exactly the vault it
was made for. A key is ``read`` (search and read) or ``write`` (also save and
delete), and can be bound to one project, in which case it sees nothing outside
that project. Every request needs a key, even when the dashboard has no password.

Responses use one envelope: ``{"ok": true, "data": ..., "meta": {...}}`` on
success and ``{"ok": false, "error": {"code": ..., "message": ...}}`` on failure.
"""

from __future__ import annotations

import hashlib
import hmac
import json
import os
import re
import secrets
import tempfile
import threading
import time
from collections import deque
from collections.abc import Callable
from datetime import datetime, timezone
from pathlib import Path

API_PREFIX = "/api/v1"
KEY_PREFIX = "esk_"
KEYS_FILE = "api_keys.json"
ACCESS_LEVELS = ("read", "write")
MAX_KEYS = 50
MAX_KEY_NAME_CHARS = 60
RATE_LIMIT_PER_MINUTE = 120
RATE_WINDOW_SECONDS = 60.0
LAST_USED_WRITE_INTERVAL_SECONDS = 60.0
MAX_BODY_BYTES = 1 << 20  # 1 MiB: room for MAX_CONTENT_CHARS of any UTF-8 text
MAX_CONTENT_CHARS = 100_000
MAX_QUERY_CHARS = 500
MAX_TAGS = 20
MAX_TAG_CHARS = 64
DEFAULT_LIMIT = 20
MAX_LIMIT = 100
MAX_OFFSET = 100_000

_ID_RE = re.compile(r"^[A-Za-z0-9_-]{1,64}$")
_SOURCE_RE = re.compile(r"^[a-z0-9][a-z0-9 ._-]{0,39}$")
_TYPE_RE = re.compile(r"^[a-z][a-z0-9_-]{0,31}$")
_KEY_RE = re.compile(r"^esk_[A-Za-z0-9_-]{20,128}$")


class ApiError(Exception):
    """A request the API refuses, with the HTTP status and a stable error code."""

    def __init__(self, status: int, code: str, message: str):
        super().__init__(message)
        self.status = status
        self.code = code
        self.message = message


def ok(data, status: int = 200, meta: dict | None = None) -> tuple[int, dict]:
    body: dict = {"ok": True, "data": data}
    if meta is not None:
        body["meta"] = meta
    return status, body


def error_body(code: str, message: str) -> dict:
    return {"ok": False, "error": {"code": code, "message": message}}


def bearer_token(header: str | None) -> str:
    """The key from an ``Authorization: Bearer esk_...`` header, or ''."""
    scheme, _, value = (header or "").strip().partition(" ")
    if scheme.lower() != "bearer":
        return ""
    value = value.strip()
    return value if _KEY_RE.match(value) else ""


def _now() -> str:
    return datetime.now(timezone.utc).isoformat(timespec="seconds")


def _hash(secret: str) -> str:
    return hashlib.sha256(secret.encode("utf-8")).hexdigest()


# ── Keys ─────────────────────────────────────────────────────────────────────


class ApiKeyStore:
    """The vault's API keys. Secrets are shown once, at creation; only hashes persist."""

    _locks: dict[str, threading.Lock] = {}
    _locks_guard = threading.Lock()

    def __init__(self, vault_dir: str | Path):
        self.path = Path(vault_dir).expanduser() / KEYS_FILE
        with self._locks_guard:
            self._lock = self._locks.setdefault(str(self.path), threading.Lock())

    def _load(self) -> list[dict]:
        try:
            data = json.loads(self.path.read_text(encoding="utf-8"))
        except FileNotFoundError:
            return []
        except (OSError, json.JSONDecodeError) as exc:
            # Never treat an unreadable key file as "no keys" and overwrite it.
            raise OSError(f"API key file {self.path.name} could not be read") from exc
        keys = data.get("keys") if isinstance(data, dict) else None
        return [k for k in keys if isinstance(k, dict)] if isinstance(keys, list) else []

    def _save(self, keys: list[dict]) -> None:
        self.path.parent.mkdir(parents=True, exist_ok=True)
        fd, tmp = tempfile.mkstemp(dir=str(self.path.parent), prefix=".api-keys-", suffix=".tmp")
        try:
            with os.fdopen(fd, "w", encoding="utf-8") as handle:
                json.dump({"version": 1, "keys": keys}, handle, indent=2)
            os.replace(tmp, self.path)
        except BaseException:
            Path(tmp).unlink(missing_ok=True)
            raise

    @staticmethod
    def public(key: dict) -> dict:
        """What the dashboard may show about a key: never its hash or secret."""
        return {
            "id": key.get("id", ""),
            "name": key.get("name", ""),
            "access": key.get("access", "read"),
            "project_id": key.get("project_id") or None,
            "preview": key.get("preview", ""),
            "created_at": key.get("created_at", ""),
            "last_used_at": key.get("last_used_at") or None,
        }

    def list(self) -> list[dict]:
        with self._lock:
            return [self.public(k) for k in self._load()]

    def create(self, name: str, access: str, project_id: str | None = None) -> tuple[dict, str]:
        name = " ".join(str(name or "").split())
        if not name:
            raise ValueError("give the key a name, for example the app that will use it")
        if len(name) > MAX_KEY_NAME_CHARS:
            raise ValueError(f"key name must be at most {MAX_KEY_NAME_CHARS} characters")
        if access not in ACCESS_LEVELS:
            raise ValueError("access must be 'read' or 'write'")
        project_id = (project_id or "").strip() or None
        if project_id is not None and not _ID_RE.match(project_id):
            raise ValueError("invalid project id")
        secret = KEY_PREFIX + secrets.token_urlsafe(32)
        key = {
            "id": secrets.token_hex(6),
            "name": name,
            "access": access,
            "project_id": project_id,
            "hash": _hash(secret),
            "preview": secret[:8] + "…",
            "created_at": _now(),
            "last_used_at": None,
        }
        with self._lock:
            keys = self._load()
            if len(keys) >= MAX_KEYS:
                raise ValueError(f"at most {MAX_KEYS} API keys per vault; revoke one first")
            keys.append(key)
            self._save(keys)
        return self.public(key), secret

    def revoke(self, key_id: str) -> bool:
        with self._lock:
            keys = self._load()
            kept = [k for k in keys if k.get("id") != key_id]
            if len(kept) == len(keys):
                return False
            self._save(kept)
            return True

    def verify(self, secret: str) -> dict | None:
        """The key record for a presented secret, or None. Compares in constant time."""
        if not secret or not _KEY_RE.match(secret):
            return None
        digest = _hash(secret)
        with self._lock:
            try:
                keys = self._load()
            except OSError:
                return None
            match = None
            for key in keys:
                if hmac.compare_digest(str(key.get("hash", "")), digest):
                    match = key
            if match is None:
                return None
            # Record use, at most once a minute per key, so reads don't rewrite the file.
            last = match.get("last_used_at")
            stale = True
            if last:
                try:
                    age = datetime.now(timezone.utc) - datetime.fromisoformat(last)
                    stale = age.total_seconds() >= LAST_USED_WRITE_INTERVAL_SECONDS
                except ValueError:
                    stale = True
            if stale:
                match["last_used_at"] = _now()
                try:
                    self._save(keys)
                except OSError:
                    pass  # A failed bookkeeping write never blocks a valid key.
            return dict(match)


class RateLimiter:
    """Sliding-window limit per key: RATE_LIMIT_PER_MINUTE requests per minute."""

    def __init__(self, limit: int = RATE_LIMIT_PER_MINUTE, window: float = RATE_WINDOW_SECONDS):
        self.limit = limit
        self.window = window
        self._hits: dict[str, deque[float]] = {}
        self._lock = threading.Lock()

    def retry_after(self, key_id: str, now: float | None = None) -> float:
        """0 when the request may proceed (and is counted), else seconds to wait."""
        now = time.monotonic() if now is None else now
        with self._lock:
            hits = self._hits.setdefault(key_id, deque())
            while hits and now - hits[0] >= self.window:
                hits.popleft()
            if len(hits) >= self.limit:
                return max(0.0, self.window - (now - hits[0]))
            hits.append(now)
            return 0.0


RATE_LIMITER = RateLimiter()


# ── Requests ─────────────────────────────────────────────────────────────────


def _param(query: dict, name: str) -> str:
    values = query.get(name) or [""]
    return str(values[0])


def _int_param(query: dict, name: str, default: int, low: int, high: int) -> int:
    raw = _param(query, name).strip()
    if not raw:
        return default
    try:
        value = int(raw)
    except ValueError as exc:
        raise ApiError(400, "invalid_parameter", f"{name} must be a whole number") from exc
    if not low <= value <= high:
        raise ApiError(400, "invalid_parameter", f"{name} must be between {low} and {high}")
    return value


def _check_id(value: str, what: str) -> str:
    if not _ID_RE.match(value):
        raise ApiError(404, "not_found", f"{what} not found")
    return value


def memory_dict(item) -> dict:
    return {
        "id": item.id,
        "content": item.content,
        "source": item.source,
        "type": item.type,
        "tags": list(item.tags or []),
        "created_at": item.created_at,
    }


class RestApi:
    """Routes one authenticated ``/api/v1`` request to the vault."""

    def __init__(self, store, project_service, audit=None):
        self.store = store
        self.projects = project_service
        self.audit = audit

    def handle(
        self, method: str, path: str, query: dict, read_body: Callable[[], object], key: dict
    ) -> tuple[int, dict]:
        try:
            return self._route(method, path, query, read_body, key)
        except ApiError as exc:
            return exc.status, error_body(exc.code, exc.message)

    def _route(self, method, path, query, read_body, key) -> tuple[int, dict]:
        rest = path[len(API_PREFIX) :].strip("/")
        parts = rest.split("/") if rest else []
        if not parts:
            return self._expect(method, "GET") or self._info(key)
        if parts[0] == "memories":
            if len(parts) == 1:
                if method == "GET":
                    return self._list_memories(query, key)
                if method == "POST":
                    return self._save_memory(read_body, key)
                raise ApiError(405, "method_not_allowed", "use GET or POST")
            if len(parts) == 2:
                memory_id = _check_id(parts[1], "memory")
                if method == "GET":
                    return self._get_memory(memory_id, key)
                if method == "DELETE":
                    return self._delete_memory(memory_id, key)
                raise ApiError(405, "method_not_allowed", "use GET or DELETE")
        if parts[0] == "projects":
            self._expect(method, "GET")
            if len(parts) == 1:
                return self._list_projects(key)
            project_id = _check_id(parts[1], "project")
            if len(parts) == 2:
                return self._get_project(project_id, key)
            if len(parts) == 3 and parts[2] == "context":
                return self._project_context(project_id, key)
        raise ApiError(404, "not_found", "no such endpoint; see docs/API.md")

    @staticmethod
    def _expect(method: str, allowed: str) -> None:
        if method != allowed:
            raise ApiError(405, "method_not_allowed", f"use {allowed}")

    def _log(self, operation: str, key: dict, memory_id: str = "", detail: str = "") -> None:
        if self.audit is not None:
            self.audit.log(
                operation, client=f"api:{key.get('name', '')}", memory_id=memory_id, detail=detail
            )

    @staticmethod
    def _require_write(key: dict) -> None:
        if key.get("access") != "write":
            raise ApiError(403, "read_only_key", "this key is read-only")

    def _visible_project(self, project_id: str, key: dict) -> None:
        """404 for a project the key may not see, exactly as for one that doesn't exist."""
        bound = key.get("project_id")
        if (bound and project_id != bound) or self.projects.get(project_id) is None:
            raise ApiError(404, "not_found", "project not found")

    def _memory_in_scope(self, memory_id: str, key: dict):
        item = self.store.get(memory_id)
        bound = key.get("project_id")
        if item is not None and bound:
            if bound not in {p["id"] for p in self.store.memory_projects(memory_id)}:
                item = None
        if item is None:
            raise ApiError(404, "not_found", "memory not found")
        return item

    # ── endpoints ──

    def _info(self, key: dict) -> tuple[int, dict]:
        from . import __version__

        return ok(
            {
                "api": "EleSync REST API",
                "api_version": "1",
                "app_version": __version__,
                "key": {
                    "name": key.get("name", ""),
                    "access": key.get("access", "read"),
                    "project_id": key.get("project_id") or None,
                },
            }
        )

    def _list_memories(self, query: dict, key: dict) -> tuple[int, dict]:
        q = _param(query, "q").strip()
        if len(q) > MAX_QUERY_CHARS:
            raise ApiError(
                400, "invalid_parameter", f"q must be at most {MAX_QUERY_CHARS} characters"
            )
        limit = _int_param(query, "limit", DEFAULT_LIMIT, 1, MAX_LIMIT)
        offset = _int_param(query, "offset", 0, 0, MAX_OFFSET)
        bound = key.get("project_id")
        if bound:
            if q:
                items = self.store.search_project(bound, q, limit=limit + offset)[offset:]
                total = None
            else:
                items = self.store.project_memories(bound)
                total = len(items)
                items = items[offset : offset + limit]
        elif q:
            items = self.store.search(q, limit=limit + offset)[offset:]
            total = None
        else:
            items, total = self.store.page(limit=limit, offset=offset)
        self._log("RECALL", key, detail=f"api q={q[:60]!r} n={len(items)}" if q else "api list")
        meta = {"limit": limit, "offset": offset, "count": len(items)}
        if total is not None:
            meta["total"] = total
        return ok([memory_dict(i) for i in items], meta=meta)

    def _get_memory(self, memory_id: str, key: dict) -> tuple[int, dict]:
        item = self._memory_in_scope(memory_id, key)
        data = memory_dict(item)
        projects = self.store.memory_projects(item.id)
        bound = key.get("project_id")
        data["projects"] = [
            {"id": p["id"], "name": p["name"]} for p in projects if not bound or p["id"] == bound
        ]
        return ok(data)

    def _save_memory(self, read_body: Callable[[], object], key: dict) -> tuple[int, dict]:
        self._require_write(key)
        try:
            body = read_body()
        except ValueError as exc:
            raise ApiError(400, "invalid_json", "the request body must be JSON") from exc
        if not isinstance(body, dict):
            raise ApiError(400, "invalid_json", "the request body must be a JSON object")
        content = body.get("content")
        if not isinstance(content, str) or not content.strip():
            raise ApiError(400, "invalid_field", "content is required")
        if len(content) > MAX_CONTENT_CHARS:
            raise ApiError(
                400, "invalid_field", f"content must be at most {MAX_CONTENT_CHARS} characters"
            )
        tags = body.get("tags", [])
        if (
            not isinstance(tags, list)
            or len(tags) > MAX_TAGS
            or not all(isinstance(t, str) and 0 < len(t.strip()) <= MAX_TAG_CHARS for t in tags)
        ):
            raise ApiError(
                400, "invalid_field", f"tags must be a list of at most {MAX_TAGS} short strings"
            )
        source = str(body.get("source") or "api").strip().lower()
        if not _SOURCE_RE.match(source):
            raise ApiError(400, "invalid_field", "source must be a short lowercase name")
        mtype = str(body.get("type") or "note").strip().lower()
        if not _TYPE_RE.match(mtype):
            raise ApiError(400, "invalid_field", "type must be a short lowercase word")
        bound = key.get("project_id")
        project_id = str(body.get("project_id") or "").strip() or bound
        if project_id:
            _check_id(project_id, "project")
            self._visible_project(project_id, key)

        from .models import MemoryItem

        item = MemoryItem(content=content.strip(), source=source, type=mtype, tags=tags)
        stored_id, inserted = self.store.upsert_status(item)
        if bound and not inserted:
            in_scope = bound in {p["id"] for p in self.store.memory_projects(stored_id)}
            if not in_scope:
                # Identical text already exists outside this key's project. Linking it in
                # would let a project key pull in (then read or delete) memories it was
                # never given, so refuse without revealing which memory it is, as MCP does.
                self._log("REMEMBER", key, detail="api duplicate outside project, refused")
                raise ApiError(409, "already_exists", "this memory already exists in your vault")
        if project_id:
            self.projects.add_memory(project_id, stored_id)
        stored = self.store.get(stored_id)
        if stored is None:
            raise ApiError(500, "not_saved", "the memory could not be saved")
        self._log(
            "REMEMBER", key, memory_id=stored_id, detail="api" + ("" if inserted else " duplicate")
        )
        return ok(memory_dict(stored), status=201 if inserted else 200, meta={"created": inserted})

    def _delete_memory(self, memory_id: str, key: dict) -> tuple[int, dict]:
        self._require_write(key)
        self._memory_in_scope(memory_id, key)
        if not self.store.delete(memory_id):
            raise ApiError(404, "not_found", "memory not found")
        self._log("FORGET", key, memory_id=memory_id, detail="api")
        return ok({"id": memory_id, "deleted": True})

    def _list_projects(self, key: dict) -> tuple[int, dict]:
        bound = key.get("project_id")
        rows = self.projects.list_projects()
        if bound:
            rows = [p for p in rows if p.get("id") == bound]
        data = [
            {
                "id": p.get("id"),
                "name": p.get("name"),
                "description": p.get("description", ""),
                "status": p.get("status", "active"),
                "memory_count": p.get("memory_count", 0),
            }
            for p in rows
        ]
        return ok(data, meta={"count": len(data)})

    def _get_project(self, project_id: str, key: dict) -> tuple[int, dict]:
        self._visible_project(project_id, key)
        project = self.projects.get(project_id)
        return ok(
            {
                "id": project.get("id"),
                "name": project.get("name"),
                "description": project.get("description", ""),
                "status": project.get("status", "active"),
                "memories": [memory_dict(i) for i in project.get("memories", [])][:MAX_LIMIT],
            }
        )

    def _project_context(self, project_id: str, key: dict) -> tuple[int, dict]:
        self._visible_project(project_id, key)
        self._log("RECALL", key, detail=f"api project={project_id[:8]} context")
        return ok(self.projects.context(project_id))
