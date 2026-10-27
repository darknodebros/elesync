"""Command-line interface for the vault.

python -m elesync.cli import chatgpt_export.json --source chatgpt
python -m elesync.cli search "Helios project"
python -m elesync.cli add "Prefers direct, no-fluff answers" --type preference
python -m elesync.cli stats
python -m elesync.cli reindex          # rebuild the index from notes/*.md
python -m elesync.cli export backup.json
python -m elesync.cli serve            # start the MCP server
"""

from __future__ import annotations

import base64
import contextlib
import getpass
import logging
import os
import sys
import threading
from pathlib import Path

from . import crypto
from . import scopes as scopes_mod
from .config import config_path as _config_path
from .config import load_config as _load_config
from .config import save_config as _save_config
from .i18n import _, get_language, set_language
from .store import PROJECTS_ENCRYPTED_FILE, PROJECTS_FILE, MemoryStore
from .summary import build_summary

_log = logging.getLogger("elesync.cli")

# Max characters of a memory's content shown per hit in `ele search` before it's
# truncated with an ellipsis — keeps long memories from flooding the terminal.
SEARCH_SNIPPET_CHARS = 500

# Process exit codes — a stable contract for scripts wrapping the CLI.
#   0  success
#   1  a general runtime error (the operation failed; see stderr)
#   2  a precondition isn't met that needs a deliberate user action before
#      retrying — an optional extra to install, or an unsafe config to fix.
# Anything nonzero means failure; only 2 carries the extra "you must act first"
# meaning, so a wrapper can special-case it (e.g. prompt to `pip install`).
EXIT_OK = 0
EXIT_ERROR = 1
EXIT_PRECONDITION = 2
_CONFIG_LOCK = threading.RLock()


def _progress(label: str):
    """Return a ``progress(done, total)`` callback that draws a one-line bar on
    stderr, or None when stderr isn't a TTY (piped/CI — no spinner spam, and no
    risk of polluting stdout, which the MCP stdio transport uses as its channel).
    Only worth showing for sizeable jobs, so it stays quiet under a small total."""
    if not sys.stderr.isatty():
        return None

    def cb(done: int, total: int) -> None:
        if total < 50:  # small job — not worth a bar
            return
        pct = int(done * 100 / total) if total else 100
        end = "\n" if done >= total else ""
        print(f"\r{label}: {done}/{total} ({pct}%)   ", end=end, file=sys.stderr, flush=True)

    return cb


def _err(msg) -> None:
    """Print a hard-error line to stderr with the canonical ❌ prefix.

    Single choke point so every operation-failed message looks identical
    (`❌  <message>`). Warnings keep their own ⚠️ prefix and don't route here;
    i18n error strings are passed already-translated (the emoji is language-
    neutral chrome, so it leads the localized text)."""
    print(f"❌  {msg}", file=sys.stderr)


def _grant(args):
    """Build the scope grant for read commands from the global --as flag.
    None (flag absent) means unrestricted — the owner's full vault."""
    return scopes_mod.grant_from(getattr(args, "as_scopes", None))


def _semantic_on(args) -> bool:
    """True when the user explicitly requested semantic/hybrid search."""
    if bool(getattr(args, "semantic", False) or os.environ.get("ELESYNC_SEMANTIC")):
        return True
    return getattr(args, "mode", "auto") in ("semantic", "hybrid")


def _embedder(args):
    """Load the embedding model only when semantic mode is requested. Returns None
    (and a friendly note) if requested but the optional extra isn't installed."""
    if not _semantic_on(args):
        return None
    from . import embeddings

    emb = embeddings.load_embedder()
    if emb is None:
        print(_("error_semantic_unavailable"), file=sys.stderr)
    return emb


def _verify_key(vault, key) -> bool:
    """Cheap check that the derived key actually opens the vault (decrypts a note),
    so we can report a wrong passphrase clearly instead of returning an empty vault."""
    from nacl.exceptions import CryptoError

    vault_path = Path(vault).expanduser()
    encs = sorted((vault_path / "notes").glob("*.md.enc"))
    project_manifest = vault_path / "projects.json.enc"
    candidates = encs or ([project_manifest] if project_manifest.is_file() else [])
    if not candidates:
        return True
    try:
        crypto.decrypt(key, candidates[0].read_bytes())
        return True
    except (CryptoError, OSError):
        return False


def _resolve_vault(args=None) -> Path:
    """The vault path a command should act on: `--vault`, else $ELESYNC_DIR, else the
    configured `vault_dir`, else the default. This is the single source of truth for
    vault resolution -- every command (and `ele serve`) must route through it so two
    commands can never disagree about which vault they act on."""
    vault = (getattr(args, "vault", None) if args is not None else None) or os.environ.get(
        "ELESYNC_DIR", ""
    )
    if not vault:
        cfg = _load_config()
        vault = cfg.get("vault_dir") or "~/EleSyncVault"
    return Path(vault).expanduser()


def _store(args, create: bool = False) -> MemoryStore:
    """Open the vault. If it's encrypted, unlock it live (in-memory index) using the
    passphrase from $ELESYNC_PASSPHRASE or a prompt — no separate `ele decrypt` needed.
    Pass `create=True` for recovery commands (e.g. `restore`) that must be able to
    target a fresh location rather than erroring when the vault doesn't exist yet."""
    vault_path = _resolve_vault(args)
    vault = str(vault_path)
    if not vault_path.exists():
        if create:
            vault_path.mkdir(parents=True, exist_ok=True)
        else:
            _err(
                f"Vault not found at {vault_path} — run 'ele vault list' to see available vaults, "
                "or 'ele vault add' to register one."
            )
            raise SystemExit(1)
    key = None
    if crypto.is_encrypted(vault):
        if not crypto.available():
            _err(_("error_encryption_needed"))
            raise SystemExit(EXIT_PRECONDITION)
        meta = crypto.read_meta(vault)
        if meta is None:
            _err(_("error_encryption_meta_missing"))
            raise SystemExit(1)
        key = crypto.derive_key(
            _get_passphrase(confirm=False),
            base64.b64decode(meta["salt"]),
            meta["opslimit"],
            meta["memlimit"],
        )
        if not _verify_key(vault, key):
            _err(_("error_wrong_passphrase"))
            raise SystemExit(1)
    return MemoryStore(vault, embedder=_embedder(args), key=key)


def cmd_import(args) -> int:
    from .commands.import_ import cmd_import as implementation

    return implementation(args)


def _import_stdin(args) -> int:
    from .commands.import_ import import_stdin

    return import_stdin(args)


