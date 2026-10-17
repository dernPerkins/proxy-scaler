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
// upscale pipeline when the project's custom_upscale setting asks for it
// (always registered as printable either way). Since it has no Scryfall
// printing, the generation
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

use crate::mpc_art::{self, MpcProvenance, SyncStep};
use crate::project_store::open_db;

const CUSTOMS_DIRNAME: &str = "customs";

// Mirrors proxy_scaler/customs.py's own cap. Enforced on both sides on
// purpose: this one gives the user an error before a long upload, the
// server's protects it from any client at all.
const MAX_BYTES: usize = 50 * 1024 * 1024;

// Mirrors proxy_scaler/dpi.py::MAX_BLEED_MM. Past this a bleed is a typo,
// not a print spec.
pub(crate) const MAX_BLEED_MM: f64 = 10.0;

// MakePlayingCards' bleed (1/8 in per side), and the custom_images.bleed_mm
// column default: what a new row is declared with until the user says
// otherwise.
pub(crate) const DEFAULT_BLEED_MM: f64 = 3.175;

#[derive(Debug, Clone, Serialize, Deserialize)]
pub struct CustomImage {
    pub id: i64,
    pub content_hash: String,
    pub label: String,
    pub original_filename: String,
    /// The user's declaration that the file already carries bleed, and
    /// how much (mm per side). `bleed_mm` is meaningful only while
    /// `includes_bleed` is set; it is kept otherwise so re-ticking the
    /// box restores the typed amount.
    pub includes_bleed: bool,
    pub bleed_mm: f64,
    pub width: i64,
    pub height: i64,
    pub created_at: String,
    /// Effective print DPI at card size (63×88mm). The client warns below
    /// ~300; it never blocks, because upscaling is a real remedy here and
    /// plenty of art is fine soft.
    pub source_dpi: f64,
    /// Where the image came from when it was picked on MPC Fill; None for
    /// the user's own uploads.
    pub mpc: Option<MpcProvenance>,
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
/// disagree about whether an image is low-res. A file declared to carry
/// `bleed_mm` of bleed spans (88 + 2*bleed) mm on its long edge, and is
/// measured against that.
pub(crate) fn dpi_at_card_size(width: i64, height: i64, bleed_mm: f64) -> f64 {
    const CARD_HEIGHT_MM: f64 = 88.0;
    const MM_PER_IN: f64 = 25.4;
    (width.max(height) as f64) / ((CARD_HEIGHT_MM + 2.0 * bleed_mm) / MM_PER_IN)
}

/// The bleed a declaration amounts to: the typed amount when the box is
/// ticked, none otherwise. What the server is told, and what the DPI is
/// measured against.
pub(crate) fn effective_bleed_mm(includes_bleed: bool, bleed_mm: f64) -> f64 {
    if includes_bleed {
        bleed_mm
    } else {
        0.0
    }
}

pub(crate) fn validate_bleed_mm(bleed_mm: f64) -> Result<f64, String> {
    if !bleed_mm.is_finite() || bleed_mm < 0.0 || bleed_mm > MAX_BLEED_MM {
        return Err(format!(
            "Bleed must be between 0 and {MAX_BLEED_MM} mm per side."
        ));
    }
    Ok(bleed_mm)
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
    let includes_bleed: bool = row.get("includes_bleed")?;
    let bleed_mm: f64 = row.get("bleed_mm")?;
    Ok(CustomImage {
        id: row.get("id")?,
        content_hash: row.get("content_hash")?,
        label: row.get("label")?,
        original_filename: row.get("original_filename")?,
        includes_bleed,
        bleed_mm,
        width,
        height,
        created_at: row.get("created_at")?,
        source_dpi: dpi_at_card_size(width, height, effective_bleed_mm(includes_bleed, bleed_mm)),
        mpc: mpc_art::provenance_from_row(row)?,
    })
}

fn find_by_hash(conn: &Connection, content_hash: &str) -> Result<Option<CustomImage>, String> {
    conn.query_row(
        "SELECT * FROM custom_images WHERE content_hash = ?1",
        params![content_hash],
        row_to_custom_image,
    )
    .optional()
    .map_err(|e| e.to_string())
}

/// A library row about to be inserted (its files already written).
struct NewCustomImageRow<'a> {
    content_hash: &'a str,
    label: String,
    original_filename: &'a str,
    file_name: &'a str,
    thumb_name: &'a str,
    includes_bleed: bool,
    bleed_mm: f64,
    width: i64,
    height: i64,
    mpc: Option<&'a MpcProvenance>,
}

