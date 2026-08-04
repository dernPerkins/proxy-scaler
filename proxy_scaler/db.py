"""SQLite persistence for the generation server: the task queue and the
gallery of completed images. Project management (decklist text, project
CRUD, settings) lives in the desktop app itself now, not here — see
ARCHITECTURE.md."""

from __future__ import annotations

import fcntl
import os
import re
import sqlite3
import sys
from dataclasses import dataclass
from datetime import datetime, timezone
from pathlib import Path
from typing import TYPE_CHECKING, Any

from .decklist import DeckEntry

if TYPE_CHECKING:
    from .pipeline import FaceResult

_IS_FROZEN = bool(getattr(sys, "frozen", False))


DATA_DIR_ENV_VAR = "PROXY_SCALER_DATA_DIR"


def default_data_dir() -> Path:
    """Stable, OS-conventional per-user data directory. Used as the
    default DB/lock/log location below when frozen, and reused by
    supervisor.py as the frozen sidecar's working directory (so relative
    output/cache/weights paths land somewhere persistent too).

    $PROXY_SCALER_DATA_DIR overrides it outright. That's what a packaged
    server deployment uses — the .deb points it at /var/lib/proxy-scaler,
    since a system service has no business writing under the invoking
    user's home directory (and, for a plain non-editable `pip install`,
    the dev fallback below would otherwise resolve inside site-packages).

    Deliberately NOT derived from __file__/sys.executable: for a frozen
    PyInstaller onefile build those resolve inside a temp extraction
    directory that's wiped and recreated fresh on every single launch —
    a real shipped bug where saved projects (and, via supervisor.py's
    cwd, generated images left at their relative default paths) silently
    vanished on every app restart, even though everything worked fine
    within a single running session."""
    override = os.environ.get(DATA_DIR_ENV_VAR)
    if override:
        return Path(override).expanduser()
    if sys.platform == "darwin":
        return Path.home() / "Library" / "Application Support" / "proxy-scaler"
    if sys.platform == "win32":
        base = os.environ.get("APPDATA") or str(Path.home())
        return Path(base) / "proxy-scaler"
    base = os.environ.get("XDG_DATA_HOME") or str(Path.home() / ".local" / "share")
    return Path(base) / "proxy-scaler"


# An explicit data dir wins even when not frozen, so a headless install
# can be pointed somewhere sane without having to be a frozen build.
if _IS_FROZEN or os.environ.get(DATA_DIR_ENV_VAR):
    _DATA_ROOT = default_data_dir()
else:
    _DATA_ROOT = Path(__file__).resolve().parents[1] / "data"

DEFAULT_DB_PATH = _DATA_ROOT / "proxy_scaler.db"
WORKER_LOCK_FILE = _DATA_ROOT / "worker.lock"
WORKER_LOG_FILE = _DATA_ROOT / "worker.log"

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

# Project management (projects/project_cards/app_settings) moved out of
# this database entirely as of the client/generation split — see
# ARCHITECTURE.md. It's now owned by the desktop app itself (Rust,
# in-process, its own local SQLite file), always local regardless of
# which generation server is connected. This database only ever knows
# about generation work, scoped by an opaque `project_tag` string the
# client mints and includes on every request — not a foreign key to
# anything, since this process has no idea projects exist.
_SCHEMA = """
PRAGMA foreign_keys = ON;

CREATE TABLE IF NOT EXISTS generation_tasks (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    project_tag TEXT,
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
CREATE INDEX IF NOT EXISTS idx_tasks_project_tag
    ON generation_tasks(project_tag);

CREATE TABLE IF NOT EXISTS project_gallery_items (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    project_tag TEXT NOT NULL,
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
    UNIQUE (project_tag, scryfall_id, face_index, model, dpi)
);

CREATE INDEX IF NOT EXISTS idx_gallery_project_tag
    ON project_gallery_items(project_tag);
"""


def _utc_now() -> str:
    return datetime.now(timezone.utc).replace(microsecond=0).isoformat()


@dataclass
class TaskRow:
    """One row of generation_tasks — a single (face, dpi, model) unit of
    background generation work. Carries fully-resolved Scryfall data (no
    Scryfall call needed by the worker) since resolution already happened,
    fast, at enqueue time. project_tag is an opaque string the client
    mints per local project — purely for scoping (list/filter), not a
    foreign key to anything this process knows about; see
    ARCHITECTURE.md."""

    id: int
    project_tag: str | None
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
            project_tag=row["project_tag"],
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
    # WAL allows the API server and the background worker process to both
    # hit this file regularly without "database is locked" errors — the
    # default rollback-journal mode serializes writers too coarsely for two
    # independent processes polling/writing on their own schedules.
    conn.execute("PRAGMA journal_mode = WAL")
    return conn


def init_db(db_path: Path | str | None = None) -> Path:
    path = Path(db_path) if db_path else DEFAULT_DB_PATH
    with connect(path) as conn:
        # Drop stale/incompatible tables *before* creating the current
        # schema, not after — see _migrate's docstring.
        _migrate(conn)
        conn.executescript(_SCHEMA)
        conn.commit()
    return path


