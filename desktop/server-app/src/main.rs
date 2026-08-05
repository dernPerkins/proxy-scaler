// Proxy Scaler Server: a small status window that runs proxy-scaler-serve
// so other machines can point the desktop client at this one.
//
// The sidecar lifecycle here is deliberately the same shape as the
// client's (desktop/src-tauri/src/main.rs): resolve the PyInstaller
// onedir bundle as a sibling of our own executable via current_exe(),
// spawn it with tauri-plugin-shell, watch stdout for PROXY_SCALER_READY,
// and stop it by writing "shutdown\n" to stdin before ever resorting to
// a kill. That path is load-bearing — a hard kill orphans the API server
// and worker underneath the supervisor.
//
// What's different: this app passes --host/--port (the client always
// wants loopback), keeps a log ring buffer for the window to display,
// and lives in the tray rather than exiting when its window is closed.
#![cfg_attr(not(debug_assertions), windows_subsystem = "windows")]

use std::collections::VecDeque;
use std::sync::Arc;
use std::time::Duration;

use serde::Serialize;
use tauri::menu::{Menu, MenuItem};
use tauri::tray::TrayIconBuilder;
use tauri::{AppHandle, Manager, State, WindowEvent};
use tauri_plugin_shell::process::CommandEvent;
use tauri_plugin_shell::ShellExt;
use tokio::sync::{Mutex, Notify};

const READY_MARKER: &str = "PROXY_SCALER_READY";
const SHUTDOWN_GRACE: Duration = Duration::from_secs(12);
const READY_TIMEOUT: Duration = Duration::from_secs(120);
const MAX_LOG_LINES: usize = 300;
const LOOPBACK: &str = "127.0.0.1";
const ALL_INTERFACES: &str = "0.0.0.0";

#[derive(Default)]
struct ServerState {
    child: Mutex<Option<tauri_plugin_shell::process::CommandChild>>,
    exited: Arc<Notify>,
    /// Serializes start against stop — stop releases the child slot
    /// before waiting out its shutdown grace period, so without this a
    /// start in that window would spawn a second supervisor onto a port
    /// the first hasn't let go of yet.
    transition: Mutex<()>,
    logs: Mutex<VecDeque<String>>,
    settings: Mutex<Settings>,
    /// True only once READY_MARKER has actually been seen on stdout —
    /// distinct from `child.is_some()` (a process merely having been
    /// spawned). Without this, get_status()'s polling loop reports
    /// "running" the instant the child handle exists, well before Uvicorn
    /// is actually accepting connections, which reads to the user as the
    /// server being up when a connection attempt would still fail.
    ready: Mutex<bool>,
}

struct Settings {
    port: u16,
    allow_remote: bool,
}

impl Default for Settings {
    fn default() -> Self {
        Self {
            // Matches supervisor.py's DEFAULT_PORT (13207 — M-T-G by letter
            // position, picked to dodge the usual 8000/8080/8888/9000/etc
            // collisions).
            port: 13207,
            // Off by default. The API has no authentication, so becoming
            // reachable from the network is a decision the user makes,
            // not something that happens by opening the app.
            allow_remote: false,
        }
    }
}

#[derive(Serialize)]
struct Status {
    /// True as soon as the process is spawned — a process existing, not
    /// necessarily serving anything yet.
    running: bool,
    /// True once READY_MARKER has been seen — the server is genuinely
    /// accepting connections now, not just running. See ServerState::ready.
    ready: bool,
    port: u16,
    allow_remote: bool,
    /// Addresses to type into the client's "Connect to a server" box.
    addresses: Vec<String>,
    logs: Vec<String>,
}

async fn push_log(state: &ServerState, line: String) {
    let mut logs = state.logs.lock().await;
    if logs.len() >= MAX_LOG_LINES {
        logs.pop_front();
    }
    logs.push_back(line);
}

