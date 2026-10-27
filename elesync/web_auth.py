"""Web authentication and user management.

Provides:
  - User creation/deletion with bcrypt-hashed passwords
  - Password verification
  - Role-based access control (admin, viewer/guest)
  - Session token generation and validation

Threat model - be clear-eyed. This protects credentials at rest (passwords are
bcrypt-hashed; session tokens are stored as SHA-256 hashes, never in plaintext)
and guards against basic file-corruption from concurrent writes within a single
process (an in-process lock plus atomic temp-file-then-rename writes). It does
NOT provide cross-process locking - if you run `ele web` and a separate script
both writing web_users.json at the same moment, last-write-wins, same as the
rest of EleSync's file-based storage. For anything exposed past localhost, put
a real reverse proxy with TLS in front of it; this module is a local-first
building block, not a hardened multi-tenant auth server.
"""

from __future__ import annotations

import hashlib
import json
import os
import secrets
import threading
from datetime import datetime, timedelta, timezone
from pathlib import Path

# Configurable bcrypt work factor (rounds). Default 12; can be lowered in
# CI/tests by setting the ELE_BCRYPT_ROUNDS environment variable, since a
# full 12-round hash on every test that touches a password adds up fast
# across dozens of tests.
BCRYPT_ROUNDS = int(os.environ.get("ELE_BCRYPT_ROUNDS", "12"))


def _get_bcrypt():
    """Import bcrypt lazily, with a friendly error if it's not installed.

    Matches the optional-dependency pattern used elsewhere in the codebase
    (see elesync/embeddings.py) - imported inside the function that needs
    it rather than as a module-level sentinel, which also keeps mypy happy
    (a module-level `bcrypt = None` fallback confuses its type inference
    once `import bcrypt` has already bound the name to a module type).
    """
    try:
        import bcrypt
    except ImportError as e:
        raise ImportError('bcrypt not installed. Install with: pip install "bcrypt>=4.0"') from e
    return bcrypt


def hash_password(password: str) -> str:
    """Hash a password using bcrypt. Returns the hash as a string."""
    if not password:
        raise ValueError("Password cannot be empty")
    bcrypt = _get_bcrypt()
    # bcrypt.hashpw returns bytes; decode to string for JSON storage
    return bcrypt.hashpw(password.encode(), bcrypt.gensalt(rounds=BCRYPT_ROUNDS)).decode()


def verify_password(password: str, hashed: str) -> bool:
    """Verify a password against a bcrypt hash."""
    if not password or not hashed:
        return False
    bcrypt = _get_bcrypt()
    try:
        return bcrypt.checkpw(password.encode(), hashed.encode())
    except (ValueError, TypeError):
        # Malformed hash (e.g. truncated file, wrong format) - treat as
        # "does not match" rather than crashing the caller.
        return False


def generate_session_token() -> str:
    """Generate a secure session token (URL-safe, ~256 bits)."""
    return secrets.token_urlsafe(32)


def _hash_token(token: str) -> str:
    """SHA-256 hash of a session token, for at-rest storage.

    Session tokens are high-entropy (256 bits) and single-use-per-session, so
    a fast hash is appropriate here (unlike passwords, which need bcrypt's
    deliberate slowness against offline brute-force). The goal is that
    reading web_sessions.json off disk does not hand out a live, reusable
    session the way storing the raw token would.
    """
    return hashlib.sha256(token.encode()).hexdigest()


def _atomic_write_json(path: Path, payload: dict) -> None:
    """Write JSON atomically: write to a temp file in the same directory,
    then os.replace() it over the target. os.replace is atomic on both
    POSIX and Windows, so a crash or concurrent read mid-write can never
    observe a truncated/corrupt file - it sees either the old version or
    the fully-written new one, never a half-written one.

    Also best-effort restricts the file to owner read/write (0o600) on
    platforms that support it - these files hold password and session
    hashes, so they shouldn't be world-readable on a shared machine.
    """
    path.parent.mkdir(parents=True, exist_ok=True)
    tmp_path = None
    try:
        tmp_path = path.with_suffix(path.suffix + f".tmp{os.getpid()}")
        tmp_path.write_text(json.dumps(payload, indent=2), encoding="utf-8")
        os.replace(tmp_path, path)
        try:
            os.chmod(path, 0o600)
        except OSError:
            # Non-fatal; Windows and some filesystems have different chmod
            # semantics, and this is defense-in-depth, not the primary
            # protection (that's the hashing itself).
            pass
    except OSError as e:
        if tmp_path is not None and tmp_path.exists():
            try:
                tmp_path.unlink()
            except OSError:
                pass
        raise RuntimeError(f"Failed to write {path.name}: {e}") from e


