"""Print DPI targets for standard MTG card size (2.5\" × 3.5\")."""

from __future__ import annotations

from .upscale import UpscaleModel

CARD_WIDTH_IN = 2.5
CARD_HEIGHT_IN = 3.5

# User-facing DPI choices
DPI_OPTIONS: tuple[int, ...] = (600, 800, 1200)
DEFAULT_DPI = 1200


def target_pixels(dpi: int) -> tuple[int, int]:
    """Exact pixel size for a given print DPI at card dimensions."""
    return (round(CARD_WIDTH_IN * dpi), round(CARD_HEIGHT_IN * dpi))


def native_scale_for_dpi(dpi: int, model: UpscaleModel) -> int:
    """Which Real-ESRGAN/SwinIR factor to run before optional Lanczos resize.

    600 DPI ≈ x2 from Scryfall; 800/1200 use x4 then resize to exact pixels.
    RealESRNet is x4-only, so 600 DPI is derived from x4 + downscale.
    """
    if dpi <= 600 and 2 in model.supported_scales:
        return 2
    if 4 in model.supported_scales:
        return 4
    # Fallback to whatever the model offers
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
