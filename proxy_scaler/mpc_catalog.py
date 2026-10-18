"""The MPC Fill art catalog: a local index of the MPC Fill uploaders'
public Google Drive folders, searched in place of mpcfill.com.

A daily Cloudflare Workflow (workers/mpc-catalog) crawls the folders'
metadata and publishes one gzipped NDJSON shard per source, plus a
manifest, at CATALOG_URL. The format is fixed by
.scratch/mpcfill-art-picker/phase-c/format.md. This module:

- checks the manifest for a newer catalog (check_update), so the client
  can offer the download;
- downloads only the shards whose sha256 changed and swaps them into a
  local SQLite in one transaction (run_import), so searches read the old
  catalog until the new one commits;
- answers art_sources.ArtSource from it (CatalogSource).

Images still come from Google through drive_images.py, exactly as with
live MPC Fill search: the catalog only says which drive ids exist. See
docs/adr/0007-mpc-art-catalog-on-cloudflare-workflows.md.
"""

from __future__ import annotations

import gzip
import hashlib
import json
import os
import re
import sqlite3
import threading
import time
import unicodedata
from dataclasses import dataclass
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Iterator

import requests

from . import mpc_catalog_jobs as jobs
from .art_sources import ArtCard, ArtFilters, ArtQuery, ArtSource, ArtSourceError
from .db import _DATA_ROOT
from .mpcfill import MAXIMUM_DPI, MAXIMUM_SIZE_MB, NSFW_TAG, USER_AGENT

CATALOG_URL = "https://catalog.proxy-scaler.com/"
DEFAULT_DB_PATH = _DATA_ROOT / "mpc_catalog.db"
# The manifest format this build reads (format.md). A newer one is refused
# rather than misread.
MANIFEST_FORMAT = 1
SCHEMA_VERSION = 1

# A manifest is re-read at most this often unless the user asks (the
# manual Update button), so launch-time checks from several windows, or a
# client restarted all afternoon, cost one request.
MANIFEST_TTL_S = 6 * 60 * 60
TIMEOUT_S = (5, 60)
_INSERT_BATCH_ROWS = 5000
# SQLite's default limit on bound parameters is 999.
_ID_CHUNK = 900

CARD_TYPES = frozenset({"CARD", "TOKEN", "CARDBACK"})
_SHARD_PATH = re.compile(r"sources/[A-Za-z0-9._-]+\.ndjson\.gz")
_SHA256 = re.compile(r"[0-9a-f]{64}")

_SCHEMA = """
CREATE TABLE IF NOT EXISTS art (
    drive_id TEXT PRIMARY KEY,
    name TEXT NOT NULL,
    search_name TEXT NOT NULL,
    card_type TEXT NOT NULL,
    extension TEXT NOT NULL,
    language TEXT NOT NULL,
    tags TEXT NOT NULL,
    nsfw INTEGER NOT NULL,
    dpi INTEGER NOT NULL,
    size_bytes INTEGER NOT NULL,
    date_created TEXT NOT NULL,
    source_key TEXT NOT NULL
);
CREATE INDEX IF NOT EXISTS idx_art_search ON art(search_name, card_type);
CREATE INDEX IF NOT EXISTS idx_art_type ON art(card_type);
CREATE INDEX IF NOT EXISTS idx_art_source ON art(source_key);

CREATE TABLE IF NOT EXISTS sources (
    key TEXT PRIMARY KEY,
    name TEXT NOT NULL,
    ord INTEGER NOT NULL,
    sha256 TEXT NOT NULL,
    count INTEGER NOT NULL,
    crawled_at TEXT
);

CREATE TABLE IF NOT EXISTS meta (
    key TEXT PRIMARY KEY,
    value TEXT NOT NULL
);

CREATE VIRTUAL TABLE IF NOT EXISTS art_fts USING fts5(
    search_name, content='art', content_rowid='rowid', tokenize='trigram'
);
"""

# meta keys, written together at the end of a successful import.
META_VERSION = "version"
META_GENERATED_AT = "generated_at"
META_IMPORTED_AT = "imported_at"
META_CARD_COUNT = "card_count"


class CatalogError(RuntimeError):
    """The catalog can't be read or updated right now. The message is
    shown to the user as-is."""


_APOSTROPHES = str.maketrans("", "", "'\u2018\u2019`\u00b4")
_POSSESSIVE_UNDERSCORE = re.compile(r"(?<=\w)_(?=s\b)")


