"""

elesync.web — local web UI for the vault.



Usage:

    ele web                    # opens browser at http://127.0.0.1:4settle7

    ele web --port 8080        # custom port

    ele web --vault ~/MyVault  # custom vault path

    ele web --no-browser       # don't auto-open browser



The web server keeps its local-first behavior and uses the small qrcode
package to render private pairing codes locally.

"""

from __future__ import annotations

import base64
import contextlib
import gzip
import hashlib
import ipaddress
import json
import math
import os
import re
import secrets
import socket
import sys
import threading
import time
import webbrowser
from email.parser import BytesParser
from email.policy import default as email_default
from functools import wraps
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
from typing import TYPE_CHECKING
from urllib.parse import parse_qs, quote, unquote, urlencode, urlparse

from .config import load_config as _load_config
from .config import save_config as _save_config

if TYPE_CHECKING:
    from .store import MemoryStore
    from .web_auth import SessionManager, UserManager


class _ServerState:
    """Server-wide runtime state, guarded by a single RLock.

    The web server is a ThreadingHTTPServer: request threads read the store
    while a vault switch rebinds it. Holding every live ref on one instance
    behind one lock keeps that swap atomic — no request thread can ever
    observe a closed or half-open store.

    `store`, `vault_dir`: current vault handle + path.

    `user_mgr`, `session_mgr`: opt-in — stay None (server behaves as before
    web_auth existed) unless the vault has a .web_auth/web_users.json with
    at least one user. `ele web adduser` is what turns auth on — there's no
    separate --require-auth flag to remember to pass.
    """

    def __init__(self) -> None:
        self.store: MemoryStore | None = None
        self.vault_dir: str = ""
        self.user_mgr: UserManager | None = None
        self.session_mgr: SessionManager | None = None
        self.tls_enabled = False
        self.share_targets: dict[str, tuple[float, dict]] = {}
        self.whatsapp_previews: dict[str, tuple[float, dict]] = {}
        self.store_leases: dict[MemoryStore, int] = {}
        self.retired_stores: set[MemoryStore] = set()
        self.lock = threading.RLock()
        self.port = 8917
        self.demo_mode = False
        from .tunnel import TunnelManager

        self.tunnel_manager = TunnelManager()


_state = _ServerState()
_request_store = threading.local()

SESSION_COOKIE_NAME = "ele_session"
CSRF_FORM_FIELD = "csrf_token"
MAX_REQUEST_BODY_BYTES = 4 << 20  # 4 MiB
MAX_HTTP_WORKERS = 32
HEADER_TIMEOUT_SECONDS = 15.0
BODY_TIMEOUT_SECONDS = 60.0
RESPONSE_IO_TIMEOUT_SECONDS = 30.0

# Process-local HMAC key used to derive per-session CSRF tokens. Generated
# once at import; a restart mints a new key and invalidates any in-flight
# consent form, which is the intended behavior — the session cookie is
# also invalidated on restart.
_CSRF_KEY: bytes = secrets.token_bytes(32)


def _csrf_token_for(session_token: str) -> str:
    """Derive a per-session CSRF token from the session cookie.

    The dashboard has no server-side CSRF store; deriving the token from the
    session cookie means a cross-origin form doesn't know the value even
    though the browser will still send the session cookie itself. Uses a
    process-local HMAC key so the derivation is opaque to an attacker who
    only sees the CSRF token."""
    import hashlib
    import hmac as _hmac

    return _hmac.new(_CSRF_KEY, session_token.encode("utf-8"), hashlib.sha256).hexdigest()


def _verify_csrf(session_token: str, submitted: str) -> bool:
    import hmac as _hmac

    if not (session_token and submitted):
        return False
    return _hmac.compare_digest(_csrf_token_for(session_token), submitted)


# Imported archives are user-selected local files. A WhatsApp transcript can
# legitimately exceed the small limit appropriate for settings and API forms.
MAX_IMPORT_REQUEST_BODY_BYTES = 32 << 20  # 32 MiB

# Bound on the request body we'll read-and-discard when rejecting a request
# (see _drain_request_body): a huge Content-Length can't be allowed to stall
# the response, so we drain at most this many bytes, in chunks this size.
MAX_DRAIN_BYTES = 1 << 20  # 1 MiB
DRAIN_CHUNK_BYTES = 1 << 16  # 64 KiB per read
# Total time a drain may take: a client that announced more body than it
# sends must not hold a rejected request, or the handler thread, open.
DRAIN_TIMEOUT_SECONDS = 2.0


class RequestTooLarge(ValueError):
    """An HTTP request body exceeded the dashboard's configured hard limit."""


class RequestTimeout(ValueError):
    """An accepted body did not arrive before its total read deadline."""


class BoundedHTTPServer(ThreadingHTTPServer):
    """Bound active connections without blocking the accept loop on a slot."""

    def __init__(self, *args, **kwargs):
        self._worker_slots = threading.BoundedSemaphore(MAX_HTTP_WORKERS)
        super().__init__(*args, **kwargs)

    def process_request(self, request, client_address):
        if not self._worker_slots.acquire(blocking=False):
            self.shutdown_request(request)
            return
        try:
            request.settimeout(HEADER_TIMEOUT_SECONDS)
            super().process_request(request, client_address)
        except BaseException:
            self._worker_slots.release()
            raise

    def process_request_thread(self, request, client_address):
        try:
            super().process_request_thread(request, client_address)
        finally:
            self._worker_slots.release()


SHARE_TARGET_TTL_SECONDS = 5 * 60
WHATSAPP_PREVIEW_TTL_SECONDS = 10 * 60
WHATSAPP_PREVIEW_PAGE_SIZE = 200
WHATSAPP_PREVIEW_CACHE_LIMIT = 3


def _parse_share_target(content_type: str, body: bytes) -> dict:
    if not content_type.lower().startswith("multipart/form-data"):
        raise ValueError("share target must use multipart/form-data")
    message = BytesParser(policy=email_default).parsebytes(
        b"MIME-Version: 1.0\r\nContent-Type: "
        + content_type.encode("ascii", "strict")
        + b"\r\n\r\n"
        + body
    )
    if not message.is_multipart():
        raise ValueError("invalid share target payload")
    files: list[dict] = []
    payload: dict[str, object] = {"title": "", "text": "", "url": "", "files": files}
    for part in message.iter_parts():
        name = part.get_param("name", header="content-disposition")
        if name in {"title", "text", "url"}:
            payload[name] = part.get_content()
        elif name == "files":
            filename = part.get_filename()
            data = part.get_payload(decode=True) or b""
            if filename and data:
                files.append(
                    {
                        "name": Path(filename).name,
                        "type": part.get_content_type(),
                        "data": data,
                    }
                )
    if not payload["files"] and not any(payload[key] for key in ("title", "text", "url")):
        raise ValueError("share target contained no text or files")
    return payload


def _save_share_target(payload: dict) -> str:
    token = secrets.token_urlsafe(24)
    now = time.monotonic()
    with _state.lock:
        _state.share_targets = {
            key: value
            for key, value in _state.share_targets.items()
            if now - value[0] < SHARE_TARGET_TTL_SECONDS
        }
        _state.share_targets[token] = (now, payload)
    return token


def _take_share_target(token: str) -> dict:
    with _state.lock:
        entry = _state.share_targets.pop(token, None)
    if entry is None or time.monotonic() - entry[0] >= SHARE_TARGET_TTL_SECONDS:
        raise ValueError("share target expired or not found")
    return entry[1]


def _save_whatsapp_preview(payload: dict) -> str:
    token = secrets.token_urlsafe(24)
    now = time.monotonic()
    with _state.lock:
        active = [
            (key, value)
            for key, value in _state.whatsapp_previews.items()
            if now - value[0] < WHATSAPP_PREVIEW_TTL_SECONDS
        ]
        active.sort(key=lambda item: item[1][0], reverse=True)
        _state.whatsapp_previews = dict(active[: WHATSAPP_PREVIEW_CACHE_LIMIT - 1])
        _state.whatsapp_previews[token] = (now, payload)
    return token


def _get_whatsapp_preview(token: str) -> dict:
    with _state.lock:
        entry = _state.whatsapp_previews.get(token)
    if entry is None or time.monotonic() - entry[0] >= WHATSAPP_PREVIEW_TTL_SECONDS:
        raise ValueError("WhatsApp preview expired; choose the file again")
    return entry[1]


def _discard_whatsapp_preview(token: str) -> None:
    if not token:
        return
    with _state.lock:
        _state.whatsapp_previews.pop(token, None)


def web_auth_dir(vault_dir: str | Path) -> Path:
    """Where a vault's web-auth user/session files live. Inside the vault
    directory (not a global ~/.elesync location) so auth config travels
    with the vault - matters for USB vaults, multiple named vaults, etc."""
    return Path(vault_dir).expanduser() / ".web_auth"


def _host_name(value: str) -> str:
    """The lower-case hostname in a Host header or URL netloc, without its port."""
    value = value.strip().lower()
    if value.startswith("["):  # [::1]:7477
        return value[1:].split("]", 1)[0]
    if value.count(":") == 1:  # host:port
        return value.split(":", 1)[0]
    return value


def _host_allowed(host_header: str | None) -> bool:
    """Whether the dashboard should answer a request addressed to this host.

    A DNS-rebinding page reaches 127.0.0.1 under its own domain name, and the
    Host header is what gives it away. IP addresses, localhost names and this
    computer's own name can't be pointed here by an attacker; any other name
    must be the active phone link or listed in ELESYNC_ALLOWED_HOSTS."""
    if not host_header:
        return True
    host = _host_name(host_header)
    try:
        ipaddress.ip_address(host)
        return True
    except ValueError:
        pass
    if host == "localhost" or host.endswith(".localhost"):
        return True
    machine = socket.gethostname().lower()
    if host in (machine, f"{machine}.local"):
        return True
    tunnel_url = _state.tunnel_manager.status().get("url")
    if tunnel_url and host == _host_name(urlparse(tunnel_url).netloc):
        return True
    listed = os.environ.get("ELESYNC_ALLOWED_HOSTS", "")
    return host in {_host_name(name) for name in listed.split(",") if name.strip()}


class _SignInThrottle:
    """Slows password guessing once the phone link puts the dashboard online.

    After five failures for a username, sign-in is refused for 30 seconds,
    doubling with each further failure up to 15 minutes; a success clears it.
    Keyed by username, because behind the phone link every request arrives from
    the tunnel's loopback address."""

    FREE_ATTEMPTS = 5
    BASE_DELAY = 30.0
    MAX_DELAY = 15 * 60.0
    MAX_TRACKED = 1000

    def __init__(self) -> None:
        self._failures: dict[str, tuple[int, float]] = {}
        self._lock = threading.Lock()

    def wait_seconds(self, username: str) -> float:
        with self._lock:
            count, last = self._failures.get(username.lower(), (0, 0.0))
        if count < self.FREE_ATTEMPTS:
            return 0.0
        delay = min(self.MAX_DELAY, self.BASE_DELAY * 2 ** (count - self.FREE_ATTEMPTS))
        return max(0.0, last + delay - time.monotonic())

    def failed(self, username: str) -> None:
        with self._lock:
            count, _ = self._failures.pop(username.lower(), (0, 0.0))
            self._failures[username.lower()] = (count + 1, time.monotonic())
            while len(self._failures) > self.MAX_TRACKED:
                self._failures.pop(next(iter(self._failures)))

    def succeeded(self, username: str) -> None:
        with self._lock:
            self._failures.pop(username.lower(), None)


_sign_in_throttle = _SignInThrottle()


def _compressible(content_type: str) -> bool:
    """Text worth gzipping; the images are compressed formats already."""
    return content_type.startswith("text/") or "json" in content_type or "svg" in content_type


def _accepts_gzip(header: str | None) -> bool:
    for part in (header or "").split(","):
        name, _, params = part.partition(";")
        if name.strip().lower() in ("gzip", "*"):
            quality = params.strip().lower()
            if not quality.startswith("q="):
                return True
            try:
                return float(quality[2:]) > 0
            except ValueError:
                return False
    return False


def _etags(header: str | None) -> set[str]:
    """Entity tags listed in an If-None-Match header, compared weakly."""
    return {tag.strip().removeprefix("W/") for tag in (header or "").split(",") if tag.strip()}


# Static files change only on upgrade, so each version is compressed once.
_GZIPPED: dict[str, bytes] = {}


def _gzip_once(etag: str, data: bytes) -> bytes:
    body = _GZIPPED.get(etag)
    if body is None:
        if len(_GZIPPED) > 64:
            _GZIPPED.clear()
        body = _GZIPPED[etag] = gzip.compress(data, compresslevel=6, mtime=0)
    return body


def _lookup_bundled_file(base_dir: Path, requested: str) -> Path | None:
    """Return the file named ``requested`` sitting directly inside ``base_dir``.

    Traversal defence by construction, used for both ``/assets/`` and
    ``/static/``. Two properties matter:

    1. ``requested`` must be a single plain filename -- no separators, no "..",
       no leading dot. Anything else is rejected before it touches the
       filesystem, so ``/assets/../../../../etc/passwd`` never resolves.
    2. The path we hand back is taken from ``base_dir.iterdir()``, i.e. it is
       derived from the *filesystem*, never built by joining request text. A
       symlink pointing outside ``base_dir`` is also rejected, since the
       resolved parent must still be ``base_dir``.

    Both bundled directories are flat (three images in ``assets/``, four
    JS/CSS files in ``elesync/static/``), so requiring a bare filename costs
    nothing and removes the whole bug class rather than filtering for known-bad
    input.
    """
    if not requested or "/" in requested or "\\" in requested:
        return None
    if requested.startswith(".") or requested in {".", ".."}:
        return None
    try:
        base = base_dir.resolve()
        for entry in base.iterdir():
            if entry.name != requested:
                continue
            resolved = entry.resolve()
            if resolved.parent == base and resolved.is_file():
                return resolved
    except OSError:
        return None
    return None


def _auth_enabled() -> bool:
    return _state.user_mgr is not None and len(_state.user_mgr.list_users()) > 0


def _get_store() -> MemoryStore:
    leased = getattr(_request_store, "store", None)
    if leased is not None:
        return leased
    with _state.lock:
        assert _state.store is not None, "store not initialised — call serve() first"
        return _state.store


def _lease_store() -> MemoryStore:
    with _state.lock:
        assert _state.store is not None, "store not initialised — call serve() first"
        store = _state.store
        _state.store_leases[store] = _state.store_leases.get(store, 0) + 1
        return store


