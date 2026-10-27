"""Tests for memory templates (elesync.templates) and their CLI/MCP surfaces.

The point of a template is that captures come out the same shape every time, so
most of these tests are about *refusing* to produce something almost-right: a
missing placeholder, a misspelled `--set` key, an unknown JSON key. Silently
absorbing any of those would give back exactly the inconsistency the feature is
meant to remove.
"""

from __future__ import annotations

import io
import json
import tempfile
import unittest
from contextlib import redirect_stderr, redirect_stdout
from pathlib import Path

from elesync import i18n, templates
from elesync.service import MemoryService
from elesync.store import MemoryStore


class TemplateTestCase(unittest.TestCase):
    def setUp(self):
        self._tmp = tempfile.TemporaryDirectory()
        self.vault = Path(self._tmp.name)

    def tearDown(self):
        self._tmp.cleanup()

    def write_template(self, name: str, data: dict) -> Path:
        directory = templates.templates_dir(self.vault)
        directory.mkdir(parents=True, exist_ok=True)
        path = directory / f"{name}.json"
        path.write_text(json.dumps(data), encoding="utf-8")
        return path


# ---------------------------------------------------------------------------
# Template model
# ---------------------------------------------------------------------------


class TestTemplateModel(unittest.TestCase):
    def test_fields_are_extracted_in_order(self):
        tpl = templates.Template(name="t", body="{b} then {a} then {b} again")
        self.assertEqual(tpl.fields, ["b", "a"])  # first appearance, deduplicated

    def test_render_fills_placeholders(self):
        tpl = templates.Template(name="t", body="Hello {name}, you are {role}.")
        self.assertEqual(tpl.render({"name": "Alex", "role": "PM"}), "Hello Alex, you are PM.")

    def test_render_rejects_missing_field(self):
        tpl = templates.Template(name="t", body="{a} {b}")
        with self.assertRaises(templates.TemplateError) as ctx:
            tpl.render({"a": "x"})
        self.assertIn("missing: b", str(ctx.exception))

    def test_render_rejects_blank_field(self):
        # A whitespace-only value produces "Met with  on ..." — technically
        # rendered, actually useless.
        tpl = templates.Template(name="t", body="{a}!")
        with self.assertRaises(templates.TemplateError):
            tpl.render({"a": "   "})

    def test_render_rejects_unexpected_field(self):
        tpl = templates.Template(name="t", body="{a}")
        with self.assertRaises(templates.TemplateError) as ctx:
            tpl.render({"a": "x", "typo": "y"})
        self.assertIn("not in this template: typo", str(ctx.exception))

    def test_render_reports_all_problems_at_once(self):
        tpl = templates.Template(name="t", body="{a} {b} {c}")
        with self.assertRaises(templates.TemplateError) as ctx:
            tpl.render({"a": "x", "wrong": "y"})
        message = str(ctx.exception)
        self.assertIn("b", message)
        self.assertIn("c", message)
        self.assertIn("wrong", message)

    def test_escaped_braces_are_literal(self):
        tpl = templates.Template(name="t", body="{{not a field}} but {real} is")
        self.assertEqual(tpl.fields, ["real"])
        self.assertEqual(tpl.render({"real": "this"}), "{not a field} but this is")

    def test_value_containing_braces_is_not_rescanned(self):
        # A value like "{a}" must land verbatim, not be treated as a placeholder.
        tpl = templates.Template(name="t", body="value: {v}")
        self.assertEqual(tpl.render({"v": "{v}"}), "value: {v}")

    def test_bodyless_template_cannot_render(self):
        tpl = templates.Template(name="t", body="")
        with self.assertRaises(templates.TemplateError) as ctx:
            tpl.render({})
        self.assertIn("no body", str(ctx.exception))

    def test_invalid_name_rejected(self):
        for bad in ("", "Has Space", "-leading", "sla/sh", "dot.name"):
            with self.subTest(name=bad), self.assertRaises(templates.TemplateError):
                templates.Template(name=bad, body="{a}")

    def test_name_is_case_normalised_not_rejected(self):
        # Names map to filenames, so case-folding is friendlier than an error —
        # `ele template show Person` should just work.
        self.assertEqual(templates.Template(name="Person", body="{a}").name, "person")

    def test_unknown_type_rejected(self):
        with self.assertRaises(templates.TemplateError) as ctx:
            templates.Template(name="t", type="nonsense")
        self.assertIn("unknown type", str(ctx.exception))

    def test_unknown_json_key_rejected(self):
        # "tag" instead of "tags" would silently produce untagged memories, and
        # tags are what conflict detection and auto-supersession key on.
        with self.assertRaises(templates.TemplateError) as ctx:
            templates.Template.from_dict("t", {"tag": ["x"]})
        self.assertIn("unknown key", str(ctx.exception))

    def test_string_tags_are_accepted_as_one_tag(self):
        tpl = templates.Template.from_dict("t", {"tags": "solo"})
        self.assertEqual(tpl.tags, ["solo"])


