# -*- mode: python ; coding: utf-8 -*-
#
# PyInstaller spec for MRS PLC Programmer
#
# Build command (run on Windows):
#   pyinstaller programmer_app.spec --clean
#
# Output: dist\MRS_Programmer.exe

from PyInstaller.utils.hooks import collect_all, collect_submodules

# Collect PyQt6 (Qt plugins, translations, platform DLLs)
try:
    qt_datas, qt_binaries, qt_hidden = collect_all('PyQt6')
except Exception:
    qt_datas, qt_binaries, qt_hidden = [], [], []

# Collect python-can interfaces
can_hidden = collect_submodules('can')

a = Analysis(
    ['programmer_app.py'],
    pathex=['.'],
    binaries=qt_binaries,
    datas=qt_datas,
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
    name='MRS_Programmer',
    debug=False,
    strip=False,
    upx=True,
    upx_exclude=[],
    runtime_tmpdir=None,
    console=False,          # no black console window behind the GUI
    # icon='icon.ico',      # uncomment and add icon.ico to use a custom icon
)
