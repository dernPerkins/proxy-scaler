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

use tauri::{AppHandle, Emitter, Manager, State, WindowEvent};
use tauri_plugin_dialog::DialogExt;
use tauri_plugin_shell::process::CommandEvent;
use tauri_plugin_shell::ShellExt;
use tokio::sync::{Mutex, Notify};

use project_store::{
    add_recent_host, clear_all_projects, create_project, delete_project, get_last_project_id,
    get_project, import_decklist_text, list_projects, list_recent_hosts, remove_card,
    remove_recent_host, set_last_project_id, update_project,
};

const READY_MARKER: &str = "PROXY_SCALER_READY";
// The single source of truth for the local sidecar's port: passed to it
// explicitly as --port below, rather than assumed to match whatever
// supervisor.py's own DEFAULT_PORT happens to be baked in as. Those two
// used to just have to agree by convention — a real bug, not a
// hypothetical one: bumping DEFAULT_PORT in the Python source without
// re-freezing the sidecar (`make sidecar`) left an already-built app
// pointed at the new port while its embedded binary still listened on
// the old one, so the server visibly "started" but every API call
// silently failed to connect. 13207 itself is M-T-G by letter position —
// picked to dodge the usual 8000/8080/8888/9000/etc collisions.
const LOCAL_PORT: u16 = 13207;
const SHUTDOWN_GRACE: Duration = Duration::from_secs(12);

#[derive(Default)]
struct SidecarState {
    child: Mutex<Option<tauri_plugin_shell::process::CommandChild>>,
    exited: Arc<Notify>,
    /// Serializes start against stop. `stop_sidecar` deliberately takes
    /// the child out of `child` and releases that lock *before* waiting
    /// out its shutdown grace period, so without this a start arriving
    /// during those seconds would see an empty slot and spawn a second
    /// supervisor onto port 13207 while the first is still letting go of
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
        return Ok(local_url());
    }

    let exe_name = format!("proxy-scaler-serve{}", std::env::consts::EXE_SUFFIX);
    let exe_dir = std::env::current_exe()
        .map_err(|e| format!("failed to resolve current executable path: {e}"))?
        .parent()
        .ok_or_else(|| "current executable has no parent directory".to_string())?
        .to_path_buf();
    let exe_path = exe_dir.join("proxy-scaler-serve").join(exe_name);
    // Bound separately rather than inlined: an array literal mixing
    // `&'static str` and `&String` relies on coercion to unify, which is
    // needless risk for a build we can't check locally (same pitfall
    // server-app/src/main.rs's own spawn call already avoids this way).
    let port_arg = LOCAL_PORT.to_string();
    let (mut rx, child) = app
        .shell()
        .command(exe_path)
        // Explicit, not assumed: see LOCAL_PORT's own comment above for
        // the exact bug this prevents.
        .args(["--port", port_arg.as_str()])
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
        Ok(()) => Ok(local_url()),
        Err(_) => Err("proxy-scaler-serve did not become ready within 90s".to_string()),
    }
}

