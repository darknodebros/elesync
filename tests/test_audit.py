"""Tests for audit logging and read-only scope enforcement (v1.10.0).

Covers:
  * AuditLog: write, read, filter, format, clear, disabled (None) mode
  * Read-only scopes: parsing, service-layer enforcement across all mutators
  * Service + audit integration: operations produce log entries
  * MCP tool: audit_log tool callable via FastMCP.call_tool
  * Onboarding: server_entry with readonly / audit flags
"""

import json
import os
import tempfile
import unittest
from pathlib import Path

from elesync import scopes
from elesync.audit import AuditLog
from elesync.models import MemoryItem
from elesync.project_service import ProjectService
from elesync.service import READONLY_MSG, MemoryService
from elesync.store import MemoryStore

# ── AuditLog unit tests ─────────────────────────────────────────────────


class TestAuditLogDisabled(unittest.TestCase):
    """AuditLog(None) must be a silent no-op — no file created, no errors."""

    def test_log_does_not_raise(self):
        a = AuditLog(None)
        a.log("REMEMBER", client="test", memory_id="abc")

    def test_read_returns_empty(self):
        self.assertEqual(AuditLog(None).read(), [])

    def test_enabled_is_false(self):
        self.assertFalse(AuditLog(None).enabled)

    def test_clear_returns_false(self):
        self.assertFalse(AuditLog(None).clear())

    def test_count_returns_zero(self):
        self.assertEqual(AuditLog(None).count(), 0)


