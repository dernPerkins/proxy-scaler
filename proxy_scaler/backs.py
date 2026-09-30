"""Back Images: the user-supplied art printed on a card's Reverse.

A Back Image is not a card. It has no Scryfall identity, never appears in
a decklist, and its canonical copy lives on the *client* (the Back
Library, desktop/src-tauri/src/back_images.rs). This module is the
generation server's half: a content-addressed cache of the bytes, plus the
lookup that picks which image prints on a Reverse.

**Upscaling a back is a library-tab action, never a Generate one.** Backs
were originally never upscaled, because the only shapes on offer were a
fake Scryfall identity and registry rows no decklist could match. Custom
Images settled both: identity is typed (db migration 008 for customs, 010
for backs — a back_hash column, never a minted UUID), and a decklist
doesn't need to match a back at all, because the PDF/ZIP paths look the
back up by hash (resolve_print_source below) and rank its variants with
the same preferred-DPI/model rules as a card — degrading to the best
available rather than blanking the Reverse, exactly as a Custom Image
does. The Backs tab queues the work under dpi.LIBRARY_TAG (see
api/routers/library.py); a project is never involved. The low-resolution
*warning* (MIN_COMFORTABLE_DPI below) stays, as the nudge to upscale.

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
# plenty of people knowingly print a flat logo at low DPI; the remedy is
# the Backs tab's Upscale button, or a better file.
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
    it isn't stored. What the low-resolution warning is computed from, and
    what a back's back_source registry row and its upscale targets are
    measured against.

    Deliberately bleed-agnostic: the server holds no bleed declaration for
    a back (it rides on each render request), so this and every variant's
    recorded DPI use the same plain long-edge measure. A pre-bled back
    reads a few percent high on both sides of the "already reaches this
    target" comparison, which cancels out."""
    path = original_path(content_hash, root=root)
    if not path.is_file():
        return None
    from proxy_scaler.dpi import dpi_at_card_size

    with Image.open(path) as img:
        return dpi_at_card_size(*img.size)


def identity_key(content_hash: str) -> str:
    """'back:<sha256>' — see customs.identity_key for the shared contract."""
    from proxy_scaler.customs import identity_key as shared

    return shared(None, None, validate_hash(content_hash))


def resolve_print_source(
    content_hash: str | None,
    *,
    root: Path | str = BACKS_DIR_NAME,
    preferred_dpi: int | None = None,
    preferred_model: str | None = None,
    use_originals: bool = False,
    db_path: Path | str | None = None,
) -> Path | None:
    """The image build_pdf should print on a Reverse, or None if this
    server doesn't hold the back at all.

    The candidates are the back's registry rows — its back_source row and
    every upscale, whichever project or the library tab made them — that
    still exist on disk, plus the synced original itself. They are ranked
    exactly as a Custom Image's variants are (pdf_layout._pick_dpi_variant
    with the never-blank rule): the preferred DPI wins when it exists,
    else the best available, with the preferred model breaking ties.
    `use_originals` prints the synced original, the way it narrows a card
    to its Scryfall scan. build_pdf cover-fits whatever comes back to the
    export DPI (pdf_layout.render_back_image), so an upscale and the
    original are interchangeable downstream.
    """
    if not content_hash:
        return None
    checked = validate_hash(content_hash)
    original = original_path(checked, root=root)
    if not original.is_file():
        return None
    if use_originals:
        return original

    from proxy_scaler import db
    from proxy_scaler.dpi import BACK_SOURCE_MODEL, dpi_at_card_size
    from proxy_scaler.pdf_layout import _pick_dpi_variant
    from proxy_scaler.pipeline import FaceResult

    candidates = [
        item
        for item in (
            FaceResult.from_dict(d)
            for d in db.list_registry_items_for_back(checked, db_path=db_path)
        )
        if item.out_path.is_file()
    ]
    if not any(c.model == BACK_SOURCE_MODEL for c in candidates):
        with Image.open(original) as img:
            measured = round(dpi_at_card_size(*img.size))
        candidates.append(
            FaceResult(
                out_path=original,
                original_path=original,
                scryfall_id=None,
                back_hash=checked,
                face_index=None,
                face_name="Card back",
                card_name="Card back",
                set_code="",
                collector_number="",
                png_url="",
                dpi=measured,
                model=BACK_SOURCE_MODEL,
                native_scale=1,
            )
        )
    chosen, _ = _pick_dpi_variant(candidates, preferred_dpi, preferred_model)
    return chosen.out_path if chosen is not None else original


def delete_back(content_hash: str, *, root: Path | str = BACKS_DIR_NAME) -> int:
    """Remove a Back Image from this server. The client's own library copy
    is canonical and untouched — re-syncing it is a single upload."""
    path = original_path(content_hash, root=root)
    if not path.is_file():
        return 0
    path.unlink()
    return 1
