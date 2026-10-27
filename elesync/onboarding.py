"""Onboarding and diagnostics.

`onboard` does the one-command setup: create the vault and write the
MCP server entry into your AI client's config so the user goes from install to
connected in under two minutes. `doctor` verifies an install end to end (short of
the live MCP handshake, which only a running client can confirm).
"""

from __future__ import annotations

import json
import os
import sqlite3
import sys
from collections.abc import Callable
from datetime import datetime, timezone
from pathlib import Path

SERVER_KEY = "elesync"
_IS_WINDOWS = os.name == "nt"

# ── Multi-client target support ─────────────────────────────────────────────────
#
# Each target describes a MCP client's config location and the JSON structure it
# expects.  ``default_config_path_for_target`` returns the best-guess path;
SUPPORTED_TARGETS: list[str] = [
    "claude-desktop",
    "generic",
]
AUTO_CONNECT_TARGETS: tuple[str, ...] = ("claude-desktop",)


def _default_claude_desktop_path() -> Path:
    if sys.platform == "darwin":
        return Path.home() / "Library/Application Support/Claude/claude_desktop_config.json"
    if _IS_WINDOWS:
        roaming = Path(os.environ.get("APPDATA", str(Path.home() / "AppData/Roaming")))
        local = Path(os.environ.get("LOCALAPPDATA", str(Path.home() / "AppData/Local")))
        standard = roaming / "Claude/claude_desktop_config.json"
        store = (
            local
            / "Packages"
            / "Claude_pzs8sxrjxfjjc"
            / "LocalCache"
            / "Roaming"
            / "Claude"
            / "claude_desktop_config.json"
        )
        # Preserve whichever install already owns a config. If both do, retain
        # the long-standing standard Roaming preference. With no config yet,
        # choose the only detected installation directory, then fall back to
        # the standard path for backward compatibility.
        if standard.is_file():
            return standard
        if store.is_file():
            return store
        if store.parent.is_dir() and not standard.parent.is_dir():
            return store
        return standard
    return Path.home() / ".config/Claude/claude_desktop_config.json"


def _default_vscode_path() -> Path:
    """VS Code user settings."""
    if sys.platform == "darwin":
        return Path.home() / "Library/Application Support/Code/User/settings.json"
    if os.name == "nt":
        base = os.environ.get("APPDATA", str(Path.home() / "AppData/Roaming"))
        return Path(base) / "Code/User/settings.json"
    return Path.home() / ".config/Code/User/settings.json"


def default_config_path_for_target(target: str) -> Path:
    """Return the default config file path for *target*."""
    lookup: dict[str, Callable[[], Path]] = {
        "claude-desktop": _default_claude_desktop_path,
    }
    fn = lookup.get(target)
    if fn is None:
        # "generic" or unknown — fall back to Claude Desktop
        return _default_claude_desktop_path()
    return fn()


def default_client_config_path() -> Path:
    """Best-guess location of the Claude Desktop config for this OS.

    Kept for backward-compatibility — callers that don't pass a target get
    Claude Desktop."""
    return default_config_path_for_target("claude-desktop")


# Human-readable labels for each local target, used by the dashboard's
# one-click "Connect automatically" flow. Order matters — the most common
# clients are listed first so the UI shows them in priority order.
_LOCAL_CLIENT_LABELS: dict[str, str] = {
    "claude-desktop": "Claude Desktop",
}


def detect_local_clients() -> list[dict]:
    """Probe the filesystem for installed local MCP clients.

    A client is considered "installed" when the *parent directory* of its
    default config path exists — that's the most reliable signal that the app
    has been run at least once and will look for config in the expected place.
    The config file itself may not exist yet (the app creates it on first
    MCP-server add), so we check the parent, not the file.

    Returns a list of ``{target, label, config_path, installed}`` dicts,
    sorted with installed clients first, then by label. Only clients that
    are installed are included — the caller learns what's available, not
    what isn't.
    """
    found: list[dict] = []
    for target in AUTO_CONNECT_TARGETS:
        if target == "generic":
            continue
        cfg_path = default_config_path_for_target(target)
        # The parent dir existing means the app has been installed and run.
        # The config file itself may not exist yet — that's fine, wire_client_config
        # creates it.
        if cfg_path.parent.is_dir():
            found.append(
                {
                    "target": target,
                    "label": _LOCAL_CLIENT_LABELS.get(target, target),
                    "config_path": str(cfg_path),
                    "installed": True,
                }
            )
    return found


