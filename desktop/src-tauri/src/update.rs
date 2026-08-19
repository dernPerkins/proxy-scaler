// The boot-time update check and the installer handoff. The manifest
// (dist/latest.json, built by packaging/generate-manifest.py and uploaded
// to R2 next to the artifacts) is fetched HERE in Rust, not in the
// webview: reqwest is CORS-exempt, so the website needs no CORS headers,
// and the check keeps working no matter what host serves the file.
//
// The actual download reuses main.rs::download_to_path (streaming,
// progress events, cancel) — this module only decides WHAT to download
// (check_for_update) and what to do with it afterwards (launch_installer).
// UpdatePrompt.tsx drives the whole flow.

use std::time::Duration;

use serde::{Deserialize, Serialize};
use tauri::{AppHandle, Manager};
use tauri_plugin_shell::ShellExt;

/// Overridable via PROXY_SCALER_UPDATE_URL — how the whole flow is tested
/// against a local python -m http.server without touching production.
const MANIFEST_URL: &str = "https://dl.proxy-scaler.com/latest.json";
/// Bumped when the manifest layout changes incompatibly; a manifest from
/// the future is skipped rather than misread, so an old installed app
/// degrades to "no update offered" instead of offering garbage.
const MANIFEST_SCHEMA: u64 = 1;
/// Generous for a ~2KB JSON file — this runs on the boot path (albeit
/// fire-and-forget) and must never hold anything for long.
const FETCH_TIMEOUT: Duration = Duration::from_secs(15);

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
    /// offers notes_url instead of an in-app download.
    artifact: Option<ArtifactOut>,
}

#[derive(Serialize)]
pub struct ArtifactOut {
    url: String,
    /// Where download_to_path should put it: the user's Downloads
    /// directory — somewhere they can find (and re-run, or delete) the
    /// installer if the handoff is canceled — not a temp dir the OS
    /// cleans behind their back mid-install.
    save_path: String,
    filename: String,
    size: u64,
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

/// Fetch the manifest and compare. Ok(None) means "nothing to offer" for
/// ANY reason — up to date, offline, host down, malformed manifest. The
/// distinction is logged, never surfaced: this runs unprompted on every
/// boot, and a user with no internet must not see an error toast for it.
#[tauri::command]
pub async fn check_for_update(app: AppHandle) -> Result<Option<UpdateInfo>, String> {
    let url = std::env::var("PROXY_SCALER_UPDATE_URL")
        .unwrap_or_else(|_| MANIFEST_URL.to_string());

    let manifest: Manifest = match fetch_manifest(&url).await {
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
        return Ok(None);
    }

    let variant = gpu_variant();
    let artifact = manifest
        .artifacts
        .iter()
        .find(|a| {
            a.app == "client"
                && a.platform == std::env::consts::OS
                && a.arch == std::env::consts::ARCH
                && a.variant == variant
                // Only formats that ARE installers get downloaded in-app;
                // archives (zip/tar.gz) would just strand the user with a
                // file to unpack by hand — the download page explains
                // those better than a silent Downloads drop would.
                && matches!(a.format.as_str(), "setup-exe" | "dmg")
        })
        .and_then(|a| {
            let filename = a.url.rsplit('/').next().unwrap_or("proxy-scaler-update")
                .to_string();
            // Downloads dir can be unresolvable (unusual setups); temp is
            // the fallback that keeps the update possible rather than
            // failing the whole offer.
            let dir = app
                .path()
                .download_dir()
                .unwrap_or_else(|_| std::env::temp_dir());
            Some(ArtifactOut {
                url: a.url.clone(),
                save_path: dir.join(&filename).to_string_lossy().into_owned(),
                filename,
                size: a.size,
                sha256: a.sha256.clone(),
            })
        });

    Ok(Some(UpdateInfo {
        current: current_text.to_string(),
        latest: manifest.version,
        notes: manifest.notes,
        notes_url: manifest.notes_url,
        artifact,
    }))
}

async fn fetch_manifest(url: &str) -> Result<Manifest, String> {
    let client = reqwest::Client::builder()
        .timeout(FETCH_TIMEOUT)
        .build()
        .map_err(|e| e.to_string())?;
    let response = client.get(url).send().await.map_err(|e| e.to_string())?;
    if !response.status().is_success() {
        return Err(format!("manifest fetch returned {}", response.status()));
    }
    // Bytes + serde_json rather than Response::json(): reqwest is built
    // without its "json" feature (see Cargo.toml).
    let body = response.bytes().await.map_err(|e| e.to_string())?;
    serde_json::from_slice(&body).map_err(|e| format!("manifest parse failed: {e}"))
}

/// Verify the downloaded installer against the manifest's size/sha256,
/// hand it to the OS, and exit. Verification is the load-bearing part:
/// the worst outcome of this whole feature would be feeding a truncated
/// or corrupted 4GB file to an installer that's about to replace a
/// working install. This is corruption protection, not tamper-proofing —
/// hash and artifact come from the same host; authenticity is the
/// platform code-signing's job.
///
/// The exit matters too: on Windows the MSI can't replace files the
/// running app holds open, and on macOS Finder shouldn't be asked to
/// replace a running .app — so after the spawn/open succeeds, this runs
/// the same graceful teardown the window's close button does (sidecar
/// stopped first) and the installer takes over.
#[tauri::command]
pub async fn launch_installer(
    app: AppHandle,
    path: String,
    expected_size: u64,
    expected_sha256: String,
) -> Result<(), String> {
    let file_path = std::path::PathBuf::from(&path);

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
    use super::{parse_version, Manifest};

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
}
