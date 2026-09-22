"""Custom Images: user-supplied art printed on a card's *front*.

A Custom Image is a card — that is the whole difference from a Back Image
(see backs.py). It occupies a row in the decklist, carries a quantity, and
goes through the upscale pipeline like any Scryfall printing. What it does
not have is a Scryfall identity, so the generation database identifies it
by the sha256 of its bytes instead: every task and registry row carries
either a `scryfall_id` or a `custom_hash`, never both (see db.py migration
008).

Like the Back Library, the canonical copy lives on the *client* and this
module is only the generation server's content-addressed cache: the
desktop app syncs bytes here on demand (before a generate run, before an
export) and losing all of it costs the user one re-upload. That is what
makes "don't upload unless the server actually needs it" implementable
without the server ever being authoritative for a file it may never see.

Two deliberate asymmetries with backs.py:

**Upscaling is allowed, and is the point.** backs.py explains at length why
Back Images are never upscaled — an uploaded back is usually flat design
where upscaling buys least, and running them through the pipeline would
have meant inventing a synthetic Scryfall identity. Custom fronts are the
opposite case: they are card art, upscaling is exactly what this
application is for, and rather than fake a UUID the identity is made
explicit and typed all the way down.

**Uploads are cover-cropped to card aspect on the way in.** Scryfall art is
already 63:88; a user's file is whatever they had. Cropping here, once,
means the upscaler, the PDF renderer and the ZIP export all see a
card-shaped image and none of them need a special case. The client keeps
the uncropped file, so nothing is destroyed.

A file the user declares as *already carrying bleed* (an MPC Fill image is
63x88 plus 3.175 mm per side) is cropped to the bled aspect instead, and
the declared amount is recorded in a JSON sidecar next to the PNG. That
amount is the one thing downstream must know about a custom front: the
upscaler sizes its output to the bled box, and the renderer trims the
file's own bleed down to the project's (or tops it up) rather than
extending a second border. The client owns the declaration; re-uploading
with a different amount re-crops and discards everything derived from the
old geometry (see api/routers/customs.py).

The directory is a sibling of `output/` and `imgcache/`, never inside
them — `clear_generated_data` empties those two and
`prune_registry_under_dir` drops registry rows found under `output/`.
Living outside both is what makes "Custom Images survive the wipe" a
property of where the files are rather than a condition somebody has to
remember to re-check.
"""

from __future__ import annotations

import hashlib
import io
import json
import math
import re
from collections.abc import Iterable
from pathlib import Path
from typing import TYPE_CHECKING

from PIL import Image

if TYPE_CHECKING:  # pragma: no cover
    from proxy_scaler.pipeline import FaceResult

# Relative name, resolved against the server process's cwd exactly like
# DEFAULT_OUTPUT_DIR / DEFAULT_CACHE_DIR in api/routers/misc.py.
CUSTOMS_DIR_NAME = "customs"

_HASH_RE = re.compile(r"^[0-9a-f]{64}$")

# Formats accepted on upload. Everything is normalised to PNG on the way
# in, because a JPEG hiding behind a .png name would work only by PIL's
# content sniffing — true today, and a trap the first time anything reads
# the extension instead.
ACCEPTED_FORMATS = {"PNG", "JPEG", "WEBP"}
MAX_UPLOAD_BYTES = 50 * 1024 * 1024

# Below this, a Custom Image is being asked to cover a 63×88mm card with
# less detail than a decent printer resolves. Warned about, never blocked —
# and unlike a Back Image, the user has a real remedy here beyond finding a
# better file: upscale it.
MIN_COMFORTABLE_DPI = 300

# Two declared bleeds closer than this are the same declaration: the
# amount round-trips through a JSON sidecar, a query string and an f64 on
# the client, and "3.175 == 3.175000001" must not trigger a re-crop.
BLEED_TOLERANCE_MM = 1e-6


class CustomImageError(ValueError):
    """Rejected upload — message is user-facing."""


def validate_hash(content_hash: str) -> str:
    """Lowercase hex sha256, or raise. Called on every path that takes a
    hash from a request — it reaches the filesystem, so it is never
    trusted enough to interpolate unchecked."""
    normalized = (content_hash or "").strip().lower()
    if not _HASH_RE.match(normalized):
        raise CustomImageError("Custom image id must be a lowercase hex sha256.")
    return normalized


def customs_dir(root: Path | str = CUSTOMS_DIR_NAME) -> Path:
    return Path(root)


