"""Regression coverage for replacing an existing secure pairing tunnel."""

import subprocess
import unittest
from unittest.mock import Mock, patch

from elesync.tunnel import TunnelManager


class _RunningProcess:
    def __init__(self):
        self.terminated = False

    def poll(self):
        return None

    def wait(self, timeout=None):
        return 0

    def terminate(self):
        self.terminated = True


class TestTunnelLifecycle(unittest.TestCase):
    def test_start_replaces_existing_process_without_locking_status(self):
        manager = TunnelManager()
        previous = _RunningProcess()
        manager.process = previous

        with patch.object(manager, "is_ssh_available", return_value=False):
            self.assertFalse(manager.start(7685))

        self.assertTrue(previous.terminated)
        self.assertIsNone(manager.process)
        self.assertIn("SSH", manager.status()["error"])

    def test_disconnect_waits_and_kills_unresponsive_process(self):
        manager = TunnelManager()
        process = Mock()
        process.poll.return_value = None
        process.wait.side_effect = [subprocess.TimeoutExpired("ssh", 3), 0]
        manager.process = process
        manager.public_url = "https://test.lhr.life"
        manager.stop()
        process.terminate.assert_called_once()
        process.kill.assert_called_once()
        self.assertEqual(process.wait.call_count, 2)
        self.assertEqual(manager.status(), {"active": False, "url": None, "error": None})

    def test_launch_flags_and_captured_output_on_each_platform(self):
        for platform, flags in [("win32", 0x08000000), ("linux", 0)]:
            with self.subTest(platform=platform):
                manager = TunnelManager()
                process = Mock()
                process.poll.return_value = None
                process.stdout = iter(["ready https://test.lhr.life\n"])
                with (
                    patch.object(manager, "is_ssh_available", return_value=True),
                    patch("elesync.tunnel.sys.platform", platform),
                    patch("elesync.tunnel.subprocess.CREATE_NO_WINDOW", 0x08000000, create=True),
                    patch("elesync.tunnel.subprocess.Popen", return_value=process) as launch,
                    self.assertLogs("elesync.tunnel", level="DEBUG") as logs,
                ):
                    self.assertTrue(manager.start(7477))
                self.assertIs(manager.process, process)
                self.assertTrue(manager.status()["active"])
                self.assertEqual(launch.call_args.kwargs["creationflags"], flags)
                self.assertEqual(launch.call_args.kwargs["stdout"], subprocess.PIPE)
                self.assertEqual(launch.call_args.kwargs["stderr"], subprocess.STDOUT)
                self.assertEqual(launch.call_args.kwargs["stdin"], subprocess.DEVNULL)
                self.assertIn("ready https://test.lhr.life", logs.output[0])
                manager.stop()

    def test_failed_start_reaps_process_and_preserves_error(self):
        manager = TunnelManager()
        process = Mock()
        process.poll.return_value = 1
        process.stdout = iter([])
        with (
            patch.object(manager, "is_ssh_available", return_value=True),
            patch("elesync.tunnel.subprocess.Popen", return_value=process),
        ):
            self.assertFalse(manager.start(7477))
        process.wait.assert_called_once_with(timeout=3)
        self.assertIsNone(manager.process)
        self.assertFalse(manager.status()["active"])
        self.assertIn("closed", manager.error)