def _resolve_search_params(args) -> tuple[str | None, float | None]:
    """Resolve (mode, semantic_weight) from CLI flags + config.

    Returns (mode, semantic_weight) where:
    - mode is one of 'fts', 'semantic', 'hybrid', or None (= auto, the default).
      None means let store.search() decide based on whether embeddings exist.
    - semantic_weight is a float in [0.0, 1.0] or None (use default 0.5).

    CLI flags override config; config overrides the built-in default.
    """
    mode = getattr(args, "mode", "auto")
    sw = getattr(args, "semantic_weight", None)

    cfg = _load_config()
    search_cfg = cfg.get("search", {})

    if mode == "auto":
        # Config can set a non-auto default mode.
        cfg_mode = search_cfg.get("mode", "auto")
        if cfg_mode in ("fts", "semantic", "hybrid"):
            mode = cfg_mode
        else:
            mode = "auto"

    if sw is None:
        sw = search_cfg.get("hybrid_weight")
        if sw is not None:
            try:
                sw = float(sw)
            except (TypeError, ValueError):
                sw = None

    # Translate mode to what store.search() expects.
    # 'auto' -> None (let the store decide), 'fts' -> weight 0.0,
    # 'semantic' -> weight 1.0, 'hybrid' -> use sw or 0.5.
    if mode == "fts":
        return None, 0.0  # weight 0.0 means pure FTS in _hybrid_rows
    if mode == "semantic":
        return None, 1.0  # weight 1.0 means pure vector
    if mode == "hybrid":
        return None, sw if sw is not None else 0.5
    return None, sw  # auto: pass through the weight (or None for default)


def cmd_search(args) -> int:
    from .commands.search import cmd_search as implementation

    return implementation(args)


def cmd_add(args) -> int:
    from .commands.memory import cmd_add as implementation

    return implementation(args)


def _parse_set_values(pairs) -> dict:
    """Turn `--set k=v` repeats into a dict, rejecting anything that isn't `k=v`.

    A bare `--set name` is almost always a shell-quoting accident on a value
    containing a space; guessing at the intent would fill a template with the
    wrong thing.
    """
    values: dict[str, str] = {}
    for raw in pairs or []:
        if "=" not in raw:
            raise ValueError(f"--set expects key=value, got {raw!r}")
        key, _, val = raw.partition("=")
        key = key.strip()
        if not key:
            raise ValueError(f"--set expects key=value, got {raw!r}")
        values[key] = val
    return values


def cmd_template_list(args) -> int:
    from .commands.templates import cmd_template_list as implementation

    return implementation(args)


def cmd_template_show(args) -> int:
    from .commands.templates import cmd_template_show as implementation

    return implementation(args)


def cmd_template_new(args) -> int:
    from .commands.templates import cmd_template_new as implementation

    return implementation(args)


def cmd_template_remove(args) -> int:
    from .commands.templates import cmd_template_remove as implementation

    return implementation(args)


def cmd_reindex(args) -> int:
    with _store(args) as store:
        res = store.reindex(progress=_progress("Reindexing"))
        print(_("reindexed", res["reindexed"], res["vault_dir"]))
        return 0


def _ensure_vault_gitignore(vault: Path) -> None:
    """Make sure the binary index isn't synced — it's a local, rebuildable cache.
    Appends `index.db` to the vault's .gitignore (creating it) if not already listed."""
    gi = vault / ".gitignore"
    existing = gi.read_text(encoding="utf-8") if gi.exists() else ""
    if "index.db" in existing:
        return
    block = "# EleSync: the search index is a local, rebuildable cache — don't sync it\nindex.db\n"
    sep = "" if (not existing or existing.endswith("\n")) else "\n"
    with gi.open("a", encoding="utf-8") as f:
        f.write(sep + block)


def cmd_sync(args) -> int:
    with _store(args) as store:
        _ensure_vault_gitignore(store.vault_dir)
        res = store.sync(progress=_progress("Syncing"))
        print(_("synced", res["added"], res["changed"], res["removed"], res["total"]))
        return 0


def cmd_forget(args) -> int:
    from .service import MemoryService

    with _store(args) as store:
        msg = MemoryService(store).forget(args.id)
        print(msg if msg != "Deleted." else _("forget_deleted"))
        return 0 if msg == "Deleted." else 1


def cmd_conflicts(args) -> int:
    from .service import MemoryService

    with _store(args) as store:
        svc = MemoryService(store, grant=_grant(args))
        subcmd = getattr(args, "conflicts_subcmd", None)
        if subcmd == "list" or subcmd is None:
            # 'ele conflicts list' and bare 'ele conflicts' both show the listing
            # with cluster IDs for use with resolve-conflict.
            print(svc.conflicts_list())
        else:
            print(svc.conflicts())
        return 0


def cmd_resolve_conflict(args) -> int:
    from .service import MemoryService

    with _store(args) as store:
        msg = MemoryService(store, grant=_grant(args)).resolve_conflict(
            cluster_id_prefix=args.cluster_id,
            choose_id=getattr(args, "choose", None),
            strategy=getattr(args, "strategy", None),
        )
        print(msg)
        # Return non-zero if message starts with a known error pattern.
        ok_prefixes = ("Resolved",)
        return 0 if any(msg.startswith(p) for p in ok_prefixes) else 1


def cmd_supersede(args) -> int:
    from .service import MemoryService

    with _store(args) as store:
        msg = MemoryService(store, grant=_grant(args)).supersede(args.old_id, args.by)
        print(msg)
        return 0 if msg.startswith("Superseded") else 1


def cmd_link(args) -> int:
    from .service import MemoryService

    with _store(args) as store:
        msg = MemoryService(store, grant=_grant(args)).link_memories(
            args.source_id, args.target_id, args.relation, remove=args.remove
        )
        print(msg)
        return 0 if msg.startswith(("Linked", "Removed")) else 1


def cmd_related(args) -> int:
    from .service import MemoryService

    with _store(args) as store:
        print(MemoryService(store, grant=_grant(args)).related_memories(args.id))
        return 0


def cmd_history(args) -> int:
    from .service import MemoryService

    with _store(args) as store:
        print(MemoryService(store, grant=_grant(args)).history(args.id))
        return 0


def cmd_scope(args) -> int:
    """Re-assign the scopes of an existing memory (or clear them back to general)."""
    from .service import MemoryService

    with _store(args) as store:
        new_scopes = "" if args.clear else (args.scopes or "")
        msg = MemoryService(store, grant=_grant(args)).scope(args.id, new_scopes)
        print(msg)
        return 0 if msg.startswith("Scoped") else 1


def cmd_scopes(args) -> int:
    """List the scope compartments in the vault, with per-scope counts."""
    from .service import MemoryService

    with _store(args) as store:
        print(MemoryService(store, grant=_grant(args)).scope_summary())
        return 0


def cmd_embed(args) -> int:
    from . import embeddings

    emb = embeddings.load_embedder()
    if emb is None:
        _err(_("error_no_embedder"))
        return EXIT_PRECONDITION
    with MemoryStore(
        args.vault or os.environ.get("ELESYNC_DIR", "~/EleSyncVault"), embedder=emb
    ) as store:
        print(_("embedding_started", emb.model_name))
        n = store.embed_missing()
        print(_("embedded", n))
        return 0


def _get_passphrase(confirm: bool) -> str:
    """Read the vault passphrase from $ELESYNC_PASSPHRASE, or prompt for it
    (never echoed). With confirm=True (encrypting), ask twice."""
    env = os.environ.get("ELESYNC_PASSPHRASE")
    if env:
        return env
    p = getpass.getpass("Vault passphrase: ")
    if not p:
        _err(_("error_empty_passphrase"))
        raise SystemExit(1)
    if confirm and p != getpass.getpass("Confirm passphrase: "):
        _err(_("error_passphrase_mismatch"))
        raise SystemExit(1)
    return p