class UserManager:
    """Manage users, passwords, and roles for web auth."""

    def __init__(self, config_dir: str | Path):
        """Initialize user manager with config directory."""
        self.config_dir = Path(config_dir).expanduser()
        self.users_file = self.config_dir / "web_users.json"
        self.sessions_file = self.config_dir / "web_sessions.json"
        self._lock = threading.Lock()
        self._ensure_config_dir()
        self._load_users()

    def _ensure_config_dir(self) -> None:
        """Ensure config directory exists."""
        self.config_dir.mkdir(parents=True, exist_ok=True)

    def _load_users(self) -> None:
        """Load users from disk. Initialize empty if file doesn't exist."""
        if self.users_file.exists():
            try:
                data = json.loads(self.users_file.read_text(encoding="utf-8"))
                self.users = data.get("users", {})
            except (json.JSONDecodeError, OSError):
                self.users = {}
        else:
            self.users = {}

    def _save_users(self) -> None:
        """Save users to disk (atomic write). Caller must hold self._lock."""
        payload = {
            "users": self.users,
            "updated_at": datetime.now(timezone.utc).isoformat(),
        }
        _atomic_write_json(self.users_file, payload)

    def add_user(
        self, username: str, password: str, role: str = "admin", guest: bool = False
    ) -> None:
        """Add or update a user. Updating an existing username preserves
        their original created_at rather than resetting it.

        Args:
            username: Username (alphanumeric + underscore + dash)
            password: Plain-text password (will be hashed)
            role: "admin" (default) or "viewer" (read-only)
            guest: If True, enforce read-only access
        """
        if not username or not isinstance(username, str):
            raise ValueError("Username must be a non-empty string")
        if not password:
            raise ValueError("Password cannot be empty")
        if len(password) < 8:
            raise ValueError("Password must be at least 8 characters")

        # Validate username (alphanumeric + underscore + dash)
        if not all(c.isalnum() or c in "_-" for c in username):
            raise ValueError("Username must contain only alphanumeric, _, and - chars")

        # If guest=True, force role to viewer
        if guest:
            role = "viewer"
        if role not in ("admin", "viewer"):
            raise ValueError("role must be 'admin' or 'viewer'")

        hashed = hash_password(password)
        now_iso = datetime.now(timezone.utc).isoformat()
        with self._lock:
            existing = self.users.get(username)
            created_at = existing.get("created_at") if existing else now_iso
            self.users[username] = {
                "password_hash": hashed,
                "role": role,
                "guest": guest,
                "created_at": created_at,
            }
            self._save_users()

    def remove_user(self, username: str) -> None:
        """Remove a user."""
        with self._lock:
            if username in self.users:
                del self.users[username]
                self._save_users()
            else:
                raise ValueError(f"User '{username}' not found")

    def authenticate(self, username: str, password: str) -> bool:
        """Check if username and password are correct. Reads the user's
        hash under the lock, then verifies outside it - bcrypt takes
        ~100ms by design, and holding the lock for that long would let one
        slow auth attempt block every other read/write on this manager."""
        with self._lock:
            user = self.users.get(username)
        if user is None:
            # Still run a bcrypt comparison against a dummy hash so that
            # "unknown username" and "wrong password" take roughly the same
            # amount of time - avoids leaking which usernames exist via a
            # timing side channel.
            verify_password(password or "x", _DUMMY_HASH)
            return False
        return verify_password(password, user["password_hash"])

    def get_user(self, username: str) -> dict | None:
        """Get user info (without password hash)."""
        with self._lock:
            user = self.users.get(username)
            if user is not None:
                user = user.copy()
        if user is None:
            return None
        user.pop("password_hash", None)
        return user

    def list_users(self) -> list[dict]:
        """List all users (without password hashes)."""
        with self._lock:
            items = list(self.users.items())
        return [
            {
                "username": name,
                "role": user.get("role", "admin"),
                "guest": user.get("guest", False),
                "created_at": user.get("created_at"),
            }
            for name, user in items
        ]

    def is_admin(self, username: str) -> bool:
        """Check if user is an admin. Always returns bool, never None."""
        with self._lock:
            user = self.users.get(username)
        return bool(user) and user.get("role") == "admin"

    def is_guest_or_viewer(self, username: str) -> bool:
        """Check if user is read-only (guest or viewer role). Always
        returns bool, never None."""
        with self._lock:
            user = self.users.get(username)
        return bool(user) and (bool(user.get("guest")) or user.get("role") == "viewer")


