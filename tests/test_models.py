"""Tests for model registry helpers."""

import pytest

from proxy_scaler.upscale import UpscaleModel, parse_model


def test_parse_model():
    assert parse_model("realesrgan") is UpscaleModel.REALESRGAN
    assert parse_model("SwinIR") is UpscaleModel.SWINIR
    assert parse_model("hat") is UpscaleModel.HAT
    assert parse_model("realesrgan_anime") is UpscaleModel.REALESRGAN_ANIME
    assert parse_model("illustrationjanai") is UpscaleModel.ILLUSTRATIONJANAI
    assert parse_model("ultrasharp_v2") is UpscaleModel.ULTRASHARP_V2
    with pytest.raises(ValueError):
        parse_model("seedvr2")


def test_supported_scales():
    assert UpscaleModel.REALESRGAN.supported_scales == (2, 4)
    assert UpscaleModel.REALESRNET.supported_scales == (4,)
    assert UpscaleModel.SWINIR.supported_scales == (2, 4)
    assert UpscaleModel.REALESRGAN_ANIME.supported_scales == (4,)
    assert UpscaleModel.ILLUSTRATIONJANAI.supported_scales == (4,)
    assert UpscaleModel.ULTRASHARP_V2.supported_scales == (4,)
    assert UpscaleModel.HAT.supported_scales == (4,)


def test_all_models_have_labels():
    for model in UpscaleModel:
        assert model.label
