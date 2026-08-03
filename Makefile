VENV := .venv
PYTHON := $(VENV)/bin/python3
PIP := $(VENV)/bin/pip
TARGET_TRIPLE := $(shell rustc -vV 2>/dev/null | sed -n 's/^host: //p')
SIDECAR_BIN := desktop/src-tauri/binaries/proxy-scaler-serve-$(TARGET_TRIPLE)

.PHONY: help install test serve sidecar sidecar-clean desktop frontend-install frontend-dev frontend-build

help:
	@echo "install          Create $(VENV) and pip install -e ."
	@echo "test             Run the full pytest suite"
	@echo "serve            Run the supervisor directly (proxy-scaler-serve)"
	@echo "sidecar          Freeze the desktop sidecar binary and place it for Tauri"
	@echo "sidecar-clean    Remove built sidecar artifacts (stale binaries, dist/build dirs)"
	@echo "desktop          Run the Tauri desktop app (cargo tauri dev)"
	@echo "frontend-install npm install in desktop/frontend"
	@echo "frontend-dev     Run the Vite dev server (needed alongside 'make desktop')"
	@echo "frontend-build   Build desktop/frontend/dist (needed for a real tauri build)"

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
	rm -f desktop/src-tauri/binaries/proxy-scaler-serve*
	rm -rf desktop/src-tauri/target/debug/_internal

# Onefile PyInstaller freeze (see desktop/pyinstaller/proxy-scaler-serve.spec
# for why not one-folder — Tauri's sidecar mechanism only manages a single
# named executable). Re-freezes torch every time, so this is slow — that's
# inherent to the approach, not something to optimize away here.
sidecar: sidecar-clean
	@test -n "$(TARGET_TRIPLE)" || { echo "error: couldn't detect a target triple — is rustc on PATH?"; exit 1; }
	$(PIP) install pyinstaller pyinstaller-hooks-contrib
	$(VENV)/bin/pyinstaller desktop/pyinstaller/proxy-scaler-serve.spec \
		--distpath desktop/pyinstaller/dist \
		--workpath desktop/pyinstaller/build
	mkdir -p desktop/src-tauri/binaries
	cp desktop/pyinstaller/dist/proxy-scaler-serve $(SIDECAR_BIN)
	@echo "sidecar built: $(SIDECAR_BIN)"

desktop:
	cd desktop/src-tauri && cargo tauri dev

frontend-install:
	cd desktop/frontend && npm install

frontend-dev:
	cd desktop/frontend && npm run dev

frontend-build:
	cd desktop/frontend && npm run build
