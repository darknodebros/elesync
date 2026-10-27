"""
elesync.usb — portable USB / external-drive vault support.

One command. Any drive. Fully encrypted. No cloud.

Usage:
    ele usb install /Volumes/MyUSB          # set up a portable vault on the drive
    ele usb attach  /Volumes/MyUSB          # unlock + wire into your AI client
    ele usb detach  /Volumes/MyUSB          # lock + unwire from your AI client
    ele usb status  /Volumes/MyUSB          # check what's on the drive
    ele usb eject   /Volumes/MyUSB          # safe-eject after detaching
    ele usb list                            # show all detected removable drives

Design goals
------------
* **One command** — `ele usb install <drive>` bootstraps everything:
  creates the vault, encrypts it, writes a self-contained launcher script onto
  the drive so the user only ever runs ONE command on any new machine.
* **Encrypted by default** — unlike the main vault where encryption is opt-in,
  USB vaults are ALWAYS encrypted (XSalsa20-Poly1305 + argon2id, same as
  `ele encrypt`). A lost USB drive exposes nothing without the passphrase.
* **Portable launcher script** — `RUN_ME.py` is written to the root of the drive.
  On any machine with Python installed, `python RUN_ME.py` installs EleSync (if
  needed) and wires the USB vault into your AI client — the user never needs to
  know a pip command.
* **Cross-platform** — macOS, Windows, Linux. Drive detection uses /proc/mounts
  (Linux), diskutil (macOS) and native device queries (Windows).
* **Safe eject** — `ele usb eject` calls the OS safe-eject API before the drive
  is physically removed, so in-flight writes are flushed.

Relationship to the main vault
-------------------------------
The USB vault IS a normal EleSync vault (same notes/*.md.enc layout, same
store/service/mcp_server stack). `ele usb attach` just tells the MCP server
to point at the USB path instead of ~/EleSyncVault — by writing that server
entry into your AI client's config. `ele usb detach` removes it again.

Nothing is copied or synced automatically. The user's USB is the canonical store
for memories they carry with them; if they also want those memories in their main
vault they can run `ele import` against an export from either side.
"""

from __future__ import annotations

import json
import logging
import os
import platform
import shutil
import subprocess
import sys
import uuid
from pathlib import Path

_log = logging.getLogger(__name__)

# Module-level references so tests can patch elesync.usb.crypto / MemoryStore / onboarding
try:
    from . import crypto  # type: ignore[attr-defined]
except Exception:  # pragma: no cover
    crypto = None  # type: ignore[assignment]

try:
    from .store import MemoryStore  # type: ignore[attr-defined]
except Exception:  # pragma: no cover
    MemoryStore = None  # type: ignore[misc,assignment]

try:
    from . import onboarding  # type: ignore[attr-defined]
except Exception:  # pragma: no cover
    onboarding = None  # type: ignore[assignment]

# ---------------------------------------------------------------------------
# Drive detection
# ---------------------------------------------------------------------------

SYSTEM = platform.system()


def list_removable_drives(*, strict: bool = False) -> list[dict]:
    """Return a list of dicts: {path, label, size_gb, fs} for removable drives.

    Results are best-effort — falls back to an empty list on unrecognised systems
    or missing utilities rather than crashing.
    """
    try:
        if SYSTEM == "Darwin":
            return _list_drives_macos()
        if SYSTEM == "Windows":
            return _list_drives_windows()
        return _list_drives_linux()
    except Exception:
        _log.debug("failed to list removable drives", exc_info=True)
        if strict:
            raise
        return []


