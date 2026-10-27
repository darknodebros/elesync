"""Tests for scoped sharing — per-client memory compartments.

Covers the four layers the feature touches:
  * the pure visibility logic (`elesync.scopes`),
  * persistence + round-trip + re-scoping in the store,
  * the grant-aware service surface an MCP client actually sees,
  * per-client wiring in onboarding.

The invariant under test everywhere: a `general` (unscoped) memory is visible to
everyone; an unrestricted client (grant=None) sees everything; a scoped client
sees general plus the compartments it was granted — and can neither read nor infer
the existence of anything else.
"""

import json
import tempfile
import unittest
from pathlib import Path

from elesync import scopes
from elesync.models import MemoryItem
from elesync.service import MemoryService
from elesync.store import MemoryStore


class TestScopeParsing(unittest.TestCase):
    def test_parse_normalises_lowercases_dedupes_orders(self):
        self.assertEqual(scopes.parse("Legal, work ,, legal"), ["legal", "work"])

    def test_parse_drops_general_as_implicit(self):
        self.assertEqual(scopes.parse("general, legal"), ["legal"])
        self.assertEqual(scopes.parse("General"), [])

    def test_parse_accepts_list_or_string_or_none(self):
        self.assertEqual(scopes.parse(["A", "b"]), ["a", "b"])
        self.assertEqual(scopes.parse(None), [])
        self.assertEqual(scopes.parse("a;b"), ["a", "b"])  # semicolons too

    def test_grant_from_unset_is_unrestricted(self):
        self.assertIsNone(scopes.grant_from(None))

    def test_grant_from_present_but_empty_is_general_only(self):
        g = scopes.grant_from("")
        self.assertEqual(g, frozenset())  # scoped, but no restricted labels

    def test_grant_from_labels(self):
        self.assertEqual(scopes.grant_from("writing, legal"), frozenset({"writing", "legal"}))

    def test_grant_from_general_only_is_tighter_than_unset(self):
        # "general" alone => scoped to general only, which is NOT the same as unset.
        self.assertEqual(scopes.grant_from("general"), frozenset())
        self.assertIsNotNone(scopes.grant_from("general"))


class TestVisibilityRule(unittest.TestCase):
    def test_general_memory_visible_to_everyone(self):
        for grant in (None, frozenset(), frozenset({"legal"})):
            self.assertTrue(scopes.is_visible([], grant))
            self.assertTrue(scopes.is_visible(["general"], grant))  # explicit general == []

    def test_unrestricted_client_sees_restricted_memory(self):
        self.assertTrue(scopes.is_visible(["legal"], None))

    def test_restricted_memory_hidden_without_matching_grant(self):
        self.assertFalse(scopes.is_visible(["legal"], frozenset()))
        self.assertFalse(scopes.is_visible(["legal"], frozenset({"writing"})))

    def test_restricted_memory_visible_on_intersection(self):
        self.assertTrue(scopes.is_visible(["legal", "tax"], frozenset({"writing", "legal"})))

    def test_describe_and_label(self):
        self.assertEqual(scopes.describe(None), "unrestricted (full vault)")
        self.assertEqual(scopes.describe(frozenset()), "general only")
        self.assertIn("writing", scopes.describe(frozenset({"writing"})))
        self.assertEqual(scopes.label_of([]), "general")
        self.assertEqual(scopes.label_of(["a", "b"]), "{a, b}")


class TestModelScopes(unittest.TestCase):
    def test_scopes_normalised_on_construction(self):
        m = MemoryItem(content="x", scopes="Legal, general, legal")
        self.assertEqual(m.scopes, ["legal"])

    def test_scopes_not_in_content_hash(self):
        # Re-scoping must not fork identity: same content/source/type -> same hash.
        a = MemoryItem(content="same", source="claude", type="fact", scopes=["legal"])
        b = MemoryItem(content="same", source="claude", type="fact", scopes=[])
        self.assertEqual(a.content_hash, b.content_hash)

    def test_markdown_roundtrip_preserves_scopes(self):
        m = MemoryItem(content="secret", type="fact", scopes=["legal", "tax"])
        back = MemoryItem.from_markdown(m.to_markdown())
        self.assertEqual(back.scopes, ["legal", "tax"])
        self.assertEqual(back.id, m.id)
        self.assertEqual(back.content_hash, m.content_hash)

    def test_old_note_without_scopes_line_defaults_general(self):
        legacy = (
            "---\n"
            "id: abc123\n"
            "source: claude\n"
            "type: fact\n"
            "tags: [home]\n"
            "confidence: 1.0\n"
            "created_at: 2026-01-01T00:00:00+00:00\n"
            "source_ref: \n"
            "content_hash: deadbeef\n"
            "---\n\n"
            "Based in Barcelona\n"
        )
        item = MemoryItem.from_markdown(legacy)
        self.assertEqual(item.scopes, [])  # general — unchanged behaviour


