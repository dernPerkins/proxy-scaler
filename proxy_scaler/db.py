"""SQLite persistence for the generation server: the task queue and the
gallery of completed images. Project management (decklist text, project
CRUD, settings) lives in the desktop app itself now, not here — see
ARCHITECTURE.md.

Schema changes are applied as ordered, non-destructive migrations tracked
via PRAGMA user_version — see _MIGRATIONS and _migrate() below. Call
init_db() before using a database; supervisor.main() already does this on
every server start, so this only matters for code that talks to the
database without going through the supervisor (see connect()'s
SchemaVersionMismatch)."""

from __future__ import annotations

import os
import re
import sqlite3
import sys
from dataclasses import dataclass
from datetime import datetime, timezone
from pathlib import Path
from typing import TYPE_CHECKING, Any, Callable, Sequence

from .decklist import DeckEntry

if TYPE_CHECKING:
    from .pipeline import FaceResult

# fcntl/msvcrt are both stdlib but mutually platform-exclusive (no fcntl on
# Windows, no msvcrt elsewhere) — see acquire_worker_lock/release_worker_lock
# below for the actual locking, gated on IS_WINDOWS the same way
# supervisor.py already gates its own platform-specific process handling.
IS_WINDOWS = sys.platform == "win32"
if IS_WINDOWS:
    import msvcrt
else:
    import fcntl

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
#
# This is only ever the *current, latest* shape — used directly for a
# brand new database, and re-applied defensively (CREATE TABLE/INDEX IF
# NOT EXISTS, a no-op once already current) after migrations run on an
# existing one. It is NOT where upgrade logic for an existing database
# lives — that's _MIGRATIONS/_migrate() below.
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
    completed_at TEXT,
    total_faces INTEGER
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
    -- Set on every insert AND refreshed on re-generation (see
    -- upsert_gallery_item): "when was this image last produced", which is
    -- what the PDF tab's most-recent-wins variant pick means by recency.
    created_at TEXT,
    -- How many physical faces this card has (see scryfall.CardFaceImage) —
    -- lets pdf_layout.match_quantities notice a DFC missing its other face
    -- without a live Scryfall call. NULL for rows predating migration 003.
    total_faces INTEGER,
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
    total_faces: int | None

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
            total_faces=row["total_faces"],
        )


def _raw_connect(path: Path) -> sqlite3.Connection:
    """Opens a connection with no schema-version assumptions. Used only by
    init_db()/the migration runner, which must be able to operate on a
    database that isn't at SCHEMA_VERSION yet. Everything else should use
    connect(), which adds the version guard below."""
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


class SchemaVersionMismatch(RuntimeError):
    """Raised by connect() when a database's PRAGMA user_version doesn't
    match SCHEMA_VERSION — i.e. something opened it without ever calling
    init_db() first. supervisor.main() already calls init_db() once,
    before spawning the API/worker children, on every server start — so
    this should only ever fire for a caller that bypasses the supervisor
    entirely."""


def connect(db_path: Path | str | None = None) -> sqlite3.Connection:
    path = Path(db_path) if db_path else DEFAULT_DB_PATH
    conn = _raw_connect(path)
    version = conn.execute("PRAGMA user_version").fetchone()[0]
    if version != SCHEMA_VERSION:
        conn.close()
        raise SchemaVersionMismatch(
            f"{path} is at schema version {version}, expected "
            f"{SCHEMA_VERSION}. Call proxy_scaler.db.init_db() first — "
            "supervisor.main() already does this on every server start."
        )
    return conn


def init_db(db_path: Path | str | None = None) -> Path:
    path = Path(db_path) if db_path else DEFAULT_DB_PATH
    # _raw_connect, not connect(): this is the one place allowed to open a
    # database that isn't at SCHEMA_VERSION yet — it's the code that fixes
    # that, not a caller that should be turned away by the version guard.
    conn = _raw_connect(path)
    try:
        _migrate(conn)
    finally:
        conn.close()
    return path


@dataclass(frozen=True)
class Migration:
    """One schema step. `apply` runs inside a transaction the runner in
    _migrate() has already opened (BEGIN IMMEDIATE) — use conn.execute()
    per statement inside it, never conn.executescript() (which issues an
    implicit COMMIT first and would break the atomicity of "schema change
    + PRAGMA user_version bump" the runner depends on). The common case is
    a plain `conn.execute("ALTER TABLE ... ADD COLUMN ...")`; for anything
    ALTER TABLE can't express (dropping/retyping a column, adding a NOT
    NULL column with no usable default), use _rebuild_table() below."""

    version: int
    description: str
    apply: Callable[[sqlite3.Connection], None]


