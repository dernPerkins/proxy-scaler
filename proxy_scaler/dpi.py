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


def target_pixels(dpi: int) -> tuple[int, int]:
    """Exact pixel size for a given print DPI at card dimensions."""
    return (
        round(CARD_WIDTH_MM / MM_PER_IN * dpi),
        round(CARD_HEIGHT_MM / MM_PER_IN * dpi),
    )


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
