// The local project store: project CRUD, decklist text, and the parsed
// (but unresolved) card list, all in-process via rusqlite against a
// SQLite file in the app's own data dir. No network calls, no separate
// process — see ARCHITECTURE.md for why this lives here instead of on
// the generation server. Scryfall resolution, the download+upscale
// pipeline, and the task queue stay server-side, scoped by this table's
// `tag` column (an opaque string minted once per project and passed to
// the generation server as `project_tag` — plain scoping, not a foreign
// key).
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

fn open_db(app: &AppHandle) -> Result<Connection, String> {
    let dir = app
        .path()
        .app_data_dir()
        .map_err(|e| format!("failed to resolve app data dir: {e}"))?;
    std::fs::create_dir_all(&dir).map_err(|e| format!("failed to create app data dir: {e}"))?;
    let conn = Connection::open(dir.join(DB_FILENAME)).map_err(|e| e.to_string())?;
    conn.execute_batch(SCHEMA).map_err(|e| e.to_string())?;
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
    let qty_re = regex::Regex::new(r"^(?P<qty>\d+)\s+(?P<rest>.+)$").expect("static regex");

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
}

impl Default for ProjectSettings {
    fn default() -> Self {
        Self {
            model: "ultrasharp_v2".to_string(),
            dpi_targets: vec![1200],
            skip_existing: true,
            tile_size: 0,
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
    let (tag, name, text, model, dpi_targets_text, skip_existing, tile_size, created_at, updated_at): (
        String,
        String,
        String,
        String,
        String,
        bool,
        i64,
        String,
        String,
    ) = conn
        .query_row(
            "SELECT tag, name, import_decklist_text, model, dpi_targets, skip_existing, tile_size, created_at, updated_at
             FROM projects WHERE id = ?1",
            params![project_id],
            |row| {
                Ok((
                    row.get(0)?,
                    row.get(1)?,
                    row.get(2)?,
                    row.get(3)?,
                    row.get(4)?,
                    row.get(5)?,
                    row.get(6)?,
                    row.get(7)?,
                    row.get(8)?,
                ))
            },
        )
        .map_err(|e| match e {
            rusqlite::Error::QueryReturnedNoRows => format!("project {project_id} not found"),
            other => other.to_string(),
        })?;

    Ok(LoadedProject {
        id: project_id,
        tag,
        name,
        import_decklist_text: text,
        settings: ProjectSettings {
            model,
            dpi_targets: dpi_targets_from_text(&dpi_targets_text),
            skip_existing,
            tile_size,
        },
        cards: cards_for_project(conn, project_id)?,
        created_at,
        updated_at,
    })
}

// --- Tauri commands -------------------------------------------------------

#[tauri::command]
pub fn create_project(app: AppHandle, name: String) -> Result<ProjectSummary, String> {
    let trimmed = name.trim();
    if trimmed.is_empty() {
        return Err("Project name is required.".to_string());
    }
    let conn = open_db(&app)?;
    let now = now_timestamp();
    conn.execute(
        "INSERT INTO projects (tag, name, import_decklist_text, created_at, updated_at)
         VALUES (lower(hex(randomblob(16))), ?1, '', ?2, ?2)",
        params![trimmed, now],
    )
    .map_err(|e| {
        if e.to_string().contains("UNIQUE") {
            format!("A project named {trimmed:?} already exists.")
        } else {
            e.to_string()
        }
    })?;
    let id = conn.last_insert_rowid();
    conn.query_row(
        "SELECT id, tag, name, updated_at FROM projects WHERE id = ?1",
        params![id],
        row_to_summary,
    )
    .map_err(|e| e.to_string())
}

#[tauri::command]
pub fn list_projects(app: AppHandle) -> Result<Vec<ProjectSummary>, String> {
    let conn = open_db(&app)?;
    let mut stmt = conn
        .prepare("SELECT id, tag, name, updated_at FROM projects ORDER BY updated_at DESC")
        .map_err(|e| e.to_string())?;
    let rows = stmt
        .query_map([], row_to_summary)
        .map_err(|e| e.to_string())?;
    rows.collect::<Result<Vec<_>, _>>().map_err(|e| e.to_string())
}

#[tauri::command]
pub fn get_project(app: AppHandle, project_id: i64) -> Result<LoadedProject, String> {
    let conn = open_db(&app)?;
    load_project(&conn, project_id)
}

#[tauri::command]
pub fn update_project(
    app: AppHandle,
    project_id: i64,
    name: String,
    settings: ProjectSettings,
) -> Result<ProjectSummary, String> {
    let trimmed = name.trim();
    if trimmed.is_empty() {
        return Err("Project name is required.".to_string());
    }
    let conn = open_db(&app)?;
    let now = now_timestamp();
    conn.execute(
        "UPDATE projects SET name = ?1, model = ?2, dpi_targets = ?3, skip_existing = ?4,
         tile_size = ?5, updated_at = ?6 WHERE id = ?7",
        params![
            trimmed,
            settings.model,
            dpi_targets_to_text(&settings.dpi_targets),
            settings.skip_existing,
            settings.tile_size,
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
    conn.query_row(
        "SELECT id, tag, name, updated_at FROM projects WHERE id = ?1",
        params![project_id],
        row_to_summary,
    )
    .map_err(|e| e.to_string())
}

#[tauri::command]
pub fn delete_project(app: AppHandle, project_id: i64) -> Result<(), String> {
    let conn = open_db(&app)?;
    conn.execute("DELETE FROM projects WHERE id = ?1", params![project_id])
        .map_err(|e| e.to_string())?;
    Ok(())
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
#[tauri::command]
pub fn import_decklist_text(
    app: AppHandle,
    project_id: i64,
    text: String,
) -> Result<Vec<CardRow>, String> {
    let mut conn = open_db(&app)?;
    let entries = parse_decklist_text(&text);
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

    cards_for_project(&conn, project_id)
}

#[tauri::command]
pub fn remove_card(app: AppHandle, card_id: i64) -> Result<(), String> {
    let conn = open_db(&app)?;
    conn.execute("DELETE FROM project_cards WHERE id = ?1", params![card_id])
        .map_err(|e| e.to_string())?;
    Ok(())
}

#[tauri::command]
pub fn get_last_project_id(app: AppHandle) -> Result<Option<i64>, String> {
    let conn = open_db(&app)?;
    conn.query_row(
        "SELECT value FROM app_settings WHERE key = 'last_project_id'",
        [],
        |row| row.get::<_, String>(0),
    )
    .optional()
    .map_err(|e| e.to_string())?
    .map(|v| v.parse::<i64>().map_err(|e| e.to_string()))
    .transpose()
}

#[tauri::command]
pub fn set_last_project_id(app: AppHandle, project_id: i64) -> Result<(), String> {
    let conn = open_db(&app)?;
    conn.execute(
        "INSERT INTO app_settings (key, value) VALUES ('last_project_id', ?1)
         ON CONFLICT(key) DO UPDATE SET value = excluded.value",
        params![project_id.to_string()],
    )
    .map_err(|e| e.to_string())?;
    Ok(())
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
    let text: Option<String> = conn
        .query_row(
            "SELECT value FROM app_settings WHERE key = ?1",
            params![RECENT_HOSTS_KEY],
            |row| row.get(0),
        )
        .optional()
        .map_err(|e| e.to_string())?;
    Ok(text.map(|t| hosts_from_text(&t)).unwrap_or_default())
}

fn write_recent_hosts(conn: &Connection, hosts: &[RecentHost]) -> Result<(), String> {
    conn.execute(
        "INSERT INTO app_settings (key, value) VALUES (?1, ?2)
         ON CONFLICT(key) DO UPDATE SET value = excluded.value",
        params![RECENT_HOSTS_KEY, hosts_to_text(hosts)],
    )
    .map_err(|e| e.to_string())?;
    Ok(())
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