def _list_drives_macos() -> list[dict]:

    out = subprocess.run(
        ["diskutil", "list", "-plist"],
        capture_output=True,
        text=True,
        check=False,
    )
    if out.returncode != 0 or not out.stdout.strip():
        return []
    import plistlib

    data = plistlib.loads(out.stdout.encode())
    drives = []
    for disk in data.get("AllDisksAndPartitions", []):
        # Removable physical disks (not disk0, not disk images)
        devname = disk.get("DeviceIdentifier", "")
        if not devname or devname == "disk0":
            continue
        for part in disk.get("Partitions", []):
            mp = part.get("MountPoint", "")
            if not mp or mp in ("/", "/System/Volumes/Data"):
                continue
            info = subprocess.run(
                ["diskutil", "info", "-plist", part.get("DeviceIdentifier", "")],
                capture_output=True,
                text=True,
                check=False,
            )
            pdata: dict = {}
            if info.returncode == 0:
                try:
                    pdata = plistlib.loads(info.stdout.encode())
                except Exception:
                    _log.debug("failed to parse diskutil plist output", exc_info=True)
            size_bytes = pdata.get("TotalSize", 0) or 0
            drives.append(
                {
                    "path": mp,
                    "label": pdata.get("VolumeName") or Path(mp).name,
                    "size_gb": round(size_bytes / 1e9, 1),
                    "fs": pdata.get("FilesystemType", "?"),
                }
            )
    return drives


def _windows_kernel():
    import ctypes
    from ctypes import wintypes as w

    kernel = ctypes.WinDLL("kernel32", use_last_error=True)
    signatures = {
        "GetLogicalDrives": ([], w.DWORD),
        "GetDriveTypeW": ([w.LPCWSTR], w.UINT),
        "GetVolumeInformationW": (
            [
                w.LPCWSTR,
                w.LPWSTR,
                w.DWORD,
                ctypes.POINTER(w.DWORD),
                ctypes.POINTER(w.DWORD),
                ctypes.POINTER(w.DWORD),
                w.LPWSTR,
                w.DWORD,
            ],
            w.BOOL,
        ),
        "CreateFileW": (
            [w.LPCWSTR, w.DWORD, w.DWORD, w.LPVOID, w.DWORD, w.DWORD, w.HANDLE],
            w.HANDLE,
        ),
        "DeviceIoControl": (
            [
                w.HANDLE,
                w.DWORD,
                w.LPVOID,
                w.DWORD,
                w.LPVOID,
                w.DWORD,
                ctypes.POINTER(w.DWORD),
                w.LPVOID,
            ],
            w.BOOL,
        ),
        "CloseHandle": ([w.HANDLE], w.BOOL),
        "SetThreadErrorMode": ([w.DWORD, ctypes.POINTER(w.DWORD)], w.BOOL),
    }
    for name, (args, result) in signatures.items():
        function = getattr(kernel, name)
        function.argtypes = args
        function.restype = result
    return kernel


def _windows_usb_bus(kernel, root: str) -> bool:
    import ctypes
    from ctypes import wintypes as w

    # Metadata access only; no disk reads/writes and no administrative access.
    handle = kernel.CreateFileW("\\\\.\\" + root[:2], 0, 3, None, 3, 0, None)
    if handle == ctypes.c_void_p(-1).value:
        return False
    try:
        query = ctypes.create_string_buffer(12)  # StorageDeviceProperty, PropertyStandardQuery
        descriptor = ctypes.create_string_buffer(1024)
        returned = w.DWORD()
        ok = kernel.DeviceIoControl(
            handle,
            0x2D1400,
            query,
            len(query),
            descriptor,
            len(descriptor),
            ctypes.byref(returned),
            None,
        )
        # STORAGE_DEVICE_DESCRIPTOR.BusType is a DWORD at byte offset 28.
        return bool(
            ok and returned.value >= 32 and int.from_bytes(descriptor.raw[28:32], "little") == 7
        )
    finally:
        kernel.CloseHandle(handle)


def _list_drives_windows() -> list[dict]:
    import ctypes

    kernel = _windows_kernel()
    mask = kernel.GetLogicalDrives()
    if not mask:
        raise OSError("Windows could not list drives. Browse for a destination folder.")
    drives = []
    for index, letter in enumerate("ABCDEFGHIJKLMNOPQRSTUVWXYZ"):
        if not mask & (1 << index):
            continue
        root = letter + ":\\"
        kind = kernel.GetDriveTypeW(root)
        if kind != 2 and not (kind == 3 and _windows_usb_bus(kernel, root)):
            continue
        label = ctypes.create_unicode_buffer(261)
        filesystem = ctypes.create_unicode_buffer(261)
        previous_mode = ctypes.c_ulong()
        changed_mode = kernel.SetThreadErrorMode(1, ctypes.byref(previous_mode))
        try:
            kernel.GetVolumeInformationW(
                root, label, len(label), None, None, None, filesystem, len(filesystem)
            )
            try:
                size = round(shutil.disk_usage(root).total / 1e9, 1)
            except OSError:
                size = 0.0
        finally:
            if changed_mode:
                kernel.SetThreadErrorMode(previous_mode.value, None)
        drives.append(
            {
                "path": root,
                "label": label.value or root,
                "size_gb": size,
                "fs": filesystem.value or "?",
            }
        )
    return drives


