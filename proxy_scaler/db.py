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

from .customs import identity_key
from .dpi import CUSTOM_SOURCE_MODEL
from .decklist import DeckEntry
from .scryfall import SCRYFALL_LANGUAGES
from .upscale import UpscaleModel

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

# Abandoned_Air_Temple-TLA-263-ultrasharp_v2-600dpi.png  (legacy, no id)
# Name-SET-COLLECTOR-front-ultrasharp_v2-800dpi.png  (collector may contain hyphens)
# Name-SET-COLLECTOR-ja-front-ultrasharp_v2-800dpi.png  (non-English printing)
# Name-SET-COLLECTOR-ultrasharp_v2-800dpi-<scryfall uuid>.png  (current format)
# Parsed from the right so numeric collectors are not mistaken for set codes.
# Model slugs come straight from the enum so the alternation can't drift;
# longest-first guards against any future slug being a prefix of another.
_MODEL_SLUGS = "|".join(
    re.escape(m.value) for m in sorted(UpscaleModel, key=lambda m: -len(m.value))
)
# Scryfall's printed-language codes, as an explicit closed alternation (not
# a generic [a-z]{2,3}) so a hyphen-containing collector number can never
# be misread as a language. English is never written into filenames (see
# pipeline.output_filename), so an absent group reads back as "en" — which
# also keeps every pre-language filename parsing exactly as before.
_LANG_CODES = "|".join(lang for lang in SCRYFALL_LANGUAGES if lang != "en")
# The trailing scryfall_id (a Scryfall UUID, appended by output_filename
# since the generated_images registry) is optional: files produced before
# it existed keep their names forever — the server never renames on disk,
# since it only ever learns output_dir per request — so both formats must
# parse for as long as legacy files can exist.
_SCRYFALL_UUID = (
    r"[0-9a-fA-F]{8}-[0-9a-fA-F]{4}-[0-9a-fA-F]{4}"
    r"-[0-9a-fA-F]{4}-[0-9a-fA-F]{12}"
)
# A Custom Image's output file — Name-custom-<sha256>[-model-dpi].png. A
# separate pattern rather than another optional group on the Scryfall one:
# it has no SET-COLLECTOR segment at all, so the two heads parse by
# genuinely different rules and interleaving them would make both harder to
# read. Tried first, since a card literally named "custom" is possible and
# the hash makes this shape unambiguous.
_CUSTOM_OUTPUT_RE = re.compile(
    r"^(?P<stem>.+?)"
    r"-custom-(?P<custom_hash>[0-9a-f]{64})"
    rf"(?:-(?P<model>{_MODEL_SLUGS})"
    r"-(?P<dpi>\d+)dpi)?"
    r"\.png$",
    re.IGNORECASE,
)

_OUTPUT_SUFFIX_RE = re.compile(
    r"^(?P<head>.+?)"
    rf"(?:-(?P<lang>{_LANG_CODES}))?"
    r"(?:-(?P<face>front|back))?"
    rf"-(?P<model>{_MODEL_SLUGS})"
    r"-(?P<dpi>\d+)dpi"
    rf"(?:-(?P<scryfall_id>{_SCRYFALL_UUID}))?"
    r"\.png$",
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
    -- Exactly one of scryfall_id / custom_hash is set, enforced by the
    -- CHECK below: a task either generates a Scryfall printing or a
    -- Custom Image (a user-uploaded card front, keyed by the sha256 of
    -- its bytes — see proxy_scaler/customs.py). set_code,
    -- collector_number and png_url are all NULL for the custom case,
    -- which is why none of them are NOT NULL any more.
    scryfall_id TEXT,
    custom_hash TEXT,
    face_index INTEGER,
    face_label TEXT,
    face_name TEXT NOT NULL,
    card_name TEXT NOT NULL,
    set_code TEXT,
    collector_number TEXT,
    png_url TEXT,
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
    total_faces INTEGER,
    -- Scryfall language code of the printing being generated. Part of the
    -- output filename for non-English printings (pipeline.output_filename)
    -- so two languages of one set/collector never collide on disk.
    lang TEXT NOT NULL DEFAULT 'en',
    -- 1 = user-initiated regeneration: bypass the x4 upscale cache and
    -- re-run inference. 0 (first generation) reuses the cache, which is
    -- what lets sibling DPI tasks of one face share a single model pass.
    force INTEGER NOT NULL DEFAULT 0,
    CHECK ((scryfall_id IS NULL) <> (custom_hash IS NULL))
);

CREATE INDEX IF NOT EXISTS idx_tasks_status
    ON generation_tasks(status, created_at);
CREATE INDEX IF NOT EXISTS idx_tasks_project_tag
    ON generation_tasks(project_tag);

-- The global registry of generated images: one row per physically
-- distinct output image, shared by every project. No project_tag —
-- which projects show an image is project_gallery_memberships' job.
-- Identity is a typed either/or: exactly one of scryfall_id /
-- custom_hash is set on every row, enforced by the CHECK below.
-- scryfall_id, when set, is always a real Scryfall UUID — files that
-- can't be resolved to one (output-dir rescans without a card corpus)
-- are simply not registered, rather than minting sentinel ids, which is
-- what migration 006 cleaned up. custom_hash is the sha256 of a Custom
-- Image the user uploaded (proxy_scaler/customs.py); migration 008 added
-- it as the honest alternative to reintroducing fake UUIDs for art that
-- genuinely has no Scryfall printing. This table is the authoritative
-- "does this image exist" answer — query paths (gallery list,
-- generation-status lookups, PDF layout) read it without touching the
-- filesystem; reconcile paths (prune/adopt) are what re-align it with
-- disk.
CREATE TABLE IF NOT EXISTS generated_images (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    scryfall_id TEXT,
    custom_hash TEXT,
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
    -- NULL for a Custom Image: there is no upstream URL, the bytes were
    -- uploaded by the client (see custom_hash above).
    png_url TEXT,
    -- Set on every insert AND refreshed on re-generation (see
    -- upsert_gallery_item): "when was this image last produced", which is
    -- what the PDF tab's most-recent-wins variant pick means by recency.
    created_at TEXT,
    -- How many physical faces this card has (see scryfall.CardFaceImage) —
    -- lets pdf_layout.match_quantities notice a DFC missing its other face
    -- without a live Scryfall call. NULL for rows predating migration 003.
    total_faces INTEGER,
    -- Scryfall language code of the printing (see generation_tasks.lang).
    -- 'en' for rows predating migration 005.
    lang TEXT NOT NULL DEFAULT 'en',
    CHECK ((scryfall_id IS NULL) <> (custom_hash IS NULL))
);

-- DB-level backstop for one-row-per-variant. Keyed on the identity
-- *expression* rather than a column, since either half of the
-- scryfall_id/custom_hash pair may be NULL. COALESCE throughout because
-- SQLite treats every NULL as distinct in a UNIQUE index — true of
-- face_index (NULL for single-faced cards) and, since migration 008, of
-- scryfall_id too, so without the COALESCE the backstop would silently
-- not apply to custom rows at all. The write path still probes for the
-- existing row itself (see upsert_gallery_item) rather than leaning on
-- ON CONFLICT against an expression index.
CREATE UNIQUE INDEX IF NOT EXISTS idx_generated_images_variant
    ON generated_images(
        COALESCE(scryfall_id, 'custom:' || custom_hash),
        COALESCE(face_index, -1), model, dpi);

