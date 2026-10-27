"""Remote backup destinations via rclone (#93).

rclone is an external binary (https://rclone.org) that speaks S3, B2,
GCS, Drive, SFTP, and many other providers through one CLI. EleSync
invokes it as a subprocess — no cloud SDK in the dependency tree, no
per-provider auth code to maintain. If rclone isn't installed, the
commands fail with a clear message; the base install stays
cloud-SDK-free.

The remote is identified by an rclone-style path: ``remote:bucket/prefix``
or ``s3:bucket/prefix``. rclone handles all auth and transfer details;
EleSync just needs to push, pull, and list ``.eleb`` files at that path.

Design:
- **push** copies a local backup to the remote, then syncs the manifest
  so the remote view stays consistent.
- **pull** fetches a named backup (or the latest) from the remote into
  the local managed directory, ready for ``ele restore``.
- **list** reads the remote manifest (or falls back to listing ``.eleb``
  files at the remote path if the manifest is missing).
- **prune** applies ``backup.keep`` retention on the remote, deleting
  the oldest backups beyond the keep count — same create-then-prune
  discipline as local retention, with the just-pushed backup protected.

This module holds no i18n and does no printing; the CLI owns presentation.
"""

from __future__ import annotations

import json
import shutil
import subprocess
from pathlib import Path

from . import backups

#: The rclone binary name. Overridable in tests by patching this or
#: ``_rclone_path``.
RCLONE_BIN = "rclone"


class RemoteError(Exception):
    """A remote operation failed — rclone missing, remote unreachable,
    or a subprocess returned non-zero."""


def _rclone_path() -> str:
    """Locate the rclone binary, or raise RemoteError if it's not installed."""
    path = shutil.which(RCLONE_BIN)
    if path is None:
        raise RemoteError(
            "rclone not found on PATH. Install it from https://rclone.org/install/ "
            "and configure a remote with `rclone config`."
        )
    return path


def _run(args: list[str], *, check: bool = True) -> subprocess.CompletedProcess:
    """Run an rclone command, capturing output. Raises RemoteError on failure."""
    bin_path = _rclone_path()
    full = [bin_path, *args]
    try:
        result = subprocess.run(
            full,
            capture_output=True,
            text=True,
            check=False,
        )
    except OSError as e:
        raise RemoteError(f"failed to run rclone: {e}") from e
    if check and result.returncode != 0:
        stderr = result.stderr.strip()
        raise RemoteError(
            f"rclone failed (exit {result.returncode}): {stderr or 'no error output'}"
        )
    return result


def _remote_manifest_path(remote: str) -> str:
    """The rclone path to the manifest file."""
    return f"{remote.rstrip('/')}/{backups.MANIFEST_NAME}"


def _backup_name(value: object) -> str:
    """Accept only a bare archive filename, on both POSIX and Windows.

    Remote manifests are untrusted. In addition to traversal separators,
    reject drive/stream syntax and control characters before handing a name
    to rclone or joining it to a local download directory.
    """
    if (
        not isinstance(value, str)
        or not value
        or value.startswith(".")
        or not value.endswith(backups.SUFFIX)
        or any(character in value for character in "/\\:")
        or any(ord(character) < 32 or ord(character) == 127 for character in value)
    ):
        raise RemoteError(f"invalid backup name: {value!r}")
    return value


def push(local_path: Path, remote: str) -> str:
    """Push a local backup file to the remote destination.

    Copies the file, then uploads the local manifest (so the remote
    view stays consistent). Returns the rclone destination path of the
    backup.

    ``local_path`` must exist; the caller (CLI) is responsible for
    having just created it via ``ele backup``.
    """
    local_path = Path(local_path)
    if not local_path.is_file():
        raise RemoteError(f"backup file not found: {local_path}")

    dest = f"{remote.rstrip('/')}/{local_path.name}"
    _run(["copyto", str(local_path), dest])

    # Sync the manifest so the remote listing reflects the new backup.
    local_manifest = backups.manifest_path(local_path.parent)
    if local_manifest.is_file():
        _run(["copyto", str(local_manifest), _remote_manifest_path(remote)])

    return dest


