#!/usr/bin/env python3
"""Build EleSync standalone executables with PyInstaller.

Usage:
    python scripts/build.py                          # default: build for current platform
    python scripts/build.py --target all             # build placeholder for all platforms (CI only)

Output goes to dist/ele-<platform>/ for the CLI and dist/EleSync-<platform>/
for the GUI launcher.
"""

import argparse
import shutil
import subprocess
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
SPEC = ROOT / "scripts" / "elesync.spec"
DIST = ROOT / "dist"


def build() -> None:
    # Clean previous builds
    for p in [ROOT / "build", DIST]:
        if p.exists():
            shutil.rmtree(p)

    subprocess.check_call(
        [sys.executable, "-m", "PyInstaller", str(SPEC), "--distpath", str(DIST)],
        cwd=str(ROOT),
    )

    # Normalise outputs into dist/ele-<platform>/ and dist/EleSync-<platform>/.
    platform_tag = {"win32": "windows", "darwin": "macos", "linux": "linux"}.get(
        sys.platform, sys.platform
    )
    cli_dst = DIST / f"ele-{platform_tag}"
    gui_dst = DIST / f"EleSync-{platform_tag}"

    if sys.platform == "win32":
        built_cli = DIST / "ele"
        built_gui = DIST / "EleSync"
        if not built_cli.is_dir():
            raise SystemExit(f"expected a onedir CLI build at {built_cli}, found nothing")
        if not built_gui.is_dir():
            raise SystemExit(f"expected a onedir GUI build at {built_gui}, found nothing")
        shutil.move(str(built_cli), str(cli_dst))
        shutil.move(str(built_gui), str(gui_dst))
        cli_exe = cli_dst / "ele.exe"
        gui_exe = gui_dst / "EleSync.exe"
        if not cli_exe.is_file():
            raise SystemExit(f"onedir CLI build produced no {cli_exe}")
        if not gui_exe.is_file():
            raise SystemExit(f"onedir GUI build produced no {gui_exe}")
        print(f"[OK] Built {cli_exe} and {gui_exe} (directory builds)")
        return

    if sys.platform == "darwin":
        # macOS: CLI is onefile `dist/ele`; GUI is `dist/EleSync.app`.
        cli_src = DIST / "ele"
        gui_src = DIST / "EleSync.app"
        if not cli_src.is_file():
            raise SystemExit(f"expected a onefile CLI build at {cli_src}")
        if not gui_src.is_dir():
            raise SystemExit(f"expected an .app bundle at {gui_src}")
        cli_dst.mkdir(parents=True, exist_ok=True)
        gui_dst.mkdir(parents=True, exist_ok=True)
        shutil.move(str(cli_src), str(cli_dst / "ele"))
        shutil.move(str(gui_src), str(gui_dst / "EleSync.app"))
        print(f"[OK] Built {cli_dst / 'ele'} and {gui_dst / 'EleSync.app'}")
        return

    # Linux: onefile for both CLI and GUI.
    cli_src = DIST / "ele"
    gui_src = DIST / "EleSync"
    if not cli_src.is_file():
        raise SystemExit(f"expected a onefile CLI build at {cli_src}")
    if not gui_src.is_file():
        raise SystemExit(f"expected a onefile GUI build at {gui_src}")
    cli_dst.mkdir(parents=True, exist_ok=True)
    gui_dst.mkdir(parents=True, exist_ok=True)
    shutil.move(str(cli_src), str(cli_dst / "ele"))
    shutil.move(str(gui_src), str(gui_dst / "EleSync"))
    print(f"[OK] Built {cli_dst / 'ele'} and {gui_dst / 'EleSync'}")


def main() -> None:
    parser = argparse.ArgumentParser(description="Build EleSync standalone executables")
    parser.add_argument(
        "--target", default="auto", help="Target platform (auto, windows, macos, linux, all)"
    )
    args = parser.parse_args()

    if args.target == "all":
        print("Multi-platform builds require CI. Use the release workflow instead.")
        sys.exit(1)

    build()


if __name__ == "__main__":
    main()
