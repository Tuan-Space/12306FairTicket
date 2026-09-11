# -*- mode: python ; coding: utf-8 -*-

from pathlib import Path
import sys

from PyInstaller.utils.win32.versioninfo import (
    FixedFileInfo, StringFileInfo, StringStruct, StringTable,
    VarFileInfo, VarStruct, VSVersionInfo,
)
from ticket_app import __version__


project_root = Path(SPECPATH).resolve()
entry_script = project_root / "gui.py"
assets_dir = project_root / "assets"
version_tuple = tuple(int(part) for part in __version__.split(".")) + (0,)
version_info = VSVersionInfo(
    ffi=FixedFileInfo(filevers=version_tuple, prodvers=version_tuple,
                     mask=0x3F, flags=0, OS=0x40004, fileType=1, subtype=0, date=(0, 0)),
    kids=[
        StringFileInfo([StringTable("040904B0", [
            StringStruct("CompanyName", "Tuan-Space"),
            StringStruct("FileDescription", "12306 Fair Ticket"),
            StringStruct("FileVersion", __version__),
            StringStruct("ProductName", "12306 Fair Ticket"),
            StringStruct("ProductVersion", __version__),
            StringStruct("OriginalFilename", "12306FairTicket.exe"),
        ])]),
        VarFileInfo([VarStruct("Translation", [1033, 1200])]),
    ],
)

# Conda-based venvs keep OpenSSL beside the base interpreter. Collect those
# DLLs explicitly so Qt's TLS hook cannot pick an unrelated copy from PATH.
interpreter_library = Path(sys.base_prefix) / "Library" / "bin"
interpreter_openssl = [
    (str(path), ".")
    for name in ("libssl-3-x64.dll", "libcrypto-3-x64.dll")
    if (path := interpreter_library / name).is_file()
]

if not entry_script.is_file():
    raise FileNotFoundError(f"GUI entry point not found: {entry_script}")
if not assets_dir.is_dir():
    raise FileNotFoundError(f"Assets directory not found: {assets_dir}")


analysis = Analysis(
    [str(entry_script)],
    pathex=[str(project_root)],
    binaries=interpreter_openssl,
    datas=[(str(assets_dir), "assets")],
    hiddenimports=["PySide6.QtSvg"],
    hookspath=[],
    hooksconfig={},
    runtime_hooks=[],
    excludes=[],
    noarchive=False,
    optimize=0,
)

pyz = PYZ(analysis.pure)

exe = EXE(
    pyz,
    analysis.scripts,
    [],
    exclude_binaries=True,
    name="12306FairTicket",
    debug=False,
    bootloader_ignore_signals=False,
    strip=False,
    upx=False,
    console=False,
    disable_windowed_traceback=False,
    argv_emulation=False,
    target_arch=None,
    codesign_identity=None,
    entitlements_file=None,
    contents_directory="_internal",
    version=version_info,
    uac_admin=False,
    uac_uiaccess=False,
)

bundle = COLLECT(
    exe,
    analysis.binaries,
    analysis.datas,
    strip=False,
    upx=False,
    name="12306FairTicket",
)
