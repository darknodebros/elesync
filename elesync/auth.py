"""Authentication helpers for the EleSync HTTP transport.

Token-based Bearer authentication:
  - generate_token() creates a secure random token
  - verify_token() uses constant-time comparison to prevent timing attacks
  - get_configured_token() resolves the active token from env or config

mTLS authentication:
  - setup_mtls() generates a self-signed CA, server cert, and client cert
  - get_mtls_config() resolves the active mTLS paths from env or config
"""

from __future__ import annotations

import datetime
import hmac
import os
import secrets
from pathlib import Path


def generate_token() -> str:
    """Generate a secure random URL-safe token (43 chars, ~256 bits of entropy)."""
    return secrets.token_urlsafe(32)


def verify_token(request_token: str, stored_token: str) -> bool:
    """Constant-time comparison to prevent timing attacks."""
    return hmac.compare_digest(request_token, stored_token)


def get_configured_token(config: dict | None = None) -> str | None:
    """Resolve the active auth token.

    Priority:
    1. ELESYNC_TOKEN environment variable
    2. config["http"]["auth"]["token"]  (from config.json)
    """
    env_token = os.environ.get("ELESYNC_TOKEN")
    if env_token:
        return env_token
    if config:
        return config.get("http", {}).get("auth", {}).get("token")
    return None


def get_auth_mode(config: dict | None = None) -> str:
    """Return the configured auth mode (default: 'none')."""
    env_token = os.environ.get("ELESYNC_TOKEN")
    if env_token:
        return "token"
    if config:
        return config.get("http", {}).get("auth", {}).get("mode", "none")
    return "none"


# ── mTLS ─────────────────────────────────────────────────────────────────
#
# Token auth over a tunnel sends the Bearer token on every request in
# cleartext at the application layer — the tunnel provides TLS, but the
# user doesn't control its termination. mTLS closes that gap: the client
# cert *is* the auth, and it can't be replayed without the private key.
#
# setup_mtls() generates a self-signed CA, a server cert (for the EleSync
# HTTP transport), and a client cert (for the MCP client to present).
# The server is configured to require and verify client certs against the
# CA. This is the mTLS half of issue #58.


def get_mtls_config(config: dict | None = None) -> dict | None:
    """Resolve the active mTLS cert paths.

    Priority:
    1. ELESYNC_MTLS_CA_CERT / ELESYNC_MTLS_SERVER_CERT / ELESYNC_MTLS_SERVER_KEY
    2. config["http"]["auth"]["mtls"]  (from config.json)

    Returns a dict with keys 'ca_cert', 'server_cert', 'server_key' (all
    absolute path strings), or None if mTLS is not configured.
    """
    ca = os.environ.get("ELESYNC_MTLS_CA_CERT")
    cert = os.environ.get("ELESYNC_MTLS_SERVER_CERT")
    key = os.environ.get("ELESYNC_MTLS_SERVER_KEY")
    if ca and cert and key:
        return {"ca_cert": ca, "server_cert": cert, "server_key": key}
    if config:
        mtls = config.get("http", {}).get("auth", {}).get("mtls")
        if mtls and mtls.get("ca_cert") and mtls.get("server_cert") and mtls.get("server_key"):
            return {
                "ca_cert": str(mtls["ca_cert"]),
                "server_cert": str(mtls["server_cert"]),
                "server_key": str(mtls["server_key"]),
            }
    return None