def _list_drives_linux() -> list[dict]:
    drives: list[dict] = []
    mounts = Path("/proc/mounts")
    if not mounts.exists():
        return drives
    for line in mounts.read_text().splitlines():
        parts = line.split()
        if len(parts) < 3:
            continue
        _, mp, fs = parts[0], parts[1], parts[2]
        # Heuristic: removable drives are under /media or /mnt, or start with /dev/sd
        if not (mp.startswith("/media/") or mp.startswith("/mnt/")):
            continue
        if fs in ("sysfs", "proc", "devtmpfs", "tmpfs", "cgroup"):
            continue
        try:
            stat = os.statvfs(mp)  # type: ignore[attr-defined]  # Unix-only
            size_gb = round(stat.f_blocks * stat.f_frsize / 1e9, 1)
        except OSError:
            size_gb = 0.0
        drives.append({"path": mp, "label": Path(mp).name, "size_gb": size_gb, "fs": fs})
    return drives


# ---------------------------------------------------------------------------
# Vault layout on the drive
# ---------------------------------------------------------------------------

VAULT_DIR_NAME = "EleSyncVault"
LAUNCHER_NAME = "RUN_ME.py"
README_NAME = "ELESYNC_README.txt"
PORTABLE_APP_DIR_NAME = "EleSync"
PORTABLE_MARKER_NAME = "portable.json"


def vault_path(drive: str | Path) -> Path:
    return Path(drive) / VAULT_DIR_NAME


def validate_destination(drive: str | Path) -> Path:
    """Require an existing absolute local directory; never recreate lost mounts."""
    path = Path(drive).expanduser()
    if not path.is_absolute() or (SYSTEM == "Windows" and str(path).startswith("\\\\")):
        raise ValueError("Choose an absolute local drive or folder path.")
    try:
        path = path.resolve(strict=True)
    except FileNotFoundError as error:
        raise FileNotFoundError(
            "The destination is no longer available. Reconnect the drive and refresh."
        ) from error
    if SYSTEM == "Windows" and str(path).startswith("\\\\"):
        raise ValueError("Choose a local folder, not a network destination.")
    if not path.is_dir():
        raise ValueError("The destination must be an existing folder or drive.")
    return path


def destination_folders(path: str = "") -> dict:
    """Browse local directories only, with bounded output and no file contents."""
    if not path:
        roots = (
            [Path(f"{letter}:\\") for letter in "ABCDEFGHIJKLMNOPQRSTUVWXYZ"]
            if SYSTEM == "Windows"
            else [Path("/")]
        )
        return {"path": "", "parent": "", "folders": [str(p) for p in roots if p.is_dir()]}
    directory = validate_destination(path)
    folders = []
    truncated = False
    with os.scandir(directory) as entries:
        for entry in entries:
            try:
                if getattr(entry.stat(follow_symlinks=False), "st_file_attributes", 0) & 6:
                    continue  # Windows hidden/system folders are not useful destinations.
                if entry.is_dir(follow_symlinks=False):
                    folders.append(str(validate_destination(entry.path)))
                    if len(folders) == 200:
                        truncated = True
                        break
            except (OSError, ValueError):
                continue
    return {
        "path": str(directory),
        "parent": str(directory.parent) if directory.parent != directory else "",
        "folders": sorted(folders, key=str.casefold),
        "truncated": truncated,
    }


