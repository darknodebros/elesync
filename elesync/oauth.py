"""Small provider-independent OAuth 2.1/PKCE store for project connections.

Tokens are stored only as SHA-256 hashes with project and permission metadata.
Provider passwords never enter EleSync.
"""

from __future__ import annotations

import base64
import hashlib
import json
import os
import secrets
import threading
import time
from pathlib import Path

OAUTH_FILE = ".oauth.json"
CODE_TTL = 300
TOKEN_TTL = 3600
REFRESH_TOKEN_TTL = 30 * 24 * 3600
MAX_REGISTERED_CLIENTS = 128
MAX_REGISTRATIONS_PER_MINUTE = 30
MAX_REDIRECT_URIS = 8
MAX_REDIRECT_URI_CHARS = 2048


class ClientRegistrationRateLimit(ValueError):
    """The vault's public registration endpoint has received too many requests."""


_OAUTH_LOCK = threading.RLock()


def _path(vault_dir: str | Path) -> Path:
    return Path(vault_dir).expanduser() / OAUTH_FILE


def _empty_store() -> dict:
    return {"clients": {}, "codes": {}, "tokens": {}, "refresh_tokens": {}}


def _normalise(data: object) -> dict:
    if not isinstance(data, dict):
        return _empty_store()
    for key in ("clients", "codes", "tokens", "refresh_tokens"):
        if not isinstance(data.get(key), dict):
            data[key] = {}
    return data


def _load_unlocked(vault_dir: str | Path) -> dict:
    path = _path(vault_dir)
    if not path.exists():
        return _empty_store()
    try:
        return _normalise(json.loads(path.read_text(encoding="utf-8")))
    except (OSError, json.JSONDecodeError):
        return _empty_store()


def _load(vault_dir: str | Path) -> dict:
    with _OAUTH_LOCK:
        return _load_unlocked(vault_dir)


def _save_unlocked(vault_dir: str | Path, data: dict) -> None:
    path = _path(vault_dir)
    temporary = path.with_name(f".{path.name}.{secrets.token_hex(8)}.tmp")
    try:
        temporary.write_text(json.dumps(_normalise(data), indent=2) + "\n", encoding="utf-8")
        try:
            temporary.chmod(0o600)
        except OSError:
            pass
        os.replace(temporary, path)
    finally:
        try:
            temporary.unlink()
        except FileNotFoundError:
            pass


def _save(vault_dir: str | Path, data: dict) -> None:
    with _OAUTH_LOCK:
        _save_unlocked(vault_dir, data)


def _hash(value: str) -> str:
    return hashlib.sha256(value.encode("utf-8")).hexdigest()


def _pkce_challenge(verifier: str) -> str:
    digest = hashlib.sha256(verifier.encode("ascii")).digest()
    return base64.urlsafe_b64encode(digest).rstrip(b"=").decode("ascii")


def _prune(data: dict, now: float | None = None) -> bool:
    now = time.time() if now is None else now
    changed = False
    for bucket in ("codes", "tokens", "refresh_tokens"):
        records = data.setdefault(bucket, {})
        expired = [key for key, value in records.items() if value.get("expires_at", 0) < now]
        for key in expired:
            records.pop(key, None)
            changed = True
    return changed


