"""WhatsApp review keeps only the user's own messages by default (other people's are their personal data)."""

from __future__ import annotations

import json
import re
import shutil
import subprocess
import unittest
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
IMPORT_JS = (ROOT / "elesync" / "static" / "dashboard-import.js").read_text(encoding="utf-8")
TEMPLATES = (ROOT / "elesync" / "_web_templates.py").read_text(encoding="utf-8")


def _extract(js: str, name: str) -> str:
    """Pull one top-level function's source out of the script, verbatim."""
    m = re.search(r"(?:async\s+)?function\s+" + re.escape(name) + r"\s*\([^)]*\)\s*\{", js)
    if not m:
        raise AssertionError(f"function {name}() not found in dashboard-import.js")
    depth = 0
    for j in range(m.end() - 1, len(js)):
        depth += {"{": 1, "}": -1}.get(js[j], 0)
        if depth == 0:
            return js[m.start() : j + 1]
    raise AssertionError(f"unbalanced braces in {name}()")


@unittest.skipUnless(shutil.which("node"), "node is not installed")
class TestGuessWhatsAppSelf(unittest.TestCase):
    CASES = [
        # (senders, filename, remembered, expected)
        (["Ana", "Berto"], "WhatsApp Chat with Ana.txt", "", "Berto"),
        (["Ana", "Berto"], "Chat de WhatsApp con Ana.zip", "", "Berto"),
        (["Ana", "Berto"], "WhatsApp Chat - ana", "", "Berto"),
        (["Berto"], "export.txt", "", "Berto"),
        (["Ana", "Berto", "Carla"], "WhatsApp Chat with Family.txt", "", ""),
        (["Ana", "Berto", "Carla"], "WhatsApp Chat with Family.txt", "Carla", "Carla"),
        (["Ana", "Berto"], "chat.txt", "", ""),
        (["Ana", "Berto"], "WhatsApp Chat with Someone Else.txt", "", ""),
        (["Ana", "Berto"], "chat.txt", "Old Name", ""),
    ]

    def test_guesses_the_exporter_or_admits_it_cannot(self):
        script = _extract(IMPORT_JS, "guessWhatsAppSelf") + (
            ";const cases = JSON.parse(process.argv[1]);"
            "console.log(JSON.stringify(cases.map(c => guessWhatsAppSelf(c[0], c[1], c[2]))));"
        )
        done = subprocess.run(
            ["node", "-e", script, json.dumps(self.CASES)],
            capture_output=True,
            text=True,
            timeout=60,
        )
        self.assertEqual(done.returncode, 0, done.stderr)
        self.assertEqual(json.loads(done.stdout), [case[3] for case in self.CASES])


class TestOwnMessagesByDefault(unittest.TestCase):
    def test_others_need_consent_and_unknown_self_must_be_chosen(self):
        approve = _extract(IMPORT_JS, "approveWhatsApp")
        self.assertIn("confirmDialog(WHATSAPP_CONSENT)", approve)
        self.assertIn("Choose which sender is you", approve)
        review = _extract(IMPORT_JS, "reviewWhatsApp")
        self.assertIn("senderSelect.value = self || WHATSAPP_CHOOSE", review)
        # Everyone is still possible, but it is no longer the first, default option.
        self.assertNotIn("'<option value=\"\">Everyone</option>' +", review)
        self.assertIn('for="whatsapp-sender-filter">Which sender is you?', TEMPLATES)


if __name__ == "__main__":
    unittest.main()