def _release_store(store: MemoryStore) -> None:
    close_store = False
    with _state.lock:
        remaining = _state.store_leases.get(store, 0) - 1
        if remaining > 0:
            _state.store_leases[store] = remaining
        else:
            _state.store_leases.pop(store, None)
            if store in _state.retired_stores:
                _state.retired_stores.remove(store)
                close_store = True
    if close_store:
        store.close()


def _retire_store(store: MemoryStore) -> None:
    close_store = False
    with _state.lock:
        if _state.store_leases.get(store, 0):
            _state.retired_stores.add(store)
        else:
            close_store = True
    if close_store:
        store.close()


def _with_store_lease(method):
    """Pin one vault store for the full lifetime of an HTTP request."""

    @wraps(method)
    def wrapped(self, *args, **kwargs):
        store = _lease_store()
        previous = getattr(_request_store, "store", None)
        _request_store.store = store
        try:
            return method(self, *args, **kwargs)
        finally:
            if previous is None:
                del _request_store.store
            else:
                _request_store.store = previous
            _release_store(store)

    return wrapped


def _project_service():
    from .project_service import ProjectService

    return ProjectService(_get_store())


def _project_api():
    from .project_api import ProjectApi

    # Project screens list memories; the inspector fetches a memory in full when
    # opened. Full text here made one 13-memory project a 371 KB response.
    return ProjectApi(_project_service(), _item_to_list_dict)


# ---------------------------------------------------------------------------

# REST API handlers

# ---------------------------------------------------------------------------


def _last_backup_at(vault_path: Path) -> str | None:
    """When this vault was last backed up: the newest backup still on disk in the
    managed directory, or a copy saved from the Vault page if that is later."""
    from datetime import datetime, timezone

    from . import backups

    config = _load_config()
    newest: str | None = None
    try:
        directory = backups.resolve_dir(vault_path, config)
        for entry in backups.entries(directory):
            if entry.get("status") != backups.MISSING and entry.get("created_at"):
                newest = str(entry["created_at"])
                break
    except OSError:
        pass
    backup_config = config.get("backup")
    copies = backup_config.get("copies") if isinstance(backup_config, dict) else None
    copied = copies.get(_vault_key(vault_path)) if isinstance(copies, dict) else None
    if not copied:
        return newest

    def utc(stamp: str) -> datetime:
        try:
            parsed = datetime.fromisoformat(str(stamp).replace("Z", "+00:00"))
        except ValueError:
            return datetime.min.replace(tzinfo=timezone.utc)
        return parsed if parsed.tzinfo else parsed.replace(tzinfo=timezone.utc)

    return str(copied) if newest is None or utc(copied) > utc(newest) else newest


def _vault_key(vault_path) -> str:
    return str(Path(vault_path).expanduser().resolve())


def _record_vault_copy(vault_path) -> None:
    """Note that this vault was just copied from the Vault page ("Copy & encrypt
    here"), so the password-protected copy counts as a backup on the dashboard.

    Kept per vault in the app config. Best effort: the copy itself succeeded, and
    failing to note it must not turn that into an error."""
    from datetime import datetime, timezone

    config = _load_config()
    backup = config.setdefault("backup", {})
    if not isinstance(backup, dict):
        return
    copies = backup.setdefault("copies", {})
    if not isinstance(copies, dict):
        return
    copies[_vault_key(vault_path)] = datetime.now(timezone.utc).isoformat(timespec="seconds")
    _save_config(config)


def _api_backup_now() -> dict:
    """The dashboard's "Back up now": the backup `ele backup` writes, in the same
    managed directory, recorded and pruned the same way."""
    from . import backups
    from .commands.backup import _export_payload, save_managed_backup

    config = _load_config()
    with _state.lock:
        store = _get_store()
        if store.encrypted:
            # An unencrypted archive of an encrypted vault would put its contents
            # on disk in the clear.
            raise ValueError(
                "This vault is encrypted, so its backup must be too: "
                "use Vault → Copy & encrypt here."
            )
        with store._lock:
            payload = _export_payload(
                store.all(include_superseded=True), store.vault_dir, store.project_snapshot()
            )
        directory = backups.resolve_dir(store.vault_dir, config)
    try:
        return save_managed_backup(payload, directory, backups.resolve_keep(config))
    except OSError as error:
        raise ValueError(
            f"The backup could not be saved in {directory}. Check that the folder is "
            "writable and the drive has free space."
        ) from error


def _semantic_available() -> bool:
    """Whether semantic search can run here at all. The installed app is built
    without fastembed, so its dashboard must not suggest turning it on."""
    import importlib.util

    return importlib.util.find_spec("fastembed") is not None


def _api_stats() -> dict:
    from . import crypto as _crypto

    store = _get_store()
    base = store.stats()
    # Conflict count
    from . import service as _svc_mod

    svc = _svc_mod.MemoryService(store)
    try:
        clusters = svc._get_clusters()
        base["conflicts"] = len(clusters) if clusters else 0
    except Exception as e:
        # Never fail the whole stats endpoint on a conflict-scan hiccup, but
        # surface the reason so the miscount isn't invisible.
        print(f"stats: conflict count failed: {e!r}", file=sys.stderr)
        base["conflicts"] = 0
    # Scope counts
    base["scope_counts"] = store.scope_counts()
    base["projects"] = len(store.projects())
    # Encryption status
    vault_path = Path(base.get("vault_dir", _state.vault_dir))
    base["encrypted"] = _crypto.is_encrypted(vault_path) if _crypto.available() else False
    base["last_backup_at"] = _last_backup_at(vault_path)
    base["semantic_available"] = _semantic_available()
    # Auth status
    base["auth_enabled"] = _auth_enabled()
    if _auth_enabled() and _state.user_mgr:
        base["auth_users"] = len(_state.user_mgr.list_users())
    return base


def _api_memories(params: dict) -> dict:

    q = params.get("q", [""])[0]

    source = params.get("source", [""])[0] or None

    mtype = params.get("type", [""])[0] or None
    exclude_type = params.get("exclude_type", [""])[0] or None
    created_after = params.get("since", [""])[0] or None
    summary = params.get("summary", [""])[0] == "1"

    try:
        limit = int(params.get("limit", ["50"])[0])
        offset = int(params.get("offset", ["0"])[0])
    except ValueError as exc:
        raise ValueError("limit and offset must be integers") from exc

    if q:
        sw_raw = params.get("semantic_weight", [""])[0]
        sw = None
        if sw_raw:
            try:
                sw = float(sw_raw)
            except ValueError:
                pass
        items = _get_store().search(
            q,
            limit=limit + offset,
            source=source,
            mtype=mtype,
            exclude_type=exclude_type,
            semantic_weight=sw,
            created_after=created_after,
        )

        items = items[offset:]

    else:
        # Filtering, counting and slicing all happen in SQL — a browse request
        # reads `limit` rows regardless of vault size.
        items, total = _get_store().page(
            limit=limit,
            offset=offset,
            source=source,
            mtype=mtype,
            exclude_type=exclude_type,
            created_after=created_after,
        )

        return {
            "items": [_item_to_list_dict(i) if summary else _item_to_dict(i) for i in items],
            "total": total,
            "offset": offset,
            "limit": limit,
        }

    return {
        "items": [_item_to_list_dict(i) if summary else _item_to_dict(i) for i in items],
        "total": len(items),
        "offset": offset,
        "limit": limit,
    }


def _api_memory_get(mem_id: str) -> dict | None:

    item = _get_store().get(mem_id)

    if item is None:
        return None

    result = _item_to_dict(item)
    result["projects"] = _get_store().memory_projects(item.id)
    return result


def _api_memory_update(mem_id: str, body: dict) -> dict | None:
    store = _get_store()
    item = store.get(mem_id)

    if item is None:
        return None

    # Update allowed fields

    if "content" in body:
        item.content = body["content"].strip()

        if not item.content:
            raise ValueError("content is required")

        item.content_hash = item.compute_hash()

    if "tags" in body:
        # Direct field mutation bypasses __post_init__, so validate here too.
        from .models import normalize_tags

        item.tags = normalize_tags(body["tags"])

    if "type" in body:
        item.type = body["type"]

    if "scopes" in body:
        from . import scopes as scopes_mod

        item.scopes = scopes_mod.parse(body["scopes"])

    if not store.update(item):
        return None

    return _item_to_dict(item)


def _api_memory_delete(mem_id: str) -> bool:

    return _get_store().delete(mem_id)


def _api_memory_add(body: dict) -> dict:

    from .models import MemoryItem

    content = (body.get("content") or "").strip()

    if not content:
        raise ValueError("content is required")

    project_id = str(body.get("project_id") or "").strip()
    if project_id and _project_service().get(project_id) is None:
        raise ValueError("project not found")

    item = MemoryItem(
        content=content,
        source=body.get("source", "manual"),
        type=body.get("type", "note"),
        tags=body.get("tags", []),
    )

    stored_id, _inserted = _get_store().upsert_status(item)
    stored_item = _get_store().get(stored_id)
    if stored_item is None:
        raise ValueError("memory could not be stored")
    if project_id:
        _project_service().add_memory(project_id, stored_id)

    return _item_to_dict(stored_item)


def _api_whatsapp_preview(body: dict) -> dict:
    from .normalize import parse_whatsapp_messages

    token = str(body.get("preview_token") or "")
    if token:
        preview = _get_whatsapp_preview(token)
        messages = preview["messages"]
        filename = preview["filename"]
    else:
        text = str(body.get("raw_text") or "")
        messages = parse_whatsapp_messages(text)
        if not messages:
            raise ValueError("No WhatsApp messages were found")
        filename = str(body.get("filename") or "whatsapp-export")
        token = _save_whatsapp_preview({"messages": messages, "filename": filename})
    senders = sorted({message["sender"] for message in messages})
    sender = str(body.get("sender") or "")
    indexed = [
        (index, message)
        for index, message in enumerate(messages)
        if not sender or message["sender"] == sender
    ]
    try:
        offset = max(0, int(body.get("offset", 0)))
        limit = max(1, min(int(body.get("limit", WHATSAPP_PREVIEW_PAGE_SIZE)), 500))
    except (TypeError, ValueError) as exc:
        raise ValueError("offset and limit must be integers") from exc
    page = indexed[offset : offset + limit]
    return {
        "privacy_notice": "This may contain private messages from other people. Nothing is saved until you approve it.",
        "message_count": len(messages),
        "filtered_count": len(indexed),
        "senders": senders,
        "messages": [{"index": index, **message} for index, message in page],
        "offset": offset,
        "limit": limit,
        "preview_token": token,
        "filename": filename,
    }


def _api_whatsapp_approve(body: dict) -> dict:
    from .normalize import parse_whatsapp_messages, whatsapp_messages_to_items

    token = str(body.get("preview_token") or "")
    if token:
        preview = _get_whatsapp_preview(token)
        source_ref = preview["filename"]
        messages = preview["messages"]
    else:
        text = str(body.get("raw_text") or "")
        source_ref = str(body.get("filename") or "whatsapp-export")
        messages = parse_whatsapp_messages(text)
    if not messages:
        raise ValueError("No WhatsApp messages were found")

    if body.get("select_all"):
        excluded_indices = {int(index) for index in (body.get("excluded_indices") or [])}
        approved = [
            message for index, message in enumerate(messages) if index not in excluded_indices
        ]
    else:
        indices = body.get("indices")
        selected = {int(index) for index in (indices or [])}
        approved = [message for index, message in enumerate(messages) if index in selected]
    if body.get("save_full"):
        approved = messages
    if not approved:
        raise ValueError("Select at least one message or choose save full conversation")
    excluded = {str(sender) for sender in (body.get("excluded_senders") or [])}
    approved = [message for message in approved if message["sender"] not in excluded]
    items = whatsapp_messages_to_items(
        approved,
        source_ref,
        redact_phone_numbers=bool(body.get("redact_phone_numbers")),
        strip_links=bool(body.get("strip_links")),
        strip_media=bool(body.get("strip_media")),
        type="conversation" if body.get("save_full") else "note",
        tags=[] if body.get("save_full") else ["whatsapp", "approved"],
    )
    if not items:
        raise ValueError("Privacy filters removed every selected message")
    result = _get_store().import_many(items, project_id=body.get("project_id") or None)
    _discard_whatsapp_preview(token)
    return result


def _api_import(body: dict) -> dict:
    """Import memories from a JSON payload (the same format as `ele import`)."""

    from . import normalize as norm_mod

    raw = body.get("data")
    raw_text = body.get("raw_text")
    raw_base64 = body.get("raw_base64")

    source = body.get("source", "")
    project_id = body.get("project_id") or None

    filename = body.get("filename", "import.json")
    text = body.get("text")
    if text and raw is None and raw_text is None:
        items = norm_mod.from_plaintext(text, source=source or "manual", source_ref="web-paste")
        store = _get_store()
        result = store.import_many(items, project_id=project_id)
        # Fresh item ids exist only when that item was actually inserted.
        result["memory_ids"] = [item.id for item in items if store.get(item.id)]
        return result

    if raw is None and raw_text is None and raw_base64 is None:
        raise ValueError("data, raw_text, raw_base64, or text is required")

    import tempfile

    suffix = Path(filename).suffix or ".json"
    tmp_path: str

    if raw_base64 is not None:
        try:
            binary = base64.b64decode(raw_base64, validate=True)
        except (ValueError, TypeError) as exc:
            raise ValueError("raw_base64 must be valid base64") from exc
        with tempfile.NamedTemporaryFile(mode="wb", suffix=suffix, delete=False) as binary_file:
            binary_file.write(binary)
            tmp_path = binary_file.name
    else:
        with tempfile.NamedTemporaryFile(
            mode="w", suffix=suffix, delete=False, encoding="utf-8"
        ) as text_file:
            if raw_text is not None:
                # WhatsApp/txt uploads
                text_file.write(raw_text)
            else:
                import json as _json

                _json.dump(raw, text_file)
            tmp_path = text_file.name

    try:
        items = norm_mod.normalize_file(tmp_path, source=source or None)

    finally:
        Path(tmp_path).unlink(missing_ok=True)

    result = _get_store().import_many(items, project_id=project_id)

    return result


def _api_import_preview(body: dict) -> dict:
    if not isinstance(body, dict):
        raise ValueError("request body must be a JSON object")
    raw_text = body.get("raw_text")
    filename = str(body.get("filename") or "import.csv")
    if not isinstance(raw_text, str) or not raw_text.strip():
        raise ValueError("raw_text is required")
    if Path(filename).suffix.lower() != ".csv":
        raise ValueError("preview is only available for CSV files")
    from .normalize import preview_csv

    preview = preview_csv(raw_text)
    if not preview["supported"]:
        columns = ", ".join(preview["columns"]) or "none"
        raise ValueError(f"Unsupported CSV columns: {columns}")
    return preview


