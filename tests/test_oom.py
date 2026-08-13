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


def test_device_kind_directml() -> None:
    """torch-directml's device sits on torch's "privateuseone" backend
    (confirmed against the actual package), not a "directml" string —
    device_kind() has to know that mapping explicitly."""

    class _DirectMlDev:
        type = "privateuseone"

        def __str__(self) -> str:
            return "privateuseone"

    assert device_kind(_DirectMlDev()) == "gpu"  # type: ignore[arg-type]
    assert device_kind("privateuseone") == "gpu"
    assert device_kind("directml") == "gpu"


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
        # to_tensor is imported locally inside Upscaler.upscale() (see
        # upscale.py's module docstring on lazy torch/spandrel/torchvision
        # imports) rather than being a module-level attribute of
        # proxy_scaler.upscale, so it must be patched at its real source —
        # the local `from torchvision.transforms.functional import
        # to_tensor` re-resolves this attribute fresh on every call.
        patch("torchvision.transforms.functional.to_tensor", return_value=fake_rgb),
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


def test_tiled_inference_matches_full_pass(tmp_path) -> None:
    """Tiled inference should reconstruct the same result as a single
    full-image pass for a deterministic, per-pixel-independent 'model' —
    validates the tile/pad/crop/stitch math has no off-by-one gaps or
    double-counted overlap, for both an exact tile multiple and a
    non-multiple (ragged last tile) image size."""
    up = Upscaler(model=UpscaleModel.SWINIR, scale=2, weights_dir=tmp_path, tile=8, tile_pad=2)
    up._device = torch.device("cpu")

    def fake_descriptor(tensor: torch.Tensor) -> torch.Tensor:
        return torch.nn.functional.interpolate(tensor, scale_factor=2, mode="nearest")

    torch.manual_seed(0)
    for h, w in [(16, 16), (20, 14)]:  # exact multiple, then ragged
        img = torch.rand(1, 3, h, w)
        full = fake_descriptor(img)
        tiled = up._tiled_inference(fake_descriptor, img)
        assert tiled.shape == full.shape
        assert torch.allclose(tiled, full, atol=1e-5)


def test_run_inference_tiling_gate(tmp_path) -> None:
    """Tiling only kicks in when tile>0 AND the image exceeds the tile size."""
    up = Upscaler(model=UpscaleModel.SWINIR, scale=2, weights_dir=tmp_path, tile=8)
    up._device = torch.device("cpu")
    small = torch.rand(1, 3, 6, 6)  # smaller than tile=8
    large = torch.rand(1, 3, 20, 20)

    with patch.object(up, "_tiled_inference") as tiled_mock:
        up._run_inference(MagicMock(return_value=torch.rand(1, 3, 12, 12)), small)
        tiled_mock.assert_not_called()

        up._run_inference(MagicMock(), large)
        tiled_mock.assert_called_once()

    # tile=0 (disabled) never tiles regardless of image size.
    up_off = Upscaler(model=UpscaleModel.SWINIR, scale=2, weights_dir=tmp_path, tile=0)
    up_off._device = torch.device("cpu")
    with patch.object(up_off, "_tiled_inference") as tiled_mock:
        descriptor = MagicMock(return_value=torch.rand(1, 3, 40, 40))
        up_off._run_inference(descriptor, large)
        tiled_mock.assert_not_called()
        descriptor.assert_called_once()


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


def test_clear_device_cache_noop_on_directml() -> None:
    """privateuseone (DirectML) has no empty_cache()-equivalent to call —
    confirm this silently no-ops rather than raising."""

    class _DirectMlDev:
        type = "privateuseone"

    _clear_device_cache(_DirectMlDev())  # type: ignore[arg-type]
