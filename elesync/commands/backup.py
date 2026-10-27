"""Backup, restore, and export commands — extracted from ``cli.py``.

These three commands (``ele backup``, ``ele restore``, ``ele export``) plus
their helpers are ~390 lines that share one concern — moving the whole vault
in and out of a portable archive — distinct from the per-memory CRUD that
dominates ``cli.py``. Splitting them out makes both files easier to navigate.

To avoid a circular import (``cli.py`` re-exports these, and these need
``cli.py``'s helpers), every helper from ``cli`` is imported lazily inside
each function — the same pattern ``_parser.py`` already uses.
"""

from __future__ import annotations

import base64
import json
import os
import sys
from datetime import datetime, timezone
from pathlib import Path


def _export_payload(items, vault_dir=None, projects=None) -> dict:
    """The portable, self-describing backup format shared by `export` and `backup`.
    Includes superseded memories (history) so a restore is lossless: `from_elesync`
    reconstructs each MemoryItem by id, preserving supersession links.

    User-defined templates ride along under an optional `templates` key. Capture
    habits are part of what a vault *is*, and a restore that brought back every
    memory but none of the structures used to record them would quietly reset
    how the user works. The key is additive: older archives simply lack it, and
    restore treats it as absent. Built-ins are not exported — they ship with the
    package, and writing them into every archive would turn a future change to a
    built-in into a stale copy on disk.
    """
    from .. import templates

    payload = {
        "elesync_export": 1,
        "exported_at": datetime.now(timezone.utc).replace(microsecond=0).isoformat(),
        "count": len(items),
        "memories": [i.to_dict() for i in items],
    }
    if vault_dir is not None:
        user_templates = templates.user_defined(vault_dir)
        if user_templates:
            payload["templates"] = {n: t.to_dict() for n, t in sorted(user_templates.items())}
    if projects is not None:
        payload["projects"] = projects
    return payload


def cmd_export(args) -> int:
    from ..cli import _, _store

    store = _store(args)
    try:
        items = store.all(include_superseded=True)
        Path(args.path).expanduser().write_text(
            json.dumps(
                _export_payload(items, store.vault_dir, store.project_snapshot()),
                indent=2,
                ensure_ascii=False,
            ),
            encoding="utf-8",
        )
        print(_("exported", len(items), args.path))
        return 0
    finally:
        store.close()


def _backup_dir(args) -> Path:
    """The managed backup directory: `--dir`, else $ELESYNC_BACKUP_DIR / `backup.dir`,
    else `<vault>/backups`."""
    from .. import backups
    from ..cli import _load_config, _resolve_vault

    override = getattr(args, "dir", None)
    if override:
        return Path(override).expanduser()
    return backups.resolve_dir(_resolve_vault(args), _load_config())


def _default_backup_name() -> str:
    stamp = datetime.now(timezone.utc).strftime("%Y%m%d-%H%M%S")
    return f"elesync-backup-{stamp}.eleb"


