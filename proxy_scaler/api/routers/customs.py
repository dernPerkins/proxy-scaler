"""Custom Image endpoints — the generation server's half of the custom
card-front library.

The library itself is client-side (see proxy_scaler/customs.py): these
routes hold a content-addressed *cache* of the bytes the client has synced
here. Nothing here is authoritative — losing all of it costs the user one
re-upload, which is exactly what lets the client defer uploading until the
server genuinely needs the file (a generate run, or an export).

Every route is keyed by content hash rather than by project_tag. A Custom
Image belongs to the machine, not to a project — the same file dropped
into two projects is one upload and one upscale — which is also why
`POST /api/tags/{tag}/discard` leaves them alone.

Unlike Back Images, Custom Images *are* upscaled: they are card fronts, and
the generation database identifies them by content hash instead of a
Scryfall UUID. See proxy_scaler/customs.py for why that asymmetry is
deliberate.
"""

from __future__ import annotations

from pathlib import Path

from fastapi import APIRouter, HTTPException, Query, Request

from proxy_scaler import customs
from proxy_scaler.api.deps import get_db_path
from proxy_scaler.api.routers.misc import DEFAULT_CACHE_DIR
from proxy_scaler.api.schemas import CustomImageOut, DeleteCustomOut
from proxy_scaler.dpi import MAX_BLEED_MM
from proxy_scaler.pipeline import invalidate_custom_derivatives

router = APIRouter(prefix="/api/customs", tags=["customs"])


def _status(content_hash: str) -> CustomImageOut:
    dpi = customs.source_dpi(content_hash)
    return CustomImageOut(
        content_hash=content_hash,
        present=customs.has_original(content_hash),
        source_dpi=dpi,
        low_resolution=dpi is not None and dpi < customs.MIN_COMFORTABLE_DPI,
        bleed_mm=customs.read_bleed_mm(content_hash),
    )


def _checked(content_hash: str) -> str:
    try:
        return customs.validate_hash(content_hash)
    except customs.CustomImageError as exc:
        raise HTTPException(status_code=400, detail=str(exc)) from exc


@router.get("/{content_hash}", response_model=CustomImageOut)
def get_custom(content_hash: str) -> CustomImageOut:
    """Does this server have these bytes, and how sharp are they?

    The client calls this before every generate run and export so an
    already-synced image costs one small GET rather than a multi-MB
    upload — and on server switch, to find out the new host needs filling.
    """
    return _status(_checked(content_hash))


@router.post("/{content_hash}", response_model=CustomImageOut)
async def upload_custom(
    content_hash: str,
    request: Request,
    bleed_mm: float = Query(
        0.0,
        ge=0,
        le=MAX_BLEED_MM,
        description="Bleed (mm per side) the file already carries, by the user's declaration.",
    ),
) -> CustomImageOut:
    """Sync one Custom Image's bytes to this server. Idempotent for the
    same bytes under the same declared bleed.

    A different `bleed_mm` for a hash already stored re-crops the PNG to
    the new geometry and discards everything derived from the old one
    (registry rows, upscales, cache) — they were sized to the wrong box.

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
    previous = customs.read_bleed_mm(checked) if customs.has_original(checked) else None
    try:
        customs.store_original(data, expected_hash=checked, bleed_mm=bleed_mm)
    except customs.CustomImageError as exc:
        raise HTTPException(status_code=400, detail=str(exc)) from exc
    if previous is not None and not customs.same_bleed(previous, bleed_mm):
        invalidate_custom_derivatives(
            checked, db_path=get_db_path(), default_cache_dir=Path(DEFAULT_CACHE_DIR)
        )
    return _status(checked)


@router.delete("/{content_hash}", response_model=DeleteCustomOut)
def delete_custom(content_hash: str) -> DeleteCustomOut:
    """Remove a Custom Image from this server. The client's own library
    copy is canonical and untouched."""
    checked = _checked(content_hash)
    return DeleteCustomOut(removed=customs.delete_custom(checked))