def pull(remote: str, local_dir: Path, name: str | None = None) -> Path:
    """Pull a backup from the remote into the local directory.

    If ``name`` is given, pulls that specific file. Otherwise pulls the
    newest ``.eleb`` at the remote path (by listing and sorting by name,
    which works because the default backup name is timestamped).

    Returns the local path of the downloaded file.
    """
    local_dir = Path(local_dir)
    local_dir.mkdir(parents=True, exist_ok=True)

    if name is None:
        entries = list_remote(remote)
        if not entries:
            raise RemoteError(f"no backups found at {remote}")
        # entries are newest-first; pull the first one.
        name = entries[0]["filename"]
        if not name:
            raise RemoteError("remote listing returned an entry with no filename")

    name = _backup_name(name)

    remote_file = f"{remote.rstrip('/')}/{name}"
    local_path = local_dir / name
    _run(["copyto", remote_file, str(local_path)])
    return local_path


def list_remote(remote: str) -> list[dict]:
    """List backups at the remote path, newest first.

    Reads the remote manifest if available; falls back to listing
    ``.eleb`` files and reading their headers (via a temporary download)
    if the manifest is missing — same graceful-degradation principle as
    the local listing.
    """
    # Try the manifest first.
    result = _run(["cat", _remote_manifest_path(remote)], check=False)
    if result.returncode == 0 and result.stdout.strip():
        try:
            data = json.loads(result.stdout)
            if not isinstance(data, dict) or not isinstance(data.get("backups"), list):
                raise ValueError("manifest is not a backup manifest")
            entries = data.get("backups", [])
            for entry in entries:
                if not isinstance(entry, dict):
                    raise ValueError("manifest entry is not an object")
                _backup_name(entry.get("filename"))
            # Sort newest-first, same as local entries().
            entries.sort(
                key=lambda e: (e.get("created_at") or "", e.get("filename") or ""),
                reverse=True,
            )
            return entries
        except (ValueError, TypeError):
            pass  # fall through to listing files

    # Fall back to listing .eleb files at the remote path.
    result = _run(["lsf", "--files-only", remote], check=False)
    if result.returncode != 0:
        raise RemoteError(
            f"failed to list remote {remote}: {result.stderr.strip() or 'unknown error'}"
        )
    names = [
        _backup_name(line.strip())
        for line in result.stdout.splitlines()
        if line.strip().endswith(backups.SUFFIX)
    ]
    # Without the manifest we can only return filenames, not full metadata.
    # Sort newest-first by filename (timestamped names sort chronologically).
    names.sort(reverse=True)
    return [{"filename": n, "status": backups.UNRECORDED} for n in names]


def prune(remote: str, keep: int, protect: str | None = None) -> list[str]:
    """Apply retention on the remote: keep the N most recent backups,
    delete the rest. Returns the list of deleted filenames.

    ``protect`` is a filename that must never be deleted (the backup
    just pushed). Same create-then-prune discipline as local retention.
    """
    if keep <= 0:
        return []

    entries = list_remote(remote)
    # Validate the entire plan before the first deletion, even when a listing
    # adapter supplies the entries directly. A bad late entry must not leave a
    # partially pruned backup set, and protected/retained entries must be safe.
    names = [_backup_name(entry.get("filename")) for entry in entries]
    # Keep the N newest; the rest are candidates for deletion.
    to_delete = names[keep:]
    deleted: list[str] = []
    for name in to_delete:
        if name == protect:
            continue
        remote_file = f"{remote.rstrip('/')}/{name}"
        result = _run(["deletefile", remote_file], check=False)
        if result.returncode == 0:
            deleted.append(name)
    return deleted


def is_available() -> bool:
    """True if rclone is installed and on PATH. Cheap — no subprocess run."""
    return shutil.which(RCLONE_BIN) is not None
