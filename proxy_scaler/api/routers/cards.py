"""Card-corpus endpoints: bulk import lifecycle (status/start/poll/cancel),
the languages present in the corpus, and the printing-variants listing that
feeds the client's change-printing picker. Every read here answers from the
local corpus (carddb.py). Two things here touch the network: the import job
itself (card_import.run_import's CHECKING phase), which is where the user
has actually asked for fresh data, and a thumbnail cache miss, which fetches
one image through the request's image origin (thumbs.py)."""

from __future__ import annotations

import json
import sqlite3
import threading
from pathlib import Path

import requests
from fastapi import APIRouter, HTTPException, Query, Response

from proxy_scaler import card_import, card_jobs, carddb, thumbs
from proxy_scaler.api.deps import get_card_db_path
from proxy_scaler.api.routers.misc import DEFAULT_CACHE_DIR
from proxy_scaler.scryfall import (
    DEFAULT_IMAGE_ORIGIN,
    SCRYFALL_LANGUAGES,
    ImageOrigin,
    ScryfallError,
    expected_face_count,
    card_image_version,
    printable_face_names,
)
from proxy_scaler.api.schemas import (
    CardDbLocalOut,
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
    """Is a usable corpus imported, and is an import running right now —
    answered entirely from local state: a file-existence + schema-version
    check and a four-row meta read.

    This deliberately makes no network call. It used to fetch Scryfall's
    bulk catalog too, so the sidebar could size the download and say "newer
    data exists" — but this endpoint sits on the client's LAUNCH path and
    polls every 60s, so that put a live HTTPS round-trip (a full 5s stall
    when offline) in front of an answer that is a stat() away. Deciding
    when to re-import is the user's call, made from the sidebar; the import
    job fetches the catalog itself when they make it."""
    active = card_jobs.active_job()
    return CardDbStatusOut(
        local=_local_status(),
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


@router.delete("/database", status_code=204)
def delete_card_database() -> Response:
    """Remove the imported corpus entirely — the sidebar panel's "Delete
    card database". Refused while an import is writing to it."""
    if card_jobs.active_job() is not None:
        raise HTTPException(
            status_code=409,
            detail="A card database import is running — cancel it first.",
        )
    carddb.delete_card_db(get_card_db_path())
    return Response(status_code=204)


@router.get("/languages", response_model=CardLanguagesOut)
def card_languages() -> CardLanguagesOut:
    """Every language the import dropdown may request — the full Scryfall
    list, independent of what corpus (if any) is imported. The dropdown
    expresses what the user *wants*; resolution then answers from the
    corpus or the live API, and the strict import mode reports per-card
    errors when a language genuinely doesn't exist for a card."""
    return CardLanguagesOut(languages=list(SCRYFALL_LANGUAGES))


def _variant_out(row) -> CardVariantOut:
    # variants_for_oracle_id rows already computed face_count (and dropped
    # the card_json blob); an anchor is a raw corpus row (SELECT *), so
    # its count is derived from card_json here instead.
    if "face_count" in row.keys():
        face_count = int(row["face_count"])
        face_names = list(row["face_names"])
        version = row["image_version"]
    else:
        try:
            card = json.loads(row["card_json"] or "{}")
            face_count = expected_face_count(card)
            face_names = printable_face_names(card)
            version = card_image_version(card)
        except (IndexError, KeyError, TypeError, ValueError):
            face_count = 1
            face_names = [row["name"]]
            version = None
    return CardVariantOut(
        scryfall_id=row["id"],
        name=row["name"],
        printed_name=row["printed_name"],
        set_code=row["set_code"],
        set_name=row["set_name"],
        collector_number=row["collector_number"],
        lang=row["lang"],
        released_at=row["released_at"],
        digital=bool(row["digital"]),
        image_status=row["image_status"],
        highres_image=bool(row["highres_image"]),
        face_count=face_count,
        face_names=face_names,
        image_version=version,
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


# A URL naming the image version it serves (`v`, from the variants
# response) never changes meaning, so it caches forever: a scan Scryfall
# replaces arrives with a new version, hence a new URL. A URL whose `v` the
# corpus no longer has (the client's list predates a re-import) gets the
# current image, which nothing may keep under that stale URL. No `v` at
# all (an older client): a day, so a corrected scan still gets through.
_THUMB_CACHE_IMMUTABLE = "public, max-age=31536000, immutable"
_THUMB_CACHE_STALE_VERSION = "no-cache"
_THUMB_CACHE_UNVERSIONED = "public, max-age=86400"


@router.get("/{scryfall_id}/thumb")
def card_thumb(
    scryfall_id: str,
    face: int = Query(0, ge=0, le=1),
    via: ImageOrigin = DEFAULT_IMAGE_ORIGIN,
    v: str | None = None,
) -> Response:
    """One face of one printing as Scryfall's `normal` JPEG — the image
    grid's tile. Served from the disk cache when present, else fetched
    through `via` (the client's image-origin choice) and cached; see
    thumbs.py. 404 for an id the corpus doesn't hold or a face the
    printing doesn't have; no corpus at all answers like /variants."""
    conn = carddb.open_if_ready(get_card_db_path())
    if conn is None:
        raise HTTPException(status_code=404, detail=_IMPORT_HINT)
    try:
        card = carddb.get_card_by_id(conn, scryfall_id)
    finally:
        conn.close()
    if card is None:
        raise HTTPException(
            status_code=404, detail="Card not found in the local card database."
        )
    try:
        data, version = thumbs.get_thumbnail(
            card, face, cache_dir=Path(DEFAULT_CACHE_DIR), origin=via
        )
    except thumbs.NoSuchFace as exc:
        raise HTTPException(status_code=404, detail=str(exc)) from exc
    except (ScryfallError, requests.RequestException) as exc:
        # Offline, or the image cache and Scryfall both failed: the grid
        # shows its placeholder tile, which still picks the printing.
        raise HTTPException(status_code=502, detail=str(exc)) from exc
    if v is None:
        cache_control = _THUMB_CACHE_UNVERSIONED
    elif v == version:
        cache_control = _THUMB_CACHE_IMMUTABLE
    else:
        cache_control = _THUMB_CACHE_STALE_VERSION
    return Response(
        content=data,
        media_type="image/jpeg",
        headers={"Cache-Control": cache_control},
    )
