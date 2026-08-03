# -*- mode: python ; coding: utf-8 -*-
# --onefile-equivalent build: a single self-contained executable. Originally
# built as one-folder (the generally-recommended mode for large ML bundles,
# since one-folder avoids re-unpacking to a temp dir on every launch) but
# switched to onefile because Tauri's sidecar mechanism only manages a
# single named executable — in `cargo tauri dev` it copies just that one
# file next to the app binary, not a whole supporting directory, which
# broke the one-folder build's runtime lookup for its _internal/ folder.
# Revisit with a tauri `bundle.resources`-based one-folder setup during
# real packaging if onefile's slower cold start becomes a real problem —
# not worth the complexity before then.
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
    a.binaries,
    a.zipfiles,
    a.datas,
    [],
    name="proxy-scaler-serve",
    debug=False,
    bootloader_ignore_signals=False,
    strip=False,
    upx=False,
    runtime_tmpdir=None,
    console=True,
)
