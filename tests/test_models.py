"""Tests for model registry helpers."""

import pytest

from proxy_scaler.upscale import (
    AUTO_TILE_PRESET,
    NCNN_DEFAULT_PRESET,
    NCNN_MODELS,
    NCNN_TILE_PRESETS,
    TORCH_DEFAULT_PRESET,
    TORCH_TILE_PRESETS,
    VRAM_TIERS,
    Backend,
    UpscaleModel,
    default_tile_preset,
    effective_tile_size,
    parse_model,
    tile_presets_for,
)


def test_parse_model():
    assert parse_model("realesrgan_anime_fast") is UpscaleModel.REALESRGAN_ANIME_FAST
    assert parse_model("IllustrationJaNai") is UpscaleModel.ILLUSTRATIONJANAI
    assert parse_model("ultrasharp_v2") is UpscaleModel.ULTRASHARP_V2
    assert parse_model("ultrasharp_v2_lite") is UpscaleModel.ULTRASHARP_V2_LITE
    assert parse_model("realesrgan_anime_fast_vk") is UpscaleModel.REALESRGAN_ANIME_FAST_VK
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


# --- backends / Vulkan models ---------------------------------------------


def test_every_model_has_a_backend_and_group():
    """backend/group are all-members dicts like label: a new member without
    an entry must fail here, not at first generation."""
    for model in UpscaleModel:
        assert isinstance(model.backend, Backend)
        assert model.group
    assert UpscaleModel.ULTRASHARP_V2.backend is Backend.TORCH
    assert UpscaleModel.REALESRGAN_ANIME_FAST_VK.backend is Backend.NCNN
    assert UpscaleModel.REALESRGAN_ANIME_FAST_VK.group == "Vulkan Models"
    assert UpscaleModel.ULTRASHARP_V2.group == "Models"
    assert NCNN_MODELS == {m for m in UpscaleModel if m.backend is Backend.NCNN}
    assert UpscaleModel.REALESRGAN_ANIME_FAST_VK in NCNN_MODELS


def test_model_ids_are_filename_safe():
    """Ids land in output filenames (db.py's slug grammar) and, for ncnn,
    are the on-disk weight names: lowercase, no hyphens, no spaces."""
    import re

    for model in UpscaleModel:
        assert re.fullmatch(r"[a-z0-9_]+", model.value), model.value


def test_ncnn_models_have_param_and_bin_with_checksums():
    from proxy_scaler.upscale import _WEIGHTS

    for model in NCNN_MODELS:
        spec = _WEIGHTS[(model, 4)]
        names = [f.filename for f in spec.files]
        assert names == [f"{model.value}.param", f"{model.value}.bin"]
        for wf in spec.files:
            assert wf.sha256 and len(wf.sha256) == 64
            assert wf.url.endswith("/" + wf.filename)


def test_torch_models_keep_single_file_specs():
    from proxy_scaler.upscale import _WEIGHTS

    for model in UpscaleModel:
        if model.backend is Backend.TORCH:
            spec = _WEIGHTS[(model, 4)]
            assert len(spec.files) == 1
            assert spec.filename == spec.primary.filename


def test_tile_presets_shape():
    keys = [p.key for p in VRAM_TIERS]
    tiles = [p.tile for p in VRAM_TIERS]
    assert len(set(keys)) == len(keys)
    assert tiles == sorted(tiles) and len(set(tiles)) == len(tiles)
    assert all(t > 0 and t % 32 == 0 for t in tiles)
    assert NCNN_DEFAULT_PRESET in keys
    # Same tiers for both backends; torch adds Auto (tile 0) in front and
    # defaults to it, Vulkan has no Auto and defaults to medium.
    assert NCNN_TILE_PRESETS == VRAM_TIERS
    assert TORCH_TILE_PRESETS == (AUTO_TILE_PRESET,) + VRAM_TIERS
    assert AUTO_TILE_PRESET.tile == 0 and AUTO_TILE_PRESET.key == TORCH_DEFAULT_PRESET
    for model in UpscaleModel:
        presets = tile_presets_for(model)
        default = default_tile_preset(model)
        assert default is not None
        if model.backend is Backend.NCNN:
            assert presets == NCNN_TILE_PRESETS
            assert default.key == NCNN_DEFAULT_PRESET and default.tile > 0
            # Never in the torch HEAVY ladder: 0 stays 0 here and is
            # resolved to the medium preset by NcnnUpscaler itself.
            assert effective_tile_size(model, 0) == 0
        elif model.backend is Backend.ONNX:
            from proxy_scaler.upscale import ONNX_TILE_PRESETS

            # Fixed-size exports: a subset of the tiers, no Auto, no Max
            # (WebGPU's per-buffer limit), default medium.
            assert presets == ONNX_TILE_PRESETS
            assert default.key == NCNN_DEFAULT_PRESET and default.tile > 0
            assert "max" not in {p.key for p in presets}
        else:
            assert presets == TORCH_TILE_PRESETS
            assert default.key == "auto" and default.tile == 0


# --- ONNX Runtime (WebGPU) models -------------------------------------------


def test_onnx_models_have_one_hashed_file_per_tier():
    from proxy_scaler.upscale import _WEIGHTS, ONNX_TILE_PRESETS, onnx_filename, onnx_input_shape

    onnx = [m for m in UpscaleModel if m.backend is Backend.ONNX]
    assert {m.value for m in onnx} == {"ultrasharp_v2_ort", "illustrationjanai_ort"}
    for model in onnx:
        spec = _WEIGHTS[(model, 4)]
        assert [f.filename for f in spec.files] == [
            onnx_filename(model, onnx_input_shape(p.tile)) for p in ONNX_TILE_PRESETS
        ]
        for wf in spec.files:
            assert wf.sha256 and len(wf.sha256) == 64
            assert wf.url.endswith("/models/onnx/v2/" + wf.filename)
        presets = tile_presets_for(model)
        assert presets and all(p.tile > 0 for p in presets)
        assert default_tile_preset(model).key == "medium"
        assert effective_tile_size(model, 0) == 0


def test_every_model_has_a_short_label():
    for model in UpscaleModel:
        assert model.short_label


@pytest.mark.parametrize(
    "platform, api, group, badge",
    [("linux", "Vulkan", "Vulkan Models", "USV2-VK"), ("win32", "DirectX 12", "GPU-Universal Models", "USV2-DX")],
)
def test_onnx_labels_are_true_per_platform(monkeypatch, platform, api, group, badge):
    """ONNX Runtime WebGPU runs on Vulkan on Linux but Direct3D 12 on
    Windows: labels, the shared group header and badges must say so."""
    import proxy_scaler.upscale as up

    monkeypatch.setattr(up.sys, "platform", platform)
    m = UpscaleModel.ULTRASHARP_V2_ORT
    assert f"({api})" in m.label
    assert m.group == group
    assert m.short_label == badge
    assert UpscaleModel.ANIMESHARP_VK.group == group  # one shared group
    assert "(Vulkan)" in UpscaleModel.ANIMESHARP_VK.label  # ncnn is Vulkan everywhere
    assert UpscaleModel.ULTRASHARP_V2.group == "Models"
