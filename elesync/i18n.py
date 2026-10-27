"""Lightweight i18n stub for user-facing CLI messages.

The CLI ships English-only strings. The web dashboard handles its own
localization (see ``index.html``'s EN/ES switcher) — this module is the
CLI's string catalog and nothing more.

Usage:
    from .i18n import _, set_language, get_language
    print(_("welcome_title"))
    print(_("vault_path", vault="/home/user/EleSyncVault"))

The ``set_language`` / ``get_language`` / ``LANGUAGES`` symbols are kept
as no-ops/single-entry stubs because call sites across the CLI and web
server still import them. ``_()`` always returns the English string.
"""

from __future__ import annotations

import logging

_log = logging.getLogger("elesync.i18n")

LANGUAGES: dict[str, str] = {"en": "English"}

_current_lang: str = "en"

# ── English string catalog ──────────────────────────────────────────────
# Each key maps to {"en": "..."}. {0}, {1} etc are positional format args.
# Keys are retained verbatim from the previous 7-language catalog so every
# existing ``_("key", *args)`` call site keeps working unchanged.

_T: dict[str, dict[str, str]] = {
    "restored_templates": {"en": "Restored {0} template(s) into <vault>/templates."},
    "template_list_header": {
        "en": "{0} templates available (user files in <vault>/templates override built-ins):"
    },
    "template_unreadable": {"en": "{0} template file(s) could not be read:"},
    "template_body_fields": {"en": "  fields: {0}"},
    "template_saved": {"en": "Saved template '{0}' to {1}."},
    "template_removed": {"en": "Removed template '{0}'."},
    "template_restored_builtin": {
        "en": "Removed your '{0}' override; the built-in template is active again."
    },
    "template_no_file": {
        "en": "No user template file named '{0}'. Built-in templates can be overridden but not deleted."
    },
    "add_needs_content": {
        "en": "Nothing to save: provide content, or use --template with --set values."
    },
    "select_language": {
        "en": "Select your language / Seleccione su idioma / Choisissez votre langue / Wählen Sie Ihre Sprache / Selecione seu idioma / 选择您的语言 / 言語を選択してください"
    },
    "lang_number": {"en": "{0}. {1}"},
    "lang_prompt": {"en": "Enter the number of your language"},
    "lang_set": {"en": "Language set to {0}."},
    "lang_invalid": {"en": "Invalid choice, defaulting to English."},
    "welcome_title": {"en": "🐘  EleSync — your AI memory vault"},
    "welcome_vault": {"en": "   Vault: {0}"},
    "welcome_memories": {"en": "   Memories: {0} ({1})"},
    "welcome_quickstart": {"en": "   Quick start:"},
    "welcome_import": {"en": "     ele import <file>   Import memories from an AI export"},
    "welcome_search": {"en": "     ele search <query>  Search across all your memories"},
    "welcome_summary": {"en": "     ele summary         See everything your vault knows"},
    "welcome_web": {"en": "     EleSync dashboard   Opens automatically in your browser"},
    "welcome_onboard": {"en": "     ele onboard         Wire into your AI client (one command)"},
    "welcome_help": {"en": "   Need help?  ele <command> --help   or visit  https://elesync.dev"},
    "welcome_no_imports": {"en": "no imports yet"},
    "onboarded": {"en": "✅ Onboarded."},
    "onboard_vault": {"en": "   Vault:  {0}"},
    "onboard_config": {"en": "   Config: {0}{1}"},
    "onboard_backed_up": {"en": " (existing backed up to *.bak)"},
    "onboard_server_updated": {"en": "   Server '{0}' updated."},
    "onboard_server_added": {"en": "   Server '{0}' added."},
    "onboard_scope": {"en": "   Scope:  {0}"},
    "onboard_next": {"en": "\nNext: restart your MCP client so it picks up the new server."},
    "onboard_quit": {"en": "Then restart your AI client so it picks up the new server."},
    "doctor_title": {"en": "EleSync — health check\n"},
    "doctor_core_ok": {"en": "✅ Core looks good. WARNs are optional/until you serve."},
    "doctor_fail": {"en": "❌ Fix the FAIL items above."},
    "paste_prompt": {
        "en": "Paste memories below, then press Ctrl+D (Mac/Linux) or Ctrl+Z+Enter (Windows) to finish:"
    },
    "paste_empty": {"en": "No text provided."},
    "imported": {"en": "Imported {0} new memories ({1} duplicates skipped). Vault now holds {2}."},
    "exported": {"en": "Exported {0} memories to {1}. Re-import with: ele import {1}"},
    "backed_up": {
        "en": "Backed up {0} memories (including history) to {1}. Restore with: ele restore {1}"
    },
    "backup_write_failed": {"en": "Could not write the backup to {0}: {1}"},
    "backup_pruned": {"en": "Retention: removed {0} older backup(s), keeping the {1} most recent."},
    "backup_manifest_failed": {
        "en": "⚠️  Backup written, but its manifest entry could not be saved: {0}. Retention was skipped — no old backup was deleted."
    },
    "backup_list_header": {"en": "Backups in {0} (newest first):"},
    "backup_list_empty": {"en": "No backups in {0}. Create one with: ele backup"},
    "backup_mark_unrecorded": {"en": "not in manifest"},
    "backup_mark_missing": {"en": "file missing"},
    "backup_mark_unreadable": {"en": "unreadable"},
    "backup_mark_verified": {"en": "checksum ok"},
    "backup_mark_changed": {"en": "CHECKSUM MISMATCH"},
    "backup_checksum_changed": {
        "en": "⚠️  {0} does not match the checksum recorded when it was written — it has been altered or truncated since. Continuing anyway."
    },
    "restored": {"en": "Restored {0} memories ({1} already present). Vault now holds {2}."},
    "reindexed": {"en": "Reindexed {0} memories from notes/ in {1}."},
    "synced": {
        "en": "Synced the index with notes/ — {0} added, {1} changed, {2} removed. Vault holds {3}."
    },
    "saved": {"en": "Saved id={0}{1}."},
    "saved_scope": {"en": " in scope {0}"},
    "already_exists": {"en": "Already in your vault (id={0}) — nothing added."},
    "no_matches": {"en": "No matches."},
    "forget_deleted": {"en": "Deleted."},
    "error_wrong_passphrase": {"en": "Wrong passphrase — could not unlock the vault."},
    "error_encryption_needed": {
        "en": 'This vault is encrypted, which needs PyNaCl: pip install "pynacl>=1.5"'
    },
    "error_encryption_meta_missing": {
        "en": "This vault looks encrypted, but its encryption details (vault.json) are missing or unreadable. If you moved or restored the vault, make sure vault.json is in the vault folder next to the notes/ directory."
    },
    "error_semantic_unavailable": {
        "en": 'Semantic mode unavailable (the model couldn\'t be loaded — check your connection). Falling back to keyword search. Semantic mode needs fastembed: pip install "fastembed>=0.7.0,<0.9.0".'
    },
    "error_no_embedder": {
        "en": 'Could not load the embedding model. Make sure fastembed is installed (pip install "fastembed>=0.7.0,<0.9.0") and that you are online for the first run, which downloads the model.'
    },
    "error_empty_passphrase": {"en": "Empty passphrase — aborting."},
    "error_passphrase_mismatch": {"en": "Passphrases didn't match — aborting."},
    "error_already_encrypted": {"en": "Vault is already encrypted."},
    "error_no_notes": {"en": "No notes found in {0} — nothing to encrypt."},
    "error_not_encrypted": {"en": "Vault is not encrypted."},
    "error_encryption_extra": {"en": 'Encryption needs PyNaCl: pip install "pynacl>=1.5"'},
    "error_bad_archive": {"en": "Could not read this backup archive: {0}"},
    "web_started": {"en": "🌐  EleSync Web UI → {0}"},
    "web_vault": {"en": "📂  Vault: {0}"},
    "web_stop": {"en": "    Press Ctrl+C to stop.\n"},
    "web_stopped": {"en": "\n👋  EleSync web server stopped."},
    "embedding_started": {"en": "Embedding with {0} … (first run downloads the model)"},
    "embedded": {
        "en": "Embedded {0} memories. Semantic recall is active — use it with --semantic or ELESYNC_SEMANTIC=1."
    },
    "privacy_pledge": {
        "en": "🔒  Zero data collection · Zero ads · Zero tracking · Your data stays on your machine, always."
    },
    "state_on": {"en": "on"},
    "state_off": {"en": "off"},
    "yes": {"en": "yes"},
    "no": {"en": "no"},
    "stats_vault": {"en": "Vault: {0}"},
    "stats_total": {"en": "Total memories: {0}   (FTS search: {1})"},
    "stats_embedded": {"en": "Embedded: {0}/{1}   (semantic recall: {2})"},
    "semantic_available": {"en": "available"},
    "semantic_off_hint": {"en": "off — run: ele embed"},
    "stats_by_source": {"en": "By source: {0}"},
    "stats_by_type": {"en": "By type: {0}"},
    "vault_none_registered": {
        "en": "No named vaults registered. Use 'ele vault add <name> <path>' to add one."
    },
    "vault_col_name": {"en": "Name"},
    "vault_col_path": {"en": "Path"},
    "vault_col_status": {"en": "Status"},
    "vault_not_found_marker": {"en": "✗ (not found)"},
    "vault_active_marker": {"en": " ◀ active"},
    "vault_already_registered": {"en": "Vault '{0}' already registered at {1}."},
    "vault_remove_first": {"en": "Use 'ele vault remove {0}' first if you want to replace it."},
    "vault_registered": {"en": "✓ Vault '{0}' registered at {1}."},
    "vault_not_found_named": {
        "en": "No vault named '{0}' found. Run 'ele vault list' to see registered vaults."
    },
    "vault_removed_was_active": {
        "en": "Note: removed vault '{0}' was the active vault. No active vault set."
    },
    "vault_removed": {"en": "✓ Vault '{0}' ({1}) removed from registry."},
    "vault_switched": {"en": "✓ Active vault switched to '{0}' ({1})."},
    "usb_none_detected": {"en": "No removable drives detected."},
    "usb_plug_tip": {"en": "Tip: plug in your USB drive first, then run `ele usb list` again."},
    "usb_col_drive": {"en": "Drive"},
    "usb_col_label": {"en": "Label"},
    "usb_col_size": {"en": "Size"},
    "usb_col_fs": {"en": "FS"},
    "usb_col_vault": {"en": "Vault"},
    "usb_status_drive": {"en": "Drive: {0}"},
    "usb_status_vault_present": {"en": "Vault present: {0}"},
    "usb_status_encrypted": {"en": "Encrypted: {0}"},
    "usb_status_encrypted_no": {"en": "NO — run ele usb install to fix"},
    "usb_status_memory_files": {"en": "Memory files: {0}"},
    "usb_status_launcher": {"en": "Launcher: {0}"},
    "usb_launcher_present": {"en": "present"},
    "usb_launcher_missing": {"en": "missing — run ele usb install"},
    "usb_status_free_space": {"en": "Free space: {0} GB"},
}


def set_language(lang: str) -> None:
    """No-op stub. The CLI is English-only; only 'en' is accepted and any
    other value silently falls back to English. Kept for call-site
    compatibility (cli.py, web.py, tests)."""
    global _current_lang
    _current_lang = "en"


def get_language() -> str:
    return _current_lang


def _(key: str, *args, **kwargs) -> str:
    """Look up an English string by key. Falls back to the key itself if
    missing. Accepts positional and keyword format args."""
    translations = _T.get(key)
    if translations is None:
        return key  # key not found — return raw key
    text = translations.get("en", key)
    if args or kwargs:
        try:
            return text.format(*args, **kwargs)
        except (IndexError, KeyError) as e:
            # A placeholder in the template didn't match the args passed —
            # never crash user-facing text over it; fall back to the raw
            # template and log so the mismatch is debuggable.
            _log.warning("i18n format failed for key %r: %r", key, e)
            return text
    return text
