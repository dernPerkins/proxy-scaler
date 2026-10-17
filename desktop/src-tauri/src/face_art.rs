// Face art: a Custom Image printed in place of one face of a Scryfall card.
//
// A card picked from MPC Fill in the change-printing modal keeps its
// Scryfall identity — its printing, its name, its DFC pairing, its own
// back — and only the pixels of that face change. So the override lives on
// the project card, not as a different kind of card: `project_cards.
// face_art` holds, per face, the Custom Image library row to print
// instead. The generation server is told through DeckEntryIn.
// face_overrides (desktop/frontend/src/deckEntries.ts), which skips the
// Scryfall download/upscale for that face and prints the custom.
//
// Stored as opaque JSON so the column never needs migrating per face:
//
//   {"0": {"custom_image_id": 12, "total_faces": 2}}
//
// `total_faces` is the printing's face count when the art was picked. The
// server needs it (a single-faced card has no face index; a DFC front is
// face 0) and has no card corpus at print time to look it up. It can't go
// stale: changing the printing clears the art (set_card_printing).
use std::collections::{BTreeMap, HashMap};

use rusqlite::{params, Connection, OptionalExtension};
use serde::{Deserialize, Serialize};
use tauri::AppHandle;

use crate::project_store::open_db;

#[derive(Debug, Clone, Copy, PartialEq, Serialize, Deserialize)]
struct FaceArtEntry {
    custom_image_id: i64,
    total_faces: u8,
}

type FaceArtMap = BTreeMap<String, FaceArtEntry>;

/// One overridden face, as the frontend sees it: the library row plus the
/// bits of it every render needs (the hash is the server's identity).
#[derive(Debug, Clone, PartialEq, Serialize)]
pub struct CardFaceArt {
    pub face: u8,
    pub total_faces: u8,
    pub custom_image_id: i64,
    pub custom_hash: String,
    pub label: String,
    pub mpc_drive_id: Option<String>,
}

fn parse(raw: Option<&str>) -> FaceArtMap {
    raw.and_then(|s| serde_json::from_str(s).ok()).unwrap_or_default()
}

fn serialize(map: &FaceArtMap) -> Option<String> {
    if map.is_empty() {
        None
    } else {
        serde_json::to_string(map).ok()
    }
}

/// A card's stored face art, joined with the library. Entries whose image
/// is gone are dropped (delete_custom_image strips them; this is the belt
/// to that's braces). `cache` saves re-reading an image shared by cards.
/// (content_hash, label, mpc_drive_id) of a library image, or None when
/// the row is gone.
type ImageInfo = Option<(String, String, Option<String>)>;

pub(crate) fn load_card_face_art(
    conn: &Connection,
    raw: Option<&str>,
    cache: &mut HashMap<i64, ImageInfo>,
) -> Result<Vec<CardFaceArt>, String> {
    let mut out = Vec::new();
    for (face, entry) in parse(raw) {
        let Ok(face) = face.parse::<u8>() else { continue };
        if let std::collections::hash_map::Entry::Vacant(slot) = cache.entry(entry.custom_image_id) {
            let row = conn
                .query_row(
                    "SELECT content_hash, label, mpc_drive_id FROM custom_images WHERE id = ?1",
                    params![entry.custom_image_id],
                    |row| Ok((row.get(0)?, row.get(1)?, row.get(2)?)),
                )
                .optional()
                .map_err(|e| e.to_string())?;
            slot.insert(row);
        }
        if let Some(Some((custom_hash, label, mpc_drive_id))) = cache.get(&entry.custom_image_id) {
            out.push(CardFaceArt {
                face,
                total_faces: entry.total_faces,
                custom_image_id: entry.custom_image_id,
                custom_hash: custom_hash.clone(),
                label: label.clone(),
                mpc_drive_id: mpc_drive_id.clone(),
            });
        }
    }
    Ok(out)
}

fn read_card(conn: &Connection, card_id: i64) -> Result<(Option<String>, Option<i64>, FaceArtMap), String> {
    conn.query_row(
        "SELECT scryfall_id, custom_image_id, face_art FROM project_cards WHERE id = ?1",
        params![card_id],
        |row| {
            Ok((
                row.get::<_, Option<String>>(0)?,
                row.get::<_, Option<i64>>(1)?,
                row.get::<_, Option<String>>(2)?,
            ))
        },
    )
    .optional()
    .map_err(|e| e.to_string())?
    .map(|(sid, custom, raw)| (sid, custom, parse(raw.as_deref())))
    .ok_or_else(|| format!("card {card_id} not found"))
}

fn write_card(conn: &Connection, card_id: i64, map: &FaceArtMap) -> Result<(), String> {
    conn.execute(
        "UPDATE project_cards SET face_art = ?1 WHERE id = ?2",
        params![serialize(map), card_id],
    )
    .map_err(|e| e.to_string())?;
    Ok(())
}

