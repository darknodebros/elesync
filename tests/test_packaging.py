"""Packaging invariants. Every check in this file guards something that has
already silently broken once in this project's history.

These run in the base CI job: stdlib only, Python 3.11 compatible
(no tomllib dependency - pyproject is parsed with a regex).
"""

from __future__ import annotations

import importlib.util
import json
import re
import tempfile
import unittest
from pathlib import Path
from unittest import mock

ROOT = Path(__file__).resolve().parent.parent


def _load_entry_web():
    """Load scripts/entry_web.py directly so the test isn't shadowed by a
    third-party top-level ``scripts`` package (e.g. pywin32)."""
    path = ROOT / "scripts" / "entry_web.py"
    spec = importlib.util.spec_from_file_location("_test_packaging_scripts_entry_web", path)
    assert spec and spec.loader, f"could not load {path}"
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


class TestGuiEntryPoint(unittest.TestCase):
    def test_explicit_cli_arguments_do_not_start_web_server(self):
        entry_web = _load_entry_web()

        with (
            mock.patch.object(entry_web.sys, "argv", ["EleSync.exe", "--version"]),
            mock.patch.object(entry_web.elesync.cli, "main", return_value=0) as cli_main,
        ):
            self.assertEqual(entry_web.main(), 0)
        cli_main.assert_called_once_with()

    def test_no_arguments_start_the_normal_web_command(self):
        entry_web = _load_entry_web()

        with (
            mock.patch.object(entry_web.sys, "argv", ["EleSync.exe"]),
            mock.patch.object(entry_web.elesync.cli, "main", return_value=0) as cli_main,
        ):
            self.assertEqual(entry_web.main(), 0)
            self.assertEqual(entry_web.sys.argv, ["ele", "web"])
        cli_main.assert_called_once_with()

    def test_portable_marker_uses_relative_vault_after_drive_move(self):
        entry_web = _load_entry_web()
        with tempfile.TemporaryDirectory() as root:
            app = Path(root) / "F" / "EleSync"
            vault = Path(root) / "F" / "EleSyncVault"
            app.mkdir(parents=True)
            vault.mkdir()
            (app / "portable.json").write_text('{"vault_dir":"../EleSyncVault"}')
            with mock.patch.object(entry_web.sys, "executable", str(app / "EleSync.exe")):
                self.assertEqual(entry_web._portable_vault(), vault.resolve())


def _pyproject_version() -> str:
    text = (ROOT / "pyproject.toml").read_text(encoding="utf-8")
    m = re.search(r'^version\s*=\s*"([^"]+)"', text, re.MULTILINE)
    assert m, "version not found in pyproject.toml"
    return m.group(1)


class TestVersionSurfacesAgree(unittest.TestCase):
    def test_readme_version_matches_pyproject(self):
        text = (ROOT / "README.md").read_text(encoding="utf-8")
        m = re.search(r"EleSync is at \*\*([^*]+)\*\*", text)
        self.assertIsNotNone(m, "README status version not found")
        self.assertEqual(m.group(1), _pyproject_version())

    def test_init_version_matches_pyproject(self):
        text = (ROOT / "elesync" / "__init__.py").read_text(encoding="utf-8")
        m = re.search(r'__version__\s*=\s*["\']([^"\']+)', text)
        self.assertIsNotNone(m)
        self.assertEqual(m.group(1), _pyproject_version())


class TestStaticAssetsShipped(unittest.TestCase):
    """The dashboard and login JS/CSS live in ``elesync/static/`` and are served
    at runtime from the installed package. If they are not declared as
    package-data, the wheel ships an app whose every page 404s for its own
    script and stylesheet — the same 'green build, broken artifact' failure that
    shipped assetless releases from v1.7.4 to v1.10.0."""

    EXPECTED = ("dashboard.js", "dashboard.css", "login.js", "login.css")

    def test_static_files_exist(self):
        static = ROOT / "elesync" / "static"
        for name in self.EXPECTED:
            self.assertTrue((static / name).is_file(), f"missing static asset: {name}")

    def test_pyproject_declares_static_as_package_data(self):
        text = (ROOT / "pyproject.toml").read_text(encoding="utf-8")
        self.assertIn(
            "[tool.setuptools.package-data]",
            text,
            "static assets must be declared as package-data or they won't ship",
        )
        block = text.split("[tool.setuptools.package-data]", 1)[1]
        # Stop at the next table header so we only inspect this section.
        block = re.split(r"\n\[", block, maxsplit=1)[0]
        self.assertIn("static/*.js", block, "package-data must include static/*.js")
        self.assertIn("static/*.css", block, "package-data must include static/*.css")