def _rebuild_table(
    conn: sqlite3.Connection,
    table: str,
    create_sql: str,
    copy_columns: Sequence[str],
) -> None:
    """Standard SQLite "new shape, copy, drop, rename" pattern, for schema
    changes plain ALTER TABLE ADD COLUMN can't express. Must be called
    inside an already-open migration transaction (see Migration.apply).
    Caller is responsible for re-creating any indexes on `table`
    afterward — RENAME doesn't carry them across."""
    conn.execute(f"ALTER TABLE {table} RENAME TO {table}__old")
    conn.execute(create_sql)
    cols = ", ".join(copy_columns)
    conn.execute(f"INSERT INTO {table} ({cols}) SELECT {cols} FROM {table}__old")
    conn.execute(f"DROP TABLE {table}__old")


def _migration_001_drop_legacy_project_schema(conn: sqlite3.Connection) -> None:
    """The pre-split combined schema had projects/project_cards/
    app_settings tables (now owned by the desktop app itself, see
    ARCHITECTURE.md) and generation_tasks/project_gallery_items keyed by
    integer project_id/card_id foreign keys (now a plain project_tag
    string, no FK at all). Detects either shape and drops it rather than
    migrating it forward: project_tag is an opaque client-minted string
    with no relationship to the old integer ids, so there's no real data
    to carry across — generation_tasks rows are just re-enqueuable work,
    and project_gallery_items rows just index PNGs still on disk
    (recoverable via scan_gallery_from_output()). A no-op against a
    database that's already past this shape — e.g. one that already went
    through this exact drop under the old, unversioned _migrate() this
    file used to have, before it tracked PRAGMA user_version at all —
    since every branch below is gated on still finding the old shape."""
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


def _migration_002_add_gallery_created_at(conn: sqlite3.Connection) -> None:
    """Add project_gallery_items.created_at, used by the PDF tab to break
    ties toward the most recently produced image at a given DPI.

    Nullable with no backfill on purpose: rows written before this column
    existed have no honest timestamp available (the table never recorded
    one), and inventing `now` for all of them would make every pre-existing
    image look equally-and-most-recently generated. Selection treats a NULL
    as "older than anything timestamped" (see pdf_layout._pick_dpi_variant),
    so old rows lose ties to freshly regenerated ones — which is the
    intended reading. Guarded so re-running against an already-migrated
    database is a no-op rather than a duplicate-column error.

    Also a no-op when the table is absent: migration 001 drops a
    pre-reshape project_gallery_items outright, and _migrate()'s trailing
    _SCHEMA pass then recreates it already carrying this column, so there
    is nothing here to alter."""
    tables = {
        row[0]
        for row in conn.execute(
            "SELECT name FROM sqlite_master WHERE type='table'"
        ).fetchall()
    }
    if "project_gallery_items" not in tables:
        return
    cols = {
        row[1]
        for row in conn.execute("PRAGMA table_info(project_gallery_items)").fetchall()
    }
    if "created_at" not in cols:
        conn.execute("ALTER TABLE project_gallery_items ADD COLUMN created_at TEXT")


def _migration_003_add_total_faces(conn: sqlite3.Connection) -> None:
    """Add total_faces to generation_tasks and project_gallery_items — how
    many physical faces a card has (see scryfall.CardFaceImage), captured
    once at enqueue time (the only point all of a card's faces are seen
    together) and carried alongside the other denormalized identity fields
    already on these rows. Lets the PDF tab's DFC-completeness check (a
    face that was never generated at all leaves no row to notice is
    missing) read this straight off whichever face *did* generate, instead
    of the PDF tab needing its own live Scryfall call.

    Nullable with no backfill, same reasoning as migration 002's
    created_at: rows written before this column existed have no honest
    value to give it, and pdf_layout.match_quantities already treats None
    as "unknown, don't verify" rather than "single-faced". Guarded/no-op
    the same way too."""
    tables = {
        row[0]
        for row in conn.execute(
            "SELECT name FROM sqlite_master WHERE type='table'"
        ).fetchall()
    }
    if "generation_tasks" in tables:
        cols = {
            row[1]
            for row in conn.execute("PRAGMA table_info(generation_tasks)").fetchall()
        }
        if "total_faces" not in cols:
            conn.execute("ALTER TABLE generation_tasks ADD COLUMN total_faces INTEGER")
    if "project_gallery_items" in tables:
        cols = {
            row[1]
            for row in conn.execute(
                "PRAGMA table_info(project_gallery_items)"
            ).fetchall()
        }
        if "total_faces" not in cols:
            conn.execute(
                "ALTER TABLE project_gallery_items ADD COLUMN total_faces INTEGER"
            )