/// Print `custom_image_id` in place of face `face` of a Scryfall card
/// whose printing has `total_faces` faces. A different face count than the
/// card's other entries were picked under means the printing changed
/// underneath them, so those are dropped.
pub(crate) fn set_card_face_art_row(
    conn: &Connection,
    card_id: i64,
    face: u8,
    total_faces: u8,
    custom_image_id: i64,
) -> Result<(), String> {
    if !(1..=2).contains(&total_faces) || face >= total_faces {
        return Err(format!("face {face} of a {total_faces}-faced card doesn't exist"));
    }
    let (scryfall_id, custom, mut map) = read_card(conn, card_id)?;
    if scryfall_id.is_none() || custom.is_some() {
        return Err("Only a Scryfall card can take MPC art for a face.".to_string());
    }
    let exists: bool = conn
        .query_row(
            "SELECT EXISTS(SELECT 1 FROM custom_images WHERE id = ?1)",
            params![custom_image_id],
            |row| row.get(0),
        )
        .map_err(|e| e.to_string())?;
    if !exists {
        return Err("That image is no longer in the library.".to_string());
    }
    map.retain(|_, entry| entry.total_faces == total_faces);
    map.insert(face.to_string(), FaceArtEntry { custom_image_id, total_faces });
    write_card(conn, card_id, &map)
}

/// Back to Scryfall art for one face, or for every face with None.
pub(crate) fn clear_card_face_art_row(
    conn: &Connection,
    card_id: i64,
    face: Option<u8>,
) -> Result<(), String> {
    let (_, _, mut map) = read_card(conn, card_id)?;
    match face {
        Some(face) => {
            map.remove(&face.to_string());
        }
        None => map.clear(),
    }
    write_card(conn, card_id, &map)
}

/// Drop every reference to a library image that is being deleted. Those
/// faces go back to their Scryfall art rather than the card going away:
/// unlike a Custom Image card, the card still has a printing.
pub(crate) fn strip_custom_image_references(
    conn: &Connection,
    custom_image_id: i64,
) -> Result<usize, String> {
    let rows: Vec<(i64, String)> = {
        let mut stmt = conn
            .prepare("SELECT id, face_art FROM project_cards WHERE face_art IS NOT NULL")
            .map_err(|e| e.to_string())?;
        let mapped = stmt
            .query_map([], |row| Ok((row.get(0)?, row.get(1)?)))
            .map_err(|e| e.to_string())?;
        mapped.collect::<Result<_, _>>().map_err(|e| e.to_string())?
    };
    let mut touched = 0;
    for (card_id, raw) in rows {
        let mut map = parse(Some(&raw));
        let before = map.len();
        map.retain(|_, entry| entry.custom_image_id != custom_image_id);
        if map.len() != before {
            write_card(conn, card_id, &map)?;
            touched += 1;
        }
    }
    Ok(touched)
}

/// How many project cards (in any project) print this image on a face.
pub(crate) fn count_face_art_using_row(conn: &Connection, custom_image_id: i64) -> Result<i64, String> {
    let mut stmt = conn
        .prepare("SELECT face_art FROM project_cards WHERE face_art IS NOT NULL")
        .map_err(|e| e.to_string())?;
    let raws = stmt
        .query_map([], |row| row.get::<_, String>(0))
        .map_err(|e| e.to_string())?;
    let mut count = 0;
    for raw in raws {
        let raw = raw.map_err(|e| e.to_string())?;
        if parse(Some(&raw)).values().any(|e| e.custom_image_id == custom_image_id) {
            count += 1;
        }
    }
    Ok(count)
}

#[tauri::command]
pub fn set_card_face_art(
    app: AppHandle,
    card_id: i64,
    face: u8,
    total_faces: u8,
    custom_image_id: i64,
) -> Result<(), String> {
    let conn = open_db(&app)?;
    set_card_face_art_row(&conn, card_id, face, total_faces, custom_image_id)
}

#[tauri::command]
pub fn clear_card_face_art(app: AppHandle, card_id: i64, face: Option<u8>) -> Result<(), String> {
    let conn = open_db(&app)?;
    clear_card_face_art_row(&conn, card_id, face)
}

#[tauri::command]
pub fn count_face_art_using_custom_image(app: AppHandle, id: i64) -> Result<i64, String> {
    let conn = open_db(&app)?;
    count_face_art_using_row(&conn, id)
}

#[cfg(test)]
mod tests {
    use super::*;
    use crate::project_store::test_support::test_conn;

