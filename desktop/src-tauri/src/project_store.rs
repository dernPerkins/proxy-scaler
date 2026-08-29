// The local project store: project CRUD, decklist text, and the parsed
// (but unresolved) card list, all in-process via rusqlite against a
// SQLite file in the app's own data dir. No network calls, no separate
// process — see ARCHITECTURE.md for why this lives here instead of on
// the generation server. Scryfall resolution, the download+upscale
// pipeline, and the task queue stay server-side, scoped by this table's
// `tag` column (an opaque string minted once per row — naming a project
// preserves it, deleting the row ends it — and passed to the generation
// server as `project_tag`: plain scoping, not a foreign key).
use std::collections::HashSet;

use rusqlite::{params, Connection, OptionalExtension};
use serde::{Deserialize, Serialize};
use tauri::{AppHandle, Manager};

const DB_FILENAME: &str = "projects.db";

const SCHEMA: &str = "
CREATE TABLE IF NOT EXISTS projects (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    tag TEXT NOT NULL UNIQUE,
    name TEXT NOT NULL UNIQUE,
    import_decklist_text TEXT NOT NULL DEFAULT '',
    model TEXT NOT NULL DEFAULT 'ultrasharp_v2',
    dpi_targets TEXT NOT NULL DEFAULT '1200',
    skip_existing INTEGER NOT NULL DEFAULT 1,
    tile_size INTEGER NOT NULL DEFAULT 0,
    page_width_mm REAL NOT NULL DEFAULT 210.0,
    page_height_mm REAL NOT NULL DEFAULT 297.0,
    cols INTEGER NOT NULL DEFAULT 3,
    rows INTEGER NOT NULL DEFAULT 3,
    bleed_mm REAL NOT NULL DEFAULT 1.0,
    spacing_x_mm REAL NOT NULL DEFAULT 0.0,
    spacing_y_mm REAL NOT NULL DEFAULT 0.0,
    offset_x_mm REAL NOT NULL DEFAULT 0.0,
    offset_y_mm REAL NOT NULL DEFAULT 0.0,
    guide_width_pt REAL NOT NULL DEFAULT 0.75,
    guide_length_mm REAL NOT NULL DEFAULT 2.75,
    export_dpi INTEGER NOT NULL DEFAULT 1200,
    show_cut_lines INTEGER NOT NULL DEFAULT 1,
    preferred_dpi INTEGER,
    preferred_model TEXT,
    preferred_lang TEXT NOT NULL DEFAULT 'en',
    -- The import box's \"All Languages\" checkbox: 1 = best-effort matching
    -- across languages, 0 = strictly the preferred_lang (see the resolve-
    -- gated import flow in ARCHITECTURE.md).
    lang_any INTEGER NOT NULL DEFAULT 0,
    created_at TEXT NOT NULL,
    updated_at TEXT NOT NULL
);
CREATE TABLE IF NOT EXISTS project_cards (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    project_id INTEGER NOT NULL REFERENCES projects(id) ON DELETE CASCADE,
    sort_order INTEGER NOT NULL,
    original_import_line TEXT NOT NULL,
    quantity INTEGER,
    name TEXT NOT NULL,
    set_code TEXT,
    collector_number TEXT,
    -- Authoritative link to one exact Scryfall printing, filled in by the
    -- post-import resolve (or a printing change). name/set_code/
    -- collector_number stay as the offline display cache — the client must
    -- render decklists with no server reachable (see ARCHITECTURE.md).
    scryfall_id TEXT,
    -- Scryfall language code of the chosen printing; set+collector alone
    -- can't encode it (every language of a printing shares them).
    lang TEXT,
    -- Localized name as printed on a non-English printing; NULL for
    -- English. Display-only — `name` (English/oracle) stays the matching
    -- and dedup identity.
    printed_name TEXT
);
CREATE INDEX IF NOT EXISTS idx_project_cards_project_id ON project_cards(project_id);
CREATE TABLE IF NOT EXISTS app_settings (
    key TEXT PRIMARY KEY,
    value TEXT NOT NULL
);
-- The Back Library: user-supplied art printed on a card's Reverse. App-
-- global rather than per-project (a project points at one by id), and
-- client-owned — the generation server only ever holds a content-addressed
-- cache of these plus whatever it has upscaled. See docs/adr/0003.
--
-- The bytes are NOT in here. Files live hash-named under <app_data>/backs/,
-- with a small JPEG thumbnail beside each; only their metadata is a row.
-- A multi-MB BLOB per back would bloat the one database every project load
-- reads, to store something no query ever looks inside.
CREATE TABLE IF NOT EXISTS back_images (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    content_hash TEXT NOT NULL UNIQUE,
    label TEXT NOT NULL,
    original_filename TEXT NOT NULL,
    file_name TEXT NOT NULL,
    thumb_name TEXT NOT NULL,
    -- The user's declaration that their art already carries bleed, so the
    -- renderer fits it to the bled size instead of edge-extending it.
    includes_bleed INTEGER NOT NULL DEFAULT 0,
    width INTEGER NOT NULL DEFAULT 0,
    height INTEGER NOT NULL DEFAULT 0,
    created_at TEXT NOT NULL
);
";

// Columns added to `projects` after its initial release — `CREATE TABLE IF
// NOT EXISTS` (in SCHEMA above) only helps a genuinely fresh database; an
// existing projects.db from before these columns existed needs them added
// explicitly. No formal migration/version system exists in this file (see
// proxy_scaler/db.py for that pattern on the Python side) — this is
// additive-only and idempotent by checking PRAGMA table_info() each time,
// matching how the rest of this file already re-runs its schema
// defensively on every open_db() call.
const PROJECTS_ADDED_COLUMNS: &[(&str, &str)] = &[
    ("page_width_mm", "REAL NOT NULL DEFAULT 210.0"),
    ("page_height_mm", "REAL NOT NULL DEFAULT 297.0"),
    ("cols", "INTEGER NOT NULL DEFAULT 3"),
    ("rows", "INTEGER NOT NULL DEFAULT 3"),
    ("bleed_mm", "REAL NOT NULL DEFAULT 1.0"),
    ("spacing_x_mm", "REAL NOT NULL DEFAULT 0.0"),
    ("spacing_y_mm", "REAL NOT NULL DEFAULT 0.0"),
    ("offset_x_mm", "REAL NOT NULL DEFAULT 0.0"),
    ("offset_y_mm", "REAL NOT NULL DEFAULT 0.0"),
    ("guide_width_pt", "REAL NOT NULL DEFAULT 0.75"),
    ("guide_length_mm", "REAL NOT NULL DEFAULT 2.75"),
    ("export_dpi", "INTEGER NOT NULL DEFAULT 1200"),
    ("show_cut_lines", "INTEGER NOT NULL DEFAULT 1"),
    ("preferred_dpi", "INTEGER"),
    ("preferred_model", "TEXT"),
    ("use_originals", "INTEGER NOT NULL DEFAULT 0"),
    ("preferred_lang", "TEXT NOT NULL DEFAULT 'en'"),
    ("lang_any", "INTEGER NOT NULL DEFAULT 0"),
    // Guides, split from the single `show_cut_lines` boolean into one flag
    // per guide kind per page kind. Stored as HIDE flags to match the
    // checkbox the user ticks — one polarity all the way through to
    // pdf_layout.GuideVisibility, with no `not` in between to invert by
    // accident. `show_cut_lines` itself is left in place but unread:
    // SQLite column drops mean a table rebuild, and this file's migration
    // story is deliberately additive-only. migrate_guide_flags() below
    // carries its value across once.
    ("hide_card_guides_front", "INTEGER NOT NULL DEFAULT 0"),
    ("hide_page_guides_front", "INTEGER NOT NULL DEFAULT 0"),
    // Back Pages default to NO guides — not "preserve the old behaviour",
    // a deliberate difference: you cut a duplex sheet against the guides
    // on its front, so guides on the back are ink you can't use printed on
    // the side of the card that shows.
    ("hide_card_guides_back", "INTEGER NOT NULL DEFAULT 1"),
    ("hide_page_guides_back", "INTEGER NOT NULL DEFAULT 1"),
    // Back printing.
    ("back_printing", "INTEGER NOT NULL DEFAULT 0"),
    ("back_faces_as_reverse", "INTEGER NOT NULL DEFAULT 1"),
    // What fills a Reverse belonging to a card with no transform side:
    // 'back_image' or 'blank'. Blank needs no Back Image at all — the
    // mode for printing a deck purely so its double-faced cards get their
    // own backs.
    ("reverse_fill", "TEXT NOT NULL DEFAULT 'back_image'"),
    ("page_order", "TEXT NOT NULL DEFAULT 'duplex'"),
    ("flip_edge", "TEXT NOT NULL DEFAULT 'long'"),
    // Independent of the front offsets, not added to them: the two are
    // separate calibrations of two physical passes through the printer.
    ("back_offset_x_mm", "REAL NOT NULL DEFAULT 0.0"),
    ("back_offset_y_mm", "REAL NOT NULL DEFAULT 0.0"),
    // This project's Selected Back — a pointer into back_images, nullable
    // (a project may have none). Deliberately not a REAL foreign key:
    // ALTER TABLE ADD COLUMN can't attach one in SQLite, and deleting a
    // back nulls referencing projects explicitly (see delete_back_image)
    // rather than reassigning them to the app default, so a project never
    // quietly starts printing a different back than it did last month.
    ("back_image_id", "INTEGER"),
];

// Same pattern for `project_cards` — its first post-release additions.
const PROJECT_CARDS_ADDED_COLUMNS: &[(&str, &str)] = &[
    ("scryfall_id", "TEXT"),
    ("lang", "TEXT"),
    ("printed_name", "TEXT"),
];

fn add_missing_columns(
    conn: &Connection,
    table: &str,
    columns: &[(&str, &str)],
) -> Result<(), String> {
    let mut stmt = conn
        .prepare(&format!("PRAGMA table_info({table})"))
        .map_err(|e| e.to_string())?;
    let existing: HashSet<String> = stmt
        .query_map([], |row| row.get::<_, String>(1))
        .map_err(|e| e.to_string())?
        .collect::<Result<_, _>>()
        .map_err(|e| e.to_string())?;
    for (name, decl) in columns {
        if !existing.contains(*name) {
            conn.execute(&format!("ALTER TABLE {table} ADD COLUMN {name} {decl}"), [])
                .map_err(|e| e.to_string())?;
        }
    }
    Ok(())
}

/// Carry a pre-split `show_cut_lines = 0` across to all four hide flags,
/// exactly once.
///
/// Only the "off" case needs carrying: `show_cut_lines = 1` means every
/// guide was drawn, and the new columns' defaults already say that for the
/// fronts. The backs deliberately land on hidden regardless — before this
/// existed there were no Back Pages to have guides on, so there is no old
/// preference about them to preserve.
fn migrate_guide_flags(conn: &Connection) -> Result<(), String> {
    const MIGRATED_KEY: &str = "guide_flags_migrated";
    if read_app_setting(conn, MIGRATED_KEY)?.is_some() {
        return Ok(());
    }
    // Guarded rather than assumed: a database created fresh on this
    // version has the new columns but never had the old one.
    let has_old: bool = conn
        .prepare("PRAGMA table_info(projects)")
        .and_then(|mut stmt| {
            stmt.query_map([], |row| row.get::<_, String>(1))?
                .collect::<Result<Vec<String>, _>>()
        })
        .map_err(|e| e.to_string())?
        .iter()
        .any(|name| name == "show_cut_lines");
    if has_old {
        conn.execute(
            "UPDATE projects SET hide_card_guides_front = 1, hide_page_guides_front = 1,
                hide_card_guides_back = 1, hide_page_guides_back = 1
             WHERE show_cut_lines = 0",
            [],
        )
        .map_err(|e| e.to_string())?;
    }
    write_app_setting(conn, MIGRATED_KEY, "1")
}

/// Rename the page-order value `interleaved` to `duplex`, exactly once.
///
/// Only ever a concern for a database written by a pre-release build:
/// the mode shipped under the wrong name — "interleaved" describes how
/// the pages are arranged, where the user is choosing whether they are
/// duplex printing — and the column default changed with it. Rows added
/// before the rename keep the old string, which no longer deserializes
/// into PageOrder on the server.
fn migrate_page_order_naming(conn: &Connection) -> Result<(), String> {
    const MIGRATED_KEY: &str = "page_order_duplex_renamed";
    if read_app_setting(conn, MIGRATED_KEY)?.is_some() {
        return Ok(());
    }
    conn.execute(
        "UPDATE projects SET page_order = 'duplex' WHERE page_order = 'interleaved'",
        [],
    )
    .map_err(|e| e.to_string())?;
    write_app_setting(conn, MIGRATED_KEY, "1")
}

