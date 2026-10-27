"""GUI entry point for packaged builds (macOS .app / Windows exe).

Double-clicking the EleSync app launches the web dashboard in the browser
— no terminal, no commands to remember. The server runs silently in the
background; closing the browser tab or the background process stops it.

This is the entry point PyInstaller bundles as the main executable. The
CLI (ele) remains available as a separate binary for power users.
"""

import json
import os
import socket
import sys
from pathlib import Path

import elesync.cli


def _portable_vault() -> Path | None:
    """Resolve a vault beside this build without depending on a drive letter."""
    marker = Path(sys.executable).resolve().parent / "portable.json"
    if not marker.is_file():
        return None
    try:
        value = json.loads(marker.read_text(encoding="utf-8")).get("vault_dir")
        vault = (marker.parent / value).resolve()
    except (AttributeError, json.JSONDecodeError, OSError, TypeError):
        return None
    expected = (marker.parent.parent / "EleSyncVault").resolve()
    return vault if vault == expected and vault.is_dir() else None


def _prompt_for_passphrase(vault: Path) -> str | None:
    """Ask until the encrypted USB vault is unlocked or the user cancels."""
    import tkinter as tk
    from tkinter import messagebox, simpledialog

    from elesync import crypto
    from elesync.cli import _verify_key

    root = tk.Tk()
    root.withdraw()
    try:
        supplied = os.environ.get("ELESYNC_PASSPHRASE", "")
        while True:
            passphrase = supplied or simpledialog.askstring(
                "Unlock EleSync",
                "Enter the password for this encrypted USB vault:",
                show="*",
                parent=root,
            )
            supplied = ""
            if not passphrase:
                return None
            meta = crypto.read_meta(vault)
            if meta is None:
                messagebox.showerror("EleSync", "The encrypted vault metadata is missing.")
                return None
            import base64

            salt = (
                bytes.fromhex(meta["salt"])
                if isinstance(meta["salt"], str) and len(meta["salt"]) == 64
                else base64.b64decode(meta["salt"])
            )
            key = crypto.derive_key(passphrase, salt, meta["opslimit"], meta["memlimit"])
            if _verify_key(vault, key):
                return passphrase
            messagebox.showerror("EleSync", "That password did not unlock this vault.")
    finally:
        root.destroy()


def _redirect_stdio_to_log(log_dir: Path | None = None) -> None:
    """In a windowed (no-console) PyInstaller build, writes to stdout/stderr
    can raise IOError. Redirect them to a log file so the server logs are still
    available for debugging and the process never crashes on a print()."""
    if log_dir is None:
        try:
            from elesync.cli import _config_path

            log_dir = Path(_config_path()).parent
        except Exception:
            log_dir = Path.home() / ".config" / "EleSync"
    log_dir.mkdir(parents=True, exist_ok=True)
    log = log_dir / "EleSync.log"
    # Open in append + line-buffered-ish for Python's default; we're not a tty.
    try:
        sys.stdout = open(log, "a", encoding="utf-8")  # noqa: SIM115
        sys.stderr = sys.stdout
    except OSError:
        pass


def main() -> int:
    portable_vault = _portable_vault() if getattr(sys, "frozen", False) else None
    # Windowed builds have no console; redirect output to a log file.
    if getattr(sys, "frozen", False):
        _redirect_stdio_to_log(portable_vault.parent / "EleSync" if portable_vault else None)

    # Release smoke tests and troubleshooting invoke the GUI binary with
    # command-line flags such as --version. Honour explicit arguments instead
    # of silently discarding them and starting a blocking web server.
    if len(sys.argv) > 1:
        return elesync.cli.main()

    if portable_vault is not None:
        from elesync import crypto

        if crypto.available() and crypto.is_encrypted(portable_vault):
            passphrase = _prompt_for_passphrase(portable_vault)
            if not passphrase:
                return 1
            os.environ["ELESYNC_PASSPHRASE"] = passphrase
        with socket.socket() as probe:
            probe.bind(("127.0.0.1", 0))
            port = probe.getsockname()[1]
        sys.argv = ["ele", "--vault", str(portable_vault), "web", "--port", str(port)]
    else:
        # Force the "web" subcommand. Default vault resolution in _resolve_vault
        # will pick $ELESYNC_DIR / config / default; this matches `ele web`.
        sys.argv = ["ele", "web"]

    # The web command owns browser startup as well as the listening address.
    # Keeping both in one place prevents the packaged launcher from opening a
    # stale or mismatched port.
    return elesync.cli.main()


if __name__ == "__main__":
    sys.exit(main())
