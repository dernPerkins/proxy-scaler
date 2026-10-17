// MPC Fill provenance for library images, shared by the Custom Image and
// Back Image libraries.
//
// An image picked from MPC Fill is an ordinary library image (its bytes
// came from the generation server's /api/mpcfill/import) plus where it
// came from: the Google Drive file id, the uploader and MPC Fill's name
// for it. The id is what lets a *different* generation server fill its
// copy straight from Google (POST /api/{customs|backs}/{hash}/fetch)
// instead of this machine uploading the file — see sync_step below.
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
