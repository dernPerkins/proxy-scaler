# Desktop shell (Tauri)

Phase 0 (validated ✅ — native webview holds a Streamlit websocket through a
real long-running GPU job with no drops or stuck UI) is done. This is now
the real Phase 2 shell: a first-launch Local/Remote picker, and — in Local
mode — the Phase 1 `proxy-scaler-serve` supervisor spawned as a Tauri
sidecar instead of a manually-started `streamlit run`.

**Heads up**: the Rust code here (`src-tauri/src/main.rs`) was written
without a Rust/Tauri toolchain available to compile against — expect a
round or two of compiler-error fixes on your machine, the same way the
icon path needed correcting earlier. The exact `CommandEvent` variant
names and a couple of Tauri plugin-shell API details are the most likely
spots to need small adjustments.

## 1. Build the sidecar binary (required even for `cargo tauri dev`)

Sidecars aren't special-cased in dev mode — Tauri looks for a real frozen
binary at `src-tauri/binaries/proxy-scaler-serve-<target-triple>[.exe]`
whether you're running `cargo tauri dev` or a packaged build. PyInstaller
freezes are platform-specific (no cross-compiling from another OS), so
this has to be built on the same machine/OS you're testing on.

```bash
# from the repo root, using the existing project venv
.venv/bin/pip install pyinstaller pyinstaller-hooks-contrib
.venv/bin/pyinstaller desktop/pyinstaller/proxy-scaler-serve.spec \
  --distpath desktop/pyinstaller/dist \
  --workpath desktop/pyinstaller/build
```

This is a one-folder build (not `--onefile` — discouraged for large ML
bundles, it re-unpacks to a temp dir on every launch), so the output is a
whole folder: `desktop/pyinstaller/dist/proxy-scaler-serve/` containing
the executable plus torch/streamlit/etc. Expect this step to take a while
and produce a multi-GB folder.

Now place it where Tauri's sidecar lookup expects it, renaming *only* the
main executable to include your platform's target triple (everything else
in the folder keeps its original name — the frozen deps need to sit right
next to it):

```bash
mkdir -p desktop/src-tauri/binaries
cp -r desktop/pyinstaller/dist/proxy-scaler-serve/* desktop/src-tauri/binaries/

# find your target triple:
rustc -vV | grep host
# e.g. "host: aarch64-apple-darwin" on an M-series Mac

# rename just the executable to match (adjust the triple to what you got above):
mv desktop/src-tauri/binaries/proxy-scaler-serve \
   desktop/src-tauri/binaries/proxy-scaler-serve-aarch64-apple-darwin
```

(On Windows the executable is `proxy-scaler-serve.exe` → rename to
`proxy-scaler-serve-<triple>.exe`.)

## 2. Run it

```bash
cd desktop/src-tauri
cargo tauri dev
```

A window opens showing the first-launch picker: **Use this device**
(Local — spawns the sidecar, waits for it to report ready, then navigates
to it) or **Connect to a server** (Remote — asks for an IP/hostname, no
sidecar involved, navigates straight to `http://<host>:8501`). The choice
is remembered via `localStorage`; to change it later, clear it manually
(devtools → Application → Local Storage) — a proper in-app Settings
view is a nice-to-have, not built yet.

## 3. What to test

- **Local mode**: does the window transition from "Connecting…" to the
  real app once the sidecar reports ready? Check the terminal running
  `cargo tauri dev` for `[proxy-scaler-serve] PROXY_SCALER_READY` and any
  stderr output if it hangs or errors.
- **Closing the window**: this is the actual point of the stdin-handshake
  work in `main.rs` — confirm that closing the window actually stops both
  the sidecar *and* the Streamlit/worker processes underneath it, not just
  the visible window. Check `ps aux | grep -E "streamlit|proxy_scaler"`
  (or Activity Monitor on macOS) right after closing — nothing should be
  left running.
- **Remote mode**: point it at another proxy-scaler instance (or even
  `127.0.0.1` if you have one running manually via `streamlit run app.py`
  in a separate terminal) and confirm it connects with no sidecar spawned.

## 4. Notes

- `bundle.active` is still `false` — no installers/code-signing yet,
  that's Phase 3.
- `security.csp` is `null` (disabled). Fine for now; should be scoped down
  before a real release build.
- Local/Remote config is deliberately plain `localStorage`, not
  `tauri-plugin-store` — the plan called out the store plugin, but for two
  small string fields it wasn't worth the extra Rust dependency surface,
  especially with no way to compile-test it here. Worth reconsidering if a
  real Settings UI grows more state later.
- `desktop/src/index.html` is no longer a placeholder — it's the actual
  first-launch UI now, loaded via `tauri.conf.json`'s window `url:
  "index.html"` (previously pointed straight at Streamlit for the Phase 0
  spike).