def cmd_encrypt(args) -> int:
    if not crypto.available():
        _err(_("error_encryption_extra"))
        return EXIT_PRECONDITION
    vault = _resolve_vault(args)
    if crypto.is_encrypted(vault):
        _err(_("error_already_encrypted"))
        return 1
    notes = sorted((vault / "notes").glob("*.md"))
    if not notes:
        _err(_("error_no_notes", str(vault / "notes")))
        return 1
    passphrase = _get_passphrase(confirm=True)
    salt = crypto.new_salt()
    ops, mem = crypto.default_limits()
    print("Deriving key (argon2id) …")
    key = crypto.derive_key(passphrase, salt, ops, mem)
    for md in notes:
        md.with_name(md.name + ".enc").write_bytes(crypto.encrypt(key, md.read_bytes()))
        md.unlink()
    projects_path = vault / PROJECTS_FILE
    if projects_path.exists():
        (vault / PROJECTS_ENCRYPTED_FILE).write_bytes(
            crypto.encrypt(key, projects_path.read_bytes())
        )
        projects_path.unlink()
    idx = vault / "index.db"  # plaintext index would leak content — drop it
    if idx.exists():
        idx.unlink()
    crypto.write_meta(vault, salt, ops, mem)
    print(f"Encrypted {len(notes)} notes at rest. Run `ele decrypt` to open the vault again.")
    return 0


def cmd_decrypt(args) -> int:
    if not crypto.available():
        _err(_("error_encryption_extra"))
        return EXIT_PRECONDITION
    vault = _resolve_vault(args)
    meta = crypto.read_meta(vault)
    if not meta:
        _err(_("error_not_encrypted"))
        return 1
    passphrase = _get_passphrase(confirm=False)
    key = crypto.derive_key(
        passphrase, base64.b64decode(meta["salt"]), meta["opslimit"], meta["memlimit"]
    )
    encs = sorted((vault / "notes").glob("*.md.enc"))
    print("Deriving key (argon2id) …")
    from nacl.exceptions import CryptoError

    try:
        # Decrypt everything first (this also authenticates the passphrase) so a
        # wrong passphrase changes nothing on disk.
        decrypted = [(enc, crypto.decrypt(key, enc.read_bytes())) for enc in encs]
        encrypted_projects = vault / PROJECTS_ENCRYPTED_FILE
        decrypted_projects = (
            crypto.decrypt(key, encrypted_projects.read_bytes())
            if encrypted_projects.exists()
            else None
        )
    except CryptoError:
        _err("Wrong passphrase (authentication failed). Nothing was changed.")
        return 1
    except OSError as e:
        _err(f"Could not read encrypted note: {e}. Nothing was changed.")
        return 1
    for enc, data in decrypted:
        enc.with_name(enc.name[:-4]).write_bytes(data)  # strip ".enc"
        enc.unlink()
    if decrypted_projects is not None:
        (vault / PROJECTS_FILE).write_bytes(decrypted_projects)
        (vault / PROJECTS_ENCRYPTED_FILE).unlink()
    (vault / crypto.META_FILE).unlink(missing_ok=True)
    with MemoryStore(vault) as store:  # rebuild the searchable index from restored notes
        res = store.reindex(progress=_progress("Reindexing"))
    print(
        f"Decrypted {len(decrypted)} notes and rebuilt the index "
        f"({res['reindexed']} memories). Vault is open."
    )
    return 0


# --- backup / restore / export commands live in commands/backup.py ----------
# Re-imported here so `from elesync.cli import cmd_backup` and the existing
# argparse wiring in _parser.py keep working unchanged.
from .commands.backup import (  # noqa: E402,F401  (re-export; late to avoid cycle)
    _backup_dir,
    _default_backup_name,
    _export_payload,
    _format_size,
    _resolve_destination,
    _restore_items_from_archive,
    _restore_templates,
    _warn_if_checksum_changed,
    cmd_backup,
    cmd_backup_list,
    cmd_backup_pull,
    cmd_backup_push,
    cmd_export,
    cmd_restore,
)


def cmd_stats(args) -> int:
    with _store(args) as store:
        s = store.stats()
        print(_("stats_vault", s["vault_dir"]))
        print(_("stats_total", s["total"], _("state_on") if s["fts_enabled"] else _("state_off")))
        print(
            _(
                "stats_embedded",
                s["embedded"],
                s["total"],
                _("semantic_available") if s["embedded"] else _("semantic_off_hint"),
            )
        )
        print(_("stats_by_source", s["by_source"] or "—"))
        print(_("stats_by_type", s["by_type"] or "—"))
        return 0


def cmd_summarize(args) -> int:
    """Summarize a subset of the vault using an LLM (Ollama/OpenAI/Anthropic).

    Filters memories by --tag, --source, --type, or --since (days). Outputs
    in bullet/narrative/tldr format. With --save-as, writes the summary as
    a new memory. With --dry-run, shows the prompt without calling the API.
    """
    from . import summarize as _summarize

    cfg = _load_config()
    provider = getattr(args, "provider", None) or _summarize.resolve_provider(cfg)

    # Auto-detect Ollama if no provider is configured.
    if provider == "none":
        ollama_url = _summarize.resolve_ollama_url(cfg)
        if _summarize.detect_ollama(ollama_url):
            provider = "ollama"
            print("💡  Detected a local Ollama server — using it (no content leaves your machine).")
        else:
            _err(
                "No LLM provider configured.\n"
                "Options:\n"
                "  1. Install Ollama from https://ollama.com and run `ollama serve`\n"
                "  2. Set OPENAI_API_KEY and: ele summarize --provider openai\n"
                "  3. Set ANTHROPIC_API_KEY and: ele summarize --provider anthropic\n"
                "  4. Persist: ele config set llm.provider ollama\n"
                "  5. Dry run: ele summarize --dry-run"
            )
            return 1

    # Privacy warning before sending to a remote API.
    if not _summarize.is_local_provider(provider) and not getattr(args, "dry_run", False):
        dest = "OpenAI" if provider == "openai" else "Anthropic"
        print(
            f"⚠️  Sending vault content to {dest}. "
            f"Use --provider ollama for local-only summarization, or --dry-run to preview.",
            file=sys.stderr,
        )

    with _store(args) as store:
        items = store.all(include_superseded=False)

        # Apply filters.
        tag = getattr(args, "tag", None)
        source = getattr(args, "source", None)
        mtype = getattr(args, "type", None)
        since_days = getattr(args, "since", None)

        if tag:
            tag_set = {t.strip().lower() for t in tag.split(",") if t.strip()}
            items = [i for i in items if any(t.lower() in tag_set for t in (i.tags or []))]
        if source:
            items = [i for i in items if i.source == source]
        if mtype:
            items = [i for i in items if i.type == mtype]
        if since_days:
            from datetime import datetime, timedelta, timezone

            cutoff = datetime.now(timezone.utc) - timedelta(days=since_days)
            cutoff_str = cutoff.isoformat()
            items = [i for i in items if i.created_at and i.created_at >= cutoff_str]

        if not items:
            print("No memories match the given filters.")
            return 0

        print(f"Summarizing {len(items)} memor{'y' if len(items) == 1 else 'ies'}…\n")

        fmt = getattr(args, "format", "bullet") or "bullet"
        dry_run = getattr(args, "dry_run", False)

        try:
            result = _summarize.summarize(
                items,
                provider=provider,
                fmt=fmt,
                config=cfg,
                dry_run=dry_run,
            )
        except _summarize.SummarizeError as e:
            _err(str(e))
            return 1

        if dry_run:
            print(result)
            return 0

        print(result)

        # Save as a new memory if requested.
        save_as = getattr(args, "save_as", None)
        if save_as:
            from .models import MemoryItem

            summary_item = MemoryItem(
                content=result,
                type="note",
                tags=["summary", "ai-generated"],
                source="summarize",
            )
            store.upsert(summary_item)
            print(f"\n💾  Saved as memory id={summary_item.id[:8]}")

        return 0