def _item_to_dict(item) -> dict:

    return {
        "id": item.id,
        "id_short": item.id[:8],
        "content": item.content,
        "source": item.source,
        "type": item.type,
        "tags": item.tags,
        "scopes": item.scopes,
        "confidence": item.confidence,
        "created_at": item.created_at,
        "source_ref": item.source_ref,
    }


def _item_to_list_dict(item) -> dict:
    """Return the compact representation used by the paginated library."""
    content = item.content or ""
    preview_limit = 1_200
    return {
        "id": item.id,
        "id_short": item.id[:8],
        "content": content[:preview_limit],
        "content_truncated": len(content) > preview_limit,
        "source": item.source,
        "type": item.type,
        "tags": item.tags,
        "scopes": item.scopes,
        "confidence": item.confidence,
        "created_at": item.created_at,
        "source_ref": item.source_ref,
    }


# ---------------------------------------------------------------------------

# USB API

# ---------------------------------------------------------------------------


def _api_usb_list() -> dict:

    from . import usb

    try:
        drives = usb.list_removable_drives(strict=True)
    except Exception:
        return {
            "drives": [],
            "error": "Drive discovery failed. Refresh or browse for a destination folder.",
        }

    for d in drives:
        d["has_vault"] = usb.is_usb_vault(d["path"])

    return {"drives": drives}


def _api_usb_status(drive: str) -> dict:

    from . import usb

    s = usb.status(usb.validate_destination(drive))

    return s


def _api_usb_install(body: dict) -> dict:

    from . import usb

    drive = body.get("drive", "")

    passphrase = body.get("passphrase", "")

    label = body.get("label", "USB")

    if not drive or not passphrase:
        raise ValueError("drive and passphrase are required")

    if body.get("mode") == "export":
        from .commands.backup import _export_payload

        destination = usb.validate_destination(drive)
        with _state.lock:
            store = _get_store()
            source = store.vault_dir.resolve()
            if destination == source or source in destination.parents:
                raise ValueError("Choose a destination outside the active vault.")
            with store._lock:
                payload = _export_payload(
                    store.all(include_superseded=True), store.vault_dir, store.project_snapshot()
                )
        try:
            result = usb.export_backup(destination, passphrase, payload)
        except OSError as error:
            raise ValueError(
                "The backup could not be saved. Check that the drive is connected, writable, and has enough free space."
            ) from error
        _record_vault_copy(source)
        return result

    if body.get("mode") == "portable":
        from .commands.backup import _export_payload

        destination = usb.validate_destination(drive)
        with _state.lock:
            store = _get_store()
            source = store.vault_dir.resolve()
            if destination == source or source in destination.parents:
                raise ValueError("Choose a destination outside the active vault.")
            with store._lock:
                payload = _export_payload(
                    store.all(include_superseded=True), store.vault_dir, store.project_snapshot()
                )
        try:
            result = usb.install_portable(destination, passphrase, payload)
        except OSError as error:
            raise ValueError(
                "EleSync could not be installed on the drive. Check that it is connected, writable, and has enough free space."
            ) from error
        _record_vault_copy(source)
        return result

    return usb.install(drive, passphrase, label=label)


def _api_audit(params: dict) -> dict:
    """Recent audit-trail entries for the Overview activity stream.

    Audit is enabled by default for connected AI clients. A missing audit file
    means no operation has been recorded yet, not that auditing is disabled.
    Only an explicit false ELESYNC_AUDIT value reports disabled."""
    from .audit import AuditLog

    try:
        last = int(params.get("last", ["12"])[0])
    except ValueError as exc:
        raise ValueError("last must be an integer") from exc
    last = max(1, min(last, 100))
    audit = AuditLog(_state.vault_dir)
    exists = audit.log_file is not None and audit.log_file.exists()
    setting = os.environ.get("ELESYNC_AUDIT", "1").strip().lower()
    enabled = setting not in {"0", "false", "off", "no"}
    entries = audit.read(last=last) if exists else []
    if entries:
        _enrich_audit_entries(entries)
    return {"enabled": enabled, "entries": entries}


_AUDIT_PROJECT_REF = re.compile(r"project=([0-9a-f]{8,32})")
_MOBILE_USER_AGENT = re.compile(r"Mobi|Android|iPhone|iPad", re.IGNORECASE)
_AUDIT_WITH_REF = re.compile(r"with=([0-9a-f,]+)")
_AUDIT_BY_REF = re.compile(r"by=([0-9a-f]{8,32})")


def _enrich_audit_entries(entries: list[dict]) -> None:
    """Attach what an entry touched — memory snippet, source, projects, and for
    disagreements the live conflict group — so the dashboard can say "Claude
    saved to Japan trip: …" or "your assistants disagree" instead of an id."""
    store = _get_store()
    memories: dict[str, dict | None] = {}

    def summary(ref: str) -> dict | None:
        if ref not in memories:
            memories[ref] = _audit_memory_summary(store, ref)
        return memories[ref]

    project_names = {p["id"]: p["name"] for p in store.projects()}
    clusters = None
    if any(entry.get("op") == "CONFLICT" for entry in entries):
        clusters = _open_conflict_clusters(store)
    for entry in entries:
        ref = str(entry.get("mem") or "")
        detail = str(entry.get("detail") or "")
        if ref and summary(ref):
            entry["memory"] = summary(ref)
        match = _AUDIT_PROJECT_REF.search(detail)
        if match:
            prefix = match.group(1)
            found = [name for pid, name in project_names.items() if pid.startswith(prefix)]
            if len(found) == 1:
                entry["project_name"] = found[0]
        if entry.get("op") == "SUPERSEDE":
            by = _AUDIT_BY_REF.search(detail)
            if by and summary(by.group(1)):
                entry["replacement"] = summary(by.group(1))
        if entry.get("op") == "CONFLICT" and clusters is not None:
            entry["conflict"] = _audit_conflict(entry, detail, clusters, summary)


_conflict_cache: dict = {"key": None, "clusters": []}
_conflict_cache_lock = threading.Lock()


def _vault_change_key(store) -> tuple:
    """A cheap fingerprint of everything conflict grouping depends on: any save,
    edit, type change, supersession, embedding or resolution changes it.

    Counts alone miss an edit that keeps the content length ("5432" -> "5433"),
    so the fingerprint also digests every row's id, content hash, type and
    supersession. The id matters: a memory deleted and saved again with the
    same text comes back under a new id, and cached groups holding the old one
    would report its conflict as resolved. Rows are digested in id order so the
    key does not depend on storage order. One table scan, far cheaper than the
    pairwise grouping."""
    import hashlib

    with store._lock:
        row = store.db.execute(
            "SELECT COUNT(*), "
            "SUM(CASE WHEN has_embedding THEN 1 ELSE 0 END), "
            "group_concat(id || ':' || COALESCE(content_hash, '') || ':' "
            "|| COALESCE(type, '') || ':' || COALESCE(superseded_by, ''), ',') "
            "FROM (SELECT id, content_hash, type, superseded_by, "
            "embedding IS NOT NULL AS has_embedding FROM memories ORDER BY id)"
        ).fetchone()
    count, embedded, rows = tuple(row)
    digest = hashlib.sha256((rows or "").encode("utf-8")).hexdigest()
    return (id(store), count, embedded, digest, len(store.resolved_pairs()))


def _open_conflict_clusters(store) -> list[tuple[str, list]]:
    """Open conflict groups for the relay. Grouping scans every fact pairwise,
    and every open dashboard asks for it every few seconds while a disagreement
    is among the recent events, so it is recomputed only when the vault changes."""
    from . import conflicts as _cf
    from . import service as _svc_mod

    try:
        key = _vault_change_key(store)
    except Exception:
        key = None
    with _conflict_cache_lock:
        if key is not None and _conflict_cache["key"] == key:
            return _conflict_cache["clusters"]
    try:
        groups = _svc_mod.MemoryService(store)._get_clusters() or []
    except Exception:
        return []
    clusters = [(_cf.cluster_id(group), group) for group in groups]
    with _conflict_cache_lock:
        _conflict_cache["key"] = key
        _conflict_cache["clusters"] = clusters
    return clusters


def _audit_conflict(entry: dict, detail: str, clusters, summary) -> dict:
    new_id = (entry.get("memory") or {}).get("id", "")
    for cid, group in clusters:
        if any(item.id == new_id for item in group):
            items = [summary(item.id[:8]) for item in sorted(group, key=lambda i: i.created_at)]
            return {"cluster_id": cid, "resolved": False, "items": [i for i in items if i]}
    refs = [str(entry.get("mem") or "")]
    with_ref = _AUDIT_WITH_REF.search(detail)
    if with_ref:
        refs += [r for r in with_ref.group(1).split(",") if r]
    items = [summary(r) for r in refs if r]
    kept = [i for i in items if i]
    return {
        "cluster_id": None,
        "resolved": True,
        "items": sorted(kept, key=lambda i: i.get("created_at") or ""),
    }


def _audit_memory_summary(store, ref: str) -> dict | None:
    ids = store.prefix_lookup(ref)
    if len(ids) != 1:
        return None
    item = store.get(ids[0])
    if item is None:
        return None
    return {
        "id": item.id,
        "preview": item.content[:160],
        "source": item.source,
        "type": item.type,
        "created_at": item.created_at,
        "superseded": bool(item.superseded_by),
        "projects": [p["name"] for p in store.memory_projects(item.id)],
    }


def _api_onboarding_progress() -> dict:
    from . import oauth

    vault = Path(_state.vault_dir).expanduser()
    store = _get_store()
    stats = store.stats()
    by_source = stats.get("by_source", {})
    if not isinstance(by_source, dict):
        by_source = {}
    imported_sources = {
        "chatgpt",
        "claude",
        "gemini",
        "grok",
        "perplexity",
        "copilot",
        "deepseek",
        "whatsapp",
        "import",
    }
    # Live saves are attributed to the assistant that made them ("claude"), so a
    # source name alone no longer proves an import. Every import path records
    # the file it came from in source_ref; live saves leave it empty.
    imported = False
    if any(source.lower() in imported_sources for source in by_source):
        placeholders = ",".join("?" for _ in imported_sources)
        with store._lock:
            imported = (
                store.db.execute(
                    "SELECT 1 FROM memories WHERE COALESCE(source_ref, '') != '' "
                    f"AND LOWER(source) IN ({placeholders}) "
                    "AND (superseded_by IS NULL OR superseded_by = '') LIMIT 1",
                    tuple(sorted(imported_sources)),
                ).fetchone()
                is not None
            )
    projects = store.projects()
    local_connected = any(
        item["status"] == "connected" for item in _api_readiness()["local_clients"]
    )
    oauth_connected = oauth.has_active_authorization(vault)
    gemini_connected = _gemini_connection_status()["connected"]
    return {
        "vault": (vault / "notes").is_dir(),
        "secured": _auth_enabled(),
        "imported": imported,
        "memories": stats.get("total", 0),
        "project": bool(projects),
        "connected": local_connected or oauth_connected or gemini_connected,
        "verified": False,
    }


def _api_connect_verify(body: dict) -> dict:
    import urllib.error
    import urllib.request

    from . import __version__

    url = str(body.get("url") or "").strip()
    token = str(body.get("token") or "").strip()
    if not url.startswith(("https://", "http://")):
        raise ValueError("connection URL must start with http:// or https://")
    if url.startswith("http://") and not body.get("insecure"):
        raise ValueError("HTTPS is required for a remote connection")
    import json as _json

    initialize = _json.dumps(
        {
            "jsonrpc": "2.0",
            "id": "elesync-verifier",
            "method": "initialize",
            "params": {
                "protocolVersion": "2025-03-26",
                "capabilities": {},
                "clientInfo": {"name": "EleSync verifier", "version": __version__},
            },
        }
    ).encode("utf-8")
    request = urllib.request.Request(
        url,
        data=initialize,
        method="POST",
        headers={
            "Accept": "application/json, text/event-stream",
            "Content-Type": "application/json",
        },
    )
    if token:
        request.add_header("Authorization", f"Bearer {token}")

    class _NoRedirect(urllib.request.HTTPRedirectHandler):
        def redirect_request(self, req, fp, code, msg, headers, newurl):
            # A redirect can change the host, downgrade HTTPS, or turn the
            # initialize POST into a GET while retaining its bearer token.
            # Require the user to supply the final MCP URL instead.
            return None

    opener = urllib.request.build_opener(_NoRedirect())
    try:
        with opener.open(request, timeout=8) as response:
            status = response.status
    except urllib.error.HTTPError as error:
        status = error.code
        error.close()
    except (urllib.error.URLError, TimeoutError, OSError) as error:
        return {
            "verified": False,
            "reachable": False,
            "status": "unreachable",
            "detail": str(error),
        }
    if 300 <= status < 400:
        return {
            "verified": False,
            "reachable": True,
            "status": "redirect_refused",
            "detail": (
                "The endpoint redirected the request. Use its final MCP URL directly; "
                "redirects are refused to protect credentials."
            ),
        }
    if 200 <= status < 300:
        return {
            "verified": True,
            "reachable": True,
            "status": "verified",
            "detail": f"MCP initialize handshake succeeded with HTTP {status}.",
        }
    if status == 401:
        return {
            "verified": False,
            "reachable": True,
            "status": "unauthorized",
            "detail": "The endpoint is reachable, but the token was rejected.",
        }
    if status == 405:
        return {
            "verified": False,
            "reachable": True,
            "status": "reachable",
            "detail": "The endpoint is reachable, but it rejected the MCP initialize method.",
        }
    return {
        "verified": False,
        "reachable": True,
        "status": "unexpected",
        "detail": f"Endpoint responded with HTTP {status}.",
    }


_READINESS_AUDIT_WINDOW = 5000


def _assistant_activity(vault: Path) -> dict:
    """Latest audit entry per assistant: proof an assistant actually reached the
    vault, not just that it was configured. Entries with no attributed assistant
    are grouped under ``""``. Only the recent log tail is scanned: the audit
    file grows forever and this runs on every readiness refresh."""
    from .audit import AuditLog

    latest: dict[str, dict] = {}
    for entry in AuditLog(vault).read(last=_READINESS_AUDIT_WINDOW):
        ai = str(entry.get("ai") or "")
        if ai not in latest:
            latest[ai] = {"last_seen": entry.get("ts"), "op": entry.get("op")}
    return latest