def _resolve_mcp_command() -> tuple[str, list[str]]:
    """Return ``(command, args)`` for the MCP server entry.

    When running from source, ``python -m elesync.mcp_server`` is correct.
    When frozen by PyInstaller, ``sys.executable`` is the GUI or CLI binary
    — not Python — so ``-m`` doesn't work.  In that case we look for the
    CLI binary (``ele`` / ``ele.exe``) and use ``ele serve``, which runs
    the same MCP server over stdio.
    """
    if not hasattr(sys, "frozen"):
        return sys.executable, ["-m", "elesync.mcp_server"]

    exe = Path(sys.executable)
    # CLI binary itself — ele.exe serve
    if exe.name.lower() in ("ele", "ele.exe"):
        return str(exe), ["serve"]

    # GUI binary — look for the sibling CLI binary.  Three layouts:
    #   Installer:  {app}/app/EleSync.exe  +  {app}/command/ele.exe
    #   Scoop:      <root>/gui/EleSync.exe  +  <root>/cli/ele.exe
    #   Portable:   <root>/EleSync.exe      +  <root>/ele.exe
    ele_name = "ele.exe" if os.name == "nt" else "ele"
    candidates = [
        exe.parent.parent / "command" / ele_name,  # installer
        exe.parent.parent / "cli" / ele_name,  # scoop
        exe.parent / ele_name,  # portable (same dir)
    ]
    for c in candidates:
        if c.is_file():
            return str(c), ["serve"]

    # Last resort: fall back to python -m (won't work frozen, but at least
    # the config is structurally valid and the user can fix the path).
    return sys.executable, ["-m", "elesync.mcp_server"]


def server_entry(
    vault_dir: str,
    scopes: str | None = None,
    write_scope: str | None = None,
    semantic: bool = False,
    readonly: bool = False,
    audit: bool = True,
    project_id: str | None = None,
) -> dict:
    """The mcpServers entry that points an MCP client at this vault.

    `scopes` / `write_scope` bake per-client scoped sharing into the config: the
    client will only see (and by default write into) those compartments. Leaving
    them unset keeps the client unrestricted — the full vault.

    `readonly` prefixes the scopes value with ``readonly:`` so the MCP server
    blocks all writes from this client.

    `audit` enables append-only operation logging (``ELESYNC_AUDIT=1``).
    """
    env: dict[str, str] = {"ELESYNC_DIR": str(Path(vault_dir).expanduser())}
    if scopes is not None and scopes.strip():
        scope_val = scopes.strip()
        if readonly:
            scope_val = f"readonly:{scope_val}" if scope_val else "readonly"
        env["ELESYNC_SCOPES"] = scope_val
    elif readonly:
        env["ELESYNC_SCOPES"] = "readonly"
    if write_scope is not None and write_scope.strip():
        env["ELESYNC_WRITE_SCOPE"] = write_scope.strip()
    if semantic:
        env["ELESYNC_SEMANTIC"] = "1"
    if audit:
        env["ELESYNC_AUDIT"] = "1"
    if project_id:
        env["ELESYNC_PROJECT_ID"] = project_id.strip()
    command, args = _resolve_mcp_command()
    return {
        "command": command,
        "args": args,
        "env": env,
    }


def build_client_config(
    target: str,
    entry: dict,
    name: str = SERVER_KEY,
) -> dict:
    """Wrap a raw *entry* (from ``server_entry``) in the JSON envelope the
    *target* client expects.  Returns a dict ready for ``json.dumps``."""

    if target == "claude-desktop":
        return {"mcpServers": {name: entry}}

    # generic — use the standard mcpServers shape
    return {"mcpServers": {name: entry}}


def print_snippet_for_target(
    target: str,
    vault_dir: str,
    scopes: str | None = None,
    write_scope: str | None = None,
    name: str = SERVER_KEY,
    audit: bool = True,
    project_id: str | None = None,
) -> str:
    """Return a human-readable config snippet for the given *target*."""
    entry = server_entry(
        vault_dir, scopes=scopes, write_scope=write_scope, audit=audit, project_id=project_id
    )
    if project_id and target == "claude-desktop":
        entry["env"]["ELESYNC_PROJECT_PROVIDER"] = "claude"
    blob = build_client_config(target, entry, name=name)

    config_hints: dict[str, str] = {
        "claude-desktop": "MCP client config",
        "generic": "your MCP client config",
    }
    hint = config_hints.get(target, "your MCP client config")
    header = f"Add this to {hint}:\n"
    return header + "\n" + json.dumps(blob, indent=2)


