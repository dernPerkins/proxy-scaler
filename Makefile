VENV := .venv
RELEASE_BIN := desktop/src-tauri/target/release/proxy-scaler-spike
# Used to gate the macos-bundle-*-sidecar targets (Make-level, not shell-
# level — a shell `if...exit 0` inside one recipe line only exits that
# line's own subshell, not the rest of the target's recipe, so this has
# to be a Make conditional to actually skip the later lines on non-macOS),
# and to locate the venv's executables (see VENV_BIN below).
UNAME_S := $(shell uname -s)

# A venv puts its executables in Scripts/ on Windows and bin/ everywhere
# else, and only Windows gives them an .exe suffix — so every target that
# runs something out of the venv has to go through VENV_BIN/EXE rather
# than hardcoding a path. Detected via uname, which under the Git Bash /
# MSYS2 shell this runs in on Windows reports MINGW*/MSYS*/CYGWIN*.
ifneq (,$(filter MINGW% MSYS% CYGWIN%,$(UNAME_S)))
IS_WINDOWS := 1
VENV_BIN := $(VENV)/Scripts
EXE := .exe
# Bootstrap interpreter, used once to create the venv. Not `python3` on
# Windows: that name is usually the Microsoft Store alias stub, which
# prints an ad and exits without creating anything. The `py` launcher is
# the reliable way to reach a real interpreter.
#
# Pin the version when it matters — `make install PY="py -3.12"`. The
# directml variant in particular needs one: torch-directml pins
# torch==2.4.1, which has no wheels for the newest Python releases, so a
# bare `py` (newest installed) will fail to resolve it.
PY ?= py
else
IS_WINDOWS :=
VENV_BIN := $(VENV)/bin
EXE :=
PY ?= python3
endif

PYTHON := $(VENV_BIN)/python$(EXE)
PIP := $(VENV_BIN)/pip$(EXE)
# api-dev runs uvicorn directly against the ASGI app, bypassing
# supervisor.py's own CLI (and PROXY_SCALER_SERVER_PORT/HOST) entirely —
# so it needs its own overrides rather than inheriting either of those.
# PORT matches supervisor.py's DEFAULT_PORT so the plain `make api-dev`
# case still lines up with what the frontend/desktop app expect by
# default. HOST mirrors --host; BIND_ALL mirrors --bind-all (see
# supervisor.py) for testing from another machine over Tailscale — same
# "the API is UNAUTHENTICATED" caveat applies here as it does there.
PORT ?= 13207
HOST ?= 127.0.0.1
ifdef BIND_ALL
HOST := 0.0.0.0
endif

# Shared by every artifact 'release' produces, so all three filenames stay
# in lockstep — the two tauri.conf.json files (client, server-app) happen
# to carry the same "0.1.0" today, but nothing enforces that; if they ever
# drift, this is the one that wins for naming purposes.
PKG_VERSION := $(shell sed -n 's/^version = "\(.*\)"/\1/p' pyproject.toml | head -1)
# dpkg-specific (amd64, not x86_64) purely for consistency with the .deb's
# own filename — these tarballs aren't Debian packages themselves, this
# just keeps all three artifact names readable side by side in dist/.
PKG_ARCH := $(shell dpkg --print-architecture 2>/dev/null || uname -m)

# Linux ships three GPU variants (cuda / cuda-legacy / rocm), so the
# artifacts carry a variant tag or they'd overwrite each other in dist/.
# The tag is read from the torch build actually installed in the venv --
# the sidecar freezes whatever's there, so deriving the name from
# GPU_VARIANT (a flag someone can forget) could mislabel an artifact;
# torch/version.py can't. Parsed with sed rather than imported: importing
# torch costs seconds on every make invocation, reading the file costs
# nothing. cu13x is the current default wheel line ("cuda"); cu12x is the
# older-driver build ("cuda-legacy", see GPU_VARIANT below); "unknown"
# means no venv/torch, in which case the sidecar build would fail first
# anyway.
TORCH_BUILD := $(shell sed -n "s/^__version__ = '.*+\([a-z0-9.]*\)'/\1/p" $(firstword $(wildcard $(VENV)/lib/python*/site-packages/torch/version.py) $(wildcard $(VENV)/Lib/site-packages/torch/version.py)) 2>/dev/null)
ifneq (,$(findstring rocm,$(TORCH_BUILD)))
LINUX_GPU_TAG := rocm
else ifneq (,$(findstring cu13,$(TORCH_BUILD)))
LINUX_GPU_TAG := cuda
else ifneq (,$(findstring cu12,$(TORCH_BUILD)))
LINUX_GPU_TAG := cuda-legacy
else ifneq (,$(TORCH_BUILD))
LINUX_GPU_TAG := $(TORCH_BUILD)
else
LINUX_GPU_TAG := unknown
endif