def search_key(text: str) -> str:
    """What names are matched on, for stored names and queries alike:
    lower case, accents folded ("Lim-Dûl" finds "Lim-Dul"), apostrophes
    dropped, and an underscore standing in for one (uploaders' "Mishra_s
    Factory") read as one, so "Mishra's Factory" finds it. Any other
    underscore is a space."""
    folded = unicodedata.normalize("NFKD", text)
    folded = "".join(c for c in folded if not unicodedata.combining(c))
    folded = _POSSESSIVE_UNDERSCORE.sub("", folded.translate(_APOSTROPHES))
    return " ".join(folded.replace("_", " ").lower().split())


# ---------------------------------------------------------------------------
# Paths and connections


def db_path_from_env() -> Path:
    """The catalog file. PROXY_SCALER_MPC_CATALOG_DB_PATH exists for test
    isolation, like PROXY_SCALER_CARD_DB_PATH (api/deps.py)."""
    override = os.environ.get("PROXY_SCALER_MPC_CATALOG_DB_PATH")
    return Path(override) if override else DEFAULT_DB_PATH


def catalog_url() -> str:
    """Where the crawler publishes. Overridable for a dev bucket prefix."""
    url = os.environ.get("PROXY_SCALER_MPC_CATALOG_URL") or CATALOG_URL
    return url if url.endswith("/") else url + "/"


def _raw_connect(path: Path) -> sqlite3.Connection:
    path.parent.mkdir(parents=True, exist_ok=True)
    conn = sqlite3.connect(str(path), check_same_thread=False)
    conn.row_factory = sqlite3.Row
    # WAL so searches keep reading the previous catalog while an update
    # writes the new one.
    conn.execute("PRAGMA journal_mode = WAL")
    conn.execute("PRAGMA synchronous = NORMAL")
    return conn


def _init(path: Path) -> sqlite3.Connection:
    """Open for writing, creating or resetting the schema. The catalog is
    rebuildable from the manifest, so an unknown schema version is simply
    dropped and re-downloaded."""
    conn = _raw_connect(path)
    version = conn.execute("PRAGMA user_version").fetchone()[0]
    if version != SCHEMA_VERSION:
        for table in ("art_fts", "art", "sources", "meta"):
            conn.execute(f"DROP TABLE IF EXISTS {table}")
    conn.executescript(_SCHEMA)
    conn.execute(f"PRAGMA user_version = {SCHEMA_VERSION}")
    conn.commit()
    return conn


def open_if_ready(path: Path | None = None) -> sqlite3.Connection | None:
    """A connection to a complete catalog, else None — never creating a
    file as a side effect. "Complete" means a finished import wrote its
    version; an interrupted first import has none."""
    path = path or db_path_from_env()
    if not path.is_file():
        return None
    try:
        conn = _raw_connect(path)
        if conn.execute("PRAGMA user_version").fetchone()[0] != SCHEMA_VERSION:
            conn.close()
            return None
        row = conn.execute("SELECT value FROM meta WHERE key = ?", (META_VERSION,)).fetchone()
    except sqlite3.Error:
        return None
    if row is None:
        conn.close()
        return None
    return conn


def get_meta(conn: sqlite3.Connection) -> dict[str, str]:
    return {r["key"]: r["value"] for r in conn.execute("SELECT key, value FROM meta")}


def local_status(path: Path | None = None) -> dict[str, Any] | None:
    """What's imported, from local state alone (no network): version,
    generated_at, imported_at, card_count and source_count, or None."""
    conn = open_if_ready(path)
    if conn is None:
        return None
    try:
        meta = get_meta(conn)
        sources = conn.execute("SELECT COUNT(*) FROM sources").fetchone()[0]
    finally:
        conn.close()
    try:
        return {
            "version": meta[META_VERSION],
            "generated_at": meta.get(META_GENERATED_AT),
            "imported_at": meta[META_IMPORTED_AT],
            "card_count": int(meta[META_CARD_COUNT]),
            "source_count": int(sources),
        }
    except (KeyError, ValueError):
        return None


def delete_catalog(path: Path | None = None) -> bool:
    """Remove the catalog (file and WAL/SHM sidecars). Search falls back
    to live mpcfill.com afterwards. Returns whether a file existed."""
    path = path or db_path_from_env()
    existed = path.exists()
    for target in (path, path.with_name(path.name + "-wal"), path.with_name(path.name + "-shm")):
        target.unlink(missing_ok=True)
    return existed


