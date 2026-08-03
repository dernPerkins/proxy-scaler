# -*- mode: python ; coding: utf-8 -*-
# One-folder build (a directory: the exe + a supporting _internal/ tree),
# loaded directly off disk with no per-launch unpacking step. This was
# originally onefile (a single self-contained executable that
# self-extracts to a fresh temp dir on *every* launch) because Tauri's
# externalBin/sidecar mechanism only manages a single named executable —
# but with torch alone at ~1.2GB on disk, re-extracting the whole bundle
# on every single app launch was the dominant cost behind a real, reported
# "server still starting" delay, independent of how fast Python itself
# imports things once files are actually on disk (see upscale.py's lazy
# torch/spandrel/torchvision imports for that separate, still-valid fix).
# Shipped via Tauri's `bundle.resources` mechanism instead of
# `externalBin` (see tauri.conf.json + main.rs) — resources aren't
# restricted to a single file, so onedir's directory output works
# directly with no extraction step at all.
#
# This got much simpler once the sidecar's child changed from Streamlit to
# a plain FastAPI/uvicorn server: no more bundling app.py as a second
# Analysis script (Streamlit's script-runner read it off a real disk path
# at runtime; FastAPI has no equivalent), no more copy_metadata/
# collect_data_files for Streamlit's importlib.metadata version lookup and
# bundled static frontend, no more hiddenimports for Streamlit's
# dynamically-imported "magic" module. If a similar "PackageNotFoundError:
# No package metadata was found for X" ever shows up for fastapi/uvicorn/
# pydantic, the fix is the same pattern that solved it for Streamlit:
# `from PyInstaller.utils.hooks import copy_metadata` and add
# `copy_metadata("X")` to `datas` below.
#
# torch's hooks come from pyinstaller-hooks-contrib and are
# auto-discovered once that package is installed in the build venv — no
# explicit hookspath needed.
from pathlib import Path

# Spec files are exec()'d directly by PyInstaller, not imported as a
# module — there's no __file__ in this namespace. PyInstaller injects
# SPECPATH (this spec's own directory) instead.
ROOT = Path(SPECPATH).resolve().parents[1]  # noqa: F821
ENTRY_SCRIPT = Path(SPECPATH).resolve() / "run_supervisor.py"  # noqa: F821

# uvicorn picks its HTTP/websocket/event-loop implementation at runtime via
# its own internal "auto" modules (httptools vs h11, uvloop vs asyncio,
# etc.) — invisible to PyInstaller's static import-graph analysis, so they
# have to be listed explicitly, the same class of issue Streamlit's
# dynamically-imported "magic" module was.
HIDDEN_IMPORTS = [
    "uvicorn.loops.auto",
    "uvicorn.protocols.http.auto",
    "uvicorn.protocols.websockets.auto",
    "uvicorn.lifespan.on",
]

a = Analysis(
    [str(ENTRY_SCRIPT)],
    pathex=[str(ROOT)],
    binaries=[],
    datas=[],
    hiddenimports=HIDDEN_IMPORTS,
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
    name="proxy-scaler-serve",
)