def setup_mtls(cert_dir: str | Path) -> dict:
    """Generate a self-signed CA, server cert, and client cert for mTLS.

    Writes six files into ``cert_dir``:
      ca.crt, ca.key           — the root CA (server verifies client certs against this)
      server.crt, server.key    — the EleSync HTTP transport's cert
      client.crt, client.key    — the cert the MCP client presents

    Returns a dict with the three paths the server needs:
      {'ca_cert': ..., 'server_cert': ..., 'server_key': ...}

    Requires the ``cryptography`` package. Raises
    ImportError with a helpful message if it's not installed.
    """
    try:
        from cryptography import x509
        from cryptography.hazmat.primitives import hashes, serialization
        from cryptography.hazmat.primitives.asymmetric import rsa
        from cryptography.x509.oid import NameOID
    except ImportError as exc:  # pragma: no cover
        raise ImportError(
            "mTLS cert generation requires the 'cryptography' package.\n"
            'Install it with:  pip install "cryptography>=42.0"'
        ) from exc

    cert_dir = Path(cert_dir)
    cert_dir.mkdir(parents=True, exist_ok=True)

    now = datetime.datetime.now(datetime.timezone.utc)
    one_year = datetime.timedelta(days=365)

    def _name(cn: str):
        return x509.Name([x509.NameAttribute(NameOID.COMMON_NAME, cn)])

    def _gen_key():
        return rsa.generate_private_key(public_exponent=65537, key_size=2048)

    def _write_key(key, path: Path):
        # Private keys are written to disk unencrypted so the server can load
        # them without an interactive passphrase; restrict them to owner-only
        # on POSIX to prevent readback by other local users and to reduce the
        # blast radius of a `cert_dir` that ends up in a backup or cloud sync.
        # Callers should still avoid syncing `cert_dir` off-machine.
        path.write_bytes(
            key.private_bytes(
                encoding=serialization.Encoding.PEM,
                format=serialization.PrivateFormat.TraditionalOpenSSL,
                encryption_algorithm=serialization.NoEncryption(),
            )
        )
        try:
            os.chmod(path, 0o600)
        except OSError:
            # Best-effort — Windows and some filesystems ignore chmod. The
            # user-profile directory is already user-scoped by default there.
            pass

    def _write_cert(cert, path: Path):
        path.write_bytes(cert.public_bytes(serialization.Encoding.PEM))

    # ── CA ──────────────────────────────────────────────────────────────
    ca_key = _gen_key()
    ca_cert = (
        x509.CertificateBuilder()
        .subject_name(_name("EleSync Local CA"))
        .issuer_name(_name("EleSync Local CA"))
        .public_key(ca_key.public_key())
        .serial_number(x509.random_serial_number())
        .not_valid_before(now)
        .not_valid_after(now + one_year * 5)
        .add_extension(x509.BasicConstraints(ca=True, path_length=None), critical=True)
        .add_extension(
            x509.KeyUsage(
                digital_signature=True,
                content_commitment=False,
                key_encipherment=False,
                data_encipherment=False,
                key_agreement=False,
                key_cert_sign=True,
                crl_sign=True,
                encipher_only=False,
                decipher_only=False,
            ),
            critical=True,
        )
        .sign(ca_key, hashes.SHA256())
    )
    _write_key(ca_key, cert_dir / "ca.key")
    _write_cert(ca_cert, cert_dir / "ca.crt")

    # ── Server cert ────────────────────────────────────────────────────
    server_key = _gen_key()
    server_cert = (
        x509.CertificateBuilder()
        .subject_name(_name("EleSync Server"))
        .issuer_name(ca_cert.subject)
        .public_key(server_key.public_key())
        .serial_number(x509.random_serial_number())
        .not_valid_before(now)
        .not_valid_after(now + one_year)
        .add_extension(x509.BasicConstraints(ca=False, path_length=None), critical=True)
        .add_extension(
            x509.ExtendedKeyUsage([x509.ExtendedKeyUsageOID.SERVER_AUTH]), critical=False
        )
        .sign(ca_key, hashes.SHA256())
    )
    _write_key(server_key, cert_dir / "server.key")
    _write_cert(server_cert, cert_dir / "server.crt")

    # ── Client cert ────────────────────────────────────────────────────
    client_key = _gen_key()
    client_cert = (
        x509.CertificateBuilder()
        .subject_name(_name("EleSync Client"))
        .issuer_name(ca_cert.subject)
        .public_key(client_key.public_key())
        .serial_number(x509.random_serial_number())
        .not_valid_before(now)
        .not_valid_after(now + one_year)
        .add_extension(x509.BasicConstraints(ca=False, path_length=None), critical=True)
        .add_extension(
            x509.ExtendedKeyUsage([x509.ExtendedKeyUsageOID.CLIENT_AUTH]), critical=False
        )
        .sign(ca_key, hashes.SHA256())
    )
    _write_key(client_key, cert_dir / "client.key")
    _write_cert(client_cert, cert_dir / "client.crt")

    return {
        "ca_cert": str(cert_dir / "ca.crt"),
        "server_cert": str(cert_dir / "server.crt"),
        "server_key": str(cert_dir / "server.key"),
    }
