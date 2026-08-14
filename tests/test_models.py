"""Tests for model registry helpers."""

import pytest

from proxy_scaler.upscale import UpscaleModel, parse_model


def test_parse_model():
    assert parse_model("realesrgan_anime_fast") is UpscaleModel.REALESRGAN_ANIME_FAST
    assert parse_model("IllustrationJaNai") is UpscaleModel.ILLUSTRATIONJANAI
    assert parse_model("ultrasharp_v2") is UpscaleModel.ULTRASHARP_V2
    with pytest.raises(ValueError):
        parse_model("seedvr2")
    with pytest.raises(ValueError):
        parse_model("swinir")


def test_supported_scales():
    for model in UpscaleModel:
        assert model.supported_scales == (4,)


def test_all_models_have_labels():
    for model in UpscaleModel:
        assert model.label
