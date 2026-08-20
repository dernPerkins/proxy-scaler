"""Download and import a Scryfall bulk card dump into the local corpus.

The dump is discovered via Scryfall's bulk catalog (GET /bulk-data): each
dataset entry advertises a `jsonl_download_uri` (gzipped JSONL, one card
object per line, refreshed daily), its `updated_at`, and `compressed_size`.
Two datasets matter here: "default_cards" (~77MB gz — every printing, in
English or its only printed language) and "all_cards" (~392MB gz — every
printing in every language). The user picks one per import.

run_import() is a thread body driven by routers/cards.py the same way
routers/pdf.py drives _run_render: it owns the job's terminal state, and
notices cancellation at chunk/batch boundaries via card_jobs. Rows are
pruned (prune_card) down to what scryfall.expand_faces() and the client UI
consume before storage — the full objects would quadruple the database for
fields (legalities, prices, oracle text) nothing here reads.
"""

from __future__ import annotations

import gzip
import json
import shutil
from dataclasses import dataclass
from pathlib import Path
from typing import Any, Iterator

import requests

from . import card_jobs, carddb
from .card_jobs import CardImportCanceled
from .db import _DATA_ROOT
from .scryfall import USER_AGENT

BULK_DATA_URL = "https://api.scryfall.com/bulk-data"
VALID_DATASETS = ("default_cards", "all_cards")

_DOWNLOAD_CHUNK_BYTES = 1024 * 1024
_UPSERT_BATCH_ROWS = 5000

# Disk guardrails, as multiples of the advertised compressed size: the
# download needs the compressed file itself (plus slack), and the SQLite
# corpus lands at very roughly 2-3x compressed once pruned — 4x is the
# comfortable "refuse before wedging the machine" line, not a measurement.
_TMP_FREE_FACTOR = 1.2
_DB_FREE_FACTOR = 4.0


@dataclass(frozen=True)
class BulkDatasetInfo:
    dataset: str
    updated_at: str
    download_uri: str
    compressed_size: int


def fetch_bulk_catalog(
    session: requests.Session | None = None, timeout: float = 10.0
) -> dict[str, BulkDatasetInfo]:
    """The current bulk catalog, keyed by dataset type, for the datasets we
    support. One call answers both the import job and the staleness hint."""
    sess = session or requests.Session()
    resp = sess.get(
        BULK_DATA_URL,
        headers={"User-Agent": USER_AGENT, "Accept": "application/json"},
        timeout=timeout,
    )
    resp.raise_for_status()
    catalog: dict[str, BulkDatasetInfo] = {}
    for entry in resp.json().get("data", []):
        dataset = entry.get("type")
        if dataset not in VALID_DATASETS:
            continue
        uri = entry.get("jsonl_download_uri")
        if not uri:
            continue
        catalog[dataset] = BulkDatasetInfo(
            dataset=dataset,
            updated_at=str(entry.get("updated_at", "")),
            download_uri=uri,
            compressed_size=int(entry.get("compressed_size", 0)),
        )
    return catalog


def fetch_bulk_info(
    dataset: str, session: requests.Session | None = None, timeout: float = 10.0
) -> BulkDatasetInfo:
    catalog = fetch_bulk_catalog(session, timeout=timeout)
    info = catalog.get(dataset)
    if info is None:
        raise RuntimeError(
            f"Scryfall's bulk catalog has no downloadable '{dataset}' entry."
        )
    return info


_FACE_KEYS = ("name", "oracle_id", "printed_name")


def prune_card(card: dict[str, Any]) -> dict[str, Any]:
    """Slim a raw bulk card object down to the fields expand_faces() and the
    client consume. The pruned dict must remain shape-compatible with a live
    API card object — that is the whole trick that lets local rows feed the
    existing resolve/generate pipeline untouched."""
    pruned: dict[str, Any] = {}
    for key in (
        "id",
        "oracle_id",
        "name",
        "printed_name",
        "lang",
        "set",
        "set_name",
        "collector_number",
        "released_at",
        "layout",
        "digital",
        "image_status",
        "highres_image",
    ):
        if key in card:
            pruned[key] = card[key]
    png = (card.get("image_uris") or {}).get("png")
    if png:
        pruned["image_uris"] = {"png": png}
    faces = card.get("card_faces")
    if isinstance(faces, list) and faces:
        pruned_faces = []
        for face in faces:
            if not isinstance(face, dict):
                continue
            pruned_face = {k: face[k] for k in _FACE_KEYS if k in face}
            face_png = (face.get("image_uris") or {}).get("png")
            if face_png:
                pruned_face["image_uris"] = {"png": face_png}
            pruned_faces.append(pruned_face)
        if pruned_faces:
            pruned["card_faces"] = pruned_faces
    return pruned


def _check_cancel(job_id: str) -> None:
    if card_jobs.is_cancel_requested(job_id):
        raise CardImportCanceled()


