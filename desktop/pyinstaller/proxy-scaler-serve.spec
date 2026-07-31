# -*- mode: python ; coding: utf-8 -*-
# One-folder build (not --onefile, which is discouraged for large ML
# bundles — it re-unpacks to a temp dir on every launch). torch's hooks
# come from pyinstaller-hooks-contrib and are auto-discovered once that
# package is installed in the build venv — no explicit hookspath needed.
from pathlib import Path

ROOT = Path(__file__).resolve().parents[2]
ENTRY_SCRIPT = Path(__file__).resolve().parent / "run_supervisor.py"

a = Analysis(
    [str(ENTRY_SCRIPT)],
    pathex=[str(ROOT)],
    binaries=[],
    datas=[],
    hiddenimports=[],
    hookspath=[],
    hooksconfig={},
    runtime_hooks=[],
    excludes=[],
    noarchive=False,
)
pyz = PYZ(a.pure, a.zipped_data)

exe = EXE(
    pyz,
    a.scripts,
    [],
    exclude_binaries=True,
    name="proxy-scaler-serve",
    debug=False,
    bootloader_ignore_signals=False,
    strip=False,
    upx=False,
    console=True,
)

coll = COLLECT(
    exe,
    a.binaries,
    a.zipfiles,
    a.datas,
    strip=False,
    upx=False,
    upx_exclude=[],
    name="proxy-scaler-serve",
)
