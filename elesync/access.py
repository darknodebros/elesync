"""Centralized access guards for project-bound service operations."""

from __future__ import annotations

import threading
import time

# Project IDs are 32-char lowercase hex (uuid4().hex). Callers may pass a
# short prefix (first 8+ hex chars) instead of the full id; the guard treats
# a short prefix as matching the full id only when both sides look like hex
# ids and the shorter side is at least this long — enough entropy that a
# collision with an unrelated project is negligible, and short enough that
# non-id strings like "proj-1" or "proj123_secret" can never satisfy it.
_MIN_SHORT_ID_LEN = 8


def _is_hex_id(value: str) -> bool:
    return len(value) >= _MIN_SHORT_ID_LEN and all(c in "0123456789abcdef" for c in value)


def _project_ids_match(assigned: str, target: str) -> bool:
    if assigned == target:
        return True
    if not (_is_hex_id(assigned) and _is_hex_id(target)):
        return False
    short, long = (assigned, target) if len(assigned) <= len(target) else (target, assigned)
    return long.startswith(short)


# A revoked assistant usually retries several tools in a row. Every attempt is
# still refused, but one ACCESS_DENIED per assistant and project per window is
# enough for the owner to see it, instead of a burst of identical entries and
# "blocked" notifications that push real activity out of view.
DENIAL_LOG_WINDOW_SECONDS = 60.0
_last_denial_logged: dict[tuple, float] = {}
_denial_lock = threading.Lock()


def _should_log_denial(audit, project_id: str, provider: str) -> bool:
    key = (str(getattr(audit, "log_file", "") or ""), project_id, provider)
    now = time.monotonic()
    with _denial_lock:
        last = _last_denial_logged.get(key)
        if last is not None and now - last < DENIAL_LOG_WINDOW_SECONDS:
            return False
        _last_denial_logged[key] = now
        return True


def connection_permissions(
    store, project_id: str, provider: str, *, audit=None, client_label: str = ""
) -> dict:
    """Permissions a project connection for `provider` holds right now.

    Raises PermissionError when the dashboard grant is missing or revoked, and
    records the refusal in the audit log (at most once per window per assistant
    and project) so the owner can see the attempt."""
    row = next((r for r in store.project_access(project_id) if r["provider"] == provider), None)
    if row is None or row["status"] != "active":
        if audit is not None and _should_log_denial(audit, project_id, provider):
            audit.log(
                "ACCESS_DENIED",
                client=client_label,
                detail=f"project={project_id[:8]} provider={provider}",
            )
        raise PermissionError("This project connection is no longer active.")
    return {
        "can_read": bool(row["can_read"]),
        "can_write": bool(row["can_write"]),
        "can_read_conversations": bool(row["can_read_conversations"]),
    }


class AccessGuard:
    def __init__(
        self,
        project_id: str | None = None,
        readonly: bool = False,
        project_read: bool = True,
        project_write: bool = True,
        project_read_conversations: bool = True,
    ) -> None:
        self.project_id = project_id.strip() if project_id else None
        self.readonly = readonly
        self.project_read = project_read
        self.project_write = project_write
        self.project_read_conversations = project_read_conversations

    def require_project(self, project_id: str) -> None:
        if self.project_id and not _project_ids_match(self.project_id, project_id):
            raise ValueError("This connection is limited to its assigned project.")

    def require_read(self, project_id: str | None = None) -> None:
        if project_id is not None:
            self.require_project(project_id)
        if self.project_id and not self.project_read:
            raise PermissionError("This project connection does not have read access.")

    def require_write(self, project_id: str | None = None) -> None:
        if project_id is not None:
            self.require_project(project_id)
        if self.readonly:
            raise PermissionError(
                "This client has read-only access — write operations are disabled."
            )
        if self.project_id and not self.project_write:
            raise PermissionError("This project connection does not have write access.")