pub(crate) fn open_db(app: &AppHandle) -> Result<Connection, String> {
    let dir = app
        .path()
        .app_data_dir()
        .map_err(|e| format!("failed to resolve app data dir: {e}"))?;
    std::fs::create_dir_all(&dir).map_err(|e| format!("failed to create app data dir: {e}"))?;
    let conn = Connection::open(dir.join(DB_FILENAME)).map_err(|e| e.to_string())?;
    conn.execute_batch(SCHEMA).map_err(|e| e.to_string())?;
    add_missing_columns(&conn, "projects", PROJECTS_ADDED_COLUMNS)?;
    add_missing_columns(&conn, "project_cards", PROJECT_CARDS_ADDED_COLUMNS)?;
    migrate_guide_flags(&conn)?;
    migrate_page_order_naming(&conn)?;
    Ok(conn)
}

pub(crate) fn now_timestamp() -> String {
    // Not RFC3339 — SystemTime doesn't give calendar fields directly, and
    // pulling in chrono/time just to format one column is overkill here.
    // A plain epoch-seconds string still sorts correctly as text (fixed
    // width would matter for that, but won't overflow until the year
    // 2286), which is all created_at/updated_at are used for.
    let secs = std::time::SystemTime::now()
        .duration_since(std::time::UNIX_EPOCH)
        .map(|d| d.as_secs())
        .unwrap_or(0);
    secs.to_string()
}

// --- Decklist parsing (ported from proxy_scaler/decklist.py) -----------

#[derive(Debug, Clone, Serialize)]
pub struct DeckEntry {
    pub quantity: i64,
    pub name: String,
    pub set_code: Option<String>,
    pub collector_number: Option<String>,
    pub raw_line: String,
}

const SKIP_LINES: &[&str] = &[
    "deck",
    "sideboard",
    "maybeboard",
    "commander",
    "companion",
    "mainboard",
    "main",
];

fn parse_line(
    line: &str,
    set_collector_re: &regex::Regex,
    name_collector_re: &regex::Regex,
    qty_re: &regex::Regex,
) -> Option<DeckEntry> {
    let stripped = line.trim();
    if stripped.is_empty() || stripped.starts_with('#') || stripped.starts_with("//") {
        return None;
    }
    if SKIP_LINES.contains(&stripped.to_lowercase().as_str()) {
        return None;
    }

    let (quantity, rest) = match qty_re.captures(stripped) {
        Some(caps) => (
            caps.name("qty").unwrap().as_str().parse::<i64>().unwrap_or(1),
            caps.name("rest").unwrap().as_str().trim().to_string(),
        ),
        None => (1, stripped.to_string()),
    };

    if let Some(caps) = set_collector_re.captures(&rest) {
        return Some(DeckEntry {
            quantity,
            name: caps.name("name").unwrap().as_str().trim().to_string(),
            set_code: Some(caps.name("set").unwrap().as_str().to_lowercase()),
            collector_number: Some(caps.name("collector").unwrap().as_str().to_string()),
            raw_line: stripped.to_string(),
        });
    }

    if let Some(caps) = name_collector_re.captures(&rest) {
        // Collector-number hint, no set (some deck managers can't export
        // more, notably for non-English cards). set_code stays None — the
        // server matches the hint against the name's printings and falls
        // back to plain name resolution (see card_lookup.CardResolver).
        return Some(DeckEntry {
            quantity,
            name: caps.name("name").unwrap().as_str().trim().to_string(),
            set_code: None,
            collector_number: Some(caps.name("collector").unwrap().as_str().to_string()),
            raw_line: stripped.to_string(),
        });
    }

    Some(DeckEntry {
        quantity,
        name: rest,
        set_code: None,
        collector_number: None,
        raw_line: stripped.to_string(),
    })
}

pub fn parse_decklist_text(text: &str) -> Vec<DeckEntry> {
    // Trailing "(set) collector" — set is alphanumeric; collector is
    // opaque (21p, DDG-14, etc.) — mirrors decklist.py's _SET_COLLECTOR_RE.
    let set_collector_re =
        regex::Regex::new(r"^(?P<name>.+?)\s+\((?P<set>[A-Za-z0-9]+)\)\s+(?P<collector>\S+)\s*$")
            .expect("static regex");
    // Trailing bare collector number with no set — "Sol Ring 263". Strict
    // token shape (digits + one optional letter) so it can't eat the end
    // of a real card name — mirrors decklist.py's _NAME_COLLECTOR_RE.
    let name_collector_re =
        regex::Regex::new(r"^(?P<name>.+?)\s+(?P<collector>\d{1,4}[A-Za-z]?)\s*$")
            .expect("static regex");
    // Optional x after the count covers the "4x Lightning Bolt" style many
    // deck-site exports use — mirrors decklist.py's _QTY_RE.
    let qty_re = regex::Regex::new(r"^(?P<qty>\d+)[xX]?\s+(?P<rest>.+)$").expect("static regex");

    text.lines()
        .filter_map(|line| parse_line(line, &set_collector_re, &name_collector_re, &qty_re))
        .collect()
}

// --- Command payload/response types -------------------------------------

#[derive(Debug, Serialize)]
pub struct ProjectSummary {
    pub id: i64,
    pub tag: String,
    pub name: String,
    pub updated_at: String,
}

#[derive(Debug, Serialize, Deserialize)]
pub struct ProjectSettings {
    pub model: String,
    pub dpi_targets: Vec<i64>,
    pub skip_existing: bool,
    pub tile_size: i64,
    // PDF tab layout settings — mirrors desktop/frontend/src/api/types.ts's
    // PdfLayoutRequest (minus project_tag/entries/project_name, which are
    // per-request, not per-project).
    pub page_width_mm: f64,
    pub page_height_mm: f64,
    pub cols: i64,
    pub rows: i64,
    pub bleed_mm: f64,
    pub spacing_x_mm: f64,
    pub spacing_y_mm: f64,
    pub offset_x_mm: f64,
    pub offset_y_mm: f64,
    pub guide_width_pt: f64,
    pub guide_length_mm: f64,
    pub export_dpi: i64,
    // Guides: one HIDE flag per guide kind per page kind, replacing
    // `show_cut_lines`. Defaulted on deserialize so a frontend build that
    // predates one of them can't wipe the others.
    #[serde(default)]
    pub hide_card_guides_front: bool,
    #[serde(default)]
    pub hide_page_guides_front: bool,
    #[serde(default = "default_true")]
    pub hide_card_guides_back: bool,
    #[serde(default = "default_true")]
    pub hide_page_guides_back: bool,
    // Back printing.
    #[serde(default)]
    pub back_printing: bool,
    // Whether a double-faced card's transform side prints on its own back
    // rather than as a separate card. Inert while back_printing is off.
    #[serde(default = "default_true")]
    pub back_faces_as_reverse: bool,
    #[serde(default = "default_reverse_fill")]
    pub reverse_fill: String,
    #[serde(default = "default_page_order")]
    pub page_order: String,
    #[serde(default = "default_flip_edge")]
    pub flip_edge: String,
    #[serde(default)]
    pub back_offset_x_mm: f64,
    #[serde(default)]
    pub back_offset_y_mm: f64,
    // This project's Selected Back — an id into back_images, or None.
    #[serde(default)]
    pub back_image_id: Option<i64>,
    pub preferred_dpi: Option<i64>,
    pub preferred_model: Option<String>,
    // Source PDF/export runs from the cached ~300 DPI Scryfall originals
    // instead of upscaled outputs (the preferred pair above is inert while
    // set). Defaulted on deserialize so an older frontend build that
    // doesn't send it doesn't wipe the setting.
    #[serde(default)]
    pub use_originals: bool,
    // Import-language preference (Scryfall code): stamped onto cards at
    // decklist import and used to steer server-side resolution. Defaulted
    // on deserialize so an older frontend build that doesn't send it
    // doesn't wipe the setting.
    #[serde(default = "default_preferred_lang")]
    pub preferred_lang: String,
    // The import box's "All Languages" checkbox — best-effort matching
    // across languages instead of strictly preferred_lang.
    #[serde(default)]
    pub lang_any: bool,
}

fn default_preferred_lang() -> String {
    "en".to_string()
}

fn default_true() -> bool {
    true
}

fn default_reverse_fill() -> String {
    "back_image".to_string()
}

fn default_page_order() -> String {
    "duplex".to_string()
}

fn default_flip_edge() -> String {
    "long".to_string()
}

impl Default for ProjectSettings {
    fn default() -> Self {
        Self {
            model: "ultrasharp_v2".to_string(),
            dpi_targets: vec![1200],
            skip_existing: true,
            tile_size: 0,
            page_width_mm: 210.0,
            page_height_mm: 297.0,
            cols: 3,
            rows: 3,
            bleed_mm: 1.0,
            spacing_x_mm: 0.0,
            spacing_y_mm: 0.0,
            offset_x_mm: 0.0,
            offset_y_mm: 0.0,
            guide_width_pt: 0.75,
            guide_length_mm: 2.75,
            export_dpi: 1200,
            hide_card_guides_front: false,
            hide_page_guides_front: false,
            hide_card_guides_back: true,
            hide_page_guides_back: true,
            back_printing: false,
            back_faces_as_reverse: true,
            reverse_fill: default_reverse_fill(),
            page_order: default_page_order(),
            flip_edge: default_flip_edge(),
            back_offset_x_mm: 0.0,
            back_offset_y_mm: 0.0,
            back_image_id: None,
            preferred_dpi: None,
            preferred_model: None,
            use_originals: false,
            preferred_lang: default_preferred_lang(),
            lang_any: false,
        }
    }
}

#[derive(Debug, Serialize)]
pub struct CardRow {
    pub id: i64,
    pub sort_order: i64,
    pub original_import_line: String,
    pub quantity: Option<i64>,
    pub name: String,
    pub set_code: Option<String>,
    pub collector_number: Option<String>,
    pub scryfall_id: Option<String>,
    pub lang: Option<String>,
    pub printed_name: Option<String>,
}

#[derive(Debug, Serialize)]
pub struct LoadedProject {
    pub id: i64,
    pub tag: String,
    pub name: String,
    pub import_decklist_text: String,
    pub settings: ProjectSettings,
    pub cards: Vec<CardRow>,
    pub created_at: String,
    pub updated_at: String,
}

// Import-dedup identity, most-specific-first: exact set/collector/lang
// when all are given (language is part of a printing's identity — the
// Italian and English Sol Ring of one set/collector are different cards
// in a deck), set/collector for the legacy lang-less parse path,
// name+collector for a set-less collector hint ("Sol Ring 263" — see
// parse_line), bare name otherwise. Same spirit as
// mergeCardStatus.ts::cardIdentity, plus the hint tier that only matters
// at import time.
fn entry_dedup_key(
    set_code: Option<&str>,
    collector_number: Option<&str>,
    name: &str,
    lang: Option<&str>,
) -> String {
    match (set_code, collector_number) {
        (Some(s), Some(c)) if !s.is_empty() && !c.is_empty() => match lang {
            Some(l) if !l.is_empty() => {
                format!("{}/{}/{}", s.to_lowercase(), c.to_lowercase(), l.to_lowercase())
            }
            _ => format!("{}/{}", s.to_lowercase(), c.to_lowercase()),
        },
        (_, Some(c)) if !c.is_empty() => {
            format!("name:{}/{}", name.to_lowercase(), c.to_lowercase())
        }
        _ => format!("name:{}", name.to_lowercase()),
    }
}

