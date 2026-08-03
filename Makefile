VENV := .venv
PYTHON := $(VENV)/bin/python3
PIP := $(VENV)/bin/pip
RELEASE_BIN := desktop/src-tauri/target/release/proxy-scaler-spike
SIDECAR_RESOURCE_DIR := desktop/src-tauri/resources/proxy-scaler-serve

.PHONY: help install test serve sidecar sidecar-clean \
	build run desktop \
	api-dev worker-dev frontend-install frontend-dev frontend-build

help:
	@echo "--- packaged app (no hot reload -- this is the real build) ---"
	@echo "build            Build the packaged app (assumes sidecar is already fresh;"
	@echo "                 run 'make sidecar' first if Python code changed)"
	@echo "run              Launch the already-built packaged app"
	@echo "sidecar          Freeze the Python API+worker into a bundled resource directory"
	@echo "sidecar-clean    Remove built sidecar artifacts (stale resources, dist/build dirs)"
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
	rm -rf desktop/src-tauri/resources
	rm -rf desktop/src-tauri/target/debug/_internal

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
	mkdir -p $(SIDECAR_RESOURCE_DIR)
	# -L (dereference symlinks): PyInstaller's onedir output commonly
	# includes versioned .dylib/.so symlinks (torch's shared-library deps
	# in particular). Tauri's bundle.resources copier has multiple open
	# upstream bugs around symlinks inside resource directories (e.g.
	# tauri-apps/tauri#13219, #5831) — copying the real file content
	# instead of preserving the symlink sidesteps that whole class of
	# issue rather than depending on it being fixed upstream.
	rsync -aL --delete desktop/pyinstaller/dist/proxy-scaler-serve/ $(SIDECAR_RESOURCE_DIR)/
	@echo "sidecar resources built: $(SIDECAR_RESOURCE_DIR)"

# The real packaged-app build: compiles main.rs and (via tauri.conf.json's
# beforeBuildCommand) rebuilds the frontend automatically. Does NOT
# refreeze the sidecar — run 'make sidecar' first if Python changed, or
# this will happily package a stale one.
build:
	cd desktop/src-tauri && cargo tauri build

run:
	./$(RELEASE_BIN)

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
