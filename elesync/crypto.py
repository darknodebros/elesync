"""Optional encryption at rest.

Threat model — read this. Encrypting a vault protects your notes **at rest**:
a stolen disk, a leaked backup, a synced folder on someone else's machine.
It does **not** protect against a compromised running process or a host where
your passphrase is in memory. Plaintext markdown stays the default (so the vault
is Obsidian-readable); encryption is opt-in for the privacy-conscious.

Requires PyNaCl, which EleSync installs as a dependency. If it's missing:
pip install "pynacl>=1.5"

How it works (Phase 1 — encryption at rest):
  * A passphrase is stretched to a key with **argon2id** (salt + params live in
    `vault.json`; the key itself is never written to disk).
  * Each note `notes/<id>.md` is encrypted to `notes/<id>.md.enc` with
    XSalsa20-Poly1305 (libsodium SecretBox — authenticated, random nonce per file).
  * The plaintext `index.db` is removed (it would otherwise leak content); it is
    rebuilt from the decrypted notes via `reindex` when you `ele decrypt`.

Cost of the no-plaintext-index rule: while an encrypted vault is *open* (an MCP
server session, `ele --vault … search`, the web UI), its whole search index is
held in memory rather than on disk — there is nowhere safe to persist it. Expect
memory use on the order of your total note text plus SQLite/FTS overhead (and,
with the semantic extra, ~1.5 KB per memory of vectors). A 10k-note vault is
tens of MB; it grows linearly, so a very large encrypted vault on a small host
should budget for it. Plaintext vaults keep their index in `index.db` and have
no such ceiling. See `MemoryStore.__init__` for the precise footprint note.

The metadata helpers (`is_encrypted`, `read_meta`) need no crypto library, so the
rest of the app can *detect* an encrypted vault even without the extra installed.
"""

from __future__ import annotations

import base64
import json
from pathlib import Path

MARKER = "elesync_encryption"
META_FILE = "vault.json"
# Bound untrusted vault/backup metadata before entering the native KDF.
# This accepts libsodium's interactive and moderate profiles, including the
# existing default (3 operations, 256 MiB), without permitting multi-GiB work.
MAX_KDF_OPSLIMIT = 4
MAX_KDF_MEMLIMIT = 256 << 20


# -- metadata (pure stdlib; works without PyNaCl) -------------------------
def read_meta(vault_dir) -> dict | None:
    """Return the encryption metadata dict for a vault, or None if not encrypted."""
    p = Path(vault_dir).expanduser() / META_FILE
    if not p.exists():
        return None
    try:
        meta = json.loads(p.read_text(encoding="utf-8"))
    except (json.JSONDecodeError, OSError):
        return None
    return meta if isinstance(meta, dict) and meta.get(MARKER) else None


def is_encrypted(vault_dir) -> bool:
    return read_meta(vault_dir) is not None


def write_meta(vault_dir, salt: bytes, opslimit: int, memlimit: int) -> None:
    meta = {
        MARKER: 1,
        "kdf": "argon2id",
        "cipher": "xsalsa20poly1305",
        "salt": base64.b64encode(salt).decode("ascii"),
        "opslimit": opslimit,
        "memlimit": memlimit,
    }
    (Path(vault_dir).expanduser() / META_FILE).write_text(
        json.dumps(meta, indent=2) + "\n", encoding="utf-8"
    )


# -- crypto (requires the optional PyNaCl extra) --------------------------
def available() -> bool:
    try:
        from nacl import pwhash as _  # noqa: F401

        return True
    except ImportError:
        return False


def default_limits() -> tuple[int, int]:
    from nacl import pwhash

    return pwhash.argon2id.OPSLIMIT_MODERATE, pwhash.argon2id.MEMLIMIT_MODERATE


def new_salt() -> bytes:
    from nacl import pwhash, utils

    return utils.random(pwhash.argon2id.SALTBYTES)


def derive_key(passphrase: str, salt: bytes, opslimit: int, memlimit: int) -> bytes:
    """Stretch a passphrase into a 32-byte key with argon2id."""
    from nacl import pwhash, secret

    if (
        type(opslimit) is not int
        or not pwhash.argon2id.OPSLIMIT_MIN <= opslimit <= MAX_KDF_OPSLIMIT
        or type(memlimit) is not int
        or not pwhash.argon2id.MEMLIMIT_MIN <= memlimit <= MAX_KDF_MEMLIMIT
        or not isinstance(salt, bytes)
        or len(salt) != pwhash.argon2id.SALTBYTES
    ):
        raise ValueError("Invalid or excessive Argon2 parameters in encryption metadata")
    return pwhash.argon2id.kdf(
        secret.SecretBox.KEY_SIZE,
        passphrase.encode("utf-8"),
        salt,
        opslimit=opslimit,
        memlimit=memlimit,
    )


def encrypt(key: bytes, plaintext: bytes) -> bytes:
    """Authenticated encryption; output carries its own random nonce."""
    from nacl import secret

    return bytes(secret.SecretBox(key).encrypt(plaintext))


def decrypt(key: bytes, blob: bytes) -> bytes:
    """Inverse of `encrypt`. Raises nacl.exceptions.CryptoError on a wrong key
    or tampered data (the authentication tag fails)."""
    from nacl import secret

    return secret.SecretBox(key).decrypt(blob)