def cmd_backup(args) -> int:
    """Whole-vault backup, including superseded history, as a compressed
    (optionally encrypted) archive. Wraps the same lossless payload `export` writes.

    With no path, the backup lands in the *managed* directory (`<vault>/backups` by
    default) under a timestamped name, gets recorded in that directory's manifest, and
    is subject to `backup.keep` retention. An explicit path is still honoured and still
    recorded in its directory's manifest — but retention never touches a directory the
    user named, so an explicit backup is never pruned.
    """
    from .. import archive, backups, crypto
    from ..cli import EXIT_PRECONDITION, _, _backup_dir, _err, _get_passphrase, _load_config, _store

    if args.encrypt and not crypto.available():
        _err(_("error_encryption_extra"))
        return EXIT_PRECONDITION
    store = _store(args)
    try:
        managed = args.path is None
        if managed:
            directory = _backup_dir(args)
        else:
            path = Path(args.path).expanduser()
            directory = path.parent
        # A backup that refuses because its destination folder doesn't exist yet is
        # a backup that didn't happen. Create it — for the managed directory on first
        # run, and for an explicit path the user typed ahead of the folder existing.
        try:
            directory.mkdir(parents=True, exist_ok=True)
        except OSError as e:
            _err(_("backup_write_failed", str(directory), str(e)))
            return 1
        if managed:
            path = directory / _default_backup_name()
        items = store.all(include_superseded=True)
        payload = _export_payload(items, store.vault_dir, store.project_snapshot())
        key = None
        kdf_meta = None
        if args.encrypt:
            passphrase = _get_passphrase(confirm=True)
            salt = crypto.new_salt()
            ops, mem = crypto.default_limits()
            print("Deriving key (argon2id) …")
            key = crypto.derive_key(passphrase, salt, ops, mem)
            kdf_meta = {
                "kdf": "argon2id",
                "cipher": "xsalsa20poly1305",
                "salt": base64.b64encode(salt).decode("ascii"),
                "opslimit": ops,
                "memlimit": mem,
            }
        try:
            archive.write(path, payload, key=key, kdf_meta=kdf_meta)
        except OSError as e:
            _err(_("backup_write_failed", str(path), str(e)))
            return 1
        print(_("backed_up", len(items), str(path)))
    finally:
        store.close()

    # Create-then-prune: the archive above is written and closed, and the manifest
    # update below must also succeed, before any old backup is considered for deletion.
    try:
        backups.record(path)
    except (OSError, archive.ArchiveError) as e:
        # The backup itself is on disk and restorable; only its bookkeeping failed.
        # Say so and stop — pruning on an unconfirmed record is exactly the trade
        # retention must never make.
        print(_("backup_manifest_failed", str(e)), file=sys.stderr)
        return 0

    keep = backups.resolve_keep(_load_config())
    if managed and keep:
        deleted = backups.prune(path.parent, keep, protect=path)
        if deleted:
            print(_("backup_pruned", len(deleted), keep))
    return 0


def save_managed_backup(payload: dict, directory: Path, keep: int | None) -> dict:
    """The dashboard's "Back up now": write ``payload`` as an unencrypted archive in
    the managed ``directory``, record it, then apply ``keep`` retention.

    Same archive and same create-then-prune order as `ele backup`: if the record
    can't be saved, the backup stays on disk and no old backup is deleted.
    Raises OSError when the archive itself can't be written.
    """
    from .. import archive, backups

    directory.mkdir(parents=True, exist_ok=True)
    path = directory / _default_backup_name()
    archive.write(path, payload)
    result: dict = {"path": str(path), "memories": payload["count"], "pruned": 0}
    try:
        result["created_at"] = backups.record(path).get("created_at")
    except (OSError, archive.ArchiveError) as e:
        result["warning"] = f"Backup saved, but it could not be added to the backup list: {e}"
        return result
    if keep:
        result["pruned"] = len(backups.prune(directory, keep, protect=path))
    return result


def _format_size(n) -> str:
    if not isinstance(n, int):
        return "?"
    for unit in ("B", "KB", "MB", "GB"):
        if n < 1024 or unit == "GB":
            return f"{n:.0f} {unit}" if unit == "B" else f"{n:.1f} {unit}"
        n /= 1024.0
    return f"{n} B"


def cmd_backup_list(args) -> int:
    """List the backups in the managed directory, newest first, from the manifest —
    falling back to each archive's own header for anything the manifest doesn't know
    about, so a lost manifest never hides a backup."""
    from .. import backups
    from ..cli import _, _backup_dir

    directory = _backup_dir(args)
    rows = backups.entries(directory)
    if not rows:
        print(_("backup_list_empty", str(directory)))
        return 0
    print(_("backup_list_header", str(directory)))
    for entry in rows:
        marks = []
        if entry.get("encrypted"):
            marks.append("encrypted")
        if entry.get("status") == backups.UNRECORDED:
            marks.append(_("backup_mark_unrecorded"))
        if entry.get("status") == backups.MISSING:
            marks.append(_("backup_mark_missing"))
        if entry.get("unreadable"):
            marks.append(_("backup_mark_unreadable"))
        if args.verify:
            ok = backups.verify(directory, entry)
            if ok is True:
                marks.append(_("backup_mark_verified"))
            elif ok is False:
                marks.append(_("backup_mark_changed"))
        count = entry.get("count")
        print(
            f"  {entry['filename']}  "
            f"{entry.get('created_at') or '?'}  "
            f"{_format_size(entry.get('size'))}  "
            f"{count if count is not None else '?'} memories"
            + (f"  [{', '.join(marks)}]" if marks else "")
        )
    return 0


