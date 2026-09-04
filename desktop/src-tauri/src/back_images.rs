// The Back Library: user-supplied art printed on a card's Reverse.
//
// App-global and client-owned. A project points at one of these by id;
// the generation server only ever holds a content-addressed cache of the
// bytes, and losing that costs the user one re-sync. See docs/adr/0003
// for why ownership splits that way, and why these are never upscaled.
//
// Two things live here that could plausibly have lived elsewhere:
//
// **Hashing is Rust's job, not the webview's.** The content hash is the
// identity the server files these under, so it has to be computed from the
// bytes actually written to disk rather than from whatever the webview
// believed it read.
//
// **So is the upload.** Same reason main.rs downloads installers in Rust:
// a multi-MB body has no business crossing the IPC boundary and being
// re-serialised as a JSON array of numbers on the way.
//
// Thumbnails are the exception and come *in* from the webview as
// already-encoded JPEG bytes, because generating them here would mean
// adding an image-decoding crate to a build that ships in six platform
// variants — for one 220px preview.
use std::path::{Path, PathBuf};

use rusqlite::{params, Connection, OptionalExtension};
use serde::{Deserialize, Serialize};
use sha2::{Digest, Sha256};
use tauri::{AppHandle, Manager};

use crate::project_store::open_db;

const BACKS_DIRNAME: &str = "backs";
const DEFAULT_BACK_IMAGE_KEY: &str = "default_back_image_id";

// Mirrors proxy_scaler/backs.py's own cap. Enforced on both sides on
// purpose: this one gives the user an error before a long upload, the
// server's protects it from any client at all.
const MAX_BYTES: usize = 50 * 1024 * 1024;

#[derive(Debug, Clone, Serialize, Deserialize)]
pub struct BackImage {
    pub id: i64,
    pub content_hash: String,
    pub label: String,
    pub original_filename: String,
    pub includes_bleed: bool,
    pub width: i64,
    pub height: i64,
    pub created_at: String,
    /// Effective print DPI at card size (63×88mm). The client warns below
    /// ~300; it never blocks, because plenty of people knowingly print a
    /// flat logo at low resolution.
    pub source_dpi: f64,
}

fn backs_dir(app: &AppHandle) -> Result<PathBuf, String> {
    let dir = app
        .path()
        .app_data_dir()
        .map_err(|e| format!("failed to resolve app data dir: {e}"))?
        .join(BACKS_DIRNAME);
    std::fs::create_dir_all(&dir).map_err(|e| format!("failed to create backs dir: {e}"))?;
    Ok(dir)
}

/// Print DPI this image achieves across a 63×88mm card, using its longer
/// edge against the card's longer edge — the same measure the server
/// reports, so the two never disagree about whether a back is low-res.
fn dpi_at_card_size(width: i64, height: i64) -> f64 {
    const CARD_HEIGHT_MM: f64 = 88.0;
    const MM_PER_IN: f64 = 25.4;
    (width.max(height) as f64) / (CARD_HEIGHT_MM / MM_PER_IN)
}

fn row_to_back_image(row: &rusqlite::Row) -> rusqlite::Result<BackImage> {
    let width: i64 = row.get("width")?;
    let height: i64 = row.get("height")?;
    Ok(BackImage {
        id: row.get("id")?,
        content_hash: row.get("content_hash")?,
        label: row.get("label")?,
        original_filename: row.get("original_filename")?,
        includes_bleed: row.get("includes_bleed")?,
        width,
        height,
        created_at: row.get("created_at")?,
        source_dpi: dpi_at_card_size(width, height),
    })
}

fn find_by_id(conn: &Connection, id: i64) -> Result<Option<BackImage>, String> {
    conn.query_row("SELECT * FROM back_images WHERE id = ?1", params![id], row_to_back_image)
    .optional()
    .map_err(|e| e.to_string())
}

#[tauri::command]
pub fn list_back_images(app: AppHandle) -> Result<Vec<BackImage>, String> {
    let conn = open_db(&app)?;
    let mut stmt = conn
        .prepare("SELECT * FROM back_images ORDER BY created_at DESC, id DESC")
        .map_err(|e| e.to_string())?;
    let rows = stmt
        .query_map([], row_to_back_image)
        .map_err(|e| e.to_string())?;
    rows.collect::<Result<Vec<_>, _>>().map_err(|e| e.to_string())
}