def wire_client_config(
    config_path: Path,
    vault_dir: str,
    scopes: str | None = None,
    write_scope: str | None = None,
    name: str = SERVER_KEY,
    semantic: bool = False,
    project_id: str | None = None,
    target: str = "claude-desktop",
    audit: bool = True,
) -> dict:
    """Idempotently add our MCP server to an MCP client config file.

    Creates the file if absent, backs up an existing one, preserves other servers,
    and tolerates an existing-but-corrupt config without crashing. Returns a small
    result dict describing what happened (handy for tests and for the CLI message).

    `name` lets you register more than one EleSync entry in a single client — e.g.
    a general connection and a separate legal-scoped one — by giving each a distinct
    server key. `scopes` / `write_scope` carry per-client scoped sharing into env.

    `target` selects the JSON envelope format for the client (see
    ``SUPPORTED_TARGETS``). The default (``claude-desktop``) preserves backward
    compatibility.
    """
    config_path = Path(config_path)
    config_path.parent.mkdir(parents=True, exist_ok=True)

    existing: dict = {}
    backed_up = False
    if config_path.exists():
        try:
            existing = json.loads(config_path.read_text(encoding="utf-8"))
            if not isinstance(existing, dict):
                existing = {}
        except (json.JSONDecodeError, OSError):
            existing = {}  # corrupt: we'll back it up and start clean
        # Use a timestamped backup so repeated onboarding runs don't overwrite
        # the original backup and the user can always recover the first version.
        ts = datetime.now(timezone.utc).strftime("%Y%m%dT%H%M%SZ")
        backup = config_path.with_suffix(f".{ts}.bak")
        backup.write_text(config_path.read_text(encoding="utf-8"), encoding="utf-8")
        backed_up = True

    entry = server_entry(
        vault_dir,
        scopes=scopes,
        write_scope=write_scope,
        semantic=semantic,
        audit=audit,
        project_id=project_id,
    )
    if project_id and target == "claude-desktop":
        entry["env"]["ELESYNC_PROJECT_PROVIDER"] = "claude"
    already = False

    if target in ("claude-desktop", "cursor", "windsurf"):
        # All three use { "mcpServers": { "<name>": entry } }
        servers = existing.get("mcpServers")
        if not isinstance(servers, dict):
            servers = {}
        already = name in servers
        servers[name] = entry
        existing["mcpServers"] = servers

    elif target == "continue":
        # Continue.dev: { "experimental": { "modelContextProtocolServers": [ ... ] } }
        exp = existing.setdefault("experimental", {})
        if not isinstance(exp, dict):
            exp = {}
            existing["experimental"] = exp
        arr = exp.get("modelContextProtocolServers")
        if not isinstance(arr, list):
            arr = []
        # Check for existing entry by name
        for i, srv in enumerate(arr):
            if isinstance(srv, dict) and srv.get("name") == name:
                already = True
                arr[i] = {
                    "name": name,
                    "transport": {
                        "type": "stdio",
                        "command": entry["command"],
                        "args": entry.get("args", []),
                        "env": entry.get("env", {}),
                    },
                }
                break
        else:
            arr.append(
                {
                    "name": name,
                    "transport": {
                        "type": "stdio",
                        "command": entry["command"],
                        "args": entry.get("args", []),
                        "env": entry.get("env", {}),
                    },
                }
            )
        exp["modelContextProtocolServers"] = arr

    elif target == "zed":
        # Zed: { "context_servers": { "<name>": { "command": { ... } } } }
        ctx = existing.get("context_servers")
        if not isinstance(ctx, dict):
            ctx = {}
        already = name in ctx
        ctx[name] = {
            "command": {
                "path": entry["command"],
                "args": entry.get("args", []),
                "env": entry.get("env", {}),
            },
            "settings": {},
        }
        existing["context_servers"] = ctx

    elif target in ("cline", "roo-code"):
        # VS Code settings: { "cline.mcpServers": { ... } }
        settings_key = "cline.mcpServers" if target == "cline" else "roo-cline.mcpServers"
        servers = existing.get(settings_key)
        if not isinstance(servers, dict):
            servers = {}
        already = name in servers
        servers[name] = entry
        existing[settings_key] = servers

    else:
        # generic / unknown — same as claude-desktop
        servers = existing.get("mcpServers")
        if not isinstance(servers, dict):
            servers = {}
        already = name in servers
        servers[name] = entry
        existing["mcpServers"] = servers

    config_path.write_text(json.dumps(existing, indent=2) + "\n", encoding="utf-8")
    return {
        "config_path": str(config_path),
        "backed_up": backed_up,
        "updated_existing": already,
        "server_key": name,
    }


