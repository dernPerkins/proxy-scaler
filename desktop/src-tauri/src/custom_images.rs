// The Custom Image library: user-supplied art printed on a card's *front*.
//
// App-global and client-owned, exactly like the Back Library next door
// (back_images.rs) — a project card points at one of these by id, and the
// generation server only ever holds a content-addressed cache of the
// bytes, filled on demand and costing one re-sync if lost. That deferral
// is the point: an image the user never upscales and never exports need
// never leave this machine.
//
// The difference from a Back Image is that a Custom Image *is* a card. It
// takes a row in the decklist, carries a quantity, and goes through the
// upscale pipeline. Since it has no Scryfall printing, the generation
// database identifies it by the sha256 of its bytes instead — see
// proxy_scaler/customs.py and db.py migration 008.
//
// The same two ownership rules as back_images.rs apply here, for the same
// reasons: hashing happens in Rust because the hash is the identity the
// server files these under and must come from the bytes actually written
// to disk; uploading happens in Rust because a multi-MB body has no
// business crossing the IPC boundary as a JSON array of numbers.
// Thumbnails still come *in* from the webview as encoded JPEG, to avoid
// adding an image-decoding crate to a build that ships in six platform
// variants.
use std::path::{Path, PathBuf};

use rusqlite::{params, Connection, OptionalExtension};
use serde::{Deserialize, Serialize};
use sha2::{Digest, Sha256};
use tauri::{AppHandle, Manager};

use crate::project_store::open_db;

const CUSTOMS_DIRNAME: &str = "customs";

// Mirrors proxy_scaler/customs.py's own cap. Enforced on both sides on
// purpose: this one gives the user an error before a long upload, the
// server's protects it from any client at all.
const MAX_BYTES: usize = 50 * 1024 * 1024;

#[derive(Debug, Clone, Serialize, Deserialize)]
pub struct CustomImage {
    pub id: i64,
    pub content_hash: String,
    pub label: String,
    pub original_filename: String,
    pub width: i64,
    pub height: i64,
    pub created_at: String,
    /// Effective print DPI at card size (63×88mm). The client warns below
    /// ~300; it never blocks, because upscaling is a real remedy here and
    /// plenty of art is fine soft.
    pub source_dpi: f64,
}

fn customs_dir(app: &AppHandle) -> Result<PathBuf, String> {
    let dir = app
        .path()
        .app_data_dir()
        .map_err(|e| format!("failed to resolve app data dir: {e}"))?
        .join(CUSTOMS_DIRNAME);
    std::fs::create_dir_all(&dir).map_err(|e| format!("failed to create customs dir: {e}"))?;
    Ok(dir)
}

/// Print DPI this image achieves across a 63×88mm card, using its longer
/// edge against the card's longer edge — the same measure the server
/// reports (proxy_scaler/dpi.py::dpi_at_card_size), so the two never
/// disagree about whether an image is low-res.
fn dpi_at_card_size(width: i64, height: i64) -> f64 {
    const CARD_HEIGHT_MM: f64 = 88.0;
    const MM_PER_IN: f64 = 25.4;
    (width.max(height) as f64) / (CARD_HEIGHT_MM / MM_PER_IN)
}

/// The card name a dropped file gets: its filename without the extension.
/// Kept verbatim beyond that — no de-underscoring or title-casing, because
/// a guess that mangles a deliberately-named file is more annoying than a
/// name the user can already edit.
pub fn label_from_filename(original_filename: &str) -> String {
    let stem = Path::new(original_filename)
        .file_stem()
        .and_then(|s| s.to_str())
        .unwrap_or("")
        // A dotfile has no extension to strip, so file_stem() hands back
        // the leading dot and all — ".png" would become a card literally
        // named ".png".
        .trim_start_matches('.')
        .trim();
    if stem.is_empty() {
        "Custom card".to_string()
    } else {
        stem.to_string()
    }
}

fn row_to_custom_image(row: &rusqlite::Row) -> rusqlite::Result<CustomImage> {
    let width: i64 = row.get("width")?;
    let height: i64 = row.get("height")?;
    Ok(CustomImage {
        id: row.get("id")?,
        content_hash: row.get("content_hash")?,
        label: row.get("label")?,
        original_filename: row.get("original_filename")?,
        width,
        height,
        created_at: row.get("created_at")?,
        source_dpi: dpi_at_card_size(width, height),
    })
}

fn find_by_id(conn: &Connection, id: i64) -> Result<Option<CustomImage>, String> {
    conn.query_row(
        "SELECT * FROM custom_images WHERE id = ?1",
        params![id],
        row_to_custom_image,
    )
    .optional()
    .map_err(|e| e.to_string())
}