# ---------------------------------------------------------------------------
# Built-ins and vault storage
# ---------------------------------------------------------------------------


class TestBuiltins(unittest.TestCase):
    def test_every_builtin_is_valid(self):
        for name, tpl in templates.builtins().items():
            self.assertEqual(tpl.name, name)
            self.assertTrue(tpl.description)

    def test_every_builtin_body_renders(self):
        # A shipped template whose body can't be filled is a broken default.
        for name, tpl in templates.builtins().items():
            with self.subTest(template=name):
                values = {f: f"<{f}>" for f in tpl.fields}
                rendered = tpl.render(values)
                self.assertNotIn("{", rendered.replace("{{", "").replace("}}", ""))

    def test_every_builtin_sets_at_least_one_tag(self):
        # The tag is what makes a template's captures findable as a group.
        for name, tpl in templates.builtins().items():
            with self.subTest(template=name):
                self.assertTrue(tpl.tags)


class TestVaultStorage(TemplateTestCase):
    def test_builtins_available_without_any_files(self):
        available, errors = templates.load_all(self.vault)
        self.assertEqual(errors, {})
        self.assertEqual(set(available), set(templates.BUILTINS))

    def test_user_template_is_loaded(self):
        self.write_template("standup", {"body": "{done}", "tags": ["standup"]})
        available, _ = templates.load_all(self.vault)
        self.assertIn("standup", available)
        self.assertEqual(available["standup"].fields, ["done"])

    def test_user_template_shadows_builtin(self):
        self.write_template("person", {"body": "{nickname}", "tags": ["contact"]})
        available, _ = templates.load_all(self.vault)
        self.assertEqual(available["person"].fields, ["nickname"])
        self.assertEqual(available["person"].tags, ["contact"])

    def test_bad_file_is_reported_not_swallowed(self):
        path = templates.templates_dir(self.vault)
        path.mkdir(parents=True, exist_ok=True)
        (path / "broken.json").write_text("{not json", encoding="utf-8")
        available, errors = templates.load_all(self.vault)
        self.assertIn("broken.json", errors)
        # One bad file must not take the others down with it.
        self.assertEqual(set(available), set(templates.BUILTINS))

    def test_invalid_template_file_is_reported(self):
        self.write_template("bad", {"type": "nonsense"})
        _, errors = templates.load_all(self.vault)
        self.assertIn("bad.json", errors)

    def test_get_unknown_name_lists_alternatives(self):
        with self.assertRaises(templates.TemplateError) as ctx:
            templates.get(self.vault, "nope")
        self.assertIn("Available:", str(ctx.exception))

    def test_save_then_get_round_trip(self):
        tpl = templates.Template(name="mine", body="{x}", tags=["a"], type="fact")
        templates.save(self.vault, tpl)
        loaded = templates.get(self.vault, "mine")
        self.assertEqual(loaded.body, "{x}")
        self.assertEqual(loaded.type, "fact")
        self.assertEqual(loaded.tags, ["a"])

    def test_remove_returns_false_when_absent(self):
        self.assertFalse(templates.remove(self.vault, "ghost"))

    def test_removing_shadow_restores_builtin(self):
        self.write_template("person", {"body": "{nickname}"})
        self.assertEqual(templates.get(self.vault, "person").fields, ["nickname"])
        self.assertTrue(templates.remove(self.vault, "person"))
        self.assertEqual(
            templates.get(self.vault, "person").fields,
            templates.builtins()["person"].fields,
        )

    def test_user_defined_excludes_builtins(self):
        self.write_template("mine", {"body": "{x}"})
        self.assertEqual(set(templates.user_defined(self.vault)), {"mine"})