// Every key an already-stored card answers to — its own specific key plus
// every less-specific one. Needed because resolution *changes* a stored
// row's shape: a card imported as "4 Lightning Bolt" (name key only) gains
// set/collector when the resolve pins it, and without the broader keys
// re-pasting that same decklist line would no longer match the row it
// created and import a duplicate. The printed-name tiers exist for the
// same reason in the other direction: a row imported from a German line
// ends up storing the English name in `name` — the typed German text
// survives only as printed_name, so re-pasting it must match through that.
fn stored_card_dedup_keys(
    set_code: Option<&str>,
    collector_number: Option<&str>,
    name: &str,
    printed_name: Option<&str>,
    lang: Option<&str>,
) -> Vec<String> {
    let mut keys = Vec::new();
    let mut names = vec![name.to_lowercase()];
    if let Some(p) = printed_name.filter(|p| !p.is_empty()) {
        names.push(p.to_lowercase());
    }
    for n in &names {
        keys.push(format!("name:{n}"));
        if let Some(c) = collector_number.filter(|c| !c.is_empty()) {
            keys.push(format!("name:{n}/{}", c.to_lowercase()));
        }
    }
    if let (Some(s), Some(c)) = (
        set_code.filter(|s| !s.is_empty()),
        collector_number.filter(|c| !c.is_empty()),
    ) {
        // Both the lang-qualified key (what resolved imports ask with —
        // absent lang on old rows reads as English, the only language that
        // existed before lang was recorded) and the bare one (what the
        // legacy lang-less parse path asks with).
        let l = lang.filter(|l| !l.is_empty()).unwrap_or("en");
        keys.push(format!(
            "{}/{}/{}",
            s.to_lowercase(),
            c.to_lowercase(),
            l.to_lowercase()
        ));
        keys.push(format!("{}/{}", s.to_lowercase(), c.to_lowercase()));
    }
    keys
}

fn dpi_targets_to_text(dpi_targets: &[i64]) -> String {
    dpi_targets
        .iter()
        .map(|d| d.to_string())
        .collect::<Vec<_>>()
        .join(",")
}

fn dpi_targets_from_text(text: &str) -> Vec<i64> {
    text.split(',')
        .filter_map(|s| s.trim().parse::<i64>().ok())
        .collect()
}

fn row_to_summary(row: &rusqlite::Row<'_>) -> rusqlite::Result<ProjectSummary> {
    Ok(ProjectSummary {
        id: row.get("id")?,
        tag: row.get("tag")?,
        name: row.get("name")?,
        updated_at: row.get("updated_at")?,
    })
}

fn cards_for_project(conn: &Connection, project_id: i64) -> Result<Vec<CardRow>, String> {
    let mut stmt = conn
        .prepare(
            "SELECT id, sort_order, original_import_line, quantity, name, set_code, collector_number,
                    scryfall_id, lang, printed_name
             FROM project_cards WHERE project_id = ?1 ORDER BY sort_order ASC",
        )
        .map_err(|e| e.to_string())?;
    let rows = stmt
        .query_map(params![project_id], |row| {
            Ok(CardRow {
                id: row.get(0)?,
                sort_order: row.get(1)?,
                original_import_line: row.get(2)?,
                quantity: row.get(3)?,
                name: row.get(4)?,
                set_code: row.get(5)?,
                collector_number: row.get(6)?,
                scryfall_id: row.get(7)?,
                lang: row.get(8)?,
                printed_name: row.get(9)?,
            })
        })
        .map_err(|e| e.to_string())?;
    rows.collect::<Result<Vec<_>, _>>().map_err(|e| e.to_string())
}

fn load_project(conn: &Connection, project_id: i64) -> Result<LoadedProject, String> {
    // Named column access (row.get("...")) rather than positional indices —
    // the projects table has grown enough columns (generation settings +
    // the full PDF layout) that positional indices stop being readable.
    struct Row {
        tag: String,
        name: String,
        import_decklist_text: String,
        model: String,
        dpi_targets_text: String,
        skip_existing: bool,
        tile_size: i64,
        page_width_mm: f64,
        page_height_mm: f64,
        cols: i64,
        rows: i64,
        bleed_mm: f64,
        spacing_x_mm: f64,
        spacing_y_mm: f64,
        offset_x_mm: f64,
        offset_y_mm: f64,
        guide_width_pt: f64,
        guide_length_mm: f64,
        export_dpi: i64,
        hide_card_guides_front: bool,
        hide_page_guides_front: bool,
        hide_card_guides_back: bool,
        hide_page_guides_back: bool,
        back_printing: bool,
        back_faces_as_reverse: bool,
        reverse_fill: String,
        page_order: String,
        flip_edge: String,
        back_offset_x_mm: f64,
        back_offset_y_mm: f64,
        back_image_id: Option<i64>,
        preferred_dpi: Option<i64>,
        preferred_model: Option<String>,
        use_originals: bool,
        preferred_lang: String,
        lang_any: bool,
        created_at: String,
        updated_at: String,
    }

    let loaded: Row = conn
        .query_row(
            "SELECT tag, name, import_decklist_text, model, dpi_targets, skip_existing, tile_size,
                    page_width_mm, page_height_mm, cols, rows, bleed_mm, spacing_x_mm, spacing_y_mm,
                    offset_x_mm, offset_y_mm, guide_width_pt, guide_length_mm, export_dpi,
                    hide_card_guides_front, hide_page_guides_front,
                    hide_card_guides_back, hide_page_guides_back,
                    back_printing, back_faces_as_reverse, reverse_fill,
                    page_order, flip_edge,
                    back_offset_x_mm, back_offset_y_mm, back_image_id,
                    preferred_dpi, preferred_model, use_originals, preferred_lang, lang_any,
                    created_at, updated_at
             FROM projects WHERE id = ?1",
            params![project_id],
            |row| {
                Ok(Row {
                    tag: row.get("tag")?,
                    name: row.get("name")?,
                    import_decklist_text: row.get("import_decklist_text")?,
                    model: row.get("model")?,
                    dpi_targets_text: row.get("dpi_targets")?,
                    skip_existing: row.get("skip_existing")?,
                    tile_size: row.get("tile_size")?,
                    page_width_mm: row.get("page_width_mm")?,
                    page_height_mm: row.get("page_height_mm")?,
                    cols: row.get("cols")?,
                    rows: row.get("rows")?,
                    bleed_mm: row.get("bleed_mm")?,
                    spacing_x_mm: row.get("spacing_x_mm")?,
                    spacing_y_mm: row.get("spacing_y_mm")?,
                    offset_x_mm: row.get("offset_x_mm")?,
                    offset_y_mm: row.get("offset_y_mm")?,
                    guide_width_pt: row.get("guide_width_pt")?,
                    guide_length_mm: row.get("guide_length_mm")?,
                    export_dpi: row.get("export_dpi")?,
                    hide_card_guides_front: row.get("hide_card_guides_front")?,
                    hide_page_guides_front: row.get("hide_page_guides_front")?,
                    hide_card_guides_back: row.get("hide_card_guides_back")?,
                    hide_page_guides_back: row.get("hide_page_guides_back")?,
                    back_printing: row.get("back_printing")?,
                    back_faces_as_reverse: row.get("back_faces_as_reverse")?,
                    reverse_fill: row.get("reverse_fill")?,
                    page_order: row.get("page_order")?,
                    flip_edge: row.get("flip_edge")?,
                    back_offset_x_mm: row.get("back_offset_x_mm")?,
                    back_offset_y_mm: row.get("back_offset_y_mm")?,
                    back_image_id: row.get("back_image_id")?,
                    preferred_dpi: row.get("preferred_dpi")?,
                    preferred_model: row.get("preferred_model")?,
                    use_originals: row.get("use_originals")?,
                    preferred_lang: row.get("preferred_lang")?,
                    lang_any: row.get("lang_any")?,
                    created_at: row.get("created_at")?,
                    updated_at: row.get("updated_at")?,
                })
            },
        )
        .map_err(|e| match e {
            rusqlite::Error::QueryReturnedNoRows => format!("project {project_id} not found"),
            other => other.to_string(),
        })?;

    Ok(LoadedProject {
        id: project_id,
        tag: loaded.tag,
        name: loaded.name,
        import_decklist_text: loaded.import_decklist_text,
        settings: ProjectSettings {
            model: loaded.model,
            dpi_targets: dpi_targets_from_text(&loaded.dpi_targets_text),
            skip_existing: loaded.skip_existing,
            tile_size: loaded.tile_size,
            page_width_mm: loaded.page_width_mm,
            page_height_mm: loaded.page_height_mm,
            cols: loaded.cols,
            rows: loaded.rows,
            bleed_mm: loaded.bleed_mm,
            spacing_x_mm: loaded.spacing_x_mm,
            spacing_y_mm: loaded.spacing_y_mm,
            offset_x_mm: loaded.offset_x_mm,
            offset_y_mm: loaded.offset_y_mm,
            guide_width_pt: loaded.guide_width_pt,
            guide_length_mm: loaded.guide_length_mm,
            export_dpi: loaded.export_dpi,
            hide_card_guides_front: loaded.hide_card_guides_front,
            hide_page_guides_front: loaded.hide_page_guides_front,
            hide_card_guides_back: loaded.hide_card_guides_back,
            hide_page_guides_back: loaded.hide_page_guides_back,
            back_printing: loaded.back_printing,
            back_faces_as_reverse: loaded.back_faces_as_reverse,
            reverse_fill: loaded.reverse_fill,
            page_order: loaded.page_order,
            flip_edge: loaded.flip_edge,
            back_offset_x_mm: loaded.back_offset_x_mm,
            back_offset_y_mm: loaded.back_offset_y_mm,
            back_image_id: loaded.back_image_id,
            preferred_dpi: loaded.preferred_dpi,
            preferred_model: loaded.preferred_model,
            use_originals: loaded.use_originals,
            preferred_lang: loaded.preferred_lang,
            lang_any: loaded.lang_any,
        },
        cards: cards_for_project(conn, project_id)?,
        created_at: loaded.created_at,
        updated_at: loaded.updated_at,
    })
}

/// INSERTs a project row and returns its id. The tag is minted by the
/// INSERT itself, so a Project's row and its tag are always born together
/// — named or not. Callers map the UNIQUE violation, since what a
/// duplicate name means differs between them.
fn insert_project(conn: &Connection, name: &str) -> rusqlite::Result<i64> {
    let now = now_timestamp();
    // The app default Back Image is COPIED here, once, and then belongs to
    // the project. Changing the default later never reaches back into
    // existing projects: a project you printed last month has to print
    // identically today, and a live-following default would silently
    // change the output of every project that never made a choice.
    //
    // Read directly rather than through back_images.rs to keep this a
    // plain rusqlite call inside the same transaction as the INSERT.
    let default_back: Option<i64> = conn
        .query_row(
            "SELECT value FROM app_settings WHERE key = 'default_back_image_id'",
            [],
            |row| row.get::<_, String>(0),
        )
        .optional()?
        .and_then(|v| v.parse::<i64>().ok());
    conn.execute(
        "INSERT INTO projects (tag, name, import_decklist_text, back_image_id,
                               created_at, updated_at)
         VALUES (lower(hex(randomblob(16))), ?1, '', ?2, ?3, ?3)",
        params![name, default_back, now],
    )?;
    Ok(conn.last_insert_rowid())
}

fn summary_for_id(conn: &Connection, project_id: i64) -> Result<ProjectSummary, String> {
    conn.query_row(
        "SELECT id, tag, name, updated_at FROM projects WHERE id = ?1",
        params![project_id],
        row_to_summary,
    )
    .map_err(|e| e.to_string())
}

// --- The Unnamed Project --------------------------------------------------
//
// An Unnamed Project is an ordinary `projects` row whose name is the empty
// string — one concept in two states, not a second noun. Naming it *is*
// saving it, and because promotion is an UPDATE (see update_project below)
// the tag it was born with survives, along with everything the generation
// server has already produced under that tag.
//
// At most one can exist: `name TEXT NOT NULL UNIQUE` (:22) means `''`
// belongs to exactly one row, so the invariant is the schema's job rather
// than a convention. See docs — .scratch/optional-projects/decisions/01.

/// The id of the Unnamed Project row, creating it if there isn't one.
///
/// Deliberately *not* called from `open_db` — that runs on every command
/// and must not write. Creation happens lazily, on the write paths that
/// actually need a row (first decklist import, first settings change), so
/// an app installed and never used holds no row at all.
///
/// Named `_id` rather than sharing the command's name below, which returns
/// the whole summary because the frontend needs the tag too.
fn get_or_create_unnamed_project_id(conn: &Connection) -> Result<i64, String> {
    if let Some(id) = unnamed_project_id(conn)? {
        return Ok(id);
    }
    // The same INSERT a named Project gets, tag included: an Unnamed
    // Project is a full-fledged row, not a stub to be filled in later.
    match insert_project(conn, "") {
        Ok(id) => Ok(id),
        // Losing a race is a normal outcome, not an error to show. Tauri
        // commands run on a threadpool, each on its own connection, so two
        // callers that both need the row (a first decklist import and a
        // settings write, say) can both find nothing and both INSERT. The
        // schema settles it — `name TEXT NOT NULL UNIQUE` means the loser
        // gets a UNIQUE violation — and what the loser wanted is exactly
        // what the winner just created, so re-read and take it. Only a
        // failure with no row behind it reaches the user.
        Err(insert_err) => match unnamed_project_id(conn) {
            Ok(Some(id)) => Ok(id),
            _ => Err(insert_err.to_string()),
        },
    }
}