/// Add a Back Image to the library.
///
/// `bytes` is the file the user picked; `thumbnail` is a small JPEG the
/// webview already rendered for its own preview (see the module comment).
/// Re-adding identical bytes returns the existing row rather than a
/// duplicate — the library is content-addressed, so the same art picked
/// twice is one back, and the second pick just re-labels nothing.
#[tauri::command]
pub fn add_back_image(
    app: AppHandle,
    bytes: Vec<u8>,
    thumbnail: Vec<u8>,
    original_filename: String,
    label: String,
    width: i64,
    height: i64,
) -> Result<BackImage, String> {
    if bytes.is_empty() {
        return Err("That file was empty.".to_string());
    }
    if bytes.len() > MAX_BYTES {
        return Err(format!(
            "Back images are limited to {}MB.",
            MAX_BYTES / (1024 * 1024)
        ));
    }
    if width <= 0 || height <= 0 {
        return Err("That file could not be read as an image.".to_string());
    }

    let content_hash = format!("{:x}", Sha256::digest(&bytes));
    let conn = open_db(&app)?;
    if let Some(existing) = conn
        .query_row(
            "SELECT * FROM back_images WHERE content_hash = ?1",
            params![content_hash],
            row_to_back_image,
        )
        .optional()
        .map_err(|e| e.to_string())?
    {
        return Ok(existing);
    }

    let dir = backs_dir(&app)?;
    let extension = Path::new(&original_filename)
        .extension()
        .and_then(|e| e.to_str())
        .unwrap_or("img")
        .to_lowercase();
    let file_name = format!("{content_hash}.{extension}");
    let thumb_name = format!("{content_hash}_thumb.jpg");
    // Write the files before the row: a row pointing at a file that isn't
    // there would survive every future load and report a back the library
    // cannot actually print.
    std::fs::write(dir.join(&file_name), &bytes)
        .map_err(|e| format!("failed to save back image: {e}"))?;
    std::fs::write(dir.join(&thumb_name), &thumbnail)
        .map_err(|e| format!("failed to save back image preview: {e}"))?;

    let trimmed = label.trim();
    let label = if trimmed.is_empty() {
        original_filename.clone()
    } else {
        trimmed.to_string()
    };
    conn.execute(
        "INSERT INTO back_images
            (content_hash, label, original_filename, file_name, thumb_name,
             includes_bleed, width, height, created_at)
         VALUES (?1, ?2, ?3, ?4, ?5, 0, ?6, ?7, ?8)",
        params![
            content_hash,
            label,
            original_filename,
            file_name,
            thumb_name,
            width,
            height,
            crate::project_store::now_timestamp(),
        ],
    )
    .map_err(|e| e.to_string())?;
    let id = conn.last_insert_rowid();
    find_by_id(&conn, id)?.ok_or_else(|| "back image vanished after insert".to_string())
}

#[tauri::command]
pub fn set_back_image_label(app: AppHandle, id: i64, label: String) -> Result<(), String> {
    let conn = open_db(&app)?;
    let trimmed = label.trim();
    if trimmed.is_empty() {
        return Err("A back needs a name.".to_string());
    }
    conn.execute(
        "UPDATE back_images SET label = ?1 WHERE id = ?2",
        params![trimmed, id],
    )
    .map_err(|e| e.to_string())?;
    Ok(())
}

#[tauri::command]
pub fn set_back_image_includes_bleed(
    app: AppHandle,
    id: i64,
    includes_bleed: bool,
) -> Result<(), String> {
    let conn = open_db(&app)?;
    conn.execute(
        "UPDATE back_images SET includes_bleed = ?1 WHERE id = ?2",
        params![includes_bleed, id],
    )
    .map_err(|e| e.to_string())?;
    Ok(())
}

/// How many projects have this back selected. The delete confirmation says
/// so out loud, because those projects lose their back rather than
/// inheriting a replacement.
#[tauri::command]
pub fn count_projects_using_back_image(app: AppHandle, id: i64) -> Result<i64, String> {
    let conn = open_db(&app)?;
    conn.query_row(
        "SELECT COUNT(*) FROM projects WHERE back_image_id = ?1",
        params![id],
        |row| row.get(0),
    )
    .map_err(|e| e.to_string())
}