def register_client(vault_dir: str | Path, name: str, redirect_uris: list[str]) -> dict:
    if not isinstance(redirect_uris, list) or not 1 <= len(redirect_uris) <= MAX_REDIRECT_URIS:
        raise ValueError(f"provide between 1 and {MAX_REDIRECT_URIS} redirect URIs")
    # RFC 6819 §5.2.4: a redirect_uri with a fragment lets an attacker append
    # code/state after the callback and slip past strict-equality matching by
    # exploiting how browsers merge fragments. Reject them at registration so
    # the equality check in _oauth_validate_client_request is enough.
    for uri in redirect_uris:
        if not isinstance(uri, str) or not uri or len(uri) > MAX_REDIRECT_URI_CHARS:
            raise ValueError("redirect URI is empty, invalid, or too long")
        if "#" in uri:
            raise ValueError("redirect_uri must not contain a fragment (#…)")
    with _OAUTH_LOCK:
        data = _load_unlocked(vault_dir)
        if len(data["clients"]) >= MAX_REGISTERED_CLIENTS:
            raise ValueError("OAuth client registration capacity reached")
        now = time.time()
        recent = sum(record.get("created_at", 0) > now - 60 for record in data["clients"].values())
        if recent >= MAX_REGISTRATIONS_PER_MINUTE:
            raise ClientRegistrationRateLimit("Too many client registrations. Retry in 60 seconds.")
        client_id = "elesync_" + secrets.token_urlsafe(18)
        data["clients"][client_id] = {
            "name": str(name or "MCP client")[:160],
            "redirect_uris": redirect_uris,
            "created_at": int(now),
        }
        _save_unlocked(vault_dir, data)
    return {
        "client_id": client_id,
        "client_name": name or "MCP client",
        "token_endpoint_auth_method": "none",
    }


def get_client(vault_dir: str | Path, client_id: str) -> dict | None:
    with _OAUTH_LOCK:
        return _load_unlocked(vault_dir)["clients"].get(client_id)


def create_code(
    vault_dir: str | Path,
    client_id: str,
    redirect_uri: str,
    code_challenge: str,
    project_id: str,
    permissions: dict,
    provider: str = "",
) -> str:
    with _OAUTH_LOCK:
        data = _load_unlocked(vault_dir)
        _prune(data)
        code = secrets.token_urlsafe(32)
        data["codes"][_hash(code)] = {
            "client_id": client_id,
            "redirect_uri": redirect_uri,
            "code_challenge": code_challenge,
            "project_id": project_id,
            "provider": provider,
            "permissions": permissions,
            "expires_at": time.time() + CODE_TTL,
        }
        _save_unlocked(vault_dir, data)
        return code


def _issue_tokens(data: dict, record: dict) -> dict:
    now = time.time()
    authorization_id = record.get("authorization_id") or secrets.token_urlsafe(18)
    access_token = secrets.token_urlsafe(32)
    refresh_token = secrets.token_urlsafe(40)
    common = {
        "authorization_id": authorization_id,
        "client_id": record["client_id"],
        "project_id": record["project_id"],
        "provider": record.get("provider", ""),
        "permissions": record["permissions"],
        "created_at": int(now),
    }
    data["tokens"][_hash(access_token)] = {**common, "expires_at": now + TOKEN_TTL}
    data["refresh_tokens"][_hash(refresh_token)] = {
        **common,
        "expires_at": now + REFRESH_TOKEN_TTL,
    }
    return {
        "access_token": access_token,
        "refresh_token": refresh_token,
        "token_type": "Bearer",
        "expires_in": TOKEN_TTL,
        "refresh_token_expires_in": REFRESH_TOKEN_TTL,
        "scope": " ".join(key for key, value in record["permissions"].items() if value),
    }


def exchange_code(
    vault_dir: str | Path, code: str, client_id: str, redirect_uri: str, verifier: str
) -> dict:
    with _OAUTH_LOCK:
        data = _load_unlocked(vault_dir)
        _prune(data)
        key = _hash(code)
        record = data["codes"].get(key)
        if not record:
            raise ValueError("authorization code expired or invalid")
        if record["client_id"] != client_id or record["redirect_uri"] != redirect_uri:
            raise ValueError("authorization request does not match")
        if _pkce_challenge(verifier) != record["code_challenge"]:
            raise ValueError("PKCE verification failed")
        data["codes"].pop(key, None)
        response = _issue_tokens(data, record)
        _save_unlocked(vault_dir, data)
        return response