def export_backup(drive: str | Path, passphrase: str, payload: dict) -> dict:
    """Write a complete encrypted backup without overwriting destination files."""
    import base64
    import tempfile
    import uuid

    from . import archive

    destination = validate_destination(drive)
    if not isinstance(passphrase, str) or not passphrase:
        raise ValueError("A passphrase is required.")
    if crypto is None or not crypto.available():
        raise RuntimeError("Encryption is unavailable. No backup was written.")
    salt = crypto.new_salt()
    ops, mem = crypto.default_limits()
    key = crypto.derive_key(passphrase, salt, ops, mem)
    metadata = {
        "kdf": "argon2id",
        "cipher": "xsalsa20poly1305",
        "salt": base64.b64encode(salt).decode("ascii"),
        "opslimit": ops,
        "memlimit": mem,
    }
    # Temp and final files stay on the selected device; no plaintext touches disk.
    fd, temporary_name = tempfile.mkstemp(prefix=".elesync-", suffix=".tmp", dir=destination)
    os.close(fd)
    temporary = Path(temporary_name)
    target = destination / f"elesync-backup-{uuid.uuid4().hex}.eleb"
    try:
        archive.write(temporary, payload, key=key, kdf_meta=metadata)
        # Verify the encrypted payload before reporting success.
        if archive.read(temporary, key=key) != payload:
            raise OSError("Backup verification failed.")
        with temporary.open("rb+") as stream:
            os.fsync(stream.fileno())
        temporary.rename(target)
    finally:
        temporary.unlink(missing_ok=True)
    return {"backup_path": str(target), "encrypted": True, "count": payload["count"]}


def _portable_app_source() -> Path:
    """Return the directory containing the running self-contained GUI build."""
    if not getattr(sys, "frozen", False):
        raise RuntimeError("Portable installation is available from the installed Windows app.")
    source = Path(sys.executable).resolve().parent
    if not (source / "EleSync.exe").is_file() or not (source / "_internal").is_dir():
        raise RuntimeError("The installed EleSync application files could not be found.")
    return source


def install_portable(
    drive: str | Path,
    passphrase: str,
    payload: dict,
    *,
    app_source: str | Path | None = None,
) -> dict:
    """Install a self-contained app and populated encrypted vault on a drive."""
    from .commands.backup import _restore_templates
    from .models import MemoryItem

    destination = validate_destination(drive)
    if not isinstance(passphrase, str) or not passphrase:
        raise ValueError("A passphrase is required.")
    if crypto is None or not crypto.available():
        raise RuntimeError("Encryption is unavailable. No portable vault was written.")

    source = Path(app_source).resolve() if app_source is not None else _portable_app_source()
    if not source.is_dir() or not (source / "EleSync.exe").is_file():
        raise RuntimeError("The portable EleSync application files could not be found.")
    if source == destination or destination in source.parents or source in destination.parents:
        raise ValueError("Choose a destination outside the installed EleSync application.")

    app_dir = destination / PORTABLE_APP_DIR_NAME
    vp = destination / VAULT_DIR_NAME
    if app_dir.exists() or app_dir.is_symlink() or vp.exists() or vp.is_symlink():
        raise FileExistsError(
            "EleSync or EleSyncVault already exists here. Existing files were left unchanged."
        )

    stage = destination / f".elesync-portable-{uuid.uuid4().hex}"
    try:
        staged_app = stage / PORTABLE_APP_DIR_NAME
        staged_vault = stage / VAULT_DIR_NAME
        shutil.copytree(source, staged_app, symlinks=False)
        (staged_app / PORTABLE_MARKER_NAME).write_text(
            json.dumps({"vault_dir": f"../{VAULT_DIR_NAME}"}, indent=2) + "\n",
            encoding="utf-8",
        )

        staged_vault.mkdir()
        (staged_vault / "notes").mkdir()
        salt = crypto.new_salt()
        ops, mem = crypto.default_limits()
        key = crypto.derive_key(passphrase, salt, ops, mem)
        crypto.write_meta(staged_vault, salt, ops, mem)
        store = MemoryStore(staged_vault, key=key)
        try:
            items = [MemoryItem.from_dict(item) for item in payload.get("memories", [])]
            result = store.import_many(items)
            store.restore_projects(payload.get("projects"))
            _restore_templates(store.vault_dir, payload)
        finally:
            store.close()

        staged_app.rename(app_dir)
        staged_vault.rename(vp)
    except Exception:
        shutil.rmtree(stage, ignore_errors=True)
        if app_dir.exists() and not vp.exists():
            shutil.rmtree(app_dir, ignore_errors=True)
        raise
    finally:
        shutil.rmtree(stage, ignore_errors=True)

    return {
        "vault_dir": str(vp),
        "launcher": str(app_dir / "EleSync.exe"),
        "encrypted": True,
        "count": result["total"],
        "portable": True,
    }