# Supported MCP SDK range — must match the pin in pyproject.toml. Kept here
# (not imported from pyproject at runtime) because reading pyproject.toml at
# diagnosis time adds a TOML parse dependency for one number. When the pin
# changes, update both places; the test suite checks they agree.
MCP_MIN_VERSION = (1, 2, 0)
MCP_MAX_MAJOR = 2  # exclusive — `<2`


def _check_mcp_sdk() -> tuple[str, str]:
    """Probe the installed `mcp` package: present? version in range? FastMCP imports?

    Returns (status, detail) where status is PASS / WARN / FAIL:
      * PASS  — installed, version in range, FastMCP imports.
      * WARN  — installed but version out of range, OR FastMCP import fails
                (the server will not start; the user must fix before serving).
      * WARN  — not installed at all (downgraded from the old WARN; `ele serve`
                is optional, so a missing SDK is a warning, not a hard fail).

    The version check is what catches the 1.20.3 class of regression: a loose
    pin or a manual `pip install --upgrade` lands mcp 2.x, which renamed
    FastMCP → MCPServer and broke `ele serve` at first connect.
    """
    try:
        import mcp
    except ImportError:
        return (
            "WARN",
            f'not found — run: pip install "mcp[cli]>={MCP_MIN_VERSION[0]}.{MCP_MIN_VERSION[1]}.{MCP_MIN_VERSION[2]},<{MCP_MAX_MAJOR}"  (needed to serve)',
        )

    # Version string → tuple. mcp ships `__version__`; fall back to
    # importlib.metadata if the attribute is missing (older releases).
    version_str = getattr(mcp, "__version__", None)
    if not version_str:
        try:
            from importlib.metadata import version as _pkg_version

            version_str = _pkg_version("mcp")
        except Exception:
            version_str = "0.0.0"

    try:
        parts = tuple(int(p) for p in version_str.split(".")[:3])
        while len(parts) < 3:
            parts += (0,)
    except ValueError:
        parts = (0, 0, 0)

    in_range = parts >= MCP_MIN_VERSION and parts[0] < MCP_MAX_MAJOR

    # FastMCP import — the actual symbol `ele serve` needs. mcp 2.0.0 moved it,
    # so this is the load-bearing check even when the version looks fine.
    fastmcp_ok = False
    fastmcp_err = ""
    try:
        from mcp.server.fastmcp import FastMCP  # noqa: F401

        fastmcp_ok = True
    except Exception as e:
        fastmcp_err = f"{type(e).__name__}: {e}"

    if in_range and fastmcp_ok:
        return ("PASS", f"mcp {version_str} — FastMCP imports ✓")
    if not in_range and not fastmcp_ok:
        return (
            "WARN",
            f"mcp {version_str} is outside the supported range "
            f"({MCP_MIN_VERSION[0]}.{MCP_MIN_VERSION[1]}.{MCP_MIN_VERSION[2]} "
            f"<= mcp < {MCP_MAX_MAJOR}.0) and FastMCP won't import ({fastmcp_err}). "
            f'Run: pip install "mcp[cli]>={MCP_MIN_VERSION[0]}.{MCP_MIN_VERSION[1]}.{MCP_MIN_VERSION[2]},<{MCP_MAX_MAJOR}"',
        )
    if not in_range:
        return (
            "WARN",
            f"mcp {version_str} is outside the supported range "
            f"({MCP_MIN_VERSION[0]}.{MCP_MIN_VERSION[1]}.{MCP_MIN_VERSION[2]} "
            f"<= mcp < {MCP_MAX_MAJOR}.0) — `ele serve` may break. "
            f'Run: pip install "mcp[cli]>={MCP_MIN_VERSION[0]}.{MCP_MIN_VERSION[1]}.{MCP_MIN_VERSION[2]},<{MCP_MAX_MAJOR}"',
        )
    # In range but FastMCP import failed — unusual; surface the error.
    return (
        "WARN",
        f"mcp {version_str} is in range but FastMCP won't import ({fastmcp_err}). "
        "Reinstall: pip install --force-reinstall mcp[cli]",
    )