class TestApplyDefaults(unittest.TestCase):
    def setUp(self):
        self.tpl = templates.Template(
            name="t", type="fact", tags=["base"], scopes=["work"], source="tpl"
        )

    def test_template_supplies_defaults(self):
        got = templates.apply_defaults(self.tpl)
        self.assertEqual(got["type"], "fact")
        self.assertEqual(got["tags"], ["base"])
        self.assertEqual(got["scopes"], ["work"])
        self.assertEqual(got["source"], "tpl")

    def test_explicit_values_win(self):
        got = templates.apply_defaults(self.tpl, type="note", scopes=["legal"], source="claude")
        self.assertEqual(got["type"], "note")
        self.assertEqual(got["scopes"], ["legal"])
        self.assertEqual(got["source"], "claude")

    def test_tags_union_rather_than_replace(self):
        # Replacing would drop the tag that makes the template's captures a group.
        got = templates.apply_defaults(self.tpl, tags="extra")
        self.assertEqual(got["tags"], ["base", "extra"])

    def test_tag_union_does_not_duplicate(self):
        got = templates.apply_defaults(self.tpl, tags="base")
        self.assertEqual(got["tags"], ["base"])


# ---------------------------------------------------------------------------
# MCP surface: remember(template=..., fields=...)
# ---------------------------------------------------------------------------


class TestServiceTemplates(TemplateTestCase):
    def setUp(self):
        super().setUp()
        self.store = MemoryStore(self.vault)
        self.svc = MemoryService(self.store)

    def tearDown(self):
        self.store.close()
        super().tearDown()

    def test_fields_build_the_content(self):
        out = self.svc.remember(
            "", template="person", fields='{"name": "Ana", "role": "PM", "detail": "platform"}'
        )
        self.assertTrue(out.startswith("Saved"))
        saved = self.store.all()[0]
        self.assertIn("Ana", saved.content)
        self.assertEqual(saved.type, "relationship")  # template default applied
        self.assertIn("person", saved.tags)

    def test_missing_field_refuses_the_save(self):
        out = self.svc.remember("", template="person", fields='{"name": "Ana"}')
        self.assertIn("missing", out)
        self.assertEqual(self.store.count(), 0)  # nothing written

    def test_bad_json_is_explained(self):
        out = self.svc.remember("", template="person", fields="not json")
        self.assertIn("JSON object", out)
        self.assertEqual(self.store.count(), 0)

    def test_json_array_is_rejected(self):
        out = self.svc.remember("", template="person", fields='["Ana"]')
        self.assertIn("JSON object", out)

    def test_content_and_fields_together_are_refused(self):
        out = self.svc.remember("some text", template="person", fields='{"name": "Ana"}')
        self.assertIn("one or the other", out)
        self.assertEqual(self.store.count(), 0)

    def test_fields_without_template_is_refused(self):
        out = self.svc.remember("text", fields='{"a": "b"}')
        self.assertIn("needs a `template`", out)

    def test_template_without_fields_applies_defaults_only(self):
        out = self.svc.remember("Ana runs platform", template="person")
        self.assertTrue(out.startswith("Saved"))
        saved = self.store.all()[0]
        self.assertEqual(saved.content, "Ana runs platform")  # content untouched
        self.assertEqual(saved.type, "relationship")
        self.assertIn("person", saved.tags)

    def test_explicit_type_beats_template(self):
        self.svc.remember("Ana runs platform", template="person", type="fact")
        self.assertEqual(self.store.all()[0].type, "fact")

    def test_unknown_template_is_named(self):
        out = self.svc.remember("x", template="ghost")
        self.assertIn("No template named", out)
        self.assertEqual(self.store.count(), 0)

    def test_empty_content_without_template_still_refused(self):
        self.assertIn("empty", self.svc.remember(""))

    def test_templates_digest_lists_fields_and_body(self):
        out = self.svc.templates()
        self.assertIn("## person", out)
        self.assertIn("fields: name, role, detail", out)
        self.assertIn("type=relationship", out)

    def test_templates_digest_surfaces_unreadable_files(self):
        directory = templates.templates_dir(self.vault)
        directory.mkdir(parents=True, exist_ok=True)
        (directory / "broken.json").write_text("{oops", encoding="utf-8")
        self.assertIn("Unreadable template file(s)", self.svc.templates())