# ---------------------------------------------------------------------------
# Manifest


@dataclass(frozen=True)
class ManifestSource:
    key: str
    name: str
    order: int
    path: str
    sha256: str
    bytes: int
    count: int
    crawled_at: str | None


@dataclass(frozen=True)
class Manifest:
    version: str
    generated_at: str | None
    sources: tuple[ManifestSource, ...]


def parse_manifest(data: Any) -> Manifest:
    """A validated manifest. Shard paths and hashes are checked here because
    they become a URL and an integrity check; anything malformed is refused
    whole rather than half-imported."""
    if not isinstance(data, dict):
        raise CatalogError("The art catalog sent an unreadable manifest.")
    fmt = data.get("format")
    if fmt != MANIFEST_FORMAT:
        if isinstance(fmt, int) and fmt > MANIFEST_FORMAT:
            raise CatalogError(
                "The MPC Fill catalog needs a newer Proxy Scaler. Update to use it."
            )
        raise CatalogError("The art catalog sent a manifest in an unknown format.")
    version = data.get("version")
    raw_sources = data.get("sources")
    if not isinstance(version, str) or not version or not isinstance(raw_sources, list):
        raise CatalogError("The art catalog sent an incomplete manifest.")
    sources: list[ManifestSource] = []
    seen: set[str] = set()
    for raw in raw_sources:
        try:
            source = ManifestSource(
                key=str(raw["key"]),
                name=str(raw.get("name") or raw["key"]),
                order=int(raw["order"]),
                path=str(raw["path"]),
                sha256=str(raw["sha256"]).lower(),
                bytes=int(raw["bytes"]),
                count=int(raw["count"]),
                crawled_at=raw.get("crawled_at") if isinstance(raw.get("crawled_at"), str) else None,
            )
        except (KeyError, TypeError, ValueError) as exc:
            raise CatalogError("The art catalog sent an incomplete manifest.") from exc
        if (
            not source.key
            or source.key in seen
            or not _SHARD_PATH.fullmatch(source.path)
            or not _SHA256.fullmatch(source.sha256)
        ):
            raise CatalogError("The art catalog sent an invalid manifest.")
        seen.add(source.key)
        sources.append(source)
    generated_at = data.get("generated_at")
    return Manifest(
        version=version,
        generated_at=generated_at if isinstance(generated_at, str) else None,
        sources=tuple(sources),
    )


_manifest_lock = threading.Lock()
_manifest_cache: tuple[float, str, Manifest] | None = None


def _reset_for_tests() -> None:
    global _manifest_cache
    with _manifest_lock:
        _manifest_cache = None


def _session(session: requests.Session | None) -> requests.Session:
    if session is not None:
        return session
    sess = requests.Session()
    sess.headers.update({"User-Agent": USER_AGENT})
    return sess


def fetch_manifest(
    session: requests.Session | None = None, *, refresh: bool = False
) -> Manifest:
    """The published manifest, cached in memory for MANIFEST_TTL_S."""
    global _manifest_cache
    base = catalog_url()
    with _manifest_lock:
        cached = _manifest_cache
    if (
        not refresh
        and cached is not None
        and cached[1] == base
        and time.monotonic() - cached[0] < MANIFEST_TTL_S
    ):
        return cached[2]
    try:
        resp = _session(session).get(
            base + "manifest.json", headers={"User-Agent": USER_AGENT}, timeout=TIMEOUT_S
        )
        resp.raise_for_status()
        data = resp.json()
    except (requests.RequestException, ValueError) as exc:
        raise CatalogError("Couldn't reach the MPC Fill catalog.") from exc
    manifest = parse_manifest(data)
    with _manifest_lock:
        _manifest_cache = (time.monotonic(), base, manifest)
    return manifest


@dataclass(frozen=True)
class UpdateCheck:
    remote_version: str
    local_version: str | None
    # Sources to download (new or changed) and to drop.
    changed_sources: int
    removed_sources: int
    download_bytes: int
    card_count: int

    @property
    def update_available(self) -> bool:
        return self.local_version != self.remote_version and (
            self.changed_sources > 0 or self.removed_sources > 0 or self.local_version is None
        )


def _local_hashes(path: Path) -> tuple[str | None, dict[str, str]]:
    conn = open_if_ready(path)
    if conn is None:
        return None, {}
    try:
        version = get_meta(conn).get(META_VERSION)
        hashes = {r["key"]: r["sha256"] for r in conn.execute("SELECT key, sha256 FROM sources")}
    finally:
        conn.close()
    return version, hashes