/// Best-effort primary LAN address, so the window can show something the
/// user can actually type into the client rather than making them go
/// hunting through network settings.
///
/// No packets are sent: connecting a UDP socket only asks the routing
/// table which local interface would be used to reach that destination.
fn primary_local_ip() -> Option<String> {
    let socket = std::net::UdpSocket::bind("0.0.0.0:0").ok()?;
    socket.connect("8.8.8.8:80").ok()?;
    Some(socket.local_addr().ok()?.ip().to_string())
}

#[tauri::command]
async fn get_status(state: State<'_, ServerState>) -> Result<Status, String> {
    let settings = state.settings.lock().await;
    let port = settings.port;
    let allow_remote = settings.allow_remote;
    drop(settings);

    let running = state.child.lock().await.is_some();
    let ready = *state.ready.lock().await;
    let logs = state.logs.lock().await.iter().cloned().collect();

    let mut addresses = vec![format!("{LOOPBACK}:{port}")];
    if allow_remote {
        if let Some(ip) = primary_local_ip() {
            addresses.insert(0, format!("{ip}:{port}"));
        }
    }

    Ok(Status {
        running,
        ready,
        port,
        allow_remote,
        addresses,
        logs,
    })
}

#[tauri::command]
async fn set_settings(
    state: State<'_, ServerState>,
    port: u16,
    allow_remote: bool,
) -> Result<(), String> {
    if state.child.lock().await.is_some() {
        return Err("Stop the server before changing its address or port.".into());
    }
    let mut settings = state.settings.lock().await;
    settings.port = port;
    settings.allow_remote = allow_remote;
    Ok(())
}

#[tauri::command]
async fn start_server(app: AppHandle, state: State<'_, ServerState>) -> Result<(), String> {
    // Held across the spawn, released before the readiness wait so a stop
    // issued during a slow startup isn't blocked behind it.
    let transition = state.transition.lock().await;

    let mut guard = state.child.lock().await;
    if guard.is_some() {
        return Ok(());
    }

    let settings = state.settings.lock().await;
    let port = settings.port;
    let host = if settings.allow_remote { ALL_INTERFACES } else { LOOPBACK };
    drop(settings);

    let exe_name = format!("proxy-scaler-serve{}", std::env::consts::EXE_SUFFIX);
    let exe_dir = std::env::current_exe()
        .map_err(|e| format!("failed to resolve current executable path: {e}"))?
        .parent()
        .ok_or_else(|| "current executable has no parent directory".to_string())?
        .to_path_buf();
    let exe_path = exe_dir.join("proxy-scaler-serve").join(exe_name);

    // Bound separately rather than inlined: an array literal mixing
    // `&'static str` and `&String` relies on coercion to unify, which is
    // needless risk for a build we can't check locally.
    let port_arg = port.to_string();

    // Note there's deliberately no --no-stdin-shutdown here: stop_server()
    // below stops the server by writing to its stdin, so disabling that
    // watcher would break the very mechanism this app shuts down with.
    let (mut rx, child) = app
        .shell()
        .command(exe_path)
        .args(["--host", host, "--port", port_arg.as_str()])
        .spawn()
        .map_err(|e| format!("failed to spawn the server: {e}"))?;

    *guard = Some(child);
    drop(guard);
    // Reset explicitly rather than trusting it's already false: a prior
    // run's Terminated handler should have cleared it, but state surviving
    // across a start/stop/start cycle incorrectly is exactly the kind of
    // bug this field exists to prevent in the first place.
    *state.ready.lock().await = false;

    {
        let mut logs = state.logs.lock().await;
        logs.clear();
        logs.push_back(format!("Starting server on {host}:{port}…"));
    }

    let ready_notify = Arc::new(Notify::new());
    let ready_signal = ready_notify.clone();
    let exited = state.exited.clone();
    let app_for_pump = app.clone();

    tauri::async_runtime::spawn(async move {
        // State is fetched per use rather than held across the loop:
        // keeping a borrow of the AppHandle alive across every await in a
        // spawned task is the kind of thing that borrow-checks locally and
        // then doesn't. It's only a map lookup.
        while let Some(event) = rx.recv().await {
            match event {
                CommandEvent::Stdout(line) => {
                    let text = String::from_utf8_lossy(&line).trim_end().to_string();
                    if text.contains(READY_MARKER) {
                        ready_signal.notify_one();
                        *app_for_pump.state::<ServerState>().ready.lock().await = true;
                    }
                    if !text.is_empty() {
                        push_log(&app_for_pump.state::<ServerState>(), text).await;
                    }
                }
                CommandEvent::Stderr(line) => {
                    let text = String::from_utf8_lossy(&line).trim_end().to_string();
                    if !text.is_empty() {
                        push_log(&app_for_pump.state::<ServerState>(), text).await;
                    }
                }
                CommandEvent::Terminated(payload) => {
                    push_log(
                        &app_for_pump.state::<ServerState>(),
                        format!("Server exited: {payload:?}"),
                    )
                    .await;
                    // Clear the slot so the UI reflects reality and a
                    // restart isn't blocked by a dead handle.
                    *app_for_pump.state::<ServerState>().child.lock().await = None;
                    *app_for_pump.state::<ServerState>().ready.lock().await = false;
                    exited.notify_waiters();
                    break;
                }
                _ => {}
            }
        }
    });

    drop(transition);

    match tokio::time::timeout(READY_TIMEOUT, ready_notify.notified()).await {
        Ok(()) => Ok(()),
        Err(_) => Err(
            "The server didn't report ready in time. Check the log below — if the port is \
             already in use (the desktop app's own local server uses 13207 too), pick a \
             different one."
                .into(),
        ),
    }
}

