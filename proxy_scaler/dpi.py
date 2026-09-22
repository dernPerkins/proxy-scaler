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


# Upper bound on a declared or requested bleed, per side. Anything past
# this is a typo, not a print spec (MakePlayingCards' is 3.175 mm).
MAX_BLEED_MM = 10.0

# MakePlayingCards' bleed: 1/8 in per side (63x88 trim -> 69.35x94.35).
# The default for "this image already includes bleed" in both libraries.
MPC_BLEED_MM = 3.175


def target_pixels(dpi: int) -> tuple[int, int]:
    """Exact pixel size for a given print DPI at card dimensions."""
    return (
        round(CARD_WIDTH_MM / MM_PER_IN * dpi),
        round(CARD_HEIGHT_MM / MM_PER_IN * dpi),
    )


def bled_target_pixels(dpi: float, bleed_mm: float) -> tuple[int, int]:
    """Pixel size of one card *including* a bleed of bleed_mm on every
    side. bled_target_pixels(dpi, 0) == target_pixels(dpi)."""
    return (
        round((CARD_WIDTH_MM + 2 * bleed_mm) / MM_PER_IN * dpi),
        round((CARD_HEIGHT_MM + 2 * bleed_mm) / MM_PER_IN * dpi),
    )


def dpi_at_card_size(width: int, height: int, bleed_mm: float = 0.0) -> float:
    """Effective print DPI an image achieves across a 63×88mm card, using
    its longer edge against the card's longer edge.

    `bleed_mm` is the bleed the image is declared to carry per side: a
    pre-bled file spans (88 + 2*bleed) mm on its long edge, so measuring
    it against 88 mm would over-report its resolution by that ratio.

    Mirrored in the desktop client (back_images.rs::dpi_at_card_size and
    custom_images.rs) so the two halves never disagree about whether an
    image is low-res.
    """
    return max(width, height) / ((CARD_HEIGHT_MM + 2 * bleed_mm) / MM_PER_IN)


def card_aspect_crop_size(
    width: int, height: int, bleed_mm: float = 0.0
) -> tuple[int, int]:
    """Largest 63:88 box (or, with bleed_mm, the largest
    (63+2b):(88+2b) box) that fits inside (width, height).

    Used as the target for a cover-crop of a user-supplied image, so the
    crop keeps every pixel it can in the limiting axis rather than
    resampling the whole image down to some fixed size.
    """
    w_mm = CARD_WIDTH_MM + 2 * bleed_mm
    h_mm = CARD_HEIGHT_MM + 2 * bleed_mm
    scale = min(width / w_mm, height / h_mm)
    return max(1, round(w_mm * scale)), max(1, round(h_mm * scale))


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