def _api_readiness() -> dict:
    from . import __version__, oauth, onboarding
    from .audit import normalize_ai_name

    vault = Path(_state.vault_dir).expanduser()
    authorized = oauth.authorized_providers(vault)
    local_clients = []
    for client in onboarding.detect_local_clients():
        configured = False
        config_path = Path(client["config_path"])
        if config_path.is_file():
            try:
                configured = "elesync" in config_path.read_text(encoding="utf-8")
            except OSError:
                configured = False
        local_clients.append(
            {
                "id": client["target"],
                "ai": normalize_ai_name(client["target"]),
                "label": client["label"],
                "kind": "local",
                "status": "connected" if configured else "ready",
                "detail": "EleSync is wired in" if configured else "Detected on this computer",
            }
        )
    gemini_status = _gemini_connection_status()
    remote_clients = [
        {
            "id": "chatgpt",
            "label": "ChatGPT",
            "kind": "remote",
            "status": "advanced",
            "detail": "Secure connector setup",
        },
        {
            "id": "grok",
            "label": "Grok",
            "kind": "remote",
            "status": "advanced",
            "detail": "Secure connector setup",
        },
        {
            "id": "gemini",
            "label": "Gemini",
            "kind": "remote",
            "status": "connected" if gemini_status["connected"] else "ready",
            "detail": "Gemini API verified"
            if gemini_status["connected"]
            else "Connect with your Gemini API key",
        },
        {
            "id": "perplexity",
            "label": "Perplexity",
            "kind": "remote",
            "status": "advanced",
            "detail": "Secure connector setup",
        },
        {
            "id": "copilot",
            "label": "Copilot",
            "kind": "remote",
            "status": "advanced",
            "detail": "Secure connector setup",
        },
        {
            "id": "deepseek",
            "label": "DeepSeek",
            "kind": "remote",
            "status": "advanced",
            "detail": "Secure connector setup",
        },
    ]
    for client in remote_clients:
        client["ai"] = client["id"]
        if client["id"] in authorized:
            client["status"] = "connected"
            client["detail"] = "Authorized with a secure connector"
    tunnel = _state.tunnel_manager.status()
    return {
        "version": __version__,
        "vault": {"ready": (vault / "notes").is_dir(), "path": str(vault)},
        "security": {"auth": _auth_enabled(), "tls": _state.tls_enabled},
        "local_clients": local_clients,
        "remote_clients": remote_clients,
        "activity": _assistant_activity(vault),
        "tunnel": {"active": bool(tunnel.get("active")), "url": tunnel.get("url")},
        "backup": {"last_backup_at": _last_backup_at(vault)},
    }


def _gemini_connection_status() -> dict:
    from . import gemini

    config = _load_config()
    key = gemini.resolve_api_key(config)
    verified_at = config.get("llm", {}).get("gemini_verified_at")
    return {"connected": bool(key and verified_at), "verified_at": verified_at}


def _api_connect_gemini(body: dict) -> dict:
    from . import gemini
    from .credential_store import set_secret

    api_key = str(body.get("api_key") or "").strip()
    if len(api_key) < 20:
        raise ValueError("Enter a valid Gemini API key.")
    model = str(body.get("model") or gemini.DEFAULT_MODEL).strip()
    response = gemini.verify_api_key(api_key, model)
    try:
        set_secret("gemini-api-key", api_key)
    except OSError as exc:
        raise ValueError(
            "Gemini was verified, but EleSync could not access the operating system credential store."
        ) from exc
    from datetime import datetime, timezone

    config = _load_config()
    llm = config.setdefault("llm", {})
    # The legacy plaintext value is removed only after the OS store accepts it.
    llm.pop("gemini_api_key", None)
    llm["gemini_model"] = model
    llm["gemini_verified_at"] = datetime.now(timezone.utc).isoformat()
    _save_config(config)
    return {"connected": True, "provider": "gemini", "model": model, "verification": response}


def _forwarded_proto(handler) -> str | None:
    """Return the client-facing scheme from X-Forwarded-Proto, restricted to
    ``http``/``https``. Any other value (or absent header) returns ``None``
    so a malicious upstream can't inject ``javascript`` or similar into URLs
    we construct from it."""
    try:
        if not ipaddress.ip_address(handler.client_address[0]).is_loopback:
            return None
    except (AttributeError, IndexError, ValueError):
        return None
    raw = handler.headers.get("X-Forwarded-Proto", "").split(",", 1)[0].strip().lower()
    return raw if raw in ("http", "https") else None


def _oauth_base(handler) -> str:
    forwarded = _forwarded_proto(handler)
    scheme = forwarded or ("https" if _state.tls_enabled else "http")
    return f"{scheme}://{handler.headers.get('Host', 'localhost')}"


def _request_uses_https(handler) -> bool:
    return _state.tls_enabled or _forwarded_proto(handler) == "https"


def _oauth_discovery(handler) -> dict:
    base = _oauth_base(handler)
    return {
        "issuer": base,
        "authorization_endpoint": base + "/oauth/authorize",
        "token_endpoint": base + "/oauth/token",
        "registration_endpoint": base + "/oauth/register",
        "revocation_endpoint": base + "/oauth/revoke",
        "code_challenge_methods_supported": ["S256"],
        "grant_types_supported": ["authorization_code", "refresh_token"],
        "response_types_supported": ["code"],
        "token_endpoint_auth_methods_supported": ["none"],
    }


def _oauth_project_permissions(project_id: str, provider: str) -> dict:
    provider = provider.lower().strip()
    access = _project_service().access(project_id)
    for item in access:
        if item["provider"].lower() == provider and item["status"] == "active":
            return {
                "can_read": bool(item["can_read"]),
                "can_write": bool(item["can_write"]),
                "can_read_conversations": bool(item["can_read_conversations"]),
            }
    raise ValueError("This AI has not been granted access to this project.")


def _oauth_authorize_html(
    message: str = "",
    fields: dict[str, str] | None = None,
    client: dict | None = None,
    providers: list[dict] | None = None,
    csrf_token: str = "",
) -> str:
    hidden = "".join(
        f'<input type="hidden" name="{_html_escape(key)}" value="{_html_escape(value)}">'
        for key, value in (fields or {}).items()
        if key != "provider"
    )
    if csrf_token:
        hidden += (
            f'<input type="hidden" name="{CSRF_FORM_FIELD}" value="{_html_escape(csrf_token)}">'
        )
    provider_options = "".join(
        f'<option value="{_html_escape(item["provider"])}">'
        f"{_html_escape(item['provider'])} — "
        f"{_html_escape(', '.join(name for name, allowed in (('read', item['can_read']), ('write', item['can_write']), ('conversations', item['can_read_conversations'])) if allowed))}"
        "</option>"
        for item in (providers or [])
    )
    callback = (fields or {}).get("redirect_uri", "")
    parsed_callback = urlparse(callback)
    callback_origin = (
        f"{parsed_callback.scheme}://{parsed_callback.netloc}"
        if parsed_callback.scheme and parsed_callback.netloc
        else callback
    )
    identity = ""
    if client:
        identity = (
            "<dl><dt>Unverified client name</dt>"
            f"<dd>{_html_escape(str(client.get('name', 'MCP client')))}</dd>"
            "<dt>Callback destination</dt>"
            f"<dd><code>{_html_escape(callback_origin)}</code></dd></dl>"
            '<p class="warning">Choose the provider grant yourself. Client names are not trusted identities.</p>'
        )
    action = (
        '<form method="post" action="/oauth/authorize">'
        + hidden
        + '<label for="provider">Project grant</label><select id="provider" name="provider" required>'
        + provider_options
        + "</select>"
        + "<button>Approve project access</button></form>"
        if fields and provider_options
        else '<a href="/login">Open EleSync sign in</a>'
    )
    return (
        '<!doctype html><html><head><meta charset="utf-8"><title>Authorize EleSync</title>'
        "<style>body{font:16px system-ui;background:#080812;color:#fff;display:grid;place-items:center;min-height:100vh}"
        ".box{max-width:560px;padding:28px;border:1px solid #39396a;border-radius:16px;background:#131326}button,select{display:block;width:100%;box-sizing:border-box;margin-top:14px;padding:12px 18px;border:0;border-radius:9px}button{background:#c04dff;color:#fff;font-weight:700}.warning{color:#ffcf70}dt{color:#aaa;margin-top:10px}dd{margin:3px 0;overflow-wrap:anywhere}</style>"
        '</head><body><main class="box"><h1>Authorize AI access</h1>'
        f"<p>{_html_escape(message or 'Sign in to EleSync first, then retry the authorization request.')}</p>{identity}{action}"
        "</main></body></html>"
    )


def _oauth_validate_client_request(fields: dict[str, str]) -> dict:
    from . import oauth

    if fields.get("response_type") != "code" or fields.get("code_challenge_method") != "S256":
        raise ValueError("OAuth requires response_type=code and PKCE S256")
    client = oauth.get_client(_state.vault_dir, fields.get("client_id", ""))
    if not client:
        raise ValueError("unknown OAuth client")
    redirect_uri = fields.get("redirect_uri", "")
    if redirect_uri not in client.get("redirect_uris", []):
        raise ValueError("redirect URI is not registered")
    project_id = fields.get("project_id", "")
    if not project_id or _project_service().get(project_id) is None:
        raise ValueError("project not found")
    return client


def _oauth_active_providers(project_id: str) -> list[dict]:
    return [item for item in _project_service().access(project_id) if item["status"] == "active"]


def _oauth_validate_request(fields: dict[str, str]) -> tuple[dict, dict]:
    client = _oauth_validate_client_request(fields)
    provider = fields.get("provider", "").strip()
    if not provider:
        raise ValueError("Choose which project grant this client may use.")
    permissions = _oauth_project_permissions(fields["project_id"], provider)
    return client, permissions


def _html_escape(value: str) -> str:
    import html

    return html.escape(value, quote=True)


# Config keys EleSync writes by itself. They say nothing about whether anyone
# has set it up: versions up to 1.24.55 cached an update check there on their
# very first load, and existing config files still carry it.
_UNATTENDED_CONFIG_KEYS = frozenset({"update_check"})


def _api_setup_status() -> dict:
    from . import crypto as _crypto

    cfg = _load_config()
    vault_path = Path(_state.vault_dir).expanduser()
    # Installer builds set this marker explicitly.  A source or pip install has
    # no config yet, so treat that empty state as first-run too -- unless this
    # vault already has dashboard protection.  Authentication lives with the
    # vault, whereas this marker lives in the app config; treating those two
    # stores independently used to reopen setup and trap protected vaults.
    # "Empty" ignores keys nobody chose, or the update check's cache would
    # hide setup from every source install.
    auth_enabled = _auth_enabled()
    configured = any(key not in _UNATTENDED_CONFIG_KEYS for key in cfg)
    first_run = (
        bool(cfg.get("first_run_setup"))
        if "first_run_setup" in cfg
        else not configured and not auth_enabled
    )
    return {
        "first_run": first_run,
        "vault_dir": _state.vault_dir,
        "auth_enabled": auth_enabled,
        "encrypted": _crypto.is_encrypted(vault_path) if _crypto.available() else False,
        "crypto_available": _crypto.available(),
    }


_REMOTE_CONNECT_CLIENTS = {
    "chatgpt": "ChatGPT",
    "grok": "Grok",
    "gemini": "Gemini",
    "perplexity": "Perplexity",
    "copilot": "Copilot",
    "deepseek": "DeepSeek",
}
_LOCAL_CONNECT_CLIENTS = {
    "claude": ("Claude Desktop", "claude-desktop"),
}


def _quote_cli_arg(value: str) -> str:
    if os.name == "nt":
        return "'" + value.replace("'", "''") + "'"
    import shlex

    return shlex.quote(value)


def _configured_or_new_token(*, force: bool = False) -> tuple[str, bool]:
    from . import auth as _auth_mod

    env_token = os.environ.get("ELESYNC_TOKEN")
    if env_token:
        if force:
            raise ValueError(
                "Cannot replace token while ELESYNC_TOKEN is set. Unset ELESYNC_TOKEN first."
            )
        return env_token, False

    with _state.lock:
        cfg = _load_config()
        existing = cfg.get("http", {}).get("auth", {}).get("token")
        if existing and not force:
            return existing, False

        token = _auth_mod.generate_token()
        cfg.setdefault("http", {}).setdefault("auth", {})
        cfg["http"]["auth"]["mode"] = "token"
        cfg["http"]["auth"]["token"] = token
        if _save_config(cfg) is False:
            raise OSError(
                "EleSync could not save setup. Check that its configuration folder is writable."
            )
        return token, True


def _api_connect_client(body: dict) -> dict:
    client = (body.get("client") or "").strip().lower()
    vault = os.path.expanduser(_state.vault_dir)

    if client in _LOCAL_CONNECT_CLIENTS:
        label, target = _LOCAL_CONNECT_CLIENTS[client]
        project_id = str(body.get("project_id") or "").strip()
        if project_id and _project_service().get(project_id) is None:
            raise ValueError("project not found")
        project_arg = f" --project {_quote_cli_arg(project_id)}" if project_id else ""
        entry_name = f"elesync-project-{project_id[:8]}" if project_id else "elesync"
        name_arg = f" --name {_quote_cli_arg(entry_name)}"
        command = (
            f"ele --vault {_quote_cli_arg(vault)} onboard --target {target}{project_arg}{name_arg}"
        )
        return {
            "client": client,
            "label": label,
            "kind": "local",
            "command": command,
            "steps": [
                f"Run: {command}",
                f"Restart {label}.",
                'Ask: "What useful memories can you retrieve from EleSync?"',
                "This connection is project-bound; saves from it are added to the selected project."
                if project_id
                else "This connection can access the full vault unless you add a project binding.",
                f"Claude entry name: {entry_name}.",
            ],
        }

    if client not in _REMOTE_CONNECT_CLIENTS:
        raise ValueError("unsupported client")

    label = _REMOTE_CONNECT_CLIENTS[client]
    public_url = (body.get("url") or "").strip().rstrip("/")
    allowed_host = (body.get("allowed_host") or "").strip()
    insecure = bool(body.get("insecure"))
    if not public_url and not allowed_host:
        raise ValueError("url or allowed_host is required")
    if public_url and not (
        public_url.startswith("https://") or (insecure and public_url.startswith("http://"))
    ):
        raise ValueError("connector URL must use HTTPS unless insecure is true")

    if public_url:
        parsed = urlparse(public_url)
        if not parsed.netloc:
            raise ValueError("url must be a full URL")
        connector_url = public_url if parsed.path else f"{public_url}/mcp"
        host_for_serve = allowed_host or parsed.netloc
    else:
        scheme = "http" if insecure else "https"
        connector_url = f"{scheme}://{allowed_host}/mcp"
        host_for_serve = allowed_host

    port = int(body.get("port") or 8000)
    token, generated = _configured_or_new_token(force=bool(body.get("force_token")))
    serve_command = (
        f"ele --vault {_quote_cli_arg(vault)} serve --transport http --port {port} "
        f"--allowed-host {_quote_cli_arg(host_for_serve)}"
    )
    return {
        "client": client,
        "label": label,
        "kind": "remote",
        "token": token,
        "token_generated": generated,
        "server_url": connector_url,
        "serve_command": serve_command,
        "local_target": f"http://127.0.0.1:{port}",
        "steps": [
            f"Run: {serve_command}",
            f"Point your tunnel or reverse proxy at http://127.0.0.1:{port}.",
            f"In {label}, add a custom MCP connector using {connector_url}.",
            "Set Authorization: Bearer <token>.",
        ],
    }


