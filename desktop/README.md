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

## 2. Build the sidecar resources (required even for `cargo tauri dev`)

Not special-cased in dev mode — Tauri looks for the frozen server at
`src-tauri/resources/proxy-scaler-serve/proxy-scaler-serve[.exe]` whether
you're running `cargo tauri dev` or a packaged build. PyInstaller freezes
are platform-specific (no cross-compiling from another OS), so this has
to be built on the same machine/OS you're testing on.

This ships as a bundled Tauri **resource directory** (`bundle.resources`
in `tauri.conf.json`), not a sidecar/`externalBin` — `externalBin` only
supports a single executable file, which used to force PyInstaller into
`onefile` mode. Onefile self-extracts its entire bundle (torch alone runs
~1GB+) to a fresh temp directory on *every single launch* — a real,
measured startup-time cost. PyInstaller now builds `onedir` instead (a
plain directory, loaded directly off disk, no per-launch extraction), and
`main.rs` resolves + spawns it via `app.shell().command(path)` rather than
`app.shell().sidecar(name)`.

The easiest path is `make sidecar` from the repo root (see the root
`Makefile`) — it wraps exactly the commands below and clears stale
artifacts first:

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

mkdir -p desktop/src-tauri/resources/proxy-scaler-serve
rsync -a --delete desktop/pyinstaller/dist/proxy-scaler-serve/ \
  desktop/src-tauri/resources/proxy-scaler-serve/
```

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

A window opens showing the picker on every launch: **Use this device**
(Local — spawns the sidecar, waits for it to report ready, then points
the app's API client at it) or **Connect to a server** (Remote — asks
for an IP/hostname, no sidecar involved). Deliberately not remembered
across launches — asks fresh every time rather than silently reconnecting
to whatever was picked last.

## 4. What to test

- **Local mode**: does the window transition from the picker to the real
  app once the sidecar reports ready? Check the terminal running
  `cargo tauri dev` for `[proxy-scaler-serve] PROXY_SCALER_READY` and any
  stderr output if it hangs or errors.
- **Generate + download a PDF, and download a generated image, end to
  end** — this is the actual point of the whole migration: the old
  `st.download_button` silently did nothing inside Tauri's webview, and a
  first attempt at fixing it via a browser `fetch()` → `blob()` → `<a
  download>` pattern *also* failed on real testing (WKWebView on macOS
  doesn't reliably honor the HTML `download` attribute at all — an image
  link just navigated to a larger view instead of saving, a PDF blob link
  did nothing). The real fix goes through Tauri's native save-file dialog
  instead (`main.rs::save_file`, `desktop/frontend/src/download.ts`) —
  confirm both actually prompt a native "Save As" dialog and produce a
  real file on disk.
- **Saved projects survive an app restart** — a real shipped bug: the DB's
  default path was derived from `__file__`, which for a frozen
  PyInstaller onefile build resolves inside the per-launch temp
  extraction directory, wiped and recreated fresh every single run.
  Everything worked fine *within* one session and then silently reset on
  the next launch. Fixed via `db.default_data_dir()` (a stable,
  OS-conventional per-user directory — `~/Library/Application Support/
  proxy-scaler` on macOS) used both for the DB and, via
  `supervisor.py`'s frozen `ROOT`, as the API server/worker's working
  directory (so relative output/cache/weights paths land somewhere
  persistent too, not just the DB). Confirm: save a project, quit the
  app fully, relaunch, and it's still there.
- **Closing the window**: confirm that closing the window actually stops
  both the sidecar *and* the API server/worker processes underneath it,
  not just the visible window. Check `ps aux | grep -E "uvicorn|proxy_scaler"`
  (or Activity Monitor on macOS) right after closing — nothing should be
  left running.
- **Remote mode**: point it at another proxy-scaler instance (or even
  `127.0.0.1` if you have one running manually via
  `.venv/bin/uvicorn proxy_scaler.api:app` in a separate terminal) and
  confirm it connects with no sidecar spawned.
- **The picker asks fresh on every launch** — an earlier version
  remembered the last Local/Remote choice via `localStorage` and silently
  reconnected; that's gone now on purpose (real user feedback: there was
  no way to switch modes without manually clearing devtools storage).

## 5. Notes

- `bundle.active` is still `false` — no installers/code-signing yet,
  that's still ahead.
- `security.csp` is `null` (disabled). Fine for now; should be scoped down
  before a real release build.
- File downloads (`desktop/frontend/src/download.ts`) go through a
  Rust-side `save_file` command (`tauri-plugin-dialog` + `std::fs::write`)
  when running inside Tauri, with a plain browser `<a download>` fallback
  for the `npm run dev`-without-Tauri workflow, where the HTML attribute
  works fine. Untested against a real compiler as of this writing — expect
  the usual round of fixes.
- `desktop/src/index.html` (the old plain-JS picker) is now dead code —
  `tauri.conf.json`'s `frontendDist`/`devUrl` point at
  `desktop/frontend/` instead. Left in place rather than deleted until the
  new React picker (`desktop/frontend/src/ConnectGate.tsx`) is confirmed
  working end-to-end on a real machine.
