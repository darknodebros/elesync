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

Current proprietary releases must not automatically publish to:

- PyPI
- the public MCP Registry
- Scoop
- Glama or other public package/server registries
- other public source or package distribution channels

Any future public distribution mechanism requires an explicit commercial distribution decision.

## Release safety

CI and release builds install from `uv.lock` with uv 0.12.23 and `--locked`.
After intentionally changing Python dependencies, run `uv lock`, review the
version and hash changes, and run the dependency audit and tests before merging.
Do not regenerate dependency resolutions during a release. The Windows release
uses the `release` dependency group; CI uses `ci` and the development extra.

GitHub immutable releases require all assets to be attached to the draft before publication.

If a published release is incorrect, create a new patch version rather than attempting to reuse the existing tag.

The release workflow must remain limited to private distribution unless the commercial release architecture is intentionally changed.