# ---------------------------------------------------------------------------
# CLI surface
# ---------------------------------------------------------------------------


class TestTemplateCli(TemplateTestCase):
    def setUp(self):
        super().setUp()
        # The CLI ships English-only strings, so no language pinning is
        # needed; kept as a seam in case localization is reintroduced.
        i18n.set_language("en")

    def run_cli(self, *argv) -> tuple[int, str, str]:
        from elesync import cli

        out, err = io.StringIO(), io.StringIO()
        try:
            with redirect_stdout(out), redirect_stderr(err):
                code = cli.main(["--vault", str(self.vault), *argv])
        except SystemExit as e:  # argparse errors
            code = int(e.code or 0)
        return code, out.getvalue(), err.getvalue()

    def test_list_shows_builtins(self):
        code, out, _ = self.run_cli("template", "list")
        self.assertEqual(code, 0)
        for name in templates.BUILTINS:
            self.assertIn(name, out)

    def test_list_exits_nonzero_when_a_file_is_unreadable(self):
        directory = templates.templates_dir(self.vault)
        directory.mkdir(parents=True, exist_ok=True)
        (directory / "broken.json").write_text("{oops", encoding="utf-8")
        code, _, err = self.run_cli("template", "list")
        self.assertEqual(code, 1)
        self.assertIn("broken.json", err)

    def test_show_prints_body(self):
        code, out, _ = self.run_cli("template", "show", "meeting")
        self.assertEqual(code, 0)
        self.assertIn("{who}", out)

    def test_show_unknown_fails_cleanly(self):
        code, _, err = self.run_cli("template", "show", "ghost")
        self.assertEqual(code, 1)
        self.assertIn("No template named", err)

    def test_new_from_builtin_then_list(self):
        code, _, _ = self.run_cli(
            "template", "new", "standup", "--from", "meeting", "--body", "{done}"
        )
        self.assertEqual(code, 0)
        self.assertEqual(templates.get(self.vault, "standup").fields, ["done"])

    def test_new_rejects_bad_type(self):
        code, _, err = self.run_cli("template", "new", "x", "--type", "nonsense")
        self.assertEqual(code, 1)
        self.assertIn("unknown type", err)

    def test_remove_reports_missing_file(self):
        code, _, err = self.run_cli("template", "remove", "ghost")
        self.assertEqual(code, 1)
        self.assertIn("ghost", err)

    def test_add_with_template_renders(self):
        code, out, err = self.run_cli(
            "add",
            "--template",
            "person",
            "--set",
            "name=Ana",
            "--set",
            "role=PM",
            "--set",
            "detail=platform",
        )
        self.assertEqual(code, 0, err)
        store = MemoryStore(self.vault)
        try:
            saved = store.all()[0]
            self.assertIn("Ana", saved.content)
            self.assertEqual(saved.type, "relationship")
        finally:
            store.close()

    def test_add_with_template_rejects_missing_set(self):
        code, _, err = self.run_cli("add", "--template", "person", "--set", "name=Ana")
        self.assertEqual(code, 1)
        self.assertIn("missing", err)

    def test_add_with_template_rejects_misspelled_set(self):
        code, _, err = self.run_cli(
            "add",
            "--template",
            "person",
            "--set",
            "name=Ana",
            "--set",
            "role=PM",
            "--set",
            "detial=typo",
        )
        self.assertEqual(code, 1)
        self.assertIn("detial", err)

    def test_add_rejects_set_without_equals(self):
        code, _, err = self.run_cli("add", "--template", "person", "--set", "name")
        self.assertEqual(code, 1)
        self.assertIn("key=value", err)

    def test_add_rejects_content_plus_body_template(self):
        code, _, err = self.run_cli("add", "text here", "--template", "person", "--set", "name=Ana")
        self.assertEqual(code, 1)
        self.assertIn("supplies its own content", err)

    def test_add_without_content_or_template_is_refused(self):
        code, _, err = self.run_cli("add")
        self.assertEqual(code, 1)
        self.assertIn("Nothing to save", err)

    def test_plain_add_still_works(self):
        # The positional argument became optional; the original form must not regress.
        code, _, err = self.run_cli("add", "a plain memory")
        self.assertEqual(code, 0, err)


if __name__ == "__main__":
    unittest.main()
