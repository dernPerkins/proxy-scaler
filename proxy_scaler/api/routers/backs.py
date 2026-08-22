"""Back Image endpoints — the generation server's half of the Back Library.

The library itself is client-side (docs/adr/0003): these routes hold a
content-addressed *cache* of originals the client has synced here, plus
whatever upscales this particular machine has produced. Nothing here is
authoritative — losing all of it costs the user one re-upload and, if they
want it, one re-upscale.

Every route is keyed by content hash rather than by project_tag. A Back
Image belongs to the machine, not to a project, which is also why
`POST /api/tags/{tag}/discard` leaves them alone: a discarded tag has no
claim on a file another project may have selected.
"""

from __future__ import annotations

from pathlib import Path

from fastapi import APIRouter, HTTPException, Request

from proxy_scaler import backs, db
from proxy_scaler.api.deps import get_db_path
from proxy_scaler.api.routers.misc import DEFAULT_WEIGHTS_DIR
from proxy_scaler.api.schemas import (
    BackImageOut,
    BackUpscaleIn,
    BackUpscaleOut,
    BackVariantOut,
    DeleteBackOut,
)
from proxy_scaler.dpi import DPI_OPTIONS
from proxy_scaler.upscale import UpscaleModel

router = APIRouter(prefix="/api/backs", tags=["backs"])


def _status(content_hash: str) -> BackImageOut:
    variants = backs.list_variants(content_hash, db_path=get_db_path())
    dpi = backs.source_dpi(content_hash)
    return BackImageOut(
        content_hash=content_hash,
        present=backs.has_original(content_hash),
        source_dpi=dpi,
        low_resolution=dpi is not None and dpi < backs.MIN_COMFORTABLE_DPI,
        variants=[
            BackVariantOut(
                id=int(v["id"]),
                dpi=int(v["dpi"]),
                model=str(v["model"]),
                created_at=v.get("created_at"),
            )
            for v in variants
        ],
    )


def _checked(content_hash: str) -> str:
    try:
        return backs.validate_hash(content_hash)
    except backs.BackImageError as exc:
        raise HTTPException(status_code=400, detail=str(exc)) from exc


@router.get("/{content_hash}", response_model=BackImageOut)
def get_back(content_hash: str) -> BackImageOut:
    """Does this server have these bytes, and what has it upscaled them to?

    The client calls this before every sync so an unchanged back costs one
    small GET rather than a multi-MB upload — and calls it on server switch
    to find out that its upscales live on the other machine.
    """
    return _status(_checked(content_hash))


@router.post("/{content_hash}", response_model=BackImageOut)
async def upload_back(content_hash: str, request: Request) -> BackImageOut:
    """Sync one Back Image's bytes to this server. Idempotent.

    Takes the raw request body rather than a multipart upload: there is
    exactly one file and no other fields, so multipart would buy nothing
    but a `python-multipart` dependency this project does not otherwise
    have — and FastAPI raises at import time when a Form/File route is
    declared without it, which would turn a missing transitive dependency
    into a server that refuses to boot.

    The hash in the path is checked against the bytes received, so a
    truncated or mismatched upload is rejected rather than being cached
    forever under a name that lies about its contents.
    """
    checked = _checked(content_hash)
    data = await request.body()
    try:
        backs.store_original(data, expected_hash=checked)
    except backs.BackImageError as exc:
        raise HTTPException(status_code=400, detail=str(exc)) from exc
    return _status(checked)


@router.post("/{content_hash}/upscale", response_model=BackUpscaleOut, status_code=202)
def upscale_back(content_hash: str, body: BackUpscaleIn) -> BackUpscaleOut:
    """Queue upscales of a Back Image at one or more DPIs.

    Rides the ordinary generation queue via the synthetic identity in
    backs.py, so this shows up in the Tasks tab, reports progress, and
    cancels like anything else. The original must already be synced — the
    task will find it pre-seeded in its cache and never make a network
    call (docs/adr/0004).
    """
    checked = _checked(content_hash)
    if not backs.has_original(checked):
        raise HTTPException(
            status_code=409, detail="Upload the back image to this server before upscaling it."
        )
    if not body.dpi_targets:
        raise HTTPException(status_code=400, detail="Select at least one target DPI.")
    invalid = [d for d in body.dpi_targets if d not in DPI_OPTIONS]
    if invalid:
        raise HTTPException(status_code=400, detail=f"DPI must be one of {DPI_OPTIONS}.")
    try:
        model = UpscaleModel(body.model)
    except ValueError as exc:
        raise HTTPException(status_code=400, detail=f"Unknown model {body.model!r}.") from exc

    db_path = get_db_path()
    backs_dir = str(backs.backs_dir())
    scryfall_id = backs.synthetic_id(checked)
    task_ids: list[int] = []
    skipped = 0
    for dpi in sorted(set(body.dpi_targets)):
        existing = db.find_generated_image(scryfall_id, None, model.value, dpi, db_path=db_path)
        if existing is not None and Path(existing["out_path"]).is_file():
            skipped += 1
            continue
        task_ids.append(
            db.enqueue_task(
                # No project_tag: a Back Image is app-global, and tagging
                # it would put it in one project's gallery and expose it to
                # that tag's discard.
                None,
                scryfall_id=scryfall_id,
                face_index=None,
                face_label=None,
                face_name="Card back",
                card_name="Card back",
                set_code="back",
                collector_number=checked[:8],
                # Never fetched: the original is already seeded at the
                # exact cache path the pipeline probes first. Recorded so
                # the NOT NULL column has something honest in it.
                png_url=str(backs.original_path(checked)),
                dpi=dpi,
                model=model.value,
                # Both directories are the backs dir, which is what keeps
                # the produced files outside clear_generated_data's reach
                # and outside prune_registry_under_dir's scan.
                output_dir=backs_dir,
                cache_dir=backs_dir,
                weights_dir=body.weights_dir or DEFAULT_WEIGHTS_DIR,
                tile_size=body.tile_size,
                db_path=db_path,
            )
        )
    return BackUpscaleOut(queued=len(task_ids), skipped=skipped, task_ids=task_ids)


@router.delete("/{content_hash}/variants", response_model=DeleteBackOut)
def clear_back_upscales(content_hash: str) -> DeleteBackOut:
    """Drop this back's upscales, keeping the synced original — the action
    that reclaims disk on a GPU box without losing anything unrebuilding."""
    checked = _checked(content_hash)
    removed = backs.delete_variants(checked, db_path=get_db_path())
    return DeleteBackOut(removed=removed)


@router.delete("/{content_hash}", response_model=DeleteBackOut)
def delete_back(content_hash: str) -> DeleteBackOut:
    """Remove a Back Image from this server entirely. The client's own
    library copy is canonical and untouched."""
    checked = _checked(content_hash)
    removed = backs.delete_back(checked, db_path=get_db_path())
    return DeleteBackOut(removed=removed)
