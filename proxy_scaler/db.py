"""SQLite persistence for proxy-scaler projects."""

from __future__ import annotations

import re
import sqlite3
from dataclasses import dataclass
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

from .decklist import DeckEntry, parse_decklist_text
from .dpi import DEFAULT_DPI
from .upscale import UpscaleModel

DEFAULT_DB_PATH = Path(__file__).resolve().parents[1] / "data" / "proxy_scaler.db"

# Abandoned_Air_Temple-TLA-263-swinir-600dpi.png
# Name-SET-COLLECTOR-front-swinir-800dpi.png  (collector may contain hyphens)
# Parsed from the right so numeric collectors are not mistaken for set codes.
_OUTPUT_SUFFIX_RE = re.compile(
    r"^(?P<head>.+?)"
    r"(?:-(?P<face>front|back))?"
    r"-(?P<model>swinir|realesrnet|realesrgan)"
    r"-(?P<dpi>\d+)dpi\.png$",
    re.IGNORECASE,
)

_SCHEMA = """
PRAGMA foreign_keys = ON;

CREATE TABLE IF NOT EXISTS projects (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    name TEXT NOT NULL UNIQUE,
    import_decklist_text TEXT NOT NULL DEFAULT '',
    model TEXT NOT NULL DEFAULT 'swinir',
    dpi INTEGER NOT NULL DEFAULT 800,
    all_dpis INTEGER NOT NULL DEFAULT 0,
    dpi_row_mode INTEGER NOT NULL DEFAULT 0,
    page_size INTEGER NOT NULL DEFAULT 6,
    skip_existing INTEGER NOT NULL DEFAULT 1,
    output_dir TEXT NOT NULL DEFAULT '',
    cache_dir TEXT NOT NULL DEFAULT '',
    weights_dir TEXT NOT NULL DEFAULT '',
    created_at TEXT NOT NULL,
    updated_at TEXT NOT NULL
);

CREATE TABLE IF NOT EXISTS project_cards (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    project_id INTEGER NOT NULL REFERENCES projects(id) ON DELETE CASCADE,
    sort_order INTEGER NOT NULL,
    original_import_line TEXT NOT NULL,
    quantity INTEGER,
    card_name TEXT,
    set_code TEXT,
    collector_number TEXT
);

CREATE INDEX IF NOT EXISTS idx_project_cards_project
    ON project_cards(project_id, sort_order);

CREATE TABLE IF NOT EXISTS project_gallery_items (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    card_id INTEGER NOT NULL REFERENCES project_cards(id) ON DELETE CASCADE,
    scryfall_id TEXT NOT NULL,
    face_index INTEGER,
    face_name TEXT,
    card_name TEXT,
    set_code TEXT,
    collector_number TEXT,
    face_label TEXT,
    model TEXT NOT NULL,
    dpi INTEGER NOT NULL,
    native_scale INTEGER NOT NULL DEFAULT 4,
    device TEXT NOT NULL DEFAULT 'unknown',
    image_filename TEXT NOT NULL,
    out_path TEXT NOT NULL,
    original_path TEXT NOT NULL,
    png_url TEXT NOT NULL,
    UNIQUE (card_id, scryfall_id, face_index, model, dpi)
);

CREATE INDEX IF NOT EXISTS idx_gallery_card
    ON project_gallery_items(card_id);

CREATE TABLE IF NOT EXISTS app_settings (
    key TEXT PRIMARY KEY,
    value TEXT NOT NULL
);
"""

_LAST_PROJECT_KEY = "last_project_id"


def _utc_now() -> str:
    return datetime.now(timezone.utc).replace(microsecond=0).isoformat()


@dataclass(frozen=True)
class ProjectSummary:
    id: int
    name: str
    updated_at: str


@dataclass
class ProjectSettings:
    model: str = UpscaleModel.SWINIR.value
    dpi: int = DEFAULT_DPI
    all_dpis: bool = False
    page_size: int = 6
    skip_existing: bool = True
    output_dir: str = ""
    cache_dir: str = ""
    weights_dir: str = ""

    def to_row(self) -> dict[str, Any]:
        return {
            "model": self.model,
            "dpi": int(self.dpi),
            "all_dpis": 1 if self.all_dpis else 0,
            "page_size": int(self.page_size),
            "skip_existing": 1 if self.skip_existing else 0,
            "output_dir": self.output_dir,
            "cache_dir": self.cache_dir,
            "weights_dir": self.weights_dir,
        }

    @classmethod
    def from_row(cls, row: sqlite3.Row | dict[str, Any]) -> ProjectSettings:
        get = row.__getitem__
        return cls(
            model=str(get("model") or UpscaleModel.SWINIR.value),
            dpi=int(get("dpi") or DEFAULT_DPI),
            all_dpis=bool(get("all_dpis")),
            page_size=int(get("page_size") or 6),
            skip_existing=bool(get("skip_existing")),
            output_dir=str(get("output_dir") or ""),
            cache_dir=str(get("cache_dir") or ""),
            weights_dir=str(get("weights_dir") or ""),
        )


