"""Fail deployment if the public HTML or configured security headers drift."""

from __future__ import annotations

import argparse
import json
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent


def verify(body: bytes, raw_headers: str) -> None:
    if body != (ROOT / "index.html").read_bytes():
        raise ValueError("Deployed HTML does not match this source commit")
    headers = {}
    for line in raw_headers.splitlines():
        if line.startswith("HTTP/"):
            headers = {}
        elif ":" in line:
            name, value = line.split(":", 1)
            headers[name.lower()] = value.strip()
    config = json.loads((ROOT / "vercel.json").read_text())
    for expected in config["headers"][0]["headers"]:
        name, value = expected["key"].lower(), expected["value"]
        if headers.get(name) != value:
            raise ValueError(f"Missing or unexpected deployed header: {name}")


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--body", type=Path, required=True)
    parser.add_argument("--headers", type=Path, required=True)
    args = parser.parse_args()
    verify(args.body.read_bytes(), args.headers.read_text())
    print("Site HTML and security headers match this source commit.")


if __name__ == "__main__":
    main()