def _plan(manifest: Manifest, local: dict[str, str]) -> tuple[list[ManifestSource], list[str]]:
    changed = [s for s in manifest.sources if local.get(s.key) != s.sha256]
    listed = {s.key for s in manifest.sources}
    removed = [key for key in local if key not in listed]
    return changed, removed


def check_update(
    path: Path | None = None,
    session: requests.Session | None = None,
    *,
    refresh: bool = False,
) -> UpdateCheck:
    """Compare the published manifest with what's imported. Raises
    CatalogError when the manifest can't be read."""
    path = path or db_path_from_env()
    manifest = fetch_manifest(session, refresh=refresh)
    local_version, hashes = _local_hashes(path)
    changed, removed = _plan(manifest, hashes)
    return UpdateCheck(
        remote_version=manifest.version,
        local_version=local_version,
        changed_sources=len(changed),
        removed_sources=len(removed),
        download_bytes=sum(s.bytes for s in changed),
        card_count=sum(s.count for s in manifest.sources),
    )


# ---------------------------------------------------------------------------
# Import


def _check_cancel(job_id: str) -> None:
    if jobs.is_cancel_requested(job_id):
        raise jobs.CatalogImportCanceled()


def _download_shard(
    job_id: str,
    source: ManifestSource,
    session: requests.Session,
    progress: list[int],
    total: int,
) -> bytes:
    resp = _session(session).get(
        catalog_url() + source.path,
        headers={"User-Agent": USER_AGENT},
        timeout=TIMEOUT_S,
        stream=True,
    )
    resp.raise_for_status()
    digest = hashlib.sha256()
    chunks: list[bytes] = []
    for chunk in resp.iter_content(chunk_size=256 * 1024):
        _check_cancel(job_id)
        if not chunk:
            continue
        digest.update(chunk)
        chunks.append(chunk)
        progress[0] += len(chunk)
        jobs.update(job_id, bytes_downloaded=progress[0], total_bytes=total)
    if digest.hexdigest() != source.sha256:
        raise CatalogError(
            f"The catalog's copy of {source.name} didn't match its checksum. "
            "Try again later."
        )
    return b"".join(chunks)


def _iter_records(blob: bytes) -> Iterator[dict[str, Any]]:
    for line in gzip.decompress(blob).decode("utf-8").splitlines():
        line = line.strip()
        if not line:
            continue
        try:
            record = json.loads(line)
        except ValueError:
            continue
        if isinstance(record, dict):
            yield record


def record_row(record: dict[str, Any], source_key: str) -> tuple | None:
    """One `art` row from a shard record, or None for an unusable record
    (format.md: missing id/name/type are skipped; unknown fields ignored)."""
    drive_id = record.get("id")
    name = record.get("name")
    card_type = record.get("type")
    if not isinstance(drive_id, str) or not drive_id:
        return None
    if not isinstance(name, str) or not name.strip() or card_type not in CARD_TYPES:
        return None
    raw_tags = record.get("tags")
    tags = [str(t) for t in raw_tags] if isinstance(raw_tags, list) else []

    def _int(key: str) -> int:
        try:
            return int(record.get(key) or 0)
        except (TypeError, ValueError):
            return 0

    return (
        drive_id,
        name.strip(),
        search_key(name),
        card_type,
        str(record.get("ext") or "").lower(),
        str(record.get("lang") or "EN").upper(),
        json.dumps(tags),
        int(NSFW_TAG in tags),
        _int("dpi"),
        _int("size"),
        str(record.get("created") or ""),
        source_key,
    )