fn unnamed_project_id(conn: &Connection) -> Result<Option<i64>, String> {
    conn.query_row("SELECT id FROM projects WHERE name = ''", [], |row| row.get(0))
        .optional()
        .map_err(|e| e.to_string())
}

/// Deletes the Unnamed Project row if there is one, handing back its tag so
/// the caller can discard the generation server's records for it too.
/// `None` when no such row existed.
///
/// Deliberately *not* get_or_create: this is called to clear the way for a
/// blank slate (New, from a named Project), and creating a row just to
/// delete it would mint and strand a tag on every click.
fn discard_unnamed_project_row(conn: &Connection) -> Result<Option<String>, String> {
    let found: Option<(i64, String)> = conn
        .query_row("SELECT id, tag FROM projects WHERE name = ''", [], |row| {
            Ok((row.get(0)?, row.get(1)?))
        })
        .optional()
        .map_err(|e| e.to_string())?;
    let Some((id, tag)) = found else {
        return Ok(None);
    };
    delete_project_row(conn, id)?;
    Ok(Some(tag))
}

// --- Tauri commands -------------------------------------------------------

/// Returns the Unnamed Project, creating it on first call. The frontend
/// needs both the id and the tag before it can import or generate, so this
/// hands back the whole summary rather than just the id.
#[tauri::command]
pub fn get_or_create_unnamed_project(app: AppHandle) -> Result<ProjectSummary, String> {
    let conn = open_db(&app)?;
    let id = get_or_create_unnamed_project_id(&conn)?;
    summary_for_id(&conn, id)
}

/// Throws away the Unnamed Project row if one exists, returning its tag (so
/// the caller can discard that tag's generation records) or `None` when
/// there was nothing to throw away.
///
/// What "New" needs when it is *not* itself the discard — i.e. from a named
/// Project. Detaching alone isn't a blank slate: an Unnamed Project row can
/// already exist, and get_or_create would hand that row — its cards, its
/// tag — to the supposedly new project at its first write.
#[tauri::command]
pub fn discard_unnamed_project(app: AppHandle) -> Result<Option<String>, String> {
    let conn = open_db(&app)?;
    discard_unnamed_project_row(&conn)
}

fn create_project_row(conn: &Connection, name: &str) -> Result<ProjectSummary, String> {
    let trimmed = name.trim();
    // Kept as-is now that update_project accepts `''`: nothing should reach
    // an Unnamed Project through create_project, which always INSERTs and
    // would therefore mint a second tag.
    if trimmed.is_empty() {
        return Err("Project name is required.".to_string());
    }
    let id = insert_project(conn, trimmed).map_err(|e| {
        if e.to_string().contains("UNIQUE") {
            format!("A project named {trimmed:?} already exists.")
        } else {
            e.to_string()
        }
    })?;
    summary_for_id(conn, id)
}

#[tauri::command]
pub fn create_project(app: AppHandle, name: String) -> Result<ProjectSummary, String> {
    let conn = open_db(&app)?;
    create_project_row(&conn, &name)
}

fn list_project_summaries(conn: &Connection) -> Result<Vec<ProjectSummary>, String> {
    let mut stmt = conn
        // `WHERE name <> ''` hides the Unnamed Project from the picker,
        // where it would otherwise show as a blank entry. Nothing in the
        // schema enforces this — any future query that lists or counts
        // projects needs the same clause. It is the one part of the
        // Unnamed Project design carried by convention.
        .prepare(
            "SELECT id, tag, name, updated_at FROM projects
             WHERE name <> '' ORDER BY updated_at DESC",
        )
        .map_err(|e| e.to_string())?;
    let rows = stmt
        .query_map([], row_to_summary)
        .map_err(|e| e.to_string())?;
    rows.collect::<Result<Vec<_>, _>>().map_err(|e| e.to_string())
}

#[tauri::command]
pub fn list_projects(app: AppHandle) -> Result<Vec<ProjectSummary>, String> {
    let conn = open_db(&app)?;
    list_project_summaries(&conn)
}

#[tauri::command]
pub fn get_project(app: AppHandle, project_id: i64) -> Result<LoadedProject, String> {
    let conn = open_db(&app)?;
    load_project(&conn, project_id)
}

/// Writes a project's name and settings. This is also the promotion path:
/// naming an Unnamed Project is an ordinary `UPDATE ... SET name = ?` that
/// never touches `tag`, so images already generated stay attached.
///
/// An empty name is accepted — the Unnamed Project has to be writable
/// before it is named. The UNIQUE constraint still does the collision
/// check, which is why there is no second one here.
///
/// It never *un-names* a project, though: a blank name leaves the stored
/// one alone (matching the frontend, where clearing the field reverts on
/// blur) rather than being rejected, so the settings half of the write
/// still lands. Blanking a named project would either drop it out of the
/// picker for good — `list_project_summaries` filters `WHERE name <> ''`,
/// and nothing else offers a route back to a row by id — or, with an
/// Unnamed Project present, fail the UNIQUE constraint and report the
/// nonsense `A project named "" already exists.`. Deleting is the gesture
/// for getting rid of a project.
fn update_project_row(
    conn: &Connection,
    project_id: i64,
    name: &str,
    settings: &ProjectSettings,
) -> Result<ProjectSummary, String> {
    let trimmed = name.trim();
    let now = now_timestamp();
    conn.execute(
        // The CASE is the "never un-name" rule above, done in SQL so it
        // needs no read-modify-write of its own: for the Unnamed Project
        // `name` is already `''`, so keeping it is the same as writing it.
        "UPDATE projects SET name = CASE WHEN ?1 <> '' THEN ?1 ELSE name END,
         model = ?2, dpi_targets = ?3, skip_existing = ?4,
         tile_size = ?5, page_width_mm = ?6, page_height_mm = ?7, cols = ?8, rows = ?9,
         bleed_mm = ?10, spacing_x_mm = ?11, spacing_y_mm = ?12, offset_x_mm = ?13,
         offset_y_mm = ?14, guide_width_pt = ?15, guide_length_mm = ?16, export_dpi = ?17,
         preferred_dpi = ?18, preferred_model = ?19, use_originals = ?20,
         preferred_lang = ?21, lang_any = ?22,
         hide_card_guides_front = ?23, hide_page_guides_front = ?24,
         hide_card_guides_back = ?25, hide_page_guides_back = ?26,
         back_printing = ?27, back_faces_as_reverse = ?28, reverse_fill = ?29,
         page_order = ?30, flip_edge = ?31, back_offset_x_mm = ?32,
         back_offset_y_mm = ?33, back_image_id = ?34, updated_at = ?35
         WHERE id = ?36",
        params![
            trimmed,
            settings.model,
            dpi_targets_to_text(&settings.dpi_targets),
            settings.skip_existing,
            settings.tile_size,
            settings.page_width_mm,
            settings.page_height_mm,
            settings.cols,
            settings.rows,
            settings.bleed_mm,
            settings.spacing_x_mm,
            settings.spacing_y_mm,
            settings.offset_x_mm,
            settings.offset_y_mm,
            settings.guide_width_pt,
            settings.guide_length_mm,
            settings.export_dpi,
            settings.preferred_dpi,
            settings.preferred_model,
            settings.use_originals,
            settings.preferred_lang,
            settings.lang_any,
            settings.hide_card_guides_front,
            settings.hide_page_guides_front,
            settings.hide_card_guides_back,
            settings.hide_page_guides_back,
            settings.back_printing,
            settings.back_faces_as_reverse,
            settings.reverse_fill,
            settings.page_order,
            settings.flip_edge,
            settings.back_offset_x_mm,
            settings.back_offset_y_mm,
            settings.back_image_id,
            now,
            project_id,
        ],
    )
    .map_err(|e| {
        if e.to_string().contains("UNIQUE") {
            format!("A project named {trimmed:?} already exists.")
        } else {
            e.to_string()
        }
    })?;
    summary_for_id(conn, project_id)
}

#[tauri::command]
pub fn update_project(
    app: AppHandle,
    project_id: i64,
    name: String,
    settings: ProjectSettings,
) -> Result<ProjectSummary, String> {
    let conn = open_db(&app)?;
    update_project_row(&conn, project_id, &name, &settings)
}

fn delete_project_row(conn: &Connection, project_id: i64) -> Result<(), String> {
    conn.execute("DELETE FROM projects WHERE id = ?1", params![project_id])
        .map_err(|e| e.to_string())?;
    Ok(())
}

#[tauri::command]
pub fn delete_project(app: AppHandle, project_id: i64) -> Result<(), String> {
    let conn = open_db(&app)?;
    delete_project_row(&conn, project_id)
}

#[tauri::command]
pub fn clear_all_projects(app: AppHandle) -> Result<(), String> {
    let conn = open_db(&app)?;
    conn.execute("DELETE FROM projects", []).map_err(|e| e.to_string())?;
    Ok(())
}

/// Adds new card lines from `text` to this project's existing card list —
/// additive, like the old server-side "/import" endpoint, not a replace.
/// A parsed entry is skipped if its entry_dedup_key (set+collector, then
/// name+collector hint, then name) matches an existing card — which
/// answers to every key tier it satisfies, see stored_card_dedup_keys —
/// or an earlier entry in this very same paste. No Scryfall call
/// here (that's /api/resolve's job, on demand, against the generation
/// server). import_decklist_text is still overwritten to the latest
/// pasted text — it's just a convenience mirror of "what did I last paste
/// into this box", not the canonical card list; project_cards is that.
///
/// `&mut Connection` rather than `&Connection` because the whole import is
/// one transaction, and rusqlite takes the connection exclusively for it.
fn import_decklist_into(
    conn: &mut Connection,
    project_id: i64,
    text: &str,
) -> Result<Vec<CardRow>, String> {
    let entries = parse_decklist_text(text);
    let now = now_timestamp();

    let tx = conn.transaction().map_err(|e| e.to_string())?;
    tx.execute(
        "UPDATE projects SET import_decklist_text = ?1, updated_at = ?2 WHERE id = ?3",
        params![text, now, project_id],
    )
    .map_err(|e| e.to_string())?;

    // New cards are stamped with the project's language preference so
    // resolution (server-side, local-corpus-first) can look for that
    // language instead of defaulting to English. scryfall_id stays NULL
    // until the post-import resolve pins each card to an exact printing.
    let preferred_lang: String = tx
        .query_row(
            "SELECT preferred_lang FROM projects WHERE id = ?1",
            params![project_id],
            |row| row.get(0),
        )
        .map_err(|e| e.to_string())?;

    let mut seen_keys: HashSet<String> = HashSet::new();
    let mut next_sort_order: i64 = 0;
    {
        let mut stmt = tx
            .prepare(
                "SELECT sort_order, name, set_code, collector_number, printed_name, lang
                 FROM project_cards WHERE project_id = ?1",
            )
            .map_err(|e| e.to_string())?;
        let rows = stmt
            .query_map(params![project_id], |row| {
                let sort_order: i64 = row.get(0)?;
                let name: String = row.get(1)?;
                let set_code: Option<String> = row.get(2)?;
                let collector_number: Option<String> = row.get(3)?;
                let printed_name: Option<String> = row.get(4)?;
                let lang: Option<String> = row.get(5)?;
                Ok((sort_order, name, set_code, collector_number, printed_name, lang))
            })
            .map_err(|e| e.to_string())?;
        for row in rows {
            let (sort_order, name, set_code, collector_number, printed_name, lang) =
                row.map_err(|e| e.to_string())?;
            seen_keys.extend(stored_card_dedup_keys(
                set_code.as_deref(),
                collector_number.as_deref(),
                &name,
                printed_name.as_deref(),
                lang.as_deref(),
            ));
            next_sort_order = next_sort_order.max(sort_order + 1);
        }
    }

    for entry in &entries {
        let key = entry_dedup_key(entry.set_code.as_deref(), entry.collector_number.as_deref(), &entry.name, None);
        // HashSet::insert returns false when the key was already present
        // — covers both "already an existing card" and "duplicated within
        // this same paste" in one check.
        if !seen_keys.insert(key) {
            continue;
        }
        tx.execute(
            "INSERT INTO project_cards
             (project_id, sort_order, original_import_line, quantity, name, set_code, collector_number, lang)
             VALUES (?1, ?2, ?3, ?4, ?5, ?6, ?7, ?8)",
            params![
                project_id,
                next_sort_order,
                entry.raw_line,
                entry.quantity,
                entry.name,
                entry.set_code,
                entry.collector_number,
                preferred_lang,
            ],
        )
        .map_err(|e| e.to_string())?;
        next_sort_order += 1;
    }
    tx.commit().map_err(|e| e.to_string())?;

    cards_for_project(conn, project_id)
}