fn local_url() -> String {
    format!("http://127.0.0.1:{LOCAL_PORT}")
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

/// How often a running transfer reports back to the UI. A 100MB PDF
/// arrives in thousands of chunks; emitting an event per chunk would
/// flood the webview's IPC with far more updates than a progress bar can
/// use. Throttled by both time and volume so slow links still tick
/// visibly and fast ones don't spam.
const PROGRESS_INTERVAL: Duration = Duration::from_millis(120);
const PROGRESS_BYTES: u64 = 512 * 1024;

/// Ids of downloads the user has asked to abort. A set rather than a
/// single flag so a cancel can never land on the wrong transfer: the id
/// is minted per download by the frontend and echoed in every event.
#[derive(Default)]
struct DownloadCancel(Mutex<std::collections::HashSet<String>>);

#[derive(Clone, serde::Serialize)]
struct DownloadProgress {
    id: String,
    downloaded: u64,
    /// None when the server sends no content-length — the UI falls back
    /// to an indeterminate bar rather than inventing a denominator.
    total: Option<u64>,
}

/// Native "Save As" dialog, returning the chosen path (None = canceled).
///
/// Split out from the transfer below so the caller can ask *first* and
/// then do slow work: a PDF render takes tens of seconds server-side, and
/// interrupting the user with a file dialog after that wait — rather than
/// letting them choose up front and walk away — is the worse trade.
#[tauri::command]
async fn pick_save_path(app: AppHandle, suggested_name: String) -> Result<Option<String>, String> {
    // blocking_save_file() parks its calling thread on rx.recv() until the
    // native dialog resolves (tauri-plugin-dialog's own blocking_fn!
    // macro) — fine for a one-off call, but this command runs on Tauri's
    // shared async runtime, whose worker pool is small (CPU-core-sized)
    // and also backs every other async command in this file
    // (start_local_server, stop_local_server). Two downloads overlapping
    // before either dialog is dismissed — trivially easy to trigger, no
    // deliberate misuse required — is enough to exhaust that pool, after
    // which *no* async command can be scheduled at all: not more
    // downloads, not the Local/Remote server toggle. No error surfaces
    // either, since nothing ever runs to produce one — invoke() promises
    // on the JS side just never resolve, which is exactly what read like
    // "the app crashed with no error" and downloads "sitting in a weird
    // loop." spawn_blocking runs the dialog wait on Tokio's separate,
    // much larger dedicated blocking-thread pool instead, so the shared
    // async pool is never at risk regardless of how many downloads
    // overlap.
    let chosen = tauri::async_runtime::spawn_blocking(move || {
        app.dialog().file().set_file_name(&suggested_name).blocking_save_file()
    })
    .await
    .map_err(|e| e.to_string())?;

    let Some(file_path) = chosen else {
        return Ok(None);
    };
    let path = file_path.into_path().map_err(|e| e.to_string())?;
    Ok(Some(path.to_string_lossy().into_owned()))
}

/// Fetch `url` straight to `path`, emitting `download-progress` as it
/// goes. Returns Ok(false) if the user canceled mid-transfer.
///
/// The HTTP request happens *here*, not in the webview, and that split is
/// the whole point. An earlier shape of this command took the bytes as a
/// `Vec<u8>` parameter, which meant the frontend had to hand a whole
/// image across the IPC boundary: `Array.from(uint8array)` expanded a
/// 15-25MB PNG (real sizes for this app's 1200 DPI output) into a JS
/// array of 15-25 *million* numbers, which Tauri then JSON-serialized to
/// ~60-100MB of `"255,17,3,..."` text for serde to parse back into bytes
/// on this side. That read to a user as a multi-minute hang, an app that
/// "crashed with no error," or an intermittent failure that tracked file
/// size (a ~1MB Scryfall original usually squeaked through; a 1200 DPI
/// upscale did not). The pre-Streamlit-migration build never hit any of
/// this because `st.download_button(data=path.read_bytes())` sent bytes
/// straight from disk to the browser — they never transited the UI layer
/// at all. Doing the GET/POST in Rust restores that property: bytes go
/// server -> Rust -> disk, and the webview only ever passes a URL.
///
/// Streams chunk-by-chunk rather than buffering the whole body: a real
/// print sheet is ~100MB, and holding that in memory just to write it out
/// afterwards would also mean no progress could be reported until the
/// very end.
///
/// `body` selects the method: None issues a GET (image downloads, and the
/// finished-PDF fetch), Some sends it as a JSON POST body.
#[tauri::command]
async fn download_to_path(
    app: AppHandle,
    cancels: State<'_, DownloadCancel>,
    url: String,
    body: Option<String>,
    path: String,
    download_id: String,
) -> Result<bool, String> {
    use futures_util::StreamExt;
    use std::io::Write;

    let client = reqwest::Client::new();
    let request = match body {
        Some(json) => client
            .post(&url)
            .header("Content-Type", "application/json")
            .body(json),
        None => client.get(&url),
    };
    let response = request.send().await.map_err(|e| e.to_string())?;
    if !response.status().is_success() {
        let status = response.status();
        // Keep the server's own message — /api/pdf/html returns a 503 with
        // actionable text when the optional WeasyPrint extra is missing.
        let detail = response.text().await.unwrap_or_default();
        return Err(if detail.is_empty() {
            format!("Server returned {status}")
        } else {
            format!("Server returned {status}: {detail}")
        });
    }

    let total = response.content_length();
    let dest = std::path::PathBuf::from(&path);
    let mut file = std::fs::File::create(&dest).map_err(|e| e.to_string())?;
    let mut downloaded: u64 = 0;
    let mut last_emit = std::time::Instant::now();
    let mut last_emit_bytes: u64 = 0;
    let mut stream = response.bytes_stream();

    let _ = app.emit(
        "download-progress",
        DownloadProgress { id: download_id.clone(), downloaded: 0, total },
    );

    while let Some(chunk) = stream.next().await {
        if cancels.0.lock().await.remove(&download_id) {
            // Drop the handle before removing, or Windows refuses to
            // delete a file that still has an open writer.
            drop(file);
            let _ = std::fs::remove_file(&dest);
            return Ok(false);
        }
        let chunk = chunk.map_err(|e| e.to_string())?;
        file.write_all(&chunk).map_err(|e| e.to_string())?;
        downloaded += chunk.len() as u64;

        if last_emit.elapsed() >= PROGRESS_INTERVAL
            || downloaded - last_emit_bytes >= PROGRESS_BYTES
        {
            let _ = app.emit(
                "download-progress",
                DownloadProgress { id: download_id.clone(), downloaded, total },
            );
            last_emit = std::time::Instant::now();
            last_emit_bytes = downloaded;
        }
    }
    file.flush().map_err(|e| e.to_string())?;
    // Always land on a final 100% event — the throttle above can otherwise
    // swallow the last chunk and leave the bar short of full.
    let _ = app.emit(
        "download-progress",
        DownloadProgress { id: download_id, downloaded, total },
    );
    Ok(true)
}

/// Ask an in-flight transfer to stop. Recorded even if it arrives before
/// the transfer starts reading chunks, so an immediate cancel isn't lost.
#[tauri::command]
async fn cancel_download(cancels: State<'_, DownloadCancel>, download_id: String) -> Result<(), String> {
    cancels.0.lock().await.insert(download_id);
    Ok(())
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
        .manage(DownloadCancel::default())
        .invoke_handler(tauri::generate_handler![
            start_local_server,
            stop_local_server,
            pick_save_path,
            download_to_path,
            cancel_download,
            create_project,
            list_projects,
            get_project,
            update_project,
            delete_project,
            clear_all_projects,
            import_decklist_text,
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
