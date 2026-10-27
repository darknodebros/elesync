"""Local backup manifest, listing, and retention (#92).

`ele backup` (#91) writes a self-describing `.eleb` archive. This module gives those
archives a home, a record, and a retention policy:

    <backup dir>/
        elesync-backup-20260718-152430.eleb
        elesync-backup-20260719-090001.eleb
        elesync-backups.json            <- the manifest

The manifest records, per backup: filename, creation time, size, format version,
encryption state, and a SHA-256 checksum. Everything except the checksum is already
readable from the archive's plaintext header, so a manifest is a *cache plus an
integrity witness* — never the sole source of truth. That asymmetry drives the whole
design: if the manifest is missing, stale, or corrupt, listing degrades to reading
headers off disk instead of failing, and the only capability actually lost is
tamper detection for the entries that went missing.

**Retention is create-then-prune.** `prune()` is called only after a new backup and
its manifest update have both succeeded, and the newly written file is passed as
`protect` so it can never be the thing deleted — no window exists in which retention
has removed an old backup without a confirmed-good new one on disk.

Pruning considers **every `*.eleb` in the directory**, not just manifest entries, so
a backup the manifest never learned about (copied in by hand, or written while the
manifest was unwritable) still counts against `keep` instead of accumulating forever.
Consequence worth stating plainly: this directory is EleSync's to manage. That is why
`prune()` is confined to the *managed* backup directory and never runs against an
explicit `ele backup /some/path.eleb` destination — such a backup is recorded in that
directory's manifest so `list` can see it, but nothing there is ever deleted.

This module holds no i18n and does no printing; the CLI owns presentation.
"""

from __future__ import annotations

import contextlib
import hashlib
import json
import os
import tempfile
from datetime import datetime, timezone
from pathlib import Path

from . import archive

MANIFEST_NAME = "elesync-backups.json"
MANIFEST_VERSION = 1
SUFFIX = ".eleb"

#: Status values on a listed entry.
OK = "ok"  # on disk and recorded in the manifest
UNRECORDED = "unrecorded"  # on disk, not in the manifest (header read directly)
MISSING = "missing"  # in the manifest, no longer on disk


def default_dir(vault) -> Path:
    """The managed backup directory for a vault: ``<vault>/backups``."""
    return Path(vault).expanduser() / "backups"


def resolve_dir(vault, config: dict | None = None) -> Path:
    """Resolve the managed backup directory.

    Priority (mirrors ``auth.get_configured_token``): ``$ELESYNC_BACKUP_DIR``, then
    ``config["backup"]["dir"]``, then ``<vault>/backups``.
    """
    env = os.environ.get("ELESYNC_BACKUP_DIR")
    if env:
        return Path(env).expanduser()
    if config:
        configured = config.get("backup", {}).get("dir")
        if configured:
            return Path(configured).expanduser()
    return default_dir(vault)


def resolve_keep(config: dict | None = None) -> int | None:
    """Resolve the retention count (`backup.keep`): keep the N most recent backups.

    ``$ELESYNC_BACKUP_KEEP`` wins over ``config["backup"]["keep"]``. Returns ``None``
    when retention is off — which includes an unparseable or non-positive value,
    because the safe reading of "keep=0" or "keep=nonsense" is *don't delete
    anything*, not *delete everything*.
    """
    raw = os.environ.get("ELESYNC_BACKUP_KEEP")
    if raw is None and config:
        raw = config.get("backup", {}).get("keep")
    if raw is None:
        return None
    try:
        keep = int(raw)
    except (TypeError, ValueError):
        return None
    return keep if keep > 0 else None


def checksum(path) -> str:
    """SHA-256 of the whole file, read in chunks so a large archive doesn't have to
    fit in memory."""
    h = hashlib.sha256()
    with open(path, "rb") as fh:
        for chunk in iter(lambda: fh.read(1024 * 1024), b""):
            h.update(chunk)
    return h.hexdigest()


def manifest_path(directory) -> Path:
    return Path(directory) / MANIFEST_NAME


def describe(path) -> dict:
    """Build a manifest entry for an archive: header metadata + size + checksum.

    Raises ``archive.ArchiveError`` if the file isn't a readable archive envelope —
    callers record only what they just wrote, so that would be a real failure.
    """
    p = Path(path)
    header = archive.header_of(p)
    return {
        "filename": p.name,
        "created_at": header.get("created_at"),
        "count": header.get("count"),
        "size": p.stat().st_size,
        "format_version": header.get("format_version"),
        "encrypted": bool(header.get("encryption")),
        "sha256": checksum(p),
        "recorded_at": datetime.now(timezone.utc).replace(microsecond=0).isoformat(),
    }


def load(directory) -> dict:
    """Read the manifest, degrading gracefully.

    A missing manifest is simply empty. A corrupt one is *moved aside* to
    ``<name>.corrupt`` rather than being silently overwritten on the next save —
    the operator keeps whatever forensic value it had, and the next backup starts a
    clean record instead of inheriting garbage.
    """
    p = manifest_path(directory)
    if not p.exists():
        return {"manifest_version": MANIFEST_VERSION, "backups": []}
    try:
        data = json.loads(p.read_text(encoding="utf-8"))
        if not isinstance(data, dict) or not isinstance(data.get("backups"), list):
            raise ValueError("manifest is not a backup manifest")
        return data
    except (json.JSONDecodeError, ValueError, OSError, UnicodeDecodeError):
        with contextlib.suppress(OSError):
            p.replace(p.with_suffix(p.suffix + ".corrupt"))
        return {"manifest_version": MANIFEST_VERSION, "backups": []}