def is_usb_vault(drive: str | Path) -> bool:
    """Return True if the drive looks like it has an EleSync vault on it."""
    vp = vault_path(drive)
    return (vp / "notes").exists() or (vp / "vault.json").exists()


# ---------------------------------------------------------------------------
# Install
# ---------------------------------------------------------------------------


def install(drive: str | Path, passphrase: str, label: str = "USB") -> dict:
    """Bootstrap an encrypted EleSync vault on `drive`.

    1. Creates `<drive>/EleSyncVault/notes/` (the standard vault layout).
    2. Encrypts the vault (writes vault.json metadata; notes dir starts empty).
    3. Writes a self-contained `RUN_ME.py` launcher to the drive root.
    4. Writes a plain-English `ELESYNC_README.txt`.

    Returns a result dict with the paths created.
    """
    if crypto is None or not crypto.available():
        raise RuntimeError(
            'USB vaults are always encrypted, which needs PyNaCl. Run:  pip install "pynacl>=1.5"  first.'
        )

    if not isinstance(passphrase, str) or not passphrase:
        raise ValueError("A passphrase is required.")
    drive = validate_destination(drive)

    vp = vault_path(drive)
    if vp.exists() or vp.is_symlink():
        raise FileExistsError("An EleSyncVault already exists here. Choose another folder.")
    if any((drive / name).exists() for name in (LAUNCHER_NAME, README_NAME)):
        raise FileExistsError("Portable launcher files already exist. Choose another folder.")
    vp.mkdir()
    (vp / "notes").mkdir()

    # Write vault.json (encryption metadata) with a fresh salt and key derivation.
    salt = crypto.new_salt()
    ops, mem = crypto.default_limits()
    print("🔑  Deriving encryption key (argon2id) — this takes a few seconds …")
    _key = crypto.derive_key(passphrase, salt, ops, mem)  # validates the passphrase works
    crypto.write_meta(vp, salt, ops, mem)

    # Open the store once so the SQLite schema is initialised (even though the
    # in-memory index is empty — it primes the notes/ layout).
    store = MemoryStore(vp, key=_key)
    store.close()

    # Launcher script and plain-English README.
    _write_launcher(drive, vp)
    _write_readme(drive, vp, label)

    return {
        "vault_dir": str(vp),
        "launcher": str(drive / LAUNCHER_NAME),
        "readme": str(drive / README_NAME),
        "encrypted": True,
    }


# ---------------------------------------------------------------------------
# Attach / detach
# ---------------------------------------------------------------------------


def attach(drive: str | Path, passphrase: str, name: str = "elesync-usb") -> dict:
    """Unlock the USB vault and wire it into your AI client (or any MCP client).

    Verifies the passphrase unlocks the vault, then calls `ele onboard` with the
    USB vault path and passphrase baked into the MCP server config env vars.

    Returns the result dict from `onboarding.wire_client_config`.
    """
    drive = Path(drive)
    vp = vault_path(drive)

    if not is_usb_vault(drive):
        raise FileNotFoundError(
            f"No EleSync vault found at {vp}. Run `ele usb install {drive}` first."
        )

    if not crypto.is_encrypted(vp):
        raise RuntimeError(
            "This vault is not encrypted. USB vaults should always be encrypted.\n"
            f"Run:  ele usb install {drive}  to reinitialise with encryption."
        )

    meta = crypto.read_meta(vp)
    if meta is None:
        raise RuntimeError(f"Vault metadata missing in {vp}.")

    import base64 as _b64

    key = crypto.derive_key(
        passphrase,
        _b64.b64decode(meta["salt"]),
        meta["opslimit"],
        meta["memlimit"],
    )
    # Validate passphrase by trying to open the store.
    _validate_key(vp, key)

    cfg_path = onboarding.default_client_config_path()
    result = onboarding.wire_client_config(
        cfg_path,
        str(vp),
        name=name,
    )
    # Patch the env to also carry the passphrase for the MCP server process.
    _patch_passphrase_in_config(cfg_path, name, passphrase)

    result["vault_dir"] = str(vp)
    result["server_key"] = name
    return result