def original_path(content_hash: str, *, root: Path | str = CUSTOMS_DIR_NAME) -> Path:
    """Where a synced Custom Image lives: one flat, hash-named PNG.

    The hash names the bytes the *client* uploaded; the file itself is the
    normalised, cover-cropped PNG derived from them. Same shape as backs.py
    — the hash is an identity, not a checksum of what is on disk here.
    """
    return customs_dir(root) / f"{validate_hash(content_hash)}.png"


def has_original(content_hash: str, *, root: Path | str = CUSTOMS_DIR_NAME) -> bool:
    return original_path(content_hash, root=root).is_file()


def store_original(
    data: bytes,
    *,
    root: Path | str = CUSTOMS_DIR_NAME,
    expected_hash: str | None = None,
    bleed_mm: float = 0.0,
) -> tuple[str, Path]:
    """Validate an uploaded image, cover-crop it to card aspect (or to the
    bled aspect when `bleed_mm` > 0), and store it content-addressed with
    the declared bleed in a sidecar.

    Returns (content_hash, path). Idempotent: re-uploading identical bytes
    under the same declared bleed is a no-op that returns the existing
    path, which is what makes the client's "sync on miss" cheap to call
    unconditionally. The same bytes under a *different* bleed re-crop and
    overwrite — the stored PNG's geometry is a function of both.
    """
    if not data:
        raise CustomImageError("Custom image upload was empty.")
    if len(data) > MAX_UPLOAD_BYTES:
        mb = MAX_UPLOAD_BYTES // (1024 * 1024)
        raise CustomImageError(f"Custom image is larger than the {mb}MB limit.")
    bleed_mm = validate_bleed_mm(bleed_mm)

    content_hash = hashlib.sha256(data).hexdigest()
    if expected_hash is not None and validate_hash(expected_hash) != content_hash:
        raise CustomImageError(
            "Custom image contents do not match the id they were sent under."
        )

    try:
        with Image.open(io.BytesIO(data)) as probe:
            image_format = (probe.format or "").upper()
            probe.load()
            rgb = probe.convert("RGB")
    except CustomImageError:
        raise
    except Exception as exc:  # noqa: BLE001 — any decode failure is one rejection
        raise CustomImageError("Custom image could not be read as an image file.") from exc

    if image_format not in ACCEPTED_FORMATS:
        raise CustomImageError(
            f"Custom image must be PNG, JPEG or WebP (got {image_format or 'unknown'})."
        )

    dest = original_path(content_hash, root=root)
    if dest.is_file() and same_bleed(read_bleed_mm(content_hash, root=root), bleed_mm):
        return content_hash, dest

    cropped = crop_to_card_aspect(rgb, bleed_mm=bleed_mm)

    dest.parent.mkdir(parents=True, exist_ok=True)
    # Sidecar first, PNG second: has_original() keys on the PNG, so a PNG
    # with a stale sidecar would be served under the wrong geometry, while
    # a sidecar with no PNG is simply "not present".
    write_bleed_mm(content_hash, bleed_mm, root=root)
    # Write-then-rename: a half-written file at a content-addressed path
    # is worse than no file, because has_original() would report it
    # present forever after.
    tmp = dest.with_name(dest.name + ".part")
    cropped.save(tmp, format="PNG")
    tmp.replace(dest)
    return content_hash, dest


def validate_bleed_mm(bleed_mm: float) -> float:
    """A declared bleed, as a float in [0, MAX_BLEED_MM]."""
    from proxy_scaler.dpi import MAX_BLEED_MM

    try:
        value = float(bleed_mm)
    except (TypeError, ValueError) as exc:
        raise CustomImageError("Bleed amount must be a number of millimetres.") from exc
    if not math.isfinite(value) or value < 0 or value > MAX_BLEED_MM:
        raise CustomImageError(
            f"Bleed amount must be between 0 and {MAX_BLEED_MM:g} mm per side."
        )
    return value


def same_bleed(a: float, b: float) -> bool:
    return abs(a - b) <= BLEED_TOLERANCE_MM


def sidecar_path(content_hash: str, *, root: Path | str = CUSTOMS_DIR_NAME) -> Path:
    """The JSON sidecar recording what the stored PNG was cropped to."""
    return Path(root) / f"{validate_hash(content_hash)}.json"