def cmd_audit(args) -> int:
    from .audit import AuditLog

    vault = args.vault or os.environ.get("ELESYNC_DIR", "")
    if not vault:
        cfg = _load_config()
        vault = cfg.get("vault_dir") or "~/EleSyncVault"
    audit = AuditLog(vault)
    if not audit.enabled:
        print("Audit log not found. Start the connected AI server once to create it.")
        return 1

    sub = getattr(args, "audit_cmd", None)
    if sub == "clear":
        if audit.clear():
            print("Audit log cleared.")
        else:
            print("Nothing to clear.")
        return 0

    # Default: show recent entries
    last = getattr(args, "last", 50)
    op = getattr(args, "operation", None)
    client = getattr(args, "client", None)
    entries = audit.read(operation=op, client=client, last=last)
    print(audit.format_entries(entries))
    return 0


def cmd_auth(args) -> int:
    """`ele auth` with no subcommand: report the current auth posture."""
    if getattr(args, "auth_cmd", None):
        return args.auth_func(args)
    return cmd_auth_status(args)


def cmd_auth_status(args) -> int:
    from . import auth as _auth_mod

    cfg = _load_config()
    if os.environ.get("ELESYNC_TOKEN"):
        print("🔑  Bearer token auth: ENABLED (from ELESYNC_TOKEN)")
        return 0
    if _auth_mod.get_configured_token(cfg):
        print(f"🔑  Bearer token auth: ENABLED (from {_config_path()})")
        return 0
    mtls = _auth_mod.get_mtls_config(cfg)
    if mtls:
        print(f"🔐  mTLS auth: ENABLED (CA: {mtls['ca_cert']})")
        return 0
    print("🔓  Bearer token auth: DISABLED — http/sse transports are unauthenticated.")
    print("    Enable it with: ele auth generate-token")
    print("    Or use mTLS:     ele auth setup-mtls")
    return 0


def cmd_auth_generate_token(args) -> int:
    from . import auth as _auth_mod

    cfg = _load_config()
    existing = cfg.get("http", {}).get("auth", {}).get("token")
    if existing and not getattr(args, "force", False):
        _err(
            "A token is already configured. Re-run with --force to replace it "
            "(every client using the old token will start getting 401s)."
        )
        return 1

    token = _auth_mod.generate_token()
    cfg.setdefault("http", {}).setdefault("auth", {})
    cfg["http"]["auth"]["mode"] = "token"
    cfg["http"]["auth"]["token"] = token
    _save_config(cfg)

    print(f"🔑  Token written to {_config_path()}\n")
    print(f"    {token}\n")
    print("    Clients must send:  Authorization: Bearer <token>")
    print("    This is the only time it is printed — copy it now.")
    return 0


def cmd_auth_revoke(args) -> int:
    cfg = _load_config()
    if not cfg.get("http", {}).get("auth", {}).get("token"):
        print("No token is configured; nothing to revoke.")
        return 0
    cfg["http"]["auth"].pop("token", None)
    cfg["http"]["auth"]["mode"] = "none"
    _save_config(cfg)
    print("🔓  Token revoked. http/sse transports are now unauthenticated.")
    if os.environ.get("ELESYNC_TOKEN"):
        print("    Note: ELESYNC_TOKEN is still set in this environment and takes priority.")
    return 0


def cmd_auth_setup_mtls(args) -> int:
    """Generate a self-signed CA + server + client cert and wire mTLS into config."""
    from . import auth as _auth_mod

    cert_dir = Path(args.cert_dir).expanduser() if args.cert_dir else _config_path().parent / "mtls"
    existing = (
        list(cert_dir.glob("*.crt")) + list(cert_dir.glob("*.key")) if cert_dir.exists() else []
    )
    if existing and not getattr(args, "force", False):
        _err(
            f"Certificates already exist in {cert_dir}.\n"
            "Re-run with --force to regenerate them (every client using the old "
            "certs will start getting TLS handshake failures)."
        )
        return 1

    try:
        paths = _auth_mod.setup_mtls(cert_dir)
    except ImportError as e:
        _err(str(e))
        return 1

    cfg = _load_config()
    cfg.setdefault("http", {}).setdefault("auth", {})
    cfg["http"]["auth"]["mode"] = "mtls"
    cfg["http"]["auth"]["mtls"] = paths
    # A token and mTLS are independent auth layers; clear any stale token so
    # the user gets a clean mTLS-only setup unless they explicitly add a token.
    cfg["http"]["auth"].pop("token", None)
    _save_config(cfg)

    print(f"🔐  mTLS certificates generated in {cert_dir}\n")
    print("    Server (for ele serve):")
    print(f"      CA cert:      {paths['ca_cert']}")
    print(f"      Server cert:  {paths['server_cert']}")
    print(f"      Server key:   {paths['server_key']}\n")
    print("    Client (for the MCP client to present):")
    print(f"      {cert_dir / 'client.crt'}")
    print(f"      {cert_dir / 'client.key'}\n")
    print("    The client must present client.crt/client.key on every connection.")
    print("    Without a valid client cert, the TLS handshake fails before any")
    print("    HTTP request reaches the server — no token, no replay risk.")
    return 0


def _configured_or_new_token(*, force: bool = False) -> tuple[str, bool]:
    """Return the configured HTTP Bearer token, generating one when needed.

    The ChatGPT connection flow should be safe by default: a remote MCP endpoint
    exposes write/delete tools, so the helper must never hand a user an
    off-machine serve command without also ensuring token auth is configured.
    """
    from . import auth as _auth_mod

    env_token = os.environ.get("ELESYNC_TOKEN")
    if env_token:
        if force:
            raise ValueError(
                "Cannot replace token while ELESYNC_TOKEN is set. Unset ELESYNC_TOKEN first."
            )
        return env_token, False

    with _CONFIG_LOCK:
        cfg = _load_config()
        existing = cfg.get("http", {}).get("auth", {}).get("token")
        if existing and not force:
            return existing, False

        token = _auth_mod.generate_token()
        cfg.setdefault("http", {}).setdefault("auth", {})
        cfg["http"]["auth"]["mode"] = "token"
        cfg["http"]["auth"]["token"] = token
        _save_config(cfg)
        return token, True


def _quote_cli_arg(value: str) -> str:
    """Quote a command argument for POSIX shells or Windows PowerShell."""
    if os.name == "nt":
        return "'" + value.replace("'", "''") + "'"
    import shlex

    return shlex.quote(value)


_REMOTE_CONNECT_CLIENTS = {
    "chatgpt": "ChatGPT",
    "grok": "Grok",
    "gemini": "Gemini",
    "perplexity": "Perplexity",
    "copilot": "Copilot",
    "deepseek": "DeepSeek",
}
_LOCAL_CONNECT_CLIENTS = {
    "cursor": ("Cursor", "cursor"),
    "claude": ("Claude Desktop", "claude-desktop"),
}


