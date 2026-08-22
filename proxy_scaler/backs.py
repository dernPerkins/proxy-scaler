"""Back Images: the user-supplied art printed on a card's Reverse.

A Back Image is not a card. It has no Scryfall identity, never appears in
a decklist, and its canonical copy lives on the *client* (see
docs/adr/0003). This module is the generation server's half: a
content-addressed store under `backs/`, plus the synthetic identity that
lets an upload ride the existing upscale pipeline unchanged
(docs/adr/0004).

Two deliberate choices are load-bearing here:

**The directory is a sibling of `output/` and `cache/`, never inside
them.** `clear_generated_data` empties those two, and
`prune_registry_under_dir` drops every `generated_images` row found under
`output/`. Living outside both is what makes "backs survive the wipe" a
property of where the files are rather than a condition somebody has to
remember to re-check in two places.

**The synthetic id is `backimage-<sha256>`, with a hyphen.** The obvious
spelling is a colon, and it was the first thing tried. `original_cache_
path` interpolates a scryfall_id straight into a filename, and a colon in
a Windows filename is an alternate-data-stream separator, not a
character — so the colon form is unopenable on one of the three platforms
this ships to.
"""

from __future__ import annotations

import hashlib
import io
import re
from pathlib import Path
from typing import Any

from PIL import Image

from proxy_scaler import db
from proxy_scaler.upscale import original_cache_path

# Relative name, resolved against the server process's cwd exactly like
# DEFAULT_OUTPUT_DIR / DEFAULT_CACHE_DIR in api/routers/misc.py.
BACKS_DIR_NAME = "backs"

_ID_PREFIX = "backimage-"
_HASH_RE = re.compile(r"^[0-9a-f]{64}$")

# Formats accepted on upload. Everything is normalised to PNG on the way
# in, because the pipeline's original cache is PNG-by-convention and a
# JPEG hiding behind a .png name would work only by PIL's content
# sniffing — true today, and a trap the first time anything reads the
# extension instead.
ACCEPTED_FORMATS = {"PNG", "JPEG", "WEBP"}
MAX_UPLOAD_BYTES = 50 * 1024 * 1024

# Below this, a Back Image is being asked to cover a 63×88mm card with
# less detail than a decent printer resolves. Warned about, never
# blocked — plenty of people knowingly print a flat logo at low DPI.
MIN_COMFORTABLE_DPI = 300


class BackImageError(ValueError):
    """Rejected upload — message is user-facing."""


def synthetic_id(content_hash: str) -> str:
    """The `scryfall_id` a Back Image wears inside the generation tables."""
    return f"{_ID_PREFIX}{content_hash}"


def is_back_image_id(scryfall_id: str | None) -> bool:
    return bool(scryfall_id) and scryfall_id.startswith(_ID_PREFIX)


def hash_from_id(scryfall_id: str) -> str | None:
    if not is_back_image_id(scryfall_id):
        return None
    return scryfall_id[len(_ID_PREFIX) :] or None


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
    """Where a synced original lives.

    Deliberately the pipeline's OWN canonical cache location for this
    identity, not a path of our choosing. `_regenerate_faces` checks
    `original_cache_path(cache_dir, scryfall_id, face_index)` before it
    reaches for `png_url`, so seeding the file here means an upscale of a
    Back Image never makes a network call — with no change to pipeline.py
    or worker.py at all.
    """
    return original_cache_path(backs_dir(root), synthetic_id(validate_hash(content_hash)), None)


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
    from proxy_scaler.dpi import CARD_HEIGHT_MM, MM_PER_IN

    with Image.open(path) as img:
        return max(img.size) / (CARD_HEIGHT_MM / MM_PER_IN)


def list_variants(content_hash: str, db_path: Path | str | None = None) -> list[dict[str, Any]]:
    """Upscaled variants of one Back Image that exist on THIS server.

    Registry rows, newest DPI first. The client asks per connected server
    because that is genuinely where the answer differs: the library is
    yours, the upscales belong to a machine (docs/adr/0003).
    """
    rows = db.list_generated_by_scryfall_id(synthetic_id(validate_hash(content_hash)), db_path=db_path)
    live = [row for row in rows if Path(row["out_path"]).is_file()]
    return sorted(live, key=lambda r: (-int(r["dpi"]), str(r.get("created_at") or "")))


def pick_variant(
    variants: list[dict[str, Any]], *, preferred_model: str | None = None
) -> dict[str, Any] | None:
    """Which upscaled variant prints: the highest DPI available.

    `preferred_dpi` is deliberately NOT a filter here, unlike cards. For a
    card it is a hard filter and a miss drops the card from the run with a
    reported error; applying that to a Back Image would blank every
    Reverse on the sheet instead. build_pdf's export_dpi normalises
    whatever we hand it, so a back generated at 800 in a 1200 DPI export
    is a quality question, never a correctness one.
    """
    if not variants:
        return None
    if preferred_model is not None:
        matching = [v for v in variants if v["model"] == preferred_model]
        if matching:
            return matching[0]
    return variants[0]


def resolve_print_source(
    content_hash: str | None,
    *,
    root: Path | str = BACKS_DIR_NAME,
    preferred_model: str | None = None,
    db_path: Path | str | None = None,
) -> tuple[Path | None, bool]:
    """The image build_pdf should print on a Reverse, and whether it is the
    plain original rather than an upscale.

    Returns (path, is_unupscaled_original). A True second element is what
    the client turns into its non-blocking "this back was upscaled on
    another server" notice — printing still works, because the original is
    always cover-fitted and resized at export time.
    """
    if not content_hash:
        return None, False
    normalized = validate_hash(content_hash)
    variant = pick_variant(
        list_variants(normalized, db_path=db_path), preferred_model=preferred_model
    )
    if variant is not None:
        return Path(variant["out_path"]), False
    path = original_path(normalized, root=root)
    return (path, True) if path.is_file() else (None, False)


def delete_variants(content_hash: str, db_path: Path | str | None = None) -> int:
    """Remove this back's upscales from disk and registry, keeping the
    synced original. The Backs tab's "Clear upscales" — the action that
    reclaims real disk on a GPU box without losing anything that cannot be
    rebuilt."""
    removed = 0
    for row in list_variants(content_hash, db_path=db_path):
        Path(row["out_path"]).unlink(missing_ok=True)
        db.delete_gallery_item(int(row["id"]), db_path=db_path)
        removed += 1
    return removed


def delete_back(content_hash: str, *, root: Path | str = BACKS_DIR_NAME, db_path=None) -> int:
    """Remove a Back Image entirely from this server: upscales, then the
    synced original. The client's own library copy is untouched — it is
    the canonical one, and re-syncing it is a single upload."""
    removed = delete_variants(content_hash, db_path=db_path)
    path = original_path(content_hash, root=root)
    if path.is_file():
        path.unlink()
        removed += 1
    return removed