CLIENT_ARCHIVE := dist/proxy-scaler-client_$(PKG_VERSION)_linux-$(PKG_ARCH)-$(LINUX_GPU_TAG).tar.gz
SERVER_APP_ARCHIVE := dist/proxy-scaler-server-app_$(PKG_VERSION)_linux-$(PKG_ARCH)-$(LINUX_GPU_TAG).tar.gz
# Placed as a sibling of the compiled binary in *both* target profiles —
# main.rs finds it via std::env::current_exe()'s own directory at runtime.
# Windows now also gets it via tauri.windows.conf.json's bundle.resources
# (which resolves to the same directory there), but debug builds and
# macOS/Linux release builds still rely on this Makefile placement — see
# main.rs's top-of-file comment for the full per-platform story, including
# why bundle.resources was initially abandoned and what made it viable
# again. Both profiles are populated unconditionally so 'make build'
# (release) and 'make desktop'/dev mode (debug) each just work without
# having to know in advance which one you'll use.
SIDECAR_DEBUG_DIR := desktop/src-tauri/target/debug/proxy-scaler-serve
SIDECAR_RELEASE_DIR := desktop/src-tauri/target/release/proxy-scaler-serve

# The server app is a separate Tauri binary and finds its own copy the
# same way, so it needs the bundle staged next to it too. Two copies on
# disk is the accepted cost of the client keeping zero-setup local mode
# while the server ships independently.
SERVER_APP_DEBUG_DIR := desktop/server-app/target/debug/proxy-scaler-serve
SERVER_APP_RELEASE_DIR := desktop/server-app/target/release/proxy-scaler-serve
SERVER_APP_BIN := desktop/server-app/target/release/proxy-scaler-server

# Where 'cargo tauri build' actually puts the .app once bundle.active is
# on — productName-derived, must match tauri.conf.json's own "productName"
# for each app exactly. Only real on macOS; see macos-bundle-*-sidecar
# below.
CLIENT_APP_BUNDLE := desktop/src-tauri/target/release/bundle/macos/Proxy Scaler.app
SERVER_APP_BUNDLE := desktop/server-app/target/release/bundle/macos/Proxy Scaler Server.app
# Bundle basenames, spelled out rather than derived with $(notdir ...):
# these paths contain spaces, and notdir operates on whitespace-separated
# words, so it would be splitting the path apart and happening to
# reassemble it. Used as the name the .app is copied to inside the dmg
# staging dir — it's what the user sees in the dmg window.
CLIENT_APP_NAME := Proxy Scaler.app
SERVER_APP_NAME := Proxy Scaler Server.app

# macOS .dmg outputs. Built by hand from the *patched* .app rather than by
# Tauri's own dmg bundler — see macos-release-client for why that
# distinction is the whole point of these targets. arm64 vs x86_64 rather
# than PKG_ARCH's dpkg-flavored names, since dpkg isn't a thing here.
MACOS_ARCH := $(shell uname -m)

# Developer ID signing + notarization -- both optional. Left unset, the
# build ad-hoc signs exactly as before: runs locally, but downloaded
# copies still hit the Gatekeeper "could not verify" warning. Once the
# Apple Developer cert is in the Mac's login keychain, pass:
#   MACOS_SIGN_IDENTITY   the full identity string, e.g.
#                         "Developer ID Application: <name> (<TEAMID>)"
#                         -- list yours: security find-identity -v -p codesigning
#   MACOS_NOTARY_PROFILE  keychain profile made once via
#                         `xcrun notarytool store-credentials`
# Identity alone: Developer-ID-signed but unnotarized (local testing).
# Both: signed, notarized, stapled -- warning-free for users.
# See docs/releasing.md ("Developer ID & notarization").
MACOS_SIGN_IDENTITY ?=
MACOS_NOTARY_PROFILE ?=
CLIENT_DMG := dist/proxy-scaler-client_$(PKG_VERSION)_macos-$(MACOS_ARCH).dmg
SERVER_APP_DMG := dist/proxy-scaler-server-app_$(PKG_VERSION)_macos-$(MACOS_ARCH).dmg

# What the .dmg's window actually contains gets assembled here first: the
# .app plus a symlink to /Applications, so the volume opens as the
# drag-across install everyone expects rather than a lone .app icon with
# nowhere to drop it (hdiutil -srcfolder pointed straight at the .app can
# only ever produce the latter). Under target/ rather than dist/ so the
# transient multi-GB copy stays out of the upload directory, and on the
# same volume as the .app it's copying. See build_dmg below.
CLIENT_DMG_STAGE := desktop/src-tauri/target/release/dmg-stage
SERVER_APP_DMG_STAGE := desktop/server-app/target/release/dmg-stage

.PHONY: help install reinstall test serve sidecar sidecar-release _sidecar-freeze sidecar-clean \
	build run desktop deb \
	server-app server-app-dev server-app-run \
	macos-bundle-client-sidecar macos-bundle-server-app-sidecar \
	macos-release macos-release-client macos-release-server-app \
	release release-bg release-status release-client-archive release-server-app-archive \
	init-db api-dev worker-dev frontend-install frontend-dev frontend-build