def _write_sources(
    job_id: str,
    conn: sqlite3.Connection,
    manifest: Manifest,
    shards: list[tuple[ManifestSource, bytes]],
    removed: list[str],
) -> int:
    """Swap the changed sources' rows in, drop the removed ones, refresh
    every source's name/order and the FTS index, in the caller's
    transaction."""
    rows_written = 0
    for key in removed:
        conn.execute("DELETE FROM art WHERE source_key = ?", (key,))
        conn.execute("DELETE FROM sources WHERE key = ?", (key,))
    for source, blob in shards:
        conn.execute("DELETE FROM art WHERE source_key = ?", (source.key,))
        batch: list[tuple] = []
        for record in _iter_records(blob):
            row = record_row(record, source.key)
            if row is None:
                continue
            batch.append(row)
            if len(batch) >= _INSERT_BATCH_ROWS:
                _check_cancel(job_id)
                rows_written += _insert(conn, batch)
                batch.clear()
                jobs.update(job_id, rows_imported=rows_written)
        rows_written += _insert(conn, batch)
        jobs.update(job_id, rows_imported=rows_written)
        conn.execute(
            "INSERT INTO sources (key, name, ord, sha256, count, crawled_at) "
            "VALUES (?, ?, ?, ?, ?, ?) ON CONFLICT(key) DO UPDATE SET "
            "sha256 = excluded.sha256, count = excluded.count, "
            "crawled_at = excluded.crawled_at",
            (source.key, source.name, source.order, source.sha256, source.count, source.crawled_at),
        )
    # Names and order can change without a source's files changing.
    conn.executemany(
        "UPDATE sources SET name = ?, ord = ? WHERE key = ?",
        [(s.name, s.order, s.key) for s in manifest.sources],
    )
    _check_cancel(job_id)
    conn.execute("INSERT INTO art_fts(art_fts) VALUES ('rebuild')")
    return rows_written


def _insert(conn: sqlite3.Connection, rows: list[tuple]) -> int:
    if not rows:
        return 0
    # A drive id listed by two sources stays with the first one written.
    before = conn.total_changes
    conn.executemany(
        "INSERT INTO art (drive_id, name, search_name, card_type, extension, language, "
        "tags, nsfw, dpi, size_bytes, date_created, source_key) "
        "VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?) ON CONFLICT(drive_id) DO NOTHING",
        rows,
    )
    return conn.total_changes - before


def run_import(
    job_id: str,
    *,
    path: Path | None = None,
    session: requests.Session | None = None,
) -> None:
    """Import thread body. Owns the job's terminal state, like
    card_import.run_import: every exit marks the job."""
    path = path or db_path_from_env()
    sess = _session(session)
    try:
        jobs.update(job_id, phase=jobs.CHECKING)
        manifest = fetch_manifest(sess, refresh=True)
        _version, hashes = _local_hashes(path)
        changed, removed = _plan(manifest, hashes)
        total = sum(s.bytes for s in changed)
        jobs.update(job_id, phase=jobs.DOWNLOADING, bytes_downloaded=0, total_bytes=total)
        progress = [0]
        shards = [(s, _download_shard(job_id, s, sess, progress, total)) for s in changed]

        jobs.update(job_id, phase=jobs.IMPORTING)
        conn = _init(path)
        try:
            conn.execute("BEGIN IMMEDIATE")
            try:
                _write_sources(job_id, conn, manifest, shards, removed)
                count = conn.execute("SELECT COUNT(*) FROM art").fetchone()[0]
                conn.executemany(
                    "INSERT INTO meta (key, value) VALUES (?, ?) "
                    "ON CONFLICT(key) DO UPDATE SET value = excluded.value",
                    [
                        (META_VERSION, manifest.version),
                        (META_GENERATED_AT, manifest.generated_at or ""),
                        (META_IMPORTED_AT, datetime.now(timezone.utc).isoformat()),
                        (META_CARD_COUNT, str(count)),
                    ],
                )
                conn.execute("COMMIT")
            except BaseException:
                conn.execute("ROLLBACK")
                raise
        finally:
            conn.close()
        jobs.finish(job_id)
    except jobs.CatalogImportCanceled:
        jobs.mark_canceled(job_id)
    except CatalogError as exc:
        jobs.fail(job_id, str(exc))
    except requests.RequestException:
        jobs.fail(job_id, "Couldn't download the MPC Fill catalog. Try again later.")
    except Exception as exc:  # noqa: BLE001 — must reach the client as a status
        jobs.fail(job_id, str(exc))


# ---------------------------------------------------------------------------
# Search


def _filter_sql(filters: ArtFilters) -> tuple[str, list[Any]]:
    """The WHERE fragment for MPC Fill's filters, with its parameters. The
    size and DPI ceilings are MPC Fill's own defaults, so results match
    what live search returned."""
    clauses = ["a.dpi >= ?", "a.dpi <= ?", "a.size_bytes <= ?"]
    params: list[Any] = [max(0, filters.min_dpi), MAXIMUM_DPI, MAXIMUM_SIZE_MB * 1024 * 1024]
    if filters.languages:
        clauses.append(f"a.language IN ({', '.join('?' for _ in filters.languages)})")
        params.extend(lang.upper() for lang in filters.languages)
    if not filters.include_nsfw:
        clauses.append("a.nsfw = 0")
    return " AND ".join(clauses), params