#[tauri::command]
pub fn import_decklist_text(
    app: AppHandle,
    project_id: i64,
    text: String,
) -> Result<Vec<CardRow>, String> {
    let mut conn = open_db(&app)?;
    import_decklist_into(&mut conn, project_id, &text)
}

/// Parse only — no DB writes. The first half of the resolve-gated import:
/// the frontend parses here, resolves the entries against the generation
/// server, and then persists only the successes via import_resolved_cards.
#[tauri::command]
pub fn parse_decklist(text: String) -> Vec<DeckEntry> {
    parse_decklist_text(&text)
}

/// One card as it arrives from a successful strict resolve — fully pinned.
#[derive(Debug, Clone, Deserialize)]
pub struct ResolvedImportCard {
    pub raw_line: String,
    pub quantity: i64,
    pub name: String,
    #[serde(default)]
    pub printed_name: Option<String>,
    pub set_code: String,
    pub collector_number: String,
    pub scryfall_id: String,
    pub lang: String,
}

/// The second half of the resolve-gated import: inserts already-resolved
/// cards in one transaction, deduped against the project's existing rows
/// (and within the batch) via the same key scheme import_decklist_into
/// uses — the set/collector tier is always available here since every row
/// arrives pinned. Also mirrors the pasted text onto the project like the
/// legacy import did. Returns the project's full card list.
fn import_resolved_cards_into(
    conn: &mut Connection,
    project_id: i64,
    text: &str,
    cards: &[ResolvedImportCard],
) -> Result<Vec<CardRow>, String> {
    let now = now_timestamp();
    let tx = conn.transaction().map_err(|e| e.to_string())?;
    tx.execute(
        "UPDATE projects SET import_decklist_text = ?1, updated_at = ?2 WHERE id = ?3",
        params![text, now, project_id],
    )
    .map_err(|e| e.to_string())?;

    let mut seen_keys: HashSet<String> = HashSet::new();
    let mut next_sort_order: i64 = 0;
    {
        let mut stmt = tx
            .prepare(
                "SELECT sort_order, name, set_code, collector_number, printed_name, lang
                 FROM project_cards WHERE project_id = ?1",
            )
            .map_err(|e| e.to_string())?;
        let rows = stmt
            .query_map(params![project_id], |row| {
                let sort_order: i64 = row.get(0)?;
                let name: String = row.get(1)?;
                let set_code: Option<String> = row.get(2)?;
                let collector_number: Option<String> = row.get(3)?;
                let printed_name: Option<String> = row.get(4)?;
                let lang: Option<String> = row.get(5)?;
                Ok((sort_order, name, set_code, collector_number, printed_name, lang))
            })
            .map_err(|e| e.to_string())?;
        for row in rows {
            let (sort_order, name, set_code, collector_number, printed_name, lang) =
                row.map_err(|e| e.to_string())?;
            seen_keys.extend(stored_card_dedup_keys(
                set_code.as_deref(),
                collector_number.as_deref(),
                &name,
                printed_name.as_deref(),
                lang.as_deref(),
            ));
            next_sort_order = next_sort_order.max(sort_order + 1);
        }
    }

    for card in cards {
        let key = entry_dedup_key(Some(&card.set_code), Some(&card.collector_number), &card.name, Some(&card.lang));
        if !seen_keys.insert(key) {
            continue;
        }
        tx.execute(
            "INSERT INTO project_cards
             (project_id, sort_order, original_import_line, quantity, name, printed_name,
              set_code, collector_number, scryfall_id, lang)
             VALUES (?1, ?2, ?3, ?4, ?5, ?6, ?7, ?8, ?9, ?10)",
            params![
                project_id,
                next_sort_order,
                card.raw_line,
                card.quantity.max(1),
                card.name,
                card.printed_name,
                card.set_code,
                card.collector_number,
                card.scryfall_id,
                card.lang,
            ],
        )
        .map_err(|e| e.to_string())?;
        next_sort_order += 1;
    }
    tx.commit().map_err(|e| e.to_string())?;

    cards_for_project(conn, project_id)
}

#[tauri::command]
pub fn import_resolved_cards(
    app: AppHandle,
    project_id: i64,
    text: String,
    cards: Vec<ResolvedImportCard>,
) -> Result<Vec<CardRow>, String> {
    let mut conn = open_db(&app)?;
    import_resolved_cards_into(&mut conn, project_id, &text, &cards)
}

#[tauri::command]
pub fn remove_card(app: AppHandle, card_id: i64) -> Result<(), String> {
    let conn = open_db(&app)?;
    conn.execute("DELETE FROM project_cards WHERE id = ?1", params![card_id])
        .map_err(|e| e.to_string())?;
    Ok(())
}

#[tauri::command]
pub fn set_card_quantity(app: AppHandle, card_id: i64, quantity: i64) -> Result<(), String> {
    let conn = open_db(&app)?;
    set_card_quantity_in(&conn, card_id, quantity)
}

// Clamped to a minimum of 1 — deleting a card is remove_card's job, and a
// zero-quantity row would silently drop the card from PDF export.
fn set_card_quantity_in(conn: &Connection, card_id: i64, quantity: i64) -> Result<(), String> {
    conn.execute(
        "UPDATE project_cards SET quantity = ?1 WHERE id = ?2",
        params![quantity.max(1), card_id],
    )
    .map_err(|e| e.to_string())?;
    Ok(())
}

/// One card's resolved identity, as persisted after a server-side resolve
/// or a printing change: the pinned scryfall_id plus the refreshed display
/// cache (canonical name, concrete set/collector for name-only lines, and
/// the printing's language).
#[derive(Debug, Deserialize)]
pub struct CardResolutionUpdate {
    pub card_id: i64,
    pub scryfall_id: String,
    pub name: String,
    pub set_code: String,
    pub collector_number: String,
    pub lang: String,
    #[serde(default)]
    pub printed_name: Option<String>,
}

fn apply_card_resolution(conn: &Connection, update: &CardResolutionUpdate) -> Result<(), String> {
    conn.execute(
        "UPDATE project_cards SET scryfall_id = ?1, name = ?2, set_code = ?3,
         collector_number = ?4, lang = ?5, printed_name = ?6 WHERE id = ?7",
        params![
            update.scryfall_id,
            update.name,
            update.set_code,
            update.collector_number,
            update.lang,
            update.printed_name,
            update.card_id,
        ],
    )
    .map_err(|e| e.to_string())?;
    Ok(())
}

/// Change one card to a different printing (picked from the server's
/// variants endpoint) — same UPDATE as a resolution, it just happens to be
/// user-chosen rather than resolver-chosen.
#[tauri::command]
pub fn set_card_printing(
    app: AppHandle,
    card_id: i64,
    scryfall_id: String,
    name: String,
    set_code: String,
    collector_number: String,
    lang: String,
    printed_name: Option<String>,
) -> Result<(), String> {
    let conn = open_db(&app)?;
    apply_card_resolution(
        &conn,
        &CardResolutionUpdate {
            card_id,
            scryfall_id,
            name,
            set_code,
            collector_number,
            lang,
            printed_name,
        },
    )
}

/// Batched persist of post-import resolve results — one transaction for
/// the whole decklist rather than one round-trip per card.
#[tauri::command]
pub fn set_cards_resolution(
    app: AppHandle,
    updates: Vec<CardResolutionUpdate>,
) -> Result<(), String> {
    let mut conn = open_db(&app)?;
    let tx = conn.transaction().map_err(|e| e.to_string())?;
    for update in &updates {
        apply_card_resolution(&tx, update)?;
    }
    tx.commit().map_err(|e| e.to_string())
}

// --- app_settings ---------------------------------------------------------
//
// One key/value table for the handful of app-wide preferences that aren't
// project data: which project to reopen, which remote hosts to offer, and
// whether the quit prompt still fires. Every one of them reads and writes
// it the same way — absent means "not set yet", and a write is an upsert
// rather than an insert, since these are all set repeatedly.

const LAST_PROJECT_ID_KEY: &str = "last_project_id";

/// Re-exports for back_images.rs, which shares this file's database but
/// keeps its own module. Named `_pub` rather than making the originals
/// public so the many in-file callers below stay visibly module-private.
pub(crate) fn read_app_setting_pub(
    conn: &Connection,
    key: &str,
) -> Result<Option<String>, String> {
    read_app_setting(conn, key)
}

pub(crate) fn write_app_setting_pub(
    conn: &Connection,
    key: &str,
    value: &str,
) -> Result<(), String> {
    write_app_setting(conn, key, value)
}

fn read_app_setting(conn: &Connection, key: &str) -> Result<Option<String>, String> {
    conn.query_row(
        "SELECT value FROM app_settings WHERE key = ?1",
        params![key],
        |row| row.get(0),
    )
    .optional()
    .map_err(|e| e.to_string())
}

fn write_app_setting(conn: &Connection, key: &str, value: &str) -> Result<(), String> {
    conn.execute(
        "INSERT INTO app_settings (key, value) VALUES (?1, ?2)
         ON CONFLICT(key) DO UPDATE SET value = excluded.value",
        params![key, value],
    )
    .map_err(|e| e.to_string())?;
    Ok(())
}

#[tauri::command]
pub fn get_last_project_id(app: AppHandle) -> Result<Option<i64>, String> {
    let conn = open_db(&app)?;
    read_app_setting(&conn, LAST_PROJECT_ID_KEY)?
        .map(|v| v.parse::<i64>().map_err(|e| e.to_string()))
        .transpose()
}

#[tauri::command]
pub fn set_last_project_id(app: AppHandle, project_id: i64) -> Result<(), String> {
    let conn = open_db(&app)?;
    write_app_setting(&conn, LAST_PROJECT_ID_KEY, &project_id.to_string())
}

// --- The quit prompt's "don't ask again" ----------------------------------
//
// Whether closing the window with an unnamed project holding cards still
// offers to name it (QuitPrompt.tsx, .scratch/optional-projects/spec.md
// §6).
//
// Absent means "still offer" — a store that predates this setting has not
// switched anything off. A read that *fails* is a different thing and is
// reported as one; deciding what to do with that belongs to the caller,
// and QuitPrompt.tsx's own answer is to keep offering.

const QUIT_PROMPT_SUPPRESSED_KEY: &str = "quit_prompt_suppressed";

fn read_quit_prompt_suppressed(conn: &Connection) -> Result<bool, String> {
    Ok(read_app_setting(conn, QUIT_PROMPT_SUPPRESSED_KEY)?.as_deref() == Some("1"))
}

fn write_quit_prompt_suppressed(conn: &Connection, suppressed: bool) -> Result<(), String> {
    write_app_setting(
        conn,
        QUIT_PROMPT_SUPPRESSED_KEY,
        if suppressed { "1" } else { "0" },
    )
}

#[tauri::command]
pub fn get_quit_prompt_suppressed(app: AppHandle) -> Result<bool, String> {
    let conn = open_db(&app)?;
    read_quit_prompt_suppressed(&conn)
}

#[tauri::command]
pub fn set_quit_prompt_suppressed(app: AppHandle, suppressed: bool) -> Result<(), String> {
    let conn = open_db(&app)?;
    write_quit_prompt_suppressed(&conn, suppressed)
}

// --- The printing picker's "Show digital" ----------------------------------
//
// A user preference, not per-card/per-popover state: whether digital-only
// printings (MTGO/Arena sets) appear in the change-printing list. Same
// app_settings idiom as the quit prompt above; absent means off.

const SHOW_DIGITAL_PRINTINGS_KEY: &str = "show_digital_printings";

fn read_show_digital_printings(conn: &Connection) -> Result<bool, String> {
    Ok(read_app_setting(conn, SHOW_DIGITAL_PRINTINGS_KEY)?.as_deref() == Some("1"))
}

fn write_show_digital_printings(conn: &Connection, show: bool) -> Result<(), String> {
    write_app_setting(conn, SHOW_DIGITAL_PRINTINGS_KEY, if show { "1" } else { "0" })
}

#[tauri::command]
pub fn get_show_digital_printings(app: AppHandle) -> Result<bool, String> {
    let conn = open_db(&app)?;
    read_show_digital_printings(&conn)
}

#[tauri::command]
pub fn set_show_digital_printings(app: AppHandle, show: bool) -> Result<(), String> {
    let conn = open_db(&app)?;
    write_show_digital_printings(&conn, show)
}