def _resolve_destination(args) -> str | None:
    """Resolve the remote destination from --destination flag or config.

    Priority: --destination flag > $ELESYNC_BACKUP_DESTINATION >
    config["backup"]["destination"] > None.
    """
    dest = getattr(args, "destination", None)
    if dest:
        return dest
    env = os.environ.get("ELESYNC_BACKUP_DESTINATION")
    if env:
        return env
    from ..cli import _load_config

    cfg = _load_config()
    return cfg.get("backup", {}).get("destination")


def cmd_backup_push(args) -> int:
    """Push the latest local backup to a remote destination via rclone.

    The remote is an rclone-style path (e.g. ``s3:my-bucket/elesync`` or
    ``myremote:backups``). rclone handles all auth and transfer details;
    EleSync just invokes it as a subprocess. Requires rclone installed
    and configured (``rclone config``).
    """
    from .. import backups, remote
    from ..cli import _backup_dir, _err, _load_config

    dest = _resolve_destination(args)
    if not dest:
        _err(
            "No remote destination configured.\n"
            "Set one with:  ele backup push --destination s3:my-bucket/elesync\n"
            "Or persist it:  ele config set backup.destination s3:my-bucket/elesync"
        )
        return 1

    if not remote.is_available():
        _err(
            "rclone is not installed or not on PATH.\n"
            "Install it from https://rclone.org/install/ and run `rclone config`."
        )
        return 1

    directory = _backup_dir(args)
    entries = backups.entries(directory)
    if not entries:
        _err(f"No backups found in {directory}. Run `ele backup` first.")
        return 1

    # Find the newest local backup.
    latest = entries[0]
    filename = latest.get("filename")
    if not filename:
        _err("Could not determine the latest backup filename.")
        return 1

    local_path = directory / filename
    if not local_path.is_file():
        _err(f"Backup file not found: {local_path}")
        return 1

    try:
        remote_dest = remote.push(local_path, dest)
    except remote.RemoteError as e:
        _err(f"Push failed: {e}")
        return 1

    print(f"☁️  Pushed {filename} to {remote_dest}")

    # Apply remote retention if configured.
    keep = backups.resolve_keep(_load_config())
    if keep:
        try:
            deleted = remote.prune(dest, keep, protect=filename)
            if deleted:
                print(f"🧹  Pruned {len(deleted)} old backup(s) from remote (keep={keep})")
        except remote.RemoteError as e:
            print(f"⚠️  Remote retention failed: {e}", file=sys.stderr)

    return 0


def cmd_backup_pull(args) -> int:
    """Pull a backup from a remote destination into the local managed directory.

    Without a name, pulls the newest backup at the remote. With a name,
    pulls that specific file. The downloaded file lands in the managed
    backup directory, ready for `ele restore`.
    """
    from .. import remote
    from ..cli import _backup_dir, _err

    dest = _resolve_destination(args)
    if not dest:
        _err(
            "No remote destination configured.\n"
            "Set one with:  ele backup pull --destination s3:my-bucket/elesync\n"
            "Or persist it:  ele config set backup.destination s3:my-bucket/elesync"
        )
        return 1

    if not remote.is_available():
        _err(
            "rclone is not installed or not on PATH.\n"
            "Install it from https://rclone.org/install/ and run `rclone config`."
        )
        return 1

    directory = _backup_dir(args)
    name = getattr(args, "name", None)

    try:
        local_path = remote.pull(dest, directory, name=name)
    except remote.RemoteError as e:
        _err(f"Pull failed: {e}")
        return 1

    print(f"☁️  Pulled {local_path.name} to {local_path}")
    print(f"    Restore with:  ele restore {local_path}")
    return 0


