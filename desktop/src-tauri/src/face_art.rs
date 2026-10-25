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
//   {"0": {"custom_image_id": 12, "total_faces": 2},
//    "1": {"total_faces": 2, "mpc": {"drive_id": "…", "source": "…", "name": "…"}}}
//
// The second shape is MPC Fill art chosen but not downloaded yet: an
// import with "Art from: MPC Fill" records MPC Fill's top match for each
// face without fetching anything, like a Scryfall import records printings
// without downloading them. Download images (or Generate, or picking it in
// the modal) fetches it into the library, and the entry gains its
// custom_image_id. Until then the face prints nothing and reports missing:
// never silently its Scryfall art.
//
// A third shape points a face at another Scryfall printing's face:
//
//   {"1": {"total_faces": 2, "scryfall": {"scryfall_id": "…", "source_face": 1, …}}}
//
// The front always prints the card's own printing; the back follows it
// unless overridden, by MPC art or by "printing Y's face 1". An entry
// holds exactly one of custom_image_id/mpc or scryfall, and an override
// that names the card's own printing and the same face is never stored
// (that's just "follow the card", so it is removed instead).
//
// `total_faces` is the printing's face count when the art was picked. The
// server needs it (a single-faced card has no face index; a DFC front is
// face 0) and has no card corpus at print time to look it up. Changing
// the printing drops the front's entry and keeps the back's
// (retarget_face_art_for_printing).
use std::collections::{BTreeMap, HashMap};

use rusqlite::{params, Connection, OptionalExtension};
use serde::{Deserialize, Serialize};
use tauri::AppHandle;

use crate::mpc_art::MpcProvenance;
use crate::project_store::open_db;

#[derive(Debug, Clone, PartialEq, Serialize, Deserialize)]
struct FaceArtEntry {
    /// None while the art is chosen but not downloaded yet (see above).
    #[serde(default, skip_serializing_if = "Option::is_none")]
    custom_image_id: Option<i64>,
    total_faces: u8,
    /// The MPC Fill art a not-yet-downloaded entry stands for.
    #[serde(default, skip_serializing_if = "Option::is_none")]
    mpc: Option<MpcProvenance>,
    /// Another printing's face printed here instead. Exclusive with the
    /// two fields above.
    #[serde(default, skip_serializing_if = "Option::is_none")]
    scryfall: Option<ScryfallFaceSource>,
}

/// One face of a Scryfall printing, with the display cache a card row
/// keeps for its own printing (name, set/collector, lang, …) so the face
/// renders without a lookup.
#[derive(Debug, Clone, PartialEq, Serialize, Deserialize)]
pub struct ScryfallFaceSource {
    pub scryfall_id: String,
    /// Which face of that printing: 0 for a single-faced printing.
    pub source_face: u8,
    pub name: String,
    pub set_code: String,
    pub collector_number: String,
    pub lang: String,
    #[serde(default)]
    pub printed_name: Option<String>,
    #[serde(default)]
    pub flavor_name: Option<String>,
    #[serde(default)]
    pub rarity: Option<String>,
}

impl FaceArtEntry {
    fn scryfall_points_at(&self, scryfall_id: &str, face: u8) -> bool {
        self.scryfall.as_ref().is_some_and(|s| s.scryfall_id == scryfall_id && s.source_face == face)
    }
}

type FaceArtMap = BTreeMap<String, FaceArtEntry>;

