# Phase 0 spike: Tauri webview + Streamlit compatibility

This is deliberately minimal: no sidecar, no supervisor integration yet —
just a bare Tauri window pointed at a manually-started Streamlit instance.
The only thing it's testing is whether the OS-native webview (WebView2 on
Windows, WKWebView on macOS, WebKitGTK on Linux) holds a Streamlit
websocket connection through a real, long-running GPU job without dropping
or needing a reconnect. If this doesn't hold up, no shell language choice
(Tauri, Wails, pywebview) fixes it — it's a property of the underlying
native webview engine itself.

## 1. Prerequisites

Install these on whichever machine you're testing first (pick your daily
driver — Phase 0 only needs to pass once to de-risk the approach, though
you'll eventually want to confirm on each target OS):

- **Rust**: via [rustup.rs](https://rustup.rs)
- **Tauri CLI**: `cargo install tauri-cli --version "^2.0.0" --locked`
- **Platform webview deps**:
  - **Linux**: `webkit2gtk-4.1` + `libgtk-3-dev` (package name varies by
    distro — e.g. Debian/Ubuntu: `sudo apt install libwebkit2gtk-4.1-dev
    libgtk-3-dev libayatana-appindicator3-dev librsvg2-dev`)
  - **Windows**: WebView2 runtime — already preinstalled on Windows 11;
    otherwise grab the [Evergreen
    Bootstrapper](https://developer.microsoft.com/microsoft-edge/webview2/)
  - **macOS**: nothing extra, WKWebView ships with the OS
- The existing proxy-scaler Python venv, already set up for this repo
  (`.venv` with `streamlit`, `torch`, etc. installed)

## 2. Run it

From the repo root, in one terminal, start Streamlit manually (not the
supervisor — Phase 0 isolates the webview/websocket question from the
process-management question, which Phase 1 already solved separately):

```bash
.venv/bin/streamlit run app.py --server.headless true
```

Wait for `You can now view your Streamlit app in your browser` and confirm
it's on the default port 8501 (matches `tauri.conf.json`'s window `url`).

In a second terminal:

```bash
cd desktop/src-tauri
cargo tauri dev
```

A native window should open showing the proxy-scaler UI. If it opens
before Streamlit finished starting, you'll see a webview connection-error
page — just reload (Ctrl+R / Cmd+R) once Streamlit is up.

## 3. What to actually test

Run one full, real upscale job through the Tauri window — pick a card,
a real model, a real DPI, whatever most resembles genuine use. Watch for:

- Does the progress/status UI stay reactive the whole time, or does it
  ever look stuck/frozen mid-job?
- Any visible reconnect banner or dropped-connection indicator from
  Streamlit itself?
- After the job finishes, does the gallery/result update live without
  needing a manual page reload?

If all three look clean, the webview choice is validated and Phase 2 (the
sidecar-managed version, using the already-built `proxy-scaler-serve`
supervisor from Phase 1) is safe to build. If you *do* see drops, note
roughly how long into the job they happen (helps distinguish "long GPU job
idles the connection" from something Streamlit-version-specific).

## 4. Notes

- `bundle.active` is `false` in `tauri.conf.json` — this is `cargo tauri
  dev` only, no installer/icons needed yet. That's Phase 3.
- `security.csp` is `null`, which disables Tauri's default Content-Security-Policy.
  Fine for this local-only spike; Phase 2's real config should scope this
  down instead of leaving it fully open.
- `desktop/src/index.html` is an unused placeholder — the window's `url`
  in `tauri.conf.json` points straight at `http://127.0.0.1:8501`, so this
  file is never actually loaded. It exists only because Tauri's config
  validation requires `build.frontendDist` to resolve to a real directory.
