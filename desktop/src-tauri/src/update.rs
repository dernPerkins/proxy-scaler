// The boot-time update check and the installer handoff. The manifest
// (dist/latest.json, built and signed by packaging/generate-manifest.py
// and uploaded to R2 next to the artifacts together with
// latest.json.minisig) is fetched HERE in Rust, not in the webview:
// reqwest is CORS-exempt, so the website needs no CORS headers, and the
// check keeps working no matter what host serves the file.
//
// Trust model: the manifest names the installer's URL, size, AND sha256,
// so the manifest itself is what must be authentic — its minisign
// signature is verified against MANIFEST_PUBKEY before any field is
// read. The sha256 then only has to catch corruption in transit; the
// authenticity of the bytes follows from the signed manifest naming them.
// Everything the download/install steps act on (URL, destination,
// expected hash) lives in Rust-managed state (PendingUpdate), set only by
// check_for_update: the webview triggers steps but never supplies their
// parameters, so injected script can't point this machinery at a URL or
// file of its choosing. (That's also why the update download does NOT use
// main.rs::download_to_path, whose url/path are webview arguments.)
//
// UpdatePrompt.tsx drives the whole flow.

use std::path::{Path, PathBuf};
use std::time::Duration;

use serde::{Deserialize, Serialize};
use tauri::{AppHandle, Emitter, Manager, State};
use tauri_plugin_shell::ShellExt;

const MANIFEST_URL: &str = "https://dl.proxy-scaler.com/latest.json";
/// The minisign public key latest.json must be signed with (base64 line
/// from the `.pub` file `minisign -G` writes; the secret half lives only
/// on release machines — see docs/releasing.md). Must match the
/// server-app's copy; generate-manifest.py refuses to sign a release
/// whose signing key doesn't match this constant.
const MANIFEST_PUBKEY: &str = "RWSV/YmIcdrlZEfbgQsMpBuSUQbp+xUOWGaZWyRFaUBwShUAbWsbhdlP";
/// Bumped when the manifest layout changes incompatibly; a manifest from
/// the future is skipped rather than misread, so an old installed app
/// degrades to "no update offered" instead of offering garbage.
const MANIFEST_SCHEMA: u64 = 1;
/// Generous for a ~2KB JSON file — this runs on the boot path (albeit
/// fire-and-forget) and must never hold anything for long. Also the
/// connect timeout for the installer download itself (which gets no
/// whole-request timeout: a multi-GB transfer legitimately takes long).
const FETCH_TIMEOUT: Duration = Duration::from_secs(15);
/// Hard ceilings on the two small fetches. The manifest is ~2KB and a
/// .minisig ~330 bytes; anything approaching these caps is not our file,
/// and without them a hostile/broken host could feed the boot path an
/// unbounded body to buffer.
const MANIFEST_MAX_BYTES: u64 = 1024 * 1024;
const SIGNATURE_MAX_BYTES: u64 = 8 * 1024;

/// The production URL/key, overridable via env ONLY in debug builds —
/// how the whole flow is tested against a local python -m http.server
/// and a throwaway `minisign -G` test key without touching production.
/// Release builds ignore both variables: an env var must never be able
/// to redirect shipped installs to another manifest or key.
fn manifest_url() -> String {
    if cfg!(debug_assertions) {
        if let Ok(url) = std::env::var("PROXY_SCALER_UPDATE_URL") {
            return url;
        }
    }
    MANIFEST_URL.to_string()
}

fn manifest_pubkey() -> String {
    if cfg!(debug_assertions) {
        if let Ok(key) = std::env::var("PROXY_SCALER_UPDATE_PUBKEY") {
            return key;
        }
    }
    MANIFEST_PUBKEY.to_string()
}

#[derive(Deserialize)]
struct Manifest {
    #[serde(default)]
    schema: u64,
    version: String,
    #[serde(default)]
    notes: String,
    #[serde(default)]
    notes_url: String,
    #[serde(default)]
    artifacts: Vec<ManifestArtifact>,
}

#[derive(Deserialize)]
struct ManifestArtifact {
    app: String,
    platform: String,
    arch: String,
    variant: String,
    format: String,
    url: String,
    size: u64,
    sha256: String,
}

