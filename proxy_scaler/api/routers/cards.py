"""Card-corpus endpoints: bulk import lifecycle (status/start/poll/cancel),
the languages present in the corpus, and the printing-variants listing that
feeds the client's change-printing picker. All of it reads the local corpus
(carddb.py) — the only live Scryfall call here is the small bulk-catalog
fetch inside /status, and its failure is reported as "unknown", never as an
error, because the client renders the staleness hint offline too."""

from __future__ import annotations

import sqlite3
import threading

from fastapi import APIRouter, HTTPException, Response

from proxy_scaler import card_import, card_jobs, carddb
from proxy_scaler.api.deps import get_card_db_path
from proxy_scaler.api.schemas import (
    CardDbLocalOut,
    CardDbRemoteEntryOut,
    CardDbStatusOut,
    CardImportIn,
    CardImportStartedOut,
    CardImportStatusOut,
    CardLanguagesOut,
    CardVariantOut,
    CardVariantsOut,
)

router = APIRouter(prefix="/api/cards", tags=["cards"])

_IMPORT_HINT = (
    "No local card database — import it from the Card database panel first."
)


def _local_status() -> CardDbLocalOut | None:
    conn = carddb.open_if_ready(get_card_db_path())
    if conn is None:
        return None
    try:
        meta = carddb.get_meta(conn)
    finally:
        conn.close()
    try:
        return CardDbLocalOut(
            dataset_type=meta[carddb.META_DATASET_TYPE],
            dataset_updated_at=meta[carddb.META_DATASET_UPDATED_AT],
            imported_at=meta[carddb.META_IMPORTED_AT],
            card_count=int(meta[carddb.META_CARD_COUNT]),
        )
    except (KeyError, ValueError):
        # A corpus file without complete meta is an interrupted first
        # import — same as no corpus, as far as the staleness hint goes.
        return None


@router.get("/status", response_model=CardDbStatusOut)
def card_db_status() -> CardDbStatusOut:
    remote: dict[str, CardDbRemoteEntryOut] | None
    try:
        catalog = card_import.fetch_bulk_catalog(timeout=5.0)
        remote = {
            dataset: CardDbRemoteEntryOut(
                updated_at=info.updated_at, compressed_size=info.compressed_size
            )
            for dataset, info in catalog.items()
        }
    except Exception:  # noqa: BLE001 — offline is a normal state here
        remote = None
    active = card_jobs.active_job()
    return CardDbStatusOut(
        local=_local_status(),
        remote=remote,
        import_running=active is not None,
        active_job_id=active.id if active else None,
    )


@router.post("/import", response_model=CardImportStartedOut, status_code=202)
def start_card_import(body: CardImportIn) -> CardImportStartedOut:
    if body.dataset not in card_import.VALID_DATASETS:
        raise HTTPException(
            status_code=422,
            detail=f"Unknown dataset {body.dataset!r} — expected one of "
            f"{', '.join(card_import.VALID_DATASETS)}.",
        )
    # One import at a time: it monopolizes bandwidth/disk, and two threads
    # upserting the same corpus buys nothing but lock contention.
    if card_jobs.active_job() is not None:
        raise HTTPException(
            status_code=409,
            detail="A card database import is already running — wait for it to finish.",
        )
    job = card_jobs.create_job(dataset=body.dataset)
    threading.Thread(
        target=card_import.run_import,
        args=(job.id, body.dataset),
        kwargs={"card_db_path": get_card_db_path()},
        daemon=True,
    ).start()
    return CardImportStartedOut(job_id=job.id)


@router.get("/import/{job_id}", response_model=CardImportStatusOut)
def card_import_status(job_id: str) -> CardImportStatusOut:
    job = card_jobs.get(job_id)
    if job is None:
        raise HTTPException(status_code=404, detail="Unknown or expired import job.")
    return CardImportStatusOut(
        status=job.status,
        phase=job.phase,
        dataset=job.dataset,
        bytes_downloaded=job.bytes_downloaded,
        total_bytes=job.total_bytes,
        rows_imported=job.rows_imported,
        error=job.error,
    )


@router.post("/import/{job_id}/cancel", status_code=204)
def cancel_card_import(job_id: str) -> Response:
    if not card_jobs.request_cancel(job_id):
        raise HTTPException(status_code=404, detail="Unknown or expired import job.")
    return Response(status_code=204)


@router.get("/languages", response_model=CardLanguagesOut)
def card_languages() -> CardLanguagesOut:
    conn = carddb.open_if_ready(get_card_db_path())
    if conn is None:
        return CardLanguagesOut(languages=["en"])
    try:
        langs = carddb.distinct_languages(conn)
    finally:
        conn.close()
    return CardLanguagesOut(languages=langs or ["en"])


def _variant_out(row) -> CardVariantOut:
    return CardVariantOut(
        scryfall_id=row["id"],
        name=row["name"],
        set_code=row["set_code"],
        set_name=row["set_name"],
        collector_number=row["collector_number"],
        lang=row["lang"],
        released_at=row["released_at"],
        digital=bool(row["digital"]),
        image_status=row["image_status"],
        highres_image=bool(row["highres_image"]),
    )


@router.get("/variants", response_model=CardVariantsOut)
def card_variants(
    scryfall_id: str | None = None,
    set_code: str | None = None,
    collector_number: str | None = None,
    name: str | None = None,
    include_digital: bool = False,
) -> CardVariantsOut:
    """Every printing of one card, anchored by whatever identity the caller
    has: scryfall_id when the card is already pinned, set+collector for
    legacy rows, bare name as the last resort. Deliberately no live-API
    fallback — enumerating printings is exactly what the local corpus
    exists for, so its absence is reported as such."""
    conn = carddb.open_if_ready(get_card_db_path())
    if conn is None:
        raise HTTPException(status_code=404, detail=_IMPORT_HINT)
    try:
        anchor: sqlite3.Row | None = None
        if scryfall_id:
            anchor = carddb.find_row_by_id(conn, scryfall_id)
        if anchor is None and set_code and collector_number:
            anchor = carddb.find_row_by_set_collector(conn, set_code, collector_number)
        if anchor is None and name:
            anchor = carddb.find_row_by_name(conn, name)
        if anchor is None:
            raise HTTPException(
                status_code=404,
                detail="Card not found in the local card database — it may be "
                "newer than the last import.",
            )
        if not anchor["oracle_id"]:
            variants = [dict(anchor)]
        else:
            variants = carddb.variants_for_oracle_id(
                conn, anchor["oracle_id"], include_digital=include_digital
            )
    finally:
        conn.close()
    return CardVariantsOut(
        anchor=_variant_out(anchor),
        variants=[_variant_out(v) for v in variants],
        total=len(variants),
    )
