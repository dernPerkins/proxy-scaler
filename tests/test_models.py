"""Tests for model registry helpers."""

import pytest

from proxy_scaler.upscale import UpscaleModel, parse_model


def test_parse_model():
    assert parse_model("realesrgan_anime_fast") is UpscaleModel.REALESRGAN_ANIME_FAST
    assert parse_model("IllustrationJaNai") is UpscaleModel.ILLUSTRATIONJANAI
    assert parse_model("ultrasharp_v2") is UpscaleModel.ULTRASHARP_V2
    assert parse_model("ultrasharp_v2_lite") is UpscaleModel.ULTRASHARP_V2_LITE
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


def test_all_models_have_speed_labels():
    for model in UpscaleModel:
        assert model.speed


def test_all_models_have_weights():
    """Every enum member must have a (model, 4) _WEIGHTS entry — a missing
    one is a raw KeyError deep in ensure_weights at generation time."""
    from proxy_scaler.upscale import _WEIGHTS

    for model in UpscaleModel:
        for scale in model.supported_scales:
            assert (model, scale) in _WEIGHTS