// --- The boot card-database offer's "Don't ask again" ----------------------
//
// CardDbPrompt.tsx offers the corpus download on launch when none is
// imported; this suppresses that offer permanently (the sidebar panel
// remains the way in). Same app_settings idiom; absent means keep asking.

const CARD_DB_PROMPT_DISMISSED_KEY: &str = "card_db_prompt_dismissed";

fn read_card_db_prompt_dismissed(conn: &Connection) -> Result<bool, String> {
    Ok(read_app_setting(conn, CARD_DB_PROMPT_DISMISSED_KEY)?.as_deref() == Some("1"))
}

fn write_card_db_prompt_dismissed(conn: &Connection, dismissed: bool) -> Result<(), String> {
    write_app_setting(
        conn,
        CARD_DB_PROMPT_DISMISSED_KEY,
        if dismissed { "1" } else { "0" },
    )
}

#[tauri::command]
pub fn get_card_db_prompt_dismissed(app: AppHandle) -> Result<bool, String> {
    let conn = open_db(&app)?;
    read_card_db_prompt_dismissed(&conn)
}

#[tauri::command]
pub fn set_card_db_prompt_dismissed(app: AppHandle, dismissed: bool) -> Result<(), String> {
    let conn = open_db(&app)?;
    write_card_db_prompt_dismissed(&conn, dismissed)
}

// --- The update prompt's "skip this version" -------------------------------
//
// Which release the user has explicitly declined (UpdatePrompt.tsx), so
// the boot-time update check stops offering that one — and only that one:
// the next release supersedes the skip by simply not matching it, no
// expiry logic needed. "Later" deliberately writes nothing, so the offer
// returns next launch. Absent means "never skipped anything".

const UPDATE_SKIPPED_VERSION_KEY: &str = "update_skipped_version";

#[tauri::command]
pub fn get_update_skipped_version(app: AppHandle) -> Result<Option<String>, String> {
    let conn = open_db(&app)?;
    read_app_setting(&conn, UPDATE_SKIPPED_VERSION_KEY)
}

#[tauri::command]
pub fn set_update_skipped_version(app: AppHandle, version: String) -> Result<(), String> {
    let conn = open_db(&app)?;
    write_app_setting(&conn, UPDATE_SKIPPED_VERSION_KEY, &version)
}

// --- The Patch Notes dialog's "already seen" version -----------------------
//
// Which release's patch notes the user has already had auto-shown
// (PatchNotesPrompt.tsx). Same one-version-wide shape as the update skip
// above: the next release supersedes the stored version by simply not
// matching it, so the notes reappear exactly once per release with no
// expiry logic. Absent means "never seen any" — a fresh install shows
// the current release's notes.

const PATCH_NOTES_SEEN_VERSION_KEY: &str = "patch_notes_seen_version";

#[tauri::command]
pub fn get_patch_notes_seen_version(app: AppHandle) -> Result<Option<String>, String> {
    let conn = open_db(&app)?;
    read_app_setting(&conn, PATCH_NOTES_SEEN_VERSION_KEY)
}

#[tauri::command]
pub fn set_patch_notes_seen_version(app: AppHandle, version: String) -> Result<(), String> {
    let conn = open_db(&app)?;
    write_app_setting(&conn, PATCH_NOTES_SEEN_VERSION_KEY, &version)
}

// Whether the boot-time update check runs at all (UpdatePrompt.tsx reads
// this before calling check_for_update). Default ON — but the check is an
// unauthenticated request to the release host on every launch, which some
// people reasonably want their machine not to make, so the Decklist
// settings sidebar offers the off switch. The tab bar's manual "Update"
// button path is unaffected: that only exists once a check has run.

const UPDATE_CHECK_ENABLED_KEY: &str = "update_check_enabled";

#[tauri::command]
pub fn get_update_check_enabled(app: AppHandle) -> Result<bool, String> {
    let conn = open_db(&app)?;
    Ok(read_app_setting(&conn, UPDATE_CHECK_ENABLED_KEY)?.as_deref() != Some("0"))
}

#[tauri::command]
pub fn set_update_check_enabled(app: AppHandle, enabled: bool) -> Result<(), String> {
    let conn = open_db(&app)?;
    write_app_setting(&conn, UPDATE_CHECK_ENABLED_KEY, if enabled { "1" } else { "0" })
}

// --- Recent remote hosts --------------------------------------------------
//
// A plain list of remote server address+port pairs the user has
// successfully connected to, most-recent-first, so the connection screens
// (ConnectGate.tsx / SwitchServerDialog.tsx) can offer them instead of a
// blank text field every time. Same app_settings-backed pattern as
// last_project_id above, just a list-shaped value instead of a single one
// — encoded the same way dpi_targets is (join/split on a delimiter in one
// TEXT column) rather than a second table, since this is always read/
// written as a whole.

const RECENT_HOSTS_KEY: &str = "recent_remote_hosts";
const MAX_RECENT_HOSTS: usize = 8;
// Matches supervisor.py's DEFAULT_PORT and connection.tsx's own default —
// what a bare `proxy-scaler-serve` binds to. Used as the port for entries
// saved before this field existed (see hosts_from_text's fallback below).
// (13207 is M-T-G by letter position — 13th, 20th, 7th — chosen to dodge
// the usual 8000/8080/8888/9000/etc collisions.)
const DEFAULT_REMOTE_PORT: u16 = 13207;

#[derive(Debug, Clone, Serialize, Deserialize, PartialEq)]
pub struct RecentHost {
    pub host: String,
    pub port: u16,
}

// "|" rather than ":" as the delimiter: an IPv6 literal or a bracketed
// address can itself contain colons, and this keeps parsing a plain split
// instead of something that has to reason about that.
fn hosts_to_text(hosts: &[RecentHost]) -> String {
    hosts
        .iter()
        .map(|h| format!("{}|{}", h.host, h.port))
        .collect::<Vec<_>>()
        .join("\n")
}

fn hosts_from_text(text: &str) -> Vec<RecentHost> {
    text.lines()
        .filter_map(|line| {
            let line = line.trim();
            if line.is_empty() {
                return None;
            }
            match line.rsplit_once('|') {
                Some((host, port)) => {
                    let host = host.trim();
                    if host.is_empty() {
                        return None;
                    }
                    let port = port.trim().parse::<u16>().unwrap_or(DEFAULT_REMOTE_PORT);
                    Some(RecentHost { host: host.to_string(), port })
                }
                // Pre-port-support entries: the whole line is just the
                // host, so fall back to the server's own default port.
                None => Some(RecentHost {
                    host: line.to_string(),
                    port: DEFAULT_REMOTE_PORT,
                }),
            }
        })
        .collect()
}

fn read_recent_hosts(conn: &Connection) -> Result<Vec<RecentHost>, String> {
    let text = read_app_setting(conn, RECENT_HOSTS_KEY)?;
    Ok(text.map(|t| hosts_from_text(&t)).unwrap_or_default())
}

fn write_recent_hosts(conn: &Connection, hosts: &[RecentHost]) -> Result<(), String> {
    write_app_setting(conn, RECENT_HOSTS_KEY, &hosts_to_text(hosts))
}

#[tauri::command]
pub fn list_recent_hosts(app: AppHandle) -> Result<Vec<RecentHost>, String> {
    let conn = open_db(&app)?;
    read_recent_hosts(&conn)
}

/// Records a successful connection. Trims, de-dupes on the (host, port)
/// pair (an already-known pair moves to the front rather than appearing
/// twice — a different port for the same host is treated as a distinct
/// entry, since that's a genuinely different server to reconnect to), and
/// caps at MAX_RECENT_HOSTS (oldest dropped). A blank host is a no-op —
/// returns the list unchanged rather than erroring, since callers treat
/// this as fire-and-forget after a connection already succeeded.
#[tauri::command]
pub fn add_recent_host(app: AppHandle, host: String, port: u16) -> Result<Vec<RecentHost>, String> {
    let trimmed = host.trim();
    let conn = open_db(&app)?;
    if trimmed.is_empty() {
        return read_recent_hosts(&conn);
    }
    let mut hosts = read_recent_hosts(&conn)?;
    hosts.retain(|h| !(h.host == trimmed && h.port == port));
    hosts.insert(
        0,
        RecentHost { host: trimmed.to_string(), port },
    );
    hosts.truncate(MAX_RECENT_HOSTS);
    write_recent_hosts(&conn, &hosts)?;
    Ok(hosts)
}

#[tauri::command]
pub fn remove_recent_host(app: AppHandle, host: String, port: u16) -> Result<Vec<RecentHost>, String> {
    let conn = open_db(&app)?;
    let mut hosts = read_recent_hosts(&conn)?;
    hosts.retain(|h| !(h.host == host && h.port == port));
    write_recent_hosts(&conn, &hosts)?;
    Ok(hosts)
}

/// Test-only helpers shared with sibling modules' tests (back_images.rs).
/// A current database is SCHEMA plus the post-release column lists, so a
/// hand-rolled CREATE TABLE in a test would drift from what ships.
#[cfg(test)]
pub(crate) mod test_support {
    use super::*;

    pub(crate) fn test_conn() -> Connection {
        let conn = Connection::open_in_memory().expect("open in-memory db");
        conn.execute_batch(SCHEMA).expect("apply schema");
        add_missing_columns(&conn, "projects", PROJECTS_ADDED_COLUMNS).expect("projects columns");
        add_missing_columns(&conn, "project_cards", PROJECT_CARDS_ADDED_COLUMNS)
            .expect("project_cards columns");
        conn
    }
}

#[cfg(test)]
mod tests {
    use super::*;

    // The `_row`/`_id` helpers above exist so these can run against a plain
    // Connection — the #[tauri::command] wrappers need an AppHandle and a
    // real app data dir, neither of which a unit test has.
    use super::test_support::test_conn;

    fn summary(conn: &Connection, id: i64) -> ProjectSummary {
        summary_for_id(conn, id).expect("summary")
    }

    #[test]
    fn get_or_create_unnamed_project_is_idempotent() {
        let conn = test_conn();
        let first = get_or_create_unnamed_project_id(&conn).expect("first call");
        let first_tag = summary(&conn, first).tag;

        let second = get_or_create_unnamed_project_id(&conn).expect("second call");

        assert_eq!(first, second, "the same row should be returned");
        assert_eq!(first_tag, summary(&conn, second).tag, "the tag should not be re-minted");
    }

    #[test]
    fn get_or_create_unnamed_project_mints_a_tag() {
        let conn = test_conn();
        let id = get_or_create_unnamed_project_id(&conn).expect("create");
        let created = summary(&conn, id);

        assert_eq!(created.name, "");
        assert_eq!(created.tag.len(), 32, "lower(hex(randomblob(16))) is 32 hex chars");
        assert!(created.tag.chars().all(|c| c.is_ascii_hexdigit()));
    }

    #[test]
    fn discard_unnamed_project_clears_the_way_for_a_blank_slate() {
        // New, from a named Project: the Unnamed Project row left behind by
        // an earlier session has to go, or get_or_create hands it (cards and
        // all) to the supposedly new project at its first write.
        let mut conn = test_conn();
        let stale = get_or_create_unnamed_project_id(&conn).expect("stale unnamed");
        let stale_tag = summary(&conn, stale).tag;
        import_decklist_into(&mut conn, stale, "1 Sol Ring (c21) 263").expect("import");
        let named = create_project_row(&conn, "Krenko").expect("named");

        let discarded = discard_unnamed_project_row(&conn).expect("discard");

        assert_eq!(discarded.as_deref(), Some(stale_tag.as_str()));
        assert!(load_project(&conn, stale).is_err(), "the stale row is gone");
        assert_eq!(
            summary(&conn, named.id).name,
            "Krenko",
            "the named project the user came from is untouched"
        );
        // And the next write starts genuinely fresh — new row, new tag.
        let fresh = get_or_create_unnamed_project_id(&conn).expect("fresh unnamed");
        assert_ne!(summary(&conn, fresh).tag, stale_tag);
        assert!(cards_for_project(&conn, fresh).expect("cards").is_empty());
    }

    #[test]
    fn discard_unnamed_project_is_a_no_op_with_nothing_to_discard() {
        let conn = test_conn();
        create_project_row(&conn, "Krenko").expect("named");

        assert_eq!(discard_unnamed_project_row(&conn).expect("discard"), None);

        assert_eq!(
            list_project_summaries(&conn).expect("list").len(),
            1,
            "a named project is never mistaken for the Unnamed one"
        );
    }