fn insert_custom_image_row(conn: &Connection, row: &NewCustomImageRow) -> Result<CustomImage, String> {
    conn.execute(
        "INSERT INTO custom_images
            (content_hash, label, original_filename, file_name, thumb_name,
             includes_bleed, bleed_mm, width, height, created_at,
             mpc_drive_id, mpc_source, mpc_name)
         VALUES (?1, ?2, ?3, ?4, ?5, ?6, ?7, ?8, ?9, ?10, ?11, ?12, ?13)",
        params![
            row.content_hash,
            row.label,
            row.original_filename,
            row.file_name,
            row.thumb_name,
            row.includes_bleed,
            row.bleed_mm,
            row.width,
            row.height,
            crate::project_store::now_timestamp(),
            row.mpc.map(|m| m.drive_id.as_str()),
            row.mpc.map(|m| m.source.as_str()),
            row.mpc.map(|m| m.name.as_str()),
        ],
    )
    .map_err(|e| e.to_string())?;
    let id = conn.last_insert_rowid();
    find_by_id(conn, id)?.ok_or_else(|| "custom image vanished after insert".to_string())
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
    add_image(&app, &bytes, &thumbnail, &original_filename, width, height, None, None)
}

/// Put a Custom Image picked on MPC Fill in the library: the generation
/// server at `server_base_url` downloads it (and keeps a copy, so the next
/// sync uploads nothing) and hands the bytes straight to Rust. Art picked
/// before comes back from the library without another download.
///
/// A new row is named after MPC Fill's name and declared with the bleed
/// the server stored it under. If the library already holds these bytes,
/// that row keeps its own label and bleed — the user's declarations win —
/// and only learns its provenance if it had none.
#[tauri::command]
pub async fn import_mpc_custom_image(
    app: AppHandle,
    server_base_url: String,
    drive_id: String,
    mpc_source: String,
    mpc_name: String,
    bleed_mm: f64,
) -> Result<CustomImage, String> {
    let mpc = MpcProvenance::new(&drive_id, &mpc_source, &mpc_name)?;
    let bleed_mm = validate_bleed_mm(bleed_mm)?;
    if let Some(found) = find_by_drive_id(&open_db(&app)?, &mpc.drive_id)? {
        return Ok(found);
    }
    let art = mpc_art::fetch_from_server(&server_base_url, &mpc.drive_id, "custom", bleed_mm).await?;
    let filename = mpc_art::library_filename(&mpc.name, art.extension);
    add_image(
        &app,
        &art.bytes,
        &art.thumbnail,
        &filename,
        art.width,
        art.height,
        // Kept while unticked, so ticking the box later restores it.
        Some((mpc.name.clone(), art.declared_bleed_mm > 0.0, bleed_mm)),
        Some(&mpc),
    )
}

fn find_by_drive_id(conn: &Connection, drive_id: &str) -> Result<Option<CustomImage>, String> {
    conn.query_row(
        "SELECT * FROM custom_images WHERE mpc_drive_id = ?1 ORDER BY id LIMIT 1",
        params![drive_id],
        row_to_custom_image,
    )
    .optional()
    .map_err(|e| e.to_string())
}