help:
	@echo "--- packaged app (no hot reload -- this is the real build) ---"
	@echo "build            Build the packaged app (assumes sidecar is already fresh;"
	@echo "                 run 'make sidecar' first if Python code changed). On macOS,"
	@echo "                 follow with 'make macos-bundle-client-sidecar' for a runnable .app"
	@echo "run              Launch the already-built packaged app"
	@echo "sidecar          Freeze the Python API+worker, placed next to the compiled binary"
	@echo "sidecar-clean    Remove built sidecar artifacts (stale placements, dist/build dirs)"
	@echo ""
	@echo "--- fast dev loop (hot reload -- no Tauri, no PyInstaller) ---"
	@echo "api-dev          Run the API server with uvicorn --reload"
	@echo "                 (PORT=9001 make api-dev to override, default $(PORT);"
	@echo "                 BIND_ALL=1 make api-dev to bind 0.0.0.0 for remote testing"
	@echo "                 over Tailscale -- UNAUTHENTICATED, same caveat as"
	@echo "                 supervisor.py's --bind-all)"
	@echo "worker-dev       Run the background worker"
	@echo "frontend-dev     Run the Vite dev server -- open the printed localhost URL"
	@echo "                 in a plain browser tab; run all three of these together"
	@echo ""
	@echo "--- Tauri dev mode (hot reload for Rust/frontend, NOT Python) ---"
	@echo "desktop          Run the Tauri desktop app via cargo tauri dev"
	@echo "                 (needs frontend-dev running alongside it, and the"
	@echo "                 sidecar resource directory in place -- see 'sidecar' above)"
	@echo ""
	@echo "--- server app (Windows/macOS: status window + tray) ---"
	@echo "server-app       Build the server app (run 'make sidecar' first). On macOS, follow"
	@echo "                 with 'make macos-bundle-server-app-sidecar' for a runnable .app"
	@echo "server-app-run   Launch the already-built server app"
	@echo "server-app-dev   Run the server app via cargo tauri dev"
	@echo ""
	@echo "--- macOS installers (.dmg -- run 'make sidecar' first) ---"
	@echo "macos-release    Both .dmg installers into dist/ -- USE THIS, not a bare"
	@echo "                 'cargo tauri build': Tauri seals its own .dmg before the"
	@echo "                 sidecar can be copied in, producing an app that can't start"
	@echo "                 its server. See the target's comment in this Makefile"
	@echo "macos-release-client / macos-release-server-app   One at a time"
	@echo ""
	@echo "--- headless server packaging (Linux only) ---"
	@echo "deb              Build the .deb server package into dist/ (run 'make sidecar' first)"
	@echo ""
	@echo "--- everything at once, ready to upload (Linux only) ---"
	@echo "release          sidecar-release + client + server-app + deb, all landing in dist/"
	@echo "                 -- this is what you want for a GitHub release."
	@echo "release-bg       Same as 'release', but detached (setsid) so it survives an SSH"
	@echo "                 disconnect -- logs to dist/.release.log, exit code to"
	@echo "                 dist/.release.exit. Safe to close the terminal right after."
	@echo "release-status   Check progress/completion of a 'release-bg' run"
	@echo ""
	@echo "--- misc ---"
	@echo "install          Create $(VENV) and pip install -e ."
	@echo "                 Windows: pin the interpreter with PY=\"py -3.12\" if you"
	@echo "                 plan to build the directml variant -- torch-directml"
	@echo "                 pins torch==2.4.1, which has no wheels for the newest"
	@echo "                 Python releases"
	@echo "                 GPU_VARIANT=rocm|cuda-legacy|directml make install layers on an"
	@echo "                 alternate torch build (AMD-on-Linux / older-NVIDIA-drivers /"
	@echo "                 AMD-on-Windows) -- see the GPU_VARIANT comment above the"
	@echo "                 sidecar targets in this Makefile. Linux artifacts are named"
	@echo "                 for the variant found in the venv at build time"
	@echo "test             Run the full pytest suite"
	@echo "serve            Run the supervisor directly (proxy-scaler-serve)"
	@echo "frontend-install npm install in desktop/frontend"
	@echo "frontend-build   Build desktop/frontend/dist (bundled automatically by"
	@echo "                 'make build' too -- rarely needed standalone)"

$(PYTHON):
	$(PY) -m venv $(VENV)

install: $(PYTHON)
	$(PYTHON) -m pip install --upgrade pip
	$(PIP) install -e .
ifneq ($(GPU_VARIANT),)
	$(PIP) install $(TORCH_INSTALL_ARGS)
endif

# Switching GPU_VARIANT on an existing venv MUST go through this target,
# not 'install': pip sees torch already installed, says "Requirement
# already satisfied", and silently skips the alternate build (which once
# produced three byte-identical cuda releases wearing three different
# jobs). Even a forced reinstall isn't enough -- the previous variant's
# orphaned dep wheels (nvidia-*-cu13 etc.) would stay in site-packages,
# where PyInstaller's torch hook can sweep them into the freeze. A
# variant switch is a fresh venv, full stop.
reinstall:
	rm -rf $(VENV)
	$(MAKE) install

