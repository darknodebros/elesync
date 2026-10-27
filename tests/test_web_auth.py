"""Tests for elesync.web_auth — bcrypt user management + session tokens.

python -m unittest tests.test_web_auth -v
"""

import json
import os
import tempfile
import threading
import unittest
from pathlib import Path
from unittest.mock import patch

from elesync.web_auth import SessionManager, UserManager, hash_password, verify_password


def _bcrypt_available() -> bool:
    try:
        import bcrypt  # noqa: F401

        return True
    except ImportError:
        return False


# bcrypt is a base dependency now, but the skip guard keeps tests honest
# if someone installs without deps (e.g. --no-deps). Same convention as
# test_semantic.py (fastembed) and test_remote_transport.py (mcp SDK).
_needs_bcrypt = unittest.skipUnless(_bcrypt_available(), "bcrypt not installed")


@_needs_bcrypt
class TestPasswordHashing(unittest.TestCase):
    def test_hash_and_verify_roundtrip(self):
        hashed = hash_password("correct horse battery staple")
        self.assertTrue(verify_password("correct horse battery staple", hashed))

    def test_wrong_password_fails(self):
        hashed = hash_password("correct horse battery staple")
        self.assertFalse(verify_password("wrong password", hashed))

    def test_empty_password_rejected(self):
        with self.assertRaises(ValueError):
            hash_password("")

    def test_verify_against_malformed_hash_returns_false_not_crash(self):
        # Truncated/corrupt hash (e.g. from a half-written file) should
        # fail verification, not raise.
        self.assertFalse(verify_password("anything", "not-a-real-bcrypt-hash"))

    def test_verify_empty_inputs_returns_false(self):
        self.assertFalse(verify_password("", "somehash"))
        self.assertFalse(verify_password("pw", ""))

    def test_hashes_are_salted_and_distinct(self):
        # Same password, hashed twice, must produce different hashes
        # (bcrypt generates a fresh random salt each call).
        h1 = hash_password("same password")
        h2 = hash_password("same password")
        self.assertNotEqual(h1, h2)
        self.assertTrue(verify_password("same password", h1))
        self.assertTrue(verify_password("same password", h2))


class TestMissingBcryptErrorMessage(unittest.TestCase):
    """Deliberately NOT guarded by _needs_bcrypt: this tests the exact
    situation where bcrypt is absent, so it must run in the extras-free
    CI job too."""

    def test_missing_bcrypt_raises_helpful_error(self):
        # bcrypt is now imported lazily inside _get_bcrypt(), not as a
        # module-level attribute - simulate "not installed" the standard
        # way, by making the import itself fail.
        with patch.dict("sys.modules", {"bcrypt": None}):
            with self.assertRaises(ImportError) as ctx:
                hash_password("whatever12345")
            # EleSync is not on PyPI, so the hint names bcrypt itself.
            self.assertIn('pip install "bcrypt', str(ctx.exception))


