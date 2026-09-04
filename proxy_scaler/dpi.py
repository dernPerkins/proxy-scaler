"""Print DPI targets for standard MTG card size (63 × 88 mm)."""

from __future__ import annotations

from .upscale import UpscaleModel

MM_PER_IN = 25.4

# Real MTG cards measure 63 × 88 mm — slightly smaller than the nominal
# 2.5″ × 3.5″ (63.5 × 88.9 mm) poker size. The inch-derived size prints
# cards ~0.9 mm too tall against a real card.
CARD_WIDTH_MM = 63.0
CARD_HEIGHT_MM = 88.0

# User-facing DPI choices
DPI_OPTIONS: tuple[int, ...] = (600, 800, 1200)
DEFAULT_DPI = 1200

# Sentinel variant for a download-only Scryfall original (no upscale).
# Deliberately NOT an UpscaleModel and NOT in DPI_OPTIONS: parse_model()
# and resolve_dpi_targets() must never see these — download tasks bypass
# both (pipeline.process_task branches on ORIGINAL_MODEL before parsing,
# and /api/generate/downloads doesn't take dpi_targets). Scryfall's png
# is 745×1040, which is ~300 DPI at card size.
ORIGINAL_DPI = 300
ORIGINAL_MODEL = "original"

# Sentinel variant for a Custom Image the user has not upscaled: the
# uploaded file itself, cover-cropped to card aspect, registered so it can
# be printed. Like ORIGINAL_MODEL it is deliberately NOT an UpscaleModel —
# pipeline.process_task branches on it before parse_model().
#
# Unlike ORIGINAL_MODEL it has no fixed companion DPI: a Scryfall original
# is always 745×1040 (~300 DPI), but a custom upload is whatever the user
# had, so its registry row carries the real dpi_at_card_size() value. That
# is also why it is a *separate* sentinel rather than reusing
# ORIGINAL_MODEL: pdf_layout.match_quantities treats ORIGINAL_MODEL rows as
# an exclusive world (use_originals on/off), and filing customs there would
# force "Use 300 DPI originals" on to print one — blanking every upscaled
# Scryfall card on the same sheet.
CUSTOM_SOURCE_MODEL = "custom_source"


def target_pixels(dpi: int) -> tuple[int, int]:
    """Exact pixel size for a given print DPI at card dimensions."""
    return (
        round(CARD_WIDTH_MM / MM_PER_IN * dpi),
        round(CARD_HEIGHT_MM / MM_PER_IN * dpi),
    )


def dpi_at_card_size(width: int, height: int) -> float:
    """Effective print DPI an image achieves across a 63×88mm card, using
    its longer edge against the card's longer edge.

    Mirrored in the desktop client (back_images.rs::dpi_at_card_size) so
    the two halves never disagree about whether an image is low-res.
    """
    return max(width, height) / (CARD_HEIGHT_MM / MM_PER_IN)


def card_aspect_crop_size(width: int, height: int) -> tuple[int, int]:
    """Largest 63:88 box that fits inside (width, height).

    Used as the target for a cover-crop of a user-supplied image, so the
    crop keeps every pixel it can in the limiting axis rather than
    resampling the whole image down to some fixed size.
    """
    scale = min(width / CARD_WIDTH_MM, height / CARD_HEIGHT_MM)
    return max(1, round(CARD_WIDTH_MM * scale)), max(1, round(CARD_HEIGHT_MM * scale))


def native_scale_for_dpi(dpi: int, model: UpscaleModel) -> int:
    """Native upscale factor to run before the exact-pixel Lanczos resize.

    Every current model is x4-only, so all DPI targets derive from x4
    (600/800 DPI downscale from it).
    """
    return model.supported_scales[-1]


def resolve_dpi_targets(
    *,
    dpi: int = DEFAULT_DPI,
    all_dpis: bool = False,
    dpi_targets: list[int] | None = None,
) -> list[int]:
    if dpi_targets is not None:
        selected = sorted(set(dpi_targets))
        invalid = [d for d in selected if d not in DPI_OPTIONS]
        if invalid:
            raise ValueError(f"DPI must be one of {DPI_OPTIONS}, got {invalid}")
        if not selected:
            raise ValueError("At least one target DPI must be selected.")
        return selected
    if all_dpis:
        return list(DPI_OPTIONS)
    if dpi not in DPI_OPTIONS:
        raise ValueError(f"DPI must be one of {DPI_OPTIONS}, got {dpi}")
    return [dpi]