class TestAuditLogEnabled(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.audit = AuditLog(self.tmp.name)

    def tearDown(self):
        self.tmp.cleanup()

    def test_enabled_is_true(self):
        self.assertTrue(self.audit.enabled)

    def test_log_creates_jsonl_file(self):
        self.audit.log("REMEMBER", client="claude", memory_id="abc12345")
        self.assertTrue(self.audit.log_file.exists())

    def test_log_and_read_roundtrip(self):
        self.audit.log("REMEMBER", client="claude", memory_id="abc12345", detail="type=fact")
        self.audit.log("RECALL", client="chatgpt", detail="query='hello'")
        entries = self.audit.read()
        self.assertEqual(len(entries), 2)
        # newest first
        self.assertEqual(entries[0]["op"], "RECALL")
        self.assertEqual(entries[1]["op"], "REMEMBER")
        self.assertEqual(entries[1]["mem"], "abc12345")

    def test_read_filter_by_operation(self):
        self.audit.log("REMEMBER", client="a")
        self.audit.log("RECALL", client="b")
        self.audit.log("REMEMBER", client="c")
        entries = self.audit.read(operation="REMEMBER")
        self.assertEqual(len(entries), 2)
        self.assertTrue(all(e["op"] == "REMEMBER" for e in entries))

    def test_read_filter_by_client(self):
        self.audit.log("RECALL", client="claude")
        self.audit.log("RECALL", client="chatgpt")
        entries = self.audit.read(client="claude")
        self.assertEqual(len(entries), 1)
        self.assertEqual(entries[0]["client"], "claude")

    def test_read_last_limits(self):
        for i in range(10):
            self.audit.log("RECALL", detail=str(i))
        entries = self.audit.read(last=3)
        self.assertEqual(len(entries), 3)
        # newest first — last logged was detail="9"
        self.assertEqual(entries[0]["detail"], "9")

    def test_count(self):
        self.assertEqual(self.audit.count(), 0)
        self.audit.log("REMEMBER")
        self.audit.log("RECALL")
        self.assertEqual(self.audit.count(), 2)

    def test_clear(self):
        self.audit.log("REMEMBER")
        self.assertTrue(self.audit.clear())
        self.assertEqual(self.audit.count(), 0)
        self.assertFalse(self.audit.log_file.exists())

    def test_format_entries_empty(self):
        self.assertEqual(self.audit.format_entries([]), "No audit entries.")

    def test_format_entries_produces_readable_output(self):
        self.audit.log("REMEMBER", client="claude", memory_id="abc12345")
        entries = self.audit.read()
        out = self.audit.format_entries(entries)
        self.assertIn("REMEMBER", out)
        self.assertIn("claude", out)
        self.assertIn("abc12345", out)

    def test_malformed_lines_are_skipped(self):
        # Write a corrupt line directly
        with self.audit.log_file.open("a", encoding="utf-8") as f:
            f.write("this is not json\n")
        self.audit.log("RECALL", client="ok")
        entries = self.audit.read()
        self.assertEqual(len(entries), 1)
        self.assertEqual(entries[0]["op"], "RECALL")


# ── Read-only scope parsing ─────────────────────────────────────────────


class TestReadonlyScopes(unittest.TestCase):
    def test_is_readonly_plain_scopes(self):
        self.assertFalse(scopes.is_readonly(None))
        self.assertFalse(scopes.is_readonly(""))
        self.assertFalse(scopes.is_readonly("writing,legal"))

    def test_is_readonly_readonly_prefix(self):
        self.assertTrue(scopes.is_readonly("readonly"))
        self.assertTrue(scopes.is_readonly("readonly:writing,legal"))
        self.assertTrue(scopes.is_readonly("READONLY:writing"))  # case insensitive

    def test_strip_readonly(self):
        self.assertEqual(scopes.strip_readonly("readonly"), "")
        self.assertEqual(scopes.strip_readonly("readonly:writing,legal"), "writing,legal")
        self.assertEqual(scopes.strip_readonly("writing,legal"), "writing,legal")
        self.assertEqual(scopes.strip_readonly("READONLY:X"), "X")

    def test_describe_readonly(self):
        self.assertIn("read-only", scopes.describe(frozenset({"writing"}), readonly=True))
        self.assertNotIn("read-only", scopes.describe(frozenset({"writing"}), readonly=False))

    def test_describe_without_readonly_keyword_unchanged(self):
        # Existing callers that don't pass readonly= still work
        self.assertEqual(scopes.describe(None), "unrestricted (full vault)")
        self.assertEqual(scopes.describe(frozenset()), "general only")


# ── Read-only service enforcement ────────────────────────────────────────


class TestReadonlyService(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.store = MemoryStore(self.tmp.name)
        # Seed a memory so forget/scope have something to target
        self.mem_id = self.store.upsert(
            MemoryItem(content="test fact", type="fact", source="claude")
        )
        self.svc = MemoryService(self.store, readonly=True)

    def tearDown(self):
        self.store.close()
        self.tmp.cleanup()

    def test_remember_blocked(self):
        result = self.svc.remember("new fact", type="fact")
        self.assertEqual(result, READONLY_MSG)
        self.assertEqual(self.store.count(), 1)  # nothing added

    def test_forget_blocked(self):
        result = self.svc.forget(self.mem_id[:8])
        self.assertEqual(result, READONLY_MSG)
        self.assertEqual(self.store.count(), 1)  # nothing deleted

    def test_scope_blocked(self):
        result = self.svc.scope(self.mem_id[:8], "legal")
        self.assertEqual(result, READONLY_MSG)
        self.assertEqual(self.store.get(self.mem_id).scopes, [])  # unchanged

    def test_resolve_conflict_blocked(self):
        result = self.svc.resolve_conflict("abc", choose_id="xyz")
        self.assertEqual(result, READONLY_MSG)

    def test_recall_allowed(self):
        result = self.svc.recall("test")
        self.assertIn("test fact", result)

    def test_status_allowed(self):
        result = self.svc.status()
        self.assertIn("1 memories", result)

    def test_conflicts_allowed(self):
        result = self.svc.conflicts()
        # Should not raise — read operations work in readonly mode
        self.assertIsInstance(result, str)


# ── Service + audit integration ──────────────────────────────────────────


class TestServiceAuditIntegration(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.audit = AuditLog(self.tmp.name)
        self.store = MemoryStore(self.tmp.name)
        self.svc = MemoryService(self.store, audit=self.audit)

    def tearDown(self):
        self.store.close()
        self.tmp.cleanup()

    def test_remember_logged(self):
        self.svc.remember("audit me", type="fact", source="claude")
        entries = self.audit.read(operation="REMEMBER")
        self.assertEqual(len(entries), 1)
        self.assertIn("type=fact", entries[0]["detail"])

    def test_recall_logged(self):
        self.svc.recall("something")
        entries = self.audit.read(operation="RECALL")
        self.assertEqual(len(entries), 1)

    def test_forget_logged(self):
        self.svc.remember("to delete", type="note")
        mem = self.store.all()[0]
        self.svc.forget(mem.id[:8])
        entries = self.audit.read(operation="FORGET")
        self.assertEqual(len(entries), 1)

    def test_scope_logged(self):
        self.svc.remember("to scope", type="note")
        mem = self.store.all()[0]
        self.svc.scope(mem.id[:8], "legal")
        entries = self.audit.read(operation="SCOPE")
        self.assertEqual(len(entries), 1)
        self.assertIn("legal", entries[0]["detail"])

    def test_status_logged(self):
        self.svc.status()
        entries = self.audit.read(operation="STATUS")
        self.assertEqual(len(entries), 1)

    def test_audit_summary(self):
        self.svc.remember("a", type="fact")
        self.svc.recall("a")
        out = self.svc.audit_summary(last=10)
        self.assertIn("REMEMBER", out)
        self.assertIn("RECALL", out)


class TestReadonlyAuditDenials(unittest.TestCase):
    """Read-only denials should also be logged in the audit trail."""

    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.audit = AuditLog(self.tmp.name)
        self.store = MemoryStore(self.tmp.name)
        self.svc = MemoryService(self.store, readonly=True, audit=self.audit)

    def tearDown(self):
        self.store.close()
        self.tmp.cleanup()

    def test_denied_remember_logged(self):
        self.svc.remember("nope")
        entries = self.audit.read(operation="REMEMBER")
        self.assertEqual(len(entries), 1)
        self.assertIn("DENIED", entries[0]["detail"])

    def test_denied_forget_logged(self):
        self.svc.forget("abc12345")
        entries = self.audit.read(operation="FORGET")
        self.assertEqual(len(entries), 1)
        self.assertIn("DENIED", entries[0]["detail"])

    def test_denied_scope_logged(self):
        self.svc.scope("abc12345", "legal")
        entries = self.audit.read(operation="SCOPE")
        self.assertEqual(len(entries), 1)
        self.assertIn("DENIED", entries[0]["detail"])


# ── MCP tool integration (end-to-end via FastMCP.call_tool) ──────────────


class TestMCPAuditTool(unittest.TestCase):
    """Test the audit_log MCP tool via FastMCP.call_tool — the same path
    a real AI client exercises over the protocol."""

    @classmethod
    def setUpClass(cls):
        try:
            from mcp.server.fastmcp import FastMCP  # noqa: F401
        except ImportError:
            raise unittest.SkipTest("MCP SDK not installed")

    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.vault = self.tmp.name
        os.environ["ELESYNC_DIR"] = self.vault
        os.environ["ELESYNC_AUDIT"] = "1"

    def tearDown(self):
        # Close the store attached to the server to avoid leaked fds
        if hasattr(self, "_server"):
            store = getattr(self._server, "elesync_store", None)
            if store:
                store.close()
        os.environ.pop("ELESYNC_DIR", None)
        os.environ.pop("ELESYNC_AUDIT", None)
        os.environ.pop("ELESYNC_SCOPES", None)
        self.tmp.cleanup()

    def _build(self):
        from elesync.mcp_server import build_server

        self._server = build_server(vault_dir=self.vault)
        return self._server

    def _text(self, result) -> str:
        """Extract text from FastMCP call_tool result (returns a tuple)."""
        content_list = result[0] if isinstance(result, tuple) else result
        return "".join(b.text for b in content_list if hasattr(b, "text"))

    def test_audit_tool_returns_entries(self):
        import asyncio

        async def run():
            server = self._build()
            # Generate some activity
            await server.call_tool("remember", {"content": "test fact", "type": "fact"})
            await server.call_tool("recall", {"query": "test"})
            result = await server.call_tool("audit_log", {"last": 10})
            return result

        result = asyncio.run(run())
        text = self._text(result)
        self.assertIn("REMEMBER", text)
        self.assertIn("RECALL", text)

    def test_audit_tool_disabled(self):
        import asyncio

        os.environ["ELESYNC_AUDIT"] = "0"

        async def run():
            server = self._build()
            result = await server.call_tool("audit_log", {"last": 10})
            return result

        result = asyncio.run(run())
        text = self._text(result)
        self.assertIn("not enabled", text)

    def test_readonly_via_env(self):
        """ELESYNC_SCOPES=readonly:general blocks writes through MCP."""
        import asyncio

        os.environ["ELESYNC_SCOPES"] = "readonly:general"

        async def run():
            server = self._build()
            result = await server.call_tool("remember", {"content": "should fail"})
            return result

        result = asyncio.run(run())
        text = self._text(result)
        self.assertIn("read-only", text)

    def _logged(self) -> list[dict]:
        path = Path(self.vault) / "audit.jsonl"
        return [json.loads(line) for line in path.read_text(encoding="utf-8").splitlines() if line]

    def test_project_provider_names_the_ai_in_audit_entries(self):
        import asyncio

        os.environ["ELESYNC_PROJECT_PROVIDER"] = "ChatGPT"
        try:
            asyncio.run(self._build().call_tool("remember", {"content": "relay fact"}))
        finally:
            os.environ.pop("ELESYNC_PROJECT_PROVIDER", None)
        self.assertEqual(self._logged()[-1]["ai"], "chatgpt")

    def test_revoked_project_connection_is_refused_and_logged(self):
        import asyncio

        store = MemoryStore(Path(self.vault))
        project = store.create_project("Kyoto trip")
        store.set_project_access(project["id"], "chatgpt", True, True, False)
        ProjectService(store).revoke_access(project["id"], "chatgpt")
        store.close()
        os.environ["ELESYNC_PROJECT_ID"] = project["id"]
        os.environ["ELESYNC_PROJECT_PROVIDER"] = "chatgpt"
        try:
            from mcp.server.fastmcp.exceptions import ToolError

            server = self._build()
            with self.assertRaises(ToolError):
                asyncio.run(
                    server.call_tool(
                        "recall_project", {"project_id": project["id"], "query": "hotel"}
                    )
                )
        finally:
            os.environ.pop("ELESYNC_PROJECT_ID", None)
            os.environ.pop("ELESYNC_PROJECT_PROVIDER", None)
        denied = self._logged()[-1]
        self.assertEqual(denied["op"], "ACCESS_DENIED")
        self.assertEqual(denied["ai"], "chatgpt")
        self.assertIn(f"project={project['id'][:8]}", denied["detail"])

    def test_unknown_client_leaves_ai_unset(self):
        import asyncio

        asyncio.run(self._build().call_tool("remember", {"content": "anonymous fact"}))
        self.assertNotIn("ai", self._logged()[-1])


class TestAssistantAttribution(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)

    def _svc(self):
        store = MemoryStore(Path(self.tmp.name))
        self.addCleanup(store.close)
        log = AuditLog(self.tmp.name)
        return MemoryService(store, audit=log), log

    def test_disagreeing_fact_is_flagged_as_conflict(self):
        svc, log = self._svc()
        svc.remember("The Kyoto flight leaves at noon on October 12.", type="fact", source="claude")
        svc.remember("The Kyoto flight leaves at 3pm on October 12.", type="fact", source="chatgpt")
        newest = log.read()[0]
        self.assertEqual(newest["op"], "CONFLICT")
        first_id = log.read(operation="REMEMBER")[-1]["mem"]
        self.assertEqual(newest["detail"], f"with={first_id}")

    def test_unrelated_fact_is_not_flagged(self):
        svc, log = self._svc()
        svc.remember("The Kyoto flight leaves at noon on October 12.", type="fact")
        svc.remember("My sister lives in Lisbon.", type="fact")
        self.assertEqual(log.read(operation="CONFLICT"), [])

    def test_auto_superseded_update_is_not_also_flagged(self):
        svc, log = self._svc()
        svc.remember("The Kyoto flight leaves at noon on October 12.", type="fact", tags="flight")
        svc.remember("The Kyoto flight leaves at 3pm on October 12.", type="fact", tags="flight")
        self.assertEqual(log.read(operation="CONFLICT"), [])
        self.assertEqual(len(log.read(operation="SUPERSEDE")), 1)

    def test_project_reads_and_document_saves_are_logged(self):
        store = MemoryStore(Path(self.tmp.name))
        self.addCleanup(store.close)
        log = AuditLog(self.tmp.name)
        project = store.create_project("Japan trip")
        svc = MemoryService(store, audit=log)
        svc.save_project_document(project["id"], "Flights", "Noon departure", "decision")
        svc.project_context(project["id"], query="flight")
        context_entry, document_entry = log.read()
        self.assertEqual(document_entry["op"], "PROJECT_DOCUMENT")
        self.assertIn(f"project={project['id'][:8]}", document_entry["detail"])
        self.assertIn("title='Flights'", document_entry["detail"])
        self.assertEqual(context_entry["op"], "PROJECT_CONTEXT")
        self.assertIn("query='flight'", context_entry["detail"])

    def test_entries_record_the_current_assistant(self):
        import contextvars

        from elesync.audit import current_ai

        log = AuditLog(self.tmp.name)

        def logged_as_claude():
            current_ai.set("claude")
            log.log("REMEMBER", client="x", memory_id="abcd1234")

        contextvars.copy_context().run(logged_as_claude)
        log.log("RECALL", client="x")
        newest, older = log.read()
        self.assertEqual(older["ai"], "claude")
        self.assertNotIn("ai", newest)

    def test_normalize_ai_name(self):
        from elesync.audit import normalize_ai_name

        cases = {
            "claude-ai": "claude",
            "Claude Desktop": "claude",
            "openai-mcp": "chatgpt",
            "Google Gemini": "gemini",
            "xai-grok": "grok",
            "Cursor": "cursor",
            "My Tool!": "my tool",
            "": "",
            None: "",
        }
        for raw, expected in cases.items():
            self.assertEqual(normalize_ai_name(raw), expected, raw)

    def test_save_without_a_source_is_attributed_to_the_calling_assistant(self):
        import contextvars

        from elesync.audit import current_ai

        svc, _log = self._svc()

        def save_as_claude():
            current_ai.set("claude")
            return svc.remember("Hotel in Kyoto is booked near Gion.", type="fact")

        contextvars.copy_context().run(save_as_claude)
        svc.remember("A save with no assistant attached.")
        explicit = contextvars.copy_context().run(
            lambda: (current_ai.set("claude"), svc.remember("Named by the caller.", source="grok"))
        )
        self.assertIn("Saved", explicit[1])
        sources = {item.content: item.source for item in svc.store.all()}
        self.assertEqual(sources["Hotel in Kyoto is booked near Gion."], "claude")
        self.assertEqual(sources["A save with no assistant attached."], "ai")
        self.assertEqual(sources["Named by the caller."], "grok")

    def test_repeated_denials_are_logged_once_per_window(self):
        from unittest import mock

        from elesync import access

        store = MemoryStore(Path(self.tmp.name))
        self.addCleanup(store.close)
        log = AuditLog(self.tmp.name)
        project = store.create_project("Kyoto trip")
        for _ in range(4):
            with self.assertRaises(PermissionError):
                access.connection_permissions(store, project["id"], "chatgpt", audit=log)
        self.assertEqual(len(log.read(operation="ACCESS_DENIED")), 1)

        later = access.DENIAL_LOG_WINDOW_SECONDS + 1
        with mock.patch.object(access.time, "monotonic", return_value=10**9 + later):
            with self.assertRaises(PermissionError):
                access.connection_permissions(store, project["id"], "chatgpt", audit=log)
        self.assertEqual(len(log.read(operation="ACCESS_DENIED")), 2)


class TestAuditTailRead(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        self.log = AuditLog(self.tmp.name)

    def _write(self, count):
        with self.log.log_file.open("a", encoding="utf-8") as f:
            for n in range(count):
                op = "REMEMBER" if n % 3 else "RECALL"
                f.write(json.dumps({"ts": f"t{n}", "op": op, "detail": "x" * (n % 50)}) + "\n")

    def test_limited_read_matches_a_full_read_across_block_boundaries(self):
        self._write(5000)
        with self.log.log_file.open("a", encoding="utf-8") as f:
            f.write("not json\n\n")
            f.write(json.dumps({"ts": "last", "op": "RECALL", "detail": "é"}) + "\n")
        full = self.log.read()
        self.assertEqual(len(full), 5001)
        self.assertEqual(full[0]["ts"], "last")
        for last in (1, 15, 777, 5001, 9999):
            self.assertEqual(self.log.read(last=last), full[:last], last)
        self.assertEqual(
            self.log.read(operation="RECALL", last=40),
            [e for e in full if e["op"] == "RECALL"][:40],
        )

    def test_limited_read_stops_near_the_end_of_a_large_log(self):
        from unittest import mock

        self._write(20000)
        size = self.log.log_file.stat().st_size
        read_sizes = []
        real_open = Path.open

        def tracking_open(path, *args, **kwargs):
            handle = real_open(path, *args, **kwargs)
            original = handle.read

            def read(n=-1):
                data = original(n)
                read_sizes.append(len(data))
                return data

            handle.read = read
            return handle

        with mock.patch.object(Path, "open", tracking_open):
            entries = self.log.read(last=15)
        self.assertEqual([e["ts"] for e in entries], [f"t{n}" for n in range(19999, 19984, -1)])
        self.assertLess(sum(read_sizes), size / 4)


# ── Onboarding wiring ───────────────────────────────────────────────────


class TestOnboardingReadonlyAudit(unittest.TestCase):
    def test_server_entry_readonly(self):
        from elesync import onboarding

        entry = onboarding.server_entry("/v", scopes="writing", readonly=True)
        self.assertEqual(entry["env"]["ELESYNC_SCOPES"], "readonly:writing")

    def test_server_entry_readonly_no_scopes(self):
        from elesync import onboarding

        entry = onboarding.server_entry("/v", readonly=True)
        self.assertEqual(entry["env"]["ELESYNC_SCOPES"], "readonly")

    def test_server_entry_audit(self):
        from elesync import onboarding

        entry = onboarding.server_entry("/v", audit=True)
        self.assertEqual(entry["env"]["ELESYNC_AUDIT"], "1")

    def test_server_entry_audit_is_on_by_default(self):
        from elesync import onboarding

        entry = onboarding.server_entry("/v")
        self.assertEqual(entry["env"]["ELESYNC_AUDIT"], "1")

    def test_server_entry_readonly_plus_audit(self):
        from elesync import onboarding

        entry = onboarding.server_entry("/v", scopes="legal", readonly=True, audit=True)
        self.assertEqual(entry["env"]["ELESYNC_SCOPES"], "readonly:legal")
        self.assertEqual(entry["env"]["ELESYNC_AUDIT"], "1")


if __name__ == "__main__":
    unittest.main()


class TestOnboardAuditFlag(unittest.TestCase):
    """`ele onboard --audit` writes ELESYNC_AUDIT=1 into the client env block.

    `server_entry` accepted an `audit` argument from the start, but nothing
    reached it: the CLI had no flag and neither `wire_client_config` nor
    `print_snippet_for_target` forwarded one. So the documented post-incident
    remediation ("set ELESYNC_AUDIT=1 in each client's env block") could only be
    done by hand-editing every config file, which is why it never happened.
    These tests keep the wiring connected end to end.
    """

    def setUp(self):
        self._tmp = tempfile.TemporaryDirectory()
        self.root = Path(self._tmp.name)
        self.vault = self.root / "vault"

    def tearDown(self):
        self._tmp.cleanup()

    def _env_from_written_config(self, *extra) -> dict:
        from elesync import cli

        cfg = self.root / "claude.json"
        code = cli.main(
            [
                "--vault",
                str(self.vault),
                "onboard",
                "--target",
                "claude-desktop",
                "--config-path",
                str(cfg),
                *extra,
            ]
        )
        self.assertEqual(code, 0)
        blob = json.loads(cfg.read_text(encoding="utf-8"))
        return blob["mcpServers"]["elesync"]["env"]

    def test_flag_writes_the_env_var(self):
        self.assertEqual(self._env_from_written_config("--audit").get("ELESYNC_AUDIT"), "1")

    def test_enabled_by_default(self):
        self.assertEqual(self._env_from_written_config().get("ELESYNC_AUDIT"), "1")

    def test_print_only_snippet_includes_it(self):
        from elesync import onboarding

        snippet = onboarding.print_snippet_for_target("claude-desktop", str(self.vault), audit=True)
        self.assertIn("ELESYNC_AUDIT", snippet)
        self.assertIn(
            "ELESYNC_AUDIT",
            onboarding.print_snippet_for_target("claude-desktop", str(self.vault)),
        )

    def test_the_env_var_is_what_the_server_actually_reads(self):
        # Guards the contract between the config we write and the name the MCP
        # server checks -- a rename on either side would silently disable audit.
        server = (Path(__file__).resolve().parent.parent / "elesync" / "mcp_server.py").read_text(
            encoding="utf-8"
        )
        self.assertIn('os.environ.get("ELESYNC_AUDIT")', server)