# Ordered oldest-to-newest. _migrate() below walks this list and applies
# whichever steps are newer than a database's current PRAGMA user_version,
# one at a time in order — so a database several versions behind replays
# every intervening step, not just a jump straight to the latest shape.
_MIGRATIONS: list[Migration] = [
    Migration(
        1,
        "drop pre-split projects/project_cards/app_settings and any "
        "generation_tasks/project_gallery_items rows shaped before the "
        "project_tag reshape",
        _migration_001_drop_legacy_project_schema,
    ),
    Migration(
        2,
        "add project_gallery_items.created_at for most-recent-wins PDF "
        "variant selection",
        _migration_002_add_gallery_created_at,
    ),
    Migration(
        3,
        "add total_faces to generation_tasks and project_gallery_items for "
        "DFC-completeness checks without a live Scryfall call",
        _migration_003_add_total_faces,
    ),
]
SCHEMA_VERSION = 3  # kept in sync with _MIGRATIONS[-1].version
assert _MIGRATIONS[-1].version == SCHEMA_VERSION

# Tables from every schema shape this database has ever had — legacy ones
# included — so _migrate()'s "is this a genuinely fresh database" check
# below can't mistake a partially-legacy file for a brand new one.
_KNOWN_TABLES = frozenset(
    {
        "generation_tasks",
        "project_gallery_items",
        "projects",
        "project_cards",
        "app_settings",
    }
)