def detach(drive: str | Path, name: str = "elesync-usb") -> dict:
    """Remove the USB vault's MCP entry from the client config.

    Call this before physically unplugging the drive so Claude won't try to
    reconnect to a missing vault on next launch.
    """
    cfg_path = onboarding.default_client_config_path()
    if not cfg_path.exists():
        return {"removed": False, "reason": "MCP client config not found"}

    import json as _json

    try:
        cfg = _json.loads(cfg_path.read_text(encoding="utf-8"))
    except Exception:
        _log.debug("failed to parse MCP client config at %s", cfg_path, exc_info=True)
        return {"removed": False, "reason": "Could not parse MCP client config"}

    servers = cfg.get("mcpServers", {})
    if name not in servers:
        return {"removed": False, "reason": f"No server named '{name}' in config"}

    del servers[name]
    cfg["mcpServers"] = servers

    from datetime import datetime, timezone

    ts = datetime.now(timezone.utc).strftime("%Y%m%dT%H%M%SZ")
    bak = cfg_path.with_suffix(f".{ts}.bak")
    bak.write_text(cfg_path.read_text(encoding="utf-8"), encoding="utf-8")
    cfg_path.write_text(_json.dumps(cfg, indent=2) + "\n", encoding="utf-8")

    return {"removed": True, "config_path": str(cfg_path), "server_key": name}


# ---------------------------------------------------------------------------
# Status
# ---------------------------------------------------------------------------


def status(drive: str | Path) -> dict:
    """Return a status snapshot for a drive (encrypted vault present, count, etc.)."""
    from . import crypto

    drive = Path(drive)
    vp = vault_path(drive)

    result: dict = {
        "drive": str(drive),
        "vault_dir": str(vp),
        "vault_present": is_usb_vault(drive),
        "encrypted": False,
        "note_count": 0,
        "launcher_present": (drive / LAUNCHER_NAME).exists(),
        "size_gb": _drive_free_gb(drive),
    }

    if not result["vault_present"]:
        return result

    result["encrypted"] = crypto is not None and crypto.is_encrypted(vp)
    result["note_count"] = len(list((vp / "notes").glob("*.md*")))
    return result


# ---------------------------------------------------------------------------
# Safe eject
# ---------------------------------------------------------------------------


def eject(drive: str | Path) -> str:
    """Attempt a safe-eject of the drive. Returns a status string."""
    drive = str(Path(drive))
    try:
        if SYSTEM == "Darwin":
            r = subprocess.run(
                ["diskutil", "eject", drive],
                capture_output=True,
                text=True,
                check=False,
            )
            return r.stdout.strip() or r.stderr.strip() or "Ejected."
        if SYSTEM == "Windows":
            # PowerShell safe-remove via WMI. Escape single quotes and wrap the
            # drive path in a single-quoted PS literal so no character in the
            # path (quotes, `;`, `$`, backticks, subexpressions) can execute.
            escaped = drive.replace("'", "''")
            ps = (
                "$d=(New-Object -comObject Shell.Application).Namespace(17)"
                f".ParseName('{escaped}'); $d.InvokeVerb('Eject')"
            )
            subprocess.run(["powershell", "-Command", ps], check=False, capture_output=True)
            return f"Eject request sent for {drive}."
        # Linux — udisksctl
        r = subprocess.run(
            ["udisksctl", "unmount", "-b", _linux_block_dev(drive)],
            capture_output=True,
            text=True,
            check=False,
        )
        r2 = subprocess.run(
            ["udisksctl", "power-off", "-b", _linux_block_dev(drive)],
            capture_output=True,
            text=True,
            check=False,
        )
        return (r.stdout + r2.stdout).strip() or "Eject request sent."
    except FileNotFoundError:
        return f"Could not eject automatically — please use your OS to safely remove {drive}."


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------


def _validate_key(vault_dir: Path, key: bytes) -> None:
    """Raise ValueError if the key cannot decrypt an existing note.
    If there are no notes yet (empty vault), the key is accepted as valid."""
    encs = sorted((vault_dir / "notes").glob("*.md.enc"))
    if not encs:
        return  # empty vault — nothing to validate against, trust it
    try:
        crypto.decrypt(key, encs[0].read_bytes())
    except Exception:
        raise ValueError("Wrong passphrase — could not unlock the vault.")