    fn custom(conn: &Connection, hash: &str, label: &str) -> i64 {
        conn.execute(
            "INSERT INTO custom_images (content_hash, label, original_filename, file_name,
                                        thumb_name, width, height, created_at, mpc_drive_id)
             VALUES (?1, ?2, 'a.png', 'a.png', 'a_thumb.jpg', 816, 1110, '1', 'drive')",
            params![hash, label],
        )
        .expect("custom");
        conn.last_insert_rowid()
    }

    fn card(conn: &Connection, scryfall_id: Option<&str>, custom_image_id: Option<i64>) -> i64 {
        conn.execute(
            "INSERT INTO projects (tag, name, import_decklist_text, created_at, updated_at)
             VALUES (lower(hex(randomblob(16))), lower(hex(randomblob(8))), '', '1', '1')",
            [],
        )
        .expect("project");
        let project_id = conn.last_insert_rowid();
        conn.execute(
            "INSERT INTO project_cards (project_id, sort_order, original_import_line, quantity,
                                        name, scryfall_id, custom_image_id)
             VALUES (?1, 0, 'x', 1, 'Delver', ?2, ?3)",
            params![project_id, scryfall_id, custom_image_id],
        )
        .expect("card");
        conn.last_insert_rowid()
    }

    fn stored(conn: &Connection, card_id: i64) -> Vec<CardFaceArt> {
        let raw: Option<String> = conn
            .query_row("SELECT face_art FROM project_cards WHERE id = ?1", params![card_id], |r| {
                r.get(0)
            })
            .expect("row");
        load_card_face_art(conn, raw.as_deref(), &mut HashMap::new()).expect("load")
    }

    #[test]
    fn set_and_clear_round_trip() {
        let conn = test_conn();
        let art = custom(&conn, "h1", "Delver (MPC)");
        let id = card(&conn, Some("sid"), None);
        set_card_face_art_row(&conn, id, 1, 2, art).expect("set");
        let faces = stored(&conn, id);
        assert_eq!(faces.len(), 1);
        assert_eq!(faces[0].face, 1);
        assert_eq!(faces[0].total_faces, 2);
        assert_eq!(faces[0].custom_hash, "h1");
        assert_eq!(faces[0].mpc_drive_id.as_deref(), Some("drive"));

        set_card_face_art_row(&conn, id, 0, 2, art).expect("front too");
        assert_eq!(stored(&conn, id).len(), 2);
        clear_card_face_art_row(&conn, id, Some(1)).expect("clear one");
        assert_eq!(stored(&conn, id).iter().map(|f| f.face).collect::<Vec<_>>(), vec![0]);
        clear_card_face_art_row(&conn, id, None).expect("clear all");
        let raw: Option<String> = conn
            .query_row("SELECT face_art FROM project_cards WHERE id = ?1", params![id], |r| r.get(0))
            .expect("row");
        assert!(raw.is_none());
    }

    #[test]
    fn a_different_face_count_drops_the_old_entries() {
        let conn = test_conn();
        let art = custom(&conn, "h1", "A");
        let id = card(&conn, Some("sid"), None);
        set_card_face_art_row(&conn, id, 1, 2, art).expect("dfc back");
        set_card_face_art_row(&conn, id, 0, 1, art).expect("single");
        let faces = stored(&conn, id);
        assert_eq!(faces.len(), 1);
        assert_eq!((faces[0].face, faces[0].total_faces), (0, 1));
    }

    #[test]
    fn validation() {
        let conn = test_conn();
        let art = custom(&conn, "h1", "A");
        let scryfall = card(&conn, Some("sid"), None);
        let custom_card = card(&conn, None, Some(art));
        assert!(set_card_face_art_row(&conn, scryfall, 1, 1, art).is_err());
        assert!(set_card_face_art_row(&conn, scryfall, 0, 3, art).is_err());
        assert!(set_card_face_art_row(&conn, scryfall, 0, 1, 9999).is_err());
        assert!(set_card_face_art_row(&conn, custom_card, 0, 1, art).is_err());
        assert!(set_card_face_art_row(&conn, 424242, 0, 1, art).is_err());
    }

    #[test]
    fn deleting_the_image_strips_it_and_counts_its_users() {
        let conn = test_conn();
        let a = custom(&conn, "h1", "A");
        let b = custom(&conn, "h2", "B");
        let one = card(&conn, Some("s1"), None);
        let two = card(&conn, Some("s2"), None);
        set_card_face_art_row(&conn, one, 0, 2, a).expect("set");
        set_card_face_art_row(&conn, one, 1, 2, b).expect("set");
        set_card_face_art_row(&conn, two, 0, 1, a).expect("set");
        assert_eq!(count_face_art_using_row(&conn, a).expect("count"), 2);

        assert_eq!(strip_custom_image_references(&conn, a).expect("strip"), 2);
        assert_eq!(stored(&conn, one).iter().map(|f| f.face).collect::<Vec<_>>(), vec![1]);
        assert!(stored(&conn, two).is_empty());
        assert_eq!(count_face_art_using_row(&conn, a).expect("count"), 0);
    }
}
