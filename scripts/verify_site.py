"""Fail deployment if the public HTML or GitHub Pages content type drift."""

from __future__ import annotations

import argparse
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
    if not headers.get("content-type", "").lower().startswith("text/html"):
        raise ValueError("Missing or unexpected deployed header: content-type")


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--body", type=Path, required=True)
    parser.add_argument("--headers", type=Path, required=True)
    args = parser.parse_args()
    verify(args.body.read_bytes(), args.headers.read_text())
    print("Site HTML and content type match this source commit.")


if __name__ == "__main__":
    main()
