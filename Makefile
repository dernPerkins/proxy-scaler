VENV := .venv
PYTHON := $(VENV)/bin/python3
PIP := $(VENV)/bin/pip
RELEASE_BIN := desktop/src-tauri/target/release/proxy-scaler-spike
# Placed as a sibling of the compiled binary in *both* target profiles —
# main.rs finds it via std::env::current_exe()'s own directory at runtime,
# not through Tauri's bundle.resources/externalBin mechanisms (see
# main.rs's top-of-file comment for why: bundle.resources hit a
# reproducible crash in tauri-build 2.6.3's own resource-copying code on
# this real, ~1500+ file torch bundle). Both profiles are populated
# unconditionally so 'make build' (release) and 'make desktop'/dev mode
# (debug) each just work without having to know in advance which one
# you'll use.
SIDECAR_DEBUG_DIR := desktop/src-tauri/target/debug/proxy-scaler-serve
SIDECAR_RELEASE_DIR := desktop/src-tauri/target/release/proxy-scaler-serve

# The server app is a separate Tauri binary and finds its own copy the
# same way, so it needs the bundle staged next to it too. Two copies on
# disk is the accepted cost of the client keeping zero-setup local mode
# while the server ships independently.
SERVER_APP_DEBUG_DIR := desktop/server-app/target/debug/proxy-scaler-serve
SERVER_APP_RELEASE_DIR := desktop/server-app/target/release/proxy-scaler-serve
SERVER_APP_BIN := desktop/server-app/target/release/proxy-scaler-server

.PHONY: help install test serve sidecar sidecar-clean \
	build run desktop deb \
	server-app server-app-dev server-app-run \
	api-dev worker-dev frontend-install frontend-dev frontend-build

help:
	@echo "--- packaged app (no hot reload -- this is the real build) ---"
	@echo "build            Build the packaged app (assumes sidecar is already fresh;"
	@echo "                 run 'make sidecar' first if Python code changed)"
	@echo "run              Launch the already-built packaged app"
	@echo "sidecar          Freeze the Python API+worker, placed next to the compiled binary"
	@echo "sidecar-clean    Remove built sidecar artifacts (stale placements, dist/build dirs)"
	@echo ""
	@echo "--- fast dev loop (hot reload -- no Tauri, no PyInstaller) ---"
	@echo "api-dev          Run the API server with uvicorn --reload"
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
	@echo "server-app       Build the server app (run 'make sidecar' first)"
	@echo "server-app-run   Launch the already-built server app"
	@echo "server-app-dev   Run the server app via cargo tauri dev"
	@echo ""
	@echo "--- headless server packaging (Linux only) ---"
	@echo "deb              Build the .deb server package into dist/ (run 'make sidecar' first)"
	@echo ""
	@echo "--- misc ---"
	@echo "install          Create $(VENV) and pip install -e ."
	@echo "test             Run the full pytest suite"
	@echo "serve            Run the supervisor directly (proxy-scaler-serve)"
	@echo "frontend-install npm install in desktop/frontend"
	@echo "frontend-build   Build desktop/frontend/dist (bundled automatically by"
	@echo "                 'make build' too -- rarely needed standalone)"

$(VENV)/bin/python3:
	python3 -m venv $(VENV)

install: $(VENV)/bin/python3
	$(PYTHON) -m pip install --upgrade pip
	$(PIP) install -e .

test:
	$(VENV)/bin/pytest tests/ -q

serve:
	$(VENV)/bin/proxy-scaler-serve

sidecar-clean:
	rm -rf desktop/pyinstaller/dist desktop/pyinstaller/build
	rm -rf $(SIDECAR_DEBUG_DIR) $(SIDECAR_RELEASE_DIR)
	rm -rf $(SERVER_APP_DEBUG_DIR) $(SERVER_APP_RELEASE_DIR)

# One-folder PyInstaller freeze (see desktop/pyinstaller/proxy-scaler-serve.spec
# for why onedir, not onefile — onefile self-extracts its ~1GB+ torch bundle
# to a fresh temp dir on *every* launch, a real measured startup-time cost;
# onedir is loaded directly off disk instead). Re-freezes torch every time,
# so this step itself is slow — that's inherent to the approach, not
# something to optimize away here. Only needed when Python code
# (proxy_scaler/*) changed; a Rust- or frontend-only change doesn't need
# this.
sidecar: sidecar-clean
	$(PIP) install pyinstaller pyinstaller-hooks-contrib
	$(VENV)/bin/pyinstaller desktop/pyinstaller/proxy-scaler-serve.spec \
		--distpath desktop/pyinstaller/dist \
		--workpath desktop/pyinstaller/build
	mkdir -p $(SIDECAR_DEBUG_DIR) $(SIDECAR_RELEASE_DIR) \
		$(SERVER_APP_DEBUG_DIR) $(SERVER_APP_RELEASE_DIR)
	# -L (dereference symlinks): PyInstaller's onedir output commonly
	# includes versioned .dylib/.so symlinks (torch's shared-library deps
	# in particular) — copying the real file content instead keeps this
	# a plain, boring directory of regular files, nothing fancier needed.
	rsync -aL --delete desktop/pyinstaller/dist/proxy-scaler-serve/ $(SIDECAR_DEBUG_DIR)/
	rsync -aL --delete desktop/pyinstaller/dist/proxy-scaler-serve/ $(SIDECAR_RELEASE_DIR)/
	rsync -aL --delete desktop/pyinstaller/dist/proxy-scaler-serve/ $(SERVER_APP_DEBUG_DIR)/
	rsync -aL --delete desktop/pyinstaller/dist/proxy-scaler-serve/ $(SERVER_APP_RELEASE_DIR)/
	@echo "sidecar placed for the client and the server app (debug + release)"

# The real packaged-app build: compiles main.rs and (via tauri.conf.json's
# beforeBuildCommand) rebuilds the frontend automatically. Does NOT
# refreeze the sidecar — run 'make sidecar' first if Python changed, or
# this will happily package a stale one.
build:
	cd desktop/src-tauri && cargo tauri build

run:
	./$(RELEASE_BIN)

# The server app: a status window that runs the generation server for
# other machines to connect to, and lives in the tray. Same sidecar
# staging story as the client, so 'make sidecar' covers both.
server-app:
	cd desktop/server-app && cargo tauri build

server-app-run:
	./$(SERVER_APP_BIN)

server-app-dev:
	cd desktop/server-app && cargo tauri dev

# Headless server package for Linux. Consumes the same PyInstaller onedir
# bundle 'sidecar' produces, so run that first. PyInstaller output is
# platform-specific: this only produces a working package when built on
# Linux, on the architecture you're targeting — it cannot be built from
# macOS. See packaging/build-deb.sh for the staging details.
deb:
	./packaging/build-deb.sh

# Tauri dev mode: Rust auto-rebuilds/relaunches on change, and the window
# loads Vite's dev server (devUrl in tauri.conf.json) for frontend HMR.
# Needs frontend-dev running in another terminal, and sidecar resources
# already in place (dev mode doesn't build them for you either).
desktop:
	cd desktop/src-tauri && cargo tauri dev

# --- fast dev loop: no Tauri, no PyInstaller, everything hot-reloads ---

api-dev:
	$(VENV)/bin/uvicorn proxy_scaler.api:app --reload

worker-dev:
	$(VENV)/bin/python -m proxy_scaler.worker

frontend-install:
	cd desktop/frontend && npm install

frontend-dev:
	cd desktop/frontend && npm run dev

frontend-build:
	cd desktop/frontend && npm run build