-- Which projects show which registry images in their gallery. Pure
-- relation: all image metadata lives on generated_images. Cascade so
-- deleting a registry row (pruning a file that vanished from disk)
-- silently drops it from every project's gallery.
CREATE TABLE IF NOT EXISTS project_gallery_memberships (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    project_tag TEXT NOT NULL,
    image_id INTEGER NOT NULL
        REFERENCES generated_images(id) ON DELETE CASCADE,
    UNIQUE (project_tag, image_id)
);

CREATE INDEX IF NOT EXISTS idx_memberships_project_tag
    ON project_gallery_memberships(project_tag);

-- Cross-process control flags shared between the API server and the
-- worker (separate processes whose only common ground is this database).
-- Currently just 'worker_hold' — see set_worker_hold/get_worker_hold.
CREATE TABLE IF NOT EXISTS worker_control (
    key TEXT PRIMARY KEY,
    value TEXT NOT NULL
);
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
    # Exactly one of scryfall_id / custom_hash is set (db migration 008).
    # For a Custom Image the set_code, collector_number and png_url below
    # are all None too — it has no printing and no upstream URL.
    scryfall_id: str | None
    face_index: int | None
    face_label: str | None
    face_name: str
    card_name: str
    set_code: str | None
    collector_number: str | None
    png_url: str | None
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
    lang: str = "en"
    # User-initiated regeneration: bypass the x4 upscale cache. False for
    # first-generation tasks so sibling DPI tasks share one model pass.
    force: bool = False
    # sha256 of a user-uploaded card front (proxy_scaler/customs.py). Set
    # exactly when scryfall_id is not.
    custom_hash: str | None = None

    @property
    def identity_key(self) -> str:
        """The string this task's face is identified by — see
        customs.identity_key."""
        return identity_key(self.scryfall_id, self.custom_hash)

    @property
    def is_custom(self) -> bool:
        return self.custom_hash is not None

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
            lang=row["lang"] if "lang" in row.keys() else "en",
            force=bool(row["force"]) if "force" in row.keys() else False,
            custom_hash=row["custom_hash"] if "custom_hash" in row.keys() else None,
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
    # WAL still raises "database is locked" immediately on writer-vs-writer
    # collisions; with the worker's finisher thread there are now three
    # concurrent writers (API process, worker main thread, finisher), so
    # give SQLite a real retry window instead.
    conn.execute("PRAGMA busy_timeout = 5000")
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


def _migration_004_add_worker_control(conn: sqlite3.Connection) -> None:
    """Add the worker_control key-value table — the channel through which
    the API server releases a worker that the supervisor started held (the
    desktop client spawns its sidecar with --hold-worker so leftover tasks
    from the last session don't start processing before the user has been
    asked). IF NOT EXISTS keeps it a no-op on a database where _SCHEMA
    already created the table."""
    conn.execute(
        "CREATE TABLE IF NOT EXISTS worker_control ("
        "key TEXT PRIMARY KEY, value TEXT NOT NULL)"
    )


def _migration_005_add_lang(conn: sqlite3.Connection) -> None:
    """Add lang to generation_tasks and project_gallery_items — the
    Scryfall language code of the printing, captured at enqueue time like
    every other denormalized identity field. Needed now that non-English
    printings are selectable (see the card-corpus feature / carddb.py):
    language is part of a printing's identity, and it reaches output
    filenames via pipeline.output_filename for non-English printings so
    two languages of one set/collector never collide on disk.

    NOT NULL DEFAULT 'en' rather than nullable-with-no-backfill (unlike
    migrations 002/003): 'en' is the honest value for every pre-existing
    row, because until this feature only English printings were ever
    reachable — /cards/{set}/{number} answers in English and name lookups
    resolved English printings. Guarded/no-op the same way as 003."""
    for table in ("generation_tasks", "project_gallery_items"):
        tables = {
            row[0]
            for row in conn.execute(
                "SELECT name FROM sqlite_master WHERE type='table'"
            ).fetchall()
        }
        if table not in tables:
            continue
        cols = {
            row[1]
            for row in conn.execute(f"PRAGMA table_info({table})").fetchall()
        }
        if "lang" not in cols:
            conn.execute(
                f"ALTER TABLE {table} ADD COLUMN lang TEXT NOT NULL DEFAULT 'en'"
            )


def _migration_007_add_task_force(conn: sqlite3.Connection) -> None:
    """Add generation_tasks.force — 1 marks a user-initiated regeneration
    that must bypass the x4 upscale cache and re-run inference; 0 (every
    pre-existing row, honestly: they all ran force=True semantics and are
    already finished or will re-run identically) lets first-generation
    sibling DPI tasks of one face reuse the cached x4 pass instead of
    re-running the model per DPI. Guarded/no-op the same way as 005."""
    tables = {
        row[0]
        for row in conn.execute(
            "SELECT name FROM sqlite_master WHERE type='table'"
        ).fetchall()
    }
    if "generation_tasks" not in tables:
        return
    cols = {
        row[1]
        for row in conn.execute("PRAGMA table_info(generation_tasks)").fetchall()
    }
    if "force" not in cols:
        conn.execute(
            "ALTER TABLE generation_tasks "
            "ADD COLUMN force INTEGER NOT NULL DEFAULT 0"
        )


def _migration_006_split_gallery_registry(conn: sqlite3.Connection) -> None:
    """Split project_gallery_items into the global generated_images
    registry plus the project_gallery_memberships relation (see _SCHEMA).
    One registry row per physically distinct image regardless of how many
    projects showed it — per variant key the newest created_at row wins
    (same recency rule as the PDF variant pick; NULL sorts oldest), and
    every (project_tag, variant) pair becomes a membership.

    Rows with a sentinel scryfall_id ('scan:…', or '' from early builds)
    are dropped, not carried over: the registry's contract is that
    scryfall_id is always real. Those rows were pure derivations of files
    still on disk, and the adopt endpoint's rescan — which, unlike this
    migration, can reach the card corpus to resolve a filename's printing
    to a real id — re-registers them on the next project load. Worst case
    a scanned image reads "not generated" until that reconcile runs.

    A no-op (beyond creating the new tables, which _SCHEMA would anyway)
    when project_gallery_items is absent — migration 001 may have dropped
    a pre-reshape one."""
    conn.execute(
        """
        CREATE TABLE IF NOT EXISTS generated_images (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
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
            created_at TEXT,
            total_faces INTEGER,
            lang TEXT NOT NULL DEFAULT 'en'
        )
        """
    )
    conn.execute(
        "CREATE UNIQUE INDEX IF NOT EXISTS idx_generated_images_variant "
        "ON generated_images(scryfall_id, COALESCE(face_index, -1), model, dpi)"
    )
    conn.execute(
        """
        CREATE TABLE IF NOT EXISTS project_gallery_memberships (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            project_tag TEXT NOT NULL,
            image_id INTEGER NOT NULL
                REFERENCES generated_images(id) ON DELETE CASCADE,
            UNIQUE (project_tag, image_id)
        )
        """
    )
    conn.execute(
        "CREATE INDEX IF NOT EXISTS idx_memberships_project_tag "
        "ON project_gallery_memberships(project_tag)"
    )
    tables = {
        row[0]
        for row in conn.execute(
            "SELECT name FROM sqlite_master WHERE type='table'"
        ).fetchall()
    }
    if "project_gallery_items" not in tables:
        return
    # Columns added by migrations 002/003/005 are guaranteed present here:
    # steps replay strictly in order, so a database this old has already
    # passed through them by the time this one runs.
    conn.execute(
        """
        INSERT INTO generated_images (
            scryfall_id, face_index, face_name, card_name, set_code,
            collector_number, face_label, model, dpi, native_scale, device,
            image_filename, out_path, original_path, png_url, created_at,
            total_faces, lang)
        SELECT scryfall_id, face_index, face_name, card_name, set_code,
            collector_number, face_label, model, dpi, native_scale, device,
            image_filename, out_path, original_path, png_url, created_at,
            total_faces, lang
        FROM project_gallery_items p
        WHERE p.scryfall_id != '' AND p.scryfall_id NOT LIKE 'scan:%'
          AND p.id = (
              SELECT p2.id FROM project_gallery_items p2
              WHERE p2.scryfall_id = p.scryfall_id
                AND p2.face_index IS p.face_index
                AND p2.model = p.model AND p2.dpi = p.dpi
              ORDER BY p2.created_at IS NULL, p2.created_at DESC, p2.id DESC
              LIMIT 1
          )
        """
    )
    conn.execute(
        """
        INSERT OR IGNORE INTO project_gallery_memberships (project_tag, image_id)
        SELECT p.project_tag, g.id
        FROM project_gallery_items p
        JOIN generated_images g ON g.scryfall_id = p.scryfall_id
            AND g.face_index IS p.face_index
            AND g.model = p.model AND g.dpi = p.dpi
        WHERE p.scryfall_id != '' AND p.scryfall_id NOT LIKE 'scan:%'
        """
    )
    conn.execute("DROP TABLE project_gallery_items")


