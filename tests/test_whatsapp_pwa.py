"""WhatsApp import and PWA v1 regression tests."""

from __future__ import annotations

import json
import tempfile
import unittest
import zipfile
from pathlib import Path

from elesync.normalize import (
    from_grok,
    from_telegram,
    from_whatsapp,
    normalize_file,
    parse_whatsapp_messages,
    preview_csv,
    whatsapp_messages_to_item,
)

ROOT = Path(__file__).resolve().parent.parent


class TestWhatsAppImport(unittest.TestCase):
    SAMPLE = (
        "12/31/23, 10:15 PM - Alex: We should book the hotel.\n"
        "The refundable option looks best.\n"
        "01/01/24, 9:00 AM - Sam: Agreed, I will handle it.\n"
    )

    def test_parses_export_as_one_conversation(self):
        items = from_whatsapp(self.SAMPLE, source_ref="WhatsApp Chat with Family.txt")
        self.assertEqual(len(items), 1)
        item = items[0]
        self.assertEqual(item.source, "whatsapp")
        self.assertEqual(item.type, "conversation")
        self.assertEqual(item.source_ref, "WhatsApp Chat with Family.txt")
        self.assertIn("Alex: We should book the hotel.", item.content)
        self.assertIn("The refundable option looks best.", item.content)
        self.assertTrue(item.created_at.startswith("2024-01-01"))

    def test_large_export_preserves_every_message_in_order(self):
        transcript = "\n".join(
            f"12/31/23, 10:15 PM - Alex: Message {i}\nContinuation {i}" for i in range(2149)
        )
        items = from_whatsapp(transcript, source_ref="large.txt")
        self.assertEqual(len(items), 22)
        restored = [m for item in items for m in parse_whatsapp_messages(item.content)]
        self.assertEqual(restored, parse_whatsapp_messages(transcript))
        self.assertTrue(all(item.source_ref == "large.txt" for item in items))
        self.assertTrue(all(item.type == "conversation" for item in items))
        with tempfile.TemporaryDirectory() as directory:
            path = Path(directory) / "WhatsApp.txt"
            path.write_text(transcript, encoding="utf-8")
            self.assertEqual(len(normalize_file(path)), 22)

    def test_long_messages_are_bounded_without_splitting_multiline_text(self):
        transcript = "\n".join(
            f"12/31/23, 10:15 PM - Alex: {i}:" + "x" * 9000 + "\ncontinuation" for i in range(3)
        )
        items = from_whatsapp(transcript)
        self.assertEqual(len(items), 3)
        self.assertTrue(all(len(item.content) < 16_100 for item in items))
        self.assertEqual(
            [m for item in items for m in parse_whatsapp_messages(item.content)],
            parse_whatsapp_messages(transcript),
        )

    def test_parse_messages_supports_privacy_review(self):
        messages = parse_whatsapp_messages(self.SAMPLE)
        self.assertEqual(len(messages), 2)
        self.assertEqual(messages[0]["sender"], "Alex")
        self.assertIn("refundable", messages[0]["text"])
        self.assertEqual(messages[1]["sender"], "Sam")

    def test_privacy_filters_remove_phone_links_and_media(self):
        messages = parse_whatsapp_messages(
            "12/31/23, 10:15 PM - Alex: Call +1 (305) 555-1212 https://example.com\n"
            "12/31/23, 10:16 PM - Alex: <Media omitted>\n"
        )
        item = whatsapp_messages_to_item(
            messages,
            redact_phone_numbers=True,
            strip_links=True,
            strip_media=True,
        )
        self.assertIsNotNone(item)
        assert item is not None
        self.assertNotIn("555-1212", item.content)
        self.assertNotIn("https://example.com", item.content)
        self.assertNotIn("Media omitted", item.content)

    def test_deleted_message_stand_ins_are_not_imported(self):
        # WhatsApp writes these in place of a deleted message; they were saved
        # as if someone had said them.
        messages = parse_whatsapp_messages(
            "12/31/23, 10:15 PM - Alex: We should book the hotel.\n"
            "12/31/23, 10:16 PM - Alex: You deleted this message\n"
            "[7/12/26, 7:06:15 AM] Sam: ‎This message was deleted.\n"
            "12/31/23, 10:18 PM - Ana: Se eliminó este mensaje\n"
            "12/31/23, 10:19 PM - Sam: Booked it <This message was edited>\n"
        )
        self.assertEqual(
            [(m["sender"], m["text"]) for m in messages],
            [("Alex", "We should book the hotel."), ("Sam", "Booked it")],
        )

    def test_filename_detects_whatsapp_text_export(self):
        with tempfile.TemporaryDirectory() as directory:
            path = Path(directory) / "WhatsApp Chat with Family.txt"
            path.write_text(self.SAMPLE, encoding="utf-8")
            items = normalize_file(path)
        self.assertEqual(len(items), 1)
        self.assertEqual(items[0].source, "whatsapp")
        self.assertEqual(items[0].type, "conversation")

    def test_non_whatsapp_plaintext_stays_plaintext(self):
        with tempfile.TemporaryDirectory() as directory:
            path = Path(directory) / "notes.txt"
            path.write_text("one note\nsecond note", encoding="utf-8")
            items = normalize_file(path)
        self.assertEqual([item.source for item in items], ["manual", "manual"])

    def test_empty_and_media_only_exports_are_safe(self):
        self.assertEqual(from_whatsapp(""), [])
        items = from_whatsapp("12/31/23, 10:15 PM - Alex: <Media omitted>\n")
        self.assertEqual(len(items), 1)
        self.assertIn("<Media omitted>", items[0].content)