def cmd_connect(args) -> int:
    """Print setup instructions for clients that cannot use local stdio wiring."""
    if getattr(args, "connect_cmd", None):
        return args.connect_func(args)
    _err("Choose a client to connect, e.g. `ele connect chatgpt`.")
    return 1


def cmd_connect_local(args) -> int:
    from . import onboarding

    label, target = _LOCAL_CONNECT_CLIENTS[args.client]
    vault = _resolve_vault(args)
    onboard_cmd = f"ele --vault {_quote_cli_arg(str(vault))} onboard --target {target}"
    if target == "claude-desktop":
        config_path = onboarding.default_config_path_for_target(target)
        onboard_cmd += f" --config-path {_quote_cli_arg(str(config_path))}"
    if getattr(args, "name", None):
        onboard_cmd += f" --name {_quote_cli_arg(args.name)}"
    print(f"{label} connection setup\n")
    print("1. Wire EleSync into the local MCP client:")
    print(f"   {onboard_cmd}\n")
    print(f"2. Restart {label} so it loads the new EleSync server.")
    print("\n3. Test it:")
    print('   "What useful memories can you retrieve from EleSync?"')
    print('   "Save this in EleSync: I prefer concise answers."')
    return 0


def cmd_connect_remote(args) -> int:
    label = _REMOTE_CONNECT_CLIENTS[args.client]
    public_url = (args.url or "").strip().rstrip("/")
    allowed_host = (args.allowed_host or "").strip()
    if not public_url and not allowed_host:
        _err(
            f"{label} needs a reachable HTTPS URL. Pass one of:\n"
            f"    ele connect {args.client} --url https://your-domain.example/mcp\n"
            f"    ele connect {args.client} --allowed-host your-tunnel.example"
        )
        return 1

    if public_url and not (
        public_url.startswith("https://")
        or (getattr(args, "insecure", False) and public_url.startswith("http://"))
    ):
        _err(
            f"{label} connector URLs should use HTTPS. If you are only testing locally, "
            "re-run with --insecure to print an http:// URL anyway."
        )
        return 1

    if public_url:
        from urllib.parse import urlparse

        parsed = urlparse(public_url)
        if not parsed.netloc:
            _err("Invalid --url. Expected a full URL like https://your-domain.example/mcp")
            return 1
        connector_url = public_url if parsed.path else f"{public_url}/mcp"
        host_for_serve = allowed_host or parsed.netloc
    else:
        scheme = "http" if getattr(args, "insecure", False) else "https"
        connector_url = f"{scheme}://{allowed_host}/mcp"
        host_for_serve = allowed_host

    force_token = getattr(args, "force_token", False)
    try:
        token, generated = _configured_or_new_token(force=force_token)
    except ValueError as e:
        _err(str(e))
        return 1
    port = getattr(args, "port", 8000)
    vault = _resolve_vault(args)
    serve_cmd = (
        f"ele --vault {_quote_cli_arg(str(vault))} serve --transport http --port {port} "
        f"--allowed-host {_quote_cli_arg(host_for_serve)}"
    )

    print(f"{label} connection setup\n")
    if generated:
        print(f"1. Token created and stored in {_config_path()}.")
    else:
        print("1. Existing Bearer token will be used.")
    print("   Copy this token now:")
    print(f"   {token}\n")
    print("2. Start EleSync's MCP HTTP server:")
    print(f"   {serve_cmd}\n")
    print("3. If you are using a tunnel or reverse proxy, point it at:")
    print(f"   http://127.0.0.1:{port}\n")
    print(f"4. In {label}, add a custom MCP connector with:")
    print(f"   Server URL: {connector_url}")
    print("   Authentication header:")
    print(f"   Authorization: Bearer {token}\n")
    print(f"5. Test it in {label}:")
    print('   "What useful memories can you retrieve from EleSync?"')
    print('   "Save this in EleSync: I prefer concise answers."')
    return 0


def cmd_connect_chatgpt(args) -> int:
    args.client = "chatgpt"
    return cmd_connect_remote(args)


_LOOPBACK_HOSTS = frozenset({"127.0.0.1", "::1", "[::1]", "localhost", "0"})


def _normalize_allowed_host(value: str) -> str:
    """Reduce an --allowed-host value to a bare lowercase hostname."""
    name = value.strip().lower()
    if name.startswith("[") and "]" in name:  # [::1]:8000 -> [::1]
        return name[: name.index("]") + 1]
    if name.count(":") == 1:  # host:port -> host
        name = name.split(":", 1)[0]
    return name


def public_exposure_reasons(host: str, allowed_hosts: list[str] | None) -> list[str]:
    """Reasons this server would be reachable from off this machine.

    Two independent signals, because binding loopback does NOT mean private:
    a tunnel (ngrok, Cloudflare, an SSH -R) forwards a public hostname to
    127.0.0.1, and `--allowed-host <public-name>` is the user telling us
    exactly that. Keying the guard on the bind address alone missed the
    single most common way an EleSync vault reaches the internet.
    """
    reasons: list[str] = []
    if host.strip().lower() not in _LOOPBACK_HOSTS:
        reasons.append(f"bound to {host}, which is not loopback")
    for raw in allowed_hosts or []:
        name = _normalize_allowed_host(raw)
        if name and name not in _LOOPBACK_HOSTS:
            reasons.append(f"--allowed-host {raw} trusts a non-local hostname (tunnel or proxy)")
    return reasons


def cmd_serve(args) -> int:
    transport = getattr(args, "transport", "stdio")
    host = getattr(args, "host", "127.0.0.1")
    port = getattr(args, "port", 8000)
    allowed_hosts = getattr(args, "allowed_host", None)

    # Refuse to expose an unauthenticated HTTP transport off this machine.
    # The MCP surface includes remember/forget, so an open endpoint is not
    # merely a read leak -- it is anonymous write and delete access to the vault.
    if transport in ("http", "sse"):
        from . import auth as _auth_mod

        reasons = public_exposure_reasons(host, allowed_hosts)
        if reasons:
            token = _auth_mod.get_configured_token(_load_config())
            mtls = _auth_mod.get_mtls_config(_load_config())
            if not token and not mtls:
                detail = "\n".join(f"      - {r}" for r in reasons)
                print(
                    "❌  Refusing to start: this server would be reachable off this "
                    "machine with no authentication.\n"
                    f"{detail}\n"
                    "    The MCP transport exposes remember and forget, so anyone who "
                    "reaches it can read, write and delete your vault.\n\n"
                    "    Fix it:  ele auth generate-token   (Bearer token)\n"
                    "         or  ele auth setup-mtls        (mutual TLS)\n"
                    "    Or override deliberately:  ele serve ... --insecure",
                    file=sys.stderr,
                )
                if not getattr(args, "insecure", False):
                    return EXIT_PRECONDITION
                print(
                    "\n⚠️  --insecure given: starting anyway with NO authentication.",
                    file=sys.stderr,
                )
        if host.strip().lower() not in _LOOPBACK_HOSTS and not _auth_mod.get_mtls_config(
            _load_config()
        ):
            if not getattr(args, "insecure", False):
                _err(
                    "Refusing to expose MCP over unencrypted HTTP. Configure mTLS "
                    "with `ele auth setup-mtls`, or bind to 127.0.0.1 behind a TLS proxy. "
                    "Use --insecure only to deliberately allow unencrypted network access."
                )
                return EXIT_PRECONDITION
            print("⚠️  --insecure given: network transport has NO TLS.", file=sys.stderr)

    try:
        from .mcp_server import build_server

        server = build_server(
            str(_resolve_vault(args)), host=host, port=port, allowed_hosts=allowed_hosts
        )
    except ImportError as e:
        _err(e)
        return 1

    if transport == "stdio":
        server.run()
    elif transport == "http":
        print(f"🌐  EleSync MCP server (Streamable HTTP) → http://{host}:{port}/mcp")
        print("    Point a remote MCP client (e.g. ChatGPT Developer Mode) at that URL.")
        print(
            f"    For clients off this machine, tunnel it (e.g. `ngrok http {port}`) "
            "and pass --allowed-host <public-hostname>."
        )
        print("    A tunnel makes this URL public: set a token first (ele auth generate-token).")
        server.run(transport="streamable-http")
    else:  # sse
        print(f"🌐  EleSync MCP server (SSE, legacy) → http://{host}:{port}/sse")
        server.run(transport="sse")
    return 0


