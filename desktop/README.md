# Desktop shell (Tauri)

Streamlit is gone from this stack entirely. The window now loads a real
React + TypeScript + Vite frontend (`desktop/frontend/`) that talks to a
FastAPI backend (`proxy_scaler.api`) over HTTP — in Local mode, that API
server is spawned as a Tauri sidecar (`proxy-scaler-serve`, the same
`supervisor.py` as before, just managing an API server + worker instead
of Streamlit + worker); in Remote mode there's no sidecar, the frontend
just points its API client at a user-supplied host directly.

**Heads up**: none of the Rust (`main.rs`) or TypeScript (`frontend/src`)
code here has been run through a real `cargo tauri dev` or `vite build`
in this environment — Rust because there's no toolchain available at
all, TypeScript because it *does* type-check cleanly here (`npx tsc
--noEmit`, zero errors) but the sandbox's Node is too old to actually run
Vite (needs 18+). Expect a compiler-error round or two on your machine,
the same pattern every other native-toolchain piece in this project has
needed.

## 1. Build the frontend

```bash
cd desktop/frontend
npm install
npm run build
```

Produces `desktop/frontend/dist/`, which is what `tauri.conf.json`'s
`frontendDist` points at for a real `tauri build`. For day-to-day dev
(`cargo tauri dev`), you don't need this build — see step 3, which uses
Vite's dev server directly via `devUrl` instead.

## 2. Build the sidecar binary (required even for `cargo tauri dev`)

Sidecars aren't special-cased in dev mode — Tauri looks for a real frozen
binary at `src-tauri/binaries/proxy-scaler-serve-<target-triple>[.exe]`
whether you're running `cargo tauri dev` or a packaged build. PyInstaller
freezes are platform-specific (no cross-compiling from another OS), so
this has to be built on the same machine/OS you're testing on.

The easiest path is `make sidecar` from the repo root (see the root
`Makefile`) — it wraps exactly the commands below, detects your target
triple automatically, and clears stale artifacts first:

```bash
make sidecar
```

Equivalent by hand:

```bash
# from the repo root, using the existing project venv
.venv/bin/pip install pyinstaller pyinstaller-hooks-contrib
.venv/bin/pyinstaller desktop/pyinstaller/proxy-scaler-serve.spec \
  --distpath desktop/pyinstaller/dist \
  --workpath desktop/pyinstaller/build

mkdir -p desktop/src-tauri/binaries
rustc -vV | grep host   # e.g. "host: aarch64-apple-darwin" on an M-series Mac
cp desktop/pyinstaller/dist/proxy-scaler-serve \
   desktop/src-tauri/binaries/proxy-scaler-serve-aarch64-apple-darwin
```

(On Windows the executable is `proxy-scaler-serve.exe` → rename to
`proxy-scaler-serve-<triple>.exe`.)

This freeze is meaningfully simpler now than it was with Streamlit as the
sidecar's child — no more `copy_metadata`/`collect_data_files` for
Streamlit's version lookup and bundled static frontend, no more bundling
`app.py` as a second Analysis script, no more `magic_funcs` hiddenimport.
See the comment at the top of `desktop/pyinstaller/proxy-scaler-serve.spec`
if a similar "PackageNotFoundError: No package metadata" surfaces for
`fastapi`/`uvicorn`/`pydantic` — the fix pattern is the same one that
solved it for Streamlit.

## 3. Run it

```bash
make sidecar   # if you haven't already
cd desktop/src-tauri
cargo tauri dev
```

`tauri.conf.json`'s `devUrl` (`http://localhost:5173`, Vite's default)
means `cargo tauri dev` expects Vite's dev server already running — start
it in a separate terminal first:

```bash
cd desktop/frontend
npm run dev
```

A window opens showing the first-launch picker: **Use this device**
(Local — spawns the sidecar, waits for it to report ready, then points
the app's API client at it) or **Connect to a server** (Remote — asks
for an IP/hostname, no sidecar involved). The choice is remembered via
`localStorage`; to change it later, clear it manually (devtools →
Application → Local Storage) — a proper in-app Settings view is a
nice-to-have, not built yet.

## 4. What to test

- **Local mode**: does the window transition from the picker to the real
  app once the sidecar reports ready? Check the terminal running
  `cargo tauri dev` for `[proxy-scaler-serve] PROXY_SCALER_READY` and any
  stderr output if it hangs or errors.
- **Generate + download a PDF end to end** — this is the actual point of
  the whole migration: the old `st.download_button` silently did nothing
  inside Tauri's webview (a WKWebView gap around the HTML `download`
  attribute); the new PDF endpoint returns a real file response and the
  frontend does `fetch()` → `blob()` → `<a download>`, which has none of
  that gap. Confirm a real PDF actually saves.
- **Closing the window**: confirm that closing the window actually stops
  both the sidecar *and* the API server/worker processes underneath it,
  not just the visible window. Check `ps aux | grep -E "uvicorn|proxy_scaler"`
  (or Activity Monitor on macOS) right after closing — nothing should be
  left running.
- **Remote mode**: point it at another proxy-scaler instance (or even
  `127.0.0.1` if you have one running manually via
  `.venv/bin/uvicorn proxy_scaler.api:app` in a separate terminal) and
  confirm it connects with no sidecar spawned.

## 5. Notes

- `bundle.active` is still `false` — no installers/code-signing yet,
  that's still ahead.
- `security.csp` is `null` (disabled). Fine for now; should be scoped down
  before a real release build.
- Local/Remote config is deliberately plain `localStorage`, not
  `tauri-plugin-store` — for two small string fields it wasn't worth the
  extra Rust dependency surface, especially with no way to compile-test it
  here. Worth reconsidering if a real Settings UI grows more state later.
- `desktop/src/index.html` (the old plain-JS picker) is now dead code —
  `tauri.conf.json`'s `frontendDist`/`devUrl` point at
  `desktop/frontend/` instead. Left in place rather than deleted until the
  new React picker (`desktop/frontend/src/ConnectGate.tsx`) is confirmed
  working end-to-end on a real machine.
