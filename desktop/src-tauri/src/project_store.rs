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
    collector_number TEXT
);
CREATE INDEX IF NOT EXISTS idx_project_cards_project_id ON project_cards(project_id);
CREATE TABLE IF NOT EXISTS app_settings (
    key TEXT PRIMARY KEY,
    value TEXT NOT NULL
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
const PDF_LAYOUT_COLUMNS: &[(&str, &str)] = &[
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
];

fn migrate_projects_table(conn: &Connection) -> Result<(), String> {
    let mut stmt = conn
        .prepare("PRAGMA table_info(projects)")
        .map_err(|e| e.to_string())?;
    let existing: HashSet<String> = stmt
        .query_map([], |row| row.get::<_, String>(1))
        .map_err(|e| e.to_string())?
        .collect::<Result<_, _>>()
        .map_err(|e| e.to_string())?;
    for (name, decl) in PDF_LAYOUT_COLUMNS {
        if !existing.contains(*name) {
            conn.execute(&format!("ALTER TABLE projects ADD COLUMN {name} {decl}"), [])
                .map_err(|e| e.to_string())?;
        }
    }
    Ok(())
}

fn open_db(app: &AppHandle) -> Result<Connection, String> {
    let dir = app
        .path()
        .app_data_dir()
        .map_err(|e| format!("failed to resolve app data dir: {e}"))?;
    std::fs::create_dir_all(&dir).map_err(|e| format!("failed to create app data dir: {e}"))?;
    let conn = Connection::open(dir.join(DB_FILENAME)).map_err(|e| e.to_string())?;
    conn.execute_batch(SCHEMA).map_err(|e| e.to_string())?;
    migrate_projects_table(&conn)?;
    Ok(conn)
}

fn now_timestamp() -> String {
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

fn parse_line(line: &str, set_collector_re: &regex::Regex, qty_re: &regex::Regex) -> Option<DeckEntry> {
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
    // Optional x after the count covers the "4x Lightning Bolt" style many
    // deck-site exports use — mirrors decklist.py's _QTY_RE.
    let qty_re = regex::Regex::new(r"^(?P<qty>\d+)[xX]?\s+(?P<rest>.+)$").expect("static regex");

    text.lines()
        .filter_map(|line| parse_line(line, &set_collector_re, &qty_re))
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
    pub show_cut_lines: bool,
    pub preferred_dpi: Option<i64>,
    pub preferred_model: Option<String>,
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
            show_cut_lines: true,
            preferred_dpi: None,
            preferred_model: None,
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

// Same identity scheme mergeCardStatus.ts::cardIdentity uses on the
// frontend to match a local card to its generation-server records: exact
// set/collector when both are given, otherwise fall back to the card
// name — so "already have this card" means the same thing everywhere,
// not just within this one function.
fn card_dedup_key(set_code: Option<&str>, collector_number: Option<&str>, name: &str) -> String {
    match (set_code, collector_number) {
        (Some(s), Some(c)) if !s.is_empty() && !c.is_empty() => {
            format!("{}/{}", s.to_lowercase(), c.to_lowercase())
        }
        _ => format!("name:{}", name.to_lowercase()),
    }
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
            "SELECT id, sort_order, original_import_line, quantity, name, set_code, collector_number
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
        show_cut_lines: bool,
        preferred_dpi: Option<i64>,
        preferred_model: Option<String>,
        created_at: String,
        updated_at: String,
    }

    let loaded: Row = conn
        .query_row(
            "SELECT tag, name, import_decklist_text, model, dpi_targets, skip_existing, tile_size,
                    page_width_mm, page_height_mm, cols, rows, bleed_mm, spacing_x_mm, spacing_y_mm,
                    offset_x_mm, offset_y_mm, guide_width_pt, guide_length_mm, export_dpi,
                    show_cut_lines, preferred_dpi, preferred_model, created_at, updated_at
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
                    show_cut_lines: row.get("show_cut_lines")?,
                    preferred_dpi: row.get("preferred_dpi")?,
                    preferred_model: row.get("preferred_model")?,
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
            show_cut_lines: loaded.show_cut_lines,
            preferred_dpi: loaded.preferred_dpi,
            preferred_model: loaded.preferred_model,
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
    conn.execute(
        "INSERT INTO projects (tag, name, import_decklist_text, created_at, updated_at)
         VALUES (lower(hex(randomblob(16))), ?1, '', ?2, ?2)",
        params![name, now],
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
         show_cut_lines = ?18, preferred_dpi = ?19, preferred_model = ?20, updated_at = ?21
         WHERE id = ?22",
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
            settings.show_cut_lines,
            settings.preferred_dpi,
            settings.preferred_model,
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
/// A parsed entry is skipped if it already matches an existing card (or an
/// earlier entry in this very same paste) by card_dedup_key — set_code+
/// collector_number when both are given, name otherwise. No Scryfall call
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

    let mut seen_keys: HashSet<String> = HashSet::new();
    let mut next_sort_order: i64 = 0;
    {
        let mut stmt = tx
            .prepare(
                "SELECT sort_order, name, set_code, collector_number
                 FROM project_cards WHERE project_id = ?1",
            )
            .map_err(|e| e.to_string())?;
        let rows = stmt
            .query_map(params![project_id], |row| {
                let sort_order: i64 = row.get(0)?;
                let name: String = row.get(1)?;
                let set_code: Option<String> = row.get(2)?;
                let collector_number: Option<String> = row.get(3)?;
                Ok((sort_order, name, set_code, collector_number))
            })
            .map_err(|e| e.to_string())?;
        for row in rows {
            let (sort_order, name, set_code, collector_number) = row.map_err(|e| e.to_string())?;
            seen_keys.insert(card_dedup_key(
                set_code.as_deref(),
                collector_number.as_deref(),
                &name,
            ));
            next_sort_order = next_sort_order.max(sort_order + 1);
        }
    }

    for entry in &entries {
        let key = card_dedup_key(entry.set_code.as_deref(), entry.collector_number.as_deref(), &entry.name);
        // HashSet::insert returns false when the key was already present
        // — covers both "already an existing card" and "duplicated within
        // this same paste" in one check.
        if !seen_keys.insert(key) {
            continue;
        }
        tx.execute(
            "INSERT INTO project_cards
             (project_id, sort_order, original_import_line, quantity, name, set_code, collector_number)
             VALUES (?1, ?2, ?3, ?4, ?5, ?6, ?7)",
            params![
                project_id,
                next_sort_order,
                entry.raw_line,
                entry.quantity,
                entry.name,
                entry.set_code,
                entry.collector_number,
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

// --- app_settings ---------------------------------------------------------
//
// One key/value table for the handful of app-wide preferences that aren't
// project data: which project to reopen, which remote hosts to offer, and
// whether the quit prompt still fires. Every one of them reads and writes
// it the same way — absent means "not set yet", and a write is an upsert
// rather than an insert, since these are all set repeatedly.

const LAST_PROJECT_ID_KEY: &str = "last_project_id";

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

#[cfg(test)]
mod tests {
    use super::*;

    // The `_row`/`_id` helpers above exist so these can run against a plain
    // Connection — the #[tauri::command] wrappers need an AppHandle and a
    // real app data dir, neither of which a unit test has.
    fn test_conn() -> Connection {
        let conn = Connection::open_in_memory().expect("open in-memory db");
        conn.execute_batch(SCHEMA).expect("apply schema");
        conn
    }

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
}