class TestStoreScopes(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.store = MemoryStore(self.tmp.name)

    def tearDown(self):
        self.store.close()
        self.tmp.cleanup()

    def _add(self, content, scopes_=None, source="claude", mtype="fact"):
        return self.store.upsert(
            MemoryItem(content=content, source=source, type=mtype, scopes=scopes_ or [])
        )

    def test_scopes_persist_to_db_and_note(self):
        mem_id = self._add("legal stuff", ["legal"])
        item = self.store.get(mem_id)
        self.assertEqual(item.scopes, ["legal"])
        note = next((Path(self.tmp.name) / "notes").glob("*.md"))
        self.assertIn("scopes: [legal]", note.read_text())

    def test_reindex_preserves_scopes(self):
        self._add("legal stuff", ["legal"])
        self._add("general stuff", [])
        self.store.reindex()  # rebuild index from notes (the source of truth)
        got = {tuple(i.scopes) for i in self.store.all()}
        self.assertIn(("legal",), got)
        self.assertIn((), got)

    def test_search_with_grant_filters(self):
        self._add("writing voice notes", ["writing"])
        self._add("legal matter notes", ["legal"])
        self._add("general notes", [])
        # Unrestricted: sees all three.
        self.assertEqual(len(self.store.search("notes", grant=None)), 3)
        # Granted writing only: general + writing, never legal.
        hits = self.store.search("notes", grant=frozenset({"writing"}))
        contents = " ".join(h.content for h in hits)
        self.assertIn("general", contents)
        self.assertIn("writing", contents)
        self.assertNotIn("legal", contents)

    def test_set_scopes_updates_db_and_note_without_forking(self):
        mem_id = self._add("reassign me", [])
        original = self.store.get(mem_id)
        self.assertTrue(self.store.set_scopes(mem_id, "legal, work"))
        updated = self.store.get(mem_id)
        self.assertEqual(updated.scopes, ["legal", "work"])
        self.assertEqual(updated.id, original.id)  # same identity
        self.assertEqual(updated.content_hash, original.content_hash)
        self.assertEqual(self.store.count(), 1)  # not duplicated
        note = next((Path(self.tmp.name) / "notes").glob("*.md"))
        self.assertIn("scopes: [legal, work]", note.read_text())

    def test_set_scopes_clear_back_to_general(self):
        mem_id = self._add("scoped", ["legal"])
        self.store.set_scopes(mem_id, "")
        self.assertEqual(self.store.get(mem_id).scopes, [])

    def test_set_scopes_missing_returns_false(self):
        self.assertFalse(self.store.set_scopes("nope", ["legal"]))

    def test_scope_counts_full_and_granted(self):
        self._add("a", [])
        self._add("b", [])
        self._add("c", ["legal"])
        self._add("d", ["writing"])
        full = self.store.scope_counts()
        self.assertEqual(full["general"], 2)
        self.assertEqual(full["legal"], 1)
        self.assertEqual(full["writing"], 1)
        # A writing-only client can't enumerate the legal compartment.
        granted = self.store.scope_counts(grant=frozenset({"writing"}))
        self.assertEqual(granted.get("general"), 2)
        self.assertEqual(granted.get("writing"), 1)
        self.assertNotIn("legal", granted)


class TestServiceScopes(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.store = MemoryStore(self.tmp.name)

    def tearDown(self):
        self.store.close()
        self.tmp.cleanup()

    def _seed(self):
        self.store.upsert(
            MemoryItem(
                content="Writes in a dry, terse voice",
                type="preference",
                source="claude",
                scopes=["writing"],
            )
        )
        self.store.upsert(
            MemoryItem(
                content="Active lawsuit against Acme Corp",
                type="fact",
                source="claude",
                scopes=["legal"],
            )
        )
        self.store.upsert(MemoryItem(content="Based in Barcelona", type="fact", source="claude"))

    def test_unrestricted_client_sees_all(self):
        self._seed()
        svc = MemoryService(self.store, grant=None)  # unrestricted owner
        out = svc.recall("", limit=10)
        self.assertIn("Barcelona", out)
        self.assertIn("lawsuit", out)
        self.assertIn("voice", out)

    def test_recall_scoped_client_sees_only_its_compartment_plus_general(self):
        self._seed()
        svc = MemoryService(self.store, grant=frozenset({"writing"}))
        out = svc.recall("", limit=10)
        self.assertIn("voice", out)  # granted
        self.assertIn("Barcelona", out)  # general
        self.assertNotIn("lawsuit", out)  # legal — hidden

    def test_recall_shows_scope_label(self):
        self._seed()
        svc = MemoryService(self.store, grant=None)
        out = svc.recall("voice")
        self.assertIn("{writing}", out)

    def test_remember_uses_default_write_scopes(self):
        svc = MemoryService(self.store, grant=frozenset({"legal"}), write_scopes=["legal"])
        msg = svc.remember("New privileged note", type="fact")
        self.assertIn("legal", msg)
        self.assertEqual(self.store.all()[0].scopes, ["legal"])

    def test_remember_explicit_scopes_override_default(self):
        svc = MemoryService(self.store, write_scopes=["legal"])
        svc.remember("public fact", type="fact", scopes="general")  # explicit general
        self.assertEqual(self.store.all()[0].scopes, [])

    def test_status_scoped_client_does_not_leak_hidden_counts(self):
        self._seed()
        svc = MemoryService(self.store, grant=frozenset({"writing"}))
        out = svc.status()
        self.assertIn("2 memories", out)  # writing + general only, not the legal one
        self.assertIn("Scope:", out)

    def test_conflicts_respects_grant(self):
        # Two conflicting legal facts; a writing-scoped client must not see the clash.
        self.store.upsert(
            MemoryItem(
                content="Settlement is 10000 euros", type="fact", source="claude", scopes=["legal"]
            )
        )
        self.store.upsert(
            MemoryItem(
                content="Settlement is 25000 euros", type="fact", source="chatgpt", scopes=["legal"]
            )
        )
        unrestricted = MemoryService(self.store, grant=None).conflicts()
        self.assertIn("conflict", unrestricted.lower())
        scoped = MemoryService(self.store, grant=frozenset({"writing"})).conflicts()
        self.assertEqual(scoped, "No conflicting memories found.")

    def test_forget_scoped_client_cannot_target_hidden_memory(self):
        self._seed()
        legal_id = [i.id for i in self.store.all() if i.scopes == ["legal"]][0]
        svc = MemoryService(self.store, grant=frozenset({"writing"}))
        msg = svc.forget(legal_id)
        self.assertIn("No such memory", msg)
        self.assertEqual(self.store.count(), 3)  # nothing deleted

    def test_scope_mutator_reassigns(self):
        mem_id = self.store.upsert(MemoryItem(content="reassign", type="fact"))
        svc = MemoryService(self.store, grant=None)
        msg = svc.scope(mem_id[:8], "legal")
        self.assertIn("legal", msg)
        self.assertEqual(self.store.get(mem_id).scopes, ["legal"])

    def test_scope_summary_lists_visible_compartments(self):
        self._seed()
        out = MemoryService(self.store, grant=None).scope_summary()
        self.assertIn("general", out)
        self.assertIn("writing", out)
        self.assertIn("legal", out)


class TestEncryptedReScoping(unittest.TestCase):
    """Re-scoping must work in locked mode too — rewriting a note re-encrypts it,
    and nothing plaintext touches disk. Skipped if the encryption extra is absent."""

    def setUp(self):
        try:
            from elesync import crypto
        except Exception:  # pragma: no cover
            self.skipTest("crypto import failed")
        if not crypto.available():
            self.skipTest("PyNaCl not installed")

    def test_set_scopes_in_locked_mode(self):
        from elesync import crypto

        with tempfile.TemporaryDirectory() as d:
            # Build a plaintext vault, then encrypt it by hand at the store layer.
            store = MemoryStore(d)
            mem_id = store.upsert(MemoryItem(content="locked secret", type="fact"))
            store.close()
            # Encrypt the single note in place + write metadata, mirroring `ele encrypt`.
            salt = crypto.new_salt()
            ops, mem = crypto.default_limits()
            key = crypto.derive_key("pw", salt, ops, mem)
            notes = Path(d) / "notes"
            for md in notes.glob("*.md"):
                md.with_name(md.name + ".enc").write_bytes(crypto.encrypt(key, md.read_bytes()))
                md.unlink()
            (Path(d) / "index.db").unlink(missing_ok=True)
            crypto.write_meta(Path(d), salt, ops, mem)

            locked = MemoryStore(d, key=key)
            try:
                self.assertTrue(locked.encrypted)
                self.assertTrue(locked.set_scopes(mem_id, "legal"))
                self.assertEqual(locked.get(mem_id).scopes, ["legal"])
                # The on-disk note is still ciphertext (decrypts back to the scope line).
                enc = next(notes.glob("*.md.enc"))
                plain = crypto.decrypt(key, enc.read_bytes()).decode()
                self.assertIn("scopes: [legal]", plain)
            finally:
                locked.close()


class TestOnboardingScopes(unittest.TestCase):
    def test_server_entry_injects_scope_env(self):
        from elesync import onboarding

        entry = onboarding.server_entry("/v", scopes="general,writing", write_scope="writing")
        self.assertEqual(entry["env"]["ELESYNC_SCOPES"], "general,writing")
        self.assertEqual(entry["env"]["ELESYNC_WRITE_SCOPE"], "writing")

    def test_server_entry_unscoped_has_no_scope_env(self):
        from elesync import onboarding

        entry = onboarding.server_entry("/v")
        self.assertNotIn("ELESYNC_SCOPES", entry["env"])
        self.assertNotIn("ELESYNC_WRITE_SCOPE", entry["env"])

    def test_server_entry_injects_project_env(self):
        from elesync import onboarding

        entry = onboarding.server_entry("/v", project_id="project-123")
        self.assertEqual(entry["env"]["ELESYNC_PROJECT_ID"], "project-123")

    def test_claude_project_entry_injects_provider_env(self):
        from elesync import onboarding

        snippet = onboarding.print_snippet_for_target(
            "claude-desktop", "/v", project_id="project-123"
        )
        entry = json.loads(snippet.split("\n\n", 1)[1])["mcpServers"]["elesync"]
        self.assertEqual(entry["env"]["ELESYNC_PROJECT_PROVIDER"], "claude")

    def test_wire_with_custom_name_allows_two_entries(self):
        from elesync import onboarding

        with tempfile.TemporaryDirectory() as d:
            cfg = Path(d) / "config.json"
            onboarding.wire_client_config(cfg, "/v", name="elesync")
            onboarding.wire_client_config(cfg, "/v", scopes="legal", name="elesync-legal")
            data = json.loads(cfg.read_text())
            self.assertIn("elesync", data["mcpServers"])
            self.assertIn("elesync-legal", data["mcpServers"])
            self.assertEqual(data["mcpServers"]["elesync-legal"]["env"]["ELESYNC_SCOPES"], "legal")

    def test_doctor_reports_scope_grant(self):
        from elesync import onboarding

        with tempfile.TemporaryDirectory() as d:
            cfg = Path(d) / "config.json"
            vault = str(Path(d) / "vault")
            onboarding.wire_client_config(cfg, vault, scopes="general,writing")
            rows = onboarding.run_doctor(vault, config_path=cfg)
            wired = [r for r in rows if r[1] == "MCP client config wired"][0]
            self.assertEqual(wired[0], "PASS")
            self.assertIn("writing", wired[2])


if __name__ == "__main__":
    unittest.main()