#[derive(Serialize)]
pub struct UpdateInfo {
    current: String,
    latest: String,
    notes: String,
    notes_url: String,
    /// The installer matching this exact build (platform + arch +
    /// gpu-variant marker), or None when there's nothing this app can
    /// install itself — Linux (tarball/deb, no installer), a dev build
    /// (no marker), or a manifest missing the entry. The frontend then
    /// offers notes_url instead of an in-app download. Display data
    /// only: the URL/path/hash stay in PendingUpdate on this side.
    artifact: Option<ArtifactOut>,
}

#[derive(Serialize)]
pub struct ArtifactOut {
    filename: String,
    size: u64,
}

/// The one artifact check_for_update decided this build may fetch, held
/// Rust-side so download_update/launch_installer take no parameters the
/// webview could forge. `downloaded` is filled by download_update with
/// where the bytes actually landed and the sha256 computed WHILE they
/// streamed — hashed off the wire, not re-read from a world-writable
/// Downloads dir later, which shrinks the verify-then-execute window.
#[derive(Default)]
pub struct PendingUpdate(pub tokio::sync::Mutex<Option<PendingArtifact>>);

pub struct PendingArtifact {
    url: String,
    /// The user's Downloads directory — somewhere they can find (and
    /// re-run, or delete) the installer if the handoff is canceled — not
    /// a temp dir the OS cleans behind their back mid-install.
    dir: PathBuf,
    filename: String,
    size: u64,
    sha256: String,
    downloaded: Option<Downloaded>,
}

struct Downloaded {
    path: PathBuf,
    sha256: String,
}

/// "x.y.z" as a comparable triple. Anything else is None, and a version
/// that can't be parsed never triggers an update offer — a malformed
/// manifest must fail toward silence, not toward a 4GB download prompt.
fn parse_version(text: &str) -> Option<(u64, u64, u64)> {
    let mut parts = text.trim().split('.');
    let triple = (
        parts.next()?.parse().ok()?,
        parts.next()?.parse().ok()?,
        parts.next()?.parse().ok()?,
    );
    parts.next().is_none().then_some(triple)
}

/// The gpu-variant marker baked into the frozen sidecar by the Makefile's
/// _sidecar-freeze ("cuda" / "cuda-legacy" / "rocm" / "directml" /
/// "default" on macOS). "dev" when absent — an unpackaged build has no
/// marker, matches no artifact, and correctly falls back to the download
/// page rather than guessing which multi-GB variant to fetch.
fn gpu_variant() -> String {
    let Ok(dirs) = crate::sidecar_dir_candidates() else {
        return "dev".to_string();
    };
    for dir in dirs {
        if let Ok(text) = std::fs::read_to_string(dir.join("gpu-variant")) {
            let variant = text.trim();
            if !variant.is_empty() {
                return variant.to_string();
            }
        }
    }
    "dev".to_string()
}

/// https-or-nothing for any URL taken from the manifest. The signature
/// already authenticates the manifest's *contents*; this keeps the
/// *transport* of what those contents point at from being downgraded.
/// `allow_http` is only ever true in debug builds, for the local
/// http.server test loop.
fn url_is_acceptable(url: &str, allow_http: bool) -> bool {
    match reqwest::Url::parse(url) {
        Ok(u) => u.scheme() == "https" || (allow_http && u.scheme() == "http"),
        Err(_) => false,
    }
}

/// Follow a few redirects, but never off https (debug builds excepted,
/// same reasoning as manifest_url). reqwest's default policy would
/// happily follow an https→http hop, which silently discards the
/// transport security the rest of this module insists on.
fn redirect_policy() -> reqwest::redirect::Policy {
    reqwest::redirect::Policy::custom(|attempt| {
        if attempt.previous().len() > 5 {
            attempt.error("too many redirects")
        } else if attempt.url().scheme() != "https" && !cfg!(debug_assertions) {
            attempt.error("redirect to a non-https URL")
        } else {
            attempt.follow()
        }
    })
}

