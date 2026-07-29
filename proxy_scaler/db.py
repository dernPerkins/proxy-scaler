"""SQLite persistence for proxy-scaler projects."""

from __future__ import annotations

import fcntl
import os
import re
import sqlite3
import subprocess
import sys
from dataclasses import dataclass, field
from datetime import datetime, timezone
from pathlib import Path
from typing import TYPE_CHECKING, Any

from .decklist import DeckEntry, parse_decklist_text
from .dpi import DEFAULT_DPI
from .upscale import UpscaleModel

if TYPE_CHECKING:
    from .pipeline import FaceResult

DEFAULT_DB_PATH = Path(__file__).resolve().parents[1] / "data" / "proxy_scaler.db"
WORKER_LOCK_FILE = Path(__file__).resolve().parents[1] / "data" / "worker.lock"
WORKER_LOG_FILE = Path(__file__).resolve().parents[1] / "data" / "worker.log"

# Abandoned_Air_Temple-TLA-263-swinir-600dpi.png
# Name-SET-COLLECTOR-front-swinir-800dpi.png  (collector may contain hyphens)
# Parsed from the right so numeric collectors are not mistaken for set codes.
_OUTPUT_SUFFIX_RE = re.compile(
    r"^(?P<head>.+?)"
    r"(?:-(?P<face>front|back))?"
    # realesrgan_anime listed before realesrgan since it's a prefix of it.
    r"-(?P<model>swinir|realesrnet|realesrgan_anime|realesrgan"
    r"|illustrationjanai|ultrasharp_v2|hat)"
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
    dpi_targets TEXT NOT NULL DEFAULT '800',
    page_size INTEGER NOT NULL DEFAULT 6,
    skip_existing INTEGER NOT NULL DEFAULT 1,
    output_dir TEXT NOT NULL DEFAULT '',
    cache_dir TEXT NOT NULL DEFAULT '',
    weights_dir TEXT NOT NULL DEFAULT '',
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

CREATE TABLE IF NOT EXISTS generation_tasks (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    project_id INTEGER REFERENCES projects(id) ON DELETE CASCADE,
    status TEXT NOT NULL DEFAULT 'pending',
    scryfall_id TEXT NOT NULL,
    face_index INTEGER,
    face_label TEXT,
    face_name TEXT NOT NULL,
    card_name TEXT NOT NULL,
    set_code TEXT NOT NULL,
    collector_number TEXT NOT NULL,
    png_url TEXT NOT NULL,
    dpi INTEGER NOT NULL,
    model TEXT NOT NULL,
    tile_size INTEGER NOT NULL DEFAULT 0,
    output_dir TEXT NOT NULL,
    cache_dir TEXT NOT NULL,
    weights_dir TEXT NOT NULL,
    error TEXT,
    created_at TEXT NOT NULL,
    started_at TEXT,
    completed_at TEXT
);

CREATE INDEX IF NOT EXISTS idx_tasks_status
    ON generation_tasks(status, created_at);
CREATE INDEX IF NOT EXISTS idx_tasks_project
    ON generation_tasks(project_id);
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
    dpi_targets: list[int] = field(default_factory=lambda: [DEFAULT_DPI])
    page_size: int = 6
    skip_existing: bool = True
    output_dir: str = ""
    cache_dir: str = ""
    weights_dir: str = ""
    tile_size: int = 0

    def to_row(self) -> dict[str, Any]:
        targets = sorted(set(self.dpi_targets)) or [DEFAULT_DPI]
        return {
            "model": self.model,
            "dpi_targets": ",".join(str(d) for d in targets),
            "page_size": int(self.page_size),
            "skip_existing": 1 if self.skip_existing else 0,
            "output_dir": self.output_dir,
            "cache_dir": self.cache_dir,
            "weights_dir": self.weights_dir,
            "tile_size": int(self.tile_size),
        }

    @classmethod
    def from_row(cls, row: sqlite3.Row | dict[str, Any]) -> ProjectSettings:
        get = row.__getitem__
        raw_targets = str(get("dpi_targets") or "").strip()
        if raw_targets:
            targets = sorted(
                {int(t) for t in raw_targets.split(",") if t.strip().isdigit()}
            )
        else:
            targets = []
        return cls(
            model=str(get("model") or UpscaleModel.SWINIR.value),
            dpi_targets=targets or [DEFAULT_DPI],
            page_size=int(get("page_size") or 6),
            skip_existing=bool(get("skip_existing")),
            output_dir=str(get("output_dir") or ""),
            cache_dir=str(get("cache_dir") or ""),
            weights_dir=str(get("weights_dir") or ""),
            tile_size=int(get("tile_size") or 0),
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


@dataclass
class TaskRow:
    """One row of generation_tasks — a single (face, dpi, model) unit of
    background generation work. Carries fully-resolved Scryfall data (no
    Scryfall call needed by the worker) since resolution already happened,
    fast, at enqueue time."""

    id: int
    project_id: int | None
    status: str  # "pending" | "running" | "done" | "failed" | "canceled"
    scryfall_id: str
    face_index: int | None
    face_label: str | None
    face_name: str
    card_name: str
    set_code: str
    collector_number: str
    png_url: str
    dpi: int
    model: str
    tile_size: int
    output_dir: str
    cache_dir: str
    weights_dir: str
    error: str | None
    created_at: str
    started_at: str | None
    completed_at: str | None

    @classmethod
    def from_row(cls, row: sqlite3.Row) -> TaskRow:
        return cls(
            id=int(row["id"]),
            project_id=row["project_id"],
            status=row["status"],
            scryfall_id=row["scryfall_id"],
            face_index=row["face_index"],
            face_label=row["face_label"],
            face_name=row["face_name"],
            card_name=row["card_name"],
            set_code=row["set_code"],
            collector_number=row["collector_number"],
            png_url=row["png_url"],
            dpi=int(row["dpi"]),
            model=row["model"],
            tile_size=int(row["tile_size"]),
            output_dir=row["output_dir"],
            cache_dir=row["cache_dir"],
            weights_dir=row["weights_dir"],
            error=row["error"],
            created_at=row["created_at"],
            started_at=row["started_at"],
            completed_at=row["completed_at"],
        )


def connect(db_path: Path | str | None = None) -> sqlite3.Connection:
    path = Path(db_path) if db_path else DEFAULT_DB_PATH
    path.parent.mkdir(parents=True, exist_ok=True)
    conn = sqlite3.connect(str(path))
    conn.row_factory = sqlite3.Row
    conn.execute("PRAGMA foreign_keys = ON")
    # WAL allows the Streamlit process and the background worker process to
    # both hit this file regularly without "database is locked" errors —
    # the default rollback-journal mode serializes writers too coarsely for
    # two independent processes polling/writing on their own schedules.
    conn.execute("PRAGMA journal_mode = WAL")
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
    gallery_cols = {
        row[1]
        for row in conn.execute("PRAGMA table_info(project_gallery_items)").fetchall()
    }
    if "device" not in gallery_cols:
        conn.execute(
            "ALTER TABLE project_gallery_items "
            "ADD COLUMN device TEXT NOT NULL DEFAULT 'unknown'"
        )

    project_cols = {
        row[1] for row in conn.execute("PRAGMA table_info(projects)").fetchall()
    }
    if "dpi_targets" not in project_cols:
        conn.execute(
            "ALTER TABLE projects ADD COLUMN dpi_targets TEXT NOT NULL DEFAULT '800'"
        )
    if "tile_size" not in project_cols:
        conn.execute(
            "ALTER TABLE projects ADD COLUMN tile_size INTEGER NOT NULL DEFAULT 0"
        )


def enqueue_task(
    project_id: int | None,
    *,
    scryfall_id: str,
    face_index: int | None,
    face_label: str | None,
    face_name: str,
    card_name: str,
    set_code: str,
    collector_number: str,
    png_url: str,
    dpi: int,
    model: str,
    output_dir: str,
    cache_dir: str,
    weights_dir: str,
    tile_size: int = 0,
    db_path: Path | str | None = None,
) -> int:
    """Add one (face, dpi, model) unit of generation work to the queue,
    picked up by the background worker (see worker.py)."""
    now = _utc_now()
    with connect(db_path) as conn:
        cur = conn.execute(
            """
            INSERT INTO generation_tasks (
                project_id, status, scryfall_id, face_index, face_label,
                face_name, card_name, set_code, collector_number, png_url,
                dpi, model, tile_size, output_dir, cache_dir, weights_dir,
                created_at
            ) VALUES (?, 'pending', ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
            """,
            (
                project_id,
                scryfall_id,
                face_index,
                face_label,
                face_name,
                card_name,
                set_code,
                collector_number,
                png_url,
                dpi,
                model,
                tile_size,
                output_dir,
                cache_dir,
                weights_dir,
                now,
            ),
        )
        conn.commit()
        return int(cur.lastrowid)


def claim_next_task(db_path: Path | str | None = None) -> TaskRow | None:
    """Atomically pick the oldest pending task and mark it running. One
    worker process ever calls this, so contention isn't a real concern,
    but the claim itself is still a proper conditional UPDATE (not a
    plain SELECT then UPDATE) so it stays correct if that ever changes."""
    with connect(db_path) as conn:
        row = conn.execute(
            "SELECT id FROM generation_tasks WHERE status = 'pending' "
            "ORDER BY created_at ASC, id ASC LIMIT 1"
        ).fetchone()
        if row is None:
            return None
        task_id = int(row["id"])
        cur = conn.execute(
            "UPDATE generation_tasks SET status = 'running', started_at = ? "
            "WHERE id = ? AND status = 'pending'",
            (_utc_now(), task_id),
        )
        if cur.rowcount == 0:
            conn.commit()
            return None
        updated = conn.execute(
            "SELECT * FROM generation_tasks WHERE id = ?", (task_id,)
        ).fetchone()
        conn.commit()
    return TaskRow.from_row(updated)


def mark_task_done(task_id: int, db_path: Path | str | None = None) -> None:
    with connect(db_path) as conn:
        conn.execute(
            "UPDATE generation_tasks SET status = 'done', completed_at = ? WHERE id = ?",
            (_utc_now(), task_id),
        )
        conn.commit()


def mark_task_failed(
    task_id: int, error: str, db_path: Path | str | None = None
) -> None:
    with connect(db_path) as conn:
        conn.execute(
            "UPDATE generation_tasks SET status = 'failed', error = ?, "
            "completed_at = ? WHERE id = ?",
            (error[:2000], _utc_now(), task_id),
        )
        conn.commit()


def cancel_task(task_id: int, db_path: Path | str | None = None) -> bool:
    """Cancel a task if it's still pending. Returns False (no-op) if it's
    already running/done/failed/canceled — cancellation only ever applies
    to work that hasn't started."""
    with connect(db_path) as conn:
        cur = conn.execute(
            "UPDATE generation_tasks SET status = 'canceled', completed_at = ? "
            "WHERE id = ? AND status = 'pending'",
            (_utc_now(), task_id),
        )
        conn.commit()
        return cur.rowcount > 0


def list_tasks(
    project_id: int | None = None,
    statuses: list[str] | None = None,
    db_path: Path | str | None = None,
) -> list[TaskRow]:
    query = "SELECT * FROM generation_tasks"
    clauses: list[str] = []
    params: list[Any] = []
    if project_id is not None:
        clauses.append("project_id = ?")
        params.append(project_id)
    if statuses:
        placeholders = ",".join("?" for _ in statuses)
        clauses.append(f"status IN ({placeholders})")
        params.extend(statuses)
    if clauses:
        query += " WHERE " + " AND ".join(clauses)
    query += " ORDER BY created_at DESC, id DESC"
    with connect(db_path) as conn:
        rows = conn.execute(query, params).fetchall()
    return [TaskRow.from_row(r) for r in rows]


def get_task(task_id: int, db_path: Path | str | None = None) -> TaskRow | None:
    with connect(db_path) as conn:
        row = conn.execute(
            "SELECT * FROM generation_tasks WHERE id = ?", (task_id,)
        ).fetchone()
    return TaskRow.from_row(row) if row is not None else None


def list_gallery_items_for_project(
    project_id: int, db_path: Path | str | None = None
) -> list[dict[str, Any]]:
    """Lightweight read of a project's DB-persisted gallery rows (no disk
    scan, unlike load_project()) — cheap enough to call on every Decklist
    tab rerun to pick up results the background worker wrote directly via
    upsert_gallery_item_for_task(), without an explicit Save/reload."""
    with connect(db_path) as conn:
        rows = conn.execute(
            """
            SELECT g.* FROM project_gallery_items g
            JOIN project_cards c ON c.id = g.card_id
            WHERE c.project_id = ?
            ORDER BY c.sort_order ASC, g.dpi ASC, g.face_index ASC
            """,
            (project_id,),
        ).fetchall()
    return [
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
        for g in rows
    ]


def upsert_gallery_item_for_task(
    task: TaskRow, result: FaceResult, db_path: Path | str | None = None
) -> None:
    """Persist one completed task's result straight into
    project_gallery_items — how a background-worker-produced image makes
    it into a project's gallery without an explicit Save. A no-op if the
    task has no project (nothing to attach it to) or the project's cards
    no longer contain a match (e.g. the project was deleted mid-task)."""
    if task.project_id is None:
        return
    with connect(db_path) as conn:
        card_rows = conn.execute(
            "SELECT id, set_code, collector_number, card_name FROM project_cards "
            "WHERE project_id = ?",
            (task.project_id,),
        ).fetchall()
        cards = [
            (
                int(r["id"]),
                DeckEntry(
                    quantity=1,
                    name=r["card_name"] or "",
                    set_code=r["set_code"],
                    collector_number=r["collector_number"],
                ),
            )
            for r in card_rows
        ]
        card_id = _match_card_id(
            cards,
            {
                "set_code": task.set_code,
                "collector_number": task.collector_number,
                "card_name": task.card_name,
                "face_name": task.face_name,
            },
        )
        if card_id is None:
            return
        # Not a plain `ON CONFLICT` upsert: the UNIQUE constraint includes
        # face_index, which is NULL for the common single-faced-card case,
        # and SQLite treats every NULL as distinct in a UNIQUE index — so
        # ON CONFLICT would never fire and repeated regenerations of the
        # same card/dpi/model would just keep inserting duplicate rows.
        # `IS ?` (unlike `= ?`) matches NULL-to-NULL correctly.
        existing = conn.execute(
            "SELECT id FROM project_gallery_items WHERE card_id = ? "
            "AND scryfall_id = ? AND face_index IS ? AND model = ? AND dpi = ?",
            (card_id, result.scryfall_id, result.face_index, result.model, result.dpi),
        ).fetchone()
        values = (
            result.face_name,
            result.card_name,
            result.set_code,
            result.collector_number,
            result.face_label,
            result.native_scale,
            result.device,
            result.out_path.name,
            str(result.out_path),
            str(result.original_path),
            result.png_url,
        )
        if existing is not None:
            conn.execute(
                """
                UPDATE project_gallery_items SET
                    face_name = ?, card_name = ?, set_code = ?, collector_number = ?,
                    face_label = ?, native_scale = ?, device = ?, image_filename = ?,
                    out_path = ?, original_path = ?, png_url = ?
                WHERE id = ?
                """,
                (*values, int(existing["id"])),
            )
        else:
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
                    result.scryfall_id,
                    result.face_index,
                    result.face_name,
                    result.card_name,
                    result.set_code,
                    result.collector_number,
                    result.face_label,
                    result.model,
                    result.dpi,
                    result.native_scale,
                    result.device,
                    result.out_path.name,
                    str(result.out_path),
                    str(result.original_path),
                    result.png_url,
                ),
            )
        conn.commit()


