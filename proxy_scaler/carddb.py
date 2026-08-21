"""SQLite persistence for the local Scryfall card corpus.

A separate database file from proxy_scaler.db on purpose: that file is the
hot task queue polled by two processes on their own schedules, and this one
holds a bulk-imported corpus of a couple of million rows that gets rewritten
wholesale on reimport. Keeping them apart means the corpus can't bloat the
queue's WAL or anyone's backup of it, and the two never need a cross-file
JOIN — the only value that crosses the boundary is the `scryfall_id` string,
same spirit as the opaque `project_tag` (see ARCHITECTURE.md and ADR-0003).

Rows store a *pruned* card JSON (see card_import.prune_card) — exactly the
fields scryfall.expand_faces() and the client UI consume — plus indexed
columns for the lookups this corpus exists to answer: by id, by
set/collector/language, by name, and "all printings sharing an oracle_id"
for the change-printing picker.

The corpus is optional and disposable: nothing here is created at server
startup, and every reader must tolerate its absence. Use open_if_ready(),
which returns None instead of conjuring an empty database into existence.
"""

from __future__ import annotations

import json
import re
import sqlite3
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Iterable, Sequence

from . import db as _db
from .scryfall import card_printed_name, expected_face_count

DEFAULT_CARD_DB_PATH = _db._DATA_ROOT / "scryfall_cards.db"

# v2: added printed_name (column, index, and inside the pruned card_json).
# A v1 corpus lacks the data entirely — prune_card didn't keep the field —
# so there is nothing to migrate: open_if_ready() treats v1 as "no usable
# corpus" and the client's status panel offers a reimport.
CARD_SCHEMA_VERSION = 2

# Current, latest shape — everything IF NOT EXISTS, safe to re-apply.
_SCHEMA = """
CREATE TABLE IF NOT EXISTS cards (
    id TEXT PRIMARY KEY,
    oracle_id TEXT,
    name TEXT NOT NULL,
    printed_name TEXT,
    set_code TEXT NOT NULL,
    set_name TEXT,
    collector_number TEXT NOT NULL,
    lang TEXT NOT NULL,
    released_at TEXT,
    layout TEXT,
    digital INTEGER NOT NULL DEFAULT 0,
    image_status TEXT,
    highres_image INTEGER NOT NULL DEFAULT 0,
    card_json TEXT NOT NULL
);
CREATE INDEX IF NOT EXISTS idx_cards_oracle ON cards(oracle_id);
CREATE INDEX IF NOT EXISTS idx_cards_set_collector
    ON cards(set_code, collector_number, lang);
CREATE INDEX IF NOT EXISTS idx_cards_name ON cards(name COLLATE NOCASE);
CREATE INDEX IF NOT EXISTS idx_cards_printed_name
    ON cards(printed_name COLLATE NOCASE);

CREATE TABLE IF NOT EXISTS card_dataset_meta (
    key TEXT PRIMARY KEY,
    value TEXT NOT NULL
);
"""

# card_dataset_meta keys. Written together, only after the last import batch
# commits, so an interrupted import leaves them stale (or absent) and the
# staleness hint in the client stays honest.
META_DATASET_TYPE = "dataset_type"          # "default_cards" | "all_cards"
META_DATASET_UPDATED_AT = "dataset_updated_at"  # Scryfall's updated_at
META_IMPORTED_AT = "imported_at"            # our wall-clock finish time
META_CARD_COUNT = "card_count"


class CardDbVersionMismatch(RuntimeError):
    """Raised by connect() when the file's PRAGMA user_version doesn't match
    CARD_SCHEMA_VERSION. The corpus is rebuildable, so the fix is simply to
    reimport it — but silently querying a future/past shape is worse than
    telling the caller that."""


def _raw_connect(path: Path) -> sqlite3.Connection:
    path.parent.mkdir(parents=True, exist_ok=True)
    conn = sqlite3.connect(str(path))
    conn.row_factory = sqlite3.Row
    # WAL so resolve/variants reads keep working while a reimport is
    # upserting batches; NORMAL because losing the tail of an interrupted
    # import costs nothing — meta is only written after the final batch, so
    # a re-run re-upserts idempotently either way.
    conn.execute("PRAGMA journal_mode = WAL")
    conn.execute("PRAGMA synchronous = NORMAL")
    return conn