def cmd_web(args) -> int:
    if getattr(args, "web_cmd", None):
        return args.web_func(args)

    from .web import serve

    serve(
        vault_dir=getattr(args, "vault", None),
        host=getattr(args, "host", "127.0.0.1"),
        port=getattr(args, "port", 7477),
        open_browser=not getattr(args, "no_browser", False),
        lang=get_language(),
        tls_cert=getattr(args, "tls_cert", None),
        tls_key=getattr(args, "tls_key", None),
        allow_insecure_network=getattr(args, "allow_insecure_network", False),
    )
    return 0


def cmd_demo(args) -> int:
    """Open the dashboard on a throwaway vault with presenter keys 0–9."""
    import tempfile

    from . import demo
    from .web import serve

    root = (
        Path(args.dir).expanduser()
        if getattr(args, "dir", None)
        else Path(tempfile.mkdtemp(prefix="elesync-demo-"))
    )
    vault = root / "EleSyncVault"
    # The presenter keys write fake conversations, projects and access grants.
    # `<dir>/EleSyncVault` is also the default real vault's name (e.g. `--dir ~`),
    # so only reuse a folder that is empty or was created by `ele demo`.
    # The marker lives inside the demo vault, so it is removed with it and can
    # never vouch for a real vault created later at the same path.
    marker = vault / ".elesync-demo"
    if vault.exists() and any(vault.iterdir()) and not marker.exists():
        print(
            f"Refusing to run the demo in {vault}: it already holds a vault that "
            "`ele demo` did not create. Pick an empty folder, or omit --dir to use "
            "a new temporary one.",
            file=sys.stderr,
        )
        return 2
    vault.mkdir(parents=True, exist_ok=True)
    marker.write_text("Created by `ele demo`. Safe to delete.\n", encoding="utf-8")
    # Keep the demo's settings out of the real EleSync config.
    config_home = root / "config"
    config_home.mkdir(parents=True, exist_ok=True)
    os.environ["APPDATA"] = str(config_home)
    os.environ.pop("ELESYNC_BACKUP_DIR", None)
    # A real backup of the demo vault, so the health row starts out green.
    from ._parser import build_parser

    backup_args = build_parser().parse_args(["--vault", str(vault), "backup"])
    backup_args.func(backup_args)

    print(f"Demo vault: {vault}")
    print("Presenter keys (press outside text fields; nothing is shown on screen):")
    for step in demo.STEPS:
        print(f"  {step['key']}  {step['label']}")
    print("  `  show or hide this list in the browser")
    serve(
        vault_dir=str(vault),
        host="127.0.0.1",
        port=args.port,
        open_browser=not args.no_browser,
        lang=get_language(),
        demo=True,
    )
    return 0


# --- web-user commands live in commands/web_users.py -------------------------


# --- web-user commands live in commands/web_users.py -------------------------


def _get_web_password(confirm: bool) -> str:
    from .commands.web_users import get_web_password

    return get_web_password(confirm)


def cmd_web_adduser(args) -> int:
    from .commands.web_users import cmd_web_adduser as implementation

    return implementation(args)


def cmd_web_removeuser(args) -> int:
    from .commands.web_users import cmd_web_removeuser as implementation

    return implementation(args)


def cmd_web_listusers(args) -> int:
    from .commands.web_users import cmd_web_listusers as implementation

    return implementation(args)


def cmd_onboard(args) -> int:
    from . import onboarding

    vault = str(_resolve_vault(args))
    target = getattr(args, "target", None) or "claude-desktop"
    cfg = (
        Path(args.config_path)
        if args.config_path
        else onboarding.default_config_path_for_target(target)
    )
    name = args.name or onboarding.SERVER_KEY

    # Make sure the vault exists.
    MemoryStore(vault).close()

    if args.print_only:
        print(
            onboarding.print_snippet_for_target(
                target,
                vault,
                scopes=args.scopes,
                write_scope=args.write_scope,
                name=name,
                audit=args.audit,
                project_id=args.project_id,
            )
        )
        return 0

    if target == "generic":
        # Generic target only prints a snippet — there's no single config file to write to.
        print(
            onboarding.print_snippet_for_target(
                target,
                vault,
                scopes=args.scopes,
                write_scope=args.write_scope,
                name=name,
                audit=args.audit,
                project_id=args.project_id,
            )
        )
        return 0

    res = onboarding.wire_client_config(
        cfg,
        vault,
        scopes=args.scopes,
        write_scope=args.write_scope,
        name=name,
        target=target,
        audit=args.audit,
        project_id=args.project_id,
    )
    print(_("onboarded"))
    print(_("onboard_vault", str(Path(vault).expanduser())))
    bak = _("onboard_backed_up") if res["backed_up"] else ""
    print(_("onboard_config", res["config_path"], bak))
    if res["updated_existing"]:
        print(_("onboard_server_updated", res["server_key"]))
    else:
        print(_("onboard_server_added", res["server_key"]))
    if args.scopes:
        print(_("onboard_scope", scopes_mod.describe(scopes_mod.grant_from(args.scopes))))
    print(_("onboard_next"))
    _target_restart_hint(target)
    print("Verify anytime with:  ele doctor")
    return 0


def _target_restart_hint(target: str) -> None:
    """Print a target-specific hint about restarting the client."""
    hints = {
        "claude-desktop": "Then restart your AI client so it picks up the new server.",
    }
    hint = hints.get(target)
    if hint:
        print(hint)


def cmd_doctor(args) -> int:
    from . import onboarding

    cfg = Path(args.config_path) if args.config_path else None
    if args.fix:
        fixes = onboarding.fix_doctor(str(_resolve_vault(args)), config_path=cfg)
        if fixes:
            print("Fixes applied:")
            for fix in fixes:
                print(f"  - {fix}")
            print()
        else:
            print("No fixable issues found.")
            print()

    rows = onboarding.run_doctor(str(_resolve_vault(args)), config_path=cfg)
    mark = {"PASS": "✅", "WARN": "⚠️ ", "FAIL": "❌"}
    print(_("doctor_title"))
    for status, check, detail in rows:
        print(f"  {mark[status]} {check:<32} {detail}")
    failed = [r for r in rows if r[0] == "FAIL"]
    print("\n" + (_("doctor_fail") if failed else _("doctor_core_ok")))
    return 1 if failed else 0