/// The local filename the installer is saved under — built from fields
/// that already survived validation (the version re-rendered from its
/// parsed triple, the variant character-filtered), never from the URL:
/// a URL's last path segment can smuggle separators, `..`, or (on
/// Windows) a drive letter straight into the PathBuf::join.
fn installer_filename(version: (u64, u64, u64), variant: &str, format: &str) -> Option<String> {
    let (major, minor, patch) = version;
    match format {
        "setup-exe" => {
            let variant: String = variant
                .chars()
                .filter(|c| c.is_ascii_alphanumeric() || *c == '-')
                .take(32)
                .collect();
            Some(format!(
                "proxy-scaler-client_{major}.{minor}.{patch}_{variant}-setup.exe"
            ))
        }
        // macOS ships one dmg per arch, no variant in the name.
        "dmg" => Some(format!("proxy-scaler-client_{major}.{minor}.{patch}.dmg")),
        _ => None,
    }
}

/// Fetch the manifest and compare. Ok(None) means "nothing to offer" for
/// ANY reason — up to date, offline, host down, malformed manifest, bad
/// signature. The distinction is logged, never surfaced: this runs
/// unprompted on every boot, and a user with no internet must not see an
/// error toast for it.
#[tauri::command]
pub async fn check_for_update(
    app: AppHandle,
    pending: State<'_, PendingUpdate>,
) -> Result<Option<UpdateInfo>, String> {
    let manifest: Manifest = match fetch_manifest(&manifest_url(), &manifest_pubkey()).await {
        Ok(m) => m,
        Err(reason) => {
            eprintln!("[update] check skipped: {reason}");
            return Ok(None);
        }
    };

    if manifest.schema > MANIFEST_SCHEMA {
        eprintln!(
            "[update] check skipped: manifest schema {} is newer than this app understands",
            manifest.schema
        );
        return Ok(None);
    }

    let current_text = env!("CARGO_PKG_VERSION");
    let (Some(current), Some(latest)) =
        (parse_version(current_text), parse_version(&manifest.version))
    else {
        eprintln!(
            "[update] check skipped: unparseable version ({current_text:?} vs {:?})",
            manifest.version
        );
        return Ok(None);
    };
    if latest <= current {
        *pending.0.lock().await = None;
        return Ok(None);
    }

    let variant = gpu_variant();
    let artifact = manifest.artifacts.iter().find(|a| {
        a.app == "client"
            && a.platform == std::env::consts::OS
            && a.arch == std::env::consts::ARCH
            && a.variant == variant
            // Only formats that ARE installers get downloaded in-app;
            // archives (zip/tar.gz) would just strand the user with a
            // file to unpack by hand — the download page explains
            // those better than a silent Downloads drop would.
            && matches!(a.format.as_str(), "setup-exe" | "dmg")
            // A signed manifest naming an http:// artifact is refused
            // (release builds): fall back to the download page rather
            // than fetch an installer over a downgradable transport.
            && url_is_acceptable(&a.url, cfg!(debug_assertions))
    });

    let artifact_out = match artifact {
        Some(a) => match installer_filename(latest, &variant, &a.format) {
            Some(filename) => {
                // Downloads dir can be unresolvable (unusual setups);
                // temp is the fallback that keeps the update possible
                // rather than failing the whole offer.
                let dir = app
                    .path()
                    .download_dir()
                    .unwrap_or_else(|_| std::env::temp_dir());
                *pending.0.lock().await = Some(PendingArtifact {
                    url: a.url.clone(),
                    dir,
                    filename: filename.clone(),
                    size: a.size,
                    sha256: a.sha256.clone(),
                    downloaded: None,
                });
                Some(ArtifactOut {
                    filename,
                    size: a.size,
                })
            }
            None => None,
        },
        None => None,
    };
    if artifact_out.is_none() {
        *pending.0.lock().await = None;
    }

    Ok(Some(UpdateInfo {
        current: current_text.to_string(),
        latest: manifest.version,
        notes: manifest.notes,
        // The notes link lands in an <a href> / the system browser —
        // https or nothing, so a hostile value can't smuggle another
        // scheme (javascript:, file:) into the webview.
        notes_url: if url_is_acceptable(&manifest.notes_url, false) {
            manifest.notes_url
        } else {
            String::new()
        },
        artifact: artifact_out,
    }))
}

