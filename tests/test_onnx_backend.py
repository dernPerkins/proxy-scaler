"""ONNX Runtime (WebGPU) backend — no real onnxruntime or GPU needed: the
session factory is monkeypatched with a fake that honours the fixed-shape
contract. Pins tier selection, the ladder, the plausibility gate, cache
eviction across runtimes, and the Upscaler contract."""

from __future__ import annotations

import numpy as np
import pytest
from PIL import Image

from proxy_scaler import onnx_backend as ob
from proxy_scaler.upscale import (
    ONNX_TILE_PRESETS,
    UpscaleModel,
    UpscaleResult,
    make_upscaler,
    onnx_filename,
    onnx_input_shape,
)

MODEL = UpscaleModel.ULTRASHARP_V2_ORT


class FakeSession:
    def __init__(self, path, gpu, behaviour):
        self.path, self.gpu, self.behaviour = path, gpu, behaviour
        w, h = path.stem.rsplit("_", 1)[1].split("x")
        self.input_shape = (int(h), int(w))
        self.calls = []

    def run(self, chw):
        assert chw.shape == (3, *self.input_shape), "fixed-shape contract"
        self.calls.append(chw.shape)
        mode = self.behaviour(self.gpu, self.input_shape)
        if mode == "raise":
            raise RuntimeError("webgpu: device lost")
        out = np.clip(np.repeat(np.repeat(chw, 4, axis=1), 4, axis=2) * 0.97 + 0.01, 0, 1)
        if mode == "black":
            out = np.zeros_like(out)
        return out.astype(np.float32)


@pytest.fixture
def harness(monkeypatch, tmp_path):
    sessions = []
    behaviour = {"fn": lambda gpu, n: "ok"}
    fetched = []

    def make_session(path, gpu):
        s = FakeSession(path, gpu, lambda g, n: behaviour["fn"](g, n))
        sessions.append(s)
        return s

    def fake_fetch(model, scale, weights_dir, filename):
        fetched.append(filename)
        return weights_dir / filename, False

    monkeypatch.setattr(ob, "_make_session", make_session)
    monkeypatch.setattr(ob, "ensure_weight_file", fake_fetch)
    monkeypatch.setattr(ob, "_CPU_ONLY_REASON", None)
    monkeypatch.delenv(ob.ONNX_TILE_ENV, raising=False)
    ob.clear_onnx_cache()
    yield {"sessions": sessions, "behaviour": behaviour, "fetched": fetched, "dir": tmp_path}
    ob.clear_onnx_cache()


def _card(w=300, h=420):
    rng = np.random.default_rng(1)
    small = Image.fromarray(rng.integers(30, 220, (12, 9, 3), dtype=np.uint8), "RGB")
    img = small.resize((w, h), Image.Resampling.BICUBIC).convert("RGBA")
    for y in range(6):
        for x in range(6):
            img.putpixel((x, y), (0, 0, 0, 0))
    return img


def test_resolve_tile_snaps_to_an_exported_tier(monkeypatch):
    monkeypatch.delenv(ob.ONNX_TILE_ENV, raising=False)
    tiles = [p.tile for p in ONNX_TILE_PRESETS]
    medium = next(p.tile for p in ONNX_TILE_PRESETS if p.key == "medium")
    assert ob.resolve_onnx_tile(0) == medium
    for t in tiles:
        assert ob.resolve_onnx_tile(t) == t
    assert ob.resolve_onnx_tile(400) == max(t for t in tiles if t <= 400)
    assert ob.resolve_onnx_tile(50) == min(tiles)
    monkeypatch.setenv(ob.ONNX_TILE_ENV, str(max(tiles) + 999))
    assert ob.resolve_onnx_tile(0) == max(tiles)


def test_happy_path_uses_one_tier_file_and_keeps_alpha(harness):
    tile = next(p.tile for p in ONNX_TILE_PRESETS if p.key == "medium")
    up = ob.OnnxUpscaler(MODEL, 4, harness["dir"], tile=tile)
    result = up.upscale(_card())
    assert isinstance(result, UpscaleResult)
    assert result.image.size == (1200, 1680) and result.image.mode == "RGBA"
    assert result.image.getpixel((2, 2))[3] == 0
    assert result.device == "gpu" and result.dtype == "fp32"
    assert harness["fetched"] == [onnx_filename(MODEL, onnx_input_shape(tile))]
    [session] = harness["sessions"]
    assert session.gpu and session.calls and set(session.calls) == {(3, *onnx_input_shape(tile))}


def test_ladder_steps_down_tiers_then_cpu_with_hook(harness):
    harness["behaviour"]["fn"] = lambda gpu, n: "raise" if gpu else "ok"
    fired = []
    top = max(p.tile for p in ONNX_TILE_PRESETS)
    up = ob.OnnxUpscaler(MODEL, 4, harness["dir"], tile=top, on_cpu_fallback=lambda: fired.append(1))
    result = up.upscale(_card())
    assert result.device == "cpu"
    assert fired == [1]
    gpu_shapes = [s.input_shape for s in harness["sessions"] if s.gpu]
    tiers = sorted((p.tile for p in ONNX_TILE_PRESETS), reverse=True)
    assert gpu_shapes == [onnx_input_shape(t) for t in tiers]
    # Next task in the process skips the GPU tiers and the hook.
    up2 = ob.OnnxUpscaler(MODEL, 4, harness["dir"], tile=top, on_cpu_fallback=lambda: fired.append(2))
    assert up2.upscale(_card()).device == "cpu" and fired == [1]


def test_implausible_output_is_not_kept(harness):
    medium = next(p.tile for p in ONNX_TILE_PRESETS if p.key == "medium")
    harness["behaviour"]["fn"] = lambda gpu, n: "black" if gpu and n == onnx_input_shape(medium) else "ok"
    up = ob.OnnxUpscaler(MODEL, 4, harness["dir"], tile=medium)
    result = up.upscale(_card())
    assert result.device == "gpu" and up.tile < medium  # recovered one tier down


def test_loading_a_session_evicts_the_other_runtimes(harness, monkeypatch):
    from proxy_scaler import ncnn_backend, upscale

    calls = []
    monkeypatch.setattr(upscale, "clear_model_cache", lambda: calls.append("torch"))
    monkeypatch.setattr(ncnn_backend, "clear_ncnn_cache", lambda: calls.append("ncnn"))
    ob.OnnxUpscaler(MODEL, 4, harness["dir"], tile=0).upscale(_card(80, 100))
    assert calls == ["torch", "ncnn"]


def test_dispatch_and_type_guards(harness):
    assert isinstance(make_upscaler(MODEL, scale=4, weights_dir=harness["dir"]), ob.OnnxUpscaler)
    with pytest.raises(TypeError, match="torch"):
        ob.OnnxUpscaler("ultrasharp_v2", 4, harness["dir"])


def test_pipeline_refuses_a_model_this_build_cannot_run(monkeypatch, tmp_path):
    from proxy_scaler import pipeline

    monkeypatch.setattr(pipeline, "model_available", lambda m: False)
    with pytest.raises(ValueError, match="isn't available in this build"):
        pipeline._upscalers_for_targets(MODEL, [1200], tmp_path)


def test_availability_is_false_without_the_package(monkeypatch):
    monkeypatch.setattr(ob, "_AVAILABLE", None)

    def no_ort():
        raise ob.OnnxUnavailable("no package")

    monkeypatch.setattr(ob, "_ort", no_ort)
    assert ob.onnx_available() is False
