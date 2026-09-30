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
#
# A Custom Image is upscaled only when the project's custom_upscale
# setting asks for it (see custom_upscale_targets below); the source row
# is registered either way, so the upload always prints.
CUSTOM_SOURCE_MODEL = "custom_source"

# Sentinel variant for a Back Image's synced original, registered so the
# library tab can show it and the PDF/ZIP back lookup can rank it against
# the back's upscales (backs.resolve_print_source). Same shape and rules as
# CUSTOM_SOURCE_MODEL: not an UpscaleModel, no fixed DPI (the row carries
# the file's measured dpi_at_card_size), branched on before parse_model().
BACK_SOURCE_MODEL = "back_source"

# The project setting that decides whether Generate upscales Custom Images:
#   off     never (the upload prints at its own resolution)
#   target  to each selected DPI the upload doesn't already reach
#   native  the model's own 4x result, capped at NATIVE_MAX_DPI
CUSTOM_UPSCALE_OFF = "off"
CUSTOM_UPSCALE_TARGET = "target"
CUSTOM_UPSCALE_NATIVE = "native"
CUSTOM_UPSCALE_MODES: tuple[str, ...] = (
    CUSTOM_UPSCALE_OFF,
    CUSTOM_UPSCALE_TARGET,
    CUSTOM_UPSCALE_NATIVE,
)

# The project_tag every task and gallery membership made from the Customs
# or Backs tab carries. Those libraries belong to the machine, not to a
# project, so their upscales are queued under this fixed tag instead of
# whichever project happens to be open: the registry row is global either
# way, a project adopts it when the image becomes one of its cards
# (db.adopt_gallery_items), and the PDF/ZIP paths look library images up
# by identity regardless of tag. Can never collide with a real tag — the
# client mints those as 32 hex characters (project_store.rs).
LIBRARY_TAG = "library"

# Ceiling on a "native" custom upscale. A full 4x of a ~1150 DPI upload
# is over 179 Mpx, which Pillow refuses to open (its decompression-bomb
# limit) — so the x4 cache, the PDF renderer, the ZIP export and the
# gallery thumbnail would all fail on the file. 2400 DPI keeps a card with
# MPC bleed at ~58 Mpx, well inside that, and is already 2x the finest
# selectable print density.
NATIVE_MAX_DPI = 2400


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


def native_output_dpi(source_dpi: int, scale: int = 4) -> int:
    """The DPI a "native" custom upscale is registered at: the model's own
    scale over the upload's measured DPI, capped at NATIVE_MAX_DPI."""
    return min(int(source_dpi) * scale, NATIVE_MAX_DPI)


def custom_upscale_targets(
    mode: str, source_dpi: int, dpi_targets: list[int]
) -> list[int]:
    """Which upscaled variants Generate should queue for one Custom Image
    measured at `source_dpi`, given the project's selected targets.

    Only an upload *below* a selected target is ever upscaled: one that
    already reaches every target prints from its source row, and running a
    4x model over it would spend GPU time (and, above ~1150 DPI, more
    memory than a pass can have) to reproduce what is there. Empty for
    mode "off"."""
    if mode not in CUSTOM_UPSCALE_MODES:
        raise ValueError(f"custom_upscale must be one of {CUSTOM_UPSCALE_MODES}, got {mode!r}")
    below = sorted({t for t in dpi_targets if t > source_dpi})
    if mode == CUSTOM_UPSCALE_OFF or not below:
        return []
    if mode == CUSTOM_UPSCALE_TARGET:
        return below
    return [native_output_dpi(source_dpi)]


def is_native_custom_dpi(dpi: int) -> bool:
    """Whether a custom upscale task's DPI came from native_output_dpi
    rather than the selectable targets: those are written as the model
    produced them instead of being resampled to an exact box."""
    return dpi not in DPI_OPTIONS


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