# -m rather than the pytest console script: pytest isn't a declared
# dependency of this project, so `pip install -e .` alone doesn't produce
# that script, and "No module named pytest" is a far more actionable error
# than a bare "command not found" on a path that was never going to exist.
test:
	$(PYTHON) -m pytest tests/ -q

serve:
	$(VENV_BIN)/proxy-scaler-serve$(EXE)

sidecar-clean:
	rm -rf desktop/pyinstaller/dist desktop/pyinstaller/build
	rm -rf $(SIDECAR_DEBUG_DIR) $(SIDECAR_RELEASE_DIR)
	rm -rf $(SERVER_APP_DEBUG_DIR) $(SERVER_APP_RELEASE_DIR)

# GPU_VARIANT picks which torch build 'install' layers on top of the base
# `pip install -e .` — a mutually-exclusive alternate build (not an
# additive extra, hence a Makefile-level choice rather than a pyproject.toml
# optional-dependency): default/unset = PyPI's default CUDA-enabled wheel
# (unchanged behavior); 'rocm' = AMD on Linux (resolve_device() in
# upscale.py needs no code for this — ROCm's HIP backend already reports
# through the same torch.cuda.* namespace CUDA uses); 'directml' = AMD (or
# any DirectX12 GPU) on Windows, where no ROCm build exists at all (see
# upscale.py's torch_directml branch). 'sidecar' never re-resolves
# dependencies itself, so the variant choice has to happen at install
# time — `GPU_VARIANT=rocm make install` into a FRESH venv, or
# `GPU_VARIANT=rocm make reinstall` to switch an existing one (see
# reinstall's comment for why plain 'install' can't switch variants).
GPU_VARIANT ?=
ifeq ($(GPU_VARIANT),rocm)
# rocm7.2 rather than an older line: it's the newest ROCm index carrying
# the same torch version as the default cu13x build (checked Aug 2026 --
# rocm6.4 stopped at torch 2.9.x).
TORCH_INSTALL_ARGS := torch torchvision --index-url https://download.pytorch.org/whl/rocm7.2
else ifeq ($(GPU_VARIANT),cuda-legacy)
# Same torch as the default build, compiled against CUDA 12.6 instead of
# 13.x: runs on the CUDA 12 driver line (~525+) for users who can't or
# won't move to the CUDA 13 drivers the default cu13x wheels require.
TORCH_INSTALL_ARGS := torch torchvision --index-url https://download.pytorch.org/whl/cu126
else ifeq ($(GPU_VARIANT),directml)
# torch-directml hard-pins these exact versions in its own package
# metadata — not a range, an exact pin (confirmed against the published
# wheel) — so a directml build can't float to a newer torch the way a
# default/rocm build might.
TORCH_INSTALL_ARGS := torch==2.4.1 torchvision==0.19.1 torch-directml
else
TORCH_INSTALL_ARGS :=
endif

# One-folder PyInstaller freeze (see desktop/pyinstaller/proxy-scaler-serve.spec
# for why onedir, not onefile — onefile self-extracts its ~1GB+ torch bundle
# to a fresh temp dir on *every* launch, a real measured startup-time cost;
# onedir is loaded directly off disk instead). Re-freezes torch every time,
# so this step itself is slow — that's inherent to the approach, not
# something to optimize away here. Only needed when Python code
# (proxy_scaler/*) changed; a Rust- or frontend-only change doesn't need
# this. Produces desktop/pyinstaller/dist/proxy-scaler-serve/ only —
# placing copies of it next to compiled binaries is sidecar/sidecar-release's
# own job below, so a standalone 'make deb' (which reads straight from this
# dist dir, see packaging/build-deb.sh) doesn't need either of those to run.
# Place a copy of the frozen sidecar at $(1). Hardlink first (free), fall
# back to a dereferencing recursive copy where hardlinks aren't supported.
# $(1) must not already exist — see the mkdir/rm dance in the callers.
#
# The rm -rf between the two attempts is load-bearing, not defensive
# fluff: `cp -al` doesn't abort on the first file it can't hardlink (e.g.
# every *.so on a filesystem that refuses hardlinks entirely, like a FUSE/
# NTFS-3G mount) — it keeps going, links whatever it can, and only then
# exits non-zero. That leaves $(1) already existing and partially
# populated by the time the fallback runs. `cp -RL SRC DEST` treats an
# *existing* DEST as "copy SRC into DEST", not "recreate DEST as SRC", so
# without clearing it first the fallback tries to nest the whole source
# tree at DEST/proxy-scaler-serve/ and collides with the same-named file
# the partial first attempt already placed there — "cannot overwrite
# non-directory ... with directory", not a hardlink error at all.
define stage_sidecar
cp -al desktop/pyinstaller/dist/proxy-scaler-serve $(1) \
		|| { rm -rf $(1); cp -RL desktop/pyinstaller/dist/proxy-scaler-serve $(1); }
endef