def _api_keys_store():
    from .rest_api import ApiKeyStore

    return ApiKeyStore(_get_store().vault_dir)


def _api_key_create(body: dict) -> dict:
    """Create a public-API key; the secret is returned this once and never again."""
    if not isinstance(body, dict):
        raise ValueError("request body must be a JSON object")
    project_id = str(body.get("project_id") or "").strip() or None
    if project_id and _project_service().get(project_id) is None:
        raise ValueError("project not found")
    key, secret = _api_keys_store().create(
        str(body.get("name") or ""), str(body.get("access") or "read"), project_id
    )
    return {"key": key, "secret": secret}


def _api_connect_auto() -> dict:
    """One-click local client wiring.

    Detects which local MCP clients are installed on this machine and writes
    the EleSync server entry into each one's config file — no command to copy,
    no terminal needed. Returns a summary of what was wired so the dashboard
    can show the user exactly what happened.
    """
    from .onboarding import detect_local_clients, wire_client_config

    vault = os.path.expanduser(_state.vault_dir)
    detected = detect_local_clients()
    wired: list[dict] = []
    for client in detected:
        try:
            result = wire_client_config(
                Path(client["config_path"]),
                vault,
                target=client["target"],
            )
            wired.append(
                {
                    "client": client["label"],
                    "target": client["target"],
                    "config_path": result["config_path"],
                    "already_wired": result["updated_existing"],
                    "backed_up": result["backed_up"],
                }
            )
        except OSError as e:
            wired.append(
                {
                    "client": client["label"],
                    "target": client["target"],
                    "error": str(e),
                }
            )
    return {"wired": wired, "count": len(wired)}


def _api_setup_complete(body: dict) -> dict:
    from . import web_auth as _web_auth

    cfg = _load_config()
    already_protected = _auth_enabled()

    # "Skip" is also the recovery action for an existing protected vault.  It
    # must acknowledge setup without weakening or replacing that protection.
    # Changing vaults is deliberately left to the authenticated vault switcher.
    if already_protected:
        requested_vault = str(body.get("vault_dir") or "").strip()
        if (
            requested_vault
            and Path(requested_vault).expanduser() != Path(_state.vault_dir).expanduser()
        ):
            raise ValueError(
                "This dashboard is already protected. Use Switch vault to choose another vault."
            )
        if bool(body.get("protect_dashboard")):
            raise ValueError("Dashboard protection is already enabled. Manage it from Settings.")
        cfg["first_run_setup"] = False
        _save_config(cfg)
        return {"ok": True, "auth_enabled": True, "session_token": None}

    username = str(body.get("username") or "owner").strip()
    passcode = body.get("passcode") or ""
    protect = bool(body.get("protect_dashboard"))
    if protect:
        if not username or len(username) > 64 or any(char.isspace() for char in username):
            raise ValueError("Username must be 1–64 characters with no spaces.")
        if len(passcode) < 8:
            raise ValueError("Password must be at least 8 characters.")

    requested_vault = str(body.get("vault_dir") or "").strip()
    if requested_vault:
        requested_path = Path(requested_vault).expanduser()
        current_path = Path(_state.vault_dir).expanduser()
        if requested_path != current_path:
            requested_path.mkdir(parents=True, exist_ok=True)
            (requested_path / "notes").mkdir(exist_ok=True)
            _api_vault_switch({"vault_dir": str(requested_path)})
        cfg["vault_dir"] = str(requested_path)

    if protect:
        assert _state.user_mgr is not None
        _state.user_mgr.add_user(username, passcode, role="admin")
        _state.session_mgr = _web_auth.SessionManager(
            web_auth_dir(_state.vault_dir), require_existing_user=True
        )

    cfg["first_run_setup"] = False
    if _save_config(cfg) is False:
        raise OSError(
            "EleSync could not save setup. Check that its configuration folder is writable."
        )

    session_token = None
    if protect:
        assert _state.session_mgr is not None
        session_token = _state.session_mgr.create_session(username)

    return {
        "ok": True,
        "auth_enabled": _auth_enabled(),
        "session_token": session_token,
    }


def _api_vault_switch(body: dict) -> dict:
    # Accept the canonical "vault_dir" key; tolerate "path" for robustness.
    new_dir = body.get("vault_dir") or body.get("path") or ""

    passphrase = body.get("passphrase", "")

    if not new_dir:
        raise ValueError("vault_dir is required")

    from . import crypto as _crypto
    from .store import MemoryStore

    vp = Path(new_dir).expanduser()

    if not (vp / "notes").exists():
        raise FileNotFoundError(f"No EleSync vault found at {vp}")

    if not (vp / "notes").is_dir():
        raise NotADirectoryError(f"{vp / 'notes'} is not a directory")

    key = None

    if _crypto.available() and _crypto.is_encrypted(vp):
        if not passphrase:
            raise ValueError("This vault is encrypted. Provide a passphrase.")

        meta = _crypto.read_meta(vp)

        assert meta is not None

        key = _crypto.derive_key(
            passphrase,
            bytes.fromhex(meta["salt"])
            if isinstance(meta["salt"], str) and len(meta["salt"]) == 64
            else __import__("base64").b64decode(meta["salt"]),
            meta["opslimit"],
            meta["memlimit"],
        )

    from . import web_auth as _web_auth

    # Build the new store and managers FULLY before touching any global, so the
    # old vault keeps serving requests while the (possibly slow: USB, reindex)
    # open runs. Then swap _state atomically under _state.lock and close the
    # old store afterwards, so no concurrent request thread can ever observe a
    # closed or half-initialised store.
    new_store = MemoryStore(vp, key=key)
    try:
        new_user_mgr = _web_auth.UserManager(web_auth_dir(vp))
        new_session_mgr = _web_auth.SessionManager(web_auth_dir(vp), require_existing_user=True)
        stats = new_store.stats()
    except Exception:
        new_store.close()
        raise

    with _state.lock:
        old_store = _state.store
        _state.store = new_store
        _state.vault_dir = str(vp)
        _state.user_mgr = new_user_mgr
        _state.session_mgr = new_session_mgr
        _state.whatsapp_previews = {}

    if old_store is not None:
        _retire_store(old_store)

    return {
        "vault_dir": str(vp),
        "encrypted": key is not None,
        "stats": stats,
    }


# ---------------------------------------------------------------------------

# Auth helpers

# ---------------------------------------------------------------------------


def _parse_cookie(header: str | None, name: str) -> str | None:
    """Extract a single cookie value from a raw Cookie header. Deliberately
    minimal (no external cookie-parsing dependency) - this only needs to
    read back the one cookie this server itself sets."""
    if not header:
        return None
    for part in header.split(";"):
        part = part.strip()
        if "=" not in part:
            continue
        key, _, value = part.partition("=")
        if key.strip() == name:
            return value.strip()
    return None


# ---------------------------------------------------------------------------

# HTTP request handler

# ---------------------------------------------------------------------------


