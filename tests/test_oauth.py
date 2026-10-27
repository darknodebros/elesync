import base64
import hashlib
import tempfile
import threading
import unittest
from concurrent.futures import ThreadPoolExecutor
from pathlib import Path

from elesync import oauth


class TestOAuthFoundation(unittest.TestCase):
    def test_pkce_code_exchange_and_revoke(self):
        with tempfile.TemporaryDirectory() as directory:
            vault = Path(directory)
            client = oauth.register_client(vault, "Grok", ["https://client.example/callback"])
            verifier = "test-verifier-with-enough-entropy"
            challenge = (
                base64.urlsafe_b64encode(hashlib.sha256(verifier.encode("ascii")).digest())
                .rstrip(b"=")
                .decode("ascii")
            )
            code = oauth.create_code(
                vault,
                client["client_id"],
                "https://client.example/callback",
                challenge,
                "project-123",
                {"can_read": True, "can_write": False},
            )
            token = oauth.exchange_code(
                vault,
                code,
                client["client_id"],
                "https://client.example/callback",
                verifier,
            )
            access = oauth.validate_token(vault, token["access_token"])
            self.assertEqual(access["project_id"], "project-123")
            self.assertFalse(access["permissions"]["can_write"])
            self.assertIn("refresh_token", token)
            self.assertTrue(oauth.revoke_token(vault, token["access_token"]))
            self.assertIsNone(oauth.validate_token(vault, token["access_token"]))
            with self.assertRaisesRegex(ValueError, "refresh token"):
                oauth.refresh_access_token(vault, token["refresh_token"], client["client_id"])

    def test_authorized_providers_follow_live_tokens(self):
        with tempfile.TemporaryDirectory() as directory:
            vault = Path(directory)
            self.assertEqual(oauth.authorized_providers(vault), set())
            client = oauth.register_client(vault, "ChatGPT", ["https://client.example/callback"])
            verifier = "test-verifier-with-enough-entropy"
            challenge = (
                base64.urlsafe_b64encode(hashlib.sha256(verifier.encode("ascii")).digest())
                .rstrip(b"=")
                .decode("ascii")
            )
            code = oauth.create_code(
                vault,
                client["client_id"],
                "https://client.example/callback",
                challenge,
                "project-123",
                {"can_read": True},
                provider="ChatGPT",
            )
            oauth.exchange_code(
                vault, code, client["client_id"], "https://client.example/callback", verifier
            )
            self.assertEqual(oauth.authorized_providers(vault), {"chatgpt"})
            oauth.revoke_project_authorizations(vault, "project-123", "chatgpt")
            self.assertEqual(oauth.authorized_providers(vault), set())

    def test_wrong_pkce_verifier_cannot_exchange_code(self):
        with tempfile.TemporaryDirectory() as directory:
            vault = Path(directory)
            client = oauth.register_client(vault, "Grok", ["https://client.example/callback"])
            verifier = "correct-verifier"
            challenge = (
                base64.urlsafe_b64encode(hashlib.sha256(verifier.encode("ascii")).digest())
                .rstrip(b"=")
                .decode("ascii")
            )
            code = oauth.create_code(
                vault,
                client["client_id"],
                "https://client.example/callback",
                challenge,
                "project-123",
                {"can_read": True},
            )
            with self.assertRaisesRegex(ValueError, "PKCE"):
                oauth.exchange_code(
                    vault,
                    code,
                    client["client_id"],
                    "https://client.example/callback",
                    "wrong-verifier",
                )

    def test_authorization_code_is_single_use_under_concurrency(self):
        with tempfile.TemporaryDirectory() as directory:
            vault = Path(directory)
            client = oauth.register_client(vault, "Concurrent", ["https://client.example/cb"])
            verifier = "concurrent-verifier"
            challenge = (
                base64.urlsafe_b64encode(hashlib.sha256(verifier.encode("ascii")).digest())
                .rstrip(b"=")
                .decode("ascii")
            )
            code = oauth.create_code(
                vault,
                client["client_id"],
                "https://client.example/cb",
                challenge,
                "project-123",
                {"can_read": True},
            )
            barrier = threading.Barrier(2)

            def exchange():
                barrier.wait()
                try:
                    oauth.exchange_code(
                        vault,
                        code,
                        client["client_id"],
                        "https://client.example/cb",
                        verifier,
                    )
                    return True
                except ValueError:
                    return False

            with ThreadPoolExecutor(max_workers=2) as pool:
                results = list(pool.map(lambda _index: exchange(), range(2)))
            self.assertEqual(sorted(results), [False, True])

    def test_refresh_token_rotates_and_keeps_connection_active(self):
        with tempfile.TemporaryDirectory() as directory:
            vault = Path(directory)
            client = oauth.register_client(vault, "Durable", ["https://client.example/cb"])
            verifier = "refresh-verifier"
            challenge = (
                base64.urlsafe_b64encode(hashlib.sha256(verifier.encode("ascii")).digest())
                .rstrip(b"=")
                .decode("ascii")
            )
            code = oauth.create_code(
                vault,
                client["client_id"],
                "https://client.example/cb",
                challenge,
                "project-123",
                {"can_read": True},
                provider="chatgpt",
            )
            issued = oauth.exchange_code(
                vault, code, client["client_id"], "https://client.example/cb", verifier
            )
            refreshed = oauth.refresh_access_token(
                vault, issued["refresh_token"], client["client_id"]
            )

            self.assertNotEqual(refreshed["access_token"], issued["access_token"])
            self.assertEqual(
                oauth.validate_token(vault, refreshed["access_token"])["provider"], "chatgpt"
            )
            self.assertTrue(oauth.has_active_authorization(vault))
            with self.assertRaisesRegex(ValueError, "refresh token"):
                oauth.refresh_access_token(vault, issued["refresh_token"], client["client_id"])

    def test_revoke_project_authorizations_invalidates_access_and_refresh_tokens(self):
        with tempfile.TemporaryDirectory() as directory:
            vault = Path(directory)
            client = oauth.register_client(vault, "Claude", ["https://client.example/cb"])
            verifier = "project-revocation-verifier"
            challenge = (
                base64.urlsafe_b64encode(hashlib.sha256(verifier.encode("ascii")).digest())
                .rstrip(b"=")
                .decode("ascii")
            )
            code = oauth.create_code(
                vault,
                client["client_id"],
                "https://client.example/cb",
                challenge,
                "project-123",
                {"can_read": True, "can_write": True},
                provider="claude",
            )
            issued = oauth.exchange_code(
                vault, code, client["client_id"], "https://client.example/cb", verifier
            )

            self.assertEqual(oauth.revoke_project_authorizations(vault, "project-123", "claude"), 2)
            self.assertIsNone(oauth.validate_token(vault, issued["access_token"]))
            with self.assertRaisesRegex(ValueError, "refresh token"):
                oauth.refresh_access_token(vault, issued["refresh_token"], client["client_id"])

    def test_parallel_client_registration_does_not_lose_records(self):
        with tempfile.TemporaryDirectory() as directory:
            vault = Path(directory)
            with ThreadPoolExecutor(max_workers=8) as pool:
                clients = list(
                    pool.map(
                        lambda index: oauth.register_client(
                            vault, f"Client {index}", [f"https://client{index}.example/cb"]
                        ),
                        range(24),
                    )
                )
            stored = oauth._load(vault)
            self.assertEqual(len(stored["clients"]), 24)
            self.assertEqual({item["client_id"] for item in clients}, set(stored["clients"]))


if __name__ == "__main__":
    unittest.main()
