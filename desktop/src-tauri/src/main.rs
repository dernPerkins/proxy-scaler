// The desktop shell. In Local mode this spawns proxy-scaler-serve (the
// supervisor, frozen via PyInstaller — see desktop/pyinstaller/) and waits
// for it to report ready, returning the local API server's base URL to the
// React frontend; in Remote mode there's no local process at all, the
// frontend just configures its API client to point at the user-supplied
// host directly. The Local/Remote choice itself is asked fresh on every
// launch (see ConnectGate.tsx), not persisted.
//
// proxy-scaler-serve is a PyInstaller onedir build (a plain directory,
// loaded directly off disk, no per-launch extraction) placed by `make
// sidecar` as a sibling of the compiled binary at
// target/release/proxy-scaler-serve/ — NOT via Tauri's externalBin
// (single-file only, which forced onefile mode: self-extracting its
// ~1GB+ torch/spandrel/torchvision bundle to a fresh temp dir on *every*
// launch, a real measured startup-time regression) and NOT via
// tauri.conf.json's bundle.resources either (tried that first; hit a
// reproducible crash — "Not a directory (os error 20)" — inside
// tauri-build 2.6.3's own copy_resources/ResourcePaths code walking this
// real, ~1500+ file bundle, that couldn't be resolved without a local
// Rust toolchain to step through it). This sidesteps Tauri's resource
// pipeline entirely: main.rs computes the path itself, relative to
// std::env::current_exe(), and the Makefile places files there directly.
// Revisit if/when bundle.active flips to true for real .app/.dmg
// packaging — this placement doesn't survive that step and needs its own
// solution then (afterBuildCommand copying into the bundle, most likely).
mod project_store;

use std::sync::Arc;
use std::time::Duration;

use tauri::{AppHandle, Manager, State, WindowEvent};
use tauri_plugin_dialog::DialogExt;
use tauri_plugin_shell::process::CommandEvent;
use tauri_plugin_shell::ShellExt;
use tokio::sync::{Mutex, Notify};

use project_store::{
    add_recent_host, clear_all_projects, create_project, delete_project, get_last_project_id,
    get_project, list_projects, list_recent_hosts, remove_card, remove_recent_host,
    set_decklist_text, set_last_project_id, update_project,
};

const READY_MARKER: &str = "PROXY_SCALER_READY";
const LOCAL_URL: &str = "http://127.0.0.1:8000";
const SHUTDOWN_GRACE: Duration = Duration::from_secs(12);

#[derive(Default)]
struct SidecarState {
    child: Mutex<Option<tauri_plugin_shell::process::CommandChild>>,
    exited: Arc<Notify>,
    /// Serializes start against stop. `stop_sidecar` deliberately takes
    /// the child out of `child` and releases that lock *before* waiting
    /// out its shutdown grace period, so without this a start arriving
    /// during those seconds would see an empty slot and spawn a second
    /// supervisor onto port 8000 while the first is still letting go of
    /// it. Only reachable now that the frontend can stop and restart the
    /// server mid-session (the Local/Remote toggle) — previously stop
    /// only ever ran on the way out of the process.
    transition: Mutex<()>,
}

#[tauri::command]
async fn start_local_server(
    app: AppHandle,
    state: State<'_, SidecarState>,
) -> Result<String, String> {
    // Held across the spawn only, then dropped before the readiness wait
    // below — a stop arriving mid-startup should be able to proceed
    // rather than block behind a 90s timeout.
    let transition = state.transition.lock().await;

    let mut guard = state.child.lock().await;
    if guard.is_some() {
        // Idempotent — fine if the frontend calls this more than once
        // (e.g. retrying after a slow first paint).
        return Ok(LOCAL_URL.to_string());
    }

    let exe_name = format!("proxy-scaler-serve{}", std::env::consts::EXE_SUFFIX);
    let exe_dir = std::env::current_exe()
        .map_err(|e| format!("failed to resolve current executable path: {e}"))?
        .parent()
        .ok_or_else(|| "current executable has no parent directory".to_string())?
        .to_path_buf();
    let exe_path = exe_dir.join("proxy-scaler-serve").join(exe_name);
    let (mut rx, child) = app
        .shell()
        .command(exe_path)
        .spawn()
        .map_err(|e| format!("failed to spawn local server: {e}"))?;
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

    drop(transition);

    match tokio::time::timeout(Duration::from_secs(90), ready.notified()).await {
        Ok(()) => Ok(LOCAL_URL.to_string()),
        Err(_) => Err("proxy-scaler-serve did not become ready within 90s".to_string()),
    }
}

/// Stops the local server without exiting the app — used by the
/// Local/Remote toggle, which shuts the sidecar down before pointing the
/// frontend at a remote host so the worker stops holding RAM/GPU for a
/// server nobody is talking to any more. Safe to call when nothing is
/// running (`stop_sidecar` no-ops on an empty slot), and a later
/// `start_local_server` spawns a genuinely fresh process.
#[tauri::command]
async fn stop_local_server(state: State<'_, SidecarState>) -> Result<(), String> {
    stop_sidecar(&state).await;
    Ok(())
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
    // Held for the whole shutdown, grace period included — see
    // SidecarState::transition for the double-spawn this prevents.
    let _transition = state.transition.lock().await;

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
        .invoke_handler(tauri::generate_handler![
            start_local_server,
            stop_local_server,
            save_file,
            create_project,
            list_projects,
            get_project,
            update_project,
            delete_project,
            clear_all_projects,
            set_decklist_text,
            remove_card,
            get_last_project_id,
            set_last_project_id,
            list_recent_hosts,
            add_recent_host,
            remove_recent_host
        ])
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
