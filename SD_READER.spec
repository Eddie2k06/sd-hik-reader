# -*- mode: python ; coding: utf-8 -*-


a = Analysis(
    ['C:/Pythons/sd_3\\main.py'],
    pathex=[],
    binaries=[],
    datas=[('C:/Pythons/sd_3\\README.md', '.'), ('C:/Pythons/sd_3\\core', 'core'), ('C:/Pythons/sd_3\\gui', 'gui'), ('C:/Pythons/sd_3\\requirements.txt', '.'), ('C:/Pythons/sd_3\\sd-card.ico', '.'), ('C:/Pythons/sd_3\\sd_3.zip', '.')],
    hiddenimports=[],
    hookspath=[],
    hooksconfig={},
    runtime_hooks=[],
    excludes=[],
    noarchive=False,
    optimize=0,
)
pyz = PYZ(a.pure)

exe = EXE(
    pyz,
    a.scripts,
    a.binaries,
    a.datas,
    [],
    name='SD_READER',
    debug=False,
    bootloader_ignore_signals=False,
    strip=False,
    upx=False,
    upx_exclude=[],
    runtime_tmpdir=None,
    console=False,
    disable_windowed_traceback=False,
    argv_emulation=False,
    target_arch=None,
    codesign_identity=None,
    entitlements_file=None,
    icon=['C:\\Pythons\\sd_3\\sd-card.ico'],
)
