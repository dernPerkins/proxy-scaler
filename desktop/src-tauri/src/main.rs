// The desktop shell. In Local mode this spawns proxy-scaler-serve (the
// supervisor, frozen via PyInstaller — see desktop/pyinstaller/) and waits
// for it to report ready, returning the local API server's base URL to the
// React frontend; in Remote mode there's no local process at all, the
// frontend just configures its API client to point at the user-supplied
// host directly. The Local/Remote choice itself is asked fresh on every
// launch (see ConnectGate.tsx), not persisted.
//
// proxy-scaler-serve is a PyInstaller onedir build (a plain directory,
// loaded directly off disk, no per-launch extraction). Looked up here at
// runtime relative to std::env::current_exe(), rather than through
// Tauri's app.path().resource_dir() API — NOT via Tauri's externalBin
// either (single-file only, which forced onefile mode: self-extracting
// its ~1GB+ torch/spandrel/torchvision bundle to a fresh temp dir on
// *every* launch, a real measured startup-time regression).
//
// bundle.resources was tried and initially abandoned for a reproducible
// crash — "Not a directory (os error 20)" — inside tauri-build 2.6.3's
// own copy_resources/ResourcePaths code walking the raw PyInstaller
// output. Root cause, confirmed once a real Rust toolchain was available
// to step through it: that code's read_link()-then-is_dir() handling of
// torch's dense set of versioned .so/.dylib symlinks (is_dir() follows
// symlinks, and a dangling/relative one trips it). Feeding it an
// already-dereferenced copy (exactly what `make sidecar`/`sidecar-release`
// already produce via rsync -aL/cp -al, since real regular files have no
// such problem) avoids the crash entirely — confirmed via a clean
// `cargo build --release` with bundle.resources pointed at
// target/release/proxy-scaler-serve/.
//
// Given that, the placement per platform is:
//   - Windows: tauri.windows.conf.json declares bundle.resources pointing
//     at the dereferenced sidecar dir. Tauri's own resource_dir() on
//     Windows already resolves to "the directory containing the main
//     executable" — the exact same place current_exe()-relative lookup
//     here already checks — so this needed no main.rs change at all.
//   - macOS: resource_dir() there resolves to Contents/Resources/, NOT
//     Contents/MacOS/ where the binary (and this lookup) lives, and Tauri
//     v2 has no afterBundleCommand hook to place something there
//     post-bundle. So macOS keeps this current_exe()-relative lookup and
//     gets its sidecar copied into Contents/MacOS/ by a manual Makefile
//     step run right after `cargo tauri build` (see the Makefile's
//     macOS-only bundle step) — bundle.resources is deliberately NOT
//     configured for macOS, to avoid embedding a second, unused multi-GB
//     copy inside Contents/Resources/.
//   - Linux: no bundle.targets entry configured (see ARCHITECTURE.md /
//     desktop README for why) — the Makefile places files here exactly as
//     before, ships as a .tar.gz.
#![cfg_attr(not(debug_assertions), windows_subsystem = "windows")]

mod back_images;
mod project_store;
mod update;

use std::collections::VecDeque;
use std::sync::atomic::{AtomicBool, Ordering};
use std::sync::Arc;
use std::time::Duration;

use tauri::{AppHandle, Emitter, Manager, State, WindowEvent};
use tauri_plugin_dialog::DialogExt;
use tauri_plugin_shell::process::CommandEvent;
use tauri_plugin_shell::ShellExt;
use tokio::sync::{mpsc, oneshot, Mutex, Notify};