async fn fetch_manifest(url: &str, pubkey: &str) -> Result<Manifest, String> {
    let client = reqwest::Client::builder()
        .timeout(FETCH_TIMEOUT)
        .redirect(redirect_policy())
        .build()
        .map_err(|e| e.to_string())?;
    let body = fetch_capped(&client, url, MANIFEST_MAX_BYTES).await?;
    let sig = fetch_capped(&client, &format!("{url}.minisig"), SIGNATURE_MAX_BYTES).await?;
    let sig = String::from_utf8(sig).map_err(|_| "signature file isn't UTF-8".to_string())?;
    // Verified over the raw bytes BEFORE json parsing — nothing in an
    // unverified manifest is worth even deserializing.
    verify_manifest_signature(&body, &sig, pubkey)?;
    // Bytes + serde_json rather than Response::json(): reqwest is built
    // without its "json" feature (see Cargo.toml).
    serde_json::from_slice(&body).map_err(|e| format!("manifest parse failed: {e}"))
}

async fn fetch_capped(
    client: &reqwest::Client,
    url: &str,
    cap: u64,
) -> Result<Vec<u8>, String> {
    use futures_util::StreamExt;
    let response = client.get(url).send().await.map_err(|e| e.to_string())?;
    if !response.status().is_success() {
        return Err(format!("fetch returned {} for {url}", response.status()));
    }
    if response.content_length().is_some_and(|len| len > cap) {
        return Err(format!("{url} exceeds the {cap}-byte limit"));
    }
    // The declared length is advisory; count what actually arrives too.
    let mut body: Vec<u8> = Vec::new();
    let mut stream = response.bytes_stream();
    while let Some(chunk) = stream.next().await {
        let chunk = chunk.map_err(|e| e.to_string())?;
        if body.len() as u64 + chunk.len() as u64 > cap {
            return Err(format!("{url} exceeds the {cap}-byte limit"));
        }
        body.extend_from_slice(&chunk);
    }
    Ok(body)
}

fn verify_manifest_signature(body: &[u8], sig_text: &str, pubkey: &str) -> Result<(), String> {
    let pk = minisign_verify::PublicKey::from_base64(pubkey.trim()).map_err(|_| {
        "update public key not configured or unusable (see docs/releasing.md)".to_string()
    })?;
    let sig = minisign_verify::Signature::decode(sig_text.trim())
        .map_err(|e| format!("manifest signature unparseable: {e}"))?;
    // allow_legacy=false: only prehashed signatures, which is what any
    // current minisign produces — legacy mode would sign the raw body
    // with no length limit on what has to be held to verify.
    pk.verify(body, &sig, false)
        .map_err(|_| "manifest signature verification FAILED".to_string())
}

/// "name.ext" for attempt 0, "name (n).ext" after — the browser-style
/// answer to something already sitting in Downloads under that name.
/// Never truncate an existing file: it might be the user's own.
fn numbered_filename(filename: &str, n: u32) -> String {
    if n == 0 {
        return filename.to_string();
    }
    match filename.rsplit_once('.') {
        Some((stem, ext)) => format!("{stem} ({n}).{ext}"),
        None => format!("{filename} ({n})"),
    }
}

fn create_unique(dir: &Path, filename: &str) -> Result<(PathBuf, std::fs::File), String> {
    for n in 0..100 {
        let path = dir.join(numbered_filename(filename, n));
        // create_new: exclusive creation, so an existing file means "try
        // the next suffix" rather than silently overwriting it.
        match std::fs::OpenOptions::new()
            .write(true)
            .create_new(true)
            .open(&path)
        {
            Ok(file) => return Ok((path, file)),
            Err(e) if e.kind() == std::io::ErrorKind::AlreadyExists => continue,
            Err(e) => return Err(format!("couldn't create {}: {e}", path.display())),
        }
    }
    Err(format!("couldn't find a free filename for {filename}"))
}