def _migrate(conn: sqlite3.Connection) -> None:
    """Brings a database from whatever schema version it's currently at up
    to SCHEMA_VERSION, by running each not-yet-applied step in
    _MIGRATIONS in order — one at a time, each inside its own transaction
    (schema change + PRAGMA user_version bump committed together), so a
    crash mid-run leaves the database at a valid, fully-applied
    intermediate version rather than one where the schema changed but the
    version wasn't recorded. A genuinely fresh database (none of
    _KNOWN_TABLES exist yet) skips straight to _SCHEMA at the latest
    version instead of replaying history it never had. Safe and cheap to
    call on every process start (see init_db()) — a no-op once already
    current."""
    tables = {
        row[0]
        for row in conn.execute(
            "SELECT name FROM sqlite_master WHERE type='table'"
        ).fetchall()
    }

    if not (tables & _KNOWN_TABLES):
        conn.executescript(_SCHEMA)
        conn.execute(f"PRAGMA user_version = {SCHEMA_VERSION}")
        conn.commit()
        return

    current_version = conn.execute("PRAGMA user_version").fetchone()[0]
    # Autocommit mode so the explicit BEGIN IMMEDIATE/COMMIT/ROLLBACK below
    # doesn't fight sqlite3's own implicit transaction handling around DML
    # — the standard idiom for getting anything other than a plain
    # deferred BEGIN out of the sqlite3 module.
    conn.isolation_level = None
    for migration in _MIGRATIONS:
        if migration.version <= current_version:
            continue
        conn.execute("BEGIN IMMEDIATE")
        try:
            migration.apply(conn)
            conn.execute(f"PRAGMA user_version = {migration.version}")
        except Exception:
            conn.execute("ROLLBACK")
            raise
        else:
            conn.execute("COMMIT")

    # Defensive catch-all: pick up any IF NOT EXISTS table/index a
    # migration step didn't itself create. A no-op once already current.
    conn.executescript(_SCHEMA)
    conn.commit()


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
    total_faces: int | None = None,
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
                created_at, total_faces
            ) VALUES (?, 'pending', ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
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
                total_faces,
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


def cancel_all_tasks(db_path: Path | str | None = None) -> int:
    """Cancel every still-pending task, across all projects. Returns the
    number canceled."""
    with connect(db_path) as conn:
        cur = conn.execute(
            "UPDATE generation_tasks SET status = 'canceled', completed_at = ? "
            "WHERE status = 'pending'",
            (_utc_now(),),
        )
        conn.commit()
        return cur.rowcount


def cancel_pending_tasks_for_tag(
    project_tag: str, db_path: Path | str | None = None
) -> int:
    """Cancel every still-pending task belonging to one project_tag —
    the queue half of discarding a session (see the discard route in
    api/routers/misc.py). Returns the number canceled. Neither existing
    cancel fits: cancel_task is per-id and cancel_all_tasks is across all
    projects, which would cancel other Projects' queued work.

    Running tasks are deliberately left alone — an in-flight upscale can't
    be cancelled, so it finishes and writes its row for a tag that's
    already gone (the accepted residue in spec §8). A no-op for a falsy
    project_tag, matching every other project_tag-scoped write here."""
    if not project_tag:
        return 0
    with connect(db_path) as conn:
        cur = conn.execute(
            "UPDATE generation_tasks SET status = 'canceled', completed_at = ? "
            "WHERE project_tag = ? AND status = 'pending'",
            (_utc_now(), project_tag),
        )
        conn.commit()
        return cur.rowcount


def retry_task(task_id: int, db_path: Path | str | None = None) -> bool:
    """Re-queue a failed task in place. created_at is bumped to now so it
    joins the back of the pending queue (claim_next_task orders by
    created_at ASC) rather than jumping ahead on its original timestamp.
    Returns False (no-op) unless the task is currently failed."""
    with connect(db_path) as conn:
        cur = conn.execute(
            "UPDATE generation_tasks SET status = 'pending', error = NULL, "
            "started_at = NULL, completed_at = NULL, created_at = ? "
            "WHERE id = ? AND status = 'failed'",
            (_utc_now(), task_id),
        )
        conn.commit()
        return cur.rowcount > 0


def retry_all_failed(
    project_tag: str,
    model: str,
    dpis: list[int],
    db_path: Path | str | None = None,
) -> int:
    """Bulk retry_task, scoped to the failed tasks matching one project's
    model and set of target DPIs. Returns the number retried."""
    if not project_tag or not dpis:
        return 0
    placeholders = ",".join("?" for _ in dpis)
    with connect(db_path) as conn:
        cur = conn.execute(
            "UPDATE generation_tasks SET status = 'pending', error = NULL, "
            "started_at = NULL, completed_at = NULL, created_at = ? "
            f"WHERE project_tag = ? AND model = ? AND status = 'failed' "
            f"AND dpi IN ({placeholders})",
            (_utc_now(), project_tag, model, *dpis),
        )
        conn.commit()
        return cur.rowcount


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
        # Absent on rows written before migration 002; None is meaningful
        # (treated as oldest) rather than an error case.
        "created_at": g["created_at"] if "created_at" in g.keys() else None,
        # Absent on rows written before migration 003; None means "unknown,
        # don't verify DFC completeness" (see pdf_layout.match_quantities).
        "total_faces": g["total_faces"] if "total_faces" in g.keys() else None,
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


def clear_project_generation_records(
    project_tag: str, db_path: Path | str | None = None
) -> None:
    """Deletes this project_tag's finished generation_tasks (done/failed/
    canceled) and all of its project_gallery_items — the DB-side half of
    "delete all generated images & cache" (see pipeline.py::
    clear_generated_data for the on-disk half). pending/running tasks are
    left alone: they haven't written a file yet, so there's nothing about
    them for the delete to have invalidated.

    Both tables have to be touched, not just the gallery rows: a gallery
    row missing for a (scryfall_id, face_index, dpi, model) pair falls back
    to that pair's newest task's own status in the client's merge (see
    mergeCardStatus.ts::statusForPairs), and a *completed* task's status is
    literally "done" — so deleting only the gallery row would still leave
    the UI reporting "done" for images that no longer exist on disk, from
    task history alone. A no-op for a falsy project_tag, matching every
    other project_tag-scoped write in this module."""
    if not project_tag:
        return
    with connect(db_path) as conn:
        conn.execute(
            "DELETE FROM generation_tasks WHERE project_tag = ? "
            "AND status NOT IN ('pending', 'running')",
            (project_tag,),
        )
        conn.execute("DELETE FROM project_gallery_items WHERE project_tag = ?", (project_tag,))
        conn.commit()


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
            # Refreshed on update too, not just insert: an upsert here means
            # the image was just (re)generated, and "most recently produced"
            # is exactly what the PDF variant pick needs to see.
            _utc_now(),
            result.total_faces,
        )
        if existing is not None:
            conn.execute(
                """
                UPDATE project_gallery_items SET
                    face_name = ?, card_name = ?, set_code = ?, collector_number = ?,
                    face_label = ?, native_scale = ?, device = ?, image_filename = ?,
                    out_path = ?, original_path = ?, png_url = ?, created_at = ?,
                    total_faces = ?
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
                    device, image_filename, out_path, original_path, png_url,
                    created_at, total_faces
                ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
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
                    values[-2],  # same timestamp as the UPDATE branch above
                    values[-1],  # total_faces
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
    if IS_WINDOWS:
        # msvcrt.locking locks a byte range starting at the current file
        # position — needs at least one byte to exist to lock it, unlike
        # flock's whole-file lock which doesn't care about file size.
        if os.fstat(fd).st_size == 0:
            os.write(fd, b"\0")
        os.lseek(fd, 0, os.SEEK_SET)
        try:
            msvcrt.locking(fd, msvcrt.LK_NBLCK, 1)
        except OSError:
            os.close(fd)
            return None
        return fd
    try:
        fcntl.flock(fd, fcntl.LOCK_EX | fcntl.LOCK_NB)
    except OSError:
        os.close(fd)
        return None
    return fd


def release_worker_lock(fd: int) -> None:
    if IS_WINDOWS:
        os.lseek(fd, 0, os.SEEK_SET)
        msvcrt.locking(fd, msvcrt.LK_UNLCK, 1)
        os.close(fd)
        return
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


