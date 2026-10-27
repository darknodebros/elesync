import unittest

from elesync.access import AccessGuard


class TestAccessGuard(unittest.TestCase):
    def test_project_guard_allows_exact_match(self):
        AccessGuard(project_id="project-123").require_project("project-123")

    def test_project_guard_allows_hex_short_id_prefix(self):
        # Short-id resolution: an assigned 8-char UUID prefix matches the full id.
        full = "f47ac10b58cc4372a5670e02b2c3d479"
        AccessGuard(project_id=full[:8]).require_project(full)
        AccessGuard(project_id=full).require_project(full[:8])

    def test_project_guard_rejects_other_project(self):
        with self.assertRaisesRegex(ValueError, "assigned project"):
            AccessGuard(project_id="project-123").require_project("other")

    def test_project_guard_rejects_non_hex_prefix_sibling(self):
        # A plain-text prefix must not authorize a longer sibling — non-hex
        # project ids get exact-match only, blocking "proj-1" → "proj-11".
        with self.assertRaisesRegex(ValueError, "assigned project"):
            AccessGuard(project_id="project-1").require_project("project-11")
        with self.assertRaisesRegex(ValueError, "assigned project"):
            AccessGuard(project_id="project-123").require_project("project-123-extra")

    def test_project_guard_rejects_short_hex_below_min_length(self):
        # Prefixes shorter than 8 hex chars must not authorize — insufficient
        # entropy to distinguish projects.
        with self.assertRaisesRegex(ValueError, "assigned project"):
            AccessGuard(project_id="abcd").require_project("abcd1234567890abcdef")

    def test_read_guard_rejects_project_without_read(self):
        with self.assertRaisesRegex(PermissionError, "read access"):
            AccessGuard(project_id="project-123", project_read=False).require_read()

    def test_write_guard_rejects_read_only(self):
        with self.assertRaisesRegex(PermissionError, "read-only"):
            AccessGuard(readonly=True).require_write()

    def test_write_guard_rejects_project_without_write(self):
        with self.assertRaisesRegex(PermissionError, "write access"):
            AccessGuard(project_id="project-123", project_write=False).require_write()


if __name__ == "__main__":
    unittest.main()