def _patch_passphrase_in_config(config_path: Path, server_key: str, passphrase: str) -> None:
    """Persist the vault passphrase so the MCP server can unlock the vault.

    Preferred path: store in the OS credential manager (Windows Credential
    Manager, macOS Keychain, or Linux keyring via the optional ``keyring``
    package) keyed by ``server_key`` and record only the non-secret
    ``ELESYNC_SERVER_KEY`` in the client's MCP config env.

    Fallback (no OS credential store available): write ``ELESYNC_PASSPHRASE``
    to the config env, which is what older EleSync versions did. The config
    file is typically world-readable and often sync'd to cloud dotfiles, so
    the fallback logs a warning.
    """
    import json as _json

    from . import credential_store

    stored_in_keychain = False
    try:
        credential_store.set_secret(f"vault:{server_key}", passphrase)
        stored_in_keychain = True
    except OSError:
        _log.warning(
            "no OS credential store available — vault passphrase will be written "
            "to the MCP client config in plaintext; install the 'keyring' package "
            "or use an OS that provides one to keep it out of the config file."
        )

    try:
        cfg = _json.loads(config_path.read_text(encoding="utf-8"))
        env = cfg["mcpServers"][server_key].setdefault("env", {})
        if stored_in_keychain:
            env["ELESYNC_SERVER_KEY"] = server_key
            env.pop("ELESYNC_PASSPHRASE", None)
        else:
            env["ELESYNC_PASSPHRASE"] = passphrase
        config_path.write_text(_json.dumps(cfg, indent=2) + "\n", encoding="utf-8")
    except Exception:
        _log.debug("best-effort passphrase injection failed for %s", config_path, exc_info=True)


def _drive_free_gb(drive: Path) -> float:
    try:
        s = shutil.disk_usage(drive)
        return round(s.free / 1e9, 1)
    except OSError:
        return 0.0


def _linux_block_dev(mountpoint: str) -> str:
    """Best-effort: find the block device for a mount point from /proc/mounts."""
    try:
        for line in Path("/proc/mounts").read_text().splitlines():
            parts = line.split()
            if len(parts) >= 2 and parts[1] == mountpoint:
                return parts[0]
    except Exception:
        _log.debug("failed to resolve device for mountpoint %s", mountpoint, exc_info=True)
    return mountpoint  # fallback — will likely fail but won't crash


# ---------------------------------------------------------------------------
# Self-contained launcher (written to the drive root)
# ---------------------------------------------------------------------------