@_needs_bcrypt
class TestUserManager(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.mgr = UserManager(self.tmp.name)

    def tearDown(self):
        self.tmp.cleanup()

    def test_add_and_authenticate_user(self):
        self.mgr.add_user("alice", "hunter2pass", role="admin")
        self.assertTrue(self.mgr.authenticate("alice", "hunter2pass"))

    def test_authenticate_wrong_password_fails(self):
        self.mgr.add_user("alice", "hunter2pass")
        self.assertFalse(self.mgr.authenticate("alice", "wrongpass"))

    def test_authenticate_unknown_user_fails_without_crashing(self):
        self.assertFalse(self.mgr.authenticate("ghost", "anything"))

    def test_add_user_empty_username_rejected(self):
        with self.assertRaises(ValueError):
            self.mgr.add_user("", "hunter2pass")

    def test_add_user_empty_password_rejected(self):
        with self.assertRaises(ValueError):
            self.mgr.add_user("alice", "")

    def test_add_user_short_password_rejected(self):
        with self.assertRaises(ValueError):
            self.mgr.add_user("alice", "short")

    def test_add_user_invalid_username_chars_rejected(self):
        with self.assertRaises(ValueError):
            self.mgr.add_user("al ice!", "hunter2pass")

    def test_add_user_invalid_role_rejected(self):
        with self.assertRaises(ValueError):
            self.mgr.add_user("alice", "hunter2pass", role="superuser")

    def test_guest_flag_forces_viewer_role(self):
        self.mgr.add_user("guest1", "hunter2pass", role="admin", guest=True)
        info = self.mgr.get_user("guest1")
        self.assertEqual(info["role"], "viewer")
        self.assertTrue(info["guest"])

    def test_get_user_never_exposes_password_hash(self):
        self.mgr.add_user("alice", "hunter2pass")
        info = self.mgr.get_user("alice")
        self.assertNotIn("password_hash", info)

    def test_get_user_unknown_returns_none(self):
        self.assertIsNone(self.mgr.get_user("ghost"))

    def test_list_users_never_exposes_password_hash(self):
        self.mgr.add_user("alice", "hunter2pass")
        self.mgr.add_user("bob", "anotherpassword", role="viewer")
        users = self.mgr.list_users()
        self.assertEqual(len(users), 2)
        for u in users:
            self.assertNotIn("password_hash", u)

    def test_remove_user(self):
        self.mgr.add_user("alice", "hunter2pass")
        self.mgr.remove_user("alice")
        self.assertIsNone(self.mgr.get_user("alice"))

    def test_remove_unknown_user_raises(self):
        with self.assertRaises(ValueError):
            self.mgr.remove_user("ghost")

    def test_is_admin_true_for_admin(self):
        self.mgr.add_user("alice", "hunter2pass", role="admin")
        self.assertIs(self.mgr.is_admin("alice"), True)

    def test_is_admin_false_for_viewer(self):
        self.mgr.add_user("bob", "hunter2pass", role="viewer")
        self.assertIs(self.mgr.is_admin("bob"), False)

    def test_is_admin_returns_bool_not_none_for_unknown_user(self):
        # Regression test: is_admin used to return None (via `user and ...`
        # short-circuit) instead of False when the user didn't exist.
        result = self.mgr.is_admin("ghost")
        self.assertIs(result, False)

    def test_is_guest_or_viewer_returns_bool_not_none_for_unknown_user(self):
        result = self.mgr.is_guest_or_viewer("ghost")
        self.assertIs(result, False)

    def test_is_guest_or_viewer_true_for_guest(self):
        self.mgr.add_user("guest1", "hunter2pass", guest=True)
        self.assertIs(self.mgr.is_guest_or_viewer("guest1"), True)

    def test_users_persist_across_manager_instances(self):
        self.mgr.add_user("alice", "hunter2pass", role="admin")
        reloaded = UserManager(self.tmp.name)
        self.assertTrue(reloaded.authenticate("alice", "hunter2pass"))

    def test_updating_existing_user_preserves_created_at(self):
        self.mgr.add_user("alice", "hunter2pass", role="admin")
        original_created_at = self.mgr.get_user("alice")["created_at"]
        # Re-add the same username (e.g. changing their password) - the
        # original creation timestamp should not be reset.
        self.mgr.add_user("alice", "differentpass1", role="admin")
        self.assertEqual(self.mgr.get_user("alice")["created_at"], original_created_at)
        self.assertTrue(self.mgr.authenticate("alice", "differentpass1"))
        self.assertFalse(self.mgr.authenticate("alice", "hunter2pass"))

    def test_users_file_is_valid_json_after_save(self):
        self.mgr.add_user("alice", "hunter2pass")
        raw = (Path(self.tmp.name) / "web_users.json").read_text(encoding="utf-8")
        data = json.loads(raw)  # must not raise
        self.assertIn("alice", data["users"])

    def test_concurrent_add_user_does_not_corrupt_file(self):
        errors = []

        def add(i):
            try:
                self.mgr.add_user(f"user{i}", "hunter2pass1")
            except Exception as e:  # pragma: no cover - failure path
                errors.append(e)

        threads = [threading.Thread(target=add, args=(i,)) for i in range(20)]
        for t in threads:
            t.start()
        for t in threads:
            t.join()

        self.assertEqual(errors, [])
        raw = (Path(self.tmp.name) / "web_users.json").read_text(encoding="utf-8")
        data = json.loads(raw)  # file must still be valid JSON, not truncated
        self.assertEqual(len(data["users"]), 20)


class TestSessionManager(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.mgr = SessionManager(self.tmp.name, ttl_minutes=60)

    def tearDown(self):
        self.tmp.cleanup()

    def test_create_and_validate_session(self):
        token = self.mgr.create_session("alice")
        self.assertEqual(self.mgr.validate_session(token), "alice")

    def test_invalid_token_returns_none(self):
        self.assertIsNone(self.mgr.validate_session("not-a-real-token"))

    def test_empty_token_returns_none(self):
        self.assertIsNone(self.mgr.validate_session(""))

    def test_revoked_token_no_longer_validates(self):
        token = self.mgr.create_session("alice")
        self.mgr.revoke_session(token)
        self.assertIsNone(self.mgr.validate_session(token))

    def test_revoking_unknown_token_does_not_raise(self):
        self.mgr.revoke_session("never-issued")  # should be a no-op

    def test_expired_session_returns_none(self):
        short_mgr = SessionManager(self.tmp.name, ttl_minutes=-1)  # already expired
        token = short_mgr.create_session("alice")
        self.assertIsNone(short_mgr.validate_session(token))

    def test_raw_token_never_stored_on_disk(self):
        token = self.mgr.create_session("alice")
        raw_file_content = (Path(self.tmp.name) / "web_sessions.json").read_text(encoding="utf-8")
        self.assertNotIn(token, raw_file_content)

    def test_sessions_file_is_valid_json_after_save(self):
        self.mgr.create_session("alice")
        raw = (Path(self.tmp.name) / "web_sessions.json").read_text(encoding="utf-8")
        json.loads(raw)  # must not raise

    def test_sessions_persist_across_manager_instances(self):
        token = self.mgr.create_session("alice")
        reloaded = SessionManager(self.tmp.name, ttl_minutes=60)
        self.assertEqual(reloaded.validate_session(token), "alice")

    def test_expired_sessions_cleaned_up_on_load(self):
        short_mgr = SessionManager(self.tmp.name, ttl_minutes=-1)
        short_mgr.create_session("alice")
        # Loading a fresh manager against the same dir should prune the
        # already-expired session rather than carry it forward forever.
        reloaded = SessionManager(self.tmp.name, ttl_minutes=60)
        self.assertEqual(len(reloaded.sessions), 0)

    def test_concurrent_create_session_does_not_corrupt_file(self):
        tokens = []
        lock = threading.Lock()

        def create():
            t = self.mgr.create_session("alice")
            with lock:
                tokens.append(t)

        threads = [threading.Thread(target=create) for _ in range(20)]
        for t in threads:
            t.start()
        for t in threads:
            t.join()

        self.assertEqual(len(tokens), 20)
        self.assertEqual(len(set(tokens)), 20)  # all unique
        raw = (Path(self.tmp.name) / "web_sessions.json").read_text(encoding="utf-8")
        data = json.loads(raw)
        self.assertEqual(len(data["sessions"]), 20)
        for t in tokens:
            self.assertEqual(self.mgr.validate_session(t), "alice")


@_needs_bcrypt
class TestSessionManagerRequireExistingUser(unittest.TestCase):
    """require_existing_user=True ties sessions to real, current users."""

    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.users = UserManager(self.tmp.name)
        self.sessions = SessionManager(self.tmp.name, ttl_minutes=60, require_existing_user=True)

    def tearDown(self):
        self.tmp.cleanup()

    def test_create_session_for_existing_user_succeeds(self):
        self.users.add_user("alice", "hunter2pass")
        token = self.sessions.create_session("alice")
        self.assertEqual(self.sessions.validate_session(token), "alice")

    def test_create_session_for_unknown_user_raises(self):
        with self.assertRaises(ValueError):
            self.sessions.create_session("ghost")

    def test_create_session_with_no_users_file_raises(self):
        fresh_dir = tempfile.TemporaryDirectory()
        try:
            sessions = SessionManager(fresh_dir.name, require_existing_user=True)
            with self.assertRaises(ValueError):
                sessions.create_session("anyone")
        finally:
            fresh_dir.cleanup()

    def test_default_does_not_require_existing_user(self):
        # Without opting in, SessionManager stays usable standalone.
        sessions = SessionManager(self.tmp.name, ttl_minutes=60)
        token = sessions.create_session("nobody_registered")
        self.assertEqual(sessions.validate_session(token), "nobody_registered")


@_needs_bcrypt
class TestBcryptRounds(unittest.TestCase):
    def test_bcrypt_rounds_env_var_is_honored(self):
        import importlib

        from elesync import web_auth as wa

        original = os.environ.get("ELE_BCRYPT_ROUNDS")
        try:
            os.environ["ELE_BCRYPT_ROUNDS"] = "4"
            importlib.reload(wa)
            self.assertEqual(wa.BCRYPT_ROUNDS, 4)
            hashed = wa.hash_password("hunter2pass1")
            self.assertTrue(hashed.startswith("$2b$04$"))
            self.assertTrue(wa.verify_password("hunter2pass1", hashed))
        finally:
            if original is None:
                os.environ.pop("ELE_BCRYPT_ROUNDS", None)
            else:
                os.environ["ELE_BCRYPT_ROUNDS"] = original
            importlib.reload(wa)


if __name__ == "__main__":
    unittest.main()