def delete_card_db(db_path: Path | str | None = None) -> bool:
    """Remove the corpus database from disk — the file plus SQLite's WAL/
    SHM sidecars. Safe while no import is writing (the API layer refuses
    the delete during one); readers hold no persistent connections (every
    lookup opens and closes), so a concurrent read at worst errors once.
    Returns whether a database file actually existed."""
    path = Path(db_path) if db_path else DEFAULT_CARD_DB_PATH
    existed = path.exists()
    for target in (path, path.with_name(path.name + "-wal"), path.with_name(path.name + "-shm")):
        target.unlink(missing_ok=True)
    return existed


def init_card_db(db_path: Path | str | None = None) -> Path:
    """Create (or verify) the corpus database. Called by the import job
    before its first batch — never at server startup, so a server that has
    never imported anything also never has an empty corpus file lying
    around confusing open_if_ready()."""
    path = Path(db_path) if db_path else DEFAULT_CARD_DB_PATH
    conn = _raw_connect(path)
    try:
        version = conn.execute("PRAGMA user_version").fetchone()[0]
        if version > CARD_SCHEMA_VERSION:
            raise CardDbVersionMismatch(
                f"{path} is at card-db schema version {version}, newer than "
                f"this build's {CARD_SCHEMA_VERSION}. Delete the file or "
                "update the server."
            )
        if 0 < version < CARD_SCHEMA_VERSION:
            # Outdated corpus: rebuild from scratch rather than migrate —
            # the rows' pruned card_json predates the new shape too, so
            # there's nothing worth carrying over. This runs at the start
            # of an import job, which is about to repopulate everything
            # anyway (open_if_ready() already reported the old file as "no
            # usable corpus", so the client offered exactly that reimport).
            conn.execute("DROP TABLE IF EXISTS cards")
            conn.execute("DROP TABLE IF EXISTS card_dataset_meta")
        conn.executescript(_SCHEMA)
        conn.execute(f"PRAGMA user_version = {CARD_SCHEMA_VERSION}")
        conn.commit()
    finally:
        conn.close()
    return path


def connect(db_path: Path | str | None = None) -> sqlite3.Connection:
    """Open an initialized corpus database, guarding the schema version.
    For readers that must tolerate a corpus that was never imported, use
    open_if_ready() instead."""
    path = Path(db_path) if db_path else DEFAULT_CARD_DB_PATH
    conn = _raw_connect(path)
    version = conn.execute("PRAGMA user_version").fetchone()[0]
    if version != CARD_SCHEMA_VERSION:
        conn.close()
        raise CardDbVersionMismatch(
            f"{path} is at card-db schema version {version}, expected "
            f"{CARD_SCHEMA_VERSION}. Run the card database import."
        )
    return conn


def open_if_ready(db_path: Path | str | None = None) -> sqlite3.Connection | None:
    """The reader's entry point: a connection if a usable corpus exists at
    the path, else None — without creating an empty file as a side effect
    (sqlite3.connect would) and without raising for a version mismatch,
    which to a reader is the same thing as "no usable corpus"."""
    path = Path(db_path) if db_path else DEFAULT_CARD_DB_PATH
    if not path.exists():
        return None
    try:
        return connect(path)
    except CardDbVersionMismatch:
        return None


# ---------------------------------------------------------------------------
# Writes (import job only)


def upsert_cards(conn: sqlite3.Connection, cards: Iterable[dict[str, Any]]) -> int:
    """Upsert a batch of pruned card objects in one transaction. Returns the
    number of rows written. INSERT OR REPLACE keyed on the scryfall id makes
    reimports idempotent — Scryfall essentially never deletes printings, so
    rows that vanish upstream lingering here is acceptable."""
    rows = [_card_to_row(card) for card in cards]
    if not rows:
        return 0
    with conn:
        conn.executemany(
            """
            INSERT OR REPLACE INTO cards (
                id, oracle_id, name, printed_name, set_code, set_name,
                collector_number, lang, released_at, layout, digital,
                image_status, highres_image, card_json
            ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
            """,
            rows,
        )
    return len(rows)


def _card_to_row(card: dict[str, Any]) -> tuple:
    # reversible_card layouts carry oracle_id per face, not at top level.
    oracle_id = card.get("oracle_id")
    if not oracle_id:
        faces = card.get("card_faces") or []
        if faces and isinstance(faces[0], dict):
            oracle_id = faces[0].get("oracle_id")
    return (
        card["id"],
        oracle_id,
        card["name"],
        card_printed_name(card),
        str(card["set"]).lower(),
        card.get("set_name"),
        str(card["collector_number"]),
        card.get("lang") or "en",
        card.get("released_at"),
        card.get("layout"),
        1 if card.get("digital") else 0,
        card.get("image_status"),
        1 if card.get("highres_image") else 0,
        json.dumps(card, separators=(",", ":")),
    )


