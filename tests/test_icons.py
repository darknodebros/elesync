"""Dashboard icon sprite regression tests."""

from __future__ import annotations

import re
import unittest
from pathlib import Path

from elesync._icons import ICON_NAMES, ICON_SPRITE
from elesync._web_templates import _HTML

ROOT = Path(__file__).resolve().parent.parent
STATIC = ROOT / "elesync" / "static"


def _used_icon_names() -> set[str]:
    """Every icon the dashboard references: #ph-NAME in markup, icon('NAME') in JS."""
    used = set(re.findall(r'href="#ph-([a-z0-9-]+)"', _HTML))
    for js in STATIC.glob("dashboard*.js"):
        text = js.read_text(encoding="utf-8")
        # Quoted names inside icon(...) calls, including ternaries like icon(ok ? 'check' : 'plus').
        for args in re.findall(r"\bicon\(([^()]*)\)", text):
            used |= set(re.findall(r"'([a-z0-9-]+)'", args))
        icon_map = re.search(r"const icons = \{([^}]*)\}", text)
        if icon_map:
            used |= set(re.findall(r":'([a-z0-9-]+)'", icon_map.group(1)))
    return used


class TestIconSprite(unittest.TestCase):
    def test_sprite_is_injected_into_dashboard(self):
        self.assertNotIn("<!--ICON_SPRITE-->", _HTML)
        self.assertIn(ICON_SPRITE, _HTML)

    def test_sprite_defines_every_listed_icon(self):
        for name in ICON_NAMES:
            self.assertIn(f'<symbol id="ph-{name}"', ICON_SPRITE)

    def test_every_referenced_icon_exists(self):
        used = _used_icon_names()
        self.assertGreater(len(used), 20)
        self.assertEqual(sorted(used - set(ICON_NAMES)), [])

    def test_no_emoji_icons_left_in_dashboard(self):
        emoji = re.compile("[\U0001f300-\U0001faff☀-➿]")
        sources = {"_web_templates.py": _HTML}
        for js in STATIC.glob("dashboard*.js"):
            sources[js.name] = js.read_text(encoding="utf-8")
        for name, text in sources.items():
            code = "\n".join(
                line
                for line in text.splitlines()
                if not line.lstrip().startswith(("//", "/*", "*"))
            )
            self.assertEqual(emoji.findall(code), [], name)


if __name__ == "__main__":
    unittest.main()
