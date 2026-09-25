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
# explicit hookspath needed. torch_directml has no such hook (in
# pyinstaller-hooks-contrib or anywhere else), so it's collected
# explicitly below when present.
import importlib.util
import sys
from pathlib import Path

from PyInstaller.utils.hooks import collect_all, collect_dynamic_libs

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

# torch_directml (GPU_VARIANT=directml builds only — see the Makefile) has
# no PyInstaller hook anywhere, and Analysis alone gets it wrong: the
# pure-Python modules land in the PYZ, but DirectML.dll — loaded at
# runtime via torch.ops.load_library, invisible to binary dependency
# analysis — and the torch_directml_native extension's support files are
# left behind. The installed app then dies in upscale.py::resolve_device
# with "Failed to load dynlib/dll '..._internal/torch_directml/
# DirectML.dll'". collect_all() sweeps the whole installed package
# (DLLs, data, submodules); the find_spec guard keeps default/rocm
# builds, where the package simply isn't installed, unaffected.
BINARIES = []
DATAS = []
if importlib.util.find_spec("torch_directml") is not None:
    dml_datas, dml_binaries, dml_hiddenimports = collect_all("torch_directml")
    DATAS += dml_datas
    BINARIES += dml_binaries
    HIDDEN_IMPORTS += dml_hiddenimports

# torchvision's C++ ops extension is loaded by path via
# torch.ops.load_library (torchvision/extension.py), not imported, so the
# static import graph never sees it. The contrib hook papers over that
# with a `torchvision._C` hiddenimport -- which stopped matching anything
# when torchvision 0.29 renamed the file to `_C_stable.so` (and
# `image.so` to `image_stable.so`). The freeze then merely WARNS ("Hidden
# import 'torchvision._C' not found!") and ships torchvision with no
# extension at all. torchvision swallows the load failure, and the first
# `import torchvision` -- which is lazy, inside UpscaleWorker.upscale() --
# dies in _meta_registrations with "operator torchvision::nms does not
# exist", so every task fails and nothing surfaces until a user reports
# it (v0.3.0/v0.3.1 on every Linux variant; v0.2.1 still had 0.28's
# `_C.so`). Sweeping the package's shared libraries by glob is immune to
# the next rename -- but only with explicit patterns: collect_dynamic_libs'
# defaults are `lib*.so`/`*.dll`/`*.dylib`, which match neither `_C*.so`
# nor a Windows `_C*.pyd`, so without them it silently returns [] (that
# was the first attempt at this fix). _sidecar-freeze in the Makefile
# double-checks the result so a future miss fails the build instead of
# the user.
BINARIES += collect_dynamic_libs(
    "torchvision", search_patterns=["*.so", "*.pyd", "*.dll", "*.dylib"]
)

# ncnn: the Vulkan inference backend (proxy_scaler/ncnn_backend.py), a
# declared dependency on every variant. Like torch_directml it has no
# contrib hook, and its whole runtime is one extension module
# (ncnn/ncnn.cpython-*.so / ncnn.*.pyd) plus, on Linux and Windows, the
# wheel's vendored libgomp / msvcp140+vcomp140 that the dependency scan
# reaches through it. collect_all sweeps the package; the explicit-pattern
# collect_dynamic_libs is the same belt-and-braces the torchvision fix
# needed, since the module's name matches none of the default globs.
# Unguarded on purpose: a build without ncnn must fail here, not ship a
# "Vulkan Models" group that can't run. _sidecar-freeze double-checks.
ncnn_datas, ncnn_binaries, ncnn_hiddenimports = collect_all("ncnn")
DATAS += ncnn_datas
BINARIES += ncnn_binaries
HIDDEN_IMPORTS += ncnn_hiddenimports
BINARIES += collect_dynamic_libs("ncnn", search_patterns=["*.so", "*.pyd", "*.dll", "*.dylib"])

# onnxruntime (the onnxruntime-webgpu package): the WebGPU runtime behind
# UltraSharpV2/IllustrationJaNai on any GPU. Linux + Windows only (no macOS
# package), hence the find_spec guard. Its capi/ folder holds the runtime
# library and, on Windows, dxcompiler.dll + dxil.dll (the D3D12 shader
# compiler Dawn loads by path); collect_all + the explicit patterns make
# sure none of them is left behind. _sidecar-freeze double-checks.
if importlib.util.find_spec("onnxruntime") is not None:
    ort_datas, ort_binaries, ort_hiddenimports = collect_all("onnxruntime")
    DATAS += ort_datas
    BINARIES += ort_binaries
    HIDDEN_IMPORTS += ort_hiddenimports
    BINARIES += collect_dynamic_libs(
        "onnxruntime", search_patterns=["*.so", "*.so.*", "*.pyd", "*.dll"]
    )

# macOS: ncnn dlopens libMoltenVK.dylib by bare name and the wheel doesn't
# ship it, so the Vulkan models would silently run on the CPU. We bundle
# the pinned MoltenVK release (Apache-2.0; `make molten-vk` fetches it into
# tools/molten-vk/) as a plain data file under _internal/ncnn-vulkan/,
# where ncnn_backend._preload_moltenvk() loads it by absolute path before
# ncnn's own lookup. A data file, not a binary: PyInstaller must not
# rewrite its install name or chase its (system-only) dependencies.
if sys.platform == "darwin":
    MOLTENVK = ROOT / "tools" / "molten-vk" / "libMoltenVK.dylib"
    if not MOLTENVK.is_file():
        raise SystemExit(
            f"{MOLTENVK} is missing -- run `make molten-vk` first (the Vulkan "
            "models need MoltenVK bundled on macOS; see docs/releasing.md)"
        )
    DATAS += [(str(MOLTENVK), "ncnn-vulkan")]

a = Analysis(
    [str(ENTRY_SCRIPT)],
    pathex=[str(ROOT)],
    binaries=BINARIES,
    datas=DATAS,
    hiddenimports=HIDDEN_IMPORTS,
    hookspath=[],
    hooksconfig={},
    runtime_hooks=[],
    excludes=[],
    noarchive=False,
)

# Linux: never bundle the C++ runtime (libstdc++ / libgcc_s). Every frozen
# process runs with LD_LIBRARY_PATH pointed at _internal/, so a bundled
# copy doesn't just serve our own libraries — it shadows the system's for
# anything the process loads later, GPU drivers included. Arch's Mesa
# (2026) needs GLIBCXX_3.4.32+; the copy this build box would bundle stops
# at 3.4.30, so the Vulkan loader failed to load the AMD driver
# ("version `GLIBCXX_3.4.32' not found (required by libSPIRV-Tools.so)")
# and every Vulkan model silently ran on the CPU. Nothing we bundle needs
# more than GLIBCXX_3.4.22, which every supported distro exceeds, so the
# system's copy — always present, and exactly as new as its drivers —
# serves everything. _sidecar-freeze double-checks it stayed out.
if sys.platform.startswith("linux"):
    _SYSTEM_ONLY = ("libstdc++.so", "libgcc_s.so")
    a.binaries = [
        entry for entry in a.binaries
        if not Path(entry[0]).name.startswith(_SYSTEM_ONLY)
    ]
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