@dataclass
class LoadedProject:
    id: int
    name: str
    import_decklist_text: str
    settings: ProjectSettings
    gallery: list[dict[str, Any]]
    created_at: str
    updated_at: str


def connect(db_path: Path | str | None = None) -> sqlite3.Connection:
    path = Path(db_path) if db_path else DEFAULT_DB_PATH
    path.parent.mkdir(parents=True, exist_ok=True)
    conn = sqlite3.connect(str(path))
    conn.row_factory = sqlite3.Row
    conn.execute("PRAGMA foreign_keys = ON")
    return conn


def init_db(db_path: Path | str | None = None) -> Path:
    path = Path(db_path) if db_path else DEFAULT_DB_PATH
    with connect(path) as conn:
        conn.executescript(_SCHEMA)
        _migrate(conn)
        conn.commit()
    return path


def _migrate(conn: sqlite3.Connection) -> None:
    """Additive migrations for existing databases."""
    cols = {
        row[1]
        for row in conn.execute("PRAGMA table_info(project_gallery_items)").fetchall()
    }
    if "device" not in cols:
        conn.execute(
            "ALTER TABLE project_gallery_items "
            "ADD COLUMN device TEXT NOT NULL DEFAULT 'unknown'"
        )


def list_projects(db_path: Path | str | None = None) -> list[ProjectSummary]:
    with connect(db_path) as conn:
        rows = conn.execute(
            "SELECT id, name, updated_at FROM projects ORDER BY updated_at DESC, name ASC"
        ).fetchall()
    return [
        ProjectSummary(id=int(r["id"]), name=r["name"], updated_at=r["updated_at"])
        for r in rows
    ]


def delete_project(project_id: int, db_path: Path | str | None = None) -> None:
    with connect(db_path) as conn:
        conn.execute("DELETE FROM projects WHERE id = ?", (project_id,))
        conn.commit()


def set_last_project_id(project_id: int, db_path: Path | str | None = None) -> None:
    with connect(db_path) as conn:
        conn.execute(
            "INSERT INTO app_settings (key, value) VALUES (?, ?) "
            "ON CONFLICT(key) DO UPDATE SET value = excluded.value",
            (_LAST_PROJECT_KEY, str(project_id)),
        )
        conn.commit()


def get_last_project_id(db_path: Path | str | None = None) -> int | None:
    with connect(db_path) as conn:
        row = conn.execute(
            "SELECT value FROM app_settings WHERE key = ?", (_LAST_PROJECT_KEY,)
        ).fetchone()
    if row is None:
        return None
    try:
        return int(row["value"])
    except (TypeError, ValueError):
        return None


def parse_output_filename(name: str) -> dict[str, Any] | None:
    """Parse a proxy-scaler output PNG basename into metadata fields."""
    match = _OUTPUT_SUFFIX_RE.match(name)
    if not match:
        return None
    head = match.group("head")
    # Non-greedy stem so SET (2–6 alnum, starts with a letter) is found early.
    set_col = re.match(
        r"^(?P<stem>.+?)-(?P<set>[A-Za-z][A-Za-z0-9]{1,5})-(?P<collector>.+)$",
        head,
    )
    if not set_col:
        return None
    face = match.group("face")
    face_index = None if face is None else (0 if face.lower() == "front" else 1)
    return {
        "image_filename": name,
        "set_code": set_col.group("set").lower(),
        "collector_number": set_col.group("collector"),
        "face_label": face.lower() if face else None,
        "face_index": face_index,
        "model": match.group("model").lower(),
        "dpi": int(match.group("dpi")),
        "stem": set_col.group("stem"),
    }