use project_store::{
    add_recent_host, clear_all_projects, create_project, delete_project,
    discard_unnamed_project, get_card_db_prompt_dismissed, get_last_project_id,
    get_or_create_unnamed_project, get_project,
    get_quit_prompt_suppressed, get_show_digital_printings, get_update_check_enabled,
    get_update_skipped_version,
    import_decklist_text, import_resolved_cards, list_projects, list_recent_hosts, parse_decklist,
    remove_card, remove_recent_host, set_card_db_prompt_dismissed, set_card_printing,
    set_card_quantity, set_cards_resolution, set_last_project_id, set_quit_prompt_suppressed,
    set_show_digital_printings, set_update_check_enabled, set_update_skipped_version,
    update_project,
};
use update::{check_for_update, download_update, launch_installer};

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
/// Generous on purpose: the sidecar is a ~2.8GB PyInstaller bundle whose
/// first launch after an install can be dominated by the OS scanning it.
const STARTUP_TIMEOUT: Duration = Duration::from_secs(90);
/// How much of the sidecar's stderr to keep for the startup-failure
/// message. Enough to carry a Python traceback's tail, small enough that
/// it stays readable inside a UI toast.
const STDERR_TAIL_LINES: usize = 20;

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
    let candidates: Vec<_> = sidecar_dir_candidates()?
        .into_iter()
        .map(|dir| dir.join(&exe_name))
        .collect();
    let exe_path = candidates
        .iter()
        .find(|p| p.is_file())
        .cloned()
        .ok_or_else(|| {
            format!(
                "proxy-scaler-serve not found. Looked in: {}",
                candidates
                    .iter()
                    .map(|p| p.display().to_string())
                    .collect::<Vec<_>>()
                    .join(", ")
            )
        })?;
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
        //
        // --hold-worker: the worker starts held — leftover tasks from the
        // last session must not begin processing before the frontend has
        // asked the user to resume or cancel them (ResumeTasksPrompt
        // releases via POST /api/worker/release). Only this embedded
        // spawn passes it; the standalone server app and headless
        // installs keep processing immediately.
        .args(["--port", port_arg.as_str(), "--hold-worker"])
        .spawn()
        .map_err(|e| format!("failed to spawn local server: {e}"))?;
    *guard = Some(child);
    drop(guard);

    // The startup outcome, reported exactly once by the reader task below:
    // Ok on the ready marker, Err if the process dies first. A oneshot
    // rather than a second Notify specifically because Notify only wakes
    // waiters already registered at the time it fires — a sidecar that
    // exits before this function reaches its await would be missed
    // entirely and then waited out for the full timeout, reporting a
    // meaningless "did not become ready" for what was actually an
    // immediate, diagnosable crash.
    let (outcome_tx, outcome_rx) = oneshot::channel::<Result<(), String>>();
    let mut outcome_tx = Some(outcome_tx);
    let exited = state.exited.clone();

    tauri::async_runtime::spawn(async move {
        // Kept so a startup failure can report what the sidecar actually
        // said instead of a bare timeout. Everything worth reading lands
        // on stderr — supervisor.py's own "API server did not become
        // healthy in time", or a child's traceback.
        let mut stderr_tail: VecDeque<String> = VecDeque::new();

        while let Some(event) = rx.recv().await {
            match event {
                CommandEvent::Stdout(line) => {
                    let text = String::from_utf8_lossy(&line);
                    print!("[proxy-scaler-serve] {text}");
                    if text.contains(READY_MARKER) {
                        if let Some(tx) = outcome_tx.take() {
                            let _ = tx.send(Ok(()));
                        }
                    }
                }
                CommandEvent::Stderr(line) => {
                    let text = String::from_utf8_lossy(&line);
                    eprint!("[proxy-scaler-serve] {text}");
                    if stderr_tail.len() == STDERR_TAIL_LINES {
                        stderr_tail.pop_front();
                    }
                    stderr_tail.push_back(text.trim_end().to_string());
                }
                CommandEvent::Terminated(payload) => {
                    eprintln!("[proxy-scaler-serve] exited: {payload:?}");
                    // Only meaningful if it died *before* reporting ready;
                    // once the take() above has consumed the sender, a
                    // later exit is an ordinary shutdown, not a failure.
                    if let Some(tx) = outcome_tx.take() {
                        let detail = if stderr_tail.is_empty() {
                            "no output on stderr".to_string()
                        } else {
                            stderr_tail
                                .iter()
                                .map(String::as_str)
                                .collect::<Vec<_>>()
                                .join("; ")
                        };
                        let _ = tx.send(Err(format!(
                            "proxy-scaler-serve exited during startup (code {:?}): {detail}",
                            payload.code
                        )));
                    }
                    exited.notify_waiters();
                    break;
                }
                _ => {}
            }
        }

        // The event stream can also just end — no Terminated event, the
        // channel simply closed. Leaving the oneshot unsent would strand
        // the caller on the full timeout for no reason.
        if let Some(tx) = outcome_tx.take() {
            let _ = tx.send(Err(
                "proxy-scaler-serve stopped reporting before signalling ready".to_string(),
            ));
        }
    });

    drop(transition);

    let outcome = match tokio::time::timeout(STARTUP_TIMEOUT, outcome_rx).await {
        Ok(Ok(Ok(()))) => Ok(local_url()),
        Ok(Ok(Err(reason))) => Err(reason),
        // Sender dropped without sending. The fallback above makes this
        // unreachable in practice, but treat it as a failure rather than
        // reporting a success there's no evidence for.
        Ok(Err(_)) => Err("proxy-scaler-serve startup reporting failed".to_string()),
        Err(_) => Err(format!(
            "proxy-scaler-serve did not become ready within {}s",
            STARTUP_TIMEOUT.as_secs()
        )),
    };

    if outcome.is_err() {
        // Don't leave a dead or wedged child sitting in the slot: the
        // early-return at the top of this function treats an occupied slot
        // as "already running", so a retry would return the local URL and
        // report success with nothing behind it.
        let _transition = state.transition.lock().await;
        let mut guard = state.child.lock().await;
        if let Some(child) = guard.take() {
            // Hard kill rather than stop_sidecar's graceful path: startup
            // already failed, so there's no healthy supervisor left to
            // cooperate with a stdin shutdown request.
            let _ = child.kill();
        }
    }

    outcome
}

