VENV := .venv
PYTHON := $(VENV)/bin/python3
PIP := $(VENV)/bin/pip
RELEASE_BIN := desktop/src-tauri/target/release/proxy-scaler-spike
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
CLIENT_ARCHIVE := dist/proxy-scaler-client_$(PKG_VERSION)_linux-$(PKG_ARCH).tar.gz
SERVER_APP_ARCHIVE := dist/proxy-scaler-server-app_$(PKG_VERSION)_linux-$(PKG_ARCH).tar.gz
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
	release release-client-archive release-server-app-archive \
	init-db api-dev worker-dev frontend-install frontend-dev frontend-build

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
	@echo "server-app       Build the server app (run 'make sidecar' first)"
	@echo "server-app-run   Launch the already-built server app"
	@echo "server-app-dev   Run the server app via cargo tauri dev"
	@echo ""
	@echo "--- headless server packaging (Linux only) ---"
	@echo "deb              Build the .deb server package into dist/ (run 'make sidecar' first)"
	@echo ""
	@echo "--- everything at once, ready to upload (Linux only) ---"
	@echo "release          sidecar + client + server-app + deb, all landing in dist/ --"
	@echo "                 this is what you want for a GitHub release. Client/server-app"
	@echo "                 come out as .tar.gz (binary + its sidecar folder), since"
	@echo "                 Tauri's own installer bundling (.dmg/.msi/.AppImage) isn't"
	@echo "                 wired up in this repo yet -- see tauri.conf.json's bundle.active"
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

# One command, everything upload-ready in dist/. sidecar listed first and
# explicitly (rather than left implicit via the other three's own prereqs)
# so it's guaranteed to run exactly once, before any of them, even though
# 'deb' itself deliberately has no sidecar prerequisite of its own (see its
# comment above) — that's still true for a standalone 'make deb', this
# just orders around it here. Not safe under `make -j`: relies on plain
# left-to-right prerequisite ordering, which parallel make doesn't honor.
release: sidecar release-client-archive release-server-app-archive deb
	@echo ""
	@echo "release artifacts:"
	@ls -1 dist/

# Client binary + its sidecar folder, tarred together the same way you'd
# hand someone the whole target/release/ directory — there's no installer
# bundle to produce instead (bundle.active is off, see tauri.conf.json).
release-client-archive: sidecar build
	mkdir -p dist
	tar czf $(CLIENT_ARCHIVE) -C desktop/src-tauri/target/release \
		proxy-scaler-spike proxy-scaler-serve
	@echo "built: $(CLIENT_ARCHIVE)"

# Same idea as release-client-archive, for the status-window server app.
release-server-app-archive: sidecar server-app
	mkdir -p dist
	tar czf $(SERVER_APP_ARCHIVE) -C desktop/server-app/target/release \
		proxy-scaler-server proxy-scaler-serve
	@echo "built: $(SERVER_APP_ARCHIVE)"

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
	$(VENV)/bin/uvicorn proxy_scaler.api:app --reload --host $(HOST) --port $(PORT)

worker-dev: init-db
	$(VENV)/bin/python -m proxy_scaler.worker

frontend-install:
	cd desktop/frontend && npm install

frontend-dev:
	cd desktop/frontend && npm run dev

frontend-build:
	cd desktop/frontend && npm run build