# A bcrypt hash of an arbitrary fixed password, used only to burn a
# comparable amount of CPU time on unknown-username lookups (see
# UserManager.authenticate). Never used to authenticate anything for real.
_DUMMY_HASH = "$2b$12$CwTycUXWue0Thq9StjUM0uJ8k9jbFhAmDLGwvZ1kDpVYqbnDwYGdC"


class SessionManager:
    """Manage session tokens for web auth.

    Session tokens are stored on disk as SHA-256 hashes, not in plaintext -
    reading web_sessions.json does not hand out a usable session token.
    """

    def __init__(
        self,
        config_dir: str | Path,
        ttl_minutes: int = 1440,
        require_existing_user: bool = False,
    ):
        """Initialize session manager.

        Args:
            config_dir: Directory to store session data
            ttl_minutes: Session time-to-live in minutes (default: 24 hours)
            require_existing_user: If True, create_session() raises unless
                the username exists in web_users.json. Defaults to False to
                keep SessionManager usable on its own, independent of
                UserManager (e.g. in tests) - callers that do want the
                guarantee that every session maps to a real, current user
                should opt in explicitly.
        """
        self.config_dir = Path(config_dir).expanduser()
        self.sessions_file = self.config_dir / "web_sessions.json"
        self.ttl = timedelta(minutes=ttl_minutes)
        self._lock = threading.Lock()
        self.require_existing_user = require_existing_user
        self._load_sessions()

    def _load_sessions(self) -> None:
        """Load sessions from disk. Initialize empty if file doesn't exist."""
        if self.sessions_file.exists():
            try:
                data = json.loads(self.sessions_file.read_text(encoding="utf-8"))
                self.sessions = data.get("sessions", {})
            except (json.JSONDecodeError, OSError):
                self.sessions = {}
        else:
            self.sessions = {}
        self._cleanup_expired()

    def _save_sessions(self) -> None:
        """Save sessions to disk (atomic write). Caller must hold self._lock."""
        payload = {
            "sessions": self.sessions,
            "updated_at": datetime.now(timezone.utc).isoformat(),
        }
        _atomic_write_json(self.sessions_file, payload)

    def _cleanup_expired(self) -> None:
        """Remove expired sessions."""
        now = datetime.now(timezone.utc)
        expired = [
            token_hash
            for token_hash, data in self.sessions.items()
            if datetime.fromisoformat(data["expires_at"]) < now
        ]
        for token_hash in expired:
            del self.sessions[token_hash]

    def _username_exists(self, username: str) -> bool:
        """Check web_users.json directly for a username. Used by
        create_session() when require_existing_user=True - deliberately
        reads the file rather than taking a UserManager dependency, so
        SessionManager stays usable standalone."""
        users_file = self.config_dir / "web_users.json"
        if not users_file.exists():
            return False
        try:
            data = json.loads(users_file.read_text(encoding="utf-8"))
        except (json.JSONDecodeError, OSError):
            return False
        return username in data.get("users", {})

    def create_session(self, username: str) -> str:
        """Create a new session for a user. Returns the raw session token -
        this is the only time the raw token exists; only its hash is
        persisted.

        Raises ValueError if require_existing_user is set and username
        isn't a real, current entry in web_users.json.
        """
        if self.require_existing_user and not self._username_exists(username):
            raise ValueError(f"User '{username}' not found")

        token = generate_session_token()
        expires_at = datetime.now(timezone.utc) + self.ttl
        with self._lock:
            self.sessions[_hash_token(token)] = {
                "username": username,
                "created_at": datetime.now(timezone.utc).isoformat(),
                "expires_at": expires_at.isoformat(),
            }
            self._save_sessions()
        return token

    def validate_session(self, token: str) -> str | None:
        """Validate a session token. Returns username if valid, None otherwise."""
        if not token:
            return None
        with self._lock:
            self._cleanup_expired()
            token_hash = _hash_token(token)
            session = self.sessions.get(token_hash)
            if session is None:
                return None
            expires_at = datetime.fromisoformat(session["expires_at"])
            if expires_at > datetime.now(timezone.utc):
                return session["username"]
            return None

    def revoke_session(self, token: str) -> None:
        """Revoke a session token."""
        if not token:
            return
        with self._lock:
            token_hash = _hash_token(token)
            if token_hash in self.sessions:
                del self.sessions[token_hash]
                self._save_sessions()
