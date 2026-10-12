"""Card thumbnails for the change-printing picker's image grid.

A thumbnail is Scryfall's `normal` JPEG of one face of one printing,
derived from the png URL the card corpus already stores (no schema
change): `/png/` becomes `/normal/`, `.png` becomes `.jpg`, and the
`?<version>` suffix is kept. It is fetched through the request's image
origin (scryfall.download_png) and cached on disk under the generation
server's cache directory, in `thumbs/`, keyed by scryfall_id, face and
version suffix — so a hit never touches the network, and a corrected scan
picked up by the next corpus import is a miss that fetches again. Old
versions are left behind: there is no size cap and no eviction.
"""

from __future__ import annotations

import re
from pathlib import Path
from typing import Any
from urllib.parse import urlsplit, urlunsplit

from .scryfall import ScryfallError, download_png, expand_faces
from .upscale import atomic_write_bytes

THUMBS_DIRNAME = "thumbs"

# Filenames are built from these; anything else in an id or a version
# would be a path component nobody asked for.
_SAFE_PART = re.compile(r"[^A-Za-z0-9_-]")


class NoSuchFace(LookupError):
    """The printing has no image for the requested face (face=1 on a
    single-faced card, or a card Scryfall has no PNG for at all)."""


def face_png_url(card: dict[str, Any], face: int) -> str:
    """The stored png URL of face `face` (0 = front) of a corpus card.
    Faces are counted the way Generate counts them (expand_faces): a DFC
    has one per face with its own image, everything else just the one."""
    try:
        faces = expand_faces(card)
    except ScryfallError as exc:
        raise NoSuchFace(str(exc)) from exc
    if face >= len(faces):
        raise NoSuchFace(f"{card.get('name')} has no face {face}")
    return faces[face].png_url


def normal_jpeg_url(png_url: str) -> str:
    """Scryfall's `normal`-size JPEG for the same image as `png_url`."""
    parts = urlsplit(png_url)
    path = parts.path.replace("/png/", "/normal/", 1)
    if path.endswith(".png"):
        path = path[: -len(".png")] + ".jpg"
    return urlunsplit((parts.scheme, parts.netloc, path, parts.query, ""))


def thumb_cache_path(cache_dir: Path, scryfall_id: str, face: int, png_url: str) -> Path:
    version = _SAFE_PART.sub("", urlsplit(png_url).query) or "0"
    stem = _SAFE_PART.sub("", scryfall_id)
    return cache_dir / THUMBS_DIRNAME / f"{stem}_face{face}_v{version}.jpg"


def get_thumbnail(
    card: dict[str, Any], face: int, *, cache_dir: Path, origin: str
) -> bytes:
    """The JPEG bytes of one face's thumbnail, from the disk cache or
    fetched through `origin` and cached. Raises NoSuchFace, or
    ScryfallError when the image can't be fetched from anywhere."""
    png_url = face_png_url(card, face)
    path = thumb_cache_path(cache_dir, card["id"], face, png_url)
    if path.is_file():
        return path.read_bytes()
    data = download_png(normal_jpeg_url(png_url), origin=origin)
    path.parent.mkdir(parents=True, exist_ok=True)
    # Atomic: the grid requests many thumbnails at once, and two requests
    # for the same face may race; both write complete bytes.
    atomic_write_bytes(path, data)
    return data