/// One overridden face, as the frontend sees it: the library row plus the
/// bits of it every render needs (the hash is the server's identity). The
/// image fields are None for MPC Fill art not downloaded yet; the mpc_*
/// fields then say what to download. A face that prints another Scryfall
/// printing has only `scryfall` (and its name as the label).
#[derive(Debug, Clone, PartialEq, Serialize)]
pub struct CardFaceArt {
    pub face: u8,
    pub total_faces: u8,
    pub custom_image_id: Option<i64>,
    pub custom_hash: Option<String>,
    pub label: String,
    pub mpc_drive_id: Option<String>,
    pub mpc_source: Option<String>,
    pub scryfall: Option<ScryfallFaceSource>,
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
/// (content_hash, label, mpc_drive_id, mpc_source) of a library image, or
/// None when the row is gone.
type ImageInfo = Option<(String, String, Option<String>, Option<String>)>;

pub(crate) fn load_card_face_art(
    conn: &Connection,
    raw: Option<&str>,
    cache: &mut HashMap<i64, ImageInfo>,
) -> Result<Vec<CardFaceArt>, String> {
    let mut out = Vec::new();
    for (face, entry) in parse(raw) {
        let Ok(face) = face.parse::<u8>() else { continue };
        if let Some(scryfall) = entry.scryfall {
            out.push(CardFaceArt {
                face,
                total_faces: entry.total_faces,
                custom_image_id: None,
                custom_hash: None,
                label: scryfall.name.clone(),
                mpc_drive_id: None,
                mpc_source: None,
                scryfall: Some(scryfall),
            });
            continue;
        }
        let Some(custom_image_id) = entry.custom_image_id else {
            // Chosen, not downloaded yet.
            if let Some(mpc) = entry.mpc {
                out.push(CardFaceArt {
                    face,
                    total_faces: entry.total_faces,
                    custom_image_id: None,
                    custom_hash: None,
                    label: mpc.name.clone(),
                    mpc_drive_id: Some(mpc.drive_id),
                    mpc_source: Some(mpc.source),
                    scryfall: None,
                });
            }
            continue;
        };
        if let std::collections::hash_map::Entry::Vacant(slot) = cache.entry(custom_image_id) {
            let row = conn
                .query_row(
                    "SELECT content_hash, label, mpc_drive_id, mpc_source
                     FROM custom_images WHERE id = ?1",
                    params![custom_image_id],
                    |row| Ok((row.get(0)?, row.get(1)?, row.get(2)?, row.get(3)?)),
                )
                .optional()
                .map_err(|e| e.to_string())?;
            slot.insert(row);
        }
        if let Some(Some((custom_hash, label, mpc_drive_id, mpc_source))) = cache.get(&custom_image_id) {
            out.push(CardFaceArt {
                face,
                total_faces: entry.total_faces,
                custom_image_id: Some(custom_image_id),
                custom_hash: Some(custom_hash.clone()),
                label: label.clone(),
                mpc_drive_id: mpc_drive_id.clone(),
                mpc_source: mpc_source.clone(),
                scryfall: None,
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
    let (_, mut map) = scryfall_card_faces(conn, card_id, face, total_faces)?;
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
    map.insert(
        face.to_string(),
        FaceArtEntry { custom_image_id: Some(custom_image_id), total_faces, mpc: None, scryfall: None },
    );
    write_card(conn, card_id, &map)
}

/// A Scryfall card's own scryfall_id and face map, after checking the
/// face exists on a `total_faces`-faced printing. Entries picked under
/// another face count belong to a different printing and are dropped.
fn scryfall_card_faces(
    conn: &Connection,
    card_id: i64,
    face: u8,
    total_faces: u8,
) -> Result<(String, FaceArtMap), String> {
    if !(1..=2).contains(&total_faces) || face >= total_faces {
        return Err(format!("face {face} of a {total_faces}-faced card doesn't exist"));
    }
    let (scryfall_id, custom, mut map) = read_card(conn, card_id)?;
    let Some(scryfall_id) = scryfall_id.filter(|_| custom.is_none()) else {
        return Err("Only a Scryfall card can take its own art for a face.".to_string());
    };
    map.retain(|_, entry| entry.total_faces == total_faces);
    Ok((scryfall_id, map))
}

/// Choose MPC Fill art for a face without downloading it (an import with
/// "Art from: MPC Fill"). Download images, Generate or a pick fetches it,
/// and set_card_face_art then points the entry at the library image.
pub(crate) fn set_card_face_art_pending_row(
    conn: &Connection,
    card_id: i64,
    face: u8,
    total_faces: u8,
    mpc: MpcProvenance,
) -> Result<(), String> {
    let (_, mut map) = scryfall_card_faces(conn, card_id, face, total_faces)?;
    map.insert(
        face.to_string(),
        FaceArtEntry { custom_image_id: None, total_faces, mpc: Some(mpc), scryfall: None },
    );
    write_card(conn, card_id, &map)
}

/// Print face `source.source_face` of printing `source.scryfall_id` in
/// place of face `face`. Naming the card's own printing and the same face
/// removes the override instead (the face follows the card again). The
/// front always prints the card's own printing, so any other source for
/// face 0 is refused: that's a change of the card's printing.
pub(crate) fn set_card_face_printing_row(
    conn: &Connection,
    card_id: i64,
    face: u8,
    total_faces: u8,
    source: ScryfallFaceSource,
) -> Result<(), String> {
    let (own_id, mut map) = scryfall_card_faces(conn, card_id, face, total_faces)?;
    if source.scryfall_id.trim().is_empty() {
        return Err("A face printing needs a scryfall_id.".to_string());
    }
    if source.source_face > 1 {
        return Err(format!("face {} of a printing doesn't exist", source.source_face));
    }
    if source.scryfall_id == own_id && source.source_face == face {
        map.remove(&face.to_string());
    } else if face == 0 {
        return Err("The front prints the card's own printing; change the card's printing instead.".to_string());
    } else {
        map.insert(
            face.to_string(),
            FaceArtEntry { custom_image_id: None, total_faces, mpc: None, scryfall: Some(source) },
        );
    }
    write_card(conn, card_id, &map)
}

/// Keep a card's face art in step with a change of its printing to
/// `new_scryfall_id`. The front's art belonged to the old printing and is
/// dropped; the back's override (MPC art or another printing's face) is
/// the user's choice for the back and survives, unless it now just names
/// the new printing's own back. A no-op when the printing didn't change.
pub(crate) fn retarget_face_art_for_printing(
    conn: &Connection,
    card_id: i64,
    new_scryfall_id: &str,
) -> Result<(), String> {
    let (old_id, _, mut map) = read_card(conn, card_id)?;
    if old_id.as_deref() == Some(new_scryfall_id) {
        return Ok(());
    }
    map.remove("0");
    map.retain(|face, entry| face != "1" || !entry.scryfall_points_at(new_scryfall_id, 1));
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
        map.retain(|_, entry| entry.custom_image_id != Some(custom_image_id));
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
        if parse(Some(&raw)).values().any(|e| e.custom_image_id == Some(custom_image_id)) {
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
pub fn set_card_face_art_pending(
    app: AppHandle,
    card_id: i64,
    face: u8,
    total_faces: u8,
    drive_id: String,
    mpc_source: String,
    mpc_name: String,
) -> Result<(), String> {
    let mpc = MpcProvenance::new(&drive_id, &mpc_source, &mpc_name)?;
    let conn = open_db(&app)?;
    set_card_face_art_pending_row(&conn, card_id, face, total_faces, mpc)
}

#[tauri::command]
pub fn set_card_face_printing(
    app: AppHandle,
    card_id: i64,
    face: u8,
    total_faces: u8,
    source: ScryfallFaceSource,
) -> Result<(), String> {
    let conn = open_db(&app)?;
    set_card_face_printing_row(&conn, card_id, face, total_faces, source)
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
        assert_eq!(faces[0].custom_hash.as_deref(), Some("h1"));
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
    fn mpc_art_can_be_chosen_before_it_is_downloaded() {
        let conn = test_conn();
        let id = card(&conn, Some("sid"), None);
        let mpc = MpcProvenance::new("1taR38keSBJgplwmZkX3NETXQSblhPYSh", "Chilli_Axe", "Delver (MPC)")
            .expect("id");
        set_card_face_art_pending_row(&conn, id, 1, 2, mpc).expect("choose");
        let faces = stored(&conn, id);
        assert_eq!(faces.len(), 1);
        assert_eq!(faces[0].custom_image_id, None);
        assert_eq!(faces[0].custom_hash, None);
        assert_eq!(faces[0].label, "Delver (MPC)");
        assert_eq!(faces[0].mpc_drive_id.as_deref(), Some("1taR38keSBJgplwmZkX3NETXQSblhPYSh"));
        assert_eq!(faces[0].mpc_source.as_deref(), Some("Chilli_Axe"));

        // Downloaded: the entry now points at the library image.
        let art = custom(&conn, "h9", "Delver (MPC)");
        set_card_face_art_row(&conn, id, 1, 2, art).expect("downloaded");
        let faces = stored(&conn, id);
        assert_eq!(faces[0].custom_image_id, Some(art));
        assert_eq!(faces[0].custom_hash.as_deref(), Some("h9"));
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

    fn source(scryfall_id: &str, source_face: u8) -> ScryfallFaceSource {
        ScryfallFaceSource {
            scryfall_id: scryfall_id.to_string(),
            source_face,
            name: "Insectile Aberration".to_string(),
            set_code: "isd".to_string(),
            collector_number: "51".to_string(),
            lang: "en".to_string(),
            printed_name: None,
            flavor_name: Some("Bug".to_string()),
            rarity: Some("common".to_string()),
        }
    }

    #[test]
    fn a_face_can_print_another_printing() {
        let conn = test_conn();
        let id = card(&conn, Some("sid"), None);
        set_card_face_printing_row(&conn, id, 1, 2, source("other", 1)).expect("set");
        let faces = stored(&conn, id);
        assert_eq!(faces.len(), 1);
        assert_eq!((faces[0].face, faces[0].total_faces), (1, 2));
        assert_eq!(faces[0].label, "Insectile Aberration");
        assert_eq!(faces[0].custom_image_id, None);
        assert_eq!(faces[0].mpc_drive_id, None);
        assert_eq!(faces[0].scryfall, Some(source("other", 1)));

        // MPC art on that face replaces it, and a printing replaces MPC art.
        let art = custom(&conn, "h1", "A");
        set_card_face_art_row(&conn, id, 1, 2, art).expect("mpc");
        let faces = stored(&conn, id);
        assert_eq!((faces[0].custom_image_id, faces[0].scryfall.as_ref()), (Some(art), None));
        set_card_face_printing_row(&conn, id, 1, 2, source("other", 1)).expect("printing again");
        let faces = stored(&conn, id);
        assert_eq!((faces[0].custom_image_id, faces[0].scryfall.is_some()), (None, true));
        assert_eq!(count_face_art_using_row(&conn, art).expect("count"), 0);
        assert_eq!(strip_custom_image_references(&conn, art).expect("strip"), 0);

        clear_card_face_art_row(&conn, id, Some(1)).expect("clear");
        assert!(stored(&conn, id).is_empty());
    }

    #[test]
    fn naming_the_cards_own_face_removes_the_override() {
        let conn = test_conn();
        let id = card(&conn, Some("sid"), None);
        set_card_face_printing_row(&conn, id, 1, 2, source("other", 1)).expect("set");
        // A single-faced printing's only face is a fine back too.
        set_card_face_printing_row(&conn, id, 1, 2, source("sid", 0)).expect("own front");
        assert_eq!(stored(&conn, id)[0].scryfall, Some(source("sid", 0)));
        set_card_face_printing_row(&conn, id, 1, 2, source("sid", 1)).expect("own back");
        assert!(stored(&conn, id).is_empty());
        // The front can only follow the card.
        set_card_face_printing_row(&conn, id, 0, 2, source("sid", 0)).expect("own front, no-op");
        assert!(set_card_face_printing_row(&conn, id, 0, 2, source("other", 0)).is_err());
        assert!(set_card_face_printing_row(&conn, id, 1, 2, source("other", 2)).is_err());
        assert!(set_card_face_printing_row(&conn, id, 1, 2, source(" ", 1)).is_err());
        assert!(set_card_face_printing_row(&conn, id, 1, 1, source("other", 1)).is_err());
    }

    #[test]
    fn a_new_printing_keeps_the_back_and_drops_the_front() {
        let conn = test_conn();
        let art = custom(&conn, "h1", "A");
        let id = card(&conn, Some("sid"), None);
        set_card_face_art_row(&conn, id, 0, 2, art).expect("front");
        set_card_face_printing_row(&conn, id, 1, 2, source("other", 1)).expect("back");

        retarget_face_art_for_printing(&conn, id, "sid").expect("same printing");
        assert_eq!(stored(&conn, id).len(), 2, "no change, nothing dropped");

        retarget_face_art_for_printing(&conn, id, "third").expect("new printing");
        let faces = stored(&conn, id);
        assert_eq!(faces.iter().map(|f| f.face).collect::<Vec<_>>(), vec![1]);
        assert_eq!(faces[0].scryfall, Some(source("other", 1)));

        // Moving to the printing the back already names re-links the back.
        retarget_face_art_for_printing(&conn, id, "other").expect("back's own printing");
        assert!(stored(&conn, id).is_empty());
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
