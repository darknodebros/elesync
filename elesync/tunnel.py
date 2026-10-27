import atexit
import logging
import re
import shutil
import subprocess
import sys
import threading
import time

logger = logging.getLogger(__name__)
TUNNEL_URL_RE = re.compile(
    r"https://(?!(?:admin|localhost)\.localhost\.run\b)"
    r"[a-zA-Z0-9.-]+\.(?:lhr\.life|localhost\.run)\b"
)


class TunnelManager:
    def __init__(self):
        self.process = None
        self.public_url = None
        self.error = None
        self.last_output = ""
        # start() may need to stop a previous tunnel while it already holds
        # this lock.  A regular Lock would deadlock that recovery path.
        self.lock = threading.RLock()
        atexit.register(self.stop)

    def is_ssh_available(self):
        return shutil.which("ssh") is not None

    def start(self, port):
        with self.lock:
            if self.process:
                self.stop()

            self.public_url = None
            self.error = None
            self.last_output = ""

            if not self.is_ssh_available():
                self.error = (
                    "EleSync cannot create the automatic connection because SSH is not installed."
                )
                return False

            # Use explicit IPv4 loopback because the dashboard binds 127.0.0.1;
            # Windows may resolve localhost to ::1 first.
            # ssh -R 80:127.0.0.1:PORT nokey@localhost.run
            # localhost.run prints the public URL to stdout
            cmd = [
                "ssh",
                "-o",
                "ExitOnForwardFailure=yes",
                "-o",
                "ServerAliveInterval=30",
                "-o",
                "ServerAliveCountMax=3",
                "-o",
                "StrictHostKeyChecking=accept-new",
                "-o",
                "BatchMode=yes",
                "-R",
                f"80:127.0.0.1:{port}",
                "nokey@localhost.run",
            ]

            try:
                self.process = subprocess.Popen(
                    cmd,
                    stdin=subprocess.DEVNULL,
                    stdout=subprocess.PIPE,
                    stderr=subprocess.STDOUT,
                    text=True,
                    bufsize=1,
                    creationflags=subprocess.CREATE_NO_WINDOW if sys.platform == "win32" else 0,
                )
            except Exception as e:
                self.error = f"Failed to start SSH tunnel: {e}"
                return False

            # Thread to read stdout and find the URL
            process = self.process
            threading.Thread(target=self._read_output, args=(process,), daemon=True).start()

            # Wait for URL (up to 10 seconds)
            start_time = time.time()
            while time.time() - start_time < 10:
                if self.public_url:
                    return True
                if self.error:
                    self._stop_process()
                    return False
                time.sleep(0.5)

            self.error = (
                "EleSync could not reach the secure pairing service. Check that your network or "
                "firewall allows outbound SSH on port 22, then retry."
            )
            self._stop_process()
            return False

    def _read_output(self, process):
        if not process:
            return

        for line in process.stdout:
            logger.debug("Companion SSH: %s", line.rstrip())
            if self.process is not process:
                continue
            self.last_output = line.strip()
            # localhost.run formerly issued *.lhr.life URLs and now also
            # issues *.localhost.run URLs. Accept both, so pairing does not
            # silently fail when the provider changes the free-domain suffix.
            match = TUNNEL_URL_RE.search(line)
            if match and self.process is process:
                self.public_url = match.group(0)

        if (
            self.process is process
            and process.poll() is not None
            and not self.public_url
            and not self.error
        ):
            self.error = "The secure connection closed before it was ready."

    def _stop_process(self):
        process = self.process
        if process:
            if process.poll() is None:
                process.terminate()
            try:
                process.wait(timeout=3)
            except subprocess.TimeoutExpired:
                process.kill()
                process.wait(timeout=3)
            self.process = None

    def stop(self):
        with self.lock:
            self._stop_process()
            self.public_url = None
            self.error = None

    def status(self):
        with self.lock:
            if self.process and self.process.poll() is None:
                return {"active": True, "url": self.public_url, "error": self.error}
            return {"active": False, "url": None, "error": self.error}
