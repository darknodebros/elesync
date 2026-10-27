"""Small OS-backed secret storage adapter.

Windows uses Credential Manager through the standard Win32 API. Other systems
use the optional ``keyring`` package when present; callers can fall back to an
environment variable or a legacy config value during migration.
"""

from __future__ import annotations

import ctypes
import os
from ctypes import POINTER, Structure, byref, c_byte, c_void_p, cast
from ctypes.wintypes import BOOL, DWORD, FILETIME, LPCWSTR, LPWSTR

_SERVICE = "EleSync"
_CRED_TYPE_GENERIC = 1
_CRED_PERSIST_LOCAL_MACHINE = 2


class _Credential(Structure):
    _fields_ = [
        ("Flags", DWORD),
        ("Type", DWORD),
        ("TargetName", LPWSTR),
        ("Comment", LPWSTR),
        ("LastWritten", FILETIME),
        ("CredentialBlobSize", DWORD),
        ("CredentialBlob", POINTER(c_byte)),
        ("Persist", DWORD),
        ("AttributeCount", DWORD),
        ("Attributes", c_void_p),
        ("TargetAlias", LPWSTR),
        ("UserName", LPWSTR),
    ]


def _target(name: str) -> str:
    return f"{_SERVICE}:{name}"


def _last_windows_error() -> int:
    # get_last_error is exposed dynamically by ctypes on Windows and is
    # absent from mypy's cross-platform stubs.
    return int(getattr(ctypes, "get_last_error", lambda: 0)())


def _windows_api():
    advapi = ctypes.windll.advapi32
    kernel = ctypes.windll.kernel32
    advapi.CredWriteW.argtypes = [POINTER(_Credential), DWORD]
    advapi.CredWriteW.restype = BOOL
    advapi.CredReadW.argtypes = [LPCWSTR, DWORD, DWORD, POINTER(c_void_p)]
    advapi.CredReadW.restype = BOOL
    advapi.CredDeleteW.argtypes = [LPCWSTR, DWORD, DWORD]
    advapi.CredDeleteW.restype = BOOL
    advapi.CredFree.argtypes = [c_void_p]
    advapi.CredFree.restype = None
    return advapi, kernel


def set_secret(name: str, value: str) -> None:
    """Store a secret in the current user's OS credential store."""
    if os.name == "nt":
        advapi, _ = _windows_api()
        blob = value.encode("utf-8")
        blob_buffer = (c_byte * len(blob)).from_buffer_copy(blob)
        credential = _Credential(
            Type=_CRED_TYPE_GENERIC,
            TargetName=_target(name),
            CredentialBlobSize=len(blob),
            CredentialBlob=cast(blob_buffer, POINTER(c_byte)),
            Persist=_CRED_PERSIST_LOCAL_MACHINE,
            UserName="EleSync",
        )
        if not advapi.CredWriteW(byref(credential), 0):
            raise OSError(_last_windows_error(), "Windows Credential Manager rejected the secret")
        return
    try:
        import keyring
    except ImportError as exc:
        raise OSError("no OS credential store is available") from exc
    try:
        keyring.set_password(_SERVICE, name, value)
    except keyring.errors.KeyringError as exc:
        raise OSError("OS credential store is unavailable") from exc


def get_secret(name: str) -> str | None:
    """Return a stored secret, or ``None`` when it does not exist."""
    if os.name == "nt":
        advapi, _ = _windows_api()
        raw = c_void_p()
        if not advapi.CredReadW(_target(name), _CRED_TYPE_GENERIC, 0, byref(raw)):
            return None
        try:
            credential = cast(raw, POINTER(_Credential)).contents
            blob = cast(
                credential.CredentialBlob, POINTER(c_byte * credential.CredentialBlobSize)
            ).contents
            return bytes(blob).decode("utf-8")
        finally:
            advapi.CredFree(raw)
    try:
        import keyring
    except ImportError:
        return None
    try:
        return keyring.get_password(_SERVICE, name)
    except keyring.errors.KeyringError as exc:
        raise OSError("OS credential store is unavailable") from exc


def delete_secret(name: str) -> None:
    """Delete a stored secret; missing credentials are ignored."""
    if os.name == "nt":
        advapi, _ = _windows_api()
        advapi.CredDeleteW(_target(name), _CRED_TYPE_GENERIC, 0)
        return
    try:
        import keyring
    except ImportError:
        return
    try:
        keyring.delete_password(_SERVICE, name)
    except Exception:
        pass
