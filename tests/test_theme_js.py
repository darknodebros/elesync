"""The theme script runs before first paint, so its default and its stored choice are checked in Node."""

from __future__ import annotations

import json
import shutil
import subprocess
import unittest
from pathlib import Path

THEME = Path(__file__).resolve().parents[1] / "elesync" / "static" / "dashboard-theme.js"

# Loads the real script against a stub <html data-theme="cosmic"> and a given localStorage, then reports
# the theme it picked, what is stored, and the theme after one toggle.
SCRIPT = r"""
const fs = require('fs');
const store = JSON.parse(process.argv[2]);
globalThis.localStorage = {
  getItem: k => (k in store ? store[k] : null),
  setItem: (k, v) => { store[k] = String(v); },
  removeItem: k => { delete store[k]; },
};
const html = { dataset: { theme: 'cosmic' } };
globalThis.document = {
  documentElement: html,
  querySelector: () => null,
  querySelectorAll: () => [],
  getElementById: () => null,
  addEventListener: () => {},
};
eval(fs.readFileSync(process.argv[1], 'utf8') + ';globalThis.toggleTheme = toggleTheme;');
const atLoad = html.dataset.theme || 'classic';
const storedAtLoad = { ...store };
toggleTheme();
console.log(JSON.stringify({ atLoad, storedAtLoad, afterToggle: html.dataset.theme || 'classic', stored: store }));
"""


def run_theme(storage: dict) -> dict:
    done = subprocess.run(
        ["node", "-e", SCRIPT, str(THEME), json.dumps(storage)],
        capture_output=True,
        text=True,
        timeout=60,
    )
    if done.returncode:
        raise AssertionError(done.stderr)
    return json.loads(done.stdout)


@unittest.skipUnless(shutil.which("node"), "node is not installed")
class TestThemeDefault(unittest.TestCase):
    def test_new_visitor_gets_cosmic_and_nothing_is_stored(self):
        result = run_theme({})
        self.assertEqual(result["atLoad"], "cosmic")
        self.assertEqual(result["storedAtLoad"], {})

    def test_old_auto_saved_classic_does_not_override_the_new_default(self):
        # Up to 1.24.70 every page load wrote the current theme to 'elesync-theme'.
        result = run_theme({"elesync-theme": "classic"})
        self.assertEqual(result["atLoad"], "cosmic")
        self.assertNotIn("elesync-theme", result["storedAtLoad"])

    def test_explicit_classic_choice_is_kept(self):
        result = run_theme({"elesync-theme-choice": "classic"})
        self.assertEqual(result["atLoad"], "classic")
        self.assertEqual(result["afterToggle"], "cosmic")
        self.assertEqual(result["stored"]["elesync-theme-choice"], "cosmic")

    def test_toggling_from_the_default_stores_classic(self):
        result = run_theme({})
        self.assertEqual(result["afterToggle"], "classic")
        self.assertEqual(result["stored"], {"elesync-theme-choice": "classic"})


if __name__ == "__main__":
    unittest.main()
