"""Tests for DPI helpers."""

from proxy_scaler.dpi import (
    DEFAULT_DPI,
    native_scale_for_dpi,
    resolve_dpi_targets,
    target_pixels,
)
from proxy_scaler.upscale import UpscaleModel


def test_defaults():
    assert DEFAULT_DPI == 1200
    assert target_pixels(800) == (2000, 2800)
    assert target_pixels(600) == (1500, 2100)
    assert target_pixels(1200) == (3000, 4200)


def test_native_scale():
    # Every current model is x4-only, at every DPI target.
    for model in UpscaleModel:
        for dpi in (600, 800, 1200):
            assert native_scale_for_dpi(dpi, model) == 4


def test_resolve_targets():
    assert resolve_dpi_targets(dpi=800) == [800]
    assert resolve_dpi_targets(all_dpis=True) == [600, 800, 1200]
