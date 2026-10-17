// MPC Fill provenance for library images, shared by the Custom Image and
// Back Image libraries.
//
// An image picked from MPC Fill is an ordinary library image plus where it
// came from: the Google Drive file id, the uploader and MPC Fill's name
// for it. Its bytes come from the generation server's /api/mpcfill/import
// (fetch_from_server, below), which downloads the file from Google, keeps
// a copy and returns it — straight into Rust, so a 25 MB original never
// crosses into the webview as a JSON array of numbers. The Drive id is
// also what lets a *different* generation server fill its copy straight
// from Google (POST /api/{customs|backs}/{hash}/fetch) instead of this
// machine uploading the file — see sync_step below.
//
// Nothing here talks to MPC Fill or Google; the client never does.
use rusqlite::{params, Connection};
use serde::{Deserialize, Serialize};

/// Columns both library tables gain, added to existing databases by
/// project_store::open_db.
pub(crate) const MPC_ADDED_COLUMNS: &[(&str, &str)] = &[
    ("mpc_drive_id", "TEXT"),
    ("mpc_source", "TEXT"),
    ("mpc_name", "TEXT"),
];

#[derive(Debug, Clone, PartialEq, Serialize, Deserialize)]
pub struct MpcProvenance {
    pub drive_id: String,
    pub source: String,
    pub name: String,
}

/// A Google Drive file id, or an error. Mirrors the server's own check
/// (proxy_scaler/drive_images.py::validate_drive_id).
pub(crate) fn validate_drive_id(drive_id: &str) -> Result<String, String> {
    let id = drive_id.trim();
    let ok = (20..=80).contains(&id.len())
        && id
            .chars()
            .all(|c| c.is_ascii_alphanumeric() || c == '_' || c == '-');
    if ok {
        Ok(id.to_string())
    } else {
        Err("That isn't an MPC Fill image id.".to_string())
    }
}

impl MpcProvenance {
    pub(crate) fn new(drive_id: &str, source: &str, name: &str) -> Result<Self, String> {
        Ok(Self {
            drive_id: validate_drive_id(drive_id)?,
            source: source.trim().to_string(),
            name: name.trim().to_string(),
        })
    }
}

/// Provenance from a library row, or None for an image the user uploaded.
pub(crate) fn provenance_from_row(row: &rusqlite::Row) -> rusqlite::Result<Option<MpcProvenance>> {
    let drive_id: Option<String> = row.get("mpc_drive_id")?;
    Ok(drive_id.map(|drive_id| MpcProvenance {
        drive_id,
        source: row.get::<_, Option<String>>("mpc_source").ok().flatten().unwrap_or_default(),
        name: row.get::<_, Option<String>>("mpc_name").ok().flatten().unwrap_or_default(),
    }))
}

/// Record where an existing library image came from, unless it already
/// says. Used when an MPC pick turns out to be bytes the library already
/// holds: the row keeps its own label and bleed (the user's declarations
/// win), it just learns its Drive id.
pub(crate) fn attach_provenance_row(
    conn: &Connection,
    table: &str,
    id: i64,
    mpc: &MpcProvenance,
) -> Result<(), String> {
    // `table` is one of two constants at the call sites, never user input.
    debug_assert!(table == "custom_images" || table == "back_images");
    conn.execute(
        &format!(
            "UPDATE {table} SET mpc_drive_id = ?1, mpc_source = ?2, mpc_name = ?3
             WHERE id = ?4 AND mpc_drive_id IS NULL"
        ),
        params![mpc.drive_id, mpc.source, mpc.name, id],
    )
    .map_err(|e| e.to_string())?;
    Ok(())
}

// Same cap as a dropped file (custom_images.rs / back_images.rs MAX_BYTES).
const MAX_IMPORT_BYTES: usize = 50 * 1024 * 1024;

/// One MPC Fill image as the generation server handed it over.
pub(crate) struct FetchedArt {
    pub bytes: Vec<u8>,
    pub thumbnail: Vec<u8>,
    pub width: i64,
    pub height: i64,
    /// The bleed the server stored it under: the requested amount, or 0
    /// for a file whose aspect says it has none.
    pub declared_bleed_mm: f64,
    pub extension: &'static str,
}

fn header_f64(resp: &reqwest::Response, name: &str) -> Option<f64> {
    resp.headers().get(name)?.to_str().ok()?.trim().parse().ok()
}

/// FastAPI's `detail` message, or the status line.
async fn error_detail(resp: reqwest::Response) -> String {
    let status = resp.status();
    let body = resp.bytes().await.unwrap_or_default();
    serde_json::from_slice::<serde_json::Value>(&body)
        .ok()
        .and_then(|v| v.get("detail").and_then(|d| d.as_str()).map(str::to_string))
        .unwrap_or_else(|| format!("The generation server answered {status}."))
}

