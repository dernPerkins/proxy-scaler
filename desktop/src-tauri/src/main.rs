// The desktop shell. In Local mode this spawns proxy-scaler-serve (the
// supervisor, frozen via PyInstaller — see desktop/pyinstaller/) as a
// Tauri sidecar and waits for it to report ready, returning the local API
// server's base URL to the React frontend; in Remote mode there's no
// sidecar at all, the frontend just configures its API client to point at
// the user-supplied host directly. The Local/Remote choice itself is
// asked fresh on every launch (see ConnectGate.tsx), not persisted.
use std::sync::Arc;
use std::time::Duration;

use tauri::{AppHandle, Manager, State, WindowEvent};
use tauri_plugin_dialog::DialogExt;
use tauri_plugin_shell::process::CommandEvent;
use tauri_plugin_shell::ShellExt;
use tokio::sync::{Mutex, Notify};

const READY_MARKER: &str = "PROXY_SCALER_READY";
const LOCAL_URL: &str = "http://127.0.0.1:8000";
const SHUTDOWN_GRACE: Duration = Duration::from_secs(12);

#[derive(Default)]
struct SidecarState {
    child: Mutex<Option<tauri_plugin_shell::process::CommandChild>>,
    exited: Arc<Notify>,
}

#[tauri::command]
async fn start_local_server(
    app: AppHandle,
    state: State<'_, SidecarState>,
) -> Result<String, String> {
    let mut guard = state.child.lock().await;
    if guard.is_some() {
        // Idempotent — fine if the frontend calls this more than once
        // (e.g. retrying after a slow first paint).
        return Ok(LOCAL_URL.to_string());
    }

    let sidecar = app
        .shell()
        .sidecar("proxy-scaler-serve")
        .map_err(|e| format!("failed to prepare sidecar: {e}"))?;
    let (mut rx, child) = sidecar
        .spawn()
        .map_err(|e| format!("failed to spawn sidecar: {e}"))?;
    *guard = Some(child);
    drop(guard);

    let ready = Arc::new(Notify::new());
    let ready_signal = ready.clone();
    let exited = state.exited.clone();

    tauri::async_runtime::spawn(async move {
        while let Some(event) = rx.recv().await {
            match event {
                CommandEvent::Stdout(line) => {
                    let text = String::from_utf8_lossy(&line);
                    print!("[proxy-scaler-serve] {text}");
                    if text.contains(READY_MARKER) {
                        ready_signal.notify_one();
                    }
                }
                CommandEvent::Stderr(line) => {
                    eprint!("[proxy-scaler-serve] {}", String::from_utf8_lossy(&line));
                }
                CommandEvent::Terminated(payload) => {
                    eprintln!("[proxy-scaler-serve] exited: {payload:?}");
                    exited.notify_waiters();
                    break;
                }
                _ => {}
            }
        }
    });

    match tokio::time::timeout(Duration::from_secs(90), ready.notified()).await {
        Ok(()) => Ok(LOCAL_URL.to_string()),
        Err(_) => Err("proxy-scaler-serve did not become ready within 90s".to_string()),
    }
}

/// Native "Save As" dialog + write to disk — replaces the browser
/// `<a download>` pattern for image/PDF downloads. Confirmed by real
/// testing that the HTML `download` attribute isn't reliably honored by
/// Tauri's webview on macOS (WKWebView): an image link just navigated to
/// a larger view of the image instead of saving it, and a PDF blob link
/// did nothing at all. The frontend already has the bytes in hand (via
/// its own `fetch()`, which works fine) — this command only handles the
/// save-to-disk half, so there's no need for an HTTP client on the Rust
/// side.
///
/// Returns Ok(true) if saved, Ok(false) if the user canceled the dialog
/// (not an error case — the frontend should just no-op on false).
#[tauri::command]
async fn save_file(app: AppHandle, suggested_name: String, data: Vec<u8>) -> Result<bool, String> {
    let chosen = app.dialog().file().set_file_name(&suggested_name).blocking_save_file();

    let Some(file_path) = chosen else {
        return Ok(false);
    };
    let path = file_path.into_path().map_err(|e| e.to_string())?;
    std::fs::write(&path, data).map_err(|e| e.to_string())?;
    Ok(true)
}

/// Graceful-then-forceful sidecar shutdown, mirroring supervisor.py's own
/// discipline: try the cooperative path first — write a line to the
/// sidecar's stdin, which supervisor.py's `_watch_stdin` treats as an
/// equivalent shutdown trigger to SIGTERM — and only fall back to a hard
/// kill if it doesn't exit in time.
///
/// Never `child.kill()` as the *first* move: Tauri documents that as a
/// hard kill (no graceful signal at all), which would skip supervisor.py's
/// own cleanup of the API server/worker entirely and orphan them
/// underneath it — the same failure mode the Python test suite already
/// caught once for a bare `proc.kill()` in its own test harness.
async fn stop_sidecar(state: &SidecarState) {
    let mut guard = state.child.lock().await;
    let Some(mut child) = guard.take() else {
        return;
    };
    drop(guard);

    let _ = child.write(b"shutdown\n");

    if tokio::time::timeout(SHUTDOWN_GRACE, state.exited.notified())
        .await
        .is_err()
    {
        let _ = child.kill();
    }
}

/// Shared by both shutdown triggers below — the window's close button and
/// Ctrl+C in whatever terminal is running `cargo tauri dev` / the packaged
/// app. Ctrl+C sends the *process* a SIGINT; that's a completely different
/// thing from a Tauri window-close event, and on_window_event alone never
/// sees it — so without this, stopping dev mode with Ctrl+C (a very
/// natural thing to do while iterating) skipped sidecar cleanup entirely
/// and left the API server/worker running, still holding the port for
/// the *next* run to collide with.
async fn shutdown_and_exit(app: AppHandle) {
    let state = app.state::<SidecarState>();
    stop_sidecar(&state).await;
    app.exit(0);
}

fn main() {
    tauri::Builder::default()
        .plugin(tauri_plugin_shell::init())
        .plugin(tauri_plugin_dialog::init())
        .manage(SidecarState::default())
        .invoke_handler(tauri::generate_handler![start_local_server, save_file])
        .setup(|app| {
            let app_handle = app.handle().clone();
            tauri::async_runtime::spawn(async move {
                if tokio::signal::ctrl_c().await.is_ok() {
                    shutdown_and_exit(app_handle).await;
                }
            });
            Ok(())
        })
        .on_window_event(|window, event| {
            if let WindowEvent::CloseRequested { api, .. } = event {
                api.prevent_close();
                let app = window.app_handle().clone();
                tauri::async_runtime::spawn(shutdown_and_exit(app));
            }
        })
        .run(tauri::generate_context!())
        .expect("error while running tauri application");
}