def _restore_items_from_archive(path: str):
    """Read a `.eleb` archive into `(MemoryItems, raw payload)`. The raw payload is
    returned alongside so the caller can restore non-memory content (templates)
    without decrypting the archive a second time. Derives the key (passphrase via
    prompt / $ELESYNC_PASSPHRASE) only if the header says the body is encrypted.
    The plaintext header is validated first, so a bad magic/version fails before
    we ever ask for a passphrase."""
    from .. import archive, crypto, normalize
    from ..cli import EXIT_PRECONDITION, _, _err, _get_passphrase

    header = archive.header_of(path)  # raises ArchiveError on bad magic/version
    key = None
    if header.get("encryption"):
        if not crypto.available():
            _err(_("error_encryption_extra"))
            raise SystemExit(EXIT_PRECONDITION)
        meta = header["encryption"]
        passphrase = _get_passphrase(confirm=False)
        key = crypto.derive_key(
            passphrase,
            base64.b64decode(meta["salt"]),
            meta["opslimit"],
            meta["memlimit"],
        )
    payload = archive.read(path, key=key)  # raises DecryptError / ArchiveError
    return normalize.from_elesync(payload, source_ref=Path(path).name), payload


def _warn_if_checksum_changed(path) -> None:
    """If this archive is recorded in a manifest beside it, check the bytes still hash
    to what was recorded. A mismatch means the file changed after it was written —
    truncation, bit rot, or tampering. It's a warning, not a refusal: restore is
    idempotent and content-hashed, and a partially readable backup is often exactly
    what someone is trying to salvage. An encrypted archive additionally fails its
    Poly1305 authentication on any modification, so this covers the plaintext case."""
    from .. import backups
    from ..cli import _

    p = Path(path).expanduser()
    entry = next(
        (e for e in backups.entries(p.parent) if e.get("filename") == p.name),
        None,
    )
    if entry and backups.verify(p.parent, entry) is False:
        print(_("backup_checksum_changed", p.name), file=sys.stderr)


def cmd_restore(args) -> int:
    """Restore memories from an EleSync backup. Idempotent and lossless: existing
    memories are recognised by content hash and skipped, and supersession links are
    preserved because ids round-trip through the backup. Reads the current `.eleb`
    archive (compressed, optionally encrypted) and, for back-compat, any older
    plain-JSON `ele backup`/`ele export` file."""
    from .. import archive, normalize
    from ..cli import _, _err, _restore_templates, _store

    store = _store(args, create=True)
    try:
        if archive.is_archive(args.path):
            _warn_if_checksum_changed(args.path)
            try:
                items, payload = _restore_items_from_archive(args.path)
            except archive.DecryptError:
                _err(_("error_wrong_passphrase"))
                return 1
            except archive.ArchiveError as e:
                _err(_("error_bad_archive", str(e)))
                return 1
        else:
            payload = {}
            try:
                try:
                    raw_payload = json.loads(
                        Path(args.path).expanduser().read_text(encoding="utf-8")
                    )
                except (json.JSONDecodeError, OSError):
                    raw_payload = None
                if isinstance(raw_payload, dict) and raw_payload.get("elesync_export"):
                    payload = raw_payload
                items = normalize.normalize_file(args.path, source=args.source)
            except (ValueError, OSError) as e:
                _err(e)
                return 1
        result = store.import_many(items)
        print(_("restored", result["added"], result["skipped_duplicates"], result["total"]))
        store.restore_projects(payload.get("projects"))
        added = _restore_templates(store.vault_dir, payload)
        if added:
            print(_("restored_templates", added))
        return 0
    finally:
        store.close()


def _restore_templates(vault_dir, payload) -> int:
    """Write templates carried in a backup, without clobbering local ones.

    Restore is idempotent for memories (content hash) and must be for templates
    too. A same-named template already in the vault is left alone: the local
    file is the one the user has been editing, and silently replacing it with an
    older copy from an archive would be a data loss the user never asked for.
    Returns how many were newly written.
    """
    from .. import templates

    carried = (payload or {}).get("templates") or {}
    if not isinstance(carried, dict):
        return 0
    added = 0
    for name, data in sorted(carried.items()):
        try:
            tpl = templates.Template.from_dict(name, data)
        except templates.TemplateError:
            continue  # a corrupt entry must not abort a memory restore
        if (templates.templates_dir(vault_dir) / f"{tpl.name}.json").exists():
            continue
        try:
            templates.save(vault_dir, tpl)
        except OSError:
            continue
        added += 1
    return added