# Frozen copies of the post-008 table shapes. Repeated literally rather
# than shared with _SCHEMA, exactly as migration 006 does: a migration has
# to keep producing the shape it was written to produce even after _SCHEMA
# moves on, or replaying history on an old database lands it somewhere
# migration 009 never expected.
_TASKS_DDL_008 = """
    CREATE TABLE generation_tasks (
        id INTEGER PRIMARY KEY AUTOINCREMENT,
        project_tag TEXT,
        status TEXT NOT NULL DEFAULT 'pending',
        scryfall_id TEXT,
        custom_hash TEXT,
        face_index INTEGER,
        face_label TEXT,
        face_name TEXT NOT NULL,
        card_name TEXT NOT NULL,
        set_code TEXT,
        collector_number TEXT,
        png_url TEXT,
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
        total_faces INTEGER,
        lang TEXT NOT NULL DEFAULT 'en',
        force INTEGER NOT NULL DEFAULT 0,
        CHECK ((scryfall_id IS NULL) <> (custom_hash IS NULL))
    )
"""

_IMAGES_DDL_008 = """
    CREATE TABLE generated_images (
        id INTEGER PRIMARY KEY AUTOINCREMENT,
        scryfall_id TEXT,
        custom_hash TEXT,
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
        png_url TEXT,
        created_at TEXT,
        total_faces INTEGER,
        lang TEXT NOT NULL DEFAULT 'en',
        CHECK ((scryfall_id IS NULL) <> (custom_hash IS NULL))
    )
"""


_MEMBERSHIPS_DDL_008 = """
    CREATE TABLE project_gallery_memberships (
        id INTEGER PRIMARY KEY AUTOINCREMENT,
        project_tag TEXT NOT NULL,
        image_id INTEGER NOT NULL
            REFERENCES generated_images(id) ON DELETE CASCADE,
        UNIQUE (project_tag, image_id)
    )
"""


def _migration_008_custom_image_identity(conn: sqlite3.Connection) -> None:
    """Let a task/registry row be identified by an uploaded image's content
    hash instead of a Scryfall id, for Custom Images (user-supplied card
    fronts — see proxy_scaler/customs.py).

    This amends the contract migration 006 established. That one deleted
    sentinel scryfall_ids ('scan:…') and the comment on _SCHEMA has said
    "always a real Scryfall UUID" ever since, which is still true of the
    column: the fix here is emphatically *not* to start minting fake UUIDs
    again. Instead identity becomes a typed either/or — exactly one of
    scryfall_id / custom_hash is set on every row, enforced by CHECK — so
    the thing 006 was protecting against (rows nothing can ever match) is
    prevented by the schema rather than by a convention.

    A table rebuild, because scryfall_id and png_url are NOT NULL on both
    tables and SQLite cannot drop NOT NULL in place. set_code and
    collector_number go nullable on generation_tasks too (they are already
    nullable on generated_images): a Custom Image has no set or collector
    number, and '' would be one more sentinel to remember.

    **project_gallery_memberships is rebuilt alongside generated_images,
    and must be.** With foreign_keys=ON, ALTER TABLE RENAME rewrites
    REFERENCES clauses in *other* tables to follow the new name — and
    unlike the 3.25 rename changes, that behaviour is not switched off by
    PRAGMA legacy_alter_table. So renaming generated_images out of the way
    silently repoints memberships at generated_images_old, and the DROP at
    the end then cascade-deletes every membership row: every project's
    gallery, emptied, with the migration reporting success. Repointing
    memberships at the new table before that DROP is what makes the
    rebuild safe. Row ids are preserved through both copies so the
    relation still lines up.

    Defensive about which tables are actually present, like migration 006:
    a database can reach this step with the gallery tables missing (a
    fixture, or a user_version set past 006 on a partial file), and the
    _SCHEMA re-application at the end of _migrate() then creates whatever
    was skipped directly at the latest shape.
    """
    tables = {
        row[0]
        for row in conn.execute(
            "SELECT name FROM sqlite_master WHERE type='table'"
        ).fetchall()
    }

    if "generation_tasks" in tables:
        _rebuild_tasks_008(conn)
    if "generated_images" in tables:
        _rebuild_images_008(conn, has_memberships="project_gallery_memberships" in tables)


def _rebuild_tasks_008(conn: sqlite3.Connection) -> None:
    conn.execute("ALTER TABLE generation_tasks RENAME TO generation_tasks_old")
    conn.execute(_TASKS_DDL_008)
    conn.execute(
        """
        INSERT INTO generation_tasks (
            id, project_tag, status, scryfall_id, custom_hash, face_index,
            face_label, face_name, card_name, set_code, collector_number,
            png_url, dpi, model, tile_size, output_dir, cache_dir,
            weights_dir, error, created_at, started_at, completed_at,
            total_faces, lang, force)
        SELECT
            id, project_tag, status, scryfall_id, NULL, face_index,
            face_label, face_name, card_name, set_code, collector_number,
            png_url, dpi, model, tile_size, output_dir, cache_dir,
            weights_dir, error, created_at, started_at, completed_at,
            total_faces, lang, force
        FROM generation_tasks_old
        """
    )
    conn.execute("DROP TABLE generation_tasks_old")
    conn.execute(
        "CREATE INDEX IF NOT EXISTS idx_tasks_status "
        "ON generation_tasks(status, created_at)"
    )
    conn.execute(
        "CREATE INDEX IF NOT EXISTS idx_tasks_project_tag "
        "ON generation_tasks(project_tag)"
    )