    #[test]
    fn the_unnamed_project_is_invisible_to_the_picker() {
        let conn = test_conn();
        get_or_create_unnamed_project_id(&conn).expect("create");

        assert!(
            list_project_summaries(&conn).expect("list").is_empty(),
            "an otherwise-empty store should still list no projects"
        );

        create_project_row(&conn, "Krenko").expect("create named");
        let listed = list_project_summaries(&conn).expect("list");
        assert_eq!(
            listed.iter().map(|p| p.name.as_str()).collect::<Vec<_>>(),
            vec!["Krenko"]
        );
    }

    #[test]
    fn update_project_accepts_an_empty_name() {
        let conn = test_conn();
        let id = get_or_create_unnamed_project_id(&conn).expect("create");

        let updated = update_project_row(&conn, id, "", &ProjectSettings::default()).expect("update");

        assert_eq!(updated.name, "");
    }

    #[test]
    fn a_named_project_cannot_be_blanked() {
        let conn = test_conn();
        // With an Unnamed Project already present, blanking a named one
        // would hit the UNIQUE constraint and report the nonsense message
        // `A project named "" already exists.`; without one it would
        // succeed and quietly drop the project out of the picker, which
        // filters `WHERE name <> ''`.
        get_or_create_unnamed_project_id(&conn).expect("create unnamed");
        let named = create_project_row(&conn, "Krenko").expect("create named");
        let settings = ProjectSettings { cols: 4, ..ProjectSettings::default() };

        let updated = update_project_row(&conn, named.id, "   ", &settings)
            .expect("a blank name is ignored, not an error");

        assert_eq!(updated.name, "Krenko", "the stored name stands");
        assert_eq!(
            load_project(&conn, named.id).expect("load").settings.cols,
            4,
            "the settings half of the write still lands"
        );
        assert_eq!(
            list_project_summaries(&conn).expect("list").len(),
            1,
            "and it is still visible to the picker"
        );
    }

    #[test]
    fn get_or_create_unnamed_project_is_idempotent_across_connections() {
        // Two connections onto one shared in-memory database, because
        // every Tauri command opens its own. This does *not* reproduce the
        // interleaving the UNIQUE-violation fallback exists for — that
        // needs both callers inside the same window, which isn't
        // deterministic from here — it pins the outcome the fallback has
        // to produce: one row, whoever asks.
        let winner = Connection::open("file:race-test-db?mode=memory&cache=shared")
            .expect("open shared db");
        winner.execute_batch(SCHEMA).expect("apply schema");
        // Same two steps open_db() runs, in the same order — SCHEMA alone
        // is only half a current database.
        add_missing_columns(&winner, "projects", PROJECTS_ADDED_COLUMNS).expect("columns");
        add_missing_columns(&winner, "project_cards", PROJECT_CARDS_ADDED_COLUMNS)
            .expect("card columns");
        let loser = Connection::open("file:race-test-db?mode=memory&cache=shared")
            .expect("open second handle");

        let winner_id = get_or_create_unnamed_project_id(&winner).expect("winner creates");
        let loser_id = get_or_create_unnamed_project_id(&loser).expect("loser must not error");

        assert_eq!(winner_id, loser_id);
    }

    #[test]
    fn naming_an_unnamed_project_preserves_its_tag() {
        let conn = test_conn();
        let id = get_or_create_unnamed_project_id(&conn).expect("create");
        let minted_tag = summary(&conn, id).tag;

        update_project_row(&conn, id, "", &ProjectSettings::default()).expect("write while unnamed");
        let named = update_project_row(&conn, id, "Krenko", &ProjectSettings::default()).expect("name it");

        assert_eq!(named.name, "Krenko");
        assert_eq!(named.tag, minted_tag, "promotion is an UPDATE, so the tag survives");
        assert_eq!(named.id, id, "promotion must not create a second row");
    }

    #[test]
    fn naming_onto_an_existing_name_reports_the_collision() {
        let conn = test_conn();
        create_project_row(&conn, "Krenko").expect("create named");
        let id = get_or_create_unnamed_project_id(&conn).expect("create unnamed");

        let err = update_project_row(&conn, id, "Krenko", &ProjectSettings::default())
            .expect_err("UNIQUE should reject the duplicate name");

        assert_eq!(err, "A project named \"Krenko\" already exists.");
    }

    #[test]
    fn the_quit_prompt_is_offered_by_default() {
        let conn = test_conn();

        assert!(
            !read_quit_prompt_suppressed(&conn).expect("read"),
            "a store that has never been asked offers the prompt"
        );
    }

    #[test]
    fn a_recent_host_survives_the_app_settings_round_trip() {
        // read_/write_recent_hosts share their SQL with the quit prompt's
        // setting and last_project_id; this pins the list-shaped one, which
        // is the only reader that has to decode what it stored.
        let conn = test_conn();
        let hosts = vec![
            RecentHost { host: "10.0.0.5".to_string(), port: 13207 },
            RecentHost { host: "printbox.local".to_string(), port: 9000 },
        ];

        write_recent_hosts(&conn, &hosts).expect("write");

        assert_eq!(read_recent_hosts(&conn).expect("read"), hosts);
    }

    #[test]
    fn switching_the_quit_prompt_off_and_on_again_rewrites_one_row() {
        let conn = test_conn();

        write_quit_prompt_suppressed(&conn, true).expect("suppress");
        assert!(read_quit_prompt_suppressed(&conn).expect("read"));

        // The second write is the one that would fail on the PRIMARY KEY
        // without ON CONFLICT — and un-ticking the box has to be possible,
        // or the setting is a one-way door with no UI to reopen it.
        write_quit_prompt_suppressed(&conn, false).expect("un-suppress");
        assert!(!read_quit_prompt_suppressed(&conn).expect("read"));
    }

    // The store-visible half of the spec's end-to-end script
    // (.scratch/optional-projects/spec.md §10, steps 1-4 and 6), walked as
    // one sequence rather than as separate cases. The tests above each pin
    // one transition; this pins that they compose — in particular that a
    // discard is followed by a *fresh* tag, which only shows up when the
    // steps run in order. The GUI half of the script (the quit prompt, the
    // debounce, the picker) still needs a person; see ticket 13.
    #[test]
    fn the_verification_script_walks_the_store_from_import_to_discard() {
        let mut conn = test_conn();
        // 1. First launch, no projects: importing is what creates the row.
        let id = get_or_create_unnamed_project_id(&conn).expect("first import creates the row");
        let first_tag = summary(&conn, id).tag;
        let cards = import_decklist_into(&mut conn, id, "1 Sol Ring (c21) 263\n4 Lightning Bolt")
            .expect("import");
        assert_eq!(cards.len(), 2, "the cards land on the Unnamed Project");
        assert!(
            list_project_summaries(&conn).expect("list").is_empty(),
            "and the picker still shows nothing"
        );

        // 3. Collision: "Krenko" is taken, so only the settled name commits.
        create_project_row(&conn, "Krenko").expect("a named project already exists");
        update_project_row(&conn, id, "Krenko", &ProjectSettings::default())
            .expect_err("the name is taken");

        // 2. Generate, then name: the tag — and with it the cards — survives.
        let named = update_project_row(&conn, id, "Krenko Goblins", &ProjectSettings::default())
            .expect("the settled name commits");
        assert_eq!(named.tag, first_tag, "promotion never re-mints the tag");
        assert_eq!(
            cards_for_project(&conn, id).expect("cards").len(),
            2,
            "and the cards are still attached to it"
        );

        // 4. Clearing the name reverts rather than un-naming, so the row
        //    stays findable and the UNIQUE constraint is never tested.
        let blanked = update_project_row(&conn, id, "", &ProjectSettings::default())
            .expect("a blank name is ignored, not an error");
        assert_eq!(blanked.name, "Krenko Goblins");

        // 6. New with cards: the discard deletes the row, and the next
        //    import mints a tag of its own.
        delete_project_row(&conn, id).expect("discard");
        let next_id = get_or_create_unnamed_project_id(&conn).expect("the next import");
        let next = summary(&conn, next_id);
        assert_ne!(next.tag, first_tag, "a discarded tag is never handed back");
        assert!(
            cards_for_project(&conn, next_id).expect("cards").is_empty(),
            "and the new slate is empty"
        );
    }

    #[test]
    fn create_project_still_rejects_an_empty_name() {
        let conn = test_conn();

        let err = create_project_row(&conn, "   ").expect_err("empty name should be rejected");

        assert_eq!(err, "Project name is required.");
        assert!(list_project_summaries(&conn).expect("list").is_empty());
    }

    #[test]
    fn parse_decklist_accepts_x_after_quantity() {
        let entries = parse_decklist_text("4x Lightning Bolt\n2X Sol Ring (c21) 263\n4 Plains");
        assert_eq!(entries.len(), 3);
        assert_eq!((entries[0].quantity, entries[0].name.as_str()), (4, "Lightning Bolt"));
        assert_eq!((entries[1].quantity, entries[1].name.as_str()), (2, "Sol Ring"));
        assert_eq!(entries[1].set_code.as_deref(), Some("c21"));
        assert_eq!((entries[2].quantity, entries[2].name.as_str()), (4, "Plains"));
    }

    #[test]
    fn parse_decklist_reads_a_set_less_collector_hint() {
        let entries = parse_decklist_text(
            "1 Sol Ring 263\n2 History of Benalia 21p\n1 Borrowing 100,000 Arrows\n1 234",
        );
        assert_eq!(entries.len(), 4);
        assert_eq!(entries[0].name, "Sol Ring");
        assert_eq!(entries[0].set_code, None);
        assert_eq!(entries[0].collector_number.as_deref(), Some("263"));
        assert_eq!(entries[1].collector_number.as_deref(), Some("21p"));
        // Tokens that aren't strictly digits(+one letter) stay in the name.
        assert_eq!(entries[2].name, "Borrowing 100,000 Arrows");
        assert_eq!(entries[2].collector_number, None);
        // A bare number alone is a name, not an empty name + hint.
        assert_eq!(entries[3].name, "234");
        assert_eq!(entries[3].collector_number, None);
    }

    #[test]
    fn distinct_collector_hints_of_one_name_both_import() {
        let mut conn = test_conn();
        let id = get_or_create_unnamed_project_id(&conn).expect("create");
        let cards = import_decklist_into(&mut conn, id, "1 Sol Ring 263\n1 Sol Ring 116")
            .expect("import");
        assert_eq!(cards.len(), 2, "different hints are different printings");

        // But the same hint pasted again is the same card.
        let cards = import_decklist_into(&mut conn, id, "1 Sol Ring 263").expect("re-import");
        assert_eq!(cards.len(), 2);
    }

    #[test]
    fn a_resolved_card_still_dedups_its_original_line() {
        // A name-only (or hint) line gains set/collector when the
        // post-import resolve pins it — re-pasting the original decklist
        // must still match the row it created, not import a duplicate.
        let mut conn = test_conn();
        let id = get_or_create_unnamed_project_id(&conn).expect("create");
        let cards = import_decklist_into(&mut conn, id, "4 Lightning Bolt\n1 Sol Ring 263")
            .expect("import");
        assert_eq!(cards.len(), 2);
        apply_card_resolution(
            &conn,
            &CardResolutionUpdate {
                card_id: cards[0].id,
                scryfall_id: "bolt-id".to_string(),
                name: "Lightning Bolt".to_string(),
                set_code: "clu".to_string(),
                collector_number: "141".to_string(),
                lang: "en".to_string(),
                printed_name: None,
            },
        )
        .expect("resolve bolt");
        apply_card_resolution(
            &conn,
            &CardResolutionUpdate {
                card_id: cards[1].id,
                scryfall_id: "sol-id".to_string(),
                name: "Sol Ring".to_string(),
                set_code: "c21".to_string(),
                collector_number: "263".to_string(),
                lang: "en".to_string(),
                printed_name: None,
            },
        )
        .expect("resolve sol ring");

        let cards = import_decklist_into(&mut conn, id, "4 Lightning Bolt\n1 Sol Ring 263")
            .expect("re-paste the same decklist");
        assert_eq!(cards.len(), 2, "no duplicates after resolution reshaped the rows");
    }

    fn _resolved(raw_line: &str, name: &str, set: &str, cn: &str) -> ResolvedImportCard {
        ResolvedImportCard {
            raw_line: raw_line.to_string(),
            quantity: 1,
            name: name.to_string(),
            printed_name: None,
            set_code: set.to_string(),
            collector_number: cn.to_string(),
            scryfall_id: format!("{set}-{cn}"),
            lang: "en".to_string(),
        }
    }