_sidecar-freeze: sidecar-clean
	$(PIP) install pyinstaller pyinstaller-hooks-contrib
	$(PYTHON) -m PyInstaller desktop/pyinstaller/proxy-scaler-serve.spec \
		--distpath desktop/pyinstaller/dist \
		--workpath desktop/pyinstaller/build

sidecar: _sidecar-freeze
	# Parent dirs only — each destination itself must NOT exist, so that
	# `cp SRC DEST` creates it *as* the copy rather than nesting the copy
	# inside it. _sidecar-freeze's sidecar-clean prerequisite has already
	# removed them; the rm -rf below just keeps this target safe to run on
	# its own.
	mkdir -p $(dir $(SIDECAR_DEBUG_DIR)) $(dir $(SIDECAR_RELEASE_DIR)) \
		$(dir $(SERVER_APP_DEBUG_DIR)) $(dir $(SERVER_APP_RELEASE_DIR))
	rm -rf $(SIDECAR_DEBUG_DIR) $(SIDECAR_RELEASE_DIR) \
		$(SERVER_APP_DEBUG_DIR) $(SERVER_APP_RELEASE_DIR)
	# `cp -al` (hardlink) with a `cp -RL` fallback, rather than rsync:
	# rsync simply isn't present in the Git Bash/MSYS environment this
	# runs under on Windows, and this is the one step standing between a
	# Windows checkout and a working packaged build. Both forms produce a
	# tree of real files with no symlinks in it — the property `rsync -aL`
	# was here for, since PyInstaller's onedir output commonly contains
	# versioned .dylib/.so symlinks for torch's shared libraries.
	# Hardlinks cost no I/O; the -RL fallback covers filesystems that
	# refuse them (some FUSE/network mounts), dereferencing as it copies.
	$(call stage_sidecar,$(SIDECAR_DEBUG_DIR))
	$(call stage_sidecar,$(SIDECAR_RELEASE_DIR))
	$(call stage_sidecar,$(SERVER_APP_DEBUG_DIR))
	$(call stage_sidecar,$(SERVER_APP_RELEASE_DIR))
	@echo "sidecar placed for the client and the server app (debug + release)"

# Release-only variant: skips the two debug-dir copies entirely (pure
# cargo-tauri-dev convenience that 'release's downstream targets never
# read) and hardlinks instead of dereferencing-copying the two it does
# need. A hardlink is indistinguishable from a real file to tar/dpkg-deb
# (same "no symlinks in the shipped tree" property rsync -aL was already
# giving us) but costs no actual I/O, unlike a multi-GB byte-for-byte
# copy — both release dirs live under desktop/*/target/, the same
# filesystem as the PyInstaller output, so this is safe. Falls back to a
# dereferencing copy per-target if hardlinking isn't supported on this
# filesystem (some FUSE/network mounts silently keep everything at link
# count 1) — see stage_sidecar.
sidecar-release: _sidecar-freeze
	@echo "==> [1/4] staging sidecar (release only)"
	mkdir -p $(dir $(SIDECAR_RELEASE_DIR)) $(dir $(SERVER_APP_RELEASE_DIR))
	rm -rf $(SIDECAR_RELEASE_DIR)
	$(call stage_sidecar,$(SIDECAR_RELEASE_DIR))
	rm -rf $(SERVER_APP_RELEASE_DIR)
	$(call stage_sidecar,$(SERVER_APP_RELEASE_DIR))
	@echo "sidecar placed for the client and the server app (release only)"

# The real packaged-app build: compiles main.rs and (via tauri.conf.json's
# beforeBuildCommand) rebuilds the frontend automatically. Does NOT
# refreeze the sidecar — run 'make sidecar' first if Python changed, or
# this will happily package a stale one. On macOS, follow with
# 'make macos-bundle-client-sidecar' to get a runnable .app (see its own
# comment for why that's a separate step, not automatic).
build:
	cd desktop/src-tauri && cargo tauri build

run:
	./$(RELEASE_BIN)