def _fts_phrase(query: str) -> str:
    return '"' + query.replace('"', '""') + '"'


class CatalogSource:
    """ArtSource backed by the local catalog. Details for a drive id the
    catalog doesn't hold (art picked from a source with no public folder,
    before the catalog existed) come from `fallback`, so a pick already
    in a project still shows."""

    def __init__(self, path: Path, fallback: ArtSource | None = None) -> None:
        self.path = path
        self.fallback = fallback

    def _connect(self) -> sqlite3.Connection:
        conn = open_if_ready(self.path)
        if conn is None:
            raise ArtSourceError("The MPC Fill catalog isn't available. Update it and try again.")
        return conn

    def search(
        self, queries: list[ArtQuery], filters: ArtFilters, *, refresh: bool = False
    ) -> dict[ArtQuery, list[str]]:
        where, params = _filter_sql(filters)
        out: dict[ArtQuery, list[str]] = {}
        conn = self._connect()
        try:
            for q in queries:
                name = search_key(q.query)
                if not name:
                    out[q] = []
                    continue
                # Exact names, as MPC Fill's precise search. Fuzzy adds any
                # name containing the query (trigram FTS needs 3+ chars).
                match = "a.search_name = ?"
                match_params: list[Any] = [name]
                if filters.fuzzy and len(name) >= 3:
                    match = (
                        "(a.search_name = ? OR a.rowid IN "
                        "(SELECT rowid FROM art_fts WHERE art_fts MATCH ?))"
                    )
                    match_params.append(_fts_phrase(name))
                rows = conn.execute(
                    "SELECT a.drive_id FROM art a JOIN sources s ON s.key = a.source_key "
                    f"WHERE a.card_type = ? AND {match} AND {where} "
                    "ORDER BY (a.search_name = ?) DESC, s.ord, a.dpi DESC, a.date_created DESC",
                    [q.card_type, *match_params, *params, name],
                ).fetchall()
                out[q] = [r["drive_id"] for r in rows]
        finally:
            conn.close()
        return out

    def cards(self, drive_ids: list[str]) -> dict[str, ArtCard]:
        wanted = list(dict.fromkeys(drive_ids))
        found: dict[str, ArtCard] = {}
        conn = self._connect()
        try:
            for start in range(0, len(wanted), _ID_CHUNK):
                chunk = wanted[start : start + _ID_CHUNK]
                rows = conn.execute(
                    "SELECT a.*, s.name AS source_name FROM art a "
                    "JOIN sources s ON s.key = a.source_key "
                    f"WHERE a.drive_id IN ({', '.join('?' for _ in chunk)})",
                    chunk,
                ).fetchall()
                for row in rows:
                    found[row["drive_id"]] = _art_card(row)
        finally:
            conn.close()
        missing = [i for i in wanted if i not in found]
        if missing and self.fallback is not None:
            try:
                found.update(self.fallback.cards(missing))
            except ArtSourceError:
                pass
        return {i: found[i] for i in drive_ids if i in found}

    def cardbacks(self, filters: ArtFilters, *, refresh: bool = False) -> list[str]:
        where, params = _filter_sql(filters)
        conn = self._connect()
        try:
            rows = conn.execute(
                "SELECT a.drive_id FROM art a JOIN sources s ON s.key = a.source_key "
                f"WHERE a.card_type = 'CARDBACK' AND {where} "
                "ORDER BY s.ord, a.dpi DESC, a.date_created DESC",
                params,
            ).fetchall()
        finally:
            conn.close()
        return [r["drive_id"] for r in rows]


def _art_card(row: sqlite3.Row) -> ArtCard:
    try:
        tags = tuple(str(t) for t in json.loads(row["tags"]))
    except (TypeError, ValueError):
        tags = ()
    return ArtCard(
        drive_id=row["drive_id"],
        name=row["name"],
        card_type=row["card_type"],
        extension=row["extension"],
        dpi=int(row["dpi"]),
        size_bytes=int(row["size_bytes"]),
        language=row["language"],
        source_name=row["source_name"],
        date_created=row["date_created"],
        tags=tags,
    )


def is_ready(path: Path | None = None) -> bool:
    conn = open_if_ready(path)
    if conn is None:
        return False
    conn.close()
    return True

