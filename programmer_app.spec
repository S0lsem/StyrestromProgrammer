# -*- mode: python ; coding: utf-8 -*-
#
# PyInstaller spec for MRS PLC Programmer
#
# Build command (run on Windows):
#   pyinstaller programmer_app.spec --clean
#
# Output: dist\MRS_Programmer.exe

import os
from pathlib import Path

from PyInstaller.utils.hooks import collect_all, collect_submodules

# Collect PyQt6 (Qt plugins, translations, platform DLLs)
try:
    qt_datas, qt_binaries, qt_hidden = collect_all('PyQt6')
except Exception:
    qt_datas, qt_binaries, qt_hidden = [], [], []

# Collect python-can interfaces
can_hidden = collect_submodules('can')

# ---------------------------------------------------------------------------
# Bundle MRS_Developers_Studio_Console.exe (the actual flasher we wrap)
# alongside its DLLs. The wrapper looks for it at
# <our exe dir>/ConsoleFlasher/MRS_Developers_Studio_Console.exe.
#
# Override the source dir with MRS_CONSOLE_FLASHER_DIR if you build on a
# machine where Applics Studio is installed somewhere unusual.
# ---------------------------------------------------------------------------
_flasher_src_override = os.environ.get('MRS_CONSOLE_FLASHER_DIR')
if _flasher_src_override:
    _flasher_src = Path(_flasher_src_override)
else:
    _candidates = sorted(
        Path(os.environ.get('LOCALAPPDATA', '.')).glob(
            'ApplicsStudio/app-*/Tools/ConsoleFlasher'
        ),
        key=lambda p: p.stat().st_mtime,
        reverse=True,
    )
    _flasher_src = _candidates[0] if _candidates else None

console_flasher_datas = []
if _flasher_src and _flasher_src.is_dir():
    for f in _flasher_src.iterdir():
        if f.is_file():
            console_flasher_datas.append((str(f), 'ConsoleFlasher'))
else:
    raise SystemExit(
        'Cannot locate MRS_Developers_Studio_Console.exe to bundle. '
        'Install MRS Applics Studio on the build machine, or set '
        'MRS_CONSOLE_FLASHER_DIR to the folder containing the exe + DLLs.'
    )

a = Analysis(
    ['programmer_app.py'],
    pathex=['.'],
    binaries=qt_binaries,
    datas=qt_datas + console_flasher_datas,
    hiddenimports=(
        qt_hidden
        + can_hidden
        + [
            'mrs_protocol.config',       # gitignored — must exist on build machine
            'can.interfaces.pcan',
            'can.interfaces.pcan.pcan',
            'cryptography.fernet',
            'cryptography.hazmat.primitives.ciphers',
            'cryptography.hazmat.bindings._rust',
        ]
    ),
    hookspath=[],
    runtime_hooks=[],
    excludes=[],
    noarchive=False,
)

pyz = PYZ(a.pure)

exe = EXE(
    pyz,
    a.scripts,
    a.binaries,
    a.datas,
    [],
    name='Styrestrom_Programmer',
    debug=False,
    strip=False,
    upx=True,
    upx_exclude=[],
    runtime_tmpdir=None,
    console=False,          # no black console window behind the GUI
    # icon='icon.ico',      # uncomment and add icon.ico to use a custom icon
)