def _check_disk_space(info: BulkDatasetInfo, tmp_dir: Path, db_dir: Path) -> None:
    need_tmp = int(info.compressed_size * _TMP_FREE_FACTOR)
    need_db = int(info.compressed_size * _DB_FREE_FACTOR)
    free_tmp = shutil.disk_usage(tmp_dir).free
    if free_tmp < need_tmp:
        raise RuntimeError(
            f"Not enough disk space for the download: {tmp_dir} has "
            f"{free_tmp // 1_000_000}MB free, need about {need_tmp // 1_000_000}MB."
        )
    free_db = shutil.disk_usage(db_dir).free
    if free_db < need_db:
        raise RuntimeError(
            f"Not enough disk space for the card database: {db_dir} has "
            f"{free_db // 1_000_000}MB free, need about {need_db // 1_000_000}MB."
        )


def _download(
    job_id: str, info: BulkDatasetInfo, dest: Path, session: requests.Session
) -> None:
    """Stream the dump to `dest` — same .part-file idiom as
    upscale.ensure_weights, but the caller passes the .part path directly
    and deletes it in its own `finally`, so no atomic rename is needed:
    the file never outlives the import attempt either way."""
    resp = session.get(
        info.download_uri,
        headers={"User-Agent": USER_AGENT},
        timeout=120,
        stream=True,
    )
    resp.raise_for_status()
    total = info.compressed_size or None
    downloaded = 0
    with open(dest, "wb") as fh:
        for chunk in resp.iter_content(chunk_size=_DOWNLOAD_CHUNK_BYTES):
            _check_cancel(job_id)
            if not chunk:
                continue
            fh.write(chunk)
            downloaded += len(chunk)
            card_jobs.set_download_progress(job_id, downloaded, total)


def _iter_cards(path: Path) -> Iterator[dict[str, Any]]:
    with gzip.open(path, "rt", encoding="utf-8") as fh:
        for line in fh:
            line = line.strip()
            if not line:
                continue
            card = json.loads(line)
            if not isinstance(card, dict) or "id" not in card:
                continue
            yield card


def _import_rows(job_id: str, dump_path: Path, conn) -> int:
    imported = 0
    batch: list[dict[str, Any]] = []
    for card in _iter_cards(dump_path):
        batch.append(prune_card(card))
        if len(batch) >= _UPSERT_BATCH_ROWS:
            _check_cancel(job_id)
            imported += carddb.upsert_cards(conn, batch)
            batch.clear()
            card_jobs.set_import_progress(job_id, imported)
    if batch:
        _check_cancel(job_id)
        imported += carddb.upsert_cards(conn, batch)
        card_jobs.set_import_progress(job_id, imported)
    return imported


def run_import(
    job_id: str,
    dataset: str,
    *,
    card_db_path: Path | str | None = None,
    tmp_dir: Path | str | None = None,
    session: requests.Session | None = None,
) -> None:
    """Import thread body. Owns the job's terminal state: every exit path
    (success, cancel, failure) marks the job, or the client would poll a
    running job forever."""
    db_path = Path(card_db_path) if card_db_path else carddb.DEFAULT_CARD_DB_PATH
    tmp = Path(tmp_dir) if tmp_dir else _DATA_ROOT
    tmp.mkdir(parents=True, exist_ok=True)
    dump_path = tmp / "scryfall_bulk.jsonl.gz.part"
    sess = session or requests.Session()
    try:
        card_jobs.set_phase(job_id, card_jobs.CHECKING)
        info = fetch_bulk_info(dataset, sess)
        card_jobs.set_download_progress(job_id, 0, info.compressed_size or None)
        db_path.parent.mkdir(parents=True, exist_ok=True)
        _check_disk_space(info, tmp, db_path.parent)
        _check_cancel(job_id)

        card_jobs.set_phase(job_id, card_jobs.DOWNLOADING)
        _download(job_id, info, dump_path, sess)

        card_jobs.set_phase(job_id, card_jobs.IMPORTING)
        carddb.init_card_db(db_path)
        conn = carddb.connect(db_path)
        try:
            # Big page cache for the duration of the bulk write; connection-
            # scoped, so normal readers are unaffected.
            conn.execute("PRAGMA cache_size = -64000")
            previous = carddb.get_meta(conn).get(carddb.META_DATASET_TYPE)
            if previous is not None and previous != dataset:
                # Switching datasets: upserting a smaller dump over a larger
                # corpus would strand the old rows, so start clean. VACUUM
                # while empty is near-instant and gives the file's space
                # back before the new rows arrive.
                carddb.delete_all_cards(conn)
                conn.execute("VACUUM")
            _import_rows(job_id, dump_path, conn)

            card_jobs.set_phase(job_id, card_jobs.FINALIZING)
            # Meta last, and only on full success — an interrupted import
            # leaves stale/absent meta so the staleness hint stays honest,
            # and the next run idempotently re-upserts everything.
            carddb.write_import_meta(
                conn, dataset_type=dataset, dataset_updated_at=info.updated_at
            )
        finally:
            conn.close()
        card_jobs.finish(job_id)
    except CardImportCanceled:
        card_jobs.mark_canceled(job_id)
    except Exception as exc:  # noqa: BLE001 — must reach the client as a status
        card_jobs.fail(job_id, str(exc))
    finally:
        dump_path.unlink(missing_ok=True)