def refresh_access_token(vault_dir: str | Path, refresh_token: str, client_id: str) -> dict:
    """Rotate one refresh token and issue a fresh access/refresh token pair."""
    with _OAUTH_LOCK:
        data = _load_unlocked(vault_dir)
        _prune(data)
        key = _hash(refresh_token)
        record = data["refresh_tokens"].get(key)
        if not record:
            raise ValueError("refresh token expired or invalid")
        if record["client_id"] != client_id:
            raise ValueError("refresh token does not match this client")
        data["refresh_tokens"].pop(key, None)
        response = _issue_tokens(data, record)
        _save_unlocked(vault_dir, data)
        return response


def validate_token(vault_dir: str | Path, token: str) -> dict | None:
    with _OAUTH_LOCK:
        data = _load_unlocked(vault_dir)
        changed = _prune(data)
        record = data["tokens"].get(_hash(token))
        if changed:
            _save_unlocked(vault_dir, data)
        return record


def authorized_providers(vault_dir: str | Path) -> set[str]:
    """Providers that currently hold a live access or refresh token."""
    with _OAUTH_LOCK:
        data = _load_unlocked(vault_dir)
        changed = _prune(data)
        if changed:
            _save_unlocked(vault_dir, data)
        return {
            str(record.get("provider") or "").strip().lower()
            for bucket in ("tokens", "refresh_tokens")
            for record in data[bucket].values()
            if record.get("provider")
        }


def has_active_authorization(vault_dir: str | Path) -> bool:
    with _OAUTH_LOCK:
        data = _load_unlocked(vault_dir)
        changed = _prune(data)
        if changed:
            _save_unlocked(vault_dir, data)
        return bool(data["tokens"] or data["refresh_tokens"])


def revoke_token(vault_dir: str | Path, token: str) -> bool:
    with _OAUTH_LOCK:
        data = _load_unlocked(vault_dir)
        key = _hash(token)
        record = data["tokens"].pop(key, None)
        if record is None:
            record = data["refresh_tokens"].pop(key, None)
        if record is None:
            return False
        authorization_id = record.get("authorization_id")
        if authorization_id:
            for bucket in ("tokens", "refresh_tokens"):
                for stored_key, stored in list(data[bucket].items()):
                    if stored.get("authorization_id") == authorization_id:
                        data[bucket].pop(stored_key, None)
        _save_unlocked(vault_dir, data)
        return True


def revoke_project_authorizations(vault_dir: str | Path, project_id: str, provider: str) -> int:
    """Invalidate pending and issued authorization for one project provider."""
    project_id = project_id.strip()
    provider = provider.strip().lower()
    removed = 0
    with _OAUTH_LOCK:
        data = _load_unlocked(vault_dir)
        for bucket in ("codes", "tokens", "refresh_tokens"):
            for key, record in list(data[bucket].items()):
                if (
                    record.get("project_id") == project_id
                    and str(record.get("provider", "")).strip().lower() == provider
                ):
                    data[bucket].pop(key, None)
                    removed += 1
        if removed:
            _save_unlocked(vault_dir, data)
    return removed


def restrict_project_authorizations(
    vault_dir: str | Path, project_id: str, provider: str, permissions: dict
) -> None:
    """Apply grant reductions to pending codes and existing token families.

    Never expand a previous delegation when a dashboard grant is increased.
    Updating refresh records also prevents rotation from restoring old rights.
    """
    provider = provider.strip().lower()
    with _OAUTH_LOCK:
        data = _load_unlocked(vault_dir)
        changed = False
        for bucket in ("codes", "tokens", "refresh_tokens"):
            for record in data[bucket].values():
                if (
                    record.get("project_id") != project_id
                    or str(record.get("provider", "")).strip().lower() != provider
                ):
                    continue
                previous = record.get("permissions", {})
                reduced = {
                    name: bool(previous.get(name, False)) and bool(allowed)
                    for name, allowed in permissions.items()
                }
                if previous != reduced:
                    record["permissions"] = reduced
                    changed = True
        if changed:
            _save_unlocked(vault_dir, data)