class TestTelegramImport(unittest.TestCase):
    SAMPLE = {
        "name": "Project planning",
        "type": "private_group",
        "id": 42,
        "messages": [
            {
                "id": 1,
                "type": "message",
                "date": "2026-08-21T10:00:00",
                "from": "Alex",
                "text": "Remember the launch checklist.",
            },
            {
                "id": 2,
                "type": "message",
                "date": "2026-08-21T10:01:00",
                "from": "Sam",
                "text": [{"type": "plain", "text": "I will add the final notes."}],
            },
            {"id": 3, "type": "service", "date": "2026-08-21T10:02:00", "text": "joined"},
        ],
    }

    def test_imports_telegram_desktop_json(self):
        items = from_telegram(self.SAMPLE, source_ref="result.json")
        self.assertEqual(len(items), 1)
        self.assertEqual(items[0].source, "telegram")
        self.assertEqual(items[0].type, "conversation")
        self.assertIn("Alex: Remember the launch checklist.", items[0].content)
        self.assertIn("Sam: I will add the final notes.", items[0].content)

    def test_detects_generic_telegram_result_filename(self):
        with tempfile.TemporaryDirectory() as directory:
            path = Path(directory) / "result.json"
            path.write_text(json.dumps(self.SAMPLE), encoding="utf-8")
            items = normalize_file(path)
        self.assertEqual(len(items), 1)
        self.assertEqual(items[0].source, "telegram")


class TestDocumentImports(unittest.TestCase):
    def test_markdown_is_imported_as_one_knowledge_note(self):
        with tempfile.TemporaryDirectory() as directory:
            path = Path(directory) / "car-payment.md"
            path.write_text("# Car payment\\n\\nKeep the emergency fund intact.", encoding="utf-8")
            items = normalize_file(path)
        self.assertEqual(len(items), 1)
        self.assertEqual(items[0].type, "note")
        self.assertIn("Car payment", items[0].content)

    def test_zip_imports_supported_members_and_skips_other_files(self):
        with tempfile.TemporaryDirectory() as directory:
            path = Path(directory) / "export.zip"
            with zipfile.ZipFile(path, "w") as archive:
                archive.writestr("memories.md", "# Imported knowledge")
                archive.writestr("prod-grok-backend.json", json.dumps({"memories": ["Grok fact"]}))
                archive.writestr("account.csv", "not,an,import")
            items = normalize_file(path)
        self.assertEqual(len(items), 2)
        self.assertTrue(all(item.source_ref.startswith("export.zip/") for item in items))

    def test_zip_path_traversal_member_is_ignored(self):
        with tempfile.TemporaryDirectory() as directory:
            path = Path(directory) / "unsafe.zip"
            with zipfile.ZipFile(path, "w") as archive:
                archive.writestr("../outside.md", "do not import")
            self.assertEqual(normalize_file(path), [])


class TestGrokImport(unittest.TestCase):
    def test_prod_grok_backend_conversation_shape_is_accepted(self):
        data = {
            "conversations": [
                {
                    "conversation": {"title": "Grok planning"},
                    "responses": [
                        {"response": {"sender": "human", "message": "Plan this."}},
                        {"response": {"sender": "assistant", "message": "Here is a plan."}},
                    ],
                }
            ]
        }
        items = from_grok(data, source_ref="prod-grok-backend.json")
        self.assertEqual(len(items), 1)
        self.assertEqual(items[0].source, "grok")
        self.assertEqual(items[0].type, "conversation")
        self.assertIn("# Grok planning", items[0].content)
        self.assertIn("human: Plan this.", items[0].content)


