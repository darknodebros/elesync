#!/usr/bin/env python3
"""Regenerate elesync/_icons.py, the dashboard's inline Phosphor icon sprite.

The dashboard uses one icon family (Phosphor, regular weight, MIT licensed).
Only the icons listed in ICONS are embedded, as <symbol id="ph-NAME"> elements
that pages reference with <svg class="ph"><use href="#ph-NAME"/></svg>.

Usage:
    npm pack @phosphor-icons/core && tar -xzf phosphor-icons-core-*.tgz
    python scripts/build_icons.py path/to/package && python -m ruff format elesync/_icons.py

Add a name to ICONS, re-run, and commit the regenerated elesync/_icons.py.
"""

import argparse
import re
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
OUT = ROOT / "elesync" / "_icons.py"

ICONS = (
    # navigation
    "squares-four",
    "folders",
    "brain",
    "plus",
    "download-simple",
    "plugs-connected",
    "device-mobile",
    "usb",
    "dots-three",
    "magnifying-glass",
    "lightning",
    "lock-simple",
    # content and actions
    "note",
    "git-merge",
    "upload-simple",
    "microphone",
    "tray-arrow-down",
    "tray",
    "pulse",
    "check-circle",
    "check",
    "globe",
    "briefcase",
    "house",
    "chat-circle",
    "robot",
    "folder",
    "folder-simple",
    "hard-drive",
    "vault",
    "plugs",
    "caret-right",
    # import sources
    "open-ai-logo",
    "asterisk",
    "star-four",
    "whatsapp-logo",
    "telegram-logo",
)


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    parser.add_argument(
        "package", type=Path, help="unpacked @phosphor-icons/core package directory"
    )
    args = parser.parse_args()
    src = args.package / "assets" / "regular"
    if not src.is_dir():
        print(f"no regular icons under {src}", file=sys.stderr)
        return 1
    symbols = []
    for name in ICONS:
        svg = (src / f"{name}.svg").read_text(encoding="utf-8")
        inner = re.search(r"<svg[^>]*>(.*)</svg>", svg, re.S)
        if inner is None:
            print(f"cannot parse {name}.svg", file=sys.stderr)
            return 1
        symbols.append(
            f'<symbol id="ph-{name}" viewBox="0 0 256 256">{inner.group(1).strip()}</symbol>'
        )
    sprite = (
        '<svg width="0" height="0" style="position:absolute" aria-hidden="true" focusable="false"><defs>'
        + "".join(symbols)
        + "</defs></svg>"
    )
    OUT.write_text(
        '"""Inline Phosphor icon sprite for the dashboard. Generated; do not edit.\n\n'
        "Regenerate with scripts/build_icons.py. Icons: Phosphor Icons\n"
        "(https://phosphoricons.com), MIT License, Copyright (c) 2023 Phosphor Icons.\n"
        '"""\n\n'
        f"ICON_NAMES = {ICONS!r}\n\n"
        f"ICON_SPRITE = {sprite!r}\n",
        encoding="utf-8",
        newline="\n",
    )
    print(f"wrote {OUT.relative_to(ROOT)} ({len(ICONS)} icons, {len(sprite)} bytes)")
    return 0


if __name__ == "__main__":
    sys.exit(main())