/// Ask the generation server at `base` to download one MPC Fill image
/// (POST /api/mpcfill/import), plus its small preview for the library
/// thumbnail (GET /api/mpcfill/thumb). `kind` is "custom" or "back".
pub(crate) async fn fetch_from_server(
    base: &str,
    drive_id: &str,
    kind: &str,
    bleed_mm: f64,
) -> Result<FetchedArt, String> {
    let base = base.trim_end_matches('/');
    let client = reqwest::Client::builder()
        .timeout(std::time::Duration::from_secs(180))
        .build()
        .map_err(|e| e.to_string())?;
    let body = serde_json::to_vec(&serde_json::json!({
        "drive_id": drive_id,
        "kind": kind,
        "bleed_mm": bleed_mm,
    }))
    .map_err(|e| e.to_string())?;
    let resp = client
        .post(format!("{base}/api/mpcfill/import"))
        .header("content-type", "application/json")
        .body(body)
        .send()
        .await
        .map_err(|e| format!("Couldn't reach the generation server: {e}"))?;
    if !resp.status().is_success() {
        return Err(error_detail(resp).await);
    }
    let extension = match resp
        .headers()
        .get("content-type")
        .and_then(|v| v.to_str().ok())
        .unwrap_or("")
    {
        "image/jpeg" => "jpg",
        "image/webp" => "webp",
        _ => "png",
    };
    let width = header_f64(&resp, "x-image-width").unwrap_or(0.0) as i64;
    let height = header_f64(&resp, "x-image-height").unwrap_or(0.0) as i64;
    let declared_bleed_mm = header_f64(&resp, "x-declared-bleed-mm").unwrap_or(bleed_mm);
    let bytes = resp.bytes().await.map_err(|e| format!("The download was cut off: {e}"))?;
    if bytes.len() > MAX_IMPORT_BYTES {
        return Err("That image is larger than 50MB.".to_string());
    }
    let thumb = client
        .get(format!("{base}/api/mpcfill/thumb/{drive_id}?size=small"))
        .send()
        .await
        .map_err(|e| format!("Couldn't load the image's preview: {e}"))?;
    if !thumb.status().is_success() {
        return Err(error_detail(thumb).await);
    }
    let thumbnail = thumb
        .bytes()
        .await
        .map_err(|e| format!("Couldn't load the image's preview: {e}"))?;
    Ok(FetchedArt {
        bytes: bytes.to_vec(),
        thumbnail: thumbnail.to_vec(),
        width,
        height,
        declared_bleed_mm,
        extension,
    })
}

/// A library file name for MPC Fill's name: path-safe, with the extension
/// the bytes are (the stored file and the viewer's MIME type go by it).
pub(crate) fn library_filename(name: &str, extension: &str) -> String {
    let cleaned: String = name
        .chars()
        .map(|c| if "\\/:*?\"<>|".contains(c) || c.is_control() { ' ' } else { c })
        .collect();
    let stem = cleaned.split_whitespace().collect::<Vec<_>>().join(" ");
    let stem = if stem.is_empty() { "MPC art".to_string() } else { stem };
    format!("{stem}.{extension}")
}

/// What a sync does after asking the server whether it has an image.
#[derive(Debug, Clone, Copy, PartialEq, Eq)]
pub(crate) enum SyncStep {
    /// The server has it, under the right bleed: nothing to send.
    Done,
    /// Ask the server to download it from Google Drive itself; upload if
    /// that fails.
    FetchThenUpload,
    /// Send the bytes.
    Upload,
}

/// `present` / `bleed_matches` are the server's answer (None when the GET
/// failed outright); `has_drive_id` says the image came from MPC Fill.
pub(crate) fn sync_step(answer: Option<(bool, bool)>, has_drive_id: bool) -> SyncStep {
    match answer {
        Some((true, true)) => SyncStep::Done,
        _ if has_drive_id => SyncStep::FetchThenUpload,
        _ => SyncStep::Upload,
    }
}

/// POST {url}/fetch with `body`; true when the server filled its copy.
/// Any failure (an older server's 404, Google refusing, a replaced file's
/// 409, a timeout) is false, and the caller uploads instead.
pub(crate) async fn server_fetch(
    client: &reqwest::Client,
    url: &str,
    body: &serde_json::Value,
) -> bool {
    let Ok(payload) = serde_json::to_vec(body) else {
        return false;
    };
    match client
        .post(format!("{url}/fetch"))
        .header("content-type", "application/json")
        .timeout(std::time::Duration::from_secs(180))
        .body(payload)
        .send()
        .await
    {
        Ok(resp) => resp.status().is_success(),
        Err(_) => false,
    }
}

#[cfg(test)]
mod tests {
    use super::*;

    #[test]
    fn drive_ids_are_validated() {
        assert!(validate_drive_id("1taR38keSBJgplwmZkX3NETXQSblhPYSh").is_ok());
        assert!(validate_drive_id("short").is_err());
        assert!(validate_drive_id("../../etc/passwd/aaaaaaaaaaaaaaaa").is_err());
        assert!(validate_drive_id(&"a".repeat(81)).is_err());
    }

    #[test]
    fn library_filenames_are_path_safe() {
        assert_eq!(library_filename("Fire // Ice", "png"), "Fire Ice.png");
        assert_eq!(library_filename("Bolt (Full Art) [x]", "jpg"), "Bolt (Full Art) [x].jpg");
        assert_eq!(library_filename(" :: ", "png"), "MPC art.png");
    }

    #[test]
    fn sync_steps() {
        assert_eq!(sync_step(Some((true, true)), true), SyncStep::Done);
        assert_eq!(sync_step(Some((true, true)), false), SyncStep::Done);
        // Present under a different bleed: the server's crop is wrong.
        assert_eq!(sync_step(Some((true, false)), true), SyncStep::FetchThenUpload);
        assert_eq!(sync_step(Some((true, false)), false), SyncStep::Upload);
        assert_eq!(sync_step(Some((false, false)), true), SyncStep::FetchThenUpload);
        // The GET itself failed: still worth trying the fetch.
        assert_eq!(sync_step(None, true), SyncStep::FetchThenUpload);
        assert_eq!(sync_step(None, false), SyncStep::Upload);
    }
}