class TestRootAllowlistsAgree(unittest.TestCase):
    """The repo root is guarded in two places and they must not drift.

    ci.yml's "No stray root-level files" step fails the build when an
    unexpected entry appears at the root. .gitignore's root default-deny
    stops such an entry reaching a commit at all. The first is the backstop,
    the second is the fence -- but a root entry added to only one of them
    either breaks CI for every later commit (added to neither) or becomes
    silently uncommittable (added to ci.yml alone). Assert they agree.
    """

    def _ci_allowlist(self) -> set[str]:
        text = (ROOT / ".github" / "workflows" / "ci.yml").read_text(encoding="utf-8")
        match = re.search(r"allowed=\$\(cat << 'EOF'\n(.*?)\n\s*EOF", text, re.DOTALL)
        self.assertIsNotNone(match, "could not locate the root allowlist heredoc in ci.yml")
        assert match is not None  # for mypy
        return {line.strip() for line in match.group(1).splitlines() if line.strip()}

    def _gitignore_allowlist(self) -> set[str]:
        text = (ROOT / ".gitignore").read_text(encoding="utf-8")
        self.assertIn("\n/*\n", text, ".gitignore is missing the root default-deny marker `/*`")
        tail = text.split("\n/*\n", 1)[1]
        return {line[2:].rstrip("/") for line in tail.splitlines() if line.startswith("!/")}

    def test_ci_and_gitignore_root_allowlists_match(self):
        ci = self._ci_allowlist()
        ignore = self._gitignore_allowlist()
        self.assertEqual(
            ci,
            ignore,
            "root allowlists disagree -- in ci.yml only: "
            f"{sorted(ci - ignore)}; in .gitignore only: {sorted(ignore - ci)}",
        )

    def test_allowlist_matches_the_actual_tree(self):
        """Every allowlisted entry should exist; a stale name hides a real stray."""
        missing = sorted(n for n in self._ci_allowlist() if not (ROOT / n).exists())
        self.assertEqual(missing, [], f"allowlisted root entries that no longer exist: {missing}")


class TestPythonVersionMetadata(unittest.TestCase):
    """Package metadata must not understate what is actually supported.

    The classifiers stopped at 3.13 while CI had already been testing 3.14 for some time,
    so the project advertised less support than it shipped. Tie the two lists
    together so the classifier list can't silently fall behind the matrix.
    """

    def _ci_matrix(self) -> set[str]:
        text = (ROOT / ".github" / "workflows" / "ci.yml").read_text(encoding="utf-8")
        match = re.search(r"python-version:\s*\[([^\]]+)\]", text)
        self.assertIsNotNone(match, "could not find the python-version matrix in ci.yml")
        assert match is not None  # for mypy
        return set(re.findall(r"\d+\.\d+", match.group(1)))

    def _classifier_versions(self) -> set[str]:
        text = (ROOT / "pyproject.toml").read_text(encoding="utf-8")
        return set(re.findall(r"Programming Language :: Python :: (\d+\.\d+)", text))

    def test_classifiers_cover_every_tested_python(self):
        matrix = self._ci_matrix()
        classifiers = self._classifier_versions()
        self.assertEqual(
            matrix,
            classifiers,
            "python support metadata is out of sync -- tested but unadvertised: "
            f"{sorted(matrix - classifiers)}; advertised but untested: "
            f"{sorted(classifiers - matrix)}",
        )

    def test_requires_python_matches_the_lowest_tested_version(self):
        text = (ROOT / "pyproject.toml").read_text(encoding="utf-8")
        match = re.search(r'requires-python\s*=\s*">=(\d+\.\d+)"', text)
        self.assertIsNotNone(match, "requires-python must be a >=X.Y floor")
        assert match is not None  # for mypy
        lowest = min(self._ci_matrix(), key=lambda v: tuple(map(int, v.split("."))))
        self.assertEqual(
            match.group(1),
            lowest,
            "requires-python floor disagrees with the lowest Python CI tests",
        )