/// Remove a back from the library, from every project that selected it,
/// and from the app default if it held that job.
///
/// Referencing projects are left with NO back rather than being reassigned
/// to the app default. That is the deliberate choice: back printing then
/// blocks with a stated reason, which is recoverable, instead of a project
/// silently printing a different back than it did last month, which isn't
/// noticed until the paper comes out.
/// The database half of a delete, split out so it can be tested without an
/// AppHandle — same `*_row` split project_store.rs already uses. Returns
/// the (file, thumbnail) names the caller should unlink.
fn delete_back_image_row(conn: &Connection, id: i64) -> Result<Option<(String, String)>, String> {
    let names: Option<(String, String)> = conn
        .query_row(
            "SELECT file_name, thumb_name FROM back_images WHERE id = ?1",
            params![id],
            |row| Ok((row.get(0)?, row.get(1)?)),
        )
        .optional()
        .map_err(|e| e.to_string())?;

    conn.execute(
        "UPDATE projects SET back_image_id = NULL WHERE back_image_id = ?1",
        params![id],
    )
    .map_err(|e| e.to_string())?;
    if read_default_back_image_id(conn)? == Some(id) {
        crate::project_store::write_app_setting_pub(conn, DEFAULT_BACK_IMAGE_KEY, "")?;
    }
    conn.execute("DELETE FROM back_images WHERE id = ?1", params![id])
        .map_err(|e| e.to_string())?;
    Ok(names)
}

#[tauri::command]
pub fn delete_back_image(app: AppHandle, id: i64) -> Result<(), String> {
    let conn = open_db(&app)?;
    let names = delete_back_image_row(&conn, id)?;
    if let Some((file_name, thumb_name)) = names {
        let dir = backs_dir(&app)?;
        // Best-effort: the row is already gone, and a leftover orphan file
        // is inert — nothing reads that directory except by name.
        let _ = std::fs::remove_file(dir.join(file_name));
        let _ = std::fs::remove_file(dir.join(thumb_name));
    }
    Ok(())
}

/// The library thumbnail as a data URL, for rendering the gallery grid.
/// Small by construction (the webview made it), so this is the one place a
/// back's pixels legitimately cross IPC.
#[tauri::command]
pub fn back_image_thumbnail(app: AppHandle, id: i64) -> Result<Option<String>, String> {
    let conn = open_db(&app)?;
    let thumb_name: Option<String> = conn
        .query_row(
            "SELECT thumb_name FROM back_images WHERE id = ?1",
            params![id],
            |row| row.get(0),
        )
        .optional()
        .map_err(|e| e.to_string())?;
    let Some(thumb_name) = thumb_name else {
        return Ok(None);
    };
    let path = backs_dir(&app)?.join(thumb_name);
    let Ok(bytes) = std::fs::read(&path) else {
        return Ok(None);
    };
    Ok(Some(format!(
        "data:image/jpeg;base64,{}",
        base64_encode(&bytes)
    )))
}

fn read_default_back_image_id(conn: &Connection) -> Result<Option<i64>, String> {
    Ok(crate::project_store::read_app_setting_pub(conn, DEFAULT_BACK_IMAGE_KEY)?
        .and_then(|v| v.parse::<i64>().ok()))
}

#[tauri::command]
pub fn get_default_back_image_id(app: AppHandle) -> Result<Option<i64>, String> {
    let conn = open_db(&app)?;
    read_default_back_image_id(&conn)
}

/// Set (or clear, with None) the back new projects start with.
///
/// New projects take a COPY of this id at creation and keep it. Changing
/// the default later does not reach back into existing projects — a
/// project you printed last month must print identically today, and a
/// live-following default would silently change the output of every
/// project that never made a choice of its own.
#[tauri::command]
pub fn set_default_back_image_id(app: AppHandle, id: Option<i64>) -> Result<(), String> {
    let conn = open_db(&app)?;
    crate::project_store::write_app_setting_pub(
        &conn,
        DEFAULT_BACK_IMAGE_KEY,
        &id.map(|v| v.to_string()).unwrap_or_default(),
    )
}