/// Stream the pending artifact to disk, reporting progress on the same
/// `download-progress` channel (and honoring the same cancel set) as
/// main.rs::download_to_path — but with the URL and destination taken
/// from PendingUpdate, never from the webview, the sha256 computed as
/// the bytes arrive, and the transfer aborted the moment it exceeds the
/// manifest's declared size. Returns Ok(false) if the user canceled.
#[tauri::command]
pub async fn download_update(
    app: AppHandle,
    cancels: State<'_, crate::DownloadCancel>,
    pending: State<'_, PendingUpdate>,
    download_id: String,
) -> Result<bool, String> {
    use futures_util::StreamExt;
    use sha2::{Digest, Sha256};
    use std::io::Write;

    let (url, dir, filename, expected_size) = {
        let slot = pending.0.lock().await;
        let artifact = slot.as_ref().ok_or("no update download is pending")?;
        (
            artifact.url.clone(),
            artifact.dir.clone(),
            artifact.filename.clone(),
            artifact.size,
        )
    };

    // connect_timeout, not timeout: the whole-request clock would kill a
    // legitimate multi-GB transfer. Stalls surface through the user's
    // own progress bar (and Cancel button) instead.
    let client = reqwest::Client::builder()
        .connect_timeout(FETCH_TIMEOUT)
        .redirect(redirect_policy())
        .build()
        .map_err(|e| e.to_string())?;
    let response = client.get(&url).send().await.map_err(|e| e.to_string())?;
    if !response.status().is_success() {
        return Err(format!("Server returned {}", response.status()));
    }

    let (dest, mut file) = create_unique(&dir, &filename)?;
    let total = Some(expected_size);
    let mut hasher = Sha256::new();
    let mut downloaded: u64 = 0;
    let mut last_emit = std::time::Instant::now();
    let mut last_emit_bytes: u64 = 0;
    let mut stream = response.bytes_stream();

    let _ = app.emit(
        "download-progress",
        crate::DownloadProgress {
            id: download_id.clone(),
            downloaded: 0,
            total,
        },
    );

    let abort = |file: std::fs::File, dest: &Path| {
        // Drop the handle before removing, or Windows refuses to delete
        // a file that still has an open writer.
        drop(file);
        let _ = std::fs::remove_file(dest);
    };

    while let Some(chunk) = stream.next().await {
        if cancels.0.lock().await.remove(&download_id) {
            abort(file, &dest);
            return Ok(false);
        }
        let chunk = match chunk {
            Ok(chunk) => chunk,
            Err(e) => {
                abort(file, &dest);
                return Err(e.to_string());
            }
        };
        if downloaded + chunk.len() as u64 > expected_size {
            abort(file, &dest);
            return Err(format!(
                "download exceeded the expected {expected_size} bytes"
            ));
        }
        if let Err(e) = file.write_all(&chunk) {
            abort(file, &dest);
            return Err(e.to_string());
        }
        hasher.update(&chunk);
        downloaded += chunk.len() as u64;

        if last_emit.elapsed() >= crate::PROGRESS_INTERVAL
            || downloaded - last_emit_bytes >= crate::PROGRESS_BYTES
        {
            let _ = app.emit(
                "download-progress",
                crate::DownloadProgress {
                    id: download_id.clone(),
                    downloaded,
                    total,
                },
            );
            last_emit = std::time::Instant::now();
            last_emit_bytes = downloaded;
        }
    }
    if let Err(e) = file.flush() {
        abort(file, &dest);
        return Err(e.to_string());
    }
    if downloaded != expected_size {
        abort(file, &dest);
        return Err(format!(
            "download incomplete: got {downloaded} bytes, expected {expected_size}"
        ));
    }
    drop(file);

    {
        let mut slot = pending.0.lock().await;
        if let Some(artifact) = slot.as_mut() {
            artifact.downloaded = Some(Downloaded {
                path: dest,
                sha256: format!("{:x}", hasher.finalize()),
            });
        }
    }

    // Always land on a final 100% event — the throttle above can
    // otherwise swallow the last chunk and leave the bar short of full.
    let _ = app.emit(
        "download-progress",
        crate::DownloadProgress {
            id: download_id,
            downloaded,
            total,
        },
    );
    Ok(true)
}