def scan_gallery_from_output(
    output_dir: Path | str,
    entries: list[DeckEntry],
    *,
    cache_dir: Path | str | None = None,
) -> list[dict[str, Any]]:
    """Rebuild gallery dicts by matching output PNGs to decklist entries."""
    from .pipeline import _safe_filename_part

    out = Path(output_dir)
    if not out.is_dir() or not entries:
        return []

    # Prefer matching known printings against the filename head (handles
    # hyphenated collectors reliably).
    printings: list[tuple[str, DeckEntry]] = []
    for entry in entries:
        if entry.set_code and entry.collector_number is not None:
            token = (
                f"-{entry.set_code.upper()}-"
                f"{_safe_filename_part(str(entry.collector_number))}"
            )
            printings.append((token, entry))

    gallery: list[dict[str, Any]] = []
    for path in sorted(out.glob("*.png")):
        suffix = _OUTPUT_SUFFIX_RE.match(path.name)
        if not suffix:
            continue
        head = suffix.group("head")
        entry: DeckEntry | None = None
        for token, candidate in printings:
            if head.endswith(token):
                entry = candidate
                break
        if entry is None:
            # Fall back to generic parse (name-only lines won't match either way)
            meta = parse_output_filename(path.name)
            if meta is None:
                continue
            for token, candidate in printings:
                if (
                    candidate.set_code == meta["set_code"]
                    and str(candidate.collector_number) == str(meta["collector_number"])
                ):
                    entry = candidate
                    break
            if entry is None:
                continue
            face_label = meta["face_label"]
            face_index = meta["face_index"]
            model = meta["model"]
            dpi = meta["dpi"]
        else:
            face = suffix.group("face")
            face_label = face.lower() if face else None
            face_index = (
                None if face is None else (0 if face.lower() == "front" else 1)
            )
            model = suffix.group("model").lower()
            dpi = int(suffix.group("dpi"))

        gallery.append(
            {
                "out_path": str(path.resolve()),
                "original_path": "",
                "scryfall_id": "",
                "face_index": face_index,
                "face_name": entry.name.split(" // ")[0]
                if face_index in (None, 0)
                else (
                    entry.name.split(" // ")[-1].strip()
                    if " // " in entry.name
                    else entry.name
                ),
                "card_name": entry.name,
                "set_code": entry.set_code or "",
                "collector_number": str(entry.collector_number),
                "png_url": "",
                "dpi": dpi,
                "model": model,
                "face_label": face_label,
                "native_scale": 4,
                "device": "unknown",
                "image_filename": path.name,
            }
        )
    return gallery

def _match_card_id(
    cards: list[tuple[int, DeckEntry]],
    item: dict[str, Any],
) -> int | None:
    """Best-effort match gallery item → inserted card id."""
    set_code = (item.get("set_code") or "").lower() or None
    collector = item.get("collector_number")
    card_name = (item.get("card_name") or item.get("face_name") or "").casefold()

    # Prefer set + collector
    if set_code and collector is not None:
        for card_id, entry in cards:
            if (
                entry.set_code == set_code
                and entry.collector_number == str(collector)
            ):
                return card_id

    # Fall back to name containment (DFC front name vs full name)
    for card_id, entry in cards:
        ename = entry.name.casefold()
        if not card_name:
            continue
        if card_name == ename or card_name in ename or ename.split(" // ")[0] == card_name:
            return card_id

    # Last resort: first card
    return cards[0][0] if cards else None


