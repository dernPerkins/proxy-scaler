# Desktop shell (Tauri)

The dev loop for the two Tauri apps in this repo. For producing shippable
installers, see [`docs/releasing.md`](../docs/releasing.md) instead.

- **`src-tauri/`** — the client: a React + TypeScript + Vite frontend in a
  Tauri window, talking to the FastAPI backend (`proxy_scaler.api`) over
  HTTP. In Local mode it spawns that API server itself as a sidecar; in
  Remote mode there's no local process at all and the frontend just points
  its API client at a user-supplied host.
- **`server-app/`** — the server app: a small status window + tray icon
  that runs the same generation server for *other* machines to connect to.
  Its UI is plain HTML (`server-app/ui/`), not the React frontend.

Project data (decklist text, parsed cards, settings) lives in a local
SQLite file owned by the Rust side (`src-tauri/src/project_store.rs`) —
no network, no separate process. See [`ARCHITECTURE.md`](../ARCHITECTURE.md).

## 1. Build the frontend

```bash
cd desktop/frontend
npm install
npm run build
```

Produces `desktop/frontend/dist/`, which `tauri.conf.json`'s
`frontendDist` points at for a real `tauri build`. For day-to-day dev you
don't need this — step 3 uses Vite's dev server via `devUrl` instead.

## 2. Build the sidecar (required even for `cargo tauri dev`)

Not special-cased in dev mode: `main.rs` looks for the frozen server at
`proxy-scaler-serve/proxy-scaler-serve[.exe]`, as a sibling of whichever
binary is running — `target/debug/` under `cargo tauri dev`,
`target/release/` under a real build — resolved at runtime via
`std::env::current_exe()`.

```bash
make sidecar     # from the repo root
```

That stages the PyInstaller output into all four locations (client and
server app, debug and release), so either mode just works without
deciding in advance. `make sidecar-release` does release-only, which is
what the release targets use.

PyInstaller freezes are platform-specific — no cross-compiling — so this
has to be built on the OS you're testing on.

### Why it's placed this way

This is a PyInstaller **onedir** build (a plain directory, loaded off
disk, no per-launch extraction) placed by the Makefile, deliberately
**not** via Tauri's `externalBin` (single-file only, which forces
PyInstaller into `onefile` mode: it self-extracts its entire bundle,
torch alone ~1GB+, to a fresh temp dir on *every single launch* — a real
measured startup cost).

`bundle.resources` was also rejected initially, for a reproducible
`Not a directory (os error 20)` crash inside `tauri-build`'s own
resource-copying code walking the ~1500-file bundle. That was later
root-caused to torch's versioned `.so`/`.dylib` symlinks, and feeding it
an already-dereferenced copy avoids it — which is why Windows now *does*
use `bundle.resources` (see `tauri.windows.conf.json`). macOS still
doesn't, because Tauri's `resource_dir()` there is `Contents/Resources/`
while the binary lives in `Contents/MacOS/`. The full per-platform story
is in the comment at the top of `src-tauri/src/main.rs`.

## 3. Run it

```bash
make sidecar     # if you haven't already
cd desktop/src-tauri && cargo tauri dev
```

`devUrl` (`http://localhost:5173`) means `cargo tauri dev` expects Vite
already running — start it in a separate terminal:

```bash
cd desktop/frontend && npm run dev
```

A window opens showing the picker on every launch: **Use this device**
(Local — spawns the sidecar, waits for `PROXY_SCALER_READY`, then points
the app at it) or **Connect to a server** (Remote — asks for an
IP/hostname, no sidecar involved). Deliberately not remembered across
launches; an earlier version did, and there was no way to switch modes
without clearing devtools storage.

For the server app: `make server-app-dev`, or `make server-app` then
`make server-app-run`.

## 4. What to test

- **Local mode**: does the window get from the picker to the real app once
  the sidecar reports ready? Watch the `cargo tauri dev` terminal for
  `[proxy-scaler-serve] PROXY_SCALER_READY`, and for stderr if it hangs.
- **Generate + download a PDF, and download a generated image.** Downloads
  go through Tauri's native save dialog (`main.rs::pick_save_path` +
  `download_to_path`, `frontend/src/download.ts`) rather than the
  browser — WKWebView on macOS
  doesn't reliably honor the HTML `download` attribute, so an image link
  navigated instead of saving and a PDF blob link did nothing. Confirm
  both prompt a real "Save As" and produce a file.
- **Projects survive a restart, named or not.** Import a decklist without
  naming anything, fully quit, relaunch — the cards *and* the settings
  should still be there. There is no Save button: settings write through
  on change. Then type a name and repeat.
- **Naming keeps the images.** Generate from an unnamed project, then name
  it. Every generated image must still be attached — naming is an `UPDATE`
  that preserves `project_tag`, and this is the check that proves it.
- **Closing the window stops everything.** After closing, check
  `ps aux | grep -E "uvicorn|proxy_scaler"` — the sidecar *and* the API
  server and worker underneath it should all be gone, not just the window.
- **Remote mode**: point it at another instance (even `127.0.0.1` with a
  manually-run `proxy-scaler-serve`) and confirm no sidecar is spawned.
- **Mid-session Local↔Remote switch** leaves project data untouched.

## Notes

- `security.csp` is `null` (disabled). Should be scoped down before a
  wider release.
- No code signing or notarization on any platform yet — see
  [`docs/releasing.md`](../docs/releasing.md) for what that means for
  macOS Gatekeeper in particular.
