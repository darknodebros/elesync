# Changelog

All notable changes to EleSync are documented here. The format follows
[Keep a Changelog](https://keepachangelog.com/), and versions follow
[Semantic Versioning](https://semver.org/).

## [1.24.80] - 2026-10-28

### Added
- Restore GitHub Pages hosting, PyPI publishing, and the MCP Registry manifest and publishing pipeline.
- Public Linux x86-64 standalone dashboard launcher (`EleSync-linux`) and CLI (`ele-linux`), built on Ubuntu 22.04 and smoke-tested with the bundled dashboard assets.
- Platform dependency inventories and checksums, with a combined `SHA256SUMS` file.

## [Unreleased]

## [1.24.79] - 2026-10-27

### Release

- Publish the first downloadable Windows release from the new public repository.

## [1.24.78] - 2026-10-27

### Security

- Require Python 3.11+ and patched Pillow 13 for optional semantic search. Audit optional and build dependencies in CI as well as the installed runtime. Use the locked uv semantic installation while Fastembed retains its obsolete Pillow upper bound.

- Direct network dashboard/MCP listeners require TLS by default. Loopback remains available for local clients and TLS-terminating proxies; explicitly unsafe network overrides remain opt-in. Incomplete TLS settings and missing MCP authentication middleware are refused.
- Encryption metadata cannot request more than four Argon2 operations or 256 MiB, and invalid parameter types and salts are rejected before native key derivation.
- CI and private releases use a committed universal dependency lock. Actions are pinned to commit SHAs; semantic searches no longer install packages implicitly. Provider API credentials are sent in non-forwarded headers, and Gemini keys no longer appear in request URLs.
- The landing-page deployment is bound to its verified Vercel project, uploads only public site files, requires green CI for the exact commit, and verifies deployed HTML and security headers. Production credentials are never exposed to pull-request builds.
- Remote backup listing, download, and pruning reject unsafe archive filenames from manifests and rclone listings. Pruning validates the entire plan before deleting any file, preventing traversal outside the configured backup folder.
