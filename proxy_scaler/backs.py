"""Back Images: the user-supplied art printed on a card's Reverse.

A Back Image is not a card. It has no Scryfall identity, never appears in
a decklist, and its canonical copy lives on the *client* (see
docs/adr/0003). This module is the generation server's half: a
content-addressed cache of the bytes, and nothing more.

**Back Images are deliberately never upscaled.** The obvious symmetry with
card art is a trap: an uploaded back is whatever file the user chose, and
the honest fix for a soft one is to upload a better file, not to invent
detail the source never had. Running them through the pipeline would have
meant a synthetic Scryfall identity, registry rows that no decklist can
ever match, per-server variants that vanish when you switch hosts, and a
DPI-selection rule that had to differ from the card rule to avoid blanking
every Reverse on the sheet — a lot of machinery in exchange for very
little, on art that is usually a flat design where upscaling buys least.
What is left instead is a low-resolution *warning* (MIN_COMFORTABLE_DPI
below) and a cover-fit at export time.

The directory is a sibling of `output/` and `cache/`, never inside them.
`clear_generated_data` empties those two, and `prune_registry_under_dir`
drops every `generated_images` row found under `output/`. Living outside
both is what makes "Back Images survive the wipe" a property of where the
files are rather than a condition somebody has to remember to re-check.
"""

from __future__ import annotations

import hashlib
import io
import re
from pathlib import Path

from PIL import Image

# Relative name, resolved against the server process's cwd exactly like
# DEFAULT_OUTPUT_DIR / DEFAULT_CACHE_DIR in api/routers/misc.py.
BACKS_DIR_NAME = "backs"

_HASH_RE = re.compile(r"^[0-9a-f]{64}$")

# Formats accepted on upload. Everything is normalised to PNG on the way
# in, because a JPEG hiding behind a .png name would work only by PIL's
# content sniffing — true today, and a trap the first time anything reads
# the extension instead.
ACCEPTED_FORMATS = {"PNG", "JPEG", "WEBP"}
MAX_UPLOAD_BYTES = 50 * 1024 * 1024

# Below this, a Back Image is being asked to cover a 63×88mm card with
# less detail than a decent printer resolves. Warned about, never blocked:
# plenty of people knowingly print a flat logo at low DPI, and since backs
# are never upscaled this warning is the *only* thing standing between a
# soft source and a soft print — so it has to be visible without being a
# refusal.
MIN_COMFORTABLE_DPI = 300


class BackImageError(ValueError):
    """Rejected upload — message is user-facing."""


def validate_hash(content_hash: str) -> str:
    """Lowercase hex sha256, or raise. Called on every path that takes a
    hash from a request — it reaches the filesystem, so it is never
    trusted enough to interpolate unchecked."""
    normalized = (content_hash or "").strip().lower()
    if not _HASH_RE.match(normalized):
        raise BackImageError("Back image id must be a lowercase hex sha256.")
    return normalized


def backs_dir(root: Path | str = BACKS_DIR_NAME) -> Path:
    return Path(root)


def original_path(content_hash: str, *, root: Path | str = BACKS_DIR_NAME) -> Path:
    """Where a synced Back Image lives: one flat, hash-named PNG."""
    return backs_dir(root) / f"{validate_hash(content_hash)}.png"


def has_original(content_hash: str, *, root: Path | str = BACKS_DIR_NAME) -> bool:
    return original_path(content_hash, root=root).is_file()


def store_original(
    data: bytes, *, root: Path | str = BACKS_DIR_NAME, expected_hash: str | None = None
) -> tuple[str, Path]:
    """Validate an uploaded image and store it content-addressed.

    Returns (content_hash, path). Idempotent: re-uploading identical bytes
    is a no-op that returns the existing path, which is what makes the
    client's "sync on miss" cheap to call unconditionally.
    """
    if not data:
        raise BackImageError("Back image upload was empty.")
    if len(data) > MAX_UPLOAD_BYTES:
        mb = MAX_UPLOAD_BYTES // (1024 * 1024)
        raise BackImageError(f"Back image is larger than the {mb}MB limit.")

    content_hash = hashlib.sha256(data).hexdigest()
    if expected_hash is not None and validate_hash(expected_hash) != content_hash:
        raise BackImageError("Back image contents do not match the id they were sent under.")

    try:
        with Image.open(io.BytesIO(data)) as probe:
            image_format = (probe.format or "").upper()
            probe.load()
            rgb = probe.convert("RGB")
    except BackImageError:
        raise
    except Exception as exc:  # noqa: BLE001 — any decode failure is one rejection
        raise BackImageError("Back image could not be read as an image file.") from exc

    if image_format not in ACCEPTED_FORMATS:
        raise BackImageError(
            f"Back image must be PNG, JPEG or WebP (got {image_format or 'unknown'})."
        )

    dest = original_path(content_hash, root=root)
    if dest.is_file():
        return content_hash, dest
    dest.parent.mkdir(parents=True, exist_ok=True)
    # Write-then-rename: a half-written file at a content-addressed path
    # is worse than no file, because has_original() would report it
    # present forever after.
    tmp = dest.with_name(dest.name + ".part")
    rgb.save(tmp, format="PNG")
    tmp.replace(dest)
    return content_hash, dest


def source_dpi(content_hash: str, *, root: Path | str = BACKS_DIR_NAME) -> float | None:
    """Effective print DPI of the stored original at card size, or None if
    it isn't stored. What the low-resolution warning is computed from."""
    path = original_path(content_hash, root=root)
    if not path.is_file():
        return None
    from proxy_scaler.dpi import dpi_at_card_size

    with Image.open(path) as img:
        return dpi_at_card_size(*img.size)


def resolve_print_source(
    content_hash: str | None, *, root: Path | str = BACKS_DIR_NAME
) -> Path | None:
    """The image build_pdf should print on a Reverse, or None if this
    server doesn't hold it.

    There is exactly one candidate — the synced original — because backs
    are never upscaled. build_pdf cover-fits and resizes it to export_dpi
    at render time (see pdf_layout.render_back_image).
    """
    if not content_hash:
        return None
    path = original_path(validate_hash(content_hash), root=root)
    return path if path.is_file() else None


def delete_back(content_hash: str, *, root: Path | str = BACKS_DIR_NAME) -> int:
    """Remove a Back Image from this server. The client's own library copy
    is canonical and untouched — re-syncing it is a single upload."""
    path = original_path(content_hash, root=root)
    if not path.is_file():
        return 0
    path.unlink()
    return 1