class TestPwaAssets(unittest.TestCase):
    def test_share_target_multipart_parser_keeps_text_and_files(self):
        from elesync.web import _parse_share_target

        boundary = "----elesync-test"
        body = (
            f'--{boundary}\r\nContent-Disposition: form-data; name="text"\r\n\r\nShared note\r\n'
            f'--{boundary}\r\nContent-Disposition: form-data; name="files"; filename="notes.txt"\r\n'
            "Content-Type: text/plain\r\n\r\nFile contents\r\n"
            f"--{boundary}--\r\n"
        ).encode()
        payload = _parse_share_target(f"multipart/form-data; boundary={boundary}", body)

        self.assertEqual(payload["text"], "Shared note")
        self.assertEqual(payload["files"][0]["name"], "notes.txt")
        self.assertEqual(payload["files"][0]["data"], b"File contents")

    def test_manifest_is_installable(self):
        manifest = json.loads(
            (ROOT / "elesync" / "static" / "manifest.json").read_text(encoding="utf-8")
        )
        self.assertEqual(manifest["display"], "standalone")
        self.assertEqual(manifest["scope"], "/")
        self.assertEqual(manifest["icons"][0]["src"], "/static/elesync-main-logo-cosmic.png")
        share_target = manifest["share_target"]
        self.assertEqual(share_target["action"], "/share-target")
        self.assertEqual(share_target["method"], "POST")
        self.assertEqual(share_target["enctype"], "multipart/form-data")
        self.assertEqual(share_target["params"]["files"][0]["name"], "files")
        self.assertIn("*/*", share_target["params"]["files"][0]["accept"])

    def test_service_worker_caches_shell_and_skips_api(self):
        worker = (ROOT / "elesync" / "static" / "service-worker.js").read_text(encoding="utf-8")
        command_center = (ROOT / "elesync" / "static" / "dashboard-command-center.css").read_text(
            encoding="utf-8"
        )
        template = (ROOT / "elesync" / "_web_templates.py").read_text(encoding="utf-8")
        self.assertIn("elesync-pwa-v70", worker)
        self.assertIn("/static/elesync-main-logo-cosmic.webp", worker)
        self.assertIn("/static/elesync-favicon-cosmic.png", worker)
        self.assertIn("/static/dashboard-api.js", worker)
        self.assertIn("/static/dashboard-command-center.css", worker)
        self.assertIn("/static/dashboard-onboarding.js", worker)
        self.assertIn("/static/dashboard-projects.js", worker)
        self.assertIn("/static/dashboard-connect.js", worker)
        self.assertIn("/static/dashboard-import.js", worker)
        self.assertIn("/static/dashboard-pwa.js", worker)
        self.assertIn("/static/dashboard.js", worker)
        self.assertIn("/static/elesync-main-logo.webp", worker)
        self.assertIn("mix-blend-mode:screen", command_center)
        self.assertIn("animation:cc-elephant-float 4.8s", command_center)
        self.assertNotIn("elesync-elephant-mark.png", worker)
        self.assertNotIn('class="companion-bar-logo"', template)
        self.assertIn("/api/", worker)
        dashboard = "\n".join(
            (ROOT / "elesync" / "static" / name).read_text(encoding="utf-8")
            for name in ("dashboard-import.js", "dashboard.js")
        )
        self.assertIn("OFFLINE_DRAFTS_KEY", dashboard)
        self.assertIn("AES-GCM", dashboard)
        self.assertIn("clearOfflineDrafts", dashboard)
        self.assertIn("handleShareTarget", dashboard)
        self.assertIn("/api/share-target", dashboard)
        self.assertIn("Retry", dashboard)
        pwa = (ROOT / "elesync" / "static" / "dashboard-pwa.js").read_text(encoding="utf-8")
        self.assertIn("beforeinstallprompt", pwa)
        self.assertIn("appinstalled", pwa)

    def test_copilot_csv_preview_detects_supported_headers(self):
        preview = preview_csv("Conversation,Time,Author,Message\nTrip,2026-01-01,User,Hello\n")
        self.assertTrue(preview["supported"])
        self.assertEqual(preview["format"], "copilot")
        self.assertEqual(preview["columns"], ["Conversation", "Time", "Author", "Message"])
        self.assertEqual(preview["sample"][0]["Message"], "Hello")