def cmd_usb_list(args) -> int:
    from . import usb

    drives = usb.list_removable_drives()
    if not drives:
        print(_("usb_none_detected"))
        print(_("usb_plug_tip"))
        return 0
    print(
        f"{_('usb_col_drive'):<20} {_('usb_col_label'):<20} {_('usb_col_size'):>6}  "
        f"{_('usb_col_fs'):<8}  {_('usb_col_vault')}"
    )
    print("-" * 70)
    for d in drives:
        vault = "🐘 EleSync" if usb.is_usb_vault(d["path"]) else "—"
        print(f"{d['path']:<20} {d['label']:<20} {d['size_gb']:>5.1f}G  {d['fs']:<8}  {vault}")
    return 0


def cmd_usb_install(args) -> int:
    from . import usb

    drive = args.drive
    passphrase = _get_passphrase(confirm=True)
    print(f"\n🐘  Setting up EleSync portable vault on {drive} …")
    try:
        result = usb.install(drive, passphrase, label=args.label or "USB")
    except (OSError, ValueError) as e:
        _err(e)
        return 1
    except RuntimeError as e:
        _err(e)
        return 1
    print(f"✅  Vault created:   {result['vault_dir']}")
    print("   Encrypted:      yes (XSalsa20-Poly1305 + argon2id)")
    print(f"   Launcher:       {result['launcher']}")
    print(f"   README:         {result['readme']}")
    print()
    print("Next steps:")
    print(f"  Import memories:  ele --vault {result['vault_dir']} import <file> --source chatgpt")
    print(f"  Connect Claude:   ele usb attach {drive}")
    print("  On another machine: plug in the drive and run  python RUN_ME.py")
    return 0


def cmd_usb_attach(args) -> int:
    from . import usb

    drive = args.drive
    passphrase = _get_passphrase(confirm=False)
    print(f"\n🔌  Attaching USB vault from {drive} …")
    try:
        result = usb.attach(drive, passphrase, name=args.name or "elesync-usb")
    except (FileNotFoundError, RuntimeError, ValueError) as e:
        _err(e)
        return 1
    print(f"✅  Vault wired into your AI client as '{result['server_key']}'.")
    print(f"   Config: {result['config_path']}")
    print()
    print("Restart your AI client — your USB memories are live.")
    print(f"When done, run:  ele usb detach {drive}  (before unplugging)")
    return 0


def cmd_usb_detach(args) -> int:
    from . import usb

    drive = args.drive
    result = usb.detach(drive, name=args.name or "elesync-usb")
    if result["removed"]:
        print("✅  USB vault disconnected from your AI client.")
        print(f"   Config: {result['config_path']}")
        print()
        print("You can now safely eject the drive.")
        if args.eject:
            print(usb.eject(drive))
    else:
        print(f"ℹ️   {result.get('reason', 'Nothing to detach.')}")
    return 0


def cmd_usb_status(args) -> int:
    from . import usb

    drive = args.drive
    s = usb.status(drive)
    print(_("usb_status_drive", s["drive"]))
    print(_("usb_status_vault_present", _("yes") if s["vault_present"] else _("no")))
    if s["vault_present"]:
        print(
            _(
                "usb_status_encrypted",
                _("yes") if s["encrypted"] else _("usb_status_encrypted_no"),
            )
        )
        print(_("usb_status_memory_files", s["note_count"]))
        print(
            _(
                "usb_status_launcher",
                _("usb_launcher_present") if s["launcher_present"] else _("usb_launcher_missing"),
            )
        )
    print(_("usb_status_free_space", s["size_gb"]))
    return 0


def cmd_usb_eject(args) -> int:
    from . import usb

    print(usb.eject(args.drive))
    return 0


def cmd_usb(args) -> int:
    """Dispatch USB sub-subcommands."""
    return args.usb_func(args)


# ── vault management ─────────────────────────────────────────────────────────────


def cmd_vault_list(args) -> int:
    """List all known named vaults."""
    cfg = _load_config()
    vaults = cfg.get("vaults", {})
    active = cfg.get("vault_dir") or os.environ.get("ELESYNC_DIR", "~/EleSyncVault")
    active_expanded = str(Path(active).expanduser())
    if not vaults:
        print(_("vault_none_registered"))
        return 0
    print(f"{_('vault_col_name'):<20} {_('vault_col_path'):<50} {_('vault_col_status')}")
    print("-" * 80)
    for name, path in sorted(vaults.items()):
        expanded = str(Path(path).expanduser())
        exists = "✓" if Path(path).expanduser().exists() else _("vault_not_found_marker")
        active_marker = _("vault_active_marker") if expanded == active_expanded else ""
        print(f"{name:<20} {path:<50} {exists}{active_marker}")
    return 0


def cmd_vault_current(args) -> int:
    """Show the active vault."""
    cfg = _load_config()
    active = cfg.get("vault_dir") or os.environ.get("ELESYNC_DIR", "~/EleSyncVault")
    # Find the name for the current vault, if registered
    vaults = cfg.get("vaults", {})
    active_expanded = str(Path(active).expanduser())
    name = None
    for n, p in vaults.items():
        if str(Path(p).expanduser()) == active_expanded:
            name = n
            break
    if name:
        print(f"{name}: {active}")
    else:
        print(active)
    return 0


def cmd_vault_add(args) -> int:
    """Register a vault by name."""
    cfg = _load_config()
    vaults = cfg.setdefault("vaults", {})
    name = args.vault_name
    path = args.vault_path
    if name in vaults:
        print(_("vault_already_registered", name, vaults[name]))
        print(_("vault_remove_first", name))
        return 1
    vaults[name] = path
    _save_config(cfg)
    print(_("vault_registered", name, path))
    return 0


def cmd_vault_remove(args) -> int:
    """Remove a vault from the registry."""
    cfg = _load_config()
    vaults = cfg.get("vaults", {})
    name = args.vault_name
    if name not in vaults:
        print(_("vault_not_found_named", name))
        return 1
    removed_path = vaults.pop(name)
    # If this was the active vault, clear vault_dir
    active = cfg.get("vault_dir", "")
    if active and str(Path(active).expanduser()) == str(Path(removed_path).expanduser()):
        print(_("vault_removed_was_active", name))
        del cfg["vault_dir"]
    cfg["vaults"] = vaults
    _save_config(cfg)
    print(_("vault_removed", name, removed_path))
    return 0


def cmd_vault_switch(args) -> int:
    """Set the active vault by name."""
    cfg = _load_config()
    vaults = cfg.get("vaults", {})
    name = args.vault_name
    if name not in vaults:
        print(_("vault_not_found_named", name))
        return 1
    path = vaults[name]
    cfg["vault_dir"] = path
    _save_config(cfg)
    print(_("vault_switched", name, path))
    return 0


def cmd_vault(args) -> int:
    """Dispatch vault sub-subcommands."""
    return args.vault_func(args)