fn local_url() -> String {
    format!("http://127.0.0.1:{LOCAL_PORT}")
}

/// The two places the frozen sidecar directory can live, relative to this
/// binary — first one that has what the caller wants wins. Shared by the
/// spawn path above and update.rs's gpu-variant marker lookup, so the two
/// can never disagree about where the sidecar is:
///   - a sibling of this binary — dev builds (target/{debug,release}/)
///     and the shipped Windows/Linux layout.
///   - ../Resources/ — inside a macOS .app. Contents/MacOS is defined by
///     Apple's bundle format as executables-*only*, and codesign enforces
///     it: a PyInstaller onedir tree there fails to sign outright, with
///     "code object is not signed at all" on the first non-code file it
///     meets (hyphenation dictionaries, .dist-info metadata, ...).
///     Contents/Resources is where non-code belongs — sealed by hash
///     rather than treated as code. See docs/releasing.md.
/// Checking both keeps one lookup correct everywhere instead of
/// cfg!(target_os)-ing it, and means a mis-placed sidecar reports where
/// it actually looked rather than a bare spawn failure.
fn sidecar_dir_candidates() -> Result<Vec<std::path::PathBuf>, String> {
    let exe_dir = std::env::current_exe()
        .map_err(|e| format!("failed to resolve current executable path: {e}"))?
        .parent()
        .ok_or_else(|| "current executable has no parent directory".to_string())?
        .to_path_buf();
    Ok(vec![
        exe_dir.join("proxy-scaler-serve"),
        exe_dir
            .join("..")
            .join("Resources")
            .join("proxy-scaler-serve"),
    ])
}

