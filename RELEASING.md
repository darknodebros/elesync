# Releasing EleSync

EleSync is developed and distributed as proprietary software.

Historical versions previously released under the MIT License remain governed by the licenses attached to those specific releases.

## Current release flow

1. Merge approved changes to `master`.
2. Confirm CI is green for the exact release commit.
3. Update the version and `CHANGELOG.md`.
4. Create and push the version tag.
5. `.github/workflows/release.yml` creates a draft release.
6. Release artifacts are built and attached before the GitHub draft is published.
7. Publishing makes the release visible to everyone who can access the repository.

## Public publishing

The current public repository is `darknodebros/elesync`. Release tags publish
Windows and Linux downloads, a Python wheel and sdist, then PyPI, then the MCP
Registry listing `io.github.darknodebros/elesync`. The license remains proprietary.
GitHub Pages hosts the website; Vercel is no longer the deployment target.

One-time service configuration:

- Enable Settings → Pages → Source: GitHub Actions in this repository. Set the custom domain to `elesync.dev` and enforce HTTPS after domain validation.
- Point the domain's DNS at GitHub Pages, removing the old Vercel destination. Confirm ownership before switching live DNS. The default project address is `https://darknodebros.github.io/elesync/`.
- In the existing PyPI `elesync` project, configure a Trusted Publisher with owner `darknodebros`, repository `elesync`, workflow `release.yml`, environment `pypi`. The old repository's trust configuration does not cover this repository.
- MCP publishing uses GitHub OIDC for the organization namespace; no registry token is embedded in the repository. The package must be visible on PyPI before the registry accepts its README ownership marker.

The MCP entry starts a local stdio server. GitHub Pages serves only the static
website; remote MCP connections still run on the user's computer or server.
Never put vault files, provider keys, or runtime configuration in the Pages artifact.

## Release safety

CI and release builds install from `uv.lock` with uv 0.12.23 and `--locked`.
After intentionally changing Python dependencies, run `uv lock`, review the
version and hash changes, and run the dependency audit and tests before merging.
Do not regenerate dependency resolutions during a release. The Windows release
uses the `release` dependency group; CI uses `ci` and the development extra.

GitHub immutable releases require all assets to be attached to the draft before publication.

If a published release is incorrect, create a new patch version rather than attempting to reuse the existing tag.

GitHub releases in `darknodebros/elesync` intentionally distribute Windows and Linux binaries publicly. PyPI and MCP Registry publishing run after GitHub release publication. Linux builds target x86-64 and glibc 2.35+ on Ubuntu 22.04. Both platform builds must pass before publication; platform checksum files are combined before the immutable release is published.