/// Verify the downloaded installer against the manifest's size/sha256,
/// hand it to the OS, and exit. Verification is the load-bearing part:
/// the worst outcome of this whole feature would be feeding a truncated
/// or corrupted 4GB file to an installer that's about to replace a
/// working install. Two hashes must match the (signature-verified)
/// manifest: the one computed while the bytes streamed in, and a fresh
/// re-hash of the file as it sits on disk now — the freshest look this
/// process can get before the spawn. (A same-user process racing that
/// last window could still swap the file; that attacker already has code
/// execution, so this is not a boundary — the boundary is the manifest
/// signature plus platform code-signing.)
///
/// The exit matters too: on Windows the MSI can't replace files the
/// running app holds open, and on macOS Finder shouldn't be asked to
/// replace a running .app — so after the spawn/open succeeds, this runs
/// the same graceful teardown the window's close button does (sidecar
/// stopped first) and the installer takes over.
#[tauri::command]
pub async fn launch_installer(
    app: AppHandle,
    pending: State<'_, PendingUpdate>,
) -> Result<(), String> {
    let (file_path, expected_size, expected_sha256, streamed_sha256) = {
        let slot = pending.0.lock().await;
        let artifact = slot.as_ref().ok_or("no update is pending")?;
        let downloaded = artifact
            .downloaded
            .as_ref()
            .ok_or("the update hasn't been downloaded")?;
        (
            downloaded.path.clone(),
            artifact.size,
            artifact.sha256.clone(),
            downloaded.sha256.clone(),
        )
    };

    if !streamed_sha256.eq_ignore_ascii_case(expected_sha256.trim()) {
        return Err(
            "the downloaded installer failed its integrity check (corrupted download)"
                .to_string(),
        );
    }

    let actual_size = std::fs::metadata(&file_path)
        .map_err(|e| format!("downloaded file missing: {e}"))?
        .len();
    if actual_size != expected_size {
        return Err(format!(
            "download incomplete: got {actual_size} bytes, expected {expected_size}"
        ));
    }

    // Off the async pool: hashing a 4GB file is seconds of blocking I/O,
    // and Tauri's shared worker pool is small and backs every other
    // command in this app (the exact pitfall pick_save_path's comment
    // documents).
    let hash_path = file_path.clone();
    let actual_sha256 = tauri::async_runtime::spawn_blocking(move || -> Result<String, String> {
        use sha2::{Digest, Sha256};
        use std::io::Read;
        let mut file = std::fs::File::open(&hash_path).map_err(|e| e.to_string())?;
        let mut hasher = Sha256::new();
        let mut buf = vec![0u8; 1024 * 1024];
        loop {
            let n = file.read(&mut buf).map_err(|e| e.to_string())?;
            if n == 0 {
                break;
            }
            hasher.update(&buf[..n]);
        }
        Ok(format!("{:x}", hasher.finalize()))
    })
    .await
    .map_err(|e| e.to_string())??;

    if !actual_sha256.eq_ignore_ascii_case(expected_sha256.trim()) {
        return Err(
            "the downloaded installer failed its integrity check (corrupted download)"
                .to_string(),
        );
    }

    if cfg!(target_os = "windows") {
        // The Inno setup.exe is its own process; spawn-and-forget is the
        // whole handoff. It waits politely for msiexec, which itself waits
        // for this app to exit before touching Program Files.
        std::process::Command::new(&file_path)
            .spawn()
            .map_err(|e| format!("couldn't launch the installer: {e}"))?;
    } else if cfg!(target_os = "macos") {
        // Mount the dmg; the user drags to Applications themselves — the
        // same styled drag-across window a fresh install uses.
        // allow(deprecated): same call and same reasoning as main.rs's
        // open_directory — staying on the shell plugin's open rather than
        // adding tauri-plugin-opener as a second dependency for one call.
        #[allow(deprecated)]
        app.shell()
            .open(file_path.to_string_lossy(), None)
            .map_err(|e| format!("couldn't open the installer image: {e}"))?;
    } else {
        // Linux ships as tarball/.deb — no installer to hand off to; the
        // frontend never calls this here (it opens the download page).
        return Err("in-app install isn't supported on this platform".to_string());
    }

    crate::shutdown_and_exit(app).await;
    Ok(())
}