def _rebuild_images_008(conn: sqlite3.Connection, *, has_memberships: bool) -> None:
    conn.execute("ALTER TABLE generated_images RENAME TO generated_images_old")
    conn.execute(_IMAGES_DDL_008)
    conn.execute(
        """
        INSERT INTO generated_images (
            id, scryfall_id, custom_hash, face_index, face_name, card_name,
            set_code, collector_number, face_label, model, dpi,
            native_scale, device, image_filename, out_path, original_path,
            png_url, created_at, total_faces, lang)
        SELECT
            id, scryfall_id, NULL, face_index, face_name, card_name,
            set_code, collector_number, face_label, model, dpi,
            native_scale, device, image_filename, out_path, original_path,
            png_url, created_at, total_faces, lang
        FROM generated_images_old
        """
    )
    # Repoint memberships at the new table before generated_images_old is
    # dropped — see the docstring. The rows are copied by id, so which
    # image each project shows is unchanged.
    if has_memberships:
        conn.execute(
            "ALTER TABLE project_gallery_memberships "
            "RENAME TO project_gallery_memberships_old"
        )
        conn.execute(_MEMBERSHIPS_DDL_008)
        conn.execute(
            """
            INSERT INTO project_gallery_memberships (id, project_tag, image_id)
            SELECT id, project_tag, image_id
            FROM project_gallery_memberships_old
            """
        )
        conn.execute("DROP TABLE project_gallery_memberships_old")
    conn.execute("DROP TABLE generated_images_old")
    if has_memberships:
        conn.execute(
            "CREATE INDEX IF NOT EXISTS idx_memberships_project_tag "
            "ON project_gallery_memberships(project_tag)"
        )
    # Rebuilt over the identity *expression*, not the raw column: with
    # scryfall_id now nullable, SQLite's treat-every-NULL-as-distinct
    # rule would quietly remove the one-row-per-variant backstop for
    # exactly the new custom rows the column was made nullable for.
    conn.execute(
        "CREATE UNIQUE INDEX IF NOT EXISTS idx_generated_images_variant "
        "ON generated_images("
        "COALESCE(scryfall_id, 'custom:' || custom_hash), "
        "COALESCE(face_index, -1), model, dpi)"
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
    Migration(
        4,
        "add worker_control key-value table for the startup worker "
        "hold/release handshake between the API server and the worker",
        _migration_004_add_worker_control,
    ),
    Migration(
        5,
        "add lang to generation_tasks and project_gallery_items — printing "
        "language became part of identity when non-English printings became "
        "selectable from the local card corpus",
        _migration_005_add_lang,
    ),
    Migration(
        6,
        "split project_gallery_items into the global generated_images "
        "registry (real scryfall_id only) plus project_gallery_memberships, "
        "making the database the authoritative record of which images exist",
        _migration_006_split_gallery_registry,
    ),
    Migration(
        7,
        "add force to generation_tasks — distinguishes user-initiated "
        "regeneration (bypass the x4 upscale cache) from first generation "
        "(reuse it, so sibling DPI tasks share one model pass)",
        _migration_007_add_task_force,
    ),
    Migration(
        8,
        "rebuild generation_tasks/generated_images so a row can be "
        "identified by a Custom Image's content hash instead of a "
        "scryfall_id — exactly one of the two, enforced by CHECK",
        _migration_008_custom_image_identity,
    ),
]
SCHEMA_VERSION = 8  # kept in sync with _MIGRATIONS[-1].version
assert _MIGRATIONS[-1].version == SCHEMA_VERSION

