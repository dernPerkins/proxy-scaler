# -*- mode: python ; coding: utf-8 -*-
# --onefile-equivalent build: a single self-contained executable. Originally
# built as one-folder (the generally-recommended mode for large ML bundles,
# since one-folder avoids re-unpacking to a temp dir on every launch) but
# switched to onefile because Tauri's sidecar mechanism only manages a
# single named executable — in `cargo tauri dev` it copies just that one
# file next to the app binary, not a whole supporting directory, which
# broke the one-folder build's runtime lookup for its _internal/ folder.
# Revisit with a tauri `bundle.resources`-based one-folder setup during
# real Phase 3 packaging if onefile's slower cold start becomes a real
# problem — not worth the complexity before then.
#
# torch's hooks come from pyinstaller-hooks-contrib and are
# auto-discovered once that package is installed in the build venv — no
# explicit hookspath needed.
from pathlib import Path

from PyInstaller.utils.hooks import collect_data_files, copy_metadata

# Spec files are exec()'d directly by PyInstaller, not imported as a
# module — there's no __file__ in this namespace. PyInstaller injects
# SPECPATH (this spec's own directory) instead.
ROOT = Path(SPECPATH).resolve().parents[1]  # noqa: F821
ENTRY_SCRIPT = Path(SPECPATH).resolve() / "run_supervisor.py"  # noqa: F821
APP_SCRIPT = ROOT / "app.py"

# PyInstaller only bundles a package's actual code by default, not its
# pip-level dist-info/METADATA — but streamlit reads its own version via
# importlib.metadata.version("streamlit") at import time, which needs
# that metadata to exist on disk. Without this, streamlit fails to import
# at all inside the frozen build with "PackageNotFoundError: No package
# metadata was found for streamlit". If other packages hit the same
# error later, add them here too — this is a common, expected pattern for
# frozen builds, not specific to streamlit.
METADATA_PACKAGES = ["streamlit"]

# Separately: streamlit also ships its compiled frontend (the static
# HTML/JS/CSS app shell it serves at "/") as plain data files inside its
# own package directory, not Python code — PyInstaller's import-graph
# analysis has no way to discover those on its own. Without this, the
# frozen build's Streamlit process starts and reports healthy, but every
# page request 404s ("Not Found") since the app shell it would serve
# was never bundled.
DATA_PACKAGES = ["streamlit"]

# Streamlit's "magic" feature (auto-writing a bare top-level expression,
# e.g. app.py's own module docstring, via st.write()) dynamically imports
# this module at runtime rather than at normal import time — invisible to
# PyInstaller's static import-graph analysis, so it has to be listed
# explicitly. If other "ModuleNotFoundError" surprises show up for
# similarly dynamically-imported streamlit/proxy_scaler internals later,
# add them here too.
HIDDEN_IMPORTS = ["streamlit.runtime.scriptrunner.magic_funcs"]

a = Analysis(
    # app.py is included as a second script (not just a data file) so
    # PyInstaller's static analysis also traces *its* import graph
    # (proxy_scaler.ui.*, etc.) — run_supervisor.py alone never imports
    # app.py, it only ever invokes it as a path via Streamlit's own CLI
    # (see supervisor.frozen_main's "streamlit" role), so nothing app.py
    # needs would otherwise get bundled. It's also listed under `datas`
    # below because Streamlit's script loader reads the file directly off
    # disk at a real path — bundling its bytecode into the archive alone
    # isn't enough for that lookup to succeed.
    [str(ENTRY_SCRIPT), str(APP_SCRIPT)],
    pathex=[str(ROOT)],
    binaries=[],
    datas=[(str(APP_SCRIPT), ".")]
    + [entry for pkg in METADATA_PACKAGES for entry in copy_metadata(pkg)]
    + [entry for pkg in DATA_PACKAGES for entry in collect_data_files(pkg)],
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