_LAUNCHER_TEMPLATE = '''\
#!/usr/bin/env python3
"""
┌──────────────────────────────────────────────────────────────────────┐
│  🐘  EleSync — Portable AI Memory                                    │
│                                                                      │
│  This script sets up EleSync from this USB drive on any computer.   │
│  You need Python 3.10+ with EleSync already installed.               │
│                                                                      │
│  Just run:  python RUN_ME.py                                         │
└──────────────────────────────────────────────────────────────────────┘

What this does:
  1. Checks that EleSync is installed. EleSync is not on PyPI, so install
     it first; if it's missing, this script
     tells you how and stops.
  2. Connects this USB vault to your AI client — one onboard command.
  3. You're done. Restart your AI client to activate.
"""
import os, sys, subprocess, pathlib

HERE = pathlib.Path(__file__).parent.resolve()
VAULT = HERE / {vault_dir_name!r}

print()
print("🐘  EleSync Portable Setup")
print("=" * 50)

# --- Step 1: check EleSync is installed ----------------------------------
# EleSync is not on PyPI, so this script can't install it for you.
try:
    import elesync  # noqa: F401
except ImportError:
    print("❌  EleSync is not installed for this Python.")
    print()
    print("    EleSync is not on PyPI, so this")
    print("    script can't install it. Install it first:")
    print()
    print("    • From source: download the ZIP from the private GitHub repository")
    print("      (github.com/darknodebros/EleSync → Code → Download ZIP), run")
    print('        python -m pip install "<path to EleSync-master.zip>"')
    print("      and then run this script again.")
    print()
    if os.name == "nt":
        ele_exe = os.path.join(
            os.environ.get("LOCALAPPDATA", "%LOCALAPPDATA%"),
            "Programs", "EleSync", "command", "ele.exe",
        )
        print("    • Windows installer (EleSync-Setup.exe from the latest release):")
        print("      it brings its own copy of EleSync, so connect this drive with")
        print('        "' + ele_exe + '" usb attach "' + str(HERE) + '"')
        print('      (installed in another folder? use ele.exe from its "command" subfolder)')
        print()
    sys.exit(1)
print("✅  EleSync is installed.")

# --- Step 2: wire your AI client -----------------------------------------
print()
print("🔌  Connecting vault to your AI client …")
r = subprocess.run(
    [
        sys.executable, "-m", "elesync.cli",
        "--vault", str(VAULT),
        "onboard",
        "--name", "elesync-usb",
        "--print-only",
    ],
    capture_output=True,
    text=True,
)
if r.returncode == 0:
    subprocess.run(
        [
            sys.executable, "-m", "elesync.cli",
            "--vault", str(VAULT),
            "onboard",
            "--name", "elesync-usb",
        ],
        check=False,
    )
    print("✅  AI client wired. Set ELESYNC_PASSPHRASE env var to your vault passphrase,")
    print("    or run:  ele usb attach", str(HERE))
else:
    print("⚠️   Could not auto-wire your AI client.")
    print("    Run this to connect manually:")
    print()
    subprocess.run(
        [
            sys.executable, "-m", "elesync.cli",
            "--vault", str(VAULT),
            "onboard",
            "--print-only",
            "--name", "elesync-usb",
        ],
        check=False,
    )

print()
print("=" * 50)
print("✅  Done! Restart your AI client to activate.")
print("    Then ask it: \\"What do you remember about me?\\"")
print()
'''

_README_TEMPLATE = """\
EleSync — Portable AI Memory
=============================

This USB drive contains your personal AI memory vault.

Your memories are encrypted on this drive. Nobody can read them
without your passphrase — not even if this drive is lost or stolen.

━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━
  HOW TO USE ON A NEW COMPUTER
━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━

Requirements: Python 3.10 or newer  (python.org/downloads), with
              EleSync installed. EleSync is not on PyPI; if it's
              missing, RUN_ME.py tells you how to install it.

  1. Open a terminal (Command Prompt on Windows, Terminal on Mac/Linux)
  2. Run:   python RUN_ME.py
  3. Enter your passphrase when asked
  4. Restart your AI client

That's it. Your AI will know everything you've shared with any AI.

━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━
  IMPORTING MEMORIES FROM YOUR AIs
━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━

ChatGPT: Settings → Data controls → Export memory → save .json here
Claude:  claude.ai → Settings → Export data → memory export
Gemini:  Google Takeout → Gemini → download ZIP → extract .json here

Then run:
  ele --vault {vault_dir} import chatgpt_memory.json  --source chatgpt
  ele --vault {vault_dir} import claude_export.json   --source claude
  ele --vault {vault_dir} import gemini_memory.json   --source gemini

━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━
  BEFORE UNPLUGGING
━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━

Run:  ele usb detach {drive_placeholder}
Then eject the drive normally from your OS.

━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━
  VAULT LOCATION ON THIS DRIVE
━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━

  {vault_dir}/notes/    ← your encrypted memory files
  {vault_dir}/vault.json ← encryption parameters (no secrets)

━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━
  NEED HELP?
━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━

  ele doctor              — run a health check
  ele usb status <drive>  — check vault status
  github.com/darknodebros/EleSync/issues

"""


def _write_launcher(drive: Path, vault_dir: Path) -> None:
    launcher = drive / LAUNCHER_NAME
    launcher.write_text(
        _LAUNCHER_TEMPLATE.format(vault_dir_name=VAULT_DIR_NAME),
        encoding="utf-8",
    )


def _write_readme(drive: Path, vault_dir: Path, label: str) -> None:
    readme = drive / README_NAME
    readme.write_text(
        _README_TEMPLATE.format(
            vault_dir=str(vault_dir),
            drive_placeholder=str(drive),
        ),
        encoding="utf-8",
    )