#[tauri::command]
pub fn list_custom_images(app: AppHandle) -> Result<Vec<CustomImage>, String> {
    let conn = open_db(&app)?;
    let mut stmt = conn
        .prepare("SELECT * FROM custom_images ORDER BY created_at DESC, id DESC")
        .map_err(|e| e.to_string())?;
    let rows = stmt
        .query_map([], row_to_custom_image)
        .map_err(|e| e.to_string())?;
    rows.collect::<Result<Vec<_>, _>>().map_err(|e| e.to_string())
}

/// Add a Custom Image to the library.
///
/// `bytes` is the file the user picked; `thumbnail` is a small JPEG the
/// webview already rendered for its own preview (see the module comment).
/// Re-adding identical bytes returns the existing row rather than a
/// duplicate — the library is content-addressed, so the same art dropped
/// twice (or dropped into a second project) is one image, one upload, and
/// one upscale.
#[tauri::command]
pub fn add_custom_image(
    app: AppHandle,
    bytes: Vec<u8>,
    thumbnail: Vec<u8>,
    original_filename: String,
    width: i64,
    height: i64,
) -> Result<CustomImage, String> {
    if bytes.is_empty() {
        return Err("That file was empty.".to_string());
    }
    if bytes.len() > MAX_BYTES {
        return Err(format!(
            "Custom images are limited to {}MB.",
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
            "SELECT * FROM custom_images WHERE content_hash = ?1",
            params![content_hash],
            row_to_custom_image,
        )
        .optional()
        .map_err(|e| e.to_string())?
    {
        return Ok(existing);
    }

    let dir = customs_dir(&app)?;
    let extension = Path::new(&original_filename)
        .extension()
        .and_then(|e| e.to_str())
        .unwrap_or("img")
        .to_lowercase();
    let file_name = format!("{content_hash}.{extension}");
    let thumb_name = format!("{content_hash}_thumb.jpg");
    // Write the files before the row: a row pointing at a file that isn't
    // there would survive every future load and report an image the
    // library cannot actually print.
    std::fs::write(dir.join(&file_name), &bytes)
        .map_err(|e| format!("failed to save custom image: {e}"))?;
    std::fs::write(dir.join(&thumb_name), &thumbnail)
        .map_err(|e| format!("failed to save custom image preview: {e}"))?;

    conn.execute(
        "INSERT INTO custom_images
            (content_hash, label, original_filename, file_name, thumb_name,
             width, height, created_at)
         VALUES (?1, ?2, ?3, ?4, ?5, ?6, ?7, ?8)",
        params![
            content_hash,
            label_from_filename(&original_filename),
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
    find_by_id(&conn, id)?.ok_or_else(|| "custom image vanished after insert".to_string())
}

#[tauri::command]
pub fn set_custom_image_label(app: AppHandle, id: i64, label: String) -> Result<(), String> {
    let conn = open_db(&app)?;
    let trimmed = label.trim();
    if trimmed.is_empty() {
        return Err("A custom image needs a name.".to_string());
    }
    conn.execute(
        "UPDATE custom_images SET label = ?1 WHERE id = ?2",
        params![trimmed, id],
    )
    .map_err(|e| e.to_string())?;
    Ok(())
}

/// How many project cards use this image. The delete confirmation says so
/// out loud, because those cards go with it.
#[tauri::command]
pub fn count_cards_using_custom_image(app: AppHandle, id: i64) -> Result<i64, String> {
    let conn = open_db(&app)?;
    conn.query_row(
        "SELECT COUNT(*) FROM project_cards WHERE custom_image_id = ?1",
        params![id],
        |row| row.get(0),
    )
    .map_err(|e| e.to_string())
}

/// The database half of a delete, split out so it can be tested without an
/// AppHandle — the same `*_row` split project_store.rs already uses.
/// Returns the (file, thumbnail) names the caller should unlink.
fn delete_custom_image_row(conn: &Connection, id: i64) -> Result<Option<(String, String)>, String> {
    let names: Option<(String, String)> = conn
        .query_row(
            "SELECT file_name, thumb_name FROM custom_images WHERE id = ?1",
            params![id],
            |row| Ok((row.get(0)?, row.get(1)?)),
        )
        .optional()
        .map_err(|e| e.to_string())?;

    // Unlike a Back Image (which projects merely *select*, and so can be
    // left with none), a Custom Image IS the card. A project card that
    // outlived its image would be a row with no art and no way to get any,
    // so the cards go too.
    conn.execute(
        "DELETE FROM project_cards WHERE custom_image_id = ?1",
        params![id],
    )
    .map_err(|e| e.to_string())?;
    conn.execute("DELETE FROM custom_images WHERE id = ?1", params![id])
        .map_err(|e| e.to_string())?;
    Ok(names)
}

#[tauri::command]
pub fn delete_custom_image(app: AppHandle, id: i64) -> Result<(), String> {
    let conn = open_db(&app)?;
    let names = delete_custom_image_row(&conn, id)?;
    if let Some((file_name, thumb_name)) = names {
        let dir = customs_dir(&app)?;
        // Best-effort: the row is already gone, and a leftover orphan file
        // is inert — nothing reads that directory except by name.
        let _ = std::fs::remove_file(dir.join(file_name));
        let _ = std::fs::remove_file(dir.join(thumb_name));
    }
    Ok(())
}

/// The library thumbnail as a data URL, for rendering the gallery grid and
/// the decklist rows. Small by construction (the webview made it), so this
/// is the one place a custom image's pixels legitimately cross IPC.
#[tauri::command]
pub fn custom_image_thumbnail(app: AppHandle, id: i64) -> Result<Option<String>, String> {
    let conn = open_db(&app)?;
    let thumb_name: Option<String> = conn
        .query_row(
            "SELECT thumb_name FROM custom_images WHERE id = ?1",
            params![id],
            |row| row.get(0),
        )
        .optional()
        .map_err(|e| e.to_string())?;
    let Some(thumb_name) = thumb_name else {
        return Ok(None);
    };
    let path = customs_dir(&app)?.join(thumb_name);
    let Ok(bytes) = std::fs::read(&path) else {
        return Ok(None);
    };
    Ok(Some(format!(
        "data:image/jpeg;base64,{}",
        crate::back_images::base64_encode(&bytes)
    )))
}

#[derive(Debug, Clone, Serialize)]
pub struct CustomSyncResult {
    pub content_hash: String,
    /// Whether these bytes had to be sent, as opposed to the server
    /// already holding them. Purely informational — the client calls sync
    /// unconditionally and this says what it cost.
    pub uploaded: bool,
}

/// Make sure a generation server holds this image's bytes.
///
/// This is the whole "don't upload unless the server needs it" mechanism.
/// Called before a generate run and before an export, never on add: a GET
/// decides whether the multi-MB POST is needed at all, so an image the
/// server already has costs one small request, and an image the user never
/// generates or exports is never sent anywhere. Switching servers
/// self-heals through exactly this path — the new host misses, and the
/// next call fills it.
#[tauri::command]
pub async fn sync_custom_image(
    app: AppHandle,
    id: i64,
    server_base_url: String,
) -> Result<CustomSyncResult, String> {
    let (content_hash, file_name) = {
        let conn = open_db(&app)?;
        conn.query_row(
            "SELECT content_hash, file_name FROM custom_images WHERE id = ?1",
            params![id],
            |row| Ok((row.get::<_, String>(0)?, row.get::<_, String>(1)?)),
        )
        .optional()
        .map_err(|e| e.to_string())?
        .ok_or_else(|| "That custom image is no longer in the library.".to_string())?
    };
    let base = server_base_url.trim_end_matches('/').to_string();
    let url = format!("{base}/api/customs/{content_hash}");

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
                return Ok(CustomSyncResult { content_hash, uploaded: false });
            }
        }
    }

    let path = customs_dir(&app)?.join(file_name);
    let bytes =
        std::fs::read(&path).map_err(|e| format!("failed to read custom image: {e}"))?;
    let resp = client
        .post(&url)
        .header("content-type", "application/octet-stream")
        .body(bytes)
        .send()
        .await
        .map_err(|e| format!("failed to send custom image to the server: {e}"))?;
    if !resp.status().is_success() {
        let status = resp.status();
        let detail = resp.text().await.unwrap_or_default();
        return Err(format!("server rejected the custom image ({status}): {detail}"));
    }
    Ok(CustomSyncResult { content_hash, uploaded: true })
}

#[cfg(test)]
mod tests {
    use super::*;

    #[test]
    fn label_is_the_filename_stem() {
        assert_eq!(label_from_filename("My Alter.png"), "My Alter");
        assert_eq!(label_from_filename("goblin_token.v2.jpeg"), "goblin_token.v2");
        assert_eq!(label_from_filename("no-extension"), "no-extension");
        assert_eq!(label_from_filename(""), "Custom card");
        // A dotfile has no extension to strip, so the leading dot is
        // trimmed rather than becoming part of the card's name.
        assert_eq!(label_from_filename(".png"), "png");
        assert_eq!(label_from_filename("."), "Custom card");
    }

    #[test]
    fn dpi_matches_the_servers_measure() {
        // 745x1040 is Scryfall's own PNG size, ~300 DPI at card height.
        assert!((dpi_at_card_size(745, 1040) - 300.0).abs() < 1.0);
        // Orientation-independent: the longer edge is what's compared.
        assert_eq!(dpi_at_card_size(1040, 745), dpi_at_card_size(745, 1040));
    }
}