/// This build's own version, for the UI's version display and the
/// client/server drift comparison (connection.tsx). Baked in at compile
/// time from Cargo.toml — one of the seven copies packaging/set-version.py
/// keeps in lockstep — rather than read from anywhere at runtime.
#[tauri::command]
fn get_app_version() -> String {
    env!("CARGO_PKG_VERSION").to_string()
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

/// Opens a local generation directory in the OS file manager. Absolute
/// paths only — the webview can't use this to launch arbitrary
/// `http:`/`file:` targets. Remote directories go through
/// open_remote_terminal instead.
#[tauri::command]
fn open_directory(app: AppHandle, target: String) -> Result<(), String> {
    let dir = std::path::PathBuf::from(&target);
    if !dir.is_absolute() {
        return Err(format!("refusing to open non-absolute path: {target}"));
    }
    // output/ doesn't exist until the first generation finishes, and
    // "open" on a missing path is a confusing silent no-op — create it
    // so the click always lands somewhere.
    std::fs::create_dir_all(&dir).map_err(|e| format!("couldn't create {target}: {e}"))?;
    #[allow(deprecated)]
    app.shell().open(dir.to_string_lossy(), None).map_err(|e| e.to_string())
}

/// Opens an interactive SSH session to `host` in a new terminal window,
/// cd'd into `path` — the Remote-mode counterpart of open_directory.
/// An earlier shape handed the OS an `sftp://host/path` URL instead, but
/// scheme-handler registration is a lottery: on Linux VLC commonly claims
/// sftp://, which "opens" the directory as a media playlist. A terminal
/// running ssh is predictable. Username/keys come from ~/.ssh/config,
/// same as any manual ssh.
#[tauri::command]
fn open_remote_terminal(host: String, path: String) -> Result<(), String> {
    // The host came from the connect dialog, but don't let a string
    // starting with '-' be parsed as an ssh option.
    if host.is_empty() || host.starts_with('-') {
        return Err(format!("refusing to ssh to suspicious host: {host}"));
    }
    // POSIX-quote the path, cd, then hand over to the user's login shell.
    // Best-effort on a non-POSIX remote (Windows OpenSSH defaults to
    // cmd.exe) — the session still opens, just not cd'd.
    let quoted = format!("'{}'", path.replace('\'', r"'\''"));
    let remote_cmd = format!("cd {quoted}; exec \"$SHELL\" -l");
    let ssh: Vec<String> = vec!["ssh".into(), "-t".into(), host, remote_cmd];
    spawn_terminal(&ssh)
}

/// Launches `cmd` (an argv, not a shell string) in a new terminal window.
#[cfg(target_os = "linux")]
fn spawn_terminal(cmd: &[String]) -> Result<(), String> {
    // No portable "the terminal" on Linux: honor $TERMINAL first, then
    // walk the common emulators. Each entry pairs the program with the
    // flag that makes it exec an argv (kitty takes one directly).
    // spawn() success only proves the binary launched — good enough for
    // best-effort; the terminal itself shows any ssh failure.
    let mut candidates: Vec<(String, Vec<&str>)> = Vec::new();
    if let Ok(term) = std::env::var("TERMINAL") {
        if !term.is_empty() {
            candidates.push((term, vec!["-e"]));
        }
    }
    for (prog, args) in [
        ("x-terminal-emulator", vec!["-e"]),
        ("gnome-terminal", vec!["--"]),
        ("konsole", vec!["-e"]),
        ("xfce4-terminal", vec!["-x"]),
        ("kitty", vec![]),
        ("alacritty", vec!["-e"]),
        ("xterm", vec!["-e"]),
    ] {
        candidates.push((prog.to_string(), args));
    }
    let mut errs = Vec::new();
    for (prog, pre) in candidates {
        match std::process::Command::new(&prog).args(&pre).args(cmd).spawn() {
            Ok(_) => return Ok(()),
            Err(e) => errs.push(format!("{prog}: {e}")),
        }
    }
    Err(format!("no terminal emulator found ({})", errs.join("; ")))
}

#[cfg(target_os = "windows")]
fn spawn_terminal(cmd: &[String]) -> Result<(), String> {
    // Windows Terminal if installed, else a plain conhost window via
    // `cmd /c start`. Windows 10+ ships the OpenSSH client.
    if std::process::Command::new("wt.exe").args(cmd).spawn().is_ok() {
        return Ok(());
    }
    std::process::Command::new("cmd")
        .args(["/C", "start", ""]) // "" fills start's title slot
        .args(cmd)
        .spawn()
        .map(|_| ())
        .map_err(|e| e.to_string())
}

#[cfg(target_os = "macos")]
fn spawn_terminal(cmd: &[String]) -> Result<(), String> {
    // Terminal.app has no "run this argv" CLI; AppleScript is the
    // supported route. Quote each arg for the shell line, then escape
    // that line for the AppleScript string literal.
    let shell_line = cmd
        .iter()
        .map(|a| format!("'{}'", a.replace('\'', r"'\''")))
        .collect::<Vec<_>>()
        .join(" ");
    let line = shell_line.replace('\\', "\\\\").replace('"', "\\\"");
    // The `is running` split matters: when Terminal isn't running yet,
    // `activate` launches it, and the launch itself opens Terminal's
    // default window — a bare `do script` then adds a *second* window,
    // leaving a stray empty zsh behind the ssh one. `in window 1` reuses
    // the launch-created window instead, so exactly one window appears
    // either way. (`is running` doesn't launch the app to answer.)
    let script = format!(
        "if application \"Terminal\" is running then\n\
         tell application \"Terminal\"\n\
         activate\n\
         do script \"{line}\"\n\
         end tell\n\
         else\n\
         tell application \"Terminal\"\n\
         activate\n\
         do script \"{line}\" in window 1\n\
         end tell\n\
         end if"
    );
    std::process::Command::new("osascript")
        .args(["-e", &script])
        .spawn()
        .map(|_| ())
        .map_err(|e| e.to_string())
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
        // Keep the server's own message — its error responses carry
        // actionable text that a bare status code would throw away.
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

// --- The quit prompt ------------------------------------------------------
//
// Closing the window with an unnamed project that holds cards offers to
// name it first (.scratch/optional-projects/spec.md §6). The offer is a
// React modal rather than a native dialog — a message dialog is
// buttons-only and cannot collect a name — so the close path has to hand
// the decision to the webview and wait for it to answer.
//
// The shape here is forced by the runtime rather than chosen: the
// prevent-close decision is read out of its channel with `try_recv()` the
// instant the window-event closure returns, so `prevent_close()` must be
// called synchronously and everything that waits has to happen on a
// spawned task. Full reasoning and the rest of the traps:
// .scratch/optional-projects/research/tauri-close-confirm.md.

/// Guards teardown against running twice. `prevent_close()` leaves the
/// window visible and clickable, so a second click on the X re-enters the
/// close handler — without this it would stop the sidecar and call
/// `app.exit(0)` underneath the first attempt.
static SHUTTING_DOWN: AtomicBool = AtomicBool::new(false);

/// How long to wait for the webview to say what it is doing about a close
/// before quitting without it. Only the *first* reply is timed: once the
/// frontend says the modal is up, the user is reading it and may take as
/// long as they like.
///
/// Giving up is the safe failure rather than a lossy one. Cards and
/// settings are already persisted, so a prompt that never appears lands on
/// exactly the "Not now" outcome; the alternative is an app that cannot be
/// closed at all when the webview never loaded or has wedged.
const QUIT_PROMPT_ACK: Duration = Duration::from_secs(3);

#[derive(Default)]
struct QuitPromptState {
    /// Whether the webview has a close-request handler mounted at all.
    /// Until it does, nobody can answer and the close path must not wait
    /// for one: the connect gate is on screen for as long as the local
    /// sidecar takes to start (up to STARTUP_TIMEOUT), and closing during
    /// it stays as immediate as it has always been.
    ///
    /// A latch, never cleared — the component is mounted for the life of
    /// the app, and a webview that reloads out from under it is covered by
    /// QUIT_PROMPT_ACK above rather than by this.
    listening: AtomicBool,
    /// The end the webview's answers to the close request currently in
    /// flight are sent on; `arm_quit_prompt` keeps the receiving end.
    ///
    /// A std Mutex rather than tokio's because it is armed from inside the
    /// window-event closure, which cannot await — and armed there, before
    /// the task that reads it is even spawned, so an answer can never
    /// arrive before there is somewhere to put it.
    replies: std::sync::Mutex<Option<mpsc::UnboundedSender<QuitReply>>>,
}

#[derive(Debug, Clone, Copy, PartialEq, serde::Deserialize)]
#[serde(rename_all = "snake_case")]
enum QuitReply {
    /// The modal is up. The next reply is the answered one, whenever it
    /// comes.
    Prompting,
    /// Nothing to ask — a named project, no cards, or the prompt switched
    /// off — or the modal has just been answered. Tear down.
    Proceed,
}

/// Called by QuitPrompt.tsx once its close-request listener is live.
#[tauri::command]
fn quit_prompt_listening(state: State<'_, QuitPromptState>) {
    state.listening.store(true, Ordering::SeqCst);
}

/// The webview's side of the close handshake; see QuitPrompt.tsx. Sent at
/// most twice per close request.
#[tauri::command]
fn answer_quit_prompt(state: State<'_, QuitPromptState>, reply: QuitReply) {
    let Ok(slot) = state.replies.lock() else { return };
    if let Some(tx) = slot.as_ref() {
        // A closed receiver just means QUIT_PROMPT_ACK expired first and
        // teardown is already under way; a late answer changes nothing.
        let _ = tx.send(reply);
    }
}

/// Opens the channel the webview answers this close request on — or None
/// when there is no listener to ask, which is the answer the close path
/// needs before it decides to wait for anything.
fn arm_quit_prompt(state: &QuitPromptState) -> Option<mpsc::UnboundedReceiver<QuitReply>> {
    if !state.listening.load(Ordering::SeqCst) {
        return None;
    }
    let (tx, rx) = mpsc::unbounded_channel();
    // A poisoned lock is not worth panicking a close path over: no channel
    // means no wait, exactly like no listener.
    let mut slot = state.replies.lock().ok()?;
    *slot = Some(tx);
    Some(rx)
}

/// Waits for the webview to answer `tauri://close-requested`. Returns as
/// soon as there is nothing (further) to wait for — including when the
/// frontend never answers at all.
async fn await_quit_prompt(mut replies: mpsc::UnboundedReceiver<QuitReply>) {
    match tokio::time::timeout(QUIT_PROMPT_ACK, replies.recv()).await {
        Ok(Some(QuitReply::Prompting)) => {
            // Unbounded on purpose: this is a person deciding whether to
            // name their project, not a machine that can be timed out.
            let _ = replies.recv().await;
        }
        // Proceed, a dropped sender, or silence.
        _ => {}
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
        // First, before any other plugin, per the plugin's own docs — the
        // whole point is to bail out of a duplicate process as early as
        // possible. Without this, a second copy in Local mode spawns a
        // second supervisor onto port 13207; its uvicorn loses the bind
        // race and dies, surfacing as a confusing generic startup failure
        // (the exact multi-instance mess observed in the wild with the
        // server app). A second launch now just focuses the window the
        // user already has.
        .plugin(tauri_plugin_single_instance::init(|app, _args, _cwd| {
            if let Some(window) = app.get_webview_window("main") {
                let _ = window.show();
                let _ = window.unminimize();
                let _ = window.set_focus();
            }
        }))
        .plugin(tauri_plugin_shell::init())
        .plugin(tauri_plugin_dialog::init())
        .manage(SidecarState::default())
        .manage(DownloadCancel::default())
        .manage(QuitPromptState::default())
        .manage(update::PendingUpdate::default())
        .invoke_handler(tauri::generate_handler![
            start_local_server,
            stop_local_server,
            pick_save_path,
            open_directory,
            open_remote_terminal,
            download_to_path,
            cancel_download,
            create_project,
            get_or_create_unnamed_project,
            discard_unnamed_project,
            list_projects,
            get_project,
            update_project,
            delete_project,
            clear_all_projects,
            import_decklist_text,
            parse_decklist,
            import_resolved_cards,
            remove_card,
            set_card_quantity,
            set_card_printing,
            set_cards_resolution,
            get_show_digital_printings,
            set_show_digital_printings,
            get_card_db_prompt_dismissed,
            set_card_db_prompt_dismissed,
            get_last_project_id,
            set_last_project_id,
            list_recent_hosts,
            add_recent_host,
            remove_recent_host,
            get_quit_prompt_suppressed,
            set_quit_prompt_suppressed,
            quit_prompt_listening,
            answer_quit_prompt,
            get_app_version,
            get_update_skipped_version,
            set_update_skipped_version,
            get_update_check_enabled,
            set_update_check_enabled,
            check_for_update,
            download_update,
            launch_installer,
            back_images::list_back_images,
            back_images::add_back_image,
            back_images::set_back_image_label,
            back_images::set_back_image_includes_bleed,
            back_images::count_projects_using_back_image,
            back_images::delete_back_image,
            back_images::back_image_thumbnail,
            back_images::get_default_back_image_id,
            back_images::set_default_back_image_id,
            back_images::sync_back_image
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
                // Synchronously, and first: the runtime reads this decision
                // the moment the closure returns, so a prevent_close() sent
                // from the task below would simply never be seen.
                api.prevent_close();
                if SHUTTING_DOWN.swap(true, Ordering::SeqCst) {
                    return;
                }
                // Armed before the spawn, so the channel is in place by the
                // time Tauri emits tauri://close-requested to the webview
                // and the frontend's answer always has somewhere to land.
                let replies = arm_quit_prompt(&window.state::<QuitPromptState>());
                let window = window.clone();
                let app = window.app_handle().clone();
                tauri::async_runtime::spawn(async move {
                    if let Some(replies) = replies {
                        await_quit_prompt(replies).await;
                    }
                    // Hidden only once the prompt has been answered — it
                    // has to be on screen while it is being answered. The
                    // original reason for hiding early still holds for
                    // everything past this point: cleanup is not instant
                    // (the sidecar has its own two children to stop), and
                    // leaving a frozen, unresponsive window on screen for
                    // those seconds reads as a hang rather than as a
                    // shutdown. The process still exits only once
                    // stop_sidecar has actually finished; this changes what
                    // the user sees, not when the work completes.
                    let _ = window.hide();
                    shutdown_and_exit(app).await;
                });
            }
        })
        .run(tauri::generate_context!())
        .expect("error while running tauri application");
}

#[cfg(test)]
mod tests {
    use super::*;

    // The close handshake without Tauri around it: on_window_event arms
    // exactly this channel, and await_quit_prompt is what its spawned task
    // waits on before hiding the window and stopping the sidecar.
    //
    // Every test runs on a paused clock, so elapsed time is virtual — a
    // three-second timeout costs no seconds, and "did it wait?" is a real
    // assertion rather than a stopwatch guess.

    #[test]
    fn a_close_with_no_listener_yet_has_nothing_to_wait_on() {
        let state = QuitPromptState::default();

        assert!(
            arm_quit_prompt(&state).is_none(),
            "closing the window at the connect gate must stay immediate"
        );

        state.listening.store(true, Ordering::SeqCst);
        assert!(arm_quit_prompt(&state).is_some());
    }

    #[tokio::test(start_paused = true)]
    async fn a_frontend_with_nothing_to_ask_releases_teardown_at_once() {
        let (tx, rx) = mpsc::unbounded_channel();
        tx.send(QuitReply::Proceed).expect("send");
        let start = tokio::time::Instant::now();

        await_quit_prompt(rx).await;

        assert!(
            start.elapsed() < QUIT_PROMPT_ACK,
            "a named project or an empty one must not sit through the wait"
        );
    }

    #[tokio::test(start_paused = true)]
    async fn a_silent_frontend_stops_holding_the_app_open() {
        // Sender kept alive: this is a webview that never loaded or has
        // wedged, not one that dropped the channel.
        let (_tx, rx) = mpsc::unbounded_channel::<QuitReply>();
        let start = tokio::time::Instant::now();

        await_quit_prompt(rx).await;

        assert!(start.elapsed() >= QUIT_PROMPT_ACK, "it should have waited first");
    }

    #[tokio::test(start_paused = true)]
    async fn a_prompt_on_screen_is_waited_out_however_long_it_takes() {
        // Far past the ack: a user reading the offer and typing a name is
        // not on a deadline.
        const THINKING: Duration = Duration::from_secs(600);
        let (tx, rx) = mpsc::unbounded_channel();
        tx.send(QuitReply::Prompting).expect("send");
        tokio::spawn(async move {
            tokio::time::sleep(THINKING).await;
            tx.send(QuitReply::Proceed).expect("send");
        });
        let start = tokio::time::Instant::now();

        await_quit_prompt(rx).await;

        assert!(
            start.elapsed() >= THINKING,
            "the second reply is the one that releases teardown"
        );
    }
}
