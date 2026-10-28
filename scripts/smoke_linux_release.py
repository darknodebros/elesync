"""Exercise each frozen Linux executable and its bundled dashboard over HTTP."""

from __future__ import annotations

import os
import socket
import subprocess
import sys
import tempfile
import time
import urllib.error
import urllib.request
from pathlib import Path


def smoke(binary: Path) -> None:
    with tempfile.TemporaryDirectory(prefix="elesync-release-") as temporary:
        root = Path(temporary)
        with socket.socket() as probe:
            probe.bind(("127.0.0.1", 0))
            port = probe.getsockname()[1]
        environment = {**os.environ, "APPDATA": str(root / "config")}
        with (root / "process.log").open("w+") as log:
            process = subprocess.Popen(
                [
                    str(binary.resolve()),
                    "--vault",
                    str(root / "vault"),
                    "web",
                    "--no-browser",
                    "--host",
                    "127.0.0.1",
                    "--port",
                    str(port),
                ],
                env=environment,
                stdout=log,
                stderr=subprocess.STDOUT,
            )
            try:
                origin = f"http://127.0.0.1:{port}"
                deadline = time.monotonic() + 90
                while True:
                    if process.poll() is not None:
                        raise RuntimeError(f"{binary} exited with {process.returncode}")
                    try:
                        with urllib.request.urlopen(origin + "/", timeout=2) as response:
                            assert response.status == 200
                            assert b"EleSync" in response.read()
                        break
                    except (urllib.error.URLError, TimeoutError):
                        if time.monotonic() >= deadline:
                            raise RuntimeError(f"{binary} dashboard did not start") from None
                        time.sleep(0.5)
                for asset in ("dashboard.css", "dashboard.js"):
                    with urllib.request.urlopen(origin + "/static/" + asset, timeout=5) as response:
                        assert response.status == 200
                        assert len(response.read()) > 100, f"Missing bundled {asset}"
                print(f"PASS: {binary.name} serves dashboard, CSS and JavaScript")
            finally:
                process.terminate()
                try:
                    process.wait(timeout=10)
                except subprocess.TimeoutExpired:
                    process.kill()
                    process.wait()
                log.seek(0)
                print(log.read())


if __name__ == "__main__":
    for argument in sys.argv[1:]:
        smoke(Path(argument))
