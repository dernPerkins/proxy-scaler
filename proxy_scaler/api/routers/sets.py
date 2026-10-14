"""Set symbols: the SVG icon for a set code, served from the on-disk
cache (set_icons.py) and fetched from Scryfall on a miss."""

from __future__ import annotations

from pathlib import Path

from fastapi import APIRouter, HTTPException, Response

from proxy_scaler import set_icons
from proxy_scaler.api.routers.misc import DEFAULT_CACHE_DIR

router = APIRouter(prefix="/api/sets", tags=["sets"])

# A week: an icon never changes under its set code, but a client shouldn't
# hold a 404's absence forever either, and that only costs a disk read.
_ICON_CACHE = "public, max-age=604800"
# The client only ever uses these as CSS masks, where nothing in the SVG
# runs. This keeps it that way for anyone who opens the URL directly.
_ICON_CSP = "default-src 'none'; style-src 'unsafe-inline'"


@router.get("/{code}/icon")
def set_icon(code: str) -> Response:
    """Set `code`'s symbol as Scryfall's monochrome SVG. 404 for a code
    Scryfall doesn't list, or an uncached icon while offline."""
    data = set_icons.get_set_icon(code, Path(DEFAULT_CACHE_DIR))
    if data is None:
        raise HTTPException(status_code=404, detail=f"No set symbol for {code!r}.")
    return Response(
        content=data,
        media_type="image/svg+xml",
        headers={
            "Cache-Control": _ICON_CACHE,
            "Content-Security-Policy": _ICON_CSP,
            "X-Content-Type-Options": "nosniff",
        },
    )
