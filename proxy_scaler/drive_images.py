"""Community card art bytes, straight from Google Drive.

Every MPC Fill image is a public Google Drive file, and Google serves it
anonymously (no API key) two ways: its image server, which can also
resize (`lh3.googleusercontent.com/d/{id}=…`), and Drive's download
endpoint. This module is the only place those are fetched, always from
the generation server — the user's own machine — so each user's traffic
comes from their own IP and nobody shares a rate limit with anyone else.
MPC Fill itself is never contacted (see art_sources.py).

- thumbnail(): `=h400` / `=h800` JPEGs for the picker grid, cached on
  disk under `<cache_dir>/mpcfill/thumbs/` and refreshed monthly (Drive
  files can be replaced in place under the same id).
- download_original(): the uploaded file byte-for-byte (`=s0`), falling
  back to Drive's download endpoint; one attempt each, size-capped, and
  sniffed so a quota or error page is never mistaken for an image.
"""

from __future__ import annotations

import re
import threading
import time
from pathlib import Path

import requests

from .art_sources import CACHE_DIRNAME, USER_AGENT
from .upscale import atomic_write_bytes

LH3_BASE = "https://lh3.googleusercontent.com/d"
DRIVE_DOWNLOAD_URL = "https://drive.usercontent.google.com/download"

THUMB_SIZES = {"small": "h400", "large": "h800"}
THUMBS_DIRNAME = "thumbs"
THUMB_REFRESH_S = 30 * 24 * 60 * 60
# The grid asks for a page of tiles at once; FastAPI's threadpool would
# otherwise send all of them to Google together.
MAX_CONCURRENT_THUMBS = 6

# Same cap as an uploaded Custom/Back Image: the bytes end up stored the
# same way.
MAX_ORIGINAL_BYTES = 50 * 1024 * 1024
_TIMEOUT_S = (5, 60)
_CHUNK = 1024 * 1024

_DRIVE_ID = re.compile(r"^[A-Za-z0-9_-]{20,80}$")

_session: requests.Session | None = None
_session_lock = threading.Lock()
_thumb_slots = threading.BoundedSemaphore(MAX_CONCURRENT_THUMBS)


class DriveImageError(RuntimeError):
    """The image couldn't be fetched; the message is user-facing."""


def _reset_for_tests() -> None:
    global _session
    with _session_lock:
        _session = None


def _shared_session() -> requests.Session:
    global _session
    with _session_lock:
        if _session is None:
            sess = requests.Session()
            sess.headers.update({"User-Agent": USER_AGENT})
            _session = sess
        return _session


def validate_drive_id(drive_id: str) -> str:
    """A Google Drive file id, or raise ValueError. Ids reach URLs and
    filenames, so they are never interpolated unchecked."""
    drive_id = (drive_id or "").strip()
    if not _DRIVE_ID.match(drive_id):
        raise ValueError("Not a Google Drive file id.")
    return drive_id


def image_format(data: bytes) -> str | None:
    """"png", "jpeg" or "webp" by magic bytes, else None."""
    if data.startswith(b"\x89PNG\r\n\x1a\n"):
        return "png"
    if data.startswith(b"\xff\xd8\xff"):
        return "jpeg"
    if len(data) >= 12 and data[:4] == b"RIFF" and data[8:12] == b"WEBP":
        return "webp"
    return None


def thumb_cache_path(cache_dir: Path, drive_id: str, size: str) -> Path:
    return Path(cache_dir) / CACHE_DIRNAME / THUMBS_DIRNAME / f"{drive_id}_{size}.jpg"


def thumbnail(drive_id: str, size: str, *, cache_dir: Path) -> bytes:
    """A JPEG preview of the art, `size` "small" (400 px tall) or "large"
    (800 px tall). Raises ValueError for a bad id or size, DriveImageError
    when Google won't serve it."""
    drive_id = validate_drive_id(drive_id)
    if size not in THUMB_SIZES:
        raise ValueError(f"Unknown thumbnail size {size!r}.")
    path = thumb_cache_path(cache_dir, drive_id, size)
    try:
        if time.time() - path.stat().st_mtime < THUMB_REFRESH_S:
            return path.read_bytes()
    except OSError:
        pass
    url = f"{LH3_BASE}/{drive_id}={THUMB_SIZES[size]}-rj"
    with _thumb_slots:
        try:
            resp = _shared_session().get(url, timeout=_TIMEOUT_S)
        except requests.RequestException as exc:
            raise DriveImageError(f"Couldn't reach Google for the preview ({exc}).") from exc
    if not resp.ok or image_format(resp.content) is None:
        raise DriveImageError(f"Google wouldn't serve a preview (HTTP {resp.status_code}).")
    path.parent.mkdir(parents=True, exist_ok=True)
    atomic_write_bytes(path, resp.content)
    return resp.content


def _fetch_original(url: str) -> bytes:
    """Stream `url` into memory, refusing anything over the cap or that
    isn't an image. Raises DriveImageError with the reason."""
    sess = _shared_session()
    try:
        with sess.get(url, timeout=_TIMEOUT_S, stream=True) as resp:
            if resp.status_code in (403, 429):
                raise DriveImageError(
                    "Google Drive is refusing downloads of this file right now "
                    "(its daily limit). Try again later or pick other art."
                )
            if resp.status_code == 404:
                raise DriveImageError("This file is no longer on Google Drive.")
            if not resp.ok:
                raise DriveImageError(f"Google Drive answered HTTP {resp.status_code}.")
            chunks: list[bytes] = []
            total = 0
            for chunk in resp.iter_content(_CHUNK):
                total += len(chunk)
                if total > MAX_ORIGINAL_BYTES:
                    raise DriveImageError(
                        f"The file is larger than {MAX_ORIGINAL_BYTES // (1024 * 1024)} MB."
                    )
                chunks.append(chunk)
    except requests.RequestException as exc:
        raise DriveImageError(f"Couldn't reach Google Drive ({exc.__class__.__name__}).") from exc
    data = b"".join(chunks)
    if image_format(data) is None:
        # Quota-exceeded and removed-file responses can be HTML pages
        # with a 200 status.
        raise DriveImageError(
            "Google Drive sent a page instead of the image (the file may be over "
            "its daily download limit or no longer shared). Try again later or "
            "pick other art."
        )
    return data


def download_original(drive_id: str) -> tuple[bytes, str]:
    """The art's original bytes and format ("png" / "jpeg" / "webp"). The
    image server first (fastest), Drive's download endpoint second; one
    attempt each. Raises ValueError for a bad id, DriveImageError (with
    the most useful of the two reasons) when both fail."""
    drive_id = validate_drive_id(drive_id)
    urls = [
        f"{LH3_BASE}/{drive_id}=s0",
        f"{DRIVE_DOWNLOAD_URL}?id={drive_id}&export=download",
    ]
    errors: list[DriveImageError] = []
    for url in urls:
        try:
            data = _fetch_original(url)
        except DriveImageError as exc:
            print(f"MPC art download failed from {url.split('/')[2]}: {exc}", flush=True)
            errors.append(exc)
            continue
        fmt = image_format(data)
        assert fmt is not None
        return data, fmt
    # The download endpoint's reason ("daily limit", "no longer on Drive")
    # is more specific than the image server's bare 500.
    raise errors[-1]