#[derive(Debug, Clone, Serialize)]
pub struct BackSyncResult {
    pub content_hash: String,
    /// Whether these bytes had to be sent, as opposed to the server
    /// already holding them. Purely informational — the client calls sync
    /// unconditionally and this says what it cost.
    pub uploaded: bool,
}

/// Make sure a generation server holds this back's bytes.
///
/// Cheap to call before every render: a GET decides whether the multi-MB
/// POST is needed at all, so an unchanged back on a familiar server costs
/// one small request. Switching servers self-heals through exactly this
/// path — the new host misses, and the next call fills it.
#[tauri::command]
pub async fn sync_back_image(
    app: AppHandle,
    id: i64,
    server_base_url: String,
) -> Result<BackSyncResult, String> {
    let (content_hash, file_name) = {
        let conn = open_db(&app)?;
        conn.query_row(
            "SELECT content_hash, file_name FROM back_images WHERE id = ?1",
            params![id],
            |row| Ok((row.get::<_, String>(0)?, row.get::<_, String>(1)?)),
        )
        .optional()
        .map_err(|e| e.to_string())?
        .ok_or_else(|| "That back image is no longer in the library.".to_string())?
    };
    let base = server_base_url.trim_end_matches('/').to_string();
    let url = format!("{base}/api/backs/{content_hash}");

    let client = reqwest::Client::new();
    if let Ok(resp) = client.get(&url).send().await {
        if resp.status().is_success() {
            // reqwest is built without its "json" feature here (see
            // Cargo.toml) — same as update.rs, the body is read as bytes
            // and handed to serde_json directly.
            let present = resp
                .bytes()
                .await
                .ok()
                .and_then(|body| serde_json::from_slice::<serde_json::Value>(&body).ok())
                .and_then(|v| v.get("present").and_then(|p| p.as_bool()))
                .unwrap_or(false);
            if present {
                return Ok(BackSyncResult { content_hash, uploaded: false });
            }
        }
    }

    let path = backs_dir(&app)?.join(file_name);
    let bytes = std::fs::read(&path).map_err(|e| format!("failed to read back image: {e}"))?;
    let resp = client
        .post(&url)
        .header("content-type", "application/octet-stream")
        .body(bytes)
        .send()
        .await
        .map_err(|e| format!("failed to send back image to the server: {e}"))?;
    if !resp.status().is_success() {
        let status = resp.status();
        let detail = resp.text().await.unwrap_or_default();
        return Err(format!("server rejected the back image ({status}): {detail}"));
    }
    Ok(BackSyncResult { content_hash, uploaded: true })
}

/// Minimal base64 for the thumbnail data URL. Hand-rolled rather than
/// pulling a crate in for a couple of call sites whose input is a few
/// kilobytes. Shared with custom_images.rs, which needs the identical
/// thing for its own library thumbnails.
pub(crate) fn base64_encode(bytes: &[u8]) -> String {
    const TABLE: &[u8; 64] = b"ABCDEFGHIJKLMNOPQRSTUVWXYZabcdefghijklmnopqrstuvwxyz0123456789+/";
    let mut out = String::with_capacity(bytes.len().div_ceil(3) * 4);
    for chunk in bytes.chunks(3) {
        let b = [
            chunk[0],
            *chunk.get(1).unwrap_or(&0),
            *chunk.get(2).unwrap_or(&0),
        ];
        let n = ((b[0] as u32) << 16) | ((b[1] as u32) << 8) | (b[2] as u32);
        out.push(TABLE[(n >> 18 & 63) as usize] as char);
        out.push(TABLE[(n >> 12 & 63) as usize] as char);
        out.push(if chunk.len() > 1 {
            TABLE[(n >> 6 & 63) as usize] as char
        } else {
            '='
        });
        out.push(if chunk.len() > 2 {
            TABLE[(n & 63) as usize] as char
        } else {
            '='
        });
    }
    out
}

#[cfg(test)]
mod tests {
    use super::*;
    use crate::project_store::test_support::test_conn;

