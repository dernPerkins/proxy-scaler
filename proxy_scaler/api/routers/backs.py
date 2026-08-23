"""Back Image endpoints — the generation server's half of the Back Library.

The library itself is client-side (docs/adr/0003): these routes hold a
content-addressed *cache* of the bytes the client has synced here.
Nothing here is authoritative — losing all of it costs the user one
re-upload.

Every route is keyed by content hash rather than by project_tag. A Back
Image belongs to the machine, not to a project, which is also why
`POST /api/tags/{tag}/discard` leaves them alone: a discarded tag has no
claim on a file another project may have selected.

Back Images are never upscaled — see the module comment in
proxy_scaler/backs.py for why that asymmetry with card art is deliberate.
"""

from __future__ import annotations

from fastapi import APIRouter, HTTPException, Request

from proxy_scaler import backs
from proxy_scaler.api.schemas import BackImageOut, DeleteBackOut

router = APIRouter(prefix="/api/backs", tags=["backs"])


def _status(content_hash: str) -> BackImageOut:
    dpi = backs.source_dpi(content_hash)
    return BackImageOut(
        content_hash=content_hash,
        present=backs.has_original(content_hash),
        source_dpi=dpi,
        low_resolution=dpi is not None and dpi < backs.MIN_COMFORTABLE_DPI,
    )


def _checked(content_hash: str) -> str:
    try:
        return backs.validate_hash(content_hash)
    except backs.BackImageError as exc:
        raise HTTPException(status_code=400, detail=str(exc)) from exc


@router.get("/{content_hash}", response_model=BackImageOut)
def get_back(content_hash: str) -> BackImageOut:
    """Does this server have these bytes, and how sharp are they?

    The client calls this before every sync so an unchanged back costs one
    small GET rather than a multi-MB upload — and on server switch, to
    find out the new host needs filling.
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


@router.delete("/{content_hash}", response_model=DeleteBackOut)
def delete_back(content_hash: str) -> DeleteBackOut:
    """Remove a Back Image from this server. The client's own library copy
    is canonical and untouched."""
    checked = _checked(content_hash)
    return DeleteBackOut(removed=backs.delete_back(checked))
