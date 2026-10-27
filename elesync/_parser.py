"""Argument parser for the EleSync CLI.

Extracted from cli.py to keep that module focused on command implementations.
build_parser() uses lazy imports to avoid a circular dependency: cli.py imports
build_parser from here, and this function imports the command functions from cli.py
at call time (not at module load time).
"""

from __future__ import annotations

import argparse


def build_parser() -> argparse.ArgumentParser:
    # Lazy import: cli.py imports build_parser from this module, so we can't
    # import cli at module level without creating a circular dependency.
    from elesync import __version__
    from elesync.cli import (
        cmd_add,
        cmd_audit,
        cmd_auth,
        cmd_auth_generate_token,
        cmd_auth_revoke,
        cmd_auth_setup_mtls,
        cmd_auth_status,
        cmd_backup,
        cmd_backup_list,
        cmd_backup_pull,
        cmd_backup_push,
        cmd_conflicts,
        cmd_connect,
        cmd_connect_chatgpt,
        cmd_connect_local,
        cmd_connect_remote,
        cmd_decrypt,
        cmd_demo,
        cmd_doctor,
        cmd_embed,
        cmd_encrypt,
        cmd_export,
        cmd_forget,
        cmd_history,
        cmd_import,
        cmd_link,
        cmd_onboard,
        cmd_reindex,
        cmd_related,
        cmd_resolve_conflict,
        cmd_restore,
        cmd_scope,
        cmd_scopes,
        cmd_search,
        cmd_serve,
        cmd_stats,
        cmd_summarize,
        cmd_summary,
        cmd_supersede,
        cmd_sync,
        cmd_template_list,
        cmd_template_new,
        cmd_template_remove,
        cmd_template_show,
        cmd_uninstall,
        cmd_usb,
        cmd_usb_attach,
        cmd_usb_detach,
        cmd_usb_eject,
        cmd_usb_install,
        cmd_usb_list,
        cmd_usb_status,
        cmd_vault,
        cmd_vault_add,
        cmd_vault_current,
        cmd_vault_list,
        cmd_vault_remove,
        cmd_vault_switch,
        cmd_web,
        cmd_web_adduser,
        cmd_web_listusers,
        cmd_web_removeuser,
    )
    from elesync.models import LINK_RELATIONS

    p = argparse.ArgumentParser(
        prog="ele", description="EleSync — local-first, MCP-native unified AI memory vault."
    )
    p.add_argument("--version", action="version", version=f"EleSync {__version__}")
    p.add_argument("--vault", help="Vault directory (default: $ELESYNC_DIR or ~/EleSyncVault)")
    p.add_argument(
        "--semantic",
        action="store_true",
        help="Use semantic (embedding) search; needs the model download on first use",
    )
    p.add_argument(
        "--as",
        dest="as_scopes",
        metavar="SCOPES",
        help="View the vault as a client granted these scopes (e.g. 'general,writing') "
        "— filters reads so you can audit exactly what that client would see",
    )
    sub = p.add_subparsers(dest="cmd")

    pi = sub.add_parser(
        "import",
        help="Import an AI export file, or plain text from stdin with `ele import -`",
    )
    pi.add_argument(
        "path",
        help="Export file path, or '-' to read plain text from stdin "
        "(one memory per paragraph / line, like the dashboard's paste box)",
    )
    pi.add_argument(
        "--source",
        help="Source label for imported memories. For file imports, forces a "
        "source adapter (chatgpt, claude, gemini, grok, deepseek, perplexity, "
        "copilot); otherwise auto-detected. For stdin import (`-`), any value "
        "is accepted as the source label (default: manual).",
    )
    pi.set_defaults(func=cmd_import)

    ps = sub.add_parser("search", help="Search the vault")
    ps.add_argument("query")
    ps.add_argument("--limit", type=int, default=10)
    ps.add_argument("--source")
    ps.add_argument("--type")
    ps.add_argument(
        "--mode",
        choices=["auto", "fts", "semantic", "hybrid"],
        default="auto",
        help="Search mode: auto (hybrid if embeddings exist, else FTS), "
        "fts (keyword only), semantic (vector only), hybrid (fuse both). "
        "Default: auto.",
    )
    ps.add_argument(
        "--semantic-weight",
        type=float,
        default=None,
        help="Balance for hybrid search: 0.0 = pure keyword (FTS), 1.0 = pure "
        "semantic (vector), 0.5 = equal weight. Default: 0.5 or config "
        "search.hybrid_weight. Ignored in pure fts/semantic mode.",
    )
    ps.set_defaults(func=cmd_search)

    pa = sub.add_parser("add", help="Add a memory manually")
    pa.add_argument(
        "content",
        nargs="?",
        help="Memory text. Optional when --template supplies a body to fill with --set.",
    )
    pa.add_argument("--template", help="Capture using a named template (see 'ele template list')")
    pa.add_argument(
        "--set",
        action="append",
        metavar="KEY=VALUE",
        help="Fill a template placeholder; repeat once per field",
    )
    pa.add_argument("--type", default="note")
    pa.add_argument("--tags")
    pa.add_argument("--source")
    pa.add_argument(
        "--scopes", help="Scope compartment(s) for this memory, e.g. 'legal' or 'writing,coding'"
    )
    pa.add_argument(
        "--supersedes",
        help="Id (or 8-char prefix) of an older memory this one replaces; the old "
        "one is kept as history but hidden from recall",
    )
    pa.set_defaults(func=cmd_add)

    ptpl = sub.add_parser("template", help="Pre-defined memory structures for consistent capture")
    tpl_sub = ptpl.add_subparsers(dest="template_cmd", required=True)

    ptpl_list = tpl_sub.add_parser("list", help="List available templates")
    ptpl_list.set_defaults(func=cmd_template_list)

    ptpl_show = tpl_sub.add_parser("show", help="Show one template's fields and body")
    ptpl_show.add_argument("name")
    ptpl_show.set_defaults(func=cmd_template_show)

    ptpl_new = tpl_sub.add_parser(
        "new",
        help="Create or overwrite a template in <vault>/templates/",
    )
    ptpl_new.add_argument("name")
    ptpl_new.add_argument("--body", help="Body skeleton; {placeholders} become --set fields")
    ptpl_new.add_argument("--description")
    ptpl_new.add_argument("--type")
    ptpl_new.add_argument("--tags")
    ptpl_new.add_argument("--scopes")
    ptpl_new.add_argument("--source")
    ptpl_new.add_argument(
        "--from",
        dest="copy_from",
        help="Start from an existing template (including a built-in) and change it",
    )
    ptpl_new.set_defaults(func=cmd_template_new)

    ptpl_rm = tpl_sub.add_parser(
        "remove",
        help="Delete a user template; removing one that shadows a built-in restores the built-in",
    )
    ptpl_rm.add_argument("name")
    ptpl_rm.set_defaults(func=cmd_template_remove)

    pt = sub.add_parser("stats", help="Show vault statistics")
    pt.set_defaults(func=cmd_stats)

    psm = sub.add_parser(
        "summarize",
        help="Summarize a subset of the vault using an LLM (Ollama/OpenAI/Anthropic)",
    )
    psm.add_argument("--tag", help="Filter by tag (comma-separated for multiple)")
    psm.add_argument("--source", help="Filter by source (chatgpt, claude, gemini, etc.)")
    psm.add_argument("--type", help="Filter by type (fact, preference, project, etc.)")
    psm.add_argument(
        "--since",
        type=int,
        help="Only memories from the last N days",
    )
    psm.add_argument(
        "--format",
        choices=["bullet", "narrative", "tldr"],
        default="bullet",
        help="Output format (default: bullet)",
    )
    psm.add_argument(
        "--provider",
        choices=["ollama", "openai", "anthropic"],
        default=None,
        help="LLM provider (default: auto-detect Ollama, else config llm.provider)",
    )
    psm.add_argument(
        "--save-as",
        default=None,
        help="Save the summary as a new memory (flag — no value needed)",
        action="store_true",
    )
    psm.add_argument(
        "--dry-run",
        action="store_true",
        help="Show the prompt that would be sent without calling the API",
    )
    psm.set_defaults(func=cmd_summarize)

    pau = sub.add_parser("audit", help="View the audit trail of vault operations")
    pau_sub = pau.add_subparsers(dest="audit_cmd")
    pau_sub.add_parser("clear", help="Delete the audit log file")
    pau.add_argument("--last", type=int, default=50, help="Number of entries to show (default: 50)")
    pau.add_argument("--operation", help="Filter by operation (REMEMBER, RECALL, FORGET, etc.)")
    pau.add_argument("--client", help="Filter by client label")
    pau.set_defaults(func=cmd_audit)

    pr = sub.add_parser("reindex", help="Rebuild the search index from notes/*.md")
    pr.set_defaults(func=cmd_reindex)

    psy = sub.add_parser("sync", help="Reconcile the index with synced notes and report changes")
    psy.set_defaults(func=cmd_sync)

    pfo = sub.add_parser("forget", help="Delete a memory by id")
    pfo.add_argument("id", help="Full id or 8-char prefix shown by search/recall")
    pfo.set_defaults(func=cmd_forget)

    ph = sub.add_parser("history", help="Show a memory's supersession history (what changed, when)")
    ph.add_argument("id", help="Full id or 8-char prefix")
    ph.set_defaults(func=cmd_history)

    pcf = sub.add_parser("conflicts", help="Surface facts that disagree (with provenance)")
    pcf_sub = pcf.add_subparsers(dest="conflicts_subcmd")
    pcf_list = pcf_sub.add_parser(
        "list",
        help="List unresolved conflicts with cluster IDs for use with resolve-conflict",
    )
    pcf_list.set_defaults(func=cmd_conflicts)
    pcf.set_defaults(func=cmd_conflicts)

    prc = sub.add_parser(
        "resolve-conflict",
        help="Resolve a conflict cluster by choosing a memory to keep or applying a strategy",
    )
    prc.add_argument(
        "cluster_id",
        help="Cluster id (or unambiguous prefix) from `ele conflicts list`",
    )
    rc_group = prc.add_mutually_exclusive_group(required=True)
    rc_group.add_argument(
        "--choose",
        metavar="MEMORY_ID",
        help="Keep this memory (full id or 8-char prefix); delete the others",
    )
    rc_group.add_argument(
        "--strategy",
        choices=["newest", "oldest", "longest"],
        help="Auto-resolve: newest keeps the most recently created, oldest the earliest, "
        "longest keeps the longest content",
    )
    prc.set_defaults(func=cmd_resolve_conflict)

    psup = sub.add_parser(
        "supersede",
        help="Mark one memory as superseded by another (kept as recoverable history)",
    )
    psup.add_argument(
        "old_id",
        help="Memory id (or 8-char prefix) that is now outdated",
    )
    psup.add_argument(
        "--by",
        required=True,
        metavar="MEMORY_ID",
        help="Memory id (or 8-char prefix) that replaces it",
    )
    psup.set_defaults(func=cmd_supersede)

    plink = sub.add_parser(
        "link", help="Link one memory to another (e.g. a decision caused_by its reason)"
    )
    plink.add_argument("source_id", help="Memory id (or 8-char prefix) the link starts from")
    plink.add_argument("target_id", help="Memory id (or 8-char prefix) the link points at")
    plink.add_argument(
        "--relation",
        "-r",
        default="related",
        choices=list(LINK_RELATIONS),
        help="How the source relates to the target (default: related)",
    )
    plink.add_argument("--remove", action="store_true", help="Delete this link instead")
    plink.set_defaults(func=cmd_link)

    prel = sub.add_parser("related", help="Show the memories linked to or from a memory")
    prel.add_argument("id", help="Full id or 8-char prefix shown by search/recall")
    prel.set_defaults(func=cmd_related)

    psc = sub.add_parser("scope", help="Set/replace the scopes of an existing memory")
    psc.add_argument("id", help="Full id or 8-char prefix shown by search/recall")
    psc.add_argument("scopes", nargs="?", help="New scope(s), e.g. 'legal' or 'writing,coding'")
    psc.add_argument(
        "--clear", action="store_true", help="Clear all scopes (back to general/visible-to-all)"
    )
    psc.set_defaults(func=cmd_scope)

    pscs = sub.add_parser("scopes", help="List scope compartments in the vault, with counts")
    pscs.set_defaults(func=cmd_scopes)

    pm = sub.add_parser("embed", help="Embed memories for semantic recall")
    pm.set_defaults(func=cmd_embed)

    pe = sub.add_parser("export", help="Export the whole vault to a portable JSON file")
    pe.add_argument("path", help="Output file, e.g. vault-backup.json")
    pe.set_defaults(func=cmd_export)

    pbk = sub.add_parser(
        "backup",
        help="Back up the whole vault, including history, to a compressed archive",
    )
    pbk.add_argument(
        "path",
        nargs="?",
        default=None,
        help="Destination file (default: a timestamped .eleb in the managed backup directory)",
    )
    pbk.add_argument(
        "--encrypt",
        dest="encrypt",
        action="store_true",
        default=False,
        help="Encrypt the backup (needs encryption support); "
        "passphrase via prompt or $ELESYNC_PASSPHRASE",
    )
    pbk.add_argument(
        "--no-encrypt",
        dest="encrypt",
        action="store_false",
        help="Write an unencrypted (still compressed) backup — the default",
    )
    pbk.add_argument(
        "--dir",
        default=None,
        help="Managed backup directory (default: $ELESYNC_BACKUP_DIR, "
        "config backup.dir, or <vault>/backups)",
    )
    pbk.set_defaults(func=cmd_backup)

    # `ele backup list` reads as a subcommand but `backup` already owns an optional
    # positional path, and argparse cannot offer both (it would swallow "list" as the
    # destination filename). Registering it as its own parser and rewriting that one
    # argv shape in `main` keeps the published `ele backup <path>` surface intact.
    pbl = sub.add_parser(
        "backup-list",
        help="List backups in the managed directory (usage: ele backup list)",
    )
    pbl.add_argument(
        "--dir",
        default=None,
        help="Managed backup directory (default: $ELESYNC_BACKUP_DIR, "
        "config backup.dir, or <vault>/backups)",
    )
    pbl.add_argument(
        "--verify",
        action="store_true",
        help="Re-hash each backup and compare it with the recorded checksum",
    )
    pbl.set_defaults(func=cmd_backup_list)

    # `ele backup push` — same argv-rewrite pattern as `backup list`.
    pbp = sub.add_parser(
        "backup-push",
        help="Push the latest backup to a remote destination via rclone "
        "(usage: ele backup push --destination s3:bucket/path)",
    )
    pbp.add_argument(
        "--destination",
        default=None,
        help="rclone-style remote path (e.g. s3:my-bucket/elesync). "
        "Default: $ELESYNC_BACKUP_DESTINATION or config backup.destination.",
    )
    pbp.add_argument(
        "--dir",
        default=None,
        help="Local managed backup directory (default: $ELESYNC_BACKUP_DIR, "
        "config backup.dir, or <vault>/backups)",
    )
    pbp.set_defaults(func=cmd_backup_push)

    # `ele backup pull` — pull a backup from remote to local.
    ppu = sub.add_parser(
        "backup-pull",
        help="Pull a backup from a remote destination via rclone "
        "(usage: ele backup pull [--name filename.eleb])",
    )
    ppu.add_argument(
        "--destination",
        default=None,
        help="rclone-style remote path (e.g. s3:my-bucket/elesync). "
        "Default: $ELESYNC_BACKUP_DESTINATION or config backup.destination.",
    )
    ppu.add_argument(
        "--name",
        default=None,
        help="Specific backup filename to pull (default: newest)",
    )
    ppu.add_argument(
        "--dir",
        default=None,
        help="Local managed backup directory to pull into (default: "
        "$ELESYNC_BACKUP_DIR, config backup.dir, or <vault>/backups)",
    )
    ppu.set_defaults(func=cmd_backup_pull)

    prs = sub.add_parser(
        "restore",
        help="Restore memories from an EleSync backup (idempotent, lossless)",
    )
    prs.add_argument(
        "path", help="Backup file made by `ele backup` (.eleb) or `ele export` (.json)"
    )
    prs.add_argument(
        "--source",
        default=None,
        help="Force a source adapter (advanced; auto-detected for EleSync backups)",
    )
    prs.set_defaults(func=cmd_restore)

    pen = sub.add_parser("encrypt", help="Encrypt the vault at rest")
    pen.set_defaults(func=cmd_encrypt)

    pde = sub.add_parser("decrypt", help="Decrypt an encrypted vault and rebuild the index")
    pde.set_defaults(func=cmd_decrypt)

    pv = sub.add_parser("serve", help="Run the MCP server")
    pv.add_argument(
        "--transport",
        choices=["stdio", "http", "sse"],
        default="stdio",
        help=(
            "stdio (default, for local clients like Claude, Cursor, Windsurf) | "
            "http (Streamable HTTP, for ChatGPT Developer Mode / remote clients) | "
            "sse (legacy remote transport)"
        ),
    )
    # Grouped purely for --help readability; grouping doesn't change parsing.
    pv_net = pv.add_argument_group(
        "network (http/sse only)", "Where the remote transports bind and who they trust."
    )
    pv_net.add_argument(
        "--host", default="127.0.0.1", help="Bind host for http/sse (default: 127.0.0.1)"
    )
    pv_net.add_argument(
        "--port", type=int, default=8000, help="Bind port for http/sse (default: 8000)"
    )
    pv_net.add_argument(
        "--allowed-host",
        action="append",
        default=None,
        help=(
            "Public hostname to trust for http/sse (repeatable). Required when "
            "serving through a tunnel, e.g. --allowed-host xxxx.ngrok-free.dev"
        ),
    )
    pv_sec = pv.add_argument_group("security")
    pv_sec.add_argument(
        "--insecure",
        action="store_true",
        help=(
            "Allow an off-machine http/sse transport without authentication or TLS. "
            "Anyone who reaches the URL can read, write and delete the vault."
        ),
    )
    pv.set_defaults(func=cmd_serve)

    pc = sub.add_parser(
        "connect",
        help="Show setup instructions for AI clients that need a remote connector",
    )
    pc.set_defaults(func=cmd_connect)
    connect_sub = pc.add_subparsers(dest="connect_cmd")

    def add_remote_connect_parser(name: str, label: str):
        prc = connect_sub.add_parser(
            name,
            help=f"Prepare a secure {label} custom MCP connector setup",
        )
        prc.add_argument(
            "--url",
            help=f"Public HTTPS MCP URL to paste into {label}, e.g. https://example.com/mcp",
        )
        prc.add_argument(
            "--allowed-host",
            help="Public tunnel/proxy hostname to trust, e.g. xxxx.ngrok-free.dev",
        )
        prc.add_argument(
            "--port",
            type=int,
            default=8000,
            help="Local port for `ele serve --transport http` (default: 8000)",
        )
        prc.add_argument(
            "--force-token",
            action="store_true",
            help="Replace the existing Bearer token and print the new one",
        )
        prc.add_argument(
            "--insecure",
            action="store_true",
            help="Allow printing an http:// connector URL for local testing only",
        )
        prc.set_defaults(connect_func=cmd_connect_remote, client=name)
        return prc

    add_remote_connect_parser("chatgpt", "ChatGPT").set_defaults(connect_func=cmd_connect_chatgpt)
    add_remote_connect_parser("grok", "Grok")
    add_remote_connect_parser("gemini", "Gemini")
    add_remote_connect_parser("perplexity", "Perplexity")
    add_remote_connect_parser("copilot", "Copilot")
    add_remote_connect_parser("deepseek", "DeepSeek")

    def add_local_connect_parser(name: str, label: str):
        plc = connect_sub.add_parser(
            name,
            help=f"Wire EleSync into {label} through local MCP config",
        )
        plc.add_argument(
            "--name",
            help="MCP server key to register under (default: elesync)",
        )
        plc.set_defaults(connect_func=cmd_connect_local, client=name)

    add_local_connect_parser("cursor", "Cursor")
    add_local_connect_parser("claude", "Claude Desktop")

    pauth = sub.add_parser("auth", help="Manage the HTTP transport's Bearer token")
    pauth.set_defaults(func=cmd_auth)
    auth_sub = pauth.add_subparsers(dest="auth_cmd")

    pauth_status = auth_sub.add_parser("status", help="Show whether token auth is enabled")
    pauth_status.set_defaults(auth_func=cmd_auth_status)

    pauth_gen = auth_sub.add_parser(
        "generate-token", help="Generate and store a Bearer token for http/sse"
    )
    pauth_gen.add_argument(
        "--force", action="store_true", help="Replace an existing token (invalidates it)"
    )
    pauth_gen.set_defaults(auth_func=cmd_auth_generate_token)

    pauth_revoke = auth_sub.add_parser("revoke", help="Remove the stored Bearer token")
    pauth_revoke.set_defaults(auth_func=cmd_auth_revoke)

    pauth_mtls = auth_sub.add_parser(
        "setup-mtls",
        help="Generate a self-signed CA + server + client cert for mutual TLS auth",
    )
    pauth_mtls.add_argument(
        "--cert-dir",
        help="Directory to store the generated certs (default: alongside config.json)",
    )
    pauth_mtls.add_argument(
        "--force", action="store_true", help="Regenerate certs even if they already exist"
    )
    pauth_mtls.set_defaults(auth_func=cmd_auth_setup_mtls)

    po = sub.add_parser(
        "onboard",
        help="One-command setup: create vault + wire an MCP client (Claude Desktop, ...)",
    )
    po.add_argument(
        "--target",
        choices=[
            "claude-desktop",
            "generic",
        ],
        default="claude-desktop",
        help="MCP client to configure (default: claude-desktop). "
        "Use 'generic' to print a portable snippet you can paste into any client.",
    )
    po.add_argument(
        "--config-path",
        help="Path to the MCP client config (default: auto-detect for the selected target)",
    )
    po.add_argument(
        "--print-only", action="store_true", help="Print the config block instead of writing it"
    )
    # Grouped purely for --help readability; grouping doesn't change parsing.
    po.add_argument(
        "--audit",
        action=argparse.BooleanOptionalAction,
        default=True,
        help="Enable the append-only operation log by default. Use --no-audit to "
        "opt out of ELESYNC_AUDIT=1 for this client.",
    )
    po_scope = po.add_argument_group(
        "scoping", "Restrict what this wired client can see and where its writes land."
    )
    po_scope.add_argument(
        "--scopes",
        help="Grant this client only these scopes (e.g. 'general,writing') — it won't "
        "see other compartments. Omit for the full vault.",
    )
    po_scope.add_argument(
        "--write-scope",
        help="Default scope this client records new memories into (e.g. 'legal')",
    )
    po_scope.add_argument(
        "--project",
        dest="project_id",
        help="Bind this local MCP connection to one project ID so new memories land there.",
    )
    po_scope.add_argument(
        "--name",
        help="MCP server key to register under (default: 'elesync'); use a distinct "
        "name to wire a second, differently-scoped EleSync connection into one client",
    )
    po.set_defaults(func=cmd_onboard)

    pd = sub.add_parser("doctor", help="Verify the install end to end")
    pd.add_argument(
        "--fix",
        action="store_true",
        help="Attempt to repair fixable diagnostics, then re-run the health check",
    )
    pd.add_argument("--config-path", help="Path to the MCP client config to check")
    pd.set_defaults(func=cmd_doctor)

    # ── usb ──────────────────────────────────────────────────────────────
    pusb = sub.add_parser(
        "usb",
        help="Portable USB vault: install, attach, detach, status, eject, list",
    )
    pusb.set_defaults(func=cmd_usb)
    usb_sub = pusb.add_subparsers(dest="usb_cmd", required=True)

    pusb_list = usb_sub.add_parser("list", help="List detected removable drives")
    pusb_list.set_defaults(usb_func=cmd_usb_list)

    pusb_install = usb_sub.add_parser(
        "install", help="Set up an encrypted EleSync vault on a USB drive"
    )
    pusb_install.add_argument("drive", help="Path to the USB drive (e.g. /Volumes/MyUSB or E:\\)")
    pusb_install.add_argument(
        "--label", help="A friendly name for this vault (default: USB)", default="USB"
    )
    pusb_install.set_defaults(usb_func=cmd_usb_install)

    pusb_attach = usb_sub.add_parser(
        "attach",
        help="Unlock a USB vault and wire it into your AI client",
    )
    pusb_attach.add_argument("drive", help="Path to the USB drive")
    pusb_attach.add_argument(
        "--name",
        default="elesync-usb",
        help="MCP server key to register (default: elesync-usb)",
    )
    pusb_attach.set_defaults(usb_func=cmd_usb_attach)

    pusb_detach = usb_sub.add_parser(
        "detach",
        help="Disconnect the USB vault from your AI client (run before unplugging)",
    )
    pusb_detach.add_argument("drive", help="Path to the USB drive")
    pusb_detach.add_argument(
        "--name",
        default="elesync-usb",
        help="MCP server key to remove (default: elesync-usb)",
    )
    pusb_detach.add_argument(
        "--eject",
        action="store_true",
        help="Also safe-eject the drive after detaching",
    )
    pusb_detach.set_defaults(usb_func=cmd_usb_detach)

    pusb_status = usb_sub.add_parser("status", help="Show vault status for a drive")
    pusb_status.add_argument("drive", help="Path to the USB drive")
    pusb_status.set_defaults(usb_func=cmd_usb_status)

    pusb_eject = usb_sub.add_parser("eject", help="Safely eject a drive")
    pusb_eject.add_argument("drive", help="Path to the USB drive")
    pusb_eject.set_defaults(usb_func=cmd_usb_eject)

    # ── vault management
    pvault = sub.add_parser(
        "vault",
        help="Manage named vaults: list, current, add, remove, switch",
    )
    pvault.set_defaults(func=cmd_vault)
    vault_sub = pvault.add_subparsers(dest="vault_cmd", required=True)

    pvault_list = vault_sub.add_parser("list", help="List all known named vaults")
    pvault_list.set_defaults(vault_func=cmd_vault_list)

    pvault_current = vault_sub.add_parser("current", help="Show the active vault")
    pvault_current.set_defaults(vault_func=cmd_vault_current)

    pvault_add = vault_sub.add_parser("add", help="Register a vault by name")
    pvault_add.add_argument("vault_name", metavar="name", help="Friendly name for the vault")
    pvault_add.add_argument("vault_path", metavar="path", help="Path to the vault directory")
    pvault_add.set_defaults(vault_func=cmd_vault_add)

    pvault_remove = vault_sub.add_parser("remove", help="Remove a vault from the registry")
    pvault_remove.add_argument("vault_name", metavar="name", help="Name of the vault to remove")
    pvault_remove.set_defaults(vault_func=cmd_vault_remove)

    pvault_switch = vault_sub.add_parser("switch", help="Set the active vault by name")
    pvault_switch.add_argument("vault_name", metavar="name", help="Name of the vault to activate")
    pvault_switch.set_defaults(vault_func=cmd_vault_switch)

    # ── web UI ───────────────────────────────────────────────────────────
    pweb = sub.add_parser("web", help="Open the local web UI in your browser")
    pweb.add_argument(
        "--host",
        default="127.0.0.1",
        help="Bind host (default: 127.0.0.1; use 0.0.0.0 for multi-device)",
    )
    pweb.add_argument("--port", type=int, default=7477, help="Port to listen on (default: 7477)")
    pweb.add_argument("--no-browser", action="store_true", help="Don't auto-open the browser")
    pweb.add_argument(
        "--allow-insecure-network",
        action="store_true",
        help="Allow a non-loopback Web UI without authentication or TLS (unsafe)",
    )
    pweb.add_argument(
        "--tls-cert",
        help="Path to TLS certificate (PEM). Enables HTTPS when paired with --tls-key.",
    )
    pweb.add_argument(
        "--tls-key",
        help="Path to TLS private key (PEM). Enables HTTPS when paired with --tls-cert.",
    )
    pweb.set_defaults(func=cmd_web)

    pdemo = sub.add_parser(
        "demo", help="Open the dashboard on a throwaway demo vault with presenter keys"
    )
    pdemo.add_argument("--dir", help="Folder for the demo vault (default: a new temp folder)")
    pdemo.add_argument("--port", type=int, default=7490, help="Port to listen on (default: 7490)")
    pdemo.add_argument("--no-browser", action="store_true", help="Don't auto-open the browser")
    pdemo.set_defaults(func=cmd_demo)
    web_sub = pweb.add_subparsers(dest="web_cmd")

    pweb_adduser = web_sub.add_parser("adduser", help="Add a login for this vault's web dashboard")
    pweb_adduser.add_argument("username", help="Username (alphanumeric, underscore, dash)")
    pweb_adduser.add_argument(
        "--role",
        choices=["admin", "viewer"],
        default="admin",
        help="admin: full read/write. viewer: read-only (default: admin)",
    )
    pweb_adduser.add_argument(
        "--guest", action="store_true", help="Shorthand for --role viewer, marked as a guest"
    )
    pweb_adduser.set_defaults(web_func=cmd_web_adduser)

    pweb_removeuser = web_sub.add_parser(
        "removeuser", help="Remove a web dashboard login for this vault"
    )
    pweb_removeuser.add_argument("username", help="Username to remove")
    pweb_removeuser.set_defaults(web_func=cmd_web_removeuser)

    pweb_listusers = web_sub.add_parser(
        "listusers", help="List web dashboard logins configured for this vault"
    )
    pweb_listusers.set_defaults(web_func=cmd_web_listusers)

    psum = sub.add_parser(
        "summary",
        help="Show a human-readable digest of everything in your vault",
    )
    psum.add_argument(
        "--short",
        action="store_true",
        help="One-line-per-type counts + top 5 facts only",
    )
    psum.add_argument(
        "--source",
        metavar="SOURCE",
        help="Filter to one AI (e.g. chatgpt, claude, gemini)",
    )
    psum.set_defaults(func=cmd_summary)

    puni = sub.add_parser("uninstall", help="Remove EleSync from your system cleanly")
    puni.add_argument(
        "--config-path",
        help="MCP client config file (default: auto-detect for the selected target)",
    )
    puni.add_argument(
        "--name",
        default="elesync",
        help="MCP server key to remove (default: elesync)",
    )
    puni.add_argument(
        "--delete-vault",
        action="store_true",
        help="Also delete the vault folder (irreversible without backup)",
    )
    puni.add_argument(
        "--backup-first",
        action="store_true",
        help="Back up the vault before deleting it (implies --delete-vault)",
    )
    puni.set_defaults(func=cmd_uninstall)

    return p