# Re-sign $(1) (an .app) ad-hoc. Must run AFTER the sidecar is copied in:
# `cargo tauri build` signs the bundle it produced, and dropping a
# multi-GB directory into Contents/Resources/ afterwards invalidates that
# signature — bundle contents are sealed by the signature, not ignored.
#
# What a broken signature costs us: Gatekeeper blocks a fresh install on
# another Mac (right-click -> Open works around it), and LaunchServices
# is entitled to refuse to register the app at all — which is what keeps
# it out of Spotlight, since LaunchServices discovers apps via Spotlight's
# index. Ad-hoc (`--sign -`) fixes the *validity* of the signature, not
# its *trust*: there's still no Developer ID and no notarization, so
# Gatekeeper's first-run prompt doesn't go away. That needs an Apple
# Developer account.
#
# --verify is where this fails loudly if it didn't take.
#
# Deliberately NOT --deep, on either command.
#
# --deep descends looking for nested code, and decides what counts as a
# nested *bundle* partly by directory name — anything dotted looks like a
# .framework/.bundle to it. PyInstaller's _internal/ is full of dotted
# directories (every `<pkg>-<version>.dist-info`), so --deep walks into
# e.g. websockets-16.1.1.dist-info, finds no Contents/Info.plist, and
# fails the whole run with "bundle format unrecognized, invalid, or
# unsuitable". That is not a fixable signing problem; it's --deep
# misreading Python packaging metadata as an app bundle.
#
# Signing the outer bundle alone is also the *correct* scope here, not
# just the working one: PyInstaller already ad-hoc signs the binaries it
# emits on macOS (it has to — arm64 refuses to execute unsigned code), so
# the sidecar's own Mach-O files arrive validly signed. The only thing
# the sidecar copy broke was the outer bundle's seal, which is exactly
# what this restores. Apple has deprecated --deep for signing anyway; the
# inside-out alternative is documented in docs/releasing.md should this
# ever stop being enough.
# With MACOS_SIGN_IDENTITY set, the ad-hoc re-seal is replaced by a full
# inside-out Developer ID pass (packaging/sign-macos.sh): every sidecar
# Mach-O re-signed with hardened runtime + entitlements, outer bundle
# last. Still no --deep, for the dist-info reason above.
define codesign_app
	$(if $(MACOS_SIGN_IDENTITY),\
	bash ./packaging/sign-macos.sh "$(1)" "$(MACOS_SIGN_IDENTITY)",\
	echo "==> ad-hoc re-signing the bundle (the sidecar copy invalidated Tauri's signature)" \
	&& codesign --force --sign - "$(1)" \
	&& codesign --verify --strict "$(1)" \
	&& echo "signed (ad-hoc, unnotarized): $(1)")
endef

# macOS only. Tauri v2 has no afterBundleCommand hook, so the sidecar has
# to be copied into the .app by hand, right after 'cargo tauri build'
# produces it.
#
# It goes in Contents/Resources/, NOT Contents/MacOS/ next to the binary.
# Apple's bundle format defines Contents/MacOS as executables-only and
# codesign enforces that: signing a bundle with a PyInstaller onedir tree
# in there fails outright with "code object is not signed at all" on the
# first non-code file it meets (a hyphenation dictionary, .dist-info
# metadata, ...). Contents/Resources is where non-code belongs, sealed by
# hash instead. main.rs checks both locations, so dev builds keep working
# with the sidecar as a plain sibling. A no-op with a clear message on any
# other OS. Plain
# copy (not a hardlink) since the .app may land on a different volume than
# target/release/proxy-scaler-serve (e.g. once DMG staging is involved).
macos-bundle-client-sidecar:
ifeq ($(UNAME_S),Darwin)
	@if [ ! -d "$(CLIENT_APP_BUNDLE)" ]; then \
		echo "error: $(CLIENT_APP_BUNDLE) not found -- run 'make build' first" >&2; \
		exit 1; \
	fi
	rm -rf "$(CLIENT_APP_BUNDLE)/Contents/Resources/proxy-scaler-serve"
	rsync -a --delete $(SIDECAR_RELEASE_DIR)/ "$(CLIENT_APP_BUNDLE)/Contents/Resources/proxy-scaler-serve/"
	@echo "sidecar copied into $(CLIENT_APP_BUNDLE)/Contents/Resources/"
	$(call codesign_app,$(CLIENT_APP_BUNDLE))
else
	@echo "macos-bundle-client-sidecar is a no-op outside macOS"
endif

# The server app: a status window that runs the generation server for
# other machines to connect to, and lives in the tray. Same sidecar
# staging story as the client, so 'make sidecar' covers both. On macOS,
# follow with 'make macos-bundle-server-app-sidecar' — see
# macos-bundle-client-sidecar's comment, identical reasoning.
server-app:
	cd desktop/server-app && cargo tauri build

macos-bundle-server-app-sidecar:
ifeq ($(UNAME_S),Darwin)
	@if [ ! -d "$(SERVER_APP_BUNDLE)" ]; then \
		echo "error: $(SERVER_APP_BUNDLE) not found -- run 'make server-app' first" >&2; \
		exit 1; \
	fi
	rm -rf "$(SERVER_APP_BUNDLE)/Contents/Resources/proxy-scaler-serve"
	rsync -a --delete $(SERVER_APP_RELEASE_DIR)/ "$(SERVER_APP_BUNDLE)/Contents/Resources/proxy-scaler-serve/"
	@echo "sidecar copied into $(SERVER_APP_BUNDLE)/Contents/Resources/"
	$(call codesign_app,$(SERVER_APP_BUNDLE))
else
	@echo "macos-bundle-server-app-sidecar is a no-op outside macOS"
endif

server-app-run:
	./$(SERVER_APP_BIN)

server-app-dev:
	cd desktop/server-app && cargo tauri dev