    fn insert_back(conn: &Connection, hash: &str) -> i64 {
        conn.execute(
            "INSERT INTO back_images
                (content_hash, label, original_filename, file_name, thumb_name,
                 includes_bleed, width, height, created_at)
             VALUES (?1, 'A back', 'back.png', ?2, ?3, 0, 745, 1040, '1')",
            params![hash, format!("{hash}.png"), format!("{hash}_thumb.jpg")],
        )
        .expect("insert back");
        conn.last_insert_rowid()
    }

    fn insert_project_with_back(conn: &Connection, name: &str, back_id: Option<i64>) -> i64 {
        conn.execute(
            "INSERT INTO projects (tag, name, import_decklist_text, back_image_id,
                                   created_at, updated_at)
             VALUES (lower(hex(randomblob(16))), ?1, '', ?2, '1', '1')",
            params![name, back_id],
        )
        .expect("insert project");
        conn.last_insert_rowid()
    }

    fn back_id_of(conn: &Connection, project_id: i64) -> Option<i64> {
        conn.query_row(
            "SELECT back_image_id FROM projects WHERE id = ?1",
            params![project_id],
            |row| row.get(0),
        )
        .expect("read project")
    }

    #[test]
    fn deleting_a_back_leaves_referencing_projects_with_none() {
        // Deliberately NOT reassigned to the app default: a project that
        // quietly starts printing a different back than it did last month
        // isn't noticed until the paper comes out, whereas a project
        // loudly missing its back is recoverable.
        let conn = test_conn();
        let back = insert_back(&conn, &"a".repeat(64));
        let other = insert_back(&conn, &"b".repeat(64));
        crate::project_store::write_app_setting_pub(
            &conn,
            DEFAULT_BACK_IMAGE_KEY,
            &other.to_string(),
        )
        .expect("set default");
        let project = insert_project_with_back(&conn, "Krenko", Some(back));

        delete_back_image_row(&conn, back).expect("delete");

        assert_eq!(back_id_of(&conn, project), None);
        // The app default was a different back, so it survives untouched.
        assert_eq!(read_default_back_image_id(&conn).expect("default"), Some(other));
    }

    #[test]
    fn deleting_the_default_back_clears_the_default() {
        let conn = test_conn();
        let back = insert_back(&conn, &"c".repeat(64));
        crate::project_store::write_app_setting_pub(
            &conn,
            DEFAULT_BACK_IMAGE_KEY,
            &back.to_string(),
        )
        .expect("set default");

        delete_back_image_row(&conn, back).expect("delete");

        assert_eq!(read_default_back_image_id(&conn).expect("default"), None);
    }

    #[test]
    fn the_library_is_content_addressed() {
        // The same art picked twice is one back, not two rows — which is
        // what lets a back shared across projects dedupe for free.
        let conn = test_conn();
        insert_back(&conn, &"d".repeat(64));
        let err = conn.execute(
            "INSERT INTO back_images
                (content_hash, label, original_filename, file_name, thumb_name,
                 includes_bleed, width, height, created_at)
             VALUES (?1, 'Same bytes', 'other.png', 'x.png', 'x_thumb.jpg', 0, 1, 1, '1')",
            params![&"d".repeat(64)],
        );
        assert!(err.is_err(), "a duplicate content_hash must be refused");
    }

    #[test]
    fn base64_matches_known_vectors() {
        assert_eq!(base64_encode(b""), "");
        assert_eq!(base64_encode(b"f"), "Zg==");
        assert_eq!(base64_encode(b"fo"), "Zm8=");
        assert_eq!(base64_encode(b"foo"), "Zm9v");
        assert_eq!(base64_encode(b"foob"), "Zm9vYg==");
        assert_eq!(base64_encode(b"fooba"), "Zm9vYmE=");
        assert_eq!(base64_encode(b"foobar"), "Zm9vYmFy");
    }

    #[test]
    fn dpi_is_measured_across_the_cards_long_edge() {
        // A 1040px-tall image over 88mm is ~300 DPI — the floor the
        // low-resolution warning uses.
        let dpi = dpi_at_card_size(745, 1040);
        assert!((dpi - 300.0).abs() < 1.0, "got {dpi}");
        // Orientation must not change the answer: the longer edge is the
        // one that spans the card's longer edge either way.
        assert_eq!(dpi_at_card_size(745, 1040), dpi_at_card_size(1040, 745));
    }
}