def acquire_worker_lock(lock_path: Path | str | None = None) -> int | None:
    """Try to exclusively lock the worker lock file. Returns an open fd
    holding the lock on success (the caller — the worker process — should
    just keep it open for its entire lifetime; the OS releases it
    automatically on exit/crash), or None if another process already
    holds it."""
    path = Path(lock_path) if lock_path else WORKER_LOCK_FILE
    path.parent.mkdir(parents=True, exist_ok=True)
    fd = os.open(str(path), os.O_CREAT | os.O_RDWR)
    try:
        fcntl.flock(fd, fcntl.LOCK_EX | fcntl.LOCK_NB)
    except OSError:
        os.close(fd)
        return None
    return fd


def release_worker_lock(fd: int) -> None:
    fcntl.flock(fd, fcntl.LOCK_UN)
    os.close(fd)


def is_worker_running(lock_path: Path | str | None = None) -> bool:
    fd = acquire_worker_lock(lock_path)
    if fd is None:
        return True
    release_worker_lock(fd)
    return False


def ensure_worker_running(
    lock_path: Path | str | None = None,
    log_path: Path | str | None = None,
) -> None:
    """Spawn the background generation worker (proxy_scaler.worker) if one
    isn't already running. Safe to call on every Streamlit rerun — the
    common case (a worker already holds the lock) is a cheap no-op, so
    there's no need to track "have I already tried this" separately."""
    if is_worker_running(lock_path):
        return
    log_file_path = Path(log_path) if log_path else WORKER_LOG_FILE
    log_file_path.parent.mkdir(parents=True, exist_ok=True)
    with open(log_file_path, "a") as log_file:
        subprocess.Popen(
            # -u: unbuffered stdout/stderr, so worker.log shows progress
            # live instead of only flushing when the buffer fills or the
            # process exits — otherwise "tail -f" on it looks dead for a
            # long time even while the worker is actively processing.
            [sys.executable, "-u", "-m", "proxy_scaler.worker"],
            cwd=str(Path(__file__).resolve().parents[1]),
            stdout=log_file,
            stderr=subprocess.STDOUT,
            start_new_session=True,
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


def delete_all_projects(db_path: Path | str | None = None) -> None:
    """Delete every project; cards/gallery rows cascade via foreign keys."""
    with connect(db_path) as conn:
        conn.execute("DELETE FROM projects")
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

def _gallery_item_key(item: dict[str, Any]) -> tuple:
    """Identity for de-duplicating gallery item dicts by physical variant
    (printing + face + model + dpi) — independent of scryfall_id/paths, so
    a DB-loaded entry and a disk-scanned entry for the same file compare
    equal."""
    return (
        (item.get("set_code") or "").lower(),
        str(item.get("collector_number") or ""),
        item.get("face_index"),
        item.get("face_label"),
        item.get("model"),
        int(item.get("dpi") or 0),
    )


def _face_identity(item: dict[str, Any]) -> tuple:
    """Identity for one physical card face, independent of DPI/model —
    matches group_by_face()'s (set/collector preferred over scryfall_id)
    grouping in pipeline.py, so a donor found here really is the same face."""
    return (
        (item.get("set_code") or "").lower(),
        str(item.get("collector_number") or ""),
        item.get("face_index"),
        item.get("face_label"),
    )


def _merge_disk_gallery(
    gallery: list[dict[str, Any]],
    entries: list[DeckEntry],
    output_dir: str | None,
    cache_dir: str | None,
) -> list[dict[str, Any]]:
    """Add any on-disk output PNGs not already represented in `gallery`.

    A project's saved gallery can lag behind what's actually on disk — e.g.
    an interrupted generate run, or files written in an earlier/different
    session before this project was last saved — leaving real output files
    that this project's own record doesn't know about. Scanning disk on
    every load/save (not just when the gallery is completely empty, as
    before) keeps the gallery in sync with reality regardless of how it got
    out of date, without needing a heavier "generated images are shared
    across projects" redesign.

    A disk-scanned item can't know its scryfall_id/original_path/png_url —
    a filename alone doesn't encode them — so it's backfilled here from any
    other already-known variant of the same physical face (e.g. a DB-loaded
    entry at a different DPI): they share the same original card image, so
    reusing those fields is exactly correct, not a guess. Without this, a
    disk-recovered variant that happens to render as a face's "first"
    (lowest DPI) item shows its Original column as missing even though a
    sibling variant has a perfectly good original on disk.
    """
    if not output_dir:
        return gallery
    disk_gallery = scan_gallery_from_output(output_dir, entries, cache_dir=cache_dir)
    if not disk_gallery:
        return gallery

    donors: dict[tuple, dict[str, str]] = {}
    for item in gallery:
        if item.get("scryfall_id") or item.get("original_path") or item.get("png_url"):
            donors.setdefault(
                _face_identity(item),
                {
                    "scryfall_id": item.get("scryfall_id") or "",
                    "original_path": item.get("original_path") or "",
                    "png_url": item.get("png_url") or "",
                },
            )

    known = {_gallery_item_key(g) for g in gallery}
    merged = list(gallery)
    for item in disk_gallery:
        item_key = _gallery_item_key(item)
        if item_key not in known:
            donor = donors.get(_face_identity(item))
            if donor:
                item = {**item, **{k: v for k, v in donor.items() if v}}
            merged.append(item)
            known.add(item_key)
    return merged


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

    # Fold in any on-disk output PNGs this project's own gallery doesn't
    # already know about (e.g. saved before generate, after a refresh, an
    # interrupted run, or files from an earlier session) — see
    # _merge_disk_gallery for why this isn't just an "empty gallery" check.
    gallery = _merge_disk_gallery(
        gallery, entries, settings.output_dir, settings.cache_dir or None
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
                    name, import_decklist_text, model, dpi_targets,
                    page_size, skip_existing, output_dir, cache_dir, weights_dir,
                    tile_size, created_at, updated_at
                ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
                """,
                (
                    name,
                    import_decklist_text,
                    s["model"],
                    s["dpi_targets"],
                    s["page_size"],
                    s["skip_existing"],
                    s["output_dir"],
                    s["cache_dir"],
                    s["weights_dir"],
                    s["tile_size"],
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
                    model = ?, dpi_targets = ?,
                    page_size = ?, skip_existing = ?,
                    output_dir = ?, cache_dir = ?, weights_dir = ?,
                    tile_size = ?,
                    updated_at = ?
                WHERE id = ?
                """,
                (
                    name,
                    import_decklist_text,
                    s["model"],
                    s["dpi_targets"],
                    s["page_size"],
                    s["skip_existing"],
                    s["output_dir"],
                    s["cache_dir"],
                    s["weights_dir"],
                    s["tile_size"],
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
    gallery = _merge_disk_gallery(
        gallery,
        parse_decklist_text(import_text),
        settings.output_dir,
        settings.cache_dir or None,
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