# --- macOS release: .dmg built AROUND Tauri's own dmg bundler ----------
#
# Tauri assembles the .app and then builds its .dmg from it, all inside one
# `cargo tauri build`. The sidecar can only be copied in *after* the .app
# exists (see macos-bundle-*-sidecar for why it can't go through
# bundle.resources), by which point Tauri's .dmg has already been sealed —
# from the unpatched .app. That .dmg installs an app with no
# proxy-scaler-serve/ directory at all, and the client dies at spawn()
# with "Couldn't start the local server". It was shipped exactly once,
# which is what these targets exist to prevent.
#
# So: build the .app target only, patch the sidecar in, then produce the
# .dmg ourselves from what's actually on disk. Nothing here is reachable
# by Tauri's own dmg step, deliberately.

# Build a .dmg whose window is the styled drag-to-install layout: app on
# the left, /Applications symlink on the right, branded background image
# with an arrow between them, fixed window size and icon positions.
#   $(1) .app to ship   $(2) staging dir   $(3) .app's basename
#   $(4) volume name    $(5) output .dmg   $(6) background png
#
# All the work lives in packaging/build-dmg-macos.sh. The styling needs a
# .DS_Store that only Finder can write, so the script mounts a read-write
# dmg and drives Finder over AppleScript before compressing — meaning it
# needs a logged-in GUI session, and the first run prompts for permission
# to control Finder (Privacy & Security > Automation). That fragility used
# to be the reason these targets shipped the plain unstyled window; the
# builds already require an interactive Mac for codesigning, so the
# constraint was being paid for anyway.
#
# The background pngs live in desktop/*/dmg/ and are drawn around the icon
# positions hardcoded in the script — regenerate or re-align them together.
define build_dmg
	@echo "==> building styled .dmg (background + drag-to-Applications layout)"
	bash ./packaging/build-dmg-macos.sh "$(1)" "$(2)" "$(3)" "$(4)" "$(5)" "$(6)"
	$(if $(MACOS_SIGN_IDENTITY),bash ./packaging/notarize-macos.sh "$(5)" "$(MACOS_SIGN_IDENTITY)" "$(MACOS_NOTARY_PROFILE)")
endef

macos-release-client:
ifeq ($(UNAME_S),Darwin)
	@echo "==> [1/3] building client .app (no dmg -- see this target's comment)"
	cd desktop/src-tauri && cargo tauri build --bundles app
	@echo "==> [2/3] patching sidecar into the .app (and re-signing it)"
	$(MAKE) macos-bundle-client-sidecar
	@echo "==> [3/3] building .dmg from the patched .app"
	$(call build_dmg,$(CLIENT_APP_BUNDLE),$(CLIENT_DMG_STAGE),$(CLIENT_APP_NAME),Proxy Scaler,$(CLIENT_DMG),desktop/src-tauri/dmg/dmg-background.png)
	@echo "built: $(CLIENT_DMG)"
else
	@echo "error: 'macos-release-client' must be run on macOS" >&2
	@exit 1
endif

macos-release-server-app:
ifeq ($(UNAME_S),Darwin)
	@echo "==> [1/3] building server app .app (no dmg)"
	cd desktop/server-app && cargo tauri build --bundles app
	@echo "==> [2/3] patching sidecar into the .app (and re-signing it)"
	$(MAKE) macos-bundle-server-app-sidecar
	@echo "==> [3/3] building .dmg from the patched .app"
	$(call build_dmg,$(SERVER_APP_BUNDLE),$(SERVER_APP_DMG_STAGE),$(SERVER_APP_NAME),Proxy Scaler Server,$(SERVER_APP_DMG),desktop/server-app/dmg/dmg-background.png)
	@echo "built: $(SERVER_APP_DMG)"
else
	@echo "error: 'macos-release-server-app' must be run on macOS" >&2
	@exit 1
endif

