"""Tests for model registry helpers."""

import pytest

from proxy_scaler.upscale import UpscaleModel, parse_model


def test_parse_model():
    assert parse_model("realesrgan") is UpscaleModel.REALESRGAN
    assert parse_model("SwinIR") is UpscaleModel.SWINIR
    with pytest.raises(ValueError):
        parse_model("seedvr2")


def test_supported_scales():
    assert UpscaleModel.REALESRGAN.supported_scales == (2, 4)
    assert UpscaleModel.REALESRNET.supported_scales == (4,)
    assert UpscaleModel.SWINIR.supported_scales == (2, 4)
