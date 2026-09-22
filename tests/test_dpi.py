"""Tests for DPI helpers."""

import pytest

from proxy_scaler.dpi import (
    DEFAULT_DPI,
    MPC_BLEED_MM,
    bled_target_pixels,
    card_aspect_crop_size,
    dpi_at_card_size,
    native_scale_for_dpi,
    resolve_dpi_targets,
    target_pixels,
)
from proxy_scaler.upscale import UpscaleModel


def test_defaults():
    assert DEFAULT_DPI == 1200
    assert target_pixels(800) == (1984, 2772)
    assert target_pixels(600) == (1488, 2079)
    assert target_pixels(1200) == (2976, 4157)


def test_native_scale():
    # Every current model is x4-only, at every DPI target.
    for model in UpscaleModel:
        for dpi in (600, 800, 1200):
            assert native_scale_for_dpi(dpi, model) == 4


def test_resolve_targets():
    assert resolve_dpi_targets(dpi=800) == [800]
    assert resolve_dpi_targets(all_dpis=True) == [600, 800, 1200]


def test_bled_geometry_helpers():
    assert MPC_BLEED_MM == 3.175
    assert bled_target_pixels(600, 0) == target_pixels(600)
    # 63+6.35 x 88+6.35 mm at 600 DPI.
    assert bled_target_pixels(600, MPC_BLEED_MM) == (1638, 2229)
    # An MPC file spans 94.35 mm on its long edge: 1114 px of it is
    # ~300 DPI, not the ~322 the plain card measure would claim.
    assert dpi_at_card_size(818, 1114, MPC_BLEED_MM) == pytest.approx(300, abs=1)
    assert dpi_at_card_size(818, 1114) == pytest.approx(321.6, abs=1)
    w, h = card_aspect_crop_size(1000, 1000, MPC_BLEED_MM)
    assert w / h == pytest.approx((63 + 2 * MPC_BLEED_MM) / (88 + 2 * MPC_BLEED_MM), rel=1e-3)
    assert card_aspect_crop_size(1000, 1000) == card_aspect_crop_size(1000, 1000, 0.0)