#[cfg(test)]
mod tests {
    use super::{
        installer_filename, numbered_filename, parse_version, url_is_acceptable,
        verify_manifest_signature, Manifest,
    };

    // Mirrors packaging/generate-manifest.py's real output shape — if
    // either side renames a field, this is the cheap place to find out,
    // not a silent "no update offered" in production.
    #[test]
    fn the_generators_manifest_shape_deserializes() {
        let json = r#"{
          "schema": 1,
          "version": "0.2.0",
          "released": "2026-08-18",
          "notes": "Fixed things.",
          "notes_url": "https://www.proxy-scaler.com/#download",
          "artifacts": [
            {
              "app": "client",
              "platform": "windows",
              "arch": "x86_64",
              "variant": "cuda",
              "format": "setup-exe",
              "url": "https://dl.proxy-scaler.com/proxy-scaler-client_0.2.0_windows-x64_cuda-setup.exe",
              "size": 4100000000,
              "sha256": "abc123"
            }
          ]
        }"#;
        let manifest: Manifest = serde_json::from_slice(json.as_bytes()).expect("parse");
        assert_eq!(manifest.schema, 1);
        assert_eq!(manifest.version, "0.2.0");
        assert_eq!(manifest.artifacts.len(), 1);
        let artifact = &manifest.artifacts[0];
        assert_eq!(artifact.app, "client");
        assert_eq!(artifact.format, "setup-exe");
        assert_eq!(artifact.size, 4_100_000_000);
    }

    // Unknown fields (like "released" above) and absent optional ones
    // must both be tolerated — the manifest will evolve ahead of shipped
    // apps.
    #[test]
    fn a_minimal_manifest_still_parses() {
        let manifest: Manifest =
            serde_json::from_slice(br#"{"version": "0.3.0"}"#).expect("parse");
        assert_eq!(manifest.version, "0.3.0");
        assert_eq!(manifest.schema, 0);
        assert!(manifest.artifacts.is_empty());
    }

    #[test]
    fn versions_parse_and_compare() {
        assert_eq!(parse_version("0.1.0"), Some((0, 1, 0)));
        assert_eq!(parse_version("10.20.30"), Some((10, 20, 30)));
        assert!(parse_version("0.2.0") > parse_version("0.1.9"));
        // Tuple ordering is the property the whole check rests on:
        // 0.10.0 must beat 0.9.9, which string comparison gets wrong.
        assert!(parse_version("0.10.0") > parse_version("0.9.9"));
    }

    #[test]
    fn malformed_versions_fail_toward_silence() {
        assert_eq!(parse_version(""), None);
        assert_eq!(parse_version("1.2"), None);
        assert_eq!(parse_version("1.2.3.4"), None);
        assert_eq!(parse_version("v1.2.3"), None);
        assert_eq!(parse_version("1.2.x"), None);
    }

    // --- Signature verification --------------------------------------------
    //
    // Fixtures generated once with a throwaway deterministic keypair (a
    // pure-python RFC 8032 Ed25519 + blake2b prehash, matching what
    // `minisign -S` emits) — the test secret key exists nowhere in this
    // repo, only these outputs of it do. Real releases are signed by the
    // key described in docs/releasing.md.

    const TEST_PUBKEY: &str = "RWRQU1RFU1RLMVat9Tc/6PEP5yYdXgeSPuFy9cfRUwpFfM759ZKWI3de";
    const TEST_BODY: &[u8] = br#"{"schema":1,"version":"0.2.0","notes":"Fixed things.","notes_url":"https://www.proxy-scaler.com/#download","artifacts":[]}"#;
    const TEST_SIG: &str = "untrusted comment: proxy-scaler test signature
RURQU1RFU1RLMVaQEl+cew3CMTWDIdx6aMbJ2ZIczfPFw27Wes+8h9+nZ2usCWm4mJtTXBdPsfrsToHjzCsTlNXAHij/Sx0CTQE=
trusted comment: timestamp:1755750000\tfile:latest.json\thashed
nW+99zuAD6/3s7N13JkBOxy1H9k2G3zk+dFO8RfkgwiYAfb0Ov/FMHj8Mla6AwhA6YoiwgRFxtk8pxyYzFp3Bw==";
    // A signature by the same key over a DIFFERENT body.
    const TEST_SIG_OTHER_BODY: &str = "untrusted comment: proxy-scaler test signature
RURQU1RFU1RLMTs3eyMmdqRxuk65uCE7S+dqyjz7yGZmLL7C1pR0R6w2zmuJ5qL49aOmkqk/q71FaWBsQuUkK0FneYaWz3mXmgU=
trusted comment: timestamp:1755750000\tfile:latest.json\thashed
4ODZBJpTdLKH1RZEF5X+HbCq6q3fhSl7DBB+4XQAt1ExBmBMOACF1Dz63SzYf/00Huc8dQi5uEUC9hBlmEeiCQ==";

    #[test]
    fn a_valid_signature_verifies() {
        verify_manifest_signature(TEST_BODY, TEST_SIG, TEST_PUBKEY).expect("should verify");
    }

    #[test]
    fn a_tampered_body_fails_verification() {
        let mut tampered = TEST_BODY.to_vec();
        tampered[10] ^= 1;
        assert!(verify_manifest_signature(&tampered, TEST_SIG, TEST_PUBKEY).is_err());
    }

    #[test]
    fn a_signature_for_another_body_fails_verification() {
        assert!(verify_manifest_signature(TEST_BODY, TEST_SIG_OTHER_BODY, TEST_PUBKEY).is_err());
    }

    // The production key must be a real, parseable minisign key (a
    // broken paste would silently disable updates for a whole release)
    // — and it must reject signatures from any other key, the test
    // fixtures' included.
    #[test]
    fn the_embedded_public_key_parses_and_rejects_foreign_signatures() {
        minisign_verify::PublicKey::from_base64(super::MANIFEST_PUBKEY)
            .expect("MANIFEST_PUBKEY must be a valid minisign public key");
        assert!(verify_manifest_signature(TEST_BODY, TEST_SIG, super::MANIFEST_PUBKEY).is_err());
    }

    // --- URL and filename hygiene ------------------------------------------

    #[test]
    fn only_https_urls_are_acceptable_in_release() {
        assert!(url_is_acceptable("https://dl.proxy-scaler.com/x.exe", false));
        assert!(!url_is_acceptable("http://dl.proxy-scaler.com/x.exe", false));
        assert!(!url_is_acceptable("file:///etc/passwd", false));
        assert!(!url_is_acceptable("javascript:alert(1)", false));
        assert!(!url_is_acceptable("not a url", false));
        // The debug-build escape hatch admits http, nothing else.
        assert!(url_is_acceptable("http://127.0.0.1:8000/x.exe", true));
        assert!(!url_is_acceptable("file:///etc/passwd", true));
    }

    #[test]
    fn installer_filenames_come_from_validated_fields_only() {
        assert_eq!(
            installer_filename((0, 2, 0), "cuda", "setup-exe").as_deref(),
            Some("proxy-scaler-client_0.2.0_cuda-setup.exe")
        );
        assert_eq!(
            installer_filename((0, 2, 0), "default", "dmg").as_deref(),
            Some("proxy-scaler-client_0.2.0.dmg")
        );
        // Hostile variant characters are dropped, never joined into the
        // path.
        assert_eq!(
            installer_filename((1, 0, 0), "../../evil\\x", "setup-exe").as_deref(),
            Some("proxy-scaler-client_1.0.0_evilx-setup.exe")
        );
        assert_eq!(installer_filename((1, 0, 0), "cuda", "tar.gz"), None);
    }

    #[test]
    fn collision_suffixes_number_like_a_browser() {
        assert_eq!(numbered_filename("a-setup.exe", 0), "a-setup.exe");
        assert_eq!(numbered_filename("a-setup.exe", 1), "a-setup (1).exe");
        assert_eq!(numbered_filename("noext", 2), "noext (2)");
    }
}