class _Handler(BaseHTTPRequestHandler):
    # Set once the request body has been read (or drained), so finish() only
    # drains a body no route consumed.
    _body_read = False

    def handle_one_request(self) -> None:
        self._body_read = False
        self._headers_expired = threading.Event()

        # A socket inactivity timeout alone can be extended forever by sending
        # one byte at a time. Bound the whole header phase, including TLS setup.
        def expire_headers():
            self._headers_expired.set()
            with contextlib.suppress(OSError):
                self.connection.shutdown(socket.SHUT_RD)

        self._header_deadline = threading.Timer(HEADER_TIMEOUT_SECONDS, expire_headers)
        self._header_deadline.daemon = True
        self._header_deadline.start()
        try:
            super().handle_one_request()
        finally:
            self._header_deadline.cancel()

    def parse_request(self) -> bool:
        try:
            parsed = super().parse_request()
            if self._headers_expired.is_set():
                self.close_connection = True
                return False
            return parsed
        finally:
            self._header_deadline.cancel()
            self.connection.settimeout(RESPONSE_IO_TIMEOUT_SECONDS)

    def finish(self) -> None:
        # Routes like /api/tunnel/start and /api/connect/auto never read their
        # body. Closing with it unread resets the connection and the client
        # loses the response (see _drain_request_body).
        if not self._body_read and getattr(self, "command", None) in ("POST", "PUT", "DELETE"):
            try:
                self._drain_request_body()
            except (OSError, ValueError):
                pass
        super().finish()

    def _proxy_mcp(self, body: bytes = b"") -> None:
        import http.client

        upstream_port = int(os.environ.get("ELESYNC_MCP_UPSTREAM_PORT", "8000"))
        headers = {
            key: value
            for key, value in self.headers.items()
            if key.lower()
            in {
                "accept",
                "authorization",
                "content-type",
                "mcp-session-id",
                "last-event-id",
                "origin",
                "user-agent",
            }
        }
        headers["Host"] = self.headers.get("Host", "localhost")
        if body:
            headers["Content-Length"] = str(len(body))
        conn = None
        try:
            conn = http.client.HTTPConnection("127.0.0.1", upstream_port, timeout=30)
            conn.request(self.command, self.path, body=body or None, headers=headers)
            response = conn.getresponse()
            self.send_response(response.status, response.reason)
            for key, value in response.getheaders():
                if key.lower() not in {"connection", "keep-alive", "transfer-encoding"}:
                    self.send_header(key, value)
            self.end_headers()
            while True:
                chunk = response.read(64 * 1024)
                if not chunk:
                    break
                self.wfile.write(chunk)
                self.wfile.flush()
        except (OSError, http.client.HTTPException) as exc:
            self._send_error(502, f"MCP upstream unavailable: {exc}")
        finally:
            if conn is not None:
                with contextlib.suppress(Exception):
                    conn.close()

    def log_message(self, fmt, *args):

        pass  # silence access logs

    def _send_json(self, data, status: int = 200):

        body = json.dumps(data, default=str).encode("utf-8")

        self.send_response(status)

        self.send_header("Content-Type", "application/json")

        self.send_header("Content-Length", str(len(body)))

        self.end_headers()

        self.wfile.write(body)

    def _send_bytes(self, body: bytes, content_type: str, status: int = 200):
        self.send_response(status)
        self.send_header("Content-Type", content_type)
        self.send_header("Content-Length", str(len(body)))
        self.end_headers()
        self.wfile.write(body)

    def _send_html(self, html: str):

        body = html.encode("utf-8")
        gzipped = len(body) > 1024 and _accepts_gzip(self.headers.get("Accept-Encoding"))
        if gzipped:
            body = gzip.compress(body, compresslevel=6, mtime=0)

        self.send_response(200)

        self.send_header("Content-Type", "text/html; charset=utf-8")
        if gzipped:
            self.send_header("Content-Encoding", "gzip")
        self.send_header("Vary", "Accept-Encoding")

        self.send_header("Content-Length", str(len(body)))

        self.end_headers()

        self.wfile.write(body)

    def _send_error(self, status: int, message: str):

        self._send_json({"error": message}, status)

    # Domain exceptions carry user-meaningful, safe-to-return messages (a missing
    # field, a vault that isn't there). Anything else is an unexpected internal
    # failure whose text can leak server paths / internals, so it's logged
    # server-side and reported to the client generically.
    _SAFE_EXC = (ValueError, FileNotFoundError, NotADirectoryError, PermissionError)

    def _fail(self, exc: Exception, *, status: int = 400):
        """Respond to a handler exception without disclosing internals."""
        if isinstance(exc, RequestTooLarge):
            self._send_error(413, str(exc))
        elif isinstance(exc, RequestTimeout):
            self._send_error(408, str(exc))
        elif isinstance(exc, self._SAFE_EXC):
            self._send_error(status, str(exc))
        else:
            print(
                f"web: unexpected error handling {self.command} {self.path}: {exc!r}",
                file=sys.stderr,
            )
            self._send_error(500, "internal server error")

    def _send_file(
        self,
        file_path: Path,
        content_type: str = "application/octet-stream",
        cache_control: str | None = None,
        extra_headers: dict[str, str] | None = None,
    ):
        """Send a file with the appropriate content type.

        Every response carries an ETag, so a browser revalidating a `no-cache`
        file gets an empty 304 when it hasn't changed, and text is gzipped for
        clients that accept it. The dashboard used to download ~2.3 MB of
        unchanged files on every load."""
        try:
            data = file_path.read_bytes()
            compressible = _compressible(content_type)
            gzipped = (
                compressible
                and len(data) > 1024
                and _accepts_gzip(self.headers.get("Accept-Encoding"))
            )
            tag = hashlib.blake2b(data, digest_size=12).hexdigest()
            etag = f'"{tag}-gz"' if gzipped else f'"{tag}"'
            headers = {"ETag": etag}
            if cache_control is not None:
                headers["Cache-Control"] = cache_control
            if compressible:
                headers["Vary"] = "Accept-Encoding"
            headers.update(extra_headers or {})
            wanted = _etags(self.headers.get("If-None-Match"))
            if etag in wanted or "*" in wanted:
                self.send_response(304)
                for header, value in headers.items():
                    self.send_header(header, value)
                self.end_headers()
                return
            if gzipped:
                data = _gzip_once(etag, data)
            self.send_response(200)
            self.send_header("Content-Type", content_type)
            if gzipped:
                self.send_header("Content-Encoding", "gzip")
            self.send_header("Content-Length", str(len(data)))
            for header, value in headers.items():
                self.send_header(header, value)
            self.end_headers()
            self.wfile.write(data)
        except Exception as e:
            print(f"web: error reading {file_path!r}: {e!r}", file=sys.stderr)
            self._send_error(500, "internal server error")

    def _read_body(self, *, max_bytes: int = MAX_REQUEST_BODY_BYTES) -> dict:
        return json.loads(self._read_request_bytes(max_bytes=max_bytes))

    def _read_request_bytes(self, *, max_bytes: int = MAX_REQUEST_BODY_BYTES) -> bytes:
        try:
            length = int(self.headers.get("Content-Length", 0))
        except (TypeError, ValueError) as e:
            raise ValueError("invalid Content-Length") from e

        if length < 0:
            raise ValueError("invalid Content-Length")

        if length > max_bytes:
            raise RequestTooLarge(f"request body exceeds the {max_bytes // (1 << 20)} MiB limit")

        self._body_read = True
        if length == 0:
            return b""

        connection = getattr(self, "connection", None)
        previous = connection.gettimeout() if connection is not None else None
        deadline = time.monotonic() + BODY_TIMEOUT_SECONDS
        chunks = []
        remaining = length
        try:
            while remaining:
                left = deadline - time.monotonic()
                if left <= 0:
                    raise RequestTimeout("request body read timed out")
                if connection is not None:
                    connection.settimeout(left)
                chunk = self.rfile.read1(min(remaining, 64 << 10))
                if not chunk:
                    raise ValueError("incomplete request body")
                chunks.append(chunk)
                remaining -= len(chunk)
        except TimeoutError as exc:
            raise RequestTimeout("request body read timed out") from exc
        finally:
            if connection is not None:
                connection.settimeout(previous)
        return b"".join(chunks)

    def _read_form_body(self) -> dict[str, list[str]]:
        return parse_qs(self._read_request_bytes().decode("utf-8"))

    def _from_phone(self, *, companion: bool = False) -> bool:
        """Whether a write came from the phone: the companion quick note, or any
        save made from a mobile browser."""
        return companion or bool(_MOBILE_USER_AGENT.search(self.headers.get("User-Agent", "")))

    def _current_session_user(self) -> str | None:
        """Username for the current request's session cookie, or None if
        there isn't a valid one. Always None when auth isn't configured."""
        if not _auth_enabled():
            return None
        token = _parse_cookie(self.headers.get("Cookie"), SESSION_COOKIE_NAME)
        if not token:
            return None
        assert _state.session_mgr is not None  # guaranteed by the _auth_enabled() check above
        return _state.session_mgr.validate_session(token)

    def _drain_request_body(self) -> None:
        """Read and discard any unread request body before responding.

        The auth gates reject requests *before* _read_body() runs. Closing
        the connection with the client's body still unread makes the OS
        send a TCP reset; Windows clients then see
        ConnectionAbortedError [WinError 10053] instead of our 401/403
        (the exact intermittent failure in
        test_viewer_cannot_update_memory). Draining is bounded in size and
        in time, so neither a huge Content-Length nor a client that announces
        more body than it sends can stall a rejected request. Only call this
        on paths where the body has NOT been read yet - reading past a
        consumed body would block on the socket."""
        try:
            length = int(self.headers.get("Content-Length", 0) or 0)
        except (TypeError, ValueError):
            return
        self._body_read = True
        remaining = min(length, MAX_DRAIN_BYTES)
        if remaining <= 0:
            return
        # One deadline for the whole drain, applied before each socket read;
        # read1 makes at most one read, so a trickling client can't stretch it.
        connection = getattr(self, "connection", None)
        previous = connection.gettimeout() if connection is not None else None
        deadline = time.monotonic() + DRAIN_TIMEOUT_SECONDS
        try:
            while remaining > 0:
                left = deadline - time.monotonic()
                if left <= 0:
                    break
                if connection is not None:
                    connection.settimeout(left)
                chunk = self.rfile.read1(min(remaining, DRAIN_CHUNK_BYTES))
                if not chunk:
                    break
                remaining -= len(chunk)
        except OSError:
            pass  # Timed out or the client left; the response still goes out.
        finally:
            if connection is not None:
                connection.settimeout(previous)

    def _refuse_foreign_host(self) -> bool:
        """Refuse a request addressed to a name this dashboard doesn't answer to
        (DNS rebinding); see ``_host_allowed``."""
        if _host_allowed(self.headers.get("Host")):
            return False
        self._drain_request_body()
        self._send_error(
            403,
            "unknown host name; add it to ELESYNC_ALLOWED_HOSTS to open the dashboard under it",
        )
        return True

    def _refuse_cross_site(self) -> bool:
        """Refuse a change that another website sent through this browser.

        Browsers send a plain-text POST to any address without asking first, so
        without this any page could write into a vault that has no dashboard
        password. Modern browsers label where a request came from
        (Sec-Fetch-Site); older ones still send Origin. Apps and AI clients send
        neither and are unaffected."""
        site = (self.headers.get("Sec-Fetch-Site") or "").lower()
        if site:
            cross = site not in ("same-origin", "none")
        else:
            origin = (self.headers.get("Origin") or "").strip().lower()
            host = (self.headers.get("Host") or "").strip().lower()
            cross = bool(origin) and (origin == "null" or urlparse(origin).netloc != host)
        if cross:
            self._drain_request_body()
            self._send_error(403, "cross-site request refused")
        return cross

    def _require_auth(self) -> str | None:
        """Gate for API routes. Returns the authenticated username, or
        sends a 401 and returns None - callers must check for None and
        stop processing the request when they get it back."""
        if not _auth_enabled():
            return "__no_auth_configured__"
        username = self._current_session_user()
        if username is None:
            self._drain_request_body()
            self._send_error(401, "authentication required")
            return None
        return username

    def _require_write_access(self, username: str) -> bool:
        """Gate for mutating routes (POST/PUT/DELETE). Returns True if the
        request may proceed, or sends a 403 and returns False for a
        viewer/guest account. Always True when auth isn't configured."""
        if not _auth_enabled() or username == "__no_auth_configured__":
            return True
        assert _state.user_mgr is not None  # guaranteed by the _auth_enabled() check above
        if _state.user_mgr.is_guest_or_viewer(username):
            self._drain_request_body()
            self._send_error(403, "read-only account - write access denied")
            return False
        return True

    def _serve_public_api(self, path: str, query: str) -> None:
        """Answer one ``/api/v1`` request (see rest_api.py).

        Always needs an API key, whether or not the dashboard has a password, and
        never uses the dashboard session. Browsers can't send the key cross-site:
        an Authorization header needs a CORS preflight, which is never granted."""
        from . import rest_api
        from .audit import AuditLog

        def send(status: int, payload: dict, headers: dict[str, str] | None = None) -> None:
            body = json.dumps(payload, default=str).encode("utf-8")
            self.send_response(status)
            self.send_header("Content-Type", "application/json")
            self.send_header("Cache-Control", "no-store")
            for name, value in (headers or {}).items():
                self.send_header(name, value)
            self.send_header("Content-Length", str(len(body)))
            self.end_headers()
            self.wfile.write(body)

        # Keys, data and the audit log all come from the store pinned for this request,
        # so a vault switch mid-request can't check a key against one vault and then
        # serve another vault's data.
        store = _get_store()
        vault_dir = store.vault_dir
        secret = rest_api.bearer_token(self.headers.get("Authorization"))
        key = rest_api.ApiKeyStore(vault_dir).verify(secret) if secret else None
        if key is None:
            self._drain_request_body()
            send(
                401,
                rest_api.error_body(
                    "unauthorized", "send a valid API key: Authorization: Bearer esk_..."
                ),
                {"WWW-Authenticate": 'Bearer realm="EleSync"'},
            )
            return
        wait = rest_api.RATE_LIMITER.retry_after(key["id"])
        if wait > 0:
            self._drain_request_body()
            send(
                429,
                rest_api.error_body("rate_limited", "too many requests; slow down and retry"),
                {"Retry-After": str(math.ceil(wait))},
            )
            return

        def read_body():
            try:
                return self._read_body(max_bytes=rest_api.MAX_BODY_BYTES)
            except RequestTooLarge as exc:
                raise rest_api.ApiError(413, "too_large", str(exc)) from exc
            except RequestTimeout as exc:
                raise rest_api.ApiError(408, "request_timeout", str(exc)) from exc

        try:
            from .project_service import ProjectService

            api = rest_api.RestApi(store, ProjectService(store), audit=AuditLog(vault_dir))
            status, payload = api.handle(self.command, path, parse_qs(query), read_body, key)
        except Exception as exc:
            print(f"web: unexpected error in {self.command} {path}: {exc!r}", file=sys.stderr)
            status, payload = 500, rest_api.error_body("internal_error", "internal server error")
        send(status, payload)

    @staticmethod
    def _is_public_api(path: str) -> bool:
        return path == "/api/v1" or path.startswith("/api/v1/")

    def do_OPTIONS(self):
        if self._refuse_foreign_host():
            return

        self.send_response(200)

        self.send_header("Allow", "GET, POST, PUT, DELETE, OPTIONS")

        self.end_headers()

    @_with_store_lease
    def do_GET(self):
        if self._refuse_foreign_host():
            return

        parsed = urlparse(self.path)

        path = parsed.path.rstrip("/") or "/"

        if path == "/mcp":
            self._proxy_mcp()
            return

        if self._is_public_api(path):
            self._serve_public_api(path, parsed.query)
            return

        params = parse_qs(parsed.query)

        if path == "/login":
            self._send_html(_LOGIN_HTML)

            return

        if path == "/" or path == "/index.html":
            if _auth_enabled() and self._current_session_user() is None:
                self.send_response(302)

                self.send_header("Location", "/login")

                self.end_headers()

                return

            self._send_html(_HTML)

            return

        # --- PWA service worker ---
        if path == "/service-worker.js":
            worker = Path(__file__).parent / "static" / "service-worker.js"
            self._send_file(
                worker,
                "text/javascript; charset=utf-8",
                cache_control="no-cache",
                extra_headers={"Service-Worker-Allowed": "/"},
            )
            return

        # --- Static assets ---
        if path.startswith("/assets/"):
            assets_dir = Path(__file__).parent.parent / "assets"
            target = _lookup_bundled_file(assets_dir, path[len("/assets/") :])
            if target is not None:
                content_type = (
                    "image/png" if target.suffix == ".png" else "application/octet-stream"
                )
                self._send_file(target, content_type)
                return
            self._send_error(404, "asset not found")
            return

        # --- Package static assets (dashboard / login JS + CSS) ---
        if path.startswith("/static/"):
            static_dir = Path(__file__).parent / "static"
            target = _lookup_bundled_file(static_dir, path[len("/static/") :])
            if target is not None:
                ctype = {
                    ".js": "text/javascript; charset=utf-8",
                    ".css": "text/css; charset=utf-8",
                    ".json": "application/manifest+json; charset=utf-8",
                    ".png": "image/png",
                    ".webp": "image/webp",
                    ".svg": "image/svg+xml",
                    ".woff2": "font/woff2",
                }.get(target.suffix, "application/octet-stream")
                self._send_file(target, ctype, cache_control="no-cache")
                return
            self._send_error(404, "static asset not found")
            return

        # --- API routes ---

        if path == "/.well-known/oauth-authorization-server":
            self._send_json(_oauth_discovery(self))
            return

        if path == "/.well-known/oauth-protected-resource":
            self._send_json(
                {
                    "resource": _oauth_base(self) + "/mcp",
                    "authorization_servers": [_oauth_base(self)],
                }
            )
            return

        if path == "/oauth/authorize":
            username = self._current_session_user()
            if username is None:
                self._send_html(
                    _oauth_authorize_html("Sign in to EleSync before approving this AI connection.")
                )
                return
            if _state.user_mgr is None or not _state.user_mgr.is_admin(username):
                self._send_error(403, "administrator approval required")
                return
            session_token = _parse_cookie(self.headers.get("Cookie"), SESSION_COOKIE_NAME) or ""
            csrf = _csrf_token_for(session_token) if session_token else ""
            fields = {key: values[0] for key, values in params.items() if values}
            try:
                client = _oauth_validate_client_request(fields)
                providers = _oauth_active_providers(fields["project_id"])
                if not providers:
                    raise ValueError("This project has no active AI access grants.")
                message = (
                    f"Approve access to project {fields.get('project_id', '')[:8]} only if "
                    "you recognize the callback destination below."
                )
                self._send_html(
                    _oauth_authorize_html(message, fields, client, providers, csrf_token=csrf)
                )
            except ValueError as e:
                self._send_error(400, str(e))
            return

        if path == "/api/me":
            username = self._current_session_user()

            if username is None:
                self._send_json({"authenticated": False, "auth_required": _auth_enabled()})

            else:
                self._send_json(
                    {
                        "authenticated": True,
                        "username": username,
                        "role": "viewer"
                        if _state.user_mgr.is_guest_or_viewer(username)
                        else "admin",
                    }
                )

            return

        username = self._require_auth()

        if username is None:
            return

        if path == "/api/share-target":
            token = params.get("token", [""])[0]
            try:
                payload = _take_share_target(token)
                response = {
                    "title": payload["title"],
                    "text": payload["text"],
                    "url": payload["url"],
                    "files": [
                        {
                            "name": item["name"],
                            "type": item["type"],
                            "data": __import__("base64").b64encode(item["data"]).decode("ascii"),
                        }
                        for item in payload["files"]
                    ],
                }
                self._send_json(response)
            except ValueError as e:
                self._fail(e, status=404)

        elif path == "/api/stats":
            self._send_json(_api_stats())

        elif path == "/api/onboarding/progress":
            self._send_json(_api_onboarding_progress())

        elif path == "/api/readiness":
            self._send_json(_api_readiness())

        elif path == "/api/setup":
            status = _api_setup_status()
            if _state.demo_mode:
                # The demo runs on a throwaway vault; never offer first-run setup.
                status["first_run"] = False
            self._send_json(status)

        elif path == "/api/demo":
            from . import demo

            self._send_json(
                {"enabled": _state.demo_mode, "steps": demo.STEPS if _state.demo_mode else []}
            )

        elif path == "/api/demo/history" and _state.demo_mode:
            from . import demo

            self._send_json(demo.history_export())

        elif path == "/api/memories":
            self._send_json(_api_memories(params))

        elif path == "/api/graph":
            from .graph import memory_graph

            try:
                self._send_json(memory_graph(_get_store(), params.get("project_id", [""])[0]))
            except ValueError as e:
                self._fail(e)

        elif path == "/api/projects":
            self._send_json(_project_api().list())

        elif path == "/api/project-discovery":
            try:
                self._send_json(_project_api().discover(params))
            except ValueError as e:
                self._fail(e)

        elif path.startswith("/api/projects/") and path.endswith("/suggestions"):
            project_id = path[len("/api/projects/") : -len("/suggestions")]
            try:
                self._send_json(_project_api().suggestions(project_id))
            except ValueError:
                self._send_error(404, "not found")

        elif path.startswith("/api/projects/") and path.endswith("/search"):
            project_id = path[len("/api/projects/") : -len("/search")]
            try:
                self._send_json(_project_api().search(project_id, params))
            except ValueError:
                self._send_error(404, "not found")

        elif path.startswith("/api/projects/") and path.endswith("/access"):
            project_id = path[len("/api/projects/") : -len("/access")]
            try:
                self._send_json(_project_api().access(project_id))
            except ValueError:
                self._send_error(404, "not found")

        elif path.startswith("/api/projects/") and path.endswith("/activity"):
            project_id = path[len("/api/projects/") : -len("/activity")]
            try:
                self._send_json(_project_api().timeline(project_id))
            except ValueError:
                self._send_error(404, "not found")

        elif path.startswith("/api/projects/") and path.endswith("/context"):
            project_id = path[len("/api/projects/") : -len("/context")]
            try:
                self._send_json(_project_api().context(project_id))
            except ValueError:
                self._send_error(404, "not found")

        elif path.startswith("/api/projects/") and path.endswith("/brief"):
            project_id = path[len("/api/projects/") : -len("/brief")]
            try:
                self._send_json(_project_api().brief(project_id))
            except ValueError:
                self._send_error(404, "not found")

        elif path.startswith("/api/projects/") and path.endswith("/handoff"):
            project_id = path[len("/api/projects/") : -len("/handoff")]
            try:
                self._send_json(_project_api().handoff(project_id))
            except ValueError:
                self._send_error(404, "not found")

        elif path.startswith("/api/projects/") and path.endswith("/export"):
            project_id = path[len("/api/projects/") : -len("/export")]
            try:
                self._send_json(_project_api().export(project_id))
            except ValueError:
                self._send_error(404, "not found")

        elif path.startswith("/api/projects/"):
            project_id = path[len("/api/projects/") :]
            project = _project_api().get(project_id)
            if project is None:
                self._send_error(404, "not found")
            else:
                self._send_json(project)

        elif path.startswith("/api/memories/"):
            mem_id = path[len("/api/memories/") :]

            item = _api_memory_get(mem_id)

            if item is None:
                self._send_error(404, "not found")

            else:
                self._send_json(item)

        elif path == "/api/vault":
            self._send_json({"vault_dir": _state.vault_dir})

        elif path == "/api/usb/list":
            self._send_json(_api_usb_list())

        elif path == "/api/usb/folders":
            from . import usb

            try:
                self._send_json(usb.destination_folders(params.get("path", [""])[0]))
            except (OSError, ValueError) as error:
                self._fail(error)

        elif path.startswith("/api/usb/status/"):
            drive = unquote(path[len("/api/usb/status/") :])
            try:
                self._send_json(_api_usb_status(drive))
            except (OSError, ValueError) as error:
                self._fail(error)

        elif path == "/api/conflicts":
            from . import service as _svc_mod

            svc = _svc_mod.MemoryService(_get_store())
            clusters = svc._get_clusters() or []
            result = []
            for grp in clusters:
                from . import conflicts as _cf

                cid = _cf.cluster_id(grp)
                items = [
                    {
                        "id": it.id,
                        "content": it.content,
                        "source": it.source,
                        "created_at": it.created_at,
                        "type": it.type,
                    }
                    for it in sorted(grp, key=lambda i: i.created_at)
                ]
                result.append({"cluster_id": cid, "items": items})
            self._send_json({"conflicts": result, "total": len(result)})

        elif path == "/api/scopes":
            self._send_json({"scopes": _get_store().scope_counts()})

        elif path == "/api/tunnel/status":
            self._send_json(_state.tunnel_manager.status())

        elif path == "/api/audit":
            self._send_json(_api_audit(params))

        elif path == "/api/api-keys":
            # Managing keys is for accounts that can write; viewers can't list them.
            if self._require_write_access(username):
                try:
                    self._send_json({"keys": _api_keys_store().list()})
                except OSError as e:
                    self._fail(e, status=500)

        elif path == "/api/export":
            store = _get_store()
            items = store.all()
            export = [
                {
                    "id": it.id,
                    "content": it.content,
                    "source": it.source,
                    "type": it.type,
                    "tags": it.tags,
                    "created_at": it.created_at,
                    "scopes": it.scopes,
                }
                for it in items
            ]
            self._send_json(
                {
                    "memories": export,
                    "total": len(export),
                    "vault_dir": _state.vault_dir,
                    "exported_at": __import__("datetime").datetime.now().isoformat(),
                }
            )

        elif path == "/api/network-info":
            import socket

            tunnel = _state.tunnel_manager.status()
            bound_host = str(self.server.server_address[0])
            local_ip = None
            try:
                s = socket.socket(socket.AF_INET, socket.SOCK_DGRAM)
                s.connect(("8.8.8.8", 80))
                local_ip = s.getsockname()[0]
                s.close()
            except OSError:
                pass
            self._send_json(
                {
                    "local_ip": local_ip,
                    "port": self.server.server_address[1],
                    "tls": _state.tls_enabled,
                    "auth_enabled": _auth_enabled(),
                    "scheme": "https" if _state.tls_enabled else "http",
                    "bound_host": bound_host,
                    "tunnel_active": bool(tunnel.get("active") and tunnel.get("url")),
                    "pairing_url": tunnel.get("url"),
                    "reachable_from_network": bool(tunnel.get("active") and tunnel.get("url"))
                    or bound_host not in ("127.0.0.1", "::1", "localhost"),
                }
            )

        elif path == "/api/network-qr":
            import io
            import socket

            tunnel = _state.tunnel_manager.status()
            if tunnel.get("active") and tunnel.get("url"):
                pairing_url = str(tunnel["url"]).rstrip("/") + "/"
            else:
                pairing_url = None
            local_ip = None
            try:
                s = socket.socket(socket.AF_INET, socket.SOCK_DGRAM)
                s.connect(("8.8.8.8", 80))
                local_ip = s.getsockname()[0]
                s.close()
            except OSError:
                pass
            if pairing_url is None:
                bound_host = str(self.server.server_address[0])
                if bound_host in ("127.0.0.1", "::1", "localhost"):
                    self._send_json(
                        {
                            "error": "Phone pairing is unavailable while EleSync is bound to this computer only."
                        },
                        status=409,
                    )
                    return
                scheme = "https" if _state.tls_enabled else "http"
                host = local_ip or self.headers.get("Host", bound_host).split(":", 1)[0]
                pairing_url = f"{scheme}://{host}:{self.server.server_address[1]}"
            import qrcode
            from qrcode.image.svg import SvgPathImage

            code = qrcode.QRCode(box_size=8, border=2)
            code.add_data(pairing_url)
            code.make(fit=True)
            image = code.make_image(image_factory=SvgPathImage)
            output = io.BytesIO()
            image.save(output)
            self._send_bytes(output.getvalue(), "image/svg+xml; charset=utf-8")

        else:
            self._send_error(404, "not found")

    @_with_store_lease
    def do_POST(self):
        if self._refuse_foreign_host() or self._refuse_cross_site():
            return

        parsed = urlparse(self.path)

        path = parsed.path.rstrip("/")

        if path == "/mcp":
            # Unauthenticated here (the upstream checks the token), so the
            # size cap and Content-Length checks matter most on this route.
            try:
                body = self._read_request_bytes()
            except ValueError as e:
                self._fail(e)
                return
            self._proxy_mcp(body)
            return

        if self._is_public_api(path):
            self._serve_public_api(path, parsed.query)
            return

        if path == "/api/login":
            try:
                body = self._read_body()

                username = (body.get("username") or "").strip()

                password = body.get("password") or ""

                if not _auth_enabled():
                    self._send_error(400, "auth is not configured for this vault")

                    return

                wait = _sign_in_throttle.wait_seconds(username)
                if wait > 0:
                    self._send_error(
                        429, f"Too many sign-in attempts. Try again in {math.ceil(wait)} seconds."
                    )
                    return

                if not _state.user_mgr.authenticate(username, password):
                    _sign_in_throttle.failed(username)
                    self._send_error(401, "invalid username or password")

                    return
                _sign_in_throttle.succeeded(username)

                token = _state.session_mgr.create_session(username)

                cookie = f"{SESSION_COOKIE_NAME}={token}; HttpOnly; SameSite=Strict; Path=/"
                if _request_uses_https(self):
                    cookie += "; Secure"

                self.send_response(200)

                self.send_header("Content-Type", "application/json")

                self.send_header("Set-Cookie", cookie)

                body_bytes = json.dumps(
                    {
                        "ok": True,
                        "username": username,
                        "role": "viewer"
                        if _state.user_mgr.is_guest_or_viewer(username)
                        else "admin",
                    }
                ).encode("utf-8")

                self.send_header("Content-Length", str(len(body_bytes)))

                self.end_headers()

                self.wfile.write(body_bytes)

            except Exception as e:
                self._fail(e)

            return

        if path == "/api/logout":
            token = _parse_cookie(self.headers.get("Cookie"), SESSION_COOKIE_NAME)

            if token and _state.session_mgr is not None:
                _state.session_mgr.revoke_session(token)

            self.send_response(200)

            self.send_header("Content-Type", "application/json")

            # Expire the cookie immediately (Max-Age=0) so the browser drops it.
            self.send_header(
                "Set-Cookie",
                f"{SESSION_COOKIE_NAME}=; HttpOnly; SameSite=Strict; Path=/; Max-Age=0",
            )

            body_bytes = json.dumps({"ok": True}).encode("utf-8")

            self.send_header("Content-Length", str(len(body_bytes)))

            self.end_headers()

            self.wfile.write(body_bytes)

            return

        if path == "/oauth/register":
            try:
                from . import oauth

                body = self._read_body()
                redirect_uris = body.get("redirect_uris") or []
                if not isinstance(redirect_uris, list):
                    raise ValueError("redirect_uris must be a list")
                self._send_json(
                    oauth.register_client(
                        _state.vault_dir, body.get("client_name", "MCP client"), redirect_uris
                    ),
                    201,
                )
            except oauth.ClientRegistrationRateLimit as e:
                self._send_error(429, str(e))
            except Exception as e:
                self._fail(e)
            return

        if path == "/oauth/token":
            try:
                from . import oauth

                form = self._read_form_body()
                grant_type = form.get("grant_type", ["authorization_code"])[0]
                if grant_type == "refresh_token":
                    response = oauth.refresh_access_token(
                        _state.vault_dir,
                        form.get("refresh_token", [""])[0],
                        form.get("client_id", [""])[0],
                    )
                elif grant_type == "authorization_code":
                    response = oauth.exchange_code(
                        _state.vault_dir,
                        form.get("code", [""])[0],
                        form.get("client_id", [""])[0],
                        form.get("redirect_uri", [""])[0],
                        form.get("code_verifier", [""])[0],
                    )
                else:
                    raise ValueError("unsupported grant_type")
                self._send_json(response)
            except Exception as e:
                self._fail(e, status=400)
            return

        if path == "/oauth/revoke":
            try:
                from . import oauth

                form = self._read_form_body()
                self._send_json(
                    {"revoked": oauth.revoke_token(_state.vault_dir, form.get("token", [""])[0])}
                )
            except Exception as e:
                self._fail(e, status=400)
            return

        if path == "/oauth/authorize":
            username = self._current_session_user()
            if username is None:
                self._send_error(401, "sign in required")
                return
            if _state.user_mgr is None or not _state.user_mgr.is_admin(username):
                self._drain_request_body()
                self._send_error(403, "administrator approval required")
                return
            try:
                from . import oauth

                form = {key: values[0] for key, values in self._read_form_body().items() if values}
                session_token = _parse_cookie(self.headers.get("Cookie"), SESSION_COOKIE_NAME) or ""
                if not _verify_csrf(session_token, form.get(CSRF_FORM_FIELD, "")):
                    self._send_error(403, "CSRF token missing or invalid")
                    return
                _client, permissions = _oauth_validate_request(form)
                code = oauth.create_code(
                    _state.vault_dir,
                    form["client_id"],
                    form["redirect_uri"],
                    form["code_challenge"],
                    form["project_id"],
                    permissions,
                    provider=form["provider"],
                )
                separator = "&" if "?" in form["redirect_uri"] else "?"
                location = (
                    form["redirect_uri"]
                    + separator
                    + urlencode({"code": code, "state": form.get("state", "")})
                )
                self.send_response(302)
                self.send_header("Location", location)
                self.end_headers()
            except Exception as e:
                self._fail(e, status=400)
            return

        username = self._require_auth()

        if username is None:
            return

        if not self._require_write_access(username):
            return

        if path == "/api/tunnel/start":
            if not _auth_enabled():
                self._send_error(
                    403,
                    "Protect the dashboard before creating a public phone pairing tunnel.",
                )
                return
            try:
                success = _state.tunnel_manager.start(_state.port)
                self._send_json({"success": success, **_state.tunnel_manager.status()})
            except Exception as e:
                self._fail(e)
            return

        if path == "/api/tunnel/stop":
            try:
                _state.tunnel_manager.stop()
                self._send_json({"success": True})
            except Exception as e:
                self._fail(e)
            return

        if path == "/share-target":
            try:
                payload = _parse_share_target(
                    self.headers.get("Content-Type", ""), self._read_request_bytes()
                )
                token = _save_share_target(payload)
                self.send_response(303)
                self.send_header("Location", "/?share_target=" + quote(token))
                self.send_header("Content-Length", "0")
                self.end_headers()
            except Exception as e:
                self._fail(e)

        elif path == "/api/setup/complete":
            try:
                result = _api_setup_complete(self._read_body())
                token = result.pop("session_token", None)
                if token:
                    body_bytes = json.dumps(result).encode("utf-8")
                    self.send_response(200)
                    self.send_header("Content-Type", "application/json")
                    cookie = f"{SESSION_COOKIE_NAME}={token}; HttpOnly; SameSite=Strict; Path=/"
                    if _request_uses_https(self):
                        cookie += "; Secure"
                    self.send_header("Set-Cookie", cookie)
                    self.send_header("Content-Length", str(len(body_bytes)))
                    self.end_headers()
                    self.wfile.write(body_bytes)
                else:
                    self._send_json(result)
            except Exception as e:
                self._fail(e)

        elif path == "/api/connect/verify":
            try:
                self._send_json(_api_connect_verify(self._read_body()))
            except Exception as e:
                self._fail(e)

        elif path == "/api/connect/gemini":
            try:
                self._send_json(_api_connect_gemini(self._read_body()))
            except Exception as e:
                self._fail(e)

        elif path == "/api/connect":
            try:
                self._send_json(_api_connect_client(self._read_body()))
            except Exception as e:
                self._fail(e)

        elif path == "/api/connect/auto":
            try:
                self._send_json(_api_connect_auto())
            except Exception as e:
                self._fail(e)

        elif path == "/api/api-keys":
            try:
                self._send_json(_api_key_create(self._read_body()), 201)
            except Exception as e:
                self._fail(e)

        elif path == "/api/memories":
            try:
                body = self._read_body()

                item = _api_memory_add(body)
                if self._from_phone():
                    from .audit import record_capture

                    record_capture(_state.vault_dir, [item.get("id", "")])

                self._send_json(item, 201)

            except ValueError as e:
                self._fail(e)

        elif path == "/api/whatsapp/preview":
            try:
                self._send_json(
                    _api_whatsapp_preview(self._read_body(max_bytes=MAX_IMPORT_REQUEST_BODY_BYTES))
                )
            except Exception as e:
                self._fail(e)

        elif path == "/api/whatsapp/approve":
            try:
                self._send_json(
                    _api_whatsapp_approve(self._read_body(max_bytes=MAX_IMPORT_REQUEST_BODY_BYTES))
                )
            except Exception as e:
                self._fail(e)

        elif path == "/api/import/preview":
            try:
                self._send_json(
                    _api_import_preview(self._read_body(max_bytes=MAX_IMPORT_REQUEST_BODY_BYTES))
                )
            except Exception as e:
                self._fail(e)

        elif path == "/api/import":
            try:
                body = self._read_body(max_bytes=MAX_IMPORT_REQUEST_BODY_BYTES)

                result = _api_import(body)
                if self._from_phone(companion=body.get("source") == "EleSync companion"):
                    from .audit import record_capture

                    record_capture(_state.vault_dir, result.get("memory_ids") or [])

                self._send_json(result)

            except Exception as e:
                self._fail(e)

        elif path == "/api/projects":
            try:
                self._send_json(_project_api().create(self._read_body()), 201)
            except ValueError as e:
                self._fail(e)

        elif path == "/api/demo/step" and _state.demo_mode:
            try:
                from . import demo

                body = self._read_body()
                self._send_json(
                    demo.run_step(_get_store(), _state.vault_dir, str(body.get("step", "")))
                )
            except ValueError as e:
                self._fail(e)

        elif path == "/api/project-discovery/accept":
            try:
                self._send_json(_project_api().accept_discovered(self._read_body()), 201)
            except ValueError as e:
                self._fail(e)

        elif path.startswith("/api/projects/") and path.endswith("/access"):
            try:
                project_id = path[len("/api/projects/") : -len("/access")]
                self._send_json(_project_api().set_access(project_id, self._read_body()))
            except ValueError as e:
                self._fail(e)

        elif (
            path.startswith("/api/projects/")
            and "/documents/" in path
            and path.endswith("/complete")
        ):
            try:
                remainder = path[len("/api/projects/") : -len("/complete")]
                project_id, document_id = remainder.split("/documents/", 1)
                document = _project_api().complete_task(project_id, document_id)
                if document is None:
                    self._send_error(404, "open task not found")
                else:
                    self._send_json(document)
            except ValueError as e:
                self._fail(e)

        elif path.startswith("/api/projects/") and path.endswith("/documents"):
            try:
                project_id = path[len("/api/projects/") : -len("/documents")]
                self._send_json(_project_api().create_document(project_id, self._read_body()), 201)
            except ValueError as e:
                self._fail(e)

        elif path.startswith("/api/projects/") and path.endswith("/memories"):
            try:
                project_id = path[len("/api/projects/") : -len("/memories")]
                self._send_json(_project_api().add_memory(project_id, self._read_body()))
            except ValueError as e:
                self._fail(e)

        elif path == "/api/usb/install":
            try:
                body = self._read_body()

                result = _api_usb_install(body)

                self._send_json(result)

            except Exception as e:
                self._fail(e)

        elif path == "/api/backup":
            try:
                self._read_body()
                self._send_json(_api_backup_now(), 201)
            except Exception as e:
                self._fail(e)

        elif path == "/api/vault/switch":
            try:
                body = self._read_body()

                result = _api_vault_switch(body)

                self._send_json(result)

            except Exception as e:
                self._fail(e)

        elif path == "/api/conflicts/resolve":
            try:
                body = self._read_body()
                from . import service as _svc_mod

                svc = _svc_mod.MemoryService(_get_store())
                result = svc.resolve_conflict(
                    body.get("cluster_id", ""),
                    choose_id=body.get("choose_id"),
                    strategy=body.get("strategy"),
                )
                self._send_json({"result": result})
            except Exception as e:
                self._fail(e)

        else:
            # Unknown path: the body was never read, and replying over it makes
            # Windows reset the connection instead of delivering the 404.
            self._drain_request_body()
            self._send_error(404, "not found")

    @_with_store_lease
    def do_PUT(self):
        if self._refuse_foreign_host() or self._refuse_cross_site():
            return

        parsed = urlparse(self.path)

        path = parsed.path.rstrip("/")

        if self._is_public_api(path):
            self._serve_public_api(path, parsed.query)
            return

        username = self._require_auth()

        if username is None:
            return

        if not self._require_write_access(username):
            return

        if path.startswith("/api/memories/"):
            mem_id = path[len("/api/memories/") :]

            try:
                body = self._read_body()

                item = _api_memory_update(mem_id, body)

                if item is None:
                    self._send_error(404, "not found")

                else:
                    self._send_json(item)

            except Exception as e:
                self._fail(e)

        elif path.startswith("/api/projects/") and "/documents/" in path:
            remainder = path[len("/api/projects/") :]
            project_id, document_id = remainder.split("/documents/", 1)
            try:
                document = _project_api().update_document(
                    project_id, document_id, self._read_body()
                )
                if document is None:
                    self._send_error(404, "document not found")
                else:
                    self._send_json(document)
            except ValueError as e:
                self._fail(e)

        elif path.startswith("/api/projects/"):
            project_id = path[len("/api/projects/") :]
            try:
                project = _project_api().update(project_id, self._read_body())
                if project is None:
                    self._send_error(404, "not found")
                else:
                    self._send_json(project)
            except ValueError as e:
                self._fail(e)

        else:
            self._send_error(404, "not found")

    @_with_store_lease
    def do_DELETE(self):
        if self._refuse_foreign_host() or self._refuse_cross_site():
            return

        parsed = urlparse(self.path)

        path = parsed.path.rstrip("/")

        if path == "/mcp":
            self._proxy_mcp()
            return

        if self._is_public_api(path):
            self._serve_public_api(path, parsed.query)
            return

        username = self._require_auth()

        if username is None:
            return

        if not self._require_write_access(username):
            return

        if path.startswith("/api/api-keys/"):
            try:
                revoked = _api_keys_store().revoke(path[len("/api/api-keys/") :])
            except OSError as e:
                self._fail(e, status=500)
            else:
                if revoked:
                    self._send_json({"revoked": True})
                else:
                    self._send_error(404, "API key not found")

        elif path.startswith("/api/projects/") and path.endswith("/access"):
            project_id = path[len("/api/projects/") : -len("/access")]
            provider = parse_qs(parsed.query).get("provider", [""])[0]
            if _project_service().revoke_access(project_id, provider):
                self._send_json({"revoked": True})
            else:
                self._send_error(404, "access record not found")

        elif path.startswith("/api/memories/"):
            mem_id = path[len("/api/memories/") :]

            ok = _api_memory_delete(mem_id)

            if ok:
                self._send_json({"deleted": True})

            else:
                self._send_error(404, "not found")

        elif path.startswith("/api/projects/") and "/documents/" in path:
            remainder = path[len("/api/projects/") :]
            project_id, document_id = remainder.split("/documents/", 1)
            if _project_api().delete_document(project_id, document_id):
                self._send_json({"deleted": True})
            else:
                self._send_error(404, "document not found")

        elif path.startswith("/api/projects/") and "/memories/" in path:
            remainder = path[len("/api/projects/") :]
            project_id, memory_id = remainder.split("/memories/", 1)
            if _project_service().remove_memory(project_id, memory_id):
                self._send_json({"removed": True})
            else:
                self._send_error(404, "not found")

        elif path.startswith("/api/projects/"):
            project_id = path[len("/api/projects/") :]
            if _project_service().delete(project_id):
                self._send_json({"deleted": True, "memories_deleted": False})
            else:
                self._send_error(404, "not found")

        else:
            self._send_error(404, "not found")


