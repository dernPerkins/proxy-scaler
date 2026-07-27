"""OOM fallback / helper tests for the upscaler (no real GPU required)."""

from __future__ import annotations

from unittest.mock import MagicMock, patch

import torch
from PIL import Image

from proxy_scaler.upscale import (
    Upscaler,
    UpscaleModel,
    UpscaleResult,
    _clear_device_cache,
    _is_oom_error,
    device_kind,
    read_cache_device,
    write_cache_device,
)


def test_is_oom_error_detects_cuda_oom() -> None:
    assert _is_oom_error(torch.cuda.OutOfMemoryError("CUDA out of memory"))
    assert _is_oom_error(RuntimeError("CUDA out of memory. Tried to allocate…"))
    assert not _is_oom_error(ValueError("bad input"))


def test_device_kind() -> None:
    assert device_kind(torch.device("cpu")) == "cpu"
    assert device_kind("cuda") == "gpu"
    assert device_kind("mps") == "gpu"
    assert device_kind(None) == "unknown"


def test_upscale_falls_back_to_cpu_after_oom(tmp_path) -> None:
    """GPU OOM should clear cache and retry once on CPU (no tile retries)."""
    up = Upscaler(model=UpscaleModel.SWINIR, scale=4, weights_dir=tmp_path, tile=0)
    up._descriptor = MagicMock()

    class _CudaDev:
        type = "cuda"

        def __str__(self) -> str:
            return "cuda"

    attempts = {"n": 0}

    def run_oom_then_ok(descriptor, tensor):
        attempts["n"] += 1
        if attempts["n"] == 1:
            raise torch.cuda.OutOfMemoryError("CUDA out of memory")
        _, _, h, w = tensor.shape
        return torch.zeros(1, 3, h * 4, w * 4)

    def relocate():
        up._device = torch.device("cpu")
        return up._descriptor

    batch = torch.zeros(1, 3, 16, 16)
    fake_rgb = MagicMock()
    fake_rgb.unsqueeze.return_value.to.return_value = batch

    src = Image.new("RGB", (16, 16), color=(1, 2, 3))
    with (
        patch.object(up, "_ensure_model", return_value=up._descriptor),
        patch.object(up, "_run_inference", side_effect=run_oom_then_ok),
        patch.object(up, "_relocate_to_cpu", side_effect=relocate),
        patch("proxy_scaler.upscale._clear_device_cache"),
        patch("proxy_scaler.upscale.to_tensor", return_value=fake_rgb),
    ):
        up._device = _CudaDev()  # type: ignore[assignment]
        result = up.upscale(src)

    assert isinstance(result, UpscaleResult)
    assert result.image.size == (64, 64)
    assert result.device == "cpu"
    assert attempts["n"] == 2


def test_upscale_preserves_alpha_corner(tmp_path) -> None:
    """Original alpha (rounded-corner transparency) should survive the RGB-only model."""
    up = Upscaler(model=UpscaleModel.SWINIR, scale=4, weights_dir=tmp_path, tile=0)
    up._descriptor = MagicMock()
    up._device = torch.device("cpu")

    # 16x16 RGBA source: opaque background with an 8x8 transparent corner block.
    src = Image.new("RGBA", (16, 16), color=(10, 20, 30, 255))
    for x in range(8):
        for y in range(8):
            src.putpixel((x, y), (0, 0, 0, 0))

    def fake_inference(descriptor, tensor):
        _, _, h, w = tensor.shape
        return torch.zeros(1, 3, h * 4, w * 4)

    with (
        patch.object(up, "_ensure_model", return_value=up._descriptor),
        patch.object(up, "_run_inference", side_effect=fake_inference),
    ):
        result = up.upscale(src)

    assert result.image.mode == "RGBA"
    assert result.image.size == (64, 64)
    # Deep inside the (now 32x32) transparent corner region.
    assert result.image.getpixel((10, 10))[3] < 10
    # Deep inside the opaque region on the far side.
    assert result.image.getpixel((50, 50))[3] > 245


def test_upscale_no_alpha_source_stays_rgb(tmp_path) -> None:
    """Plain RGB sources (no alpha channel) are returned unchanged, no crash."""
    up = Upscaler(model=UpscaleModel.SWINIR, scale=4, weights_dir=tmp_path, tile=0)
    up._descriptor = MagicMock()
    up._device = torch.device("cpu")

    src = Image.new("RGB", (16, 16), color=(10, 20, 30))

    def fake_inference(descriptor, tensor):
        _, _, h, w = tensor.shape
        return torch.zeros(1, 3, h * 4, w * 4)

    with (
        patch.object(up, "_ensure_model", return_value=up._descriptor),
        patch.object(up, "_run_inference", side_effect=fake_inference),
    ):
        result = up.upscale(src)

    assert result.image.mode == "RGB"
    assert result.image.size == (64, 64)


def test_cache_device_sidecar(tmp_path) -> None:
    png = tmp_path / "x.png"
    png.write_bytes(b"fake")
    assert read_cache_device(png) == "unknown"
    write_cache_device(png, "gpu")
    assert read_cache_device(png) == "gpu"
    write_cache_device(png, "cpu")
    assert read_cache_device(png) == "cpu"


def test_clear_device_cache_noop_on_cpu() -> None:
    _clear_device_cache(torch.device("cpu"))
