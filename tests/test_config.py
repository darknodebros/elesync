import os
import tempfile
import unittest
from pathlib import Path
from unittest import mock

from elesync import config


class TestConfig(unittest.TestCase):
    def test_config_path_uses_appdata(self):
        with mock.patch.dict(os.environ, {"APPDATA": r"C:\\EleSyncTest"}):
            self.assertEqual(
                config.config_path(), Path(r"C:\\EleSyncTest") / "EleSync" / "config.json"
            )

    def test_load_missing_config_is_empty(self):
        with (
            tempfile.TemporaryDirectory() as directory,
            mock.patch.dict(os.environ, {"APPDATA": directory}),
        ):
            self.assertEqual(config.load_config(), {})

    def test_save_and_load_round_trip(self):
        with (
            tempfile.TemporaryDirectory() as directory,
            mock.patch.dict(os.environ, {"APPDATA": directory}),
        ):
            config.save_config({"vault_dir": "D:/vault"})
            self.assertEqual(config.load_config(), {"vault_dir": "D:/vault"})


if __name__ == "__main__":
    unittest.main()