class TestReadmeStatusCounts(unittest.TestCase):
    """The README's Status line advertises a test count. Keep it honest.

    The count is the number of tests *collected* by unittest discovery, not
    the runner's "Ran N" line: in an environment missing a dependency,
    some tests are filtered out of the run, so the ran-count varies by
    environment while the collected count does not.
    """

    @staticmethod
    def _count(suite: unittest.TestSuite) -> int:
        n = 0
        for item in suite:
            if isinstance(item, unittest.TestSuite):
                n += TestReadmeStatusCounts._count(item)
            else:
                n += 1
        return n

    def test_readme_test_count_matches_collected_suite(self):
        readme = (ROOT / "README.md").read_text(encoding="utf-8")
        matches = re.findall(r"\((\d+) tests", readme)
        self.assertEqual(
            len(matches),
            1,
            "README should state the test count exactly once (the Status line); "
            f"found {len(matches)} occurrences",
        )
        advertised = int(matches[0])

        # Mirror however THIS run imported the test modules, so discovery
        # reuses the already-imported modules instead of importing every
        # test module a second time under a different name.
        if __name__.startswith("tests."):
            top_level = ROOT
        else:
            top_level = ROOT / "tests"
        suite = unittest.TestLoader().discover(
            start_dir=str(ROOT / "tests"), top_level_dir=str(top_level)
        )
        collected = self._count(suite)

        self.assertEqual(
            advertised,
            collected,
            f"README Status line says {advertised} tests but discovery collects "
            f"{collected}. Update the README (RELEASING.md: version surfaces).",
        )


class TestLandingPagePrivacy(unittest.TestCase):
    """The public site is deliberately a description-only holding page."""

    @classmethod
    def setUpClass(cls):
        cls.html = (ROOT / "index.html").read_text(encoding="utf-8")

    def test_page_and_deployments_expose_no_external_content(self):
        self.assertNotRegex(self.html, r"\b(?:href|src)=")
        self.assertNotIn("<script", self.html.lower())
        vercel = json.loads((ROOT / "vercel.json").read_text(encoding="utf-8"))
        self.assertEqual(vercel["outputDirectory"], "_site")
        self.assertIn("cp index.html CNAME _site/", vercel["buildCommand"])
        pages = (ROOT / ".github" / "workflows" / "pages.yml").read_text(encoding="utf-8")
        self.assertIn("cp index.html _site/", pages)
        self.assertIn("cp CNAME _site/", pages)

    def test_page_has_no_setup_or_distribution_guidance(self):
        forbidden = re.compile(
            r"\b(?:install|download|setup|quickstart|getting started|pipx?|pypi|"
            r"scoop|homebrew|winget|docker|npm|npx|curl|wget|powershell|bash|"
            r"terminal|command line|mcp|registry|github)\b",
            re.IGNORECASE,
        )
        self.assertEqual(
            sorted({match.group(0).lower() for match in forbidden.finditer(self.html)}),
            [],
        )

    def test_page_keeps_only_product_description(self):
        self.assertIn("private AI workspace", self.html)
        self.assertIn("local-first product", self.html)
        self.assertEqual(self.html.count("<main>"), 1)


class TestPyInstallerSpec(unittest.TestCase):
    """Guards on the build spec, which CI only exercises during a release.

    A broken spec doesn't fail here or on a push — it fails inside the release
    pipeline after a tag has been pushed, and a burned tag can't be reused
    because immutable releases are on. So the invariants that are cheap to
    assert get asserted here instead.
    """

    @classmethod
    def setUpClass(cls):
        cls.spec = (ROOT / "scripts" / "elesync.spec").read_text(encoding="utf-8")

    def test_spec_is_valid_python(self):
        import ast

        ast.parse(self.spec)

    def test_windows_is_a_onedir_build(self):
        self.assertIn('_ONEDIR = sys.platform == "win32"', self.spec)
        self.assertIn("COLLECT(", self.spec)

    def test_exclude_binaries_is_tied_to_onedir(self):
        """The flag that actually makes onedir work.

        Dropping a.binaries/a.datas from EXE is not sufficient: without
        exclude_binaries, EXE still emits a onefile executable at dist/ele and
        COLLECT then dies creating a directory at the same path. That failure
        only appears during a real build, which is why it's pinned here.
        """
        self.assertIn("exclude_binaries=_ONEDIR", self.spec)

    def test_dashboard_static_assets_are_bundled(self):
        self.assertIn(
            'STATIC_DATAS = [(str(ROOT / "elesync" / "static"), "elesync/static")]',
            self.spec,
        )
        self.assertEqual(
            self.spec.count("datas=STATIC_DATAS"),
            2,
            "both the standalone CLI and GUI must bundle dashboard CSS/JS",
        )

    def test_upx_stays_disabled(self):
        # UPX packing is a top AV heuristic trigger — the thing this build
        # shape exists to avoid. Re-enabling it would undo the point.
        self.assertIn("upx=False", self.spec)
        self.assertNotIn("upx=True", self.spec)