def read_bleed_mm(content_hash: str, *, root: Path | str = CUSTOMS_DIR_NAME) -> float:
    """The bleed (mm per side) the stored PNG carries by declaration. 0.0
    when there is no sidecar — every upload made before sidecars existed
    was cropped to plain card aspect, which is exactly what 0.0 means."""
    path = sidecar_path(content_hash, root=root)
    try:
        value = json.loads(path.read_text(encoding="utf-8")).get("bleed_mm", 0.0)
        return validate_bleed_mm(value)
    except (OSError, ValueError, AttributeError, CustomImageError):
        return 0.0


def write_bleed_mm(
    content_hash: str, bleed_mm: float, *, root: Path | str = CUSTOMS_DIR_NAME
) -> None:
    path = sidecar_path(content_hash, root=root)
    path.parent.mkdir(parents=True, exist_ok=True)
    tmp = path.with_name(path.name + ".part")
    tmp.write_text(json.dumps({"bleed_mm": float(bleed_mm)}), encoding="utf-8")
    tmp.replace(path)


def crop_to_card_aspect(image: Image.Image, bleed_mm: float = 0.0) -> Image.Image:
    """Cover-crop to 63:88 — or to (63+2b):(88+2b) for a file declared to
    carry b mm of bleed — keeping as many source pixels as possible.

    Imported locally rather than at module scope: pdf_layout pulls in fpdf2
    and the rest of the PDF renderer, and a storage module has no business
    depending on that just to reuse fifteen lines of PIL. Same idiom as
    backs.py's local dpi import.
    """
    from proxy_scaler.dpi import card_aspect_crop_size
    from proxy_scaler.pdf_layout import fit_cover

    return fit_cover(image, card_aspect_crop_size(*image.size, bleed_mm=bleed_mm))


def source_dpi(content_hash: str, *, root: Path | str = CUSTOMS_DIR_NAME) -> float | None:
    """Effective print DPI of the stored original at card size, or None if
    it isn't stored. What the low-resolution warning is computed from, and
    what the `custom_source` registry row records as its dpi."""
    path = original_path(content_hash, root=root)
    if not path.is_file():
        return None
    from proxy_scaler.dpi import dpi_at_card_size

    with Image.open(path) as img:
        return dpi_at_card_size(*img.size, bleed_mm=read_bleed_mm(content_hash, root=root))


def delete_custom(content_hash: str, *, root: Path | str = CUSTOMS_DIR_NAME) -> int:
    """Remove a Custom Image from this server. The client's own library
    copy is canonical and untouched — re-syncing it is a single upload."""
    path = original_path(content_hash, root=root)
    sidecar_path(content_hash, root=root).unlink(missing_ok=True)
    if not path.is_file():
        return 0
    path.unlink()
    return 1


def attach_bleed(
    items: Iterable[FaceResult], *, root: Path | str = CUSTOMS_DIR_NAME
) -> None:
    """Stamp every custom face's declared bleed onto it, from the sidecar.

    The renderers (pdf_layout._bled_card, the export ZIP, the page
    preview) need it and the registry does not store it: it is a property
    of the stored PNG, not of any one generated variant, and the sidecar
    is its single source of truth on this server. One read per distinct
    hash per call.
    """
    seen: dict[str, float] = {}
    for item in items:
        if not item.custom_hash:
            continue
        if item.custom_hash not in seen:
            seen[item.custom_hash] = read_bleed_mm(item.custom_hash, root=root)
        item.custom_bleed_mm = seen[item.custom_hash]


# Prefix for the string form of a Custom Image's identity. Everything that
# needs one key for "which face is this" — registry lookups, the unique
# index, the PDF face-grouping, the client's status merge — uses
# identity_key() below rather than reaching for scryfall_id directly.
CUSTOM_ID_PREFIX = "custom:"


def identity_key(scryfall_id: str | None, custom_hash: str | None) -> str:
    """The one string that identifies a face, whichever kind it is.

    Mirrored by the desktop client (mergeCardStatus.ts::cardIdentity) and
    by the generated_images unique index, which builds the same expression
    in SQL. All three have to agree or a generated image stops matching the
    card that asked for it.
    """
    if scryfall_id:
        return scryfall_id
    if custom_hash:
        return f"{CUSTOM_ID_PREFIX}{custom_hash}"
    raise ValueError("A face must have either a scryfall_id or a custom_hash.")


def is_custom_identity(key: str) -> bool:
    return key.startswith(CUSTOM_ID_PREFIX)
