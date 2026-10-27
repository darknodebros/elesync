"""Versioned backup archive envelope (#91).

`ele backup` writes a self-describing archive instead of raw JSON:

    magic    b"ELEBK1"                                    # 6 bytes, versioned
    header   <uint32 big-endian length><header-json utf-8>  # plaintext
    body     encrypt?( gzip( payload-json utf-8 ) )

The header is always plaintext, so metadata (count, created_at, format version,
encryption state) can be read without the passphrase — the same principle behind
`crypto.read_meta`, and what `ele backup list` (#92) relies on. Only the body (the
memories themselves) is encrypted.

Compress-then-encrypt: the payload is gzipped first, then optionally sealed with the
vault's XSalsa20-Poly1305 primitive (see ``crypto.py``). The compression-ratio side
channel that motivates encrypt-then-compress (CRIME/BREACH) does not apply to an
at-rest, whole-file backup, so the standard compress-then-encrypt order is used.

This module never prompts for or derives keys — it takes a ready 32-byte key (or
``None``). Passphrase handling and i18n live in the CLI so the envelope stays reusable
by #92 (listing) and #93 (remote) without dragging in getpass or the message catalog.
"""

from __future__ import annotations

import gzip
import json
import os
import struct
import tempfile
import zlib
from pathlib import Path

from . import crypto

MAGIC = b"ELEBK1"
FORMAT_VERSION = 1
MAX_HEADER_BYTES = 64 << 10  # 64 KiB
MAX_BODY_BYTES = 256 << 20  # 256 MiB compressed/encrypted
MAX_DECOMPRESSED_BYTES = 512 << 20  # 512 MiB JSON payload


class ArchiveError(Exception):
    """A non-archive file, a truncated/corrupt envelope, or an unsupported
    format version."""


class DecryptError(ArchiveError):
    """The body could not be decrypted — a wrong passphrase or a tampered
    archive (the authentication tag failed). A subclass of ArchiveError so a
    caller can distinguish 'wrong key' from 'bad envelope' by catching this first."""


def is_archive(path) -> bool:
    """True if the file begins with the archive magic. Cheap — reads a few bytes."""
    try:
        with open(path, "rb") as fh:
            return fh.read(len(MAGIC)) == MAGIC
    except OSError:
        return False


def _read_framed(path) -> tuple[dict, bytes]:
    """Return (header dict, body bytes). Validates magic + format version."""
    with open(path, "rb") as fh:
        magic = fh.read(len(MAGIC))
        if magic != MAGIC:
            raise ArchiveError("not an EleSync backup archive")
        length_bytes = fh.read(4)
        if len(length_bytes) != 4:
            raise ArchiveError("truncated archive header")
        (hlen,) = struct.unpack(">I", length_bytes)
        if hlen > MAX_HEADER_BYTES:
            raise ArchiveError("archive header exceeds the 64 KiB limit")
        header_bytes = fh.read(hlen)
        if len(header_bytes) != hlen:
            raise ArchiveError("truncated archive header")
        body = fh.read(MAX_BODY_BYTES + 1)
        if len(body) > MAX_BODY_BYTES:
            raise ArchiveError("archive body exceeds the 256 MiB limit")
    if magic != MAGIC:
        raise ArchiveError("not an EleSync backup archive")
    try:
        header = json.loads(header_bytes.decode("utf-8"))
    except (UnicodeDecodeError, json.JSONDecodeError) as e:
        raise ArchiveError(f"unreadable archive header: {e}") from e
    fv = header.get("format_version")
    if fv != FORMAT_VERSION:
        raise ArchiveError(
            f"unsupported backup format version {fv!r} (this build reads {FORMAT_VERSION})"
        )
    return header, body


def header_of(path) -> dict:
    """Read just the (plaintext) header — for listing without a passphrase (#92)."""
    header, _body = _read_framed(path)
    return header


def write(
    path,
    payload: dict,
    *,
    key: bytes | None = None,
    kdf_meta: dict | None = None,
) -> None:
    """Write ``payload`` (a JSON-serialisable dict) as an archive. If ``key`` is
    given, the body is encrypted and ``kdf_meta`` (kdf/cipher/base64 salt/opslimit/
    memlimit) is recorded in the plaintext header so restore can re-derive the key."""
    body = gzip.compress(json.dumps(payload, ensure_ascii=False).encode("utf-8"))
    encryption = None
    if key is not None:
        body = crypto.encrypt(key, body)
        encryption = dict(kdf_meta or {})
    header = {
        "format": "elesync-backup",
        "format_version": FORMAT_VERSION,
        "created_at": payload.get("exported_at"),
        "count": payload.get("count"),
        "compression": "gzip",
        "encryption": encryption,
    }
    header_bytes = json.dumps(header, ensure_ascii=False).encode("utf-8")
    # Atomic write — build the archive in a sibling tempfile and only replace
    # `path` on a successful close, so a crash mid-write cannot leave a
    # truncated archive at `path`. os.replace is atomic on the same filesystem
    # on both POSIX and Windows.
    target = Path(path)
    target.parent.mkdir(parents=True, exist_ok=True)
    tmp = tempfile.NamedTemporaryFile(
        mode="wb", dir=target.parent, prefix=target.name + ".", suffix=".tmp", delete=False
    )
    try:
        tmp.write(MAGIC)
        tmp.write(struct.pack(">I", len(header_bytes)))
        tmp.write(header_bytes)
        tmp.write(body)
        tmp.flush()
        os.fsync(tmp.fileno())
        tmp.close()
        os.replace(tmp.name, target)
    except Exception:
        tmp.close()
        try:
            os.unlink(tmp.name)
        except OSError:
            pass
        raise


def read(path, *, key: bytes | None = None) -> dict:
    """Return the inner payload dict. If the archive is encrypted, ``key`` must be
    the derived 32-byte key; a wrong key or tampered body raises ``DecryptError``.
    Raises ``ArchiveError`` for a non-archive / unsupported / corrupt envelope."""
    header, body = _read_framed(path)
    if header.get("encryption"):
        if key is None:
            raise ArchiveError("archive is encrypted but no key was provided")
        from nacl.exceptions import CryptoError

        try:
            body = crypto.decrypt(key, body)  # authenticates the key
        except CryptoError as e:
            raise DecryptError("wrong passphrase or tampered archive") from e
    try:
        decompressor = zlib.decompressobj(wbits=16 + zlib.MAX_WBITS)
        raw = decompressor.decompress(body, MAX_DECOMPRESSED_BYTES + 1)
        if len(raw) > MAX_DECOMPRESSED_BYTES or decompressor.unconsumed_tail:
            raise ArchiveError("archive expands beyond the 512 MiB limit")
        raw += decompressor.flush()
        if len(raw) > MAX_DECOMPRESSED_BYTES:
            raise ArchiveError("archive expands beyond the 512 MiB limit")
        if not decompressor.eof:
            raise ArchiveError("corrupt archive body: truncated gzip stream")
    except zlib.error as e:
        raise ArchiveError(f"corrupt archive body: {e}") from e
    try:
        payload = json.loads(raw.decode("utf-8"))
    except (UnicodeDecodeError, json.JSONDecodeError) as e:
        raise ArchiveError(f"unreadable archive payload: {e}") from e
    if not isinstance(payload, dict):
        raise ArchiveError("archive payload is not an object")
    return payload