def delete_all_cards(conn: sqlite3.Connection) -> None:
    """Used when the import's dataset type changes (all_cards ⇄
    default_cards): upserting a smaller dataset over a larger one would
    strand rows from the old one, leaving a corpus that claims to be
    English-only but still answers in twelve languages."""
    with conn:
        conn.execute("DELETE FROM cards")
        conn.execute("DELETE FROM card_dataset_meta")


def set_meta(conn: sqlite3.Connection, values: dict[str, str]) -> None:
    with conn:
        conn.executemany(
            "INSERT OR REPLACE INTO card_dataset_meta (key, value) VALUES (?, ?)",
            list(values.items()),
        )


def write_import_meta(
    conn: sqlite3.Connection, *, dataset_type: str, dataset_updated_at: str
) -> None:
    """The import's final act — see the meta-key comment above for why this
    only ever runs after the last batch has committed."""
    count = count_cards(conn)
    set_meta(
        conn,
        {
            META_DATASET_TYPE: dataset_type,
            META_DATASET_UPDATED_AT: dataset_updated_at,
            META_IMPORTED_AT: datetime.now(timezone.utc).isoformat(),
            META_CARD_COUNT: str(count),
        },
    )


# ---------------------------------------------------------------------------
# Reads


def get_meta(conn: sqlite3.Connection) -> dict[str, str]:
    rows = conn.execute("SELECT key, value FROM card_dataset_meta").fetchall()
    return {row["key"]: row["value"] for row in rows}


def count_cards(conn: sqlite3.Connection) -> int:
    return int(conn.execute("SELECT count(*) FROM cards").fetchone()[0])


def distinct_languages(conn: sqlite3.Connection) -> list[str]:
    """All languages present in the corpus, English first — feeds the
    import-language dropdown, so its order is presentation order."""
    rows = conn.execute("SELECT DISTINCT lang FROM cards ORDER BY lang").fetchall()
    langs = [row["lang"] for row in rows]
    if "en" in langs:
        langs.remove("en")
        langs.insert(0, "en")
    return langs


def find_row_by_id(conn: sqlite3.Connection, scryfall_id: str) -> sqlite3.Row | None:
    return conn.execute(
        "SELECT * FROM cards WHERE id = ?", (scryfall_id,)
    ).fetchone()


# Matches the entry's name against the English/oracle name OR the
# localized printed name, each including the DFC front-face-alone form
# ("Delver of Secrets" finds "Delver of Secrets // Insectile Aberration",
# and a printed front name finds the composed printed name the same way).
_NAME_MATCH_SQL = """(
       name = ?1 COLLATE NOCASE
    OR name LIKE ?1 || ' //%'
    OR printed_name = ?1 COLLATE NOCASE
    OR printed_name LIKE ?1 || ' //%'
)"""


def find_row_by_set_collector(
    conn: sqlite3.Connection,
    set_code: str,
    collector_number: str,
    prefer_lang: str = "en",
    *,
    only_lang: str | None = None,
) -> sqlite3.Row | None:
    """Preferred language first, then English, then whatever exists — the
    English fallback keeps id-less entries behaving like the live
    GET /cards/{set}/{number}, which always answers in English.

    only_lang instead demands exactly that language (strict import mode):
    no fallback, None when the printing has no such version."""
    if only_lang is not None:
        return conn.execute(
            "SELECT * FROM cards WHERE set_code = ? AND collector_number = ? "
            "AND lang = ? LIMIT 1",
            (set_code.lower(), str(collector_number), only_lang),
        ).fetchone()
    return conn.execute(
        """
        SELECT * FROM cards
        WHERE set_code = ? AND collector_number = ?
        ORDER BY (lang = ?) DESC, (lang = 'en') DESC, lang
        LIMIT 1
        """,
        (set_code.lower(), str(collector_number), prefer_lang),
    ).fetchone()


def find_row_by_name_and_collector(
    conn: sqlite3.Connection,
    name: str,
    collector_number: str,
    prefer_lang: str = "en",
    *,
    only_lang: str | None = None,
) -> sqlite3.Row | None:
    """Name plus a collector-number *hint* with no set code — the best some
    deck managers can export (see decklist._NAME_COLLECTOR_RE). Any set's
    printing of this name (English or printed) with that collector number
    qualifies; among those, the same preference order as find_row_by_name
    — or exactly only_lang in strict import mode. None when the hint
    matches no printing — the caller falls back to the plain name lookup."""
    lang_clause = "AND lang = ?4" if only_lang is not None else ""
    params: tuple = (name, str(collector_number), prefer_lang)
    if only_lang is not None:
        params += (only_lang,)
    return conn.execute(
        f"""
        SELECT * FROM cards
        WHERE {_NAME_MATCH_SQL}
          AND collector_number = ?2
          {lang_clause}
        ORDER BY (lang = ?3) DESC, (lang = 'en') DESC,
                 digital ASC, highres_image DESC,
                 released_at DESC
        LIMIT 1
        """,
        params,
    ).fetchone()


