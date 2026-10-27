# -*- mode: python ; coding: utf-8 -*-
"""PyInstaller spec for EleSync standalone executables.

Build commands (run from repo root):
    pyinstaller scripts/elesync.spec --distpath dist

Output:
    dist/ele              (macOS/Linux CLI — one file)
    dist/ele/ele.exe      (Windows CLI — a directory build)
    dist/EleSync          (macOS/Linux GUI launcher — one file)
    dist/EleSync/EleSync.exe  (Windows GUI — a directory build)
    dist/EleSync.app      (macOS GUI .app bundle)

Windows is a **onedir** build for both executables. A onefile executable
unpacks to a temp dir and is more likely to be flagged by Defender's ML
heuristic. Directory builds avoid that. macOS and Linux use onefile for the
bare CLI but BUNDLE for the GUI .app.
"""

import sys
import os
from pathlib import Path

ROOT = Path(os.getcwd())
block_cipher = None
STATIC_DATAS = [(str(ROOT / "elesync" / "static"), "elesync/static")]

# ---- version (read from the package, so this never drifts) -----------------
import re

VERSION = re.search(
    r'__version__\s*=\s*"([^"]+)"',
    (ROOT / "elesync" / "__init__.py").read_text(encoding="utf-8"),
).group(1)

# ---- Windows version resource ---------------------------------------------
_version_info = None
if sys.platform == "win32":
    from PyInstaller.utils.win32.versioninfo import (
        FixedFileInfo,
        StringFileInfo,
        StringStruct,
        StringTable,
        VarFileInfo,
        VarStruct,
        VSVersionInfo,
    )

    _vt = tuple(int(x) for x in VERSION.split(".")[:3]) + (0,)
    _version_info = VSVersionInfo(
        ffi=FixedFileInfo(
            filevers=_vt,
            prodvers=_vt,
            mask=0x3F,
            flags=0x0,
            OS=0x40004,
            fileType=0x1,
            subtype=0x0,
            date=(0, 0),
        ),
        kids=[
            StringFileInfo(
                [
                    StringTable(
                        "040904B0",
                        [
                            StringStruct("CompanyName", "EleSync"),
                            StringStruct(
                                "FileDescription",
                                "EleSync - local-first MCP memory vault",
                            ),
                            StringStruct("FileVersion", VERSION),
                            StringStruct("InternalName", "ele"),
                            StringStruct(
                                "LegalCopyright",
                                "Copyright (c) 2026 Alphanymous. All rights reserved.",
                            ),
                            StringStruct("OriginalFilename", "ele.exe"),
                            StringStruct("ProductName", "EleSync"),
                            StringStruct("ProductVersion", VERSION),
                        ],
                    )
                ]
            ),
            VarFileInfo([VarStruct("Translation", [0x0409, 1200])]),
        ],
    )

# ---- collect hidden imports -----------------------------------------------
_hidden = [
    "elesync.cli",
    "elesync.commands",
    "elesync.commands.backup",
    "elesync.mcp_server",
    "elesync.store",
    "elesync.store_projects",
    "elesync.i18n",
    "elesync.models",
    "elesync.normalize",
    "elesync.onboarding",
    "elesync.web",
    "elesync._web_templates",
    "elesync.embeddings",
    "elesync.crypto",
    "elesync.usb",
    "mcp",
    "mcp.server",
    "mcp.server.stdio",
    "mcp.types",
    "mcp.shared",
    "nacl",
    "nacl.secret",
    "nacl.bindings",
    "nacl.pwhash",
    "nacl.utils",
]

# ---- CLI analysis ---------------------------------------------------------
a_cli = Analysis(
    [str(ROOT / "scripts" / "entry.py")],
    pathex=[str(ROOT)],
    binaries=[],
    datas=STATIC_DATAS,
    hiddenimports=_hidden,
    hookspath=[],
    hooksconfig={},
    runtime_hooks=[],
    excludes=[],
    noarchive=False,
    cipher=block_cipher,
)
pyz_cli = PYZ(a_cli.pure, cipher=block_cipher)

# ---- GUI analysis ---------------------------------------------------------
a_gui = Analysis(
    [str(ROOT / "scripts" / "entry_web.py")],
    pathex=[str(ROOT)],
    binaries=[],
    datas=STATIC_DATAS,
    hiddenimports=_hidden,
    hookspath=[],
    hooksconfig={},
    runtime_hooks=[],
    excludes=[],
    noarchive=False,
    cipher=block_cipher,
)
pyz_gui = PYZ(a_gui.pure, cipher=block_cipher)

_ONEDIR = sys.platform == "win32"
_cli_payload = [] if _ONEDIR else [a_cli.binaries, a_cli.datas]
_gui_payload = [] if _ONEDIR else [a_gui.binaries, a_gui.datas]

# ---- CLI executable -------------------------------------------------------
exe_cli = EXE(
    pyz_cli,
    a_cli.scripts,
    *_cli_payload,
    [],
    exclude_binaries=_ONEDIR,
    name="ele",
    debug=False,
    bootloader_ignore_signals=False,
    strip=False,
    upx=False,
    upx_exclude=[],
    runtime_tmpdir=None,
    version=_version_info,
    console=True,
    disable_windowed_traceback=False,
    argv_emulation=False,
    target_arch=None,
    codesign_identity=None,
    entitlements_file=None,
    icon=None,
)

# ---- GUI executable -------------------------------------------------------
exe_gui = EXE(
    pyz_gui,
    a_gui.scripts,
    *_gui_payload,
    [],
    exclude_binaries=_ONEDIR,
    name="EleSync",
    debug=False,
    bootloader_ignore_signals=False,
    strip=False,
    upx=False,
    upx_exclude=[],
    runtime_tmpdir=None,
    version=_version_info,
    console=False,  # windowed/no-console GUI launcher
    disable_windowed_traceback=False,
    argv_emulation=True,  # macOS .app bundle needs this for URL/file args
    target_arch=None,
    codesign_identity=None,
    entitlements_file=None,
    icon=str(ROOT / "assets" / "elesync.ico") if sys.platform == "win32" else None,
)

# ---- Windows: collect onedir directories ----------------------------------
if _ONEDIR:
    coll_cli = COLLECT(
        exe_cli,
        a_cli.binaries,
        a_cli.datas,
        strip=False,
        upx=False,
        upx_exclude=[],
        name="ele",
    )
    coll_gui = COLLECT(
        exe_gui,
        a_gui.binaries,
        a_gui.datas,
        strip=False,
        upx=False,
        upx_exclude=[],
        name="EleSync",
    )

# ---- macOS: wrap GUI in .app bundle ---------------------------------------
if sys.platform == "darwin":
    app = BUNDLE(
        exe_gui,
        name="EleSync.app",
        icon=None,
        bundle_identifier="dev.elesync.app",
        version=VERSION,
        info_plist={
            "CFBundleShortVersionString": VERSION,
            "CFBundleVersion": VERSION,
            "LSMinimumSystemVersion": "10.13",
            "LSUIElement": False,  # Show dock icon while running
            "NSHighResolutionCapable": True,
        },
    )