def cmd_summary(args) -> int:
    print(
        build_summary(
            vault_dir=str(_resolve_vault(args)),
            source=args.source,
            short=args.short,
        )
    )
    return 0


def cmd_uninstall(args) -> int:
    from . import onboarding

    vault = _resolve_vault(args)
    cfg_path = (
        Path(args.config_path) if args.config_path else onboarding.default_client_config_path()
    )

    # Remove the MCP config entry.
    res = onboarding.unwire_entry(cfg_path, name=args.name or onboarding.SERVER_KEY)
    if res["removed"]:
        print(f"Removed '{res['server_key']}' from {res['config_path']}")
    else:
        print(f"No '{res['server_key']}' entry found in {res['config_path']} — nothing to remove.")

    # Handle the vault.
    if args.delete_vault:
        import shutil

        backup = None
        if args.backup_first and vault.exists():
            backup = vault.with_name(vault.name + ".bak")
            if backup.exists():
                backup = vault.parent / f"{vault.name}.bak.{int(__import__('time').time())}"
            print(f"Backing up vault to {backup} …")
            shutil.copytree(vault, backup)
        if vault.exists():
            shutil.rmtree(vault)
            print(f"Deleted vault at {vault}")
            if backup:
                print(f"Backup saved at {backup}")
        else:
            print(f"No vault found at {vault} — nothing to delete.")
    else:
        print(f"Vault left in place at {vault}")
        print("Tip: run `ele uninstall --delete-vault` to also remove it.")

    print("\nDone. To fully remove the package:  pip uninstall elesync")
    return 0


from ._parser import build_parser  # noqa: E402


def _resolve_vault_dir() -> Path:
    return _resolve_vault().resolve()


def _welcome() -> int:
    """Open the dashboard when ``ele`` is run without a subcommand."""
    vp = _resolve_vault_dir()
    store = None
    no_imports = _("welcome_no_imports")

    if hasattr(sys, "frozen") and not _load_config().get("vault_dir"):
        return _setup_vault(vp)

    try:
        if (vp / "notes").exists() and any((vp / "notes").iterdir()):
            store = MemoryStore(str(vp))
            stats = store.stats()
            total = stats["total"]
            by_source = stats["by_source"]
            src_line = (
                ", ".join(f"{k}: {v}" for k, v in sorted(by_source.items()))
                if by_source
                else no_imports
            )
        else:
            total = 0
            src_line = no_imports
    except Exception:
        _log.debug("failed to compute welcome stats", exc_info=True)
        total = 0
        src_line = "—"
    finally:
        if store:
            store.close()

    print(_("welcome_title"))
    print(_("welcome_vault", str(vp)))
    print(_("welcome_memories", total, src_line))
    print()
    print(_("welcome_quickstart"))
    print(_("welcome_import"))
    print(_("welcome_search"))
    print(_("welcome_summary"))
    print(_("welcome_web"))
    print(_("welcome_onboard"))
    print()
    print(_("welcome_help"))
    print()
    print(_("privacy_pledge"))
    print("🌐  Launching web UI in your browser…")
    from .web import serve

    serve(vault_dir=vp, port=7477, open_browser=True, lang=get_language())
    return 0


def _setup_vault(default_path: Path) -> int:
    """First-run setup for standalone exe: let user choose vault path."""
    from .i18n import _ as _tr

    print("🐘  EleSync — first-time setup\n")
    print(_tr("welcome_vault", str(default_path)))
    print()
    chosen = default_path
    while True:
        ans = input("Use this path? [Y/n] ").strip().lower()
        if ans in ("", "y", "yes"):
            break
        elif ans in ("n", "no"):
            custom = input("Enter vault path (e.g. D:\\MyVault) [Enter = cancel]: ").strip()
            if custom:
                chosen = Path(custom).expanduser().resolve()
                break
            print("  Keeping default path. Use ELESYNC_DIR env var to change it later.")
            break
        else:
            print("  Please answer Y or n.")
    print()
    try:
        chosen.mkdir(parents=True, exist_ok=True)
        (chosen / "notes").mkdir(parents=True, exist_ok=True)
        _save_config({"vault_dir": str(chosen)})
        print(f"✓ Vault ready at: {chosen}")
    except OSError as e:
        _err(f"Could not create vault: {e}")
        return 1
    print()
    print("Quick start:")
    print("  Go to the Import tab in the web UI and drop AI export files")
    print("  Or use the CLI: ele import <file> --source chatgpt")
    print("  See: https://elesync.dev/guide\n")
    print(_("privacy_pledge"))
    print("\n🌐  Launching web UI in your browser…")
    from .web import serve

    serve(vault_dir=chosen, port=7477, open_browser=True, lang=get_language())
    return 0


def _init_language(args) -> None:
    """The CLI ships English-only strings, so language resolution is a no-op.
    Kept as a seam so a future localization layer can hook in here without
    touching ``main()``. The ``--lang`` flag (if still registered) is
    accepted but ignored — the web dashboard handles its own localization."""
    set_language("en")


#: Top-level options that consume a following value. `_command_index` needs this to
#: find the subcommand token without re-implementing argparse's tokenizer.
#: `TestBackupListDispatch.test_value_globals_matches_parser` fails if a new valued
#: global is added without being listed here, so it cannot drift silently.
_VALUE_GLOBALS = frozenset({"--vault", "--as"})


def _command_index(argv: list[str]) -> int | None:
    """Index of the subcommand token in ``argv``, skipping global options and their
    values. ``--vault=X`` needs no special case: it's a single token."""
    i = 0
    while i < len(argv):
        if argv[i].startswith("-"):
            i += 2 if argv[i] in _VALUE_GLOBALS else 1
            continue
        return i
    return None


def _rewrite_backup_list(argv: list[str]) -> list[str]:
    """Map `ele backup list/push/pull …` onto the `backup-list/push/pull` parsers.

    `backup` already owns an optional positional path, and argparse cannot offer both
    a positional and subcommands — it would swallow `list`/`push`/`pull` as the
    destination filename. Rewriting these argv shapes keeps the published
    `ele backup <path>` surface intact. Only the bare word is rewritten, so
    `ele backup ./list` still backs up to a file named `list`.
    """
    i = _command_index(argv)
    if (
        i is not None
        and argv[i] == "backup"
        and i + 1 < len(argv)
        and argv[i + 1]
        in (
            "list",
            "push",
            "pull",
        )
    ):
        sub = argv[i + 1]
        return [*argv[:i], f"backup-{sub}", *argv[i + 2 :]]
    return argv


def main(argv=None) -> int:
    # Some terminals (notably the legacy Windows console at cp1252) can't encode
    # the ✅/⚠️ marks doctor/onboard print, which would crash with UnicodeEncodeError.
    # Encoding output as UTF-8 keeps it crash-free everywhere.
    for stream in (sys.stdout, sys.stderr):
        with contextlib.suppress(AttributeError, ValueError):
            stream.reconfigure(encoding="utf-8")  # type: ignore[union-attr]
    argv = list(sys.argv[1:] if argv is None else argv)
    args = build_parser().parse_args(_rewrite_backup_list(argv))
    _init_language(args)
    if getattr(args, "cmd", None) is None:
        return _welcome()
    return args.func(args)


if __name__ == "__main__":
    sys.exit(main())