def run_doctor(vault_dir: str, config_path: Path | None = None) -> list[tuple[str, str, str]]:
    """Return a list of (status, check, detail) where status is PASS/WARN/FAIL."""
    results: list[tuple[str, str, str]] = []

    # Python version
    v = sys.version_info
    ok = (v.major, v.minor) >= (3, 10)
    results.append(("PASS" if ok else "FAIL", "Python >= 3.10", f"{v.major}.{v.minor}.{v.micro}"))

    # Vault dir writable
    vp = Path(vault_dir).expanduser()
    try:
        (vp / "notes").mkdir(parents=True, exist_ok=True)
        probe = vp / ".write_probe"
        probe.write_text("ok", encoding="utf-8")
        probe.unlink()
        results.append(("PASS", "Vault directory writable", str(vp)))
    except OSError as e:
        results.append(("FAIL", "Vault directory writable", f"{vp}: {e}"))

    # SQLite FTS5
    try:
        con = sqlite3.connect(":memory:")
        con.execute("CREATE VIRTUAL TABLE t USING fts5(x)")
        con.close()
        results.append(("PASS", "SQLite FTS5 search", "available"))
    except sqlite3.OperationalError:
        results.append(
            ("WARN", "SQLite FTS5 search", "not compiled in — falls back to LIKE (still works)")
        )

    # MCP SDK — version + import check. The 1.20.3 release was broken by mcp
    # 2.0.0 (it renamed FastMCP → MCPServer and moved imports), so we verify the
    # installed version is in the supported range AND that FastMCP actually
    # imports. A loose pin or a manual `pip install --upgrade` can land an
    # incompatible major; this catches that at diagnosis time instead of at
    # first `ele serve`.
    mcp_status, mcp_detail = _check_mcp_sdk()
    results.append((mcp_status, "MCP SDK installed", mcp_detail))

    # Client config wired
    cp = Path(config_path) if config_path else default_client_config_path()
    if cp.exists():
        try:
            cfg = json.loads(cp.read_text(encoding="utf-8"))
            entry = cfg.get("mcpServers", {}).get(SERVER_KEY)
            if entry is not None:
                grant_env = (entry.get("env") or {}).get("ELESYNC_SCOPES")
                scope_note = f" — scope: {grant_env}" if grant_env else ""
                results.append(("PASS", "MCP client config wired", f"{cp}{scope_note}"))
            else:
                results.append(
                    (
                        "WARN",
                        "MCP client config wired",
                        f"{cp} exists but has no '{SERVER_KEY}' — run: ele onboard",
                    )
                )
        except (json.JSONDecodeError, OSError):
            results.append(("WARN", "MCP client config wired", f"{cp} unreadable"))
    else:
        results.append(
            (
                "WARN",
                "MCP client config wired",
                "not found — run: ele onboard (or paste the config manually)",
            )
        )

    return results


def _config_has_wired_entry(config_path: Path, vault_dir: str) -> bool:
    if not config_path.exists():
        return False
    try:
        cfg = json.loads(config_path.read_text(encoding="utf-8"))
    except (json.JSONDecodeError, OSError):
        return False
    if not isinstance(cfg, dict):
        return False
    entry = cfg.get("mcpServers", {}).get(SERVER_KEY)
    if not isinstance(entry, dict):
        return False
    env = entry.get("env")
    if not isinstance(env, dict):
        return False
    return env.get("ELESYNC_DIR") == str(Path(vault_dir).expanduser())


def fix_doctor(vault_dir: str, config_path: Path | None = None) -> list[str]:
    """Attempt to repair fixable doctor issues and return a list of applied fixes."""
    fixes: list[str] = []
    vp = Path(vault_dir).expanduser()
    if not vp.exists():
        vp.mkdir(parents=True, exist_ok=True)
        fixes.append(f"Created vault directory {vp}")

    notes_dir = vp / "notes"
    if not notes_dir.exists():
        notes_dir.mkdir(parents=True, exist_ok=True)
        fixes.append(f"Created notes directory {notes_dir}")

    cp = Path(config_path) if config_path else default_client_config_path()
    if not _config_has_wired_entry(cp, vault_dir):
        wire_client_config(cp, vault_dir=vault_dir)
        fixes.append(f"Wired MCP client config at {cp}")

    return fixes


def unwire_entry(config_path: Path, name: str = SERVER_KEY) -> dict:
    """Remove an EleSync MCP server entry from a client config file.
    Returns what was done."""
    config_path = Path(config_path)
    removed = False
    if config_path.exists():
        try:
            cfg = json.loads(config_path.read_text(encoding="utf-8"))
            servers = cfg.get("mcpServers", {})
            if isinstance(servers, dict) and name in servers:
                del servers[name]
                cfg["mcpServers"] = servers
                config_path.write_text(json.dumps(cfg, indent=2) + "\n", encoding="utf-8")
                removed = True
        except (json.JSONDecodeError, OSError):
            pass
    return {"config_path": str(config_path), "removed": removed, "server_key": name}
