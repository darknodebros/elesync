"""Guards the private release pipeline and the site's no-download boundary.

Background — the failure this file exists to prevent:

The release workflow still produces private artifacts, but elesync.dev must not
link to them or function as a public distribution channel. Upload failures must
also remain loud so private releases cannot silently lose their artifacts.

These tests are pure static checks on the repo: no network, no CI required.
"""

from __future__ import annotations

import re
import unittest
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
INDEX = ROOT / "index.html"
WORKFLOW = ROOT / ".github" / "workflows" / "release.yml"

# The archived action that silently dropped every asset upload.
ARCHIVED_UPLOAD_ACTION = "actions/upload-release-asset"


class TestReleaseAssets(unittest.TestCase):
    def setUp(self) -> None:
        if not INDEX.exists() or not WORKFLOW.exists():
            self.skipTest("index.html or release.yml not present")
        self.html = INDEX.read_text(encoding="utf-8")
        self.wf = WORKFLOW.read_text(encoding="utf-8")

    def _linked_assets(self) -> set[str]:
        return set(re.findall(r"releases/latest/download/([^\"'\s]+)", self.html))

    def _uploaded_assets(self) -> set[str]:
        return set(
            re.findall(r"(?:asset_name|cli_asset|gui_asset|installer_asset):\s*(\S+)", self.wf)
        ) - {
            '""',
            "''",
        }

    def test_site_links_no_release_downloads(self) -> None:
        self.assertFalse(
            self._linked_assets(),
            "index.html must not link to release artifacts",
        )

    def test_site_contains_no_distribution_urls(self) -> None:
        self.assertNotRegex(
            self.html,
            r"(?i)(?:github\.com|pypi\.org|registry\.modelcontextprotocol\.io|/releases/)",
        )

    def test_private_release_verifies_every_uploaded_asset(self) -> None:
        m = re.search(r"for a in (.+); do", self.wf)
        self.assertIsNotNone(m, "release workflow lost its required-asset loop")
        asserted = set(m.group(1).split())
        self.assertEqual(
            self._uploaded_assets() - asserted,
            set(),
            "release workflow does not verify every uploaded private artifact",
        )

    def _used_actions(self) -> set[str]:
        """Actions actually invoked via `uses:` — not ones merely named in comments.

        Scoping to `uses:` matters: the workflow's own comments explain *why*
        the archived action was removed, and a naive substring scan of the file
        would flag that explanation as the bug it warns about.
        """
        return {
            m.group(1).split("@")[0]
            for m in re.finditer(r"^\s*(?:-\s*)?uses:\s*(\S+)", self.wf, re.M)
        }

    def test_upload_step_does_not_use_the_archived_action(self) -> None:
        self.assertNotIn(
            ARCHIVED_UPLOAD_ACTION,
            self._used_actions(),
            f"{ARCHIVED_UPLOAD_ACTION} is archived and cannot upload to immutable "
            f"releases — it is why no release ever carried a binary",
        )

    def test_asset_upload_failure_is_loud(self) -> None:
        """`continue-on-error` on the upload step is what made this invisible.

        Keyed off the upload *command*, not the step's name — a step label is
        cosmetic and renaming it must not silently disable this guard.
        """
        steps = re.split(r"\n      - name:", self.wf)
        upload_steps = [s for s in steps if "gh release upload" in s]
        self.assertTrue(upload_steps, "no step runs `gh release upload`")
        for step in upload_steps:
            self.assertNotIn(
                "continue-on-error",
                step,
                "a failed asset upload must fail the job",
            )

    def test_windows_smoke_test_requires_dashboard_static_assets(self) -> None:
        for bundle in ("ele-windows", "EleSync-windows"):
            expected = f"dist/{bundle}/_internal/elesync/static/dashboard.css"
            self.assertIn(
                expected,
                self.wf,
                f"release smoke test does not verify static assets in {bundle}",
            )

    def test_windows_installer_marks_first_run_setup(self) -> None:
        iss = (ROOT / "packaging" / "windows" / "EleSync.iss").read_text(encoding="utf-8")
        self.assertIn('"first_run_setup": true', iss)
        self.assertIn('"vault_dir": "', iss)

    def test_windows_installer_keeps_existing_config_on_upgrade(self) -> None:
        """Re-running the installer over an install must leave config.json alone.

        Rewriting it re-armed the first-run wizard, reset a custom vault_dir to
        the Documents default (the app then opened an empty vault) and dropped
        every other key, e.g. the update_check cache.
        """
        iss = (ROOT / "packaging" / "windows" / "EleSync.iss").read_text(encoding="utf-8")
        code = iss.split("[Code]", 1)[1]
        # Decided once, before any page runs: the fresh-install write below must
        # not flip the decision if the user goes Back to the vault page.
        init = re.search(r"procedure InitializeWizard;.*?\nend;", code, re.S)
        self.assertIsNotNone(init)
        self.assertRegex(init.group(0), r"KeepExistingConfig := FileExists\(ConfigFilePath\)")
        # Upgrades skip the vault page, including /SILENT and /VERYSILENT runs.
        skip = re.search(
            r"function ShouldSkipPage\(PageID: Integer\): Boolean;.*?\nend;", code, re.S
        )
        self.assertIsNotNone(skip, "no ShouldSkipPage for the vault page")
        self.assertIn("KeepExistingConfig", skip.group(0))
        self.assertIn("VaultPage.ID", skip.group(0))
        # And the only config write bails out first if it is ever reached.
        click = re.search(
            r"function NextButtonClick\(CurPageID: Integer\): Boolean;.*?\nend;", code, re.S
        )
        self.assertIsNotNone(click)
        body = click.group(0)
        self.assertEqual(code.count("SaveStringToFile("), 1)
        self.assertIn("SaveStringToFile(ConfigFilePath", body)
        guard = body.find("if KeepExistingConfig then")
        self.assertNotEqual(guard, -1, "NextButtonClick does not check for an existing config")
        self.assertLess(guard, body.find("SaveStringToFile("))

    def test_stage_assets_bash_script_does_not_use_powershell_invocation(self) -> None:
        """The staging step runs under Bash on every OS matrix entry.

        Bash parses the whole script before evaluating target-specific branches,
        so a PowerShell-only invocation breaks Linux, macOS, and Windows builds.
        """
        start = self.wf.find("- name: Stage assets and smoke-test the binaries")
        self.assertNotEqual(start, -1, "release workflow lost the Bash staging step")
        end = self.wf.find("\n      - name:", start + 1)
        self.assertNotEqual(end, -1, "release workflow lost the step after staging")
        body = self.wf[start:end]
        self.assertIn("shell: bash", body)
        self.assertNotRegex(
            body,
            r"^\s*&\s+",
            "Bash staging step must not contain PowerShell call operator '&'",
        )
        self.assertNotIn("ISCC.exe", body)
        self.assertIn("- name: Build Windows installer", self.wf)
        self.assertRegex(
            self.wf,
            r"- name: Build Windows installer\n"
            r"\s+if: matrix\.target == 'windows'\n"
            r"\s+shell: pwsh",
        )
        self.assertIn(r"C:\Program Files (x86)\Inno Setup 6\ISCC.exe", self.wf)

    def test_release_triggers_on_tag_push_not_on_published_release(self) -> None:
        """Immutable releases force a draft-first flow, so the trigger must be a tag.

        Assets are frozen the instant a release is published, so the workflow has
        to create the draft, attach assets, and publish it *itself*. If it also
        fired on `release: published`, publishing the draft would re-trigger it.
        """
        self.assertRegex(
            self.wf,
            r"on:\s*\n\s*push:\s*\n\s*tags:",
            "release.yml must fire on a v* tag push",
        )
        self.assertNotRegex(
            self.wf,
            r"^\s*release:\s*\n\s*types:\s*\[\s*published\s*\]",
            "must NOT trigger on release:published — the workflow publishes the "
            "draft itself, which would re-fire the workflow",
        )

    def test_release_is_created_as_a_draft_before_assets_are_attached(self) -> None:
        """The whole bug: you cannot attach assets to a published immutable release.

        GitHub's prescribed flow is draft -> attach -> publish. If the draft step
        ever disappears, every binary silently stops shipping again.
        """
        # Join shell line-continuations first: `gh release create` wraps onto a
        # second line with a trailing backslash, so a line-bounded regex would
        # only ever see the first half and miss --draft entirely.
        joined = re.sub(r"\\\n\s*", " ", self.wf)
        create = re.search(r"gh release create[^\n]*", joined)
        self.assertIsNotNone(create, "no `gh release create` step")
        # NB: bind the check to `gh release create` itself. A bare
        # assertIn("--draft", self.wf) is vacuous — `--draft=false` in the
        # publish step contains that substring and satisfies it even when the
        # draft creation has been deleted entirely.
        self.assertIn(
            "--draft",
            create.group(0),
            "the release must be CREATED as a draft — assets cannot be attached "
            "to a published immutable release",
        )
        self.assertIn(
            "--draft=false",
            self.wf,
            "the draft must be explicitly published once assets are attached",
        )
        create_at = self.wf.index("gh release create")
        upload_at = self.wf.index("gh release upload")
        publish_at = self.wf.index("--draft=false")
        self.assertLess(create_at, upload_at, "draft must be created before upload")
        self.assertLess(upload_at, publish_at, "assets must be attached before publish")


class TestChangelogFeedsTheRelease(unittest.TestCase):
    """The release workflow extracts its notes from CHANGELOG.md and aborts if the
    section for the current version is missing. (Version drift across
    pyproject/__init__ is already covered by
    tests/test_packaging.py::TestVersionSurfacesAgree — not duplicated here.)"""

    def test_changelog_documents_the_current_version(self) -> None:
        py = (ROOT / "pyproject.toml").read_text(encoding="utf-8")
        v = re.search(r'^version\s*=\s*"([^"]+)"', py, re.M).group(1)
        changelog = (ROOT / "CHANGELOG.md").read_text(encoding="utf-8")
        self.assertIn(
            f"## [{v}]",
            changelog,
            f"CHANGELOG.md has no section for {v} — the release workflow extracts "
            f"its notes from there and will refuse to cut the release",
        )


if __name__ == "__main__":
    unittest.main()