    #[test]
    fn import_resolved_cards_inserts_pinned_rows_and_dedups() {
        let mut conn = test_conn();
        let id = get_or_create_unnamed_project_id(&conn).expect("create");

        let cards = import_resolved_cards_into(
            &mut conn,
            id,
            "1 Sol Ring (c21) 263\n1 Lightning Bolt",
            &[
                _resolved("1 Sol Ring (c21) 263", "Sol Ring", "c21", "263"),
                _resolved("1 Lightning Bolt", "Lightning Bolt", "clu", "141"),
            ],
        )
        .expect("import");
        assert_eq!(cards.len(), 2);
        assert_eq!(cards[0].scryfall_id.as_deref(), Some("c21-263"));
        assert_eq!(cards[1].set_code.as_deref(), Some("clu"));

        // Re-importing the same resolved cards is a no-op.
        let cards = import_resolved_cards_into(
            &mut conn,
            id,
            "1 Sol Ring (c21) 263",
            &[_resolved("1 Sol Ring (c21) 263", "Sol Ring", "c21", "263")],
        )
        .expect("re-import");
        assert_eq!(cards.len(), 2);
    }

    #[test]
    fn different_languages_of_one_printing_coexist() {
        // Language is part of a printing's identity: the Italian and
        // English Sol Ring of the same set/collector are different cards
        // in a deck — but re-importing the same language still dedups.
        let mut conn = test_conn();
        let id = get_or_create_unnamed_project_id(&conn).expect("create");
        let en = _resolved("1 Sol Ring (c21) 263", "Sol Ring", "c21", "263");
        let it = ResolvedImportCard {
            lang: "it".to_string(),
            scryfall_id: "sol-it".to_string(),
            printed_name: Some("Anello Solare".to_string()),
            ..en.clone()
        };
        let cards = import_resolved_cards_into(&mut conn, id, "x", &[en.clone(), it.clone()])
            .expect("import both languages");
        assert_eq!(cards.len(), 2);

        let cards = import_resolved_cards_into(&mut conn, id, "x", &[en, it])
            .expect("re-import");
        assert_eq!(cards.len(), 2, "same-language re-imports dedup");
    }

    #[test]
    fn a_german_line_repaste_dedups_through_printed_name() {
        // "1 Aang der Luftnomade 210" resolves to a row whose `name` is the
        // English oracle name; the typed German text survives only as
        // printed_name — the dedup keys must reach it, or re-pasting the
        // original decklist duplicates the card.
        let mut conn = test_conn();
        let id = get_or_create_unnamed_project_id(&conn).expect("create");
        let de = ResolvedImportCard {
            raw_line: "1 Aang der Luftnomade 210".to_string(),
            quantity: 1,
            name: "Aang, Air Nomad".to_string(),
            printed_name: Some("Aang der Luftnomade".to_string()),
            set_code: "tle".to_string(),
            collector_number: "210".to_string(),
            scryfall_id: "aang-de".to_string(),
            lang: "de".to_string(),
        };
        let cards =
            import_resolved_cards_into(&mut conn, id, "1 Aang der Luftnomade 210", &[de.clone()])
                .expect("import");
        assert_eq!(cards.len(), 1);
        assert_eq!(cards[0].printed_name.as_deref(), Some("Aang der Luftnomade"));

        // The re-paste arrives as the same resolved card (same set/collector
        // tier) — and even a legacy-path re-import of the raw German line
        // matches through the printed-name key.
        let cards =
            import_resolved_cards_into(&mut conn, id, "1 Aang der Luftnomade 210", &[de])
                .expect("re-import");
        assert_eq!(cards.len(), 1);
        let cards = import_decklist_into(&mut conn, id, "1 Aang der Luftnomade 210")
            .expect("legacy re-import of the raw line");
        assert_eq!(cards.len(), 1, "printed-name dedup tier caught it");
    }

    #[test]
    fn show_digital_printings_setting_roundtrips() {
        let conn = test_conn();
        assert!(!read_show_digital_printings(&conn).expect("default off"));
        write_show_digital_printings(&conn, true).expect("enable");
        assert!(read_show_digital_printings(&conn).expect("read"));
        write_show_digital_printings(&conn, false).expect("disable");
        assert!(!read_show_digital_printings(&conn).expect("read"));
    }

    #[test]
    fn card_db_prompt_dismissed_setting_roundtrips() {
        let conn = test_conn();
        assert!(!read_card_db_prompt_dismissed(&conn).expect("default: keep asking"));
        write_card_db_prompt_dismissed(&conn, true).expect("dismiss");
        assert!(read_card_db_prompt_dismissed(&conn).expect("read"));
    }

    #[test]
    fn patch_notes_seen_version_setting_roundtrips() {
        let conn = test_conn();
        assert_eq!(
            read_app_setting(&conn, PATCH_NOTES_SEEN_VERSION_KEY).expect("default: never seen"),
            None
        );
        write_app_setting(&conn, PATCH_NOTES_SEEN_VERSION_KEY, "0.2.1").expect("mark seen");
        assert_eq!(
            read_app_setting(&conn, PATCH_NOTES_SEEN_VERSION_KEY).expect("read").as_deref(),
            Some("0.2.1")
        );
        // A later release overwrites, not appends — the upsert contract.
        write_app_setting(&conn, PATCH_NOTES_SEEN_VERSION_KEY, "0.3.0").expect("supersede");
        assert_eq!(
            read_app_setting(&conn, PATCH_NOTES_SEEN_VERSION_KEY).expect("read").as_deref(),
            Some("0.3.0")
        );
    }

    #[test]
    fn lang_any_setting_roundtrips_through_project_settings() {
        let conn = test_conn();
        let id = get_or_create_unnamed_project_id(&conn).expect("create");
        assert!(!load_project(&conn, id).expect("load").settings.lang_any);
        let settings = ProjectSettings { lang_any: true, ..ProjectSettings::default() };
        update_project_row(&conn, id, "", &settings).expect("update");
        assert!(load_project(&conn, id).expect("load").settings.lang_any);
    }

    #[test]
    fn set_card_quantity_updates_and_clamps_to_one() {
        let mut conn = test_conn();
        let id = get_or_create_unnamed_project_id(&conn).expect("create");
        let cards = import_decklist_into(&mut conn, id, "1 Sol Ring (c21) 263").expect("import");
        let card_id = cards[0].id;

        set_card_quantity_in(&conn, card_id, 4).expect("update");
        assert_eq!(cards_for_project(&conn, id).expect("cards")[0].quantity, Some(4));

        set_card_quantity_in(&conn, card_id, 0).expect("clamp zero");
        assert_eq!(cards_for_project(&conn, id).expect("cards")[0].quantity, Some(1));

        set_card_quantity_in(&conn, card_id, -3).expect("clamp negative");
        assert_eq!(cards_for_project(&conn, id).expect("cards")[0].quantity, Some(1));
    }

    #[test]
    fn add_missing_columns_upgrades_a_legacy_shaped_db() {
        // A projects.db from before scryfall_id/lang/preferred_lang: both
        // tables exist but lack the new columns. CREATE TABLE IF NOT EXISTS
        // won't touch them; the additive migrator must.
        let conn = Connection::open_in_memory().expect("open in-memory db");
        conn.execute_batch(
            "CREATE TABLE projects (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                tag TEXT NOT NULL UNIQUE,
                name TEXT NOT NULL UNIQUE,
                import_decklist_text TEXT NOT NULL DEFAULT '',
                model TEXT NOT NULL DEFAULT 'ultrasharp_v2',
                dpi_targets TEXT NOT NULL DEFAULT '1200',
                skip_existing INTEGER NOT NULL DEFAULT 1,
                tile_size INTEGER NOT NULL DEFAULT 0,
                created_at TEXT NOT NULL,
                updated_at TEXT NOT NULL
            );
            CREATE TABLE project_cards (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                project_id INTEGER NOT NULL REFERENCES projects(id) ON DELETE CASCADE,
                sort_order INTEGER NOT NULL,
                original_import_line TEXT NOT NULL,
                quantity INTEGER,
                name TEXT NOT NULL,
                set_code TEXT,
                collector_number TEXT
            );",
        )
        .expect("legacy schema");

        // A real upgrade re-runs SCHEMA first (open_db does), which leaves
        // the legacy `projects` table alone — CREATE TABLE IF NOT EXISTS —
        // while creating tables that didn't exist back then at all, like
        // app_settings and back_images. Skipping it here tested a database
        // shape the app never actually opens.
        conn.execute_batch(SCHEMA).expect("current schema over the legacy one");

        add_missing_columns(&conn, "projects", PROJECTS_ADDED_COLUMNS).expect("projects");
        add_missing_columns(&conn, "project_cards", PROJECT_CARDS_ADDED_COLUMNS)
            .expect("project_cards");
        // Idempotent: a second pass must be a no-op, not a duplicate-column
        // error.
        add_missing_columns(&conn, "projects", PROJECTS_ADDED_COLUMNS).expect("re-run");

        let id = get_or_create_unnamed_project_id(&conn).expect("create");
        let loaded = load_project(&conn, id).expect("load through the new columns");
        assert_eq!(loaded.settings.preferred_lang, "en");
        assert!(loaded.cards.is_empty());
    }

    #[test]
    fn import_stamps_cards_with_the_project_preferred_lang() {
        let mut conn = test_conn();
        let id = get_or_create_unnamed_project_id(&conn).expect("create");
        let settings = ProjectSettings {
            preferred_lang: "ja".to_string(),
            ..ProjectSettings::default()
        };
        update_project_row(&conn, id, "", &settings).expect("set preferred_lang");

        let cards = import_decklist_into(&mut conn, id, "1 Sol Ring (c21) 263").expect("import");

        assert_eq!(cards[0].lang.as_deref(), Some("ja"));
        assert_eq!(cards[0].scryfall_id, None, "unpinned until the post-import resolve");
    }

    #[test]
    fn set_card_printing_roundtrip() {
        let mut conn = test_conn();
        let id = get_or_create_unnamed_project_id(&conn).expect("create");
        let cards = import_decklist_into(&mut conn, id, "1 Sol Ring (c21) 263").expect("import");

        apply_card_resolution(
            &conn,
            &CardResolutionUpdate {
                card_id: cards[0].id,
                scryfall_id: "sol-sta".to_string(),
                name: "Sol Ring".to_string(),
                set_code: "sta".to_string(),
                collector_number: "116".to_string(),
                lang: "ja".to_string(),
                printed_name: None,
            },
        )
        .expect("change printing");

        let card = &cards_for_project(&conn, id).expect("cards")[0];
        assert_eq!(card.scryfall_id.as_deref(), Some("sol-sta"));
        assert_eq!(card.set_code.as_deref(), Some("sta"));
        assert_eq!(card.collector_number.as_deref(), Some("116"));
        assert_eq!(card.lang.as_deref(), Some("ja"));
        assert_eq!(
            card.original_import_line, "1 Sol Ring (c21) 263",
            "the original pasted line is history, not display state — untouched"
        );
    }

    #[test]
    fn set_cards_resolution_batch_applies_all_in_one_transaction() {
        let mut conn = test_conn();
        let id = get_or_create_unnamed_project_id(&conn).expect("create");
        let cards = import_decklist_into(&mut conn, id, "1 Sol Ring (c21) 263\n1 Lightning Bolt")
            .expect("import");

        let updates: Vec<CardResolutionUpdate> = vec![
            CardResolutionUpdate {
                card_id: cards[0].id,
                scryfall_id: "sol-id".to_string(),
                name: "Sol Ring".to_string(),
                set_code: "c21".to_string(),
                collector_number: "263".to_string(),
                lang: "en".to_string(),
                printed_name: None,
            },
            CardResolutionUpdate {
                card_id: cards[1].id,
                scryfall_id: "bolt-id".to_string(),
                name: "Lightning Bolt".to_string(),
                set_code: "clu".to_string(),
                collector_number: "141".to_string(),
                lang: "en".to_string(),
                printed_name: None,
            },
        ];
        let tx = conn.transaction().expect("tx");
        for update in &updates {
            apply_card_resolution(&tx, update).expect("apply");
        }
        tx.commit().expect("commit");

        let cards = cards_for_project(&conn, id).expect("cards");
        assert_eq!(cards[0].scryfall_id.as_deref(), Some("sol-id"));
        // The name-only line gained a concrete printing from resolution.
        assert_eq!(cards[1].scryfall_id.as_deref(), Some("bolt-id"));
        assert_eq!(cards[1].set_code.as_deref(), Some("clu"));
    }
}

