"""Regression tests for managed Companion tunnel parsing."""

import unittest
from unittest.mock import patch

from elesync.tunnel import TUNNEL_URL_RE, TunnelManager


class _RunningTunnelProcess:
    def __init__(self):
        self.stdout = iter(
            [
                "Manage domains at https://admin.localhost.run/\n",
                "elephant.lhr.life tunneled with tls termination, https://elephant.lhr.life\n",
            ]
        )

    def poll(self):
        return None

    def wait(self, timeout=None):
        return 0

    def terminate(self):
        pass


class _StoppedTunnelProcess:
    stdout = iter(())

    def poll(self):
        return 0

    def wait(self, timeout=None):
        return 0

    def terminate(self):
        pass


class TestTunnelUrlRecognition(unittest.TestCase):
    def test_accepts_legacy_lhr_life_pairing_address(self):
        match = TUNNEL_URL_RE.search("ready: https://purple.lhr.life")
        self.assertIsNotNone(match)
        self.assertEqual(match.group(0), "https://purple.lhr.life")

    def test_accepts_current_localhost_run_pairing_address(self):
        match = TUNNEL_URL_RE.search("ready: https://elephant.localhost.run")
        self.assertIsNotNone(match)
        self.assertEqual(match.group(0), "https://elephant.localhost.run")

    def test_rejects_non_tunnel_addresses(self):
        self.assertIsNone(TUNNEL_URL_RE.search("https://127.0.0.1:7685"))

    def test_rejects_localhost_run_admin_address(self):
        self.assertIsNone(TUNNEL_URL_RE.search("https://admin.localhost.run/"))

    def test_rejects_insecure_tunnel_address(self):
        self.assertIsNone(TUNNEL_URL_RE.search("ready: http://elephant.localhost.run"))

    def test_tunnel_uses_host_verification_without_interactive_prompts(self):
        manager = TunnelManager()
        process = _RunningTunnelProcess()
        with (
            patch.object(manager, "is_ssh_available", return_value=True),
            patch("elesync.tunnel.subprocess.Popen", return_value=process) as popen,
        ):
            self.assertTrue(manager.start(7477))

        command = popen.call_args.args[0]
        self.assertEqual(manager.public_url, "https://elephant.lhr.life")
        self.assertIn("80:127.0.0.1:7477", command)
        self.assertIn("StrictHostKeyChecking=accept-new", command)
        self.assertIn("BatchMode=yes", command)
        self.assertNotIn("StrictHostKeyChecking=no", command)

    def test_output_reader_tolerates_tunnel_being_stopped(self):
        manager = TunnelManager()
        process = _StoppedTunnelProcess()
        manager.process = process
        manager.stop()

        manager._read_output(process)

        self.assertIsNone(manager.error)
