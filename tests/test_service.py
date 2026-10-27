"""Tests for the service layer (what the MCP tools expose) and onboarding/doctor.

These cover the exact behaviour an AI client sees over MCP, plus the one-command
setup — short of the live protocol handshake, which only a running client confirms.
"""

import json
import re
import tempfile
import unittest
from pathlib import Path
from unittest import mock

from elesync import onboarding
from elesync.service import MemoryService
from elesync.store import MemoryStore


class TestService(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.svc = MemoryService(MemoryStore(self.tmp.name))

    def tearDown(self):
        self.svc.store.close()
        self.tmp.cleanup()

    def test_remember_then_recall(self):
        self.svc.remember("User is based in Westbrook", type="fact", source="claude")
        out = self.svc.recall("Westbrook")
        self.assertIn("Westbrook", out)
        self.assertIn("[fact/claude]", out)

    def test_remember_empty_is_rejected(self):
        self.assertIn("empty", self.svc.remember("   ").lower())
        self.assertEqual(self.svc.store.count(), 0)

    def test_recall_no_match(self):
        self.assertEqual(self.svc.recall("nonexistent"), "No matching memories.")

    def test_recall_result_is_structured_and_legacy_text_is_preserved(self):
        self.svc.remember("User is based in Westbrook", type="fact", source="claude")
        result = self.svc.recall_result("Westbrook")
        self.assertEqual(len(result.memories), 1)
        self.assertEqual(result.memories[0].content, "User is based in Westbrook")
        self.assertEqual(self.svc.recall("Westbrook").splitlines()[1], "User is based in Westbrook")

    def test_remember_dedup_via_service(self):
        self.svc.remember("same fact", type="fact", source="chatgpt")
        self.svc.remember("Same   Fact", type="fact", source="chatgpt")
        self.assertEqual(self.svc.store.count(), 1)

    def test_forget_by_prefix(self):
        self.svc.remember("delete me please", type="note")
        mem = self.svc.store.all()[0]
        self.assertEqual(self.svc.forget(mem.id[:8]), "Deleted.")
        self.assertEqual(self.svc.store.count(), 0)

    def test_forget_ambiguous_or_missing(self):
        self.assertIn("No id", self.svc.forget(""))
        self.assertIn("No such memory", self.svc.forget("deadbeef" * 4))

    def test_status_and_recent(self):
        self.svc.remember("a fact", type="fact", source="gemini")
        self.assertIn("gemini:1", self.svc.status())
        self.assertIn("a fact", self.svc.recent())


class TestSupersedeProposal(unittest.TestCase):
    """Direction-C follow-up: propose supersessions on the review surface, accept
    them with an explicit `supersede` verb, and hint (without writing) on save when
    auto-supersession deliberately stayed its hand."""

    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.svc = MemoryService(MemoryStore(self.tmp.name))

    def tearDown(self):
        self.svc.store.close()
        self.tmp.cleanup()

    @staticmethod
    def _saved_id(msg: str) -> str:
        m = re.search(r"id=([0-9a-f]{8})", msg)
        assert m, f"no id in {msg!r}"
        return m.group(1)

    def _live_ids(self):
        return {i.id[:8] for i in self.svc.store.all()}

    def _all_ids(self):
        return {i.id[:8] for i in self.svc.store.all(include_superseded=True)}

    # --- the `supersede` verb (accept step) ---

    def test_supersede_verb_is_recoverable(self):
        # Two untagged conflicting facts: auto-supersession won't act (no tag), so
        # both stay live until the user accepts a supersession explicitly.
        a = self._saved_id(self.svc.remember("Based in Westbrook", type="fact"))
        b = self._saved_id(self.svc.remember("Based in Portland", type="fact"))
        self.assertEqual(self._live_ids(), {a, b})  # nothing auto-tombstoned
        msg = self.svc.supersede(a, b)
        self.assertTrue(msg.startswith("Superseded"), msg)
        self.assertEqual(self._live_ids(), {b})  # old hidden from live view
        self.assertEqual(self._all_ids(), {a, b})  # but kept as history
        self.assertNotIn("Westbrook", self.svc.recall("Westbrook"))

    def test_supersede_verb_refuses_self(self):
        a = self._saved_id(self.svc.remember("Based in Westbrook", type="fact"))
        msg = self.svc.supersede(a, a)
        self.assertIn("can't supersede itself", msg)
        self.assertEqual(self._live_ids(), {a})

    def test_supersede_verb_unknown_ref_is_noop(self):
        a = self._saved_id(self.svc.remember("Based in Westbrook", type="fact"))
        msg = self.svc.supersede(a, "deadbeef")
        self.assertIn("couldn't supersede", msg)
        self.assertEqual(self._live_ids(), {a})  # no write on a bad reference

    def test_supersede_verb_readonly_blocked(self):
        a = self._saved_id(self.svc.remember("Based in Westbrook", type="fact"))
        b = self._saved_id(self.svc.remember("Based in Portland", type="fact"))
        ro = MemoryService(self.svc.store, readonly=True)
        msg = ro.supersede(a, b)
        self.assertIn("read-only", msg)
        self.assertEqual(self._live_ids(), {a, b})

    # --- the proposal surface ---

    def test_conflicts_list_proposes_supersede_for_isolated_pair(self):
        a = self._saved_id(self.svc.remember("Based in Westbrook", type="fact"))
        b = self._saved_id(self.svc.remember("Based in Portland", type="fact"))
        out = self.svc.conflicts_list()
        self.assertIn("ele supersede", out)
        self.assertIn(a, out)
        self.assertIn(b, out)
        # both directions offered — no default winner
        self.assertIn(f"ele supersede {a} --by {b}", out)
        self.assertIn(f"ele supersede {b} --by {a}", out)

    def test_conflicts_list_no_proposal_for_larger_cluster(self):
        # Three mutually-similar facts collapse into one 3-member cluster — no clean
        # pair, so the choose-one resolve flow stands and no supersede is proposed.
        self.svc.remember("Based in Westbrook", type="fact")
        self.svc.remember("Based in Portland", type="fact")
        self.svc.remember("Based in Boston", type="fact")
        out = self.svc.conflicts_list()
        self.assertNotIn("ele supersede", out)
        self.assertIn("resolve-conflict", out)

    # --- the non-destructive remember() hint ---

    def test_remember_hints_on_untagged_pair_without_writing(self):
        first = self._saved_id(self.svc.remember("Based in Westbrook", type="fact"))
        res = self.svc.remember("Based in Portland", type="fact")
        self.assertIn("ele supersede", res)  # hint offered
        self.assertIn(first[:8], res)  # names the candidate it may update
        # crucially: the hint writes nothing — both facts remain live
        self.assertEqual(len(self.svc.store.all()), 2)

    def test_no_hint_when_auto_supersession_already_acted(self):
        # A tagged pair at the stricter 0.6 bar: auto-supersession acts on save, so
        # there's nothing left to hint about.
        self.svc.remember("Favorite color is blue", type="fact", tags="color")
        res = self.svc.remember("Favorite color is green", type="fact", tags="color")
        self.assertIn("Auto-superseded", res)
        self.assertNotIn("ele supersede", res)  # no redundant hint
        self.assertEqual(len(self.svc.store.all()), 1)  # old one superseded


class TestOnboarding(unittest.TestCase):
    def test_microsoft_store_claude_config_is_detected_and_preserved(self):
        with tempfile.TemporaryDirectory() as d:
            root = Path(d)
            roaming = root / "Roaming"
            local = root / "Local"
            store_cfg = (
                local
                / "Packages"
                / "Claude_pzs8sxrjxfjjc"
                / "LocalCache"
                / "Roaming"
                / "Claude"
                / "claude_desktop_config.json"
            )
            store_cfg.parent.mkdir(parents=True)
            store_cfg.write_text(
                json.dumps(
                    {
                        "preferences": {"theme": "dark"},
                        "mcpServers": {"existing": {"command": "existing"}},
                    }
                ),
                encoding="utf-8",
            )

            with (
                mock.patch.object(onboarding, "_IS_WINDOWS", True),
                mock.patch.dict(
                    onboarding.os.environ,
                    {"APPDATA": str(roaming), "LOCALAPPDATA": str(local)},
                    clear=False,
                ),
            ):
                detected = onboarding.default_config_path_for_target("claude-desktop")
                onboarding.wire_client_config(detected, str(root / "vault"))

            self.assertEqual(detected, store_cfg)
            data = json.loads(store_cfg.read_text(encoding="utf-8"))
            self.assertEqual(data["preferences"], {"theme": "dark"})
            self.assertIn("existing", data["mcpServers"])
            self.assertIn("elesync", data["mcpServers"])

    def test_existing_standard_claude_config_remains_preferred(self):
        with tempfile.TemporaryDirectory() as d:
            root = Path(d)
            roaming = root / "Roaming"
            local = root / "Local"
            standard_cfg = roaming / "Claude" / "claude_desktop_config.json"
            standard_cfg.parent.mkdir(parents=True)
            standard_cfg.write_text("{}", encoding="utf-8")
            store_parent = (
                local / "Packages" / "Claude_pzs8sxrjxfjjc" / "LocalCache" / "Roaming" / "Claude"
            )
            store_parent.mkdir(parents=True)

            with (
                mock.patch.object(onboarding, "_IS_WINDOWS", True),
                mock.patch.dict(
                    onboarding.os.environ,
                    {"APPDATA": str(roaming), "LOCALAPPDATA": str(local)},
                    clear=False,
                ),
            ):
                detected = onboarding.default_config_path_for_target("claude-desktop")

            self.assertEqual(detected, standard_cfg)

    def test_wire_creates_config(self):
        with tempfile.TemporaryDirectory() as d:
            cfg = Path(d) / "claude_desktop_config.json"
            res = onboarding.wire_client_config(cfg, vault_dir=str(Path(d) / "vault"))
            self.assertTrue(cfg.exists())
            self.assertFalse(res["backed_up"])
            data = json.loads(cfg.read_text())
            self.assertIn("elesync", data["mcpServers"])
            self.assertEqual(data["mcpServers"]["elesync"]["args"], ["-m", "elesync.mcp_server"])

    def test_wire_preserves_existing_servers_and_backs_up(self):
        with tempfile.TemporaryDirectory() as d:
            cfg = Path(d) / "config.json"
            cfg.write_text(json.dumps({"mcpServers": {"other": {"command": "x"}}}))
            res = onboarding.wire_client_config(cfg, vault_dir=str(Path(d) / "v"))
            self.assertTrue(res["backed_up"])
            # Backup uses a timestamped name (config.<timestamp>.bak) so repeated
            # calls don't overwrite the original; check that *some* backup exists.
            backups = list(Path(d).glob("config.*.bak"))
            self.assertGreater(len(backups), 0, "expected a timestamped .bak file")
            data = json.loads(cfg.read_text())
            self.assertIn("other", data["mcpServers"])  # preserved
            self.assertIn("elesync", data["mcpServers"])  # added

    def test_wire_is_idempotent(self):
        with tempfile.TemporaryDirectory() as d:
            cfg = Path(d) / "config.json"
            onboarding.wire_client_config(cfg, vault_dir=str(Path(d) / "v"))
            res2 = onboarding.wire_client_config(cfg, vault_dir=str(Path(d) / "v"))
            self.assertTrue(res2["updated_existing"])
            data = json.loads(cfg.read_text())
            self.assertEqual(len(data["mcpServers"]), 1)

    def test_wire_recovers_from_corrupt_config(self):
        with tempfile.TemporaryDirectory() as d:
            cfg = Path(d) / "config.json"
            cfg.write_text("{ this is not json")
            onboarding.wire_client_config(cfg, vault_dir=str(Path(d) / "v"))
            data = json.loads(cfg.read_text())  # now valid
            self.assertIn("elesync", data["mcpServers"])

    def test_doctor_reports_wired_status(self):
        with tempfile.TemporaryDirectory() as d:
            cfg = Path(d) / "config.json"
            vault = str(Path(d) / "vault")
            rows = onboarding.run_doctor(vault, config_path=cfg)
            wired = [r for r in rows if r[1] == "MCP client config wired"][0]
            self.assertEqual(wired[0], "WARN")  # not wired yet
            onboarding.wire_client_config(cfg, vault_dir=vault)
            rows2 = onboarding.run_doctor(vault, config_path=cfg)
            wired2 = [r for r in rows2 if r[1] == "MCP client config wired"][0]
            self.assertEqual(wired2[0], "PASS")

    def test_detect_local_clients_finds_present_app(self):
        """detect_local_clients reports a client when its config parent dir exists.
        We can't control which real apps are installed on the test machine, so
        we monkeypatch default_config_path_for_target to point at a temp dir we
        control — one that exists (app installed) and one that doesn't."""
        import unittest.mock as mock

        with tempfile.TemporaryDirectory() as d:
            installed = Path(d) / "installed_app"
            installed.mkdir()
            not_installed = Path(d) / "missing_app"  # deliberately not created

            def fake_path(target):
                if target == "claude-desktop":
                    return installed / "config.json"
                if target == "cursor":
                    return not_installed / "mcp.json"
                return Path(d) / "other" / "x.json"  # parent missing

            with mock.patch.object(
                onboarding, "default_config_path_for_target", side_effect=fake_path
            ):
                found = onboarding.detect_local_clients()
            targets = {c["target"] for c in found}
            self.assertIn("claude-desktop", targets)
            self.assertNotIn("cursor", targets)

    def test_detect_local_clients_returns_label_and_path(self):
        """Each detected client carries a human-readable label and config path."""
        import unittest.mock as mock

        with tempfile.TemporaryDirectory() as d:
            app_dir = Path(d) / "Claude"
            app_dir.mkdir()

            def fake_path(target):
                return (
                    app_dir / "claude_desktop_config.json"
                    if target == "claude-desktop"
                    else Path(d) / "x" / "y"
                )

            with mock.patch.object(
                onboarding, "default_config_path_for_target", side_effect=fake_path
            ):
                found = onboarding.detect_local_clients()
            self.assertEqual(len(found), 1)
            self.assertEqual(found[0]["target"], "claude-desktop")
            self.assertEqual(found[0]["label"], "Claude Desktop")
            self.assertTrue(found[0]["config_path"].endswith("claude_desktop_config.json"))

    def test_doctor_always_checks_python_and_vault(self):
        with tempfile.TemporaryDirectory() as d:
            rows = onboarding.run_doctor(str(Path(d) / "vault"))
            checks = {r[1] for r in rows}
            self.assertIn("Python >= 3.10", checks)
            self.assertIn("Vault directory writable", checks)

    def test_doctor_checks_mcp_sdk_version(self):
        """The MCP SDK row should report a status and mention the version or
        install hint. The 1.20.3 regression (mcp 2.0.0 broke FastMCP) is exactly
        what this check catches at diagnosis time."""
        with tempfile.TemporaryDirectory() as d:
            rows = onboarding.run_doctor(str(Path(d) / "vault"))
            mcp_rows = [r for r in rows if r[1] == "MCP SDK installed"]
            self.assertEqual(len(mcp_rows), 1)
            status, _, detail = mcp_rows[0]
            # Either PASS (installed, in range, FastMCP imports) or WARN
            # (missing / out of range / FastMCP broken). Never FAIL — `ele serve`
            # is optional, so a missing SDK is a warning, not a hard error.
            self.assertIn(status, ("PASS", "WARN"))
            # Detail should mention the version when installed, or the install
            # hint when not. Either way it should be non-empty and informative.
            self.assertTrue(detail)

    def test_check_mcp_sdk_range_logic(self):
        """The version-range check is the load-bearing part. Verify it classifies
        a few representative versions correctly by mocking the import."""
        # Build a fake `mcp` package with a controllable __version__ and a
        # `mcp.server.fastmcp` submodule exposing FastMCP, so the import probe
        # in _check_mcp_sdk succeeds without the real package.
        import sys
        import types
        from unittest.mock import patch

        def _fake_mcp_pkg(version_str, fastmcp_ok=True):
            pkg = types.ModuleType("mcp")
            pkg.__version__ = version_str
            pkg.__path__ = []  # mark as a package so submodule imports work
            server = types.ModuleType("mcp.server")
            server.__path__ = []
            fastmcp_mod = types.ModuleType("mcp.server.fastmcp")
            if fastmcp_ok:
                fastmcp_mod.FastMCP = object  # the symbol _check_mcp_sdk imports
            # else: leave FastMCP unset → `from mcp.server.fastmcp import FastMCP`
            # raises AttributeError, which _check_mcp_sdk catches as a WARN.
            server.fastmcp = fastmcp_mod
            pkg.server = server
            return {
                "mcp": pkg,
                "mcp.server": server,
                "mcp.server.fastmcp": fastmcp_mod,
            }

        # In-range version, FastMCP imports → PASS.
        with patch.dict(sys.modules, _fake_mcp_pkg("1.5.0")):
            status, detail = onboarding._check_mcp_sdk()
            self.assertEqual(status, "PASS")
            self.assertIn("1.5.0", detail)

        # Out-of-range major (2.x), FastMCP still imports → WARN (version).
        with patch.dict(sys.modules, _fake_mcp_pkg("2.0.0")):
            status, detail = onboarding._check_mcp_sdk()
            self.assertEqual(status, "WARN")
            self.assertIn("2.0.0", detail)
            self.assertIn("outside the supported range", detail)

        # Below the min version → WARN.
        with patch.dict(sys.modules, _fake_mcp_pkg("1.0.0")):
            status, detail = onboarding._check_mcp_sdk()
            self.assertEqual(status, "WARN")
            self.assertIn("1.0.0", detail)

        # In-range version but FastMCP won't import → WARN (import error).
        with patch.dict(sys.modules, _fake_mcp_pkg("1.5.0", fastmcp_ok=False)):
            status, detail = onboarding._check_mcp_sdk()
            self.assertEqual(status, "WARN")
            self.assertIn("FastMCP won't import", detail)

        # Missing entirely → WARN with install hint.
        saved = sys.modules.pop("mcp", None)
        saved_server = sys.modules.pop("mcp.server", None)
        saved_fastmcp = sys.modules.pop("mcp.server.fastmcp", None)
        try:
            # Block re-import by setting the sentinel None.
            sys.modules["mcp"] = None
            status, detail = onboarding._check_mcp_sdk()
            self.assertEqual(status, "WARN")
            self.assertIn("not found", detail)
        finally:
            if saved is not None:
                sys.modules["mcp"] = saved
            else:
                sys.modules.pop("mcp", None)
            if saved_server is not None:
                sys.modules["mcp.server"] = saved_server
            if saved_fastmcp is not None:
                sys.modules["mcp.server.fastmcp"] = saved_fastmcp


if __name__ == "__main__":
    unittest.main()