def _migrate(conn: sqlite3.Connection) -> None:
    """Deliberate clean break, not a data migration: the pre-split combined
    schema had projects/project_cards/app_settings tables (now owned by
    the desktop app itself, see ARCHITECTURE.md) and generation_tasks/
    project_gallery_items keyed by integer project_id/card_id foreign keys
    (now a plain project_tag string, no FK at all). Detects either shape
    and drops it so _SCHEMA's CREATE TABLE IF NOT EXISTS actually takes
    effect with the new columns, instead of silently no-op'ing against an
    old-shaped table left behind by a pre-split database file. A no-op on
    a database that never had the old shape (e.g. every test fixture)."""
    tables = {
        row[0]
        for row in conn.execute(
            "SELECT name FROM sqlite_master WHERE type='table'"
        ).fetchall()
    }

    if "projects" in tables:
        conn.execute("DROP TABLE IF EXISTS project_cards")
        conn.execute("DROP TABLE IF EXISTS projects")
        conn.execute("DROP TABLE IF EXISTS app_settings")

    if "generation_tasks" in tables:
        cols = {
            row[1]
            for row in conn.execute("PRAGMA table_info(generation_tasks)").fetchall()
        }
        if "project_tag" not in cols:
            conn.execute("DROP TABLE IF EXISTS generation_tasks")

    if "project_gallery_items" in tables:
        cols = {
            row[1]
            for row in conn.execute(
                "PRAGMA table_info(project_gallery_items)"
            ).fetchall()
        }
        if "project_tag" not in cols:
            conn.execute("DROP TABLE IF EXISTS project_gallery_items")


def enqueue_task(
    project_tag: str | None,
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
                project_tag, status, scryfall_id, face_index, face_label,
                face_name, card_name, set_code, collector_number, png_url,
                dpi, model, tile_size, output_dir, cache_dir, weights_dir,
                created_at
            ) VALUES (?, 'pending', ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
            """,
            (
                project_tag,
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
    project_tag: str | None = None,
    statuses: list[str] | None = None,
    db_path: Path | str | None = None,
) -> list[TaskRow]:
    query = "SELECT * FROM generation_tasks"
    clauses: list[str] = []
    params: list[Any] = []
    if project_tag is not None:
        clauses.append("project_tag = ?")
        params.append(project_tag)
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


def _gallery_row_to_dict(g: sqlite3.Row) -> dict[str, Any]:
    return {
        "id": int(g["id"]),
        "project_tag": g["project_tag"],
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


def list_gallery_items(
    project_tag: str, db_path: Path | str | None = None
) -> list[dict[str, Any]]:
    """A project's persisted gallery rows, scoped by project_tag — cheap
    enough to call on every Decklist tab poll to pick up results the
    background worker wrote directly via upsert_gallery_item_for_task()."""
    with connect(db_path) as conn:
        rows = conn.execute(
            """
            SELECT * FROM project_gallery_items
            WHERE project_tag = ?
            ORDER BY dpi ASC, face_index ASC
            """,
            (project_tag,),
        ).fetchall()
    return [_gallery_row_to_dict(g) for g in rows]


def get_gallery_item(
    gallery_item_id: int, db_path: Path | str | None = None
) -> dict[str, Any] | None:
    """Look up one gallery row by its own id — a plain global primary key,
    not scoped by project_tag, since (unlike listing, which is always
    "for this project") a single id is already unambiguous on its own."""
    with connect(db_path) as conn:
        row = conn.execute(
            "SELECT * FROM project_gallery_items WHERE id = ?", (gallery_item_id,)
        ).fetchone()
    return _gallery_row_to_dict(row) if row is not None else None


def upsert_gallery_item_for_task(
    task: TaskRow, result: FaceResult, db_path: Path | str | None = None
) -> None:
    """Persist one completed task's result straight into
    project_gallery_items — how a background-worker-produced image makes
    it into a project's gallery. A no-op if the task has no project_tag
    (nothing to scope it to).

    No project_cards lookup/matching needed any more: the task already
    carries project_tag directly (denormalized at enqueue time, like
    every other identity field on it), so this is a plain scoped upsert
    — a real simplification the client/generation split enabled, not
    just a rename. See ARCHITECTURE.md."""
    if not task.project_tag:
        return
    upsert_gallery_item(task.project_tag, result, db_path=db_path)


def upsert_gallery_item(
    project_tag: str, result: FaceResult, db_path: Path | str | None = None
) -> None:
    """Same upsert as upsert_gallery_item_for_task, for callers with no
    TaskRow to read project_tag off of — specifically
    services/generation.py's skip_existing path, which finds a face's
    output file already on disk (so no task ever runs for it under the
    current project_tag) and needs to register it into this project's
    gallery anyway, or it never shows up as "done" in the UI even though
    the image genuinely exists. A no-op if project_tag is falsy."""
    if not project_tag:
        return
    with connect(db_path) as conn:
        # Not a plain `ON CONFLICT` upsert: the UNIQUE constraint includes
        # face_index, which is NULL for the common single-faced-card case,
        # and SQLite treats every NULL as distinct in a UNIQUE index — so
        # ON CONFLICT would never fire and repeated regenerations of the
        # same card/dpi/model would just keep inserting duplicate rows.
        # `IS ?` (unlike `= ?`) matches NULL-to-NULL correctly.
        existing = conn.execute(
            "SELECT id FROM project_gallery_items WHERE project_tag = ? "
            "AND scryfall_id = ? AND face_index IS ? AND model = ? AND dpi = ?",
            (
                project_tag,
                result.scryfall_id,
                result.face_index,
                result.model,
                result.dpi,
            ),
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
                    project_tag, scryfall_id, face_index, face_name, card_name,
                    set_code, collector_number, face_label, model, dpi, native_scale,
                    device, image_filename, out_path, original_path, png_url
                ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
                """,
                (
                    project_tag,
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