class TestLicenseMetadata(unittest.TestCase):
    """EleSync went proprietary, but pyproject still advertised an OSI/MIT
    classifier and the Windows .exe version info still claimed MIT. Package
    metadata must agree with LICENSE."""

    def test_pyproject_classifiers_match_proprietary_license(self):
        text = (ROOT / "pyproject.toml").read_text(encoding="utf-8")
        self.assertRegex(text, r'license\s*=\s*\{\s*text\s*=\s*"Proprietary"\s*\}')
        license_classifiers = re.findall(r'"(License :: [^"]+)"', text)
        self.assertEqual(license_classifiers, ["License :: Other/Proprietary License"])

    def test_windows_exe_copyright_matches_license(self):
        spec = (ROOT / "scripts" / "elesync.spec").read_text(encoding="utf-8")
        match = re.search(r'"LegalCopyright",\s*"([^"]+)"', spec)
        self.assertIsNotNone(match, "LegalCopyright not found in scripts/elesync.spec")
        assert match is not None  # for mypy
        self.assertNotIn("MIT", match.group(1))
        self.assertIn(match.group(1), (ROOT / "LICENSE").read_text(encoding="utf-8"))


class TestTrackedTextEncoding(unittest.TestCase):
    """UTF-8 text re-saved through a legacy Windows code page turned em dashes
    into mojibake in pyproject.toml (shipped in the package summary), ci.yml
    and .gitignore, and left byte-order marks on LICENSE and SECURITY.md.
    Check every tracked text file so it cannot creep back in."""

    # UTF-8 bytes decoded as cp1252/latin-1: lead bytes 0xC2/0xC3 followed by
    # a continuation byte, the 0xE2 0x80 punctuation prefix, or C1 controls.
    MOJIBAKE = re.compile("[\u00c2\u00c3][\u0080-\u00bf]|\u00e2\u20ac|[\u0080-\u009f]")
    # Fixtures that reproduce third-party exports byte for byte.
    EXEMPT_PREFIXES = ("tests/sample_data/",)

    @classmethod
    def setUpClass(cls):
        import subprocess

        try:
            out = subprocess.run(
                ["git", "ls-files", "-z"],
                cwd=ROOT,
                capture_output=True,
                check=True,
            ).stdout
        except (OSError, subprocess.CalledProcessError):
            raise unittest.SkipTest("not a git checkout") from None
        cls.files: dict[str, str] = {}
        for name in out.decode("utf-8").split("\0"):
            if not name or name.startswith(cls.EXEMPT_PREFIXES):
                continue
            path = ROOT / name
            if not path.is_file():
                continue
            data = path.read_bytes()
            if b"\0" in data:
                continue
            try:
                cls.files[name] = data.decode("utf-8")
            except UnicodeDecodeError:
                continue
        if not cls.files:
            raise unittest.SkipTest("no tracked text files found")

    def test_no_byte_order_marks(self):
        offenders = sorted(name for name, text in self.files.items() if text.startswith("\ufeff"))
        self.assertEqual(offenders, [])

    def test_no_mojibake(self):
        offenders = []
        for name, text in self.files.items():
            for number, line in enumerate(text.splitlines(), 1):
                if self.MOJIBAKE.search(line):
                    offenders.append(f"{name}:{number}: {line.strip()[:80]}")
        self.assertEqual(offenders, [])
