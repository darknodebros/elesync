"""Build the gold Cosmic-theme variants of the EleSync logo, favicon and app icon.

A CSS hue-rotate turns the violet-and-blue logo red and green, and browser-tab and
home-screen icons cannot take a CSS filter at all, so the Cosmic theme swaps in
gold copies made here. Each pixel's brightness (its brightest channel,
so the blue neon keeps its glow) is mapped onto a black, amber, ivory ramp that
matches the theme's palette; transparency is left untouched.

    python scripts/build_cosmic_icons.py
"""

from __future__ import annotations

from pathlib import Path

from PIL import Image, ImageOps

STATIC = Path(__file__).resolve().parent.parent / "elesync" / "static"
ICONS = {
    "elesync-favicon.png": "elesync-favicon-cosmic.png",
    "elesync-main-logo.png": "elesync-main-logo-cosmic.png",
    "elesync-main-logo.webp": "elesync-main-logo-cosmic.webp",
}
SAVE_OPTIONS = {".png": {"optimize": True}, ".webp": {"quality": 82, "method": 6}}
SPACE_BLACK = "#05060b"
AMBER = "#d9962a"
IVORY = "#fff4d6"
AMBER_AT = 110  # brightness (0-255) that maps to pure amber


def cosmic_variant(image: Image.Image) -> Image.Image:
    rgba = image.convert("RGBA")
    brightness = rgba.convert("RGB").convert("HSV").getchannel("V")
    gold = ImageOps.colorize(
        brightness, black=SPACE_BLACK, mid=AMBER, white=IVORY, midpoint=AMBER_AT
    )
    gold.putalpha(rgba.getchannel("A"))
    return gold


def main() -> None:
    for source, target in ICONS.items():
        output = STATIC / target
        cosmic_variant(Image.open(STATIC / source)).save(output, **SAVE_OPTIONS[output.suffix])
        print(f"wrote {target}")


if __name__ == "__main__":
    main()