/// Graceful-then-forceful, mirroring supervisor.py's own discipline: a
/// line on stdin is its documented shutdown trigger, and only if it
/// doesn't exit in time do we kill — a bare kill would skip the
/// supervisor's cleanup and orphan the API server and worker under it.
async fn stop_sidecar(state: &ServerState) {
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

#[tauri::command]
async fn stop_server(state: State<'_, ServerState>) -> Result<(), String> {
    stop_sidecar(&state).await;
    // Defensive: the Terminated event handler already resets this once the
    // process actually exits, but that arrives on its own async schedule —
    // this guarantees the UI's next poll sees "stopped" immediately rather
    // than a stale "ready" for however long that takes to land.
    *state.ready.lock().await = false;
    push_log(&state, "Server stopped.".to_string()).await;
    Ok(())
}

async fn shutdown_and_exit(app: AppHandle) {
    let state = app.state::<ServerState>();
    stop_sidecar(&state).await;
    app.exit(0);
}

fn main() {
    tauri::Builder::default()
        .plugin(tauri_plugin_shell::init())
        .manage(ServerState::default())
        .invoke_handler(tauri::generate_handler![
            get_status,
            set_settings,
            start_server,
            stop_server
        ])
        .setup(|app| {
            let show = MenuItem::with_id(app, "show", "Show window", true, None::<&str>)?;
            let quit = MenuItem::with_id(app, "quit", "Quit", true, None::<&str>)?;
            let menu = Menu::with_items(app, &[&show, &quit])?;

            TrayIconBuilder::new()
                .icon(app.default_window_icon().unwrap().clone())
                .tooltip("Proxy Scaler Server")
                .menu(&menu)
                .on_menu_event(|app, event| match event.id.as_ref() {
                    "show" => {
                        if let Some(window) = app.get_webview_window("main") {
                            let _ = window.show();
                            let _ = window.set_focus();
                        }
                    }
                    "quit" => {
                        // Not app.exit(0) directly: the server has to be
                        // stopped first, or its API/worker children
                        // outlive us and keep holding the port.
                        let app = app.clone();
                        tauri::async_runtime::spawn(shutdown_and_exit(app));
                    }
                    _ => {}
                })
                .build(app)?;

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
                // Hide to tray rather than quit — this is a server you
                // leave running; closing its status window shouldn't stop
                // serving. Quit lives in the tray menu.
                api.prevent_close();
                let _ = window.hide();
            }
        })
        .run(tauri::generate_context!())
        .expect("error while running the proxy-scaler server app");
}
