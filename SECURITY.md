# Security Policy

## Supported Versions

EleSync provides security fixes for the current supported release.

Users should upgrade to the latest available version when a security update
is published. Security advisories will identify affected version ranges when
necessary.

Historical open-source releases may no longer receive active security
maintenance.

## Reporting a Vulnerability

Please do not disclose suspected security vulnerabilities publicly.

Report security issues privately to:

**hello@elesync.dev**

Include, when possible:

1. A description of the vulnerability
2. Steps required to reproduce it
3. The affected EleSync version
4. Relevant logs, screenshots, or technical details
5. A suggested remediation, if available

Reports will be reviewed as quickly as reasonably possible. Valid
vulnerabilities will be investigated and addressed according to their severity.

Please avoid accessing, modifying, or exposing data belonging to other users
while investigating a potential vulnerability.

## Security Scope

EleSync is a local-first AI workspace and continuity platform that allows
users to maintain project context and selectively make that context available
to compatible AI clients.

Important security boundaries include:

- **Companion and remote connectivity** — Remote access must use authenticated,
  encrypted connections. Public Companion pairing requires dashboard
  authentication and secure HTTPS tunnel addresses.
- **Web dashboard authentication** — Authentication must be enabled before
  exposing EleSync beyond the local machine.
- **MCP transport** — Remote MCP access must remain authenticated and protected
  against unauthorized network exposure.
- **Project and scope isolation** — Connected AI clients must only access
  context explicitly granted to them.
- **Import processing** — AI exports and other imported files are untrusted
  input and must never result in arbitrary code execution.
- **Local data protection** — Sensitive workspace data should remain protected
  through EleSync's supported encryption and authentication mechanisms.
- **Credentials and tokens** — Authentication tokens, passwords, API
  credentials, and encryption secrets must never be committed to the
  repository or exposed in logs.

## Coordinated Disclosure

EleSync follows coordinated vulnerability disclosure practices.

Security fixes should be made available before detailed vulnerability
information is publicly disclosed, unless disclosure is required by law or
necessary to protect users.

## Network and dependency defaults

Use loopback listeners behind an HTTPS tunnel or proxy, or configure TLS for
direct network access. Plain HTTP on non-loopback interfaces now requires an
explicit unsafe override. Forwarded scheme headers are trusted only from a
loopback peer; the proxy must overwrite client-supplied forwarding headers.

CI and release dependencies are pinned in `uv.lock`. Semantic support must be
installed explicitly; searches do not install executable dependencies. Argon2
metadata is limited to four operations and 256 MiB. Archives made with custom
parameters above those limits are rejected and require a separately reviewed
offline recovery process.

### Optional semantic dependencies

Python 3.11 or newer is required to use patched Pillow 13. Install optional
semantic search with `uv sync --locked --extra semantic`; the lock overrides
Fastembed's obsolete Pillow upper bound for our text-only backend. Plain pip
resolves that upstream conflict by refusing the semantic extra. Keyword search
remains available without semantic dependencies. Do not downgrade Pillow.