def find_row_by_name(
    conn: sqlite3.Connection,
    name: str,
    prefer_lang: str = "en",
    *,
    only_lang: str | None = None,
) -> sqlite3.Row | None:
    """Exact-name lookup returning the best printing to show for a bare
    name (matched against the English name or a localized printed name):
    preferred language, then English, then paper over digital, high-res
    scans over placeholders, newest release first — or restricted to
    exactly only_lang in strict import mode. Fuzzy matching stays a
    live-API concern on purpose."""
    lang_clause = "AND lang = ?3" if only_lang is not None else ""
    params: tuple = (name, prefer_lang)
    if only_lang is not None:
        params += (only_lang,)
    return conn.execute(
        f"""
        SELECT * FROM cards
        WHERE {_NAME_MATCH_SQL}
          {lang_clause}
        ORDER BY (lang = ?2) DESC, (lang = 'en') DESC,
                 digital ASC, highres_image DESC,
                 released_at DESC
        LIMIT 1
        """,
        params,
    ).fetchone()


def _row_json(row: sqlite3.Row | None) -> dict[str, Any] | None:
    return json.loads(row["card_json"]) if row else None


def get_card_by_id(conn: sqlite3.Connection, scryfall_id: str) -> dict[str, Any] | None:
    return _row_json(find_row_by_id(conn, scryfall_id))


def get_card_by_set_collector(
    conn: sqlite3.Connection,
    set_code: str,
    collector_number: str,
    prefer_lang: str = "en",
    *,
    only_lang: str | None = None,
) -> dict[str, Any] | None:
    return _row_json(
        find_row_by_set_collector(
            conn, set_code, collector_number, prefer_lang, only_lang=only_lang
        )
    )


def get_card_by_name(
    conn: sqlite3.Connection,
    name: str,
    prefer_lang: str = "en",
    *,
    only_lang: str | None = None,
) -> dict[str, Any] | None:
    return _row_json(find_row_by_name(conn, name, prefer_lang, only_lang=only_lang))


def variants_for_oracle_id(
    conn: sqlite3.Connection, oracle_id: str, *, include_digital: bool = False
) -> list[dict[str, Any]]:
    """Every printing sharing an oracle_id — the change-printing dropdown's
    contents. Returns column data (not the card_json blobs; the picker
    doesn't need image URLs) plus a computed face_count (how many images
    one generation of the printing produces per DPI — the blob is read
    here to derive it, then dropped), sorted newest release first, then
    set, then collector number in natural order (so "2" sorts before
    "10"), English before other languages of the same printing."""
    sql = """
        SELECT id, name, printed_name, set_code, set_name, collector_number,
               lang, released_at, digital, image_status, highres_image,
               card_json
        FROM cards WHERE oracle_id = ?
    """
    if not include_digital:
        sql += " AND digital = 0"
    rows = conn.execute(sql, (oracle_id,)).fetchall()

    def sort_key(row: sqlite3.Row):
        return (
            _released_desc_key(row["released_at"]),
            row["set_code"],
            collector_sort_key(row["collector_number"]),
            row["lang"] != "en",
            row["lang"],
        )

    variants = []
    for row in sorted(rows, key=sort_key):
        data = dict(row)
        raw = data.pop("card_json", None)
        try:
            data["face_count"] = expected_face_count(json.loads(raw) if raw else {})
        except (TypeError, ValueError):
            data["face_count"] = 1
        variants.append(data)
    return variants


def _released_desc_key(released_at: str | None) -> tuple:
    # ISO dates compare lexicographically; invert per character to sort
    # descending inside an ascending tuple sort. The leading flag puts
    # rows with no release date last.
    if not released_at:
        return (1, "")
    return (0, "".join(chr(0x10FFFF - ord(c)) for c in released_at))


def collector_sort_key(collector_number: str) -> tuple:
    """Natural sort for collector numbers: numeric runs compare as numbers,
    everything else as text, so 2 < 10 < 100a < 100b < A-1."""
    parts = re.split(r"(\d+)", str(collector_number))
    return tuple(
        (0, int(part)) if part.isdigit() else (1, part.lower())
        for part in parts
        if part != ""
    )