# ---------------------------------------------------------------------------

# Entry point

# ---------------------------------------------------------------------------


def serve(
    vault_dir: Path | str | None = None,
    host: str = "127.0.0.1",
    port: int = 7477,
    open_browser: bool = True,
    lang: str = "en",
    tls_cert: str | None = None,
    tls_key: str | None = None,
    allow_insecure_network: bool = False,
    demo: bool = False,
) -> None:
    if bool(tls_cert) != bool(tls_key):
        raise ValueError("Both --tls-cert and --tls-key are required for HTTPS")
    is_loopback = host.strip().lower() in ("127.0.0.1", "localhost", "::1")
    if not is_loopback and not tls_cert and not allow_insecure_network:
        raise ValueError(
            "Refusing to expose the Web UI over unencrypted HTTP. "
            "Use --tls-cert/--tls-key, bind to 127.0.0.1 behind a TLS proxy, "
            "or explicitly pass --allow-insecure-network."
        )
    # Accept either str or Path (callers commonly pass Path)
    if vault_dir is not None and not isinstance(vault_dir, str):
        vault_dir = str(vault_dir)

    # Same UTF-8 stdout defence as cli.main — legacy Windows cp1252 can't encode

    # the emoji we print below, and would crash with UnicodeEncodeError.

    import contextlib

    for stream in (sys.stdout, sys.stderr):
        with contextlib.suppress(AttributeError, ValueError):
            stream.reconfigure(encoding="utf-8")  # type: ignore[union-attr]

    from . import crypto as _crypto
    from . import default_vault_dir
    from .i18n import _
    from .i18n import set_language as _set_lang
    from .store import MemoryStore

    _set_lang(lang)

    # Resolve the vault the same way the CLI does: explicit arg → $ELESYNC_DIR
    # → config.json → ~/EleSyncVault.  Without this, the dashboard would always
    # fall back to ~/EleSyncVault and ignore the vault the user picked during
    # onboarding or `ele vault set`.
    if vault_dir is None:
        cfg_path = (
            Path(os.environ.get("APPDATA", str(Path.home() / ".config")))
            / "EleSync"
            / "config.json"
        )
        if cfg_path.exists():
            try:
                cfg = json.loads(cfg_path.read_text(encoding="utf-8"))
                vault_dir = cfg.get("vault_dir")
            except (json.JSONDecodeError, OSError):
                pass

    resolved_vault_dir = vault_dir if isinstance(vault_dir, str) else None
    vault_path = Path(default_vault_dir(resolved_vault_dir)).expanduser()

    _state.vault_dir = str(vault_path)

    # Handle encrypted vault

    key = None

    if _crypto.available() and _crypto.is_encrypted(vault_path):
        passphrase = os.environ.pop("ELESYNC_PASSPHRASE", "")
        if not passphrase:
            import getpass

            passphrase = getpass.getpass("🔑  Vault passphrase: ")

        meta = _crypto.read_meta(vault_path)

        assert meta is not None

        key = _crypto.derive_key(
            passphrase,
            bytes.fromhex(meta["salt"])
            if isinstance(meta["salt"], str) and len(meta["salt"]) == 64
            else __import__("base64").b64decode(meta["salt"]),
            meta["opslimit"],
            meta["memlimit"],
        )

    embedder = None
    if os.environ.get("ELESYNC_SEMANTIC"):
        from . import embeddings as _embeddings

        embedder = _embeddings.load_embedder()
    _state.store = MemoryStore(vault_path, key=key, embedder=embedder)
    if demo:
        from .demo import stable_project_id

        _state.store.project_id_factory = stable_project_id

    from . import web_auth as _web_auth

    _state.user_mgr = _web_auth.UserManager(web_auth_dir(vault_path))

    _state.session_mgr = _web_auth.SessionManager(
        web_auth_dir(vault_path), require_existing_user=True
    )

    if not is_loopback and not _auth_enabled() and not allow_insecure_network:
        _state.store.close()
        _state.store = None
        raise ValueError(
            "Refusing to expose the Web UI without authentication. "
            "Run `ele web adduser <username>`, bind to 127.0.0.1, or explicitly "
            "pass --allow-insecure-network."
        )

    _state.port = port
    _state.demo_mode = demo
    server = BoundedHTTPServer((host, port), _Handler)

    # TLS: wrap the socket if cert + key were provided. This is the
    # self-hosted security layer — without it, session cookies travel in
    # cleartext, so anyone on the same network can steal a login. For
    # local-only use (127.0.0.1) TLS is unnecessary; direct network access
    # requires it unless the user explicitly selected the unsafe override.
    if tls_cert and tls_key:
        import ssl

        ctx = ssl.SSLContext(ssl.PROTOCOL_TLS_SERVER)
        ctx.load_cert_chain(tls_cert, tls_key)
        # Defer TLS handshakes to bounded workers; a silent TLS client must not
        # block the accept loop before other clients can connect.
        server.socket = ctx.wrap_socket(
            server.socket, server_side=True, do_handshake_on_connect=False
        )
        _state.tls_enabled = True
        url = f"https://{host}:{port}"
    else:
        _state.tls_enabled = False
        url = f"http://{host}:{port}"

    print(_("web_started", url))

    print(_("web_vault", _state.vault_dir))

    # Warn if binding to non-loopback without auth
    if not is_loopback and not _auth_enabled():
        print(
            "⚠️   WARNING: Web UI is accessible to the entire network without authentication.",
            file=sys.stderr,
        )
        print(
            "     Use `ele web adduser <username>` to enable login protection, or bind to 127.0.0.1",
            file=sys.stderr,
        )
    if not is_loopback and not (tls_cert and tls_key):
        print(
            "⚠️   WARNING: Web UI is served over plain HTTP — session cookies travel in cleartext.",
            file=sys.stderr,
        )
        print(
            "     Use --tls-cert/--tls-key or a reverse proxy with TLS to protect credentials.",
            file=sys.stderr,
        )

    if _auth_enabled():
        assert _state.user_mgr is not None  # guaranteed by _auth_enabled()
        users = _state.user_mgr.list_users()

        print(f"🔒  Login required — {len(users)} user(s) configured (ele web listusers)")

    print(_("web_stop"))

    if open_browser:
        # Use localhost for browser when binding to 0.0.0.0
        browser_url = url.replace("0.0.0.0", "127.0.0.1")
        threading.Timer(0.5, lambda: webbrowser.open(browser_url)).start()

    try:
        server.serve_forever()

    except KeyboardInterrupt:
        pass

    finally:
        server.server_close()
        _get_store().close()

        print(_("web_stopped"))


# ---------------------------------------------------------------------------

# Embedded single-page UI

# ---------------------------------------------------------------------------


from ._web_templates import _HTML, _LOGIN_HTML  # noqa: E402