# Tables from every schema shape this database has ever had — legacy ones
# included — so _migrate()'s "is this a genuinely fresh database" check
# below can't mistake a partially-legacy file for a brand new one.
_KNOWN_TABLES = frozenset(
    {
        "generation_tasks",
        "project_gallery_items",
        "generated_images",
        "project_gallery_memberships",
        "worker_control",
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
    scryfall_id: str | None = None,
    custom_hash: str | None = None,
    face_index: int | None,
    face_label: str | None,
    face_name: str,
    card_name: str,
    set_code: str | None = None,
    collector_number: str | None = None,
    png_url: str | None = None,
    dpi: int,
    model: str,
    output_dir: str,
    cache_dir: str,
    weights_dir: str,
    tile_size: int = 0,
    total_faces: int | None = None,
    lang: str = "en",
    force: bool = False,
    db_path: Path | str | None = None,
) -> int:
    """Add one (face, dpi, model) unit of generation work to the queue,
    picked up by the background worker (see worker.py). force=True marks a
    user-initiated regeneration that must bypass the x4 upscale cache.

    Pass exactly one of scryfall_id (a Scryfall printing) or custom_hash (a
    user-uploaded card front); the database CHECK rejects both or neither.
    The set_code/collector_number/png_url trio only applies to the former.
    """
    identity_key(scryfall_id, custom_hash)  # raises before touching the DB
    now = _utc_now()
    with connect(db_path) as conn:
        cur = conn.execute(
            """
            INSERT INTO generation_tasks (
                project_tag, status, scryfall_id, custom_hash, face_index,
                face_label, face_name, card_name, set_code, collector_number,
                png_url, dpi, model, tile_size, output_dir, cache_dir,
                weights_dir, created_at, total_faces, lang, force
            ) VALUES (?, 'pending', ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
            """,
            (
                project_tag,
                scryfall_id,
                custom_hash,
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
                lang,
                int(force),
            ),
        )
        conn.commit()
        return int(cur.lastrowid)


def reset_orphaned_running_tasks(db_path: Path | str | None = None) -> int:
    """Re-queue tasks stuck in 'running' from a worker that died mid-task.

    'running' is only ever set by the single live worker (serialized by
    the worker lock file), so at worker startup — after the lock is
    held — any row still 'running' belongs to a dead worker and will
    otherwise sit in that state forever: claim_next_task() only claims
    from 'pending', and clients render the orphan as a generation
    perpetually in progress. Returns the number of tasks re-queued."""
    with connect(db_path) as conn:
        cur = conn.execute(
            "UPDATE generation_tasks SET status = 'pending', started_at = NULL "
            "WHERE status = 'running'"
        )
        conn.commit()
        return cur.rowcount


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


def peek_next_pending(db_path: Path | str | None = None) -> TaskRow | None:
    """The task claim_next_task() would hand out next, WITHOUT claiming it.

    Used by the worker's prefetcher to warm the next task's original while
    the GPU works on the current one. Must mirror claim_next_task()'s
    ORDER BY exactly — a prefetch of the wrong row is merely wasted, but
    the whole point is predicting the next claim."""
    with connect(db_path) as conn:
        row = conn.execute(
            "SELECT * FROM generation_tasks WHERE status = 'pending' "
            "ORDER BY created_at ASC, id ASC LIMIT 1"
        ).fetchone()
    return TaskRow.from_row(row) if row is not None else None


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


def cancel_all_tasks(
    db_path: Path | str | None = None, *, include_running: bool = False
) -> int:
    """Cancel every still-pending task, across all projects. Returns the
    number canceled.

    include_running additionally cancels 'running' rows (clearing their
    started_at, since nothing is actually running them). Only valid while
    the worker is held (see get_worker_hold): a held worker has claimed
    nothing, so any 'running' row is a provable orphan from a dead worker
    — whereas against a live worker the row may be genuinely in flight,
    and the worker would overwrite 'canceled' with 'done'/'failed' when it
    finishes. The API layer enforces that precondition."""
    statuses = "('pending', 'running')" if include_running else "('pending')"
    with connect(db_path) as conn:
        cur = conn.execute(
            "UPDATE generation_tasks SET status = 'canceled', "
            "started_at = NULL, completed_at = ? "
            f"WHERE status IN {statuses}",
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
    """One generated_images row as the dict shape gallery consumers use.
    Registry rows carry no project_tag — membership is a separate
    relation (project_gallery_memberships), so the same dict serves every
    project that shows the image."""
    return {
        "id": int(g["id"]),
        "out_path": g["out_path"],
        "original_path": g["original_path"],
        "scryfall_id": g["scryfall_id"],
        # Set exactly when scryfall_id is not — a user-uploaded card front.
        "custom_hash": g["custom_hash"],
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
        "device": g["device"],
        "image_filename": g["image_filename"],
        # None is meaningful (treated as oldest by the PDF variant pick),
        # not an error case — pre-migration-002 rows carried no timestamp.
        "created_at": g["created_at"],
        # None means "unknown, don't verify DFC completeness" (see
        # pdf_layout.match_quantities).
        "total_faces": g["total_faces"],
        "lang": g["lang"],
    }


def list_gallery_items(
    project_tag: str, db_path: Path | str | None = None
) -> list[dict[str, Any]]:
    """A project's gallery: the registry rows it holds memberships for —
    cheap enough to call on every Decklist tab poll to pick up results the
    background worker wrote directly via upsert_gallery_item_for_task()."""
    with connect(db_path) as conn:
        rows = conn.execute(
            """
            SELECT g.* FROM generated_images g
            JOIN project_gallery_memberships m ON m.image_id = g.id
            WHERE m.project_tag = ?
            ORDER BY g.dpi ASC, g.face_index ASC
            """,
            (project_tag,),
        ).fetchall()
    return [_gallery_row_to_dict(g) for g in rows]


def adopt_gallery_items(
    project_tag: str,
    entries: list[DeckEntry],
    db_path: Path | str | None = None,
    output_dir: Path | str | None = None,
    card_db_path: Path | str | None = None,
) -> int:
    """Make already-existing images for matching cards show in this
    project's gallery, so a freshly imported deck shows what already
    exists instead of "Not generated yet" until a Generate is requested.

    Two passes:
    1. Registry adoption, pure SQL plus one liveness stat per candidate:
       every generated_images row matching an entry — by scryfall_id when
       the entry is pinned to one, by printing (set + collector + lang)
       when it has one, else by card name — that this project holds no
       membership for yet gains a membership. Rows whose output file is
       gone from disk are deleted from the registry outright (the cascade
       drops them from every project's gallery): adopting one would
       produce a gallery entry that lists as "done" but 404s on view and
       breaks PDF export.
    2. When output_dir is sent: scan its filenames for images with no
       registry row (files predating the registry, or produced by the
       CLI). A file whose name embeds its scryfall_id (the current
       output_filename format) registers directly; a legacy-named file
       is resolved to a real scryfall_id through the card corpus by its
       printing, strictly language-matched. Files that can't be resolved
       — printing not in the corpus, or no corpus imported at all — are
       skipped entirely: the registry never holds sentinel ids.
       created_at is the file's mtime — when the image was actually
       produced — which is what the PDF tab's most-recent-wins pick means
       by recency. Name-only entries can't match here (no printing token
       to find in the filename).

    Returns the number of memberships added."""
    if not project_tag or not entries:
        return 0
    # Local import: carddb is a separate optional database (the Scryfall
    # corpus); only this reconcile path ever crosses into it from here.
    from . import carddb

    ids = {entry.scryfall_id for entry in entries if entry.scryfall_id}
    # Printing identity includes language (absent → "en" on both sides):
    # an Italian entry adopts only Italian rows of its printing.
    exact = {
        (
            (entry.set_code or "").lower(),
            str(entry.collector_number),
            (entry.lang or "en").lower(),
        )
        for entry in entries
        if entry.set_code and entry.collector_number is not None
    }
    names = {
        entry.name.lower()
        for entry in entries
        if not (entry.set_code and entry.collector_number is not None)
    }

    adopted = 0
    card_conn: sqlite3.Connection | None = None
    card_conn_opened = False

    def corpus() -> sqlite3.Connection | None:
        # Lazy and once: most adopt calls have nothing to resolve, and the
        # corpus may legitimately not exist (open_if_ready → None).
        nonlocal card_conn, card_conn_opened
        if not card_conn_opened:
            card_conn_opened = True
            card_conn = carddb.open_if_ready(
                Path(card_db_path) if card_db_path else None
            )
        return card_conn

    try:
        with connect(db_path) as conn:
            def add_membership_row(image_id: int) -> None:
                nonlocal adopted
                cur = conn.execute(
                    "INSERT OR IGNORE INTO project_gallery_memberships "
                    "(project_tag, image_id) VALUES (?, ?)",
                    (project_tag, image_id),
                )
                adopted += cur.rowcount

            candidates = conn.execute(
                """
                SELECT g.* FROM generated_images g
                WHERE g.id NOT IN (
                    SELECT image_id FROM project_gallery_memberships
                    WHERE project_tag = ?
                )
                """,
                (project_tag,),
            ).fetchall()
            for row in candidates:
                matches = (
                    row["scryfall_id"] in ids
                    or (
                        (row["set_code"] or "").lower(),
                        str(row["collector_number"]),
                        (row["lang"] or "en").lower(),
                    )
                    in exact
                    or (row["card_name"] or "").lower() in names
                )
                if not matches:
                    continue
                if not Path(row["out_path"]).is_file():
                    conn.execute(
                        "DELETE FROM generated_images WHERE id = ?",
                        (int(row["id"]),),
                    )
                    continue
                add_membership_row(int(row["id"]))

            if output_dir is not None:
                for item in scan_gallery_from_output(output_dir, entries):
                    scryfall_id = item["scryfall_id"]
                    if not scryfall_id:
                        cconn = corpus()
                        if cconn is None:
                            continue
                        card_row = carddb.find_row_by_set_collector(
                            cconn,
                            item["set_code"],
                            item["collector_number"],
                            # Strict: a Japanese file must not register
                            # under the English printing of its set/collector.
                            only_lang=item["lang"],
                        )
                        if card_row is None:
                            continue
                        scryfall_id = card_row["id"]
                    existing = conn.execute(
                        "SELECT id FROM generated_images WHERE scryfall_id = ? "
                        "AND face_index IS ? AND model = ? AND dpi = ?",
                        (
                            scryfall_id,
                            item["face_index"],
                            item["model"],
                            item["dpi"],
                        ),
                    ).fetchone()
                    if existing is not None:
                        add_membership_row(int(existing["id"]))
                        continue
                    path = Path(item["out_path"])
                    created_at = (
                        datetime.fromtimestamp(path.stat().st_mtime, tz=timezone.utc)
                        .replace(microsecond=0)
                        .isoformat()
                    )
                    cur = conn.execute(
                        """
                        INSERT INTO generated_images (
                            scryfall_id, face_index, face_name, card_name,
                            set_code, collector_number, face_label, model, dpi,
                            native_scale, device, image_filename, out_path,
                            original_path, png_url, created_at, total_faces, lang
                        ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
                        """,
                        (
                            scryfall_id,
                            item["face_index"],
                            item["face_name"],
                            item["card_name"],
                            item["set_code"],
                            item["collector_number"],
                            item["face_label"],
                            item["model"],
                            item["dpi"],
                            item["native_scale"],
                            item["device"],
                            item["image_filename"],
                            item["out_path"],
                            item["original_path"],
                            item["png_url"],
                            created_at,
                            None,
                            item["lang"],
                        ),
                    )
                    add_membership_row(int(cur.lastrowid))

            conn.commit()
    finally:
        if card_conn is not None:
            card_conn.close()
    return adopted


def prune_stale_gallery_items(project_tag: str, db_path: Path | str | None = None) -> int:
    """Delete the registry rows this project's gallery shows — and its
    "done" task records — whose output file no longer exists on disk, so
    the deck list's badges stop asserting images that can't be served.
    Output files are shared across projects and carry no tag, so any
    project (or a manual delete) can remove a file out from under every
    project's rows; this runs on project load (via the adopt endpoint) to
    reconcile. Dead rows leave the registry itself (the file is gone for
    everyone), and the membership cascade clears them from every gallery.

    Done tasks must go along with the rows: the client's status merge
    falls back to the newest task's own status for a (face, dpi, model)
    pair with no gallery row (see clear_project_generation_records), and a
    completed task's status is literally "done" — pruning only the row
    would let task history re-assert the same green badge. Pending/running
    /failed/canceled tasks are left alone: none of them claim an image
    exists. Returns the total records deleted."""
    if not project_tag:
        return 0
    # Local import: pipeline imports this module at runtime.
    from .dpi import ORIGINAL_MODEL
    from .pipeline import output_filename
    from .upscale import original_cache_path

    pruned = 0
    with connect(db_path) as conn:
        # Stale registry rows are deleted from the registry itself, not
        # just this project's membership: the file is gone for everyone,
        # and the FK cascade drops the row from every project's gallery.
        stale_rows = [
            int(r["id"])
            for r in conn.execute(
                """
                SELECT g.id, g.out_path FROM generated_images g
                JOIN project_gallery_memberships m ON m.image_id = g.id
                WHERE m.project_tag = ?
                """,
                (project_tag,),
            )
            if not Path(r["out_path"]).is_file()
        ]
        for gid in stale_rows:
            conn.execute("DELETE FROM generated_images WHERE id = ?", (gid,))
        pruned += len(stale_rows)

        stale_tasks = []
        for t in conn.execute(
            "SELECT id, scryfall_id, face_index, face_name, set_code, "
            "collector_number, face_label, model, dpi, output_dir, cache_dir, "
            "lang FROM generation_tasks "
            "WHERE project_tag = ? AND status = 'done'",
            (project_tag,),
        ):
            # Download tasks have no output file — their artifact is the
            # cached original itself, at a deterministic path (and their
            # sentinel model would crash output_filename's parse_model).
            if t["model"] == ORIGINAL_MODEL:
                original = original_cache_path(
                    Path(t["cache_dir"]), t["scryfall_id"], t["face_index"]
                )
                if not original.is_file():
                    stale_tasks.append(int(t["id"]))
                continue
            # A done task's file may carry either filename format: tasks
            # completed since the registry embed their scryfall_id, older
            # ones don't — and legacy files are never renamed. Stale only
            # when neither name exists.
            out_dir = Path(t["output_dir"])
            names = (
                output_filename(
                    t["face_name"],
                    t["set_code"],
                    t["collector_number"],
                    t["face_label"],
                    t["model"],
                    t["dpi"],
                    lang=t["lang"],
                    scryfall_id=t["scryfall_id"],
                ),
                output_filename(
                    t["face_name"],
                    t["set_code"],
                    t["collector_number"],
                    t["face_label"],
                    t["model"],
                    t["dpi"],
                    lang=t["lang"],
                ),
            )
            if not any((out_dir / name).is_file() for name in names):
                stale_tasks.append(int(t["id"]))
        for tid in stale_tasks:
            conn.execute("DELETE FROM generation_tasks WHERE id = ?", (tid,))
        pruned += len(stale_tasks)
        conn.commit()
    return pruned


def get_gallery_item(
    gallery_item_id: int, db_path: Path | str | None = None
) -> dict[str, Any] | None:
    """Look up one registry row by its own id — a plain global primary
    key, not scoped by project_tag, since (unlike listing, which is always
    "for this project") a single id is already unambiguous on its own."""
    with connect(db_path) as conn:
        row = conn.execute(
            "SELECT * FROM generated_images WHERE id = ?", (gallery_item_id,)
        ).fetchone()
    return _gallery_row_to_dict(row) if row is not None else None


def find_generated_image(
    identity: str,
    face_index: int | None,
    model: str,
    dpi: int,
    db_path: Path | str | None = None,
) -> dict[str, Any] | None:
    """One registry row by its variant key, or None. The registry-first
    half of the skip-existing check (services/generation.py): the database
    is the authority on what exists, so a known image needs no filename
    reconstruction — just a liveness stat of the recorded out_path.

    `identity` is a customs.identity_key() string — a Scryfall UUID or
    'custom:<sha256>'. The WHERE clause rebuilds the same expression
    idx_generated_images_variant is built over, so this stays an index
    lookup rather than a scan, and cannot disagree with the uniqueness
    backstop about which rows are the same variant.
    """
    with connect(db_path) as conn:
        row = conn.execute(
            "SELECT * FROM generated_images "
            "WHERE COALESCE(scryfall_id, 'custom:' || custom_hash) = ? "
            "AND face_index IS ? AND model = ? AND dpi = ?",
            (identity, face_index, model, dpi),
        ).fetchone()
    return _gallery_row_to_dict(row) if row is not None else None


def add_membership(
    project_tag: str, image_id: int, db_path: Path | str | None = None
) -> None:
    """Show one registry image in one project's gallery. Idempotent."""
    if not project_tag:
        return
    with connect(db_path) as conn:
        conn.execute(
            "INSERT OR IGNORE INTO project_gallery_memberships "
            "(project_tag, image_id) VALUES (?, ?)",
            (project_tag, image_id),
        )
        conn.commit()


def generated_variants_status(
    scryfall_ids: list[str],
    model: str,
    dpis: list[int],
    db_path: Path | str | None = None,
) -> dict[str, list[tuple[int, int | None]]]:
    """For each requested scryfall_id: the (dpi, face_index) pairs that
    exist in the registry at the given model, across every project — the
    printing picker's "already generated" lookup. Registry-only by
    design: no membership join (an image generated under any project
    counts) and no filesystem stats — a row can briefly outlive a
    deleted file until the next adopt/prune reconcile, which is the
    accepted trade for a query path that never touches disk."""
    if not scryfall_ids or not dpis or not model:
        return {}
    found: dict[str, list[tuple[int, int | None]]] = {}
    with connect(db_path) as conn:
        # Chunked to stay well under SQLite's host-parameter limit.
        for start in range(0, len(scryfall_ids), 500):
            chunk = scryfall_ids[start : start + 500]
            placeholders_ids = ",".join("?" * len(chunk))
            placeholders_dpis = ",".join("?" * len(dpis))
            rows = conn.execute(
                f"""
                SELECT DISTINCT scryfall_id, dpi, face_index
                FROM generated_images
                WHERE model = ? AND dpi IN ({placeholders_dpis})
                  AND scryfall_id IN ({placeholders_ids})
                """,
                (model, *dpis, *chunk),
            ).fetchall()
            for r in rows:
                found.setdefault(r["scryfall_id"], []).append(
                    (int(r["dpi"]), r["face_index"])
                )
    return found


def clear_project_generation_records(
    project_tag: str, db_path: Path | str | None = None
) -> None:
    """Deletes this project_tag's finished generation_tasks (done/failed/
    canceled) and all of its gallery memberships — the DB-side half of
    "delete all generated images & cache" (see pipeline.py::
    clear_generated_data for the on-disk half, and prune_registry_under_dir
    for the registry half when files are actually deleted). pending/running
    tasks are left alone: they haven't written a file yet, so there's
    nothing about them for the delete to have invalidated.

    Memberships only, never registry rows: discarding a tag deletes no
    files, so images it shared with other projects genuinely still exist
    and must keep answering existence queries for them.

    Both tables have to be touched, not just the memberships: a gallery
    row missing for a (scryfall_id, face_index, dpi, model) pair falls back
    to that pair's newest task's own status in the client's merge (see
    mergeCardStatus.ts::statusForPairs), and a *completed* task's status is
    literally "done" — so deleting only the membership would still leave
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
        conn.execute(
            "DELETE FROM project_gallery_memberships WHERE project_tag = ?",
            (project_tag,),
        )
        conn.commit()


def prune_registry_under_dir(
    output_dir: Path | str, db_path: Path | str | None = None
) -> int:
    """Delete every registry row whose out_path sits under output_dir —
    the DB-side companion to pipeline.clear_generated_data actually
    deleting those files. Without this, the stat-free existence queries
    (generated_variants_status and the skip-existing registry probe) would
    keep asserting images that were just wiped, for every project, until
    some prune happened to notice. The membership cascade clears every
    project's gallery along the way. Returns rows deleted."""
    prefix = str(Path(output_dir).resolve())
    if not prefix:
        return 0
    if not prefix.endswith(os.sep):
        prefix += os.sep
    with connect(db_path) as conn:
        cur = conn.execute(
            # ESCAPE so a directory containing SQL wildcard characters
            # (%, _) can't over-match unrelated paths.
            "DELETE FROM generated_images WHERE out_path LIKE ? ESCAPE '\\'",
            (prefix.replace("\\", "\\\\").replace("%", r"\%").replace("_", r"\_") + "%",),
        )
        conn.commit()
        return cur.rowcount


def upsert_gallery_item_for_task(
    task: TaskRow, result: FaceResult, db_path: Path | str | None = None
) -> None:
    """Persist one completed task's result straight into the
    generated_images registry, plus a membership for the task's project —
    how a background-worker-produced image makes it into a project's
    gallery. A no-op if the task has no project_tag (nothing to scope the
    membership to).

    No project_cards lookup/matching needed any more: the task already
    carries project_tag directly (denormalized at enqueue time, like
    every other identity field on it), so this is a plain scoped upsert
    — a real simplification the client/generation split enabled, not
    just a rename. See ARCHITECTURE.md."""
    if not task.project_tag:
        return
    upsert_gallery_item(task.project_tag, result, db_path=db_path)
    # An upscale had to put the ~300 DPI original on disk to run at all, so
    # register that as its own download-variant row while we're here. This
    # is what lets "Use 300 DPI originals" (and the deck list's Original
    # tile) work for a card the user only ever upscaled — before this, the
    # file sat in the cache with no registry row, and registry-first
    # matching correctly reported it missing, which read as a lie to anyone
    # looking at the cache directory. Scryfall faces only: custom uploads
    # register their source through the CUSTOM_SOURCE_MODEL path at enqueue
    # time instead (see services/generation.py) and never take this branch.
    from proxy_scaler.dpi import CUSTOM_SOURCE_MODEL, ORIGINAL_DPI, ORIGINAL_MODEL

    if (
        result.model not in (ORIGINAL_MODEL, CUSTOM_SOURCE_MODEL)
        and not result.custom_hash
        and result.original_path != result.out_path
        and result.original_path.is_file()
    ):
        from dataclasses import replace

        upsert_gallery_item(
            task.project_tag,
            replace(
                result,
                out_path=result.original_path,
                model=ORIGINAL_MODEL,
                dpi=ORIGINAL_DPI,
                native_scale=1,
                created_at=None,
            ),
            db_path=db_path,
        )


def upsert_gallery_item(
    project_tag: str, result: FaceResult, db_path: Path | str | None = None
) -> None:
    """Same upsert as upsert_gallery_item_for_task, for callers with no
    TaskRow to read project_tag off of — specifically
    services/generation.py's skip_existing path, which finds a face's
    output file already on disk (so no task ever runs for it under the
    current project_tag) and needs to register it into this project's
    gallery anyway, or it never shows up as "done" in the UI even though
    the image genuinely exists.

    Writes the registry row (insert, or refresh in place when the variant
    already exists) and then this project's membership. A no-op if
    project_tag is falsy — and if the result carries neither a scryfall_id
    nor a custom_hash, which real callers never produce (worker tasks and
    skip-existing both start from fully resolved faces): the registry's
    contract is that a row is identified by one or the other, never by a
    sentinel standing in for a missing id."""
    if not project_tag or not (result.scryfall_id or result.custom_hash):
        return
    with connect(db_path) as conn:
        # Not a plain `ON CONFLICT` upsert: the UNIQUE index includes
        # face_index, which is NULL for the common single-faced-card case,
        # and SQLite treats every NULL as distinct in a UNIQUE index (the
        # index COALESCEs around that as a backstop, but conflict targets
        # against an expression index are their own can of worms). `IS ?`
        # (unlike `= ?`) matches NULL-to-NULL correctly.
        existing = conn.execute(
            "SELECT id FROM generated_images "
            "WHERE COALESCE(scryfall_id, 'custom:' || custom_hash) = ? "
            "AND face_index IS ? AND model = ? AND dpi = ?",
            (
                result.identity_key,
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
            result.lang,
        )
        if existing is not None:
            image_id = int(existing["id"])
            conn.execute(
                """
                UPDATE generated_images SET
                    face_name = ?, card_name = ?, set_code = ?, collector_number = ?,
                    face_label = ?, native_scale = ?, device = ?, image_filename = ?,
                    out_path = ?, original_path = ?, png_url = ?, created_at = ?,
                    total_faces = ?, lang = ?
                WHERE id = ?
                """,
                (*values, image_id),
            )
        else:
            cur = conn.execute(
                """
                INSERT INTO generated_images (
                    scryfall_id, custom_hash, face_index, face_name, card_name,
                    set_code, collector_number, face_label, model, dpi, native_scale,
                    device, image_filename, out_path, original_path, png_url,
                    created_at, total_faces, lang
                ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
                """,
                (
                    result.scryfall_id,
                    result.custom_hash,
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
                    values[-3],  # same timestamp as the UPDATE branch above
                    values[-2],  # total_faces
                    values[-1],  # lang
                ),
            )
            image_id = int(cur.lastrowid)
        conn.execute(
            "INSERT OR IGNORE INTO project_gallery_memberships "
            "(project_tag, image_id) VALUES (?, ?)",
            (project_tag, image_id),
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


# --- Worker hold/release -------------------------------------------------
#
# The desktop client spawns its local sidecar with --hold-worker so
# leftover tasks from the last session don't start processing the moment
# the app launches — the worker waits (see worker._wait_while_held) until
# the client has asked the user to resume or cancel them, then releases it
# via POST /api/worker/release. The API server and the worker are separate
# processes whose only common ground is this database, hence a DB flag
# rather than a signal or pipe. The supervisor writes the initial state
# before spawning either child, so a non-held start (headless, standalone
# server, remote) actively clears any stale hold.

_WORKER_HOLD_KEY = "worker_hold"


def set_worker_hold(held: bool, db_path: Path | str | None = None) -> None:
    with connect(db_path) as conn:
        conn.execute(
            "INSERT INTO worker_control (key, value) VALUES (?, ?) "
            "ON CONFLICT(key) DO UPDATE SET value = excluded.value",
            (_WORKER_HOLD_KEY, "1" if held else "0"),
        )
        conn.commit()


def get_worker_hold(db_path: Path | str | None = None) -> bool:
    """Whether the worker is currently held. Absent row reads as not held
    — the flag only exists at all on databases the supervisor has touched
    since the hold feature landed."""
    with connect(db_path) as conn:
        row = conn.execute(
            "SELECT value FROM worker_control WHERE key = ?",
            (_WORKER_HOLD_KEY,),
        ).fetchone()
    return row is not None and row["value"] == "1"


def release_worker_hold(db_path: Path | str | None = None) -> bool:
    """Clear the hold. Returns whether it was actually held (False makes a
    repeat release an idempotent no-op)."""
    with connect(db_path) as conn:
        cur = conn.execute(
            "UPDATE worker_control SET value = '0' WHERE key = ? AND value = '1'",
            (_WORKER_HOLD_KEY,),
        )
        conn.commit()
        return cur.rowcount > 0


# Set by the worker the moment a GPU→CPU OOM fallback fires (see
# worker._start_one's on_cpu_fallback closure); read by the client via
# GET /api/worker/status and cleared when the user acknowledges the
# fallback dialog (POST /api/worker/cpu-fallback/ack) or when a fresh
# worker starts (a restart retries the GPU, so a stale flag describes a
# condition that no longer holds). The value is a small JSON note saying
# what was being generated when it happened.
_CPU_FALLBACK_KEY = "cpu_fallback"


def set_cpu_fallback(note: str, db_path: Path | str | None = None) -> None:
    with connect(db_path) as conn:
        conn.execute(
            "INSERT INTO worker_control (key, value) VALUES (?, ?) "
            "ON CONFLICT(key) DO UPDATE SET value = excluded.value",
            (_CPU_FALLBACK_KEY, note),
        )
        conn.commit()


def get_cpu_fallback(db_path: Path | str | None = None) -> str | None:
    """The pending fallback note, or None when no unacknowledged fallback
    exists. Absent row and empty value both read as None."""
    with connect(db_path) as conn:
        row = conn.execute(
            "SELECT value FROM worker_control WHERE key = ?",
            (_CPU_FALLBACK_KEY,),
        ).fetchone()
    return row["value"] if row is not None and row["value"] else None


def clear_cpu_fallback(db_path: Path | str | None = None) -> bool:
    """Acknowledge the fallback. Returns whether one was actually pending
    (False makes a repeat ack an idempotent no-op)."""
    with connect(db_path) as conn:
        cur = conn.execute(
            "UPDATE worker_control SET value = '' WHERE key = ? AND value != ''",
            (_CPU_FALLBACK_KEY,),
        )
        conn.commit()
        return cur.rowcount > 0


def parse_output_filename(name: str) -> dict[str, Any] | None:
    """Parse a proxy-scaler output PNG basename into metadata fields."""
    custom = _CUSTOM_OUTPUT_RE.match(name)
    if custom:
        model = custom.group("model")
        dpi = custom.group("dpi")
        return {
            "image_filename": name,
            # A Custom Image has no printing. Empty rather than a
            # placeholder: pdf_layout.match_quantities matches on
            # (set_code, collector_number), and any non-empty stand-in
            # would make every custom card match every other one.
            "set_code": "",
            "collector_number": "",
            "face_label": None,
            "face_index": None,
            "model": model.lower() if model else CUSTOM_SOURCE_MODEL,
            "dpi": int(dpi) if dpi else 0,
            "stem": custom.group("stem"),
            "lang": "en",
            "scryfall_id": "",
            "custom_hash": custom.group("custom_hash").lower(),
        }
    match = _OUTPUT_SUFFIX_RE.match(name)
    if not match:
        return None
    head = match.group("head")
    # Non-greedy stem so SET (2–6 alnum) is found early. Set codes may
    # start with a digit (40K, 2X2) but always contain a letter — the
    # lookahead keeps pure-number segments reading as collector numbers.
    set_col = re.match(
        r"^(?P<stem>.+?)-(?P<set>(?=[0-9]*[A-Za-z])[A-Za-z0-9]{2,6})-(?P<collector>.+)$",
        head,
    )
    if not set_col:
        return None
    face = match.group("face")
    face_index = None if face is None else (0 if face.lower() == "front" else 1)
    lang = match.group("lang")
    scryfall_id = match.group("scryfall_id")
    return {
        "image_filename": name,
        "set_code": set_col.group("set").lower(),
        "collector_number": set_col.group("collector"),
        "face_label": face.lower() if face else None,
        "face_index": face_index,
        "model": match.group("model").lower(),
        "dpi": int(match.group("dpi")),
        "stem": set_col.group("stem"),
        # English is never written into filenames, so absent means "en" —
        # which also covers every pre-language file.
        "lang": lang.lower() if lang else "en",
        # "" for legacy filenames that predate the embedded id — those
        # need the card corpus to resolve to a real scryfall_id.
        "scryfall_id": scryfall_id.lower() if scryfall_id else "",
        "custom_hash": None,
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
        raw_suffix_lang = suffix.group("lang")
        # The file's own language: from its lang segment, absent = English
        # (English is never written into filenames). A file only matches an
        # entry of the same language — a Japanese file must not register
        # under the English entry of the same printing.
        file_lang = raw_suffix_lang.lower() if raw_suffix_lang else "en"
        entry: DeckEntry | None = None
        for token, candidate in printings:
            # _OUTPUT_SUFFIX_RE already split any lang segment out of the
            # head, so the token matches regardless of the file's language
            # — the lang captured above is what has to agree.
            if head.endswith(token) and (candidate.lang or "en").lower() == file_lang:
                entry = candidate
                break
        if entry is None:
            # Fall back to generic parse (name-only lines won't match either way)
            meta = parse_output_filename(path.name)
            if meta is None:
                continue
            file_lang = meta["lang"]
            for token, candidate in printings:
                if (
                    candidate.set_code == meta["set_code"]
                    and str(candidate.collector_number) == str(meta["collector_number"])
                    and (candidate.lang or "en").lower() == file_lang
                ):
                    entry = candidate
                    break
            if entry is None:
                continue
            face_label = meta["face_label"]
            face_index = meta["face_index"]
            model = meta["model"]
            dpi = meta["dpi"]
            lang = meta["lang"]
        else:
            face = suffix.group("face")
            face_label = face.lower() if face else None
            face_index = (
                None if face is None else (0 if face.lower() == "front" else 1)
            )
            model = suffix.group("model").lower()
            dpi = int(suffix.group("dpi"))
            raw_lang = suffix.group("lang")
            lang = raw_lang.lower() if raw_lang else "en"

        raw_id = suffix.group("scryfall_id")
        gallery.append(
            {
                "out_path": str(path.resolve()),
                "original_path": "",
                # Embedded in the filename by the current output_filename
                # format; "" for legacy files, which adopt_gallery_items
                # resolves through the card corpus instead.
                "scryfall_id": raw_id.lower() if raw_id else "",
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
                "lang": lang,
            }
        )
    return gallery