def save_project(
    name: str,
    *,
    import_decklist_text: str,
    settings: ProjectSettings,
    gallery: list[dict[str, Any]],
    project_id: int | None = None,
    db_path: Path | str | None = None,
) -> int:
    """Upsert a project and replace its cards + gallery in one transaction."""
    name = name.strip()
    if not name:
        raise ValueError("Project name is required")

    entries = parse_decklist_text(import_decklist_text)
    now = _utc_now()
    s = settings.to_row()

    # If the session gallery is empty (e.g. saved before generate, or after a
    # refresh), recover metadata from PNGs already on disk.
    if not gallery and settings.output_dir:
        gallery = scan_gallery_from_output(
            settings.output_dir,
            entries,
            cache_dir=settings.cache_dir or None,
        )

    with connect(db_path) as conn:
        if project_id is not None:
            existing = conn.execute(
                "SELECT id FROM projects WHERE id = ?", (project_id,)
            ).fetchone()
            if existing is None:
                project_id = None

        if project_id is None:
            by_name = conn.execute(
                "SELECT id FROM projects WHERE name = ?", (name,)
            ).fetchone()
            if by_name:
                project_id = int(by_name["id"])

        if project_id is None:
            cur = conn.execute(
                """
                INSERT INTO projects (
                    name, import_decklist_text, model, dpi, all_dpis,
                    page_size, skip_existing, output_dir, cache_dir, weights_dir,
                    created_at, updated_at
                ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
                """,
                (
                    name,
                    import_decklist_text,
                    s["model"],
                    s["dpi"],
                    s["all_dpis"],
                    s["page_size"],
                    s["skip_existing"],
                    s["output_dir"],
                    s["cache_dir"],
                    s["weights_dir"],
                    now,
                    now,
                ),
            )
            project_id = int(cur.lastrowid)
        else:
            # Rename collision check
            clash = conn.execute(
                "SELECT id FROM projects WHERE name = ? AND id != ?",
                (name, project_id),
            ).fetchone()
            if clash:
                raise ValueError(f"A project named {name!r} already exists")
            conn.execute(
                """
                UPDATE projects SET
                    name = ?,
                    import_decklist_text = ?,
                    model = ?, dpi = ?, all_dpis = ?,
                    page_size = ?, skip_existing = ?,
                    output_dir = ?, cache_dir = ?, weights_dir = ?,
                    updated_at = ?
                WHERE id = ?
                """,
                (
                    name,
                    import_decklist_text,
                    s["model"],
                    s["dpi"],
                    s["all_dpis"],
                    s["page_size"],
                    s["skip_existing"],
                    s["output_dir"],
                    s["cache_dir"],
                    s["weights_dir"],
                    now,
                    project_id,
                ),
            )

        # Replace children
        conn.execute("DELETE FROM project_cards WHERE project_id = ?", (project_id,))

        inserted: list[tuple[int, DeckEntry]] = []
        for order, entry in enumerate(entries):
            cur = conn.execute(
                """
                INSERT INTO project_cards (
                    project_id, sort_order, original_import_line,
                    quantity, card_name, set_code, collector_number
                ) VALUES (?, ?, ?, ?, ?, ?, ?)
                """,
                (
                    project_id,
                    order,
                    entry.raw_line,
                    entry.quantity,
                    entry.name,
                    entry.set_code,
                    entry.collector_number,
                ),
            )
            inserted.append((int(cur.lastrowid), entry))

        for item in gallery:
            card_id = _match_card_id(inserted, item)
            if card_id is None:
                continue
            out_path = str(item.get("out_path") or "")
            image_filename = item.get("image_filename") or Path(out_path).name
            face_index = item.get("face_index")
            conn.execute(
                """
                INSERT INTO project_gallery_items (
                    card_id, scryfall_id, face_index, face_name, card_name,
                    set_code, collector_number, face_label, model, dpi, native_scale,
                    device, image_filename, out_path, original_path, png_url
                ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
                """,
                (
                    card_id,
                    item.get("scryfall_id") or "",
                    face_index,
                    item.get("face_name"),
                    item.get("card_name"),
                    item.get("set_code"),
                    item.get("collector_number"),
                    item.get("face_label"),
                    item.get("model") or settings.model,
                    int(item.get("dpi") or settings.dpi),
                    int(item.get("native_scale") or 4),
                    str(item.get("device") or "unknown"),
                    image_filename,
                    out_path,
                    str(item.get("original_path") or ""),
                    str(item.get("png_url") or ""),
                ),
            )

        conn.commit()
        return project_id


def load_project(
    project_id: int,
    db_path: Path | str | None = None,
) -> LoadedProject:
    with connect(db_path) as conn:
        row = conn.execute(
            "SELECT * FROM projects WHERE id = ?", (project_id,)
        ).fetchone()
        if row is None:
            raise ValueError(f"Project id {project_id} not found")

        gallery_rows = conn.execute(
            """
            SELECT g.*
            FROM project_gallery_items g
            JOIN project_cards c ON c.id = g.card_id
            WHERE c.project_id = ?
            ORDER BY c.sort_order ASC, g.dpi ASC, g.face_index ASC
            """,
            (project_id,),
        ).fetchall()

    gallery: list[dict[str, Any]] = []
    for g in gallery_rows:
        gallery.append(
            {
                "out_path": g["out_path"],
                "original_path": g["original_path"],
                "scryfall_id": g["scryfall_id"],
                "face_index": g["face_index"],
                "face_name": g["face_name"] or "",
                "card_name": g["card_name"] or "",
                "set_code": g["set_code"] or "",
                "collector_number": g["collector_number"] or "",
                "png_url": g["png_url"] or "",
                "dpi": int(g["dpi"]),
                "model": g["model"],
                "face_label": g["face_label"],
                "native_scale": int(g["native_scale"] or 4),
                "device": g["device"] if "device" in g.keys() else "unknown",
                "image_filename": g["image_filename"],
            }
        )

    settings = ProjectSettings.from_row(row)
    import_text = row["import_decklist_text"] or ""
    if not gallery and settings.output_dir:
        gallery = scan_gallery_from_output(
            settings.output_dir,
            parse_decklist_text(import_text),
            cache_dir=settings.cache_dir or None,
        )

    return LoadedProject(
        id=int(row["id"]),
        name=row["name"],
        import_decklist_text=import_text,
        settings=settings,
        gallery=gallery,
        created_at=row["created_at"],
        updated_at=row["updated_at"],
    )