# Both macOS artifacts, assuming 'make sidecar' has already run. The
# macOS counterpart to Linux's 'release' -- deliberately not called
# 'release', which stays Linux-only (it ends in 'deb').
macos-release: macos-release-client macos-release-server-app
	@echo ""
	@echo "macOS release artifacts:"
	@ls -1 dist/*.dmg

# Headless server package for Linux. Consumes the same PyInstaller onedir
# bundle 'sidecar'/'sidecar-release' produces, so run one of those first.
# PyInstaller output is platform-specific: this only produces a working
# package when built on Linux, on the architecture you're targeting — it
# cannot be built from macOS. See packaging/build-deb.sh for the staging
# details.
# Guard rather than let these fail deep in the weeds. 'deb' needs dpkg-deb
# and a Linux PyInstaller bundle; 'release' additionally tars Linux
# binaries and ends in 'deb' — which, unguarded, meant a Windows or macOS
# run did the full multi-GB freeze and both app builds first and only then
# died on the very last step.
LINUX_ONLY_MSG = builds a Linux artifact and must be run on Linux. On \
Windows/macOS use 'make sidecar' then 'make build' / 'make server-app' \
for this platform's own installers.

deb:
ifneq ($(UNAME_S),Linux)
	@echo "error: 'deb' $(LINUX_ONLY_MSG)" >&2
	@exit 1
else
	@echo "==> [4/4] building .deb"
	GPU_TAG="$(LINUX_GPU_TAG)" ./packaging/build-deb.sh
endif

# One command, everything upload-ready in dist/. sidecar-release listed
# first and explicitly (rather than left implicit via the other three's
# own prereqs) so it's guaranteed to run exactly once, before any of them,
# even though 'deb' itself deliberately has no sidecar prerequisite of its
# own (see its comment above) — that's still true for a standalone
# 'make deb', this just orders around it here. Not safe under `make -j`:
# relies on plain left-to-right prerequisite ordering, which parallel make
# doesn't honor.
ifneq ($(UNAME_S),Linux)
release:
	@echo "error: 'release' $(LINUX_ONLY_MSG)" >&2
	@exit 1
else
release: sidecar-release release-client-archive release-server-app-archive deb
	@echo ""
	@echo "release artifacts:"
	@ls -1 dist/
endif

# Client binary + its sidecar folder, tarred together the same way you'd
# hand someone the whole target/release/ directory. Linux isn't a
# configured bundle.targets entry (see tauri.conf.json — Windows/macOS get
# real installers, Linux still ships as a tarball, see ARCHITECTURE.md/
# desktop README for why), so 'cargo tauri build' here just compiles the
# binary same as always, nothing installer-shaped to produce instead.
release-client-archive: sidecar-release build
	@echo "==> [2/4] archiving client"
	mkdir -p dist
	tar czf $(CLIENT_ARCHIVE) -C desktop/src-tauri/target/release \
		proxy-scaler-spike proxy-scaler-serve
	@echo "built: $(CLIENT_ARCHIVE)"

# Same idea as release-client-archive, for the status-window server app.
release-server-app-archive: sidecar-release server-app
	@echo "==> [3/4] archiving server app"
	mkdir -p dist
	tar czf $(SERVER_APP_ARCHIVE) -C desktop/server-app/target/release \
		proxy-scaler-server proxy-scaler-serve
	@echo "built: $(SERVER_APP_ARCHIVE)"

# Runs 'release' fully detached (setsid, its own session — not just
# backgrounded within this shell) so it survives an SSH disconnect. Logs
# to dist/.release.log, writes its exit code to dist/.release.exit when
# done; check either with 'make release-status'. Safe to close the
# terminal/SSH session immediately after this returns.
release-bg:
	@mkdir -p dist
	@rm -f dist/.release.exit dist/.release.pid
	@setsid sh -c '$(MAKE) release > dist/.release.log 2>&1; echo $$? > dist/.release.exit' < /dev/null & \
	echo $$! > dist/.release.pid ; \
	echo "release started in the background (PID $$(cat dist/.release.pid))" ; \
	echo "  check status: make release-status" ; \
	echo "  raw log:      tail -f dist/.release.log"

release-status:
	@if [ ! -f dist/.release.pid ]; then \
		echo "no release-bg run found (dist/.release.pid missing)"; \
	elif kill -0 "$$(cat dist/.release.pid)" 2>/dev/null; then \
		echo "still running (PID $$(cat dist/.release.pid)) -- last 20 lines of dist/.release.log:"; \
		tail -n 20 dist/.release.log 2>/dev/null || echo "(no output yet)"; \
	else \
		if [ -f dist/.release.exit ] && [ "$$(cat dist/.release.exit)" = "0" ]; then \
			echo "done -- release succeeded"; \
		else \
			echo "done -- release FAILED (exit $$(cat dist/.release.exit 2>/dev/null || echo unknown))"; \
		fi; \
		echo "last 20 lines of dist/.release.log:"; \
		tail -n 20 dist/.release.log 2>/dev/null || true; \
	fi

# Tauri dev mode: Rust auto-rebuilds/relaunches on change, and the window
# loads Vite's dev server (devUrl in tauri.conf.json) for frontend HMR.
# Needs frontend-dev running in another terminal, and sidecar resources
# already in place (dev mode doesn't build them for you either).
desktop:
	cd desktop/src-tauri && cargo tauri dev

# --- fast dev loop: no Tauri, no PyInstaller, everything hot-reloads ---

# db.init_db() (schema creation + _migrate's old-shape cleanup) is normally
# only run once by supervisor.py before it spawns the API/worker as
# children. api-dev/worker-dev bypass the supervisor entirely, so without
# this they'd silently run against whatever schema happens to already be
# on disk — a real gap: a data/proxy_scaler.db predating a migration (e.g.
# the project_tag reshape) breaks with "no such column" instead of
# self-healing. Idempotent and cheap, so unconditional on every dev-loop
# start costs nothing.
init-db:
	$(PYTHON) -c "from proxy_scaler import db; db.init_db()"

api-dev: init-db
	$(VENV_BIN)/uvicorn$(EXE) proxy_scaler.api:app --reload --host $(HOST) --port $(PORT)

worker-dev: init-db
	$(PYTHON) -m proxy_scaler.worker

frontend-install:
	cd desktop/frontend && npm install

frontend-dev:
	cd desktop/frontend && npm run dev

frontend-build:
	cd desktop/frontend && npm run build