def save(directory, manifest: dict) -> None:
    """Write the manifest atomically (temp file in the same directory + ``os.replace``)
    so an interrupted write can never leave a half-written manifest behind."""
    directory = Path(directory)
    directory.mkdir(parents=True, exist_ok=True)
    payload = json.dumps(manifest, indent=2, ensure_ascii=False)
    fd, tmp = tempfile.mkstemp(dir=str(directory), prefix=".manifest-", suffix=".tmp")
    try:
        with os.fdopen(fd, "w", encoding="utf-8") as fh:
            fh.write(payload)
        os.replace(tmp, manifest_path(directory))
    except BaseException:
        with contextlib.suppress(OSError):
            os.unlink(tmp)
        raise


def record(path) -> dict:
    """Record ``path`` in the manifest of the directory it lives in, replacing any
    previous entry for the same filename. Returns the entry written."""
    p = Path(path)
    entry = describe(p)
    manifest = load(p.parent)
    manifest["manifest_version"] = MANIFEST_VERSION
    manifest["backups"] = [e for e in manifest["backups"] if e.get("filename") != p.name] + [entry]
    save(p.parent, manifest)
    return entry


def _sort_key(entry: dict) -> tuple[str, str]:
    """Newest first. ``created_at`` is an ISO-8601 UTC string from the archive header,
    so lexicographic order is chronological order; entries without one sort last.

    It is recorded to whole-second resolution, so two backups taken in the same second
    tie — and a tie would make *which file retention deletes* arbitrary. The filename
    breaks it: the default name carries the same timestamp, so the ordering stays
    deterministic and never depends on directory-iteration order.
    """
    return (entry.get("created_at") or "", entry.get("filename") or "")


def entries(directory) -> list[dict]:
    """The merged view of a backup directory: manifest records reconciled against
    what is actually on disk, newest first.

    Each entry carries a ``status`` of ``ok`` / ``unrecorded`` / ``missing``. Files on
    disk that the manifest doesn't know about are described from their own header, so
    a wiped manifest costs metadata fidelity (no checksum to compare against) but
    never hides a backup.
    """
    directory = Path(directory)
    recorded = {e.get("filename"): dict(e) for e in load(directory).get("backups", [])}
    on_disk = set()
    if directory.is_dir():
        on_disk = {p.name for p in directory.glob(f"*{SUFFIX}") if p.is_file()}

    out: list[dict] = []
    for name in on_disk:
        entry = recorded.pop(name, None)
        if entry is not None:
            entry["status"] = OK
        else:
            entry = {"filename": name, "status": UNRECORDED, "sha256": None}
            path = directory / name
            try:
                header = archive.header_of(path)
                entry.update(
                    created_at=header.get("created_at"),
                    count=header.get("count"),
                    format_version=header.get("format_version"),
                    encrypted=bool(header.get("encryption")),
                )
            except (archive.ArchiveError, OSError):
                entry["unreadable"] = True
            with contextlib.suppress(OSError):
                entry["size"] = path.stat().st_size
        out.append(entry)

    for entry in recorded.values():
        entry["status"] = MISSING
        out.append(entry)

    return sorted(out, key=_sort_key, reverse=True)


def verify(directory, entry: dict) -> bool | None:
    """Re-hash a listed backup and compare it with the recorded checksum.

    ``True`` = matches, ``False`` = the file changed since it was recorded (tampered
    or truncated), ``None`` = nothing to compare against (unrecorded or missing file).
    """
    if entry.get("status") != OK or not entry.get("sha256"):
        return None
    try:
        return checksum(Path(directory) / entry["filename"]) == entry["sha256"]
    except OSError:
        return None


def prune(directory, keep: int, *, protect=None) -> list[str]:
    """Delete all but the ``keep`` most recent backups in ``directory``.

    Call this **only after** a new backup and its manifest update have both
    succeeded, passing the new file as ``protect`` — it is then excluded from the
    candidates outright, so retention can never delete the backup that justified it.

    Every ``*.eleb`` in the directory is a candidate, recorded or not. Returns the
    filenames actually deleted; a file that can't be removed is left in place and
    left in the manifest rather than being reported as gone.
    """
    directory = Path(directory)
    if keep <= 0 or not directory.is_dir():
        return []
    protected = Path(protect).name if protect else None

    candidates = [e for e in entries(directory) if e.get("status") != MISSING]
    candidates = [e for e in candidates if e["filename"] != protected]
    # `protect` occupies one of the `keep` slots even though it isn't a candidate.
    surplus = candidates[keep - 1 :] if protected else candidates[keep:]

    surplus_names = [entry["filename"] for entry in surplus]
    if not surplus_names:
        return []

    # Update the manifest BEFORE removing files so a crash between the two
    # steps leaves the manifest truthful. Previously, files were unlinked
    # first and the manifest still listed them until `save` ran — a save
    # failure or process kill in that window left MISSING entries on the
    # next load. An orphan file on disk with no manifest row is preferable
    # to a manifest that lies about the filesystem.
    manifest = load(directory)
    manifest["backups"] = [
        e for e in manifest.get("backups", []) if e.get("filename") not in surplus_names
    ]
    save(directory, manifest)

    deleted: list[str] = []
    for filename in surplus_names:
        try:
            (directory / filename).unlink()
        except OSError:
            continue
        deleted.append(filename)
    return deleted