/// Shared by both add commands. `declared` is (label, includes_bleed,
/// bleed_mm) for a new row; None derives the label from the filename and
/// leaves the bleed at the table's defaults.
#[allow(clippy::too_many_arguments)]
fn add_image(
    app: &AppHandle,
    bytes: &[u8],
    thumbnail: &[u8],
    original_filename: &str,
    width: i64,
    height: i64,
    declared: Option<(String, bool, f64)>,
    mpc: Option<&MpcProvenance>,
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

    let content_hash = format!("{:x}", Sha256::digest(bytes));
    let conn = open_db(app)?;
    if let Some(existing) = find_by_hash(&conn, &content_hash)? {
        if let Some(mpc) = mpc {
            mpc_art::attach_provenance_row(&conn, "custom_images", existing.id, mpc)?;
            return find_by_id(&conn, existing.id)?
                .ok_or_else(|| "custom image vanished".to_string());
        }
        return Ok(existing);
    }

    let dir = customs_dir(app)?;
    let extension = Path::new(original_filename)
        .extension()
        .and_then(|e| e.to_str())
        .unwrap_or("img")
        .to_lowercase();
    let file_name = format!("{content_hash}.{extension}");
    let thumb_name = format!("{content_hash}_thumb.jpg");
    // Write the files before the row: a row pointing at a file that isn't
    // there would survive every future load and report an image the
    // library cannot actually print.
    std::fs::write(dir.join(&file_name), bytes)
        .map_err(|e| format!("failed to save custom image: {e}"))?;
    std::fs::write(dir.join(&thumb_name), thumbnail)
        .map_err(|e| format!("failed to save custom image preview: {e}"))?;

    let (label, includes_bleed, bleed_mm) = match declared {
        Some((label, includes_bleed, bleed_mm)) if !label.trim().is_empty() => {
            (label.trim().to_string(), includes_bleed, bleed_mm)
        }
        Some((_, includes_bleed, bleed_mm)) => {
            (label_from_filename(original_filename), includes_bleed, bleed_mm)
        }
        None => (label_from_filename(original_filename), false, DEFAULT_BLEED_MM),
    };
    insert_custom_image_row(
        &conn,
        &NewCustomImageRow {
            content_hash: &content_hash,
            label,
            original_filename,
            file_name: &file_name,
            thumb_name: &thumb_name,
            includes_bleed,
            bleed_mm,
            width,
            height,
            mpc,
        },
    )
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

/// The database half of the bleed declaration, split out so it can be
/// tested without an AppHandle.
fn set_custom_image_bleed_row(
    conn: &Connection,
    id: i64,
    includes_bleed: bool,
    bleed_mm: f64,
) -> Result<(), String> {
    let bleed_mm = validate_bleed_mm(bleed_mm)?;
    conn.execute(
        "UPDATE custom_images SET includes_bleed = ?1, bleed_mm = ?2 WHERE id = ?3",
        params![includes_bleed, bleed_mm, id],
    )
    .map_err(|e| e.to_string())?;
    Ok(())
}

/// Record the user's declaration about their own file. Takes effect on
/// the server at the next sync (see sync_custom_image), which re-uploads
/// under the new amount and discards the upscales made under the old one.
#[tauri::command]
pub fn set_custom_image_bleed(
    app: AppHandle,
    id: i64,
    includes_bleed: bool,
    bleed_mm: f64,
) -> Result<(), String> {
    let conn = open_db(&app)?;
    set_custom_image_bleed_row(&conn, id, includes_bleed, bleed_mm)
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
    // A Scryfall card printing this image on a face keeps its card: that
    // face just goes back to its own art.
    crate::face_art::strip_custom_image_references(conn, id)?;
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

/// The original file as a data URL, for the full-image viewer. Unlike the
/// thumbnail this is the whole upload — up to MAX_BYTES, base64-encoded —
/// so it crosses IPC only when the viewer opens for one image, never for
/// the grid. Still the cheapest honest option: an asset protocol would
/// need a CSP and scope change for one dialog, and decoding to a smaller
/// render would need the image crate this build deliberately avoids.
#[tauri::command]
pub fn custom_image_full(app: AppHandle, id: i64) -> Result<Option<String>, String> {
    let conn = open_db(&app)?;
    let file_name: Option<String> = conn
        .query_row(
            "SELECT file_name FROM custom_images WHERE id = ?1",
            params![id],
            |row| row.get(0),
        )
        .optional()
        .map_err(|e| e.to_string())?;
    let Some(file_name) = file_name else {
        return Ok(None);
    };
    Ok(crate::back_images::image_data_url(
        &customs_dir(&app)?.join(&file_name),
        &file_name,
    ))
}

#[derive(Debug, Clone, Serialize)]
pub struct CustomSyncResult {
    pub content_hash: String,
    /// Whether these bytes had to be sent, as opposed to the server
    /// already holding them. Purely informational — the client calls sync
    /// unconditionally and this says what it cost.
    pub uploaded: bool,
    /// Whether the server downloaded the bytes from Google Drive itself
    /// (an MPC Fill image) instead.
    pub fetched: bool,
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
    let (content_hash, file_name, includes_bleed, bleed_mm, drive_id) = {
        let conn = open_db(&app)?;
        conn.query_row(
            "SELECT content_hash, file_name, includes_bleed, bleed_mm, mpc_drive_id
             FROM custom_images WHERE id = ?1",
            params![id],
            |row| {
                Ok((
                    row.get::<_, String>(0)?,
                    row.get::<_, String>(1)?,
                    row.get::<_, bool>(2)?,
                    row.get::<_, f64>(3)?,
                    row.get::<_, Option<String>>(4)?,
                ))
            },
        )
        .optional()
        .map_err(|e| e.to_string())?
        .ok_or_else(|| "That custom image is no longer in the library.".to_string())?
    };
    let declared = effective_bleed_mm(includes_bleed, bleed_mm);
    let base = server_base_url.trim_end_matches('/').to_string();
    let url = format!("{base}/api/customs/{content_hash}");

    let client = reqwest::Client::new();
    let mut answer: Option<(bool, bool)> = None;
    if let Ok(resp) = client.get(&url).send().await {
        if resp.status().is_success() {
            // reqwest is built without its "json" feature here (see
            // Cargo.toml) — same as update.rs, the body is read as bytes
            // and handed to serde_json directly.
            let status = resp
                .bytes()
                .await
                .ok()
                .and_then(|body| serde_json::from_slice::<serde_json::Value>(&body).ok());
            let present = status
                .as_ref()
                .and_then(|v| v.get("present").and_then(|p| p.as_bool()))
                .unwrap_or(false);
            let server_bleed = status
                .as_ref()
                .and_then(|v| v.get("bleed_mm").and_then(|b| b.as_f64()));
            // A server that does not report bleed_mm predates the
            // declaration and would silently crop a bled file to card
            // aspect; say so once rather than re-uploading every sync.
            if declared > 0.0 && server_bleed.is_none() {
                return Err(
                    "This generation server is too old to accept custom images that \
                     include bleed. Update the server, or untick \"includes bleed\" \
                     for this image."
                        .to_string(),
                );
            }
            // Present AND stored under the same declared bleed: nothing to
            // send. A different amount means the server's copy was cropped
            // to the wrong box, so it is replaced below.
            answer = Some((present, (server_bleed.unwrap_or(0.0) - declared).abs() < 1e-6));
        }
    }

    match mpc_art::sync_step(answer, drive_id.is_some()) {
        SyncStep::Done => {
            return Ok(CustomSyncResult { content_hash, uploaded: false, fetched: false });
        }
        SyncStep::FetchThenUpload => {
            // An MPC Fill image: the server can download it from Google
            // itself rather than this machine uploading it.
            let body = serde_json::json!({
                "drive_id": drive_id.as_deref().unwrap_or_default(),
                "bleed_mm": declared,
            });
            if mpc_art::server_fetch(&client, &url, &body).await {
                return Ok(CustomSyncResult { content_hash, uploaded: false, fetched: true });
            }
        }
        SyncStep::Upload => {}
    }

    let path = customs_dir(&app)?.join(file_name);
    let bytes =
        std::fs::read(&path).map_err(|e| format!("failed to read custom image: {e}"))?;
    let post_url = if declared > 0.0 {
        format!("{url}?bleed_mm={declared}")
    } else {
        url.clone()
    };
    let resp = client
        .post(&post_url)
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
    Ok(CustomSyncResult { content_hash, uploaded: true, fetched: false })
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
        assert!((dpi_at_card_size(745, 1040, 0.0) - 300.0).abs() < 1.0);
        // Orientation-independent: the longer edge is what's compared.
        assert_eq!(dpi_at_card_size(1040, 745, 0.0), dpi_at_card_size(745, 1040, 0.0));
        // An MPC file (3.175 mm of bleed) spans 94.35 mm on its long edge:
        // 1114 px of it is ~300 DPI, not ~322.
        assert!((dpi_at_card_size(818, 1114, 3.175) - 300.0).abs() < 1.0);
        assert_eq!(effective_bleed_mm(false, 3.175), 0.0);
        assert_eq!(effective_bleed_mm(true, 3.175), 3.175);
    }

    fn insert_custom(conn: &Connection, hash: &str) -> i64 {
        conn.execute(
            "INSERT INTO custom_images
                (content_hash, label, original_filename, file_name, thumb_name,
                 width, height, created_at)
             VALUES (?1, 'A card', 'a.png', ?2, ?3, 818, 1114, '1')",
            params![hash, format!("{hash}.png"), format!("{hash}_thumb.jpg")],
        )
        .expect("insert custom");
        conn.last_insert_rowid()
    }

    #[test]
    fn mpc_rows_carry_provenance_and_bleed() {
        use crate::project_store::test_support::test_conn;
        let conn = test_conn();
        let mpc = MpcProvenance::new("1taR38keSBJgplwmZkX3NETXQSblhPYSh", "Chilli_Axe", "Bolt (Full Art)")
            .expect("valid id");
        let row = insert_custom_image_row(
            &conn,
            &NewCustomImageRow {
                content_hash: "m1",
                label: "Bolt (Full Art)".to_string(),
                original_filename: "Bolt (Full Art).png",
                file_name: "m1.png",
                thumb_name: "m1_thumb.jpg",
                includes_bleed: true,
                bleed_mm: 3.175,
                width: 816,
                height: 1110,
                mpc: Some(&mpc),
            },
        )
        .expect("insert");
        assert_eq!(row.mpc.as_ref(), Some(&mpc));
        assert!(row.includes_bleed);
        assert_eq!(row.label, "Bolt (Full Art)");
        assert_eq!(find_by_hash(&conn, "m1").expect("q").expect("row").id, row.id);

        // An upload made by hand has no provenance.
        let plain = insert_custom(&conn, "c2");
        assert!(find_by_id(&conn, plain).expect("q").expect("row").mpc.is_none());
    }

    #[test]
    fn provenance_is_attached_once_and_never_touches_the_users_declarations() {
        use crate::project_store::test_support::test_conn;
        let conn = test_conn();
        let id = insert_custom(&conn, "c3");
        set_custom_image_bleed_row(&conn, id, true, 2.0).expect("bleed");
        let first = MpcProvenance::new("1taR38keSBJgplwmZkX3NETXQSblhPYSh", "A", "One").unwrap();
        let second = MpcProvenance::new("1MsVtWxw2bJO9xrduU0fUBraSVuvBW0zq", "B", "Two").unwrap();
        mpc_art::attach_provenance_row(&conn, "custom_images", id, &first).expect("attach");
        mpc_art::attach_provenance_row(&conn, "custom_images", id, &second).expect("attach");
        let row = find_by_id(&conn, id).expect("q").expect("row");
        assert_eq!(row.mpc, Some(first));
        assert_eq!(row.label, "A card");
        assert!(row.includes_bleed);
        assert!((row.bleed_mm - 2.0).abs() < 1e-9);
    }

    #[test]
    fn bleed_declaration_round_trips_and_is_validated() {
        use crate::project_store::test_support::test_conn;
        let conn = test_conn();
        let id = insert_custom(&conn, "c1");
        // Fresh rows: unticked, MPC amount ready for when the box is ticked.
        let row = find_by_id(&conn, id).expect("query").expect("row");
        assert!(!row.includes_bleed);
        assert!((row.bleed_mm - 3.175).abs() < 1e-9);
        assert!((row.source_dpi - dpi_at_card_size(818, 1114, 0.0)).abs() < 1e-9);

        set_custom_image_bleed_row(&conn, id, true, 3.175).expect("set");
        let row = find_by_id(&conn, id).expect("query").expect("row");
        assert!(row.includes_bleed);
        // The DPI hint follows the declaration: the same pixels now span
        // the bled box, so they are worth fewer DPI at card size.
        assert!((row.source_dpi - 300.0).abs() < 1.0);

        // Unticking keeps the typed amount for later.
        set_custom_image_bleed_row(&conn, id, false, 2.0).expect("set");
        let row = find_by_id(&conn, id).expect("query").expect("row");
        assert!(!row.includes_bleed);
        assert!((row.bleed_mm - 2.0).abs() < 1e-9);

        assert!(set_custom_image_bleed_row(&conn, id, true, -1.0).is_err());
        assert!(set_custom_image_bleed_row(&conn, id, true, 11.0).is_err());
        assert!(set_custom_image_bleed_row(&conn, id, true, f64::NAN).is_err());
    }
}
