"""OOM fallback / helper tests for the upscaler (no real GPU required)."""

from __future__ import annotations

from unittest.mock import MagicMock, patch

import torch
from PIL import Image

from proxy_scaler.upscale import (
    DEFAULT_TILE_SIZE,
    Upscaler,
    UpscaleModel,
    UpscaleResult,
    _bf16_supported,
    _choose_auto_tile,
    _clear_device_cache,
    _is_oom_error,
    device_backend,
    device_kind,
    read_cache_device,
    resolve_dtype,
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


def test_device_backend_keeps_backends_distinct() -> None:
    """The counterpart to device_kind(): same inputs, but the real backend
    survives instead of collapsing to "gpu". The client needs this to tell
    Apple MPS from CUDA when choosing a default model."""

    class _DirectMlDev:
        type = "privateuseone"

        def __str__(self) -> str:
            return "privateuseone"

    assert device_backend(torch.device("cpu")) == "cpu"
    assert device_backend(torch.device("cuda")) == "cuda"
    assert device_backend("cuda") == "cuda"
    assert device_backend("mps") == "mps"
    assert device_backend(_DirectMlDev()) == "privateuseone"  # type: ignore[arg-type]
    assert device_backend(None) == "unknown"
    # An indexed device string ("cuda:0") must answer the same as the
    # torch.device form, whose .type has the index stripped already.
    assert device_backend("cuda:0") == "cuda"
    assert device_backend(torch.device("cuda", 0)) == "cuda"


def test_upscale_falls_back_to_cpu_after_oom(tmp_path) -> None:
    """GPU OOM should clear cache and retry once on CPU (no tile retries)."""
    up = Upscaler(model=UpscaleModel.ULTRASHARP_V2, scale=4, weights_dir=tmp_path, tile=0)
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
    up = Upscaler(model=UpscaleModel.ULTRASHARP_V2, scale=4, weights_dir=tmp_path, tile=0)
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
    up = Upscaler(model=UpscaleModel.ULTRASHARP_V2, scale=4, weights_dir=tmp_path, tile=0)
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
    up = Upscaler(model=UpscaleModel.ULTRASHARP_V2, scale=4, weights_dir=tmp_path, tile=8, tile_pad=2)
    up._device = torch.device("cpu")

    def fake_descriptor(tensor: torch.Tensor) -> torch.Tensor:
        return torch.nn.functional.interpolate(tensor, scale_factor=4, mode="nearest")

    torch.manual_seed(0)
    for h, w in [(16, 16), (20, 14)]:  # exact multiple, then ragged
        img = torch.rand(1, 3, h, w)
        full = fake_descriptor(img)
        tiled = up._tiled_inference(fake_descriptor, img)
        assert tiled.shape == full.shape
        assert torch.allclose(tiled, full, atol=1e-5)


def test_run_inference_tiling_gate(tmp_path) -> None:
    """Tiling only kicks in when tile>0 AND the image exceeds the tile size."""
    up = Upscaler(model=UpscaleModel.ULTRASHARP_V2, scale=4, weights_dir=tmp_path, tile=8)
    up._device = torch.device("cpu")
    small = torch.rand(1, 3, 6, 6)  # smaller than tile=8
    large = torch.rand(1, 3, 20, 20)

    with patch.object(up, "_tiled_inference") as tiled_mock:
        up._run_inference(MagicMock(return_value=torch.rand(1, 3, 12, 12)), small)
        tiled_mock.assert_not_called()

        up._run_inference(MagicMock(), large)
        tiled_mock.assert_called_once()

    # tile=0 (disabled) never tiles regardless of image size.
    up_off = Upscaler(model=UpscaleModel.ULTRASHARP_V2, scale=4, weights_dir=tmp_path, tile=0)
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


# --- bf16 / adaptive-tile gates ---------------------------------------------


def test_bf16_supported_gates() -> None:
    assert not _bf16_supported(None)
    assert not _bf16_supported(torch.device("cpu"))

    class _DirectMlDev:
        type = "privateuseone"

    assert not _bf16_supported(_DirectMlDev())  # type: ignore[arg-type]

    class _CudaDev:
        type = "cuda"

    with patch("torch.cuda.is_bf16_supported", return_value=True):
        assert _bf16_supported(_CudaDev())  # type: ignore[arg-type]
    with patch("torch.cuda.is_bf16_supported", return_value=False):
        assert not _bf16_supported(_CudaDev())  # type: ignore[arg-type]


def test_resolve_dtype_needs_model_and_device_support() -> None:
    """bf16 only when spandrel's descriptor flag AND the device both say
    yes — the gate is deliberately model-agnostic (no model-name checks)."""
    dev = torch.device("cuda")
    yes = MagicMock(supports_bfloat16=True)
    no = MagicMock(supports_bfloat16=False)
    with patch("proxy_scaler.upscale._bf16_supported", return_value=True):
        assert resolve_dtype(yes, dev) == torch.bfloat16
        assert resolve_dtype(no, dev) == torch.float32
    with patch("proxy_scaler.upscale._bf16_supported", return_value=False):
        assert resolve_dtype(yes, dev) == torch.float32


def test_choose_auto_tile() -> None:
    lots, little = 10 * 1024**3, 2 * 1024**3
    # Upgrade requires: auto-tiled heavy model (base>0), bf16, enough free.
    assert _choose_auto_tile(lots, torch.bfloat16, DEFAULT_TILE_SIZE) == 768
    assert _choose_auto_tile(little, torch.bfloat16, DEFAULT_TILE_SIZE) == DEFAULT_TILE_SIZE
    assert _choose_auto_tile(lots, torch.float32, DEFAULT_TILE_SIZE) == DEFAULT_TILE_SIZE
    assert _choose_auto_tile(lots, torch.bfloat16, 0) == 0  # untiled light model
    assert _choose_auto_tile(None, torch.bfloat16, DEFAULT_TILE_SIZE) == DEFAULT_TILE_SIZE


def test_upscale_oom_retries_smaller_tile_before_cpu(tmp_path) -> None:
    """An auto-upgraded tile that OOMs drops back to the base tile on the
    SAME device first; CPU relocation only happens if that also OOMs."""
    up = Upscaler(
        model=UpscaleModel.ULTRASHARP_V2, scale=4, weights_dir=tmp_path,
        tile=768, tile_auto=True,
    )
    up._descriptor = MagicMock()
    up._tile_upgraded = True

    class _CudaDev:
        type = "cuda"

        def __str__(self) -> str:
            return "cuda"

    attempts = {"n": 0}

    def oom_once(descriptor, tensor):
        attempts["n"] += 1
        if attempts["n"] == 1:
            raise torch.cuda.OutOfMemoryError("CUDA out of memory")
        _, _, h, w = tensor.shape
        return torch.zeros(1, 3, h * 4, w * 4)

    batch = torch.zeros(1, 3, 16, 16)
    fake_rgb = MagicMock()
    fake_rgb.unsqueeze.return_value.to.return_value = batch

    src = Image.new("RGB", (16, 16), color=(1, 2, 3))
    with (
        patch.object(up, "_ensure_model", return_value=up._descriptor),
        patch.object(up, "_run_inference", side_effect=oom_once),
        patch.object(up, "_relocate_to_cpu") as relocate_mock,
        patch("proxy_scaler.upscale._clear_device_cache"),
        patch("torchvision.transforms.functional.to_tensor", return_value=fake_rgb),
    ):
        up._device = _CudaDev()  # type: ignore[assignment]
        result = up.upscale(src)

    relocate_mock.assert_not_called()  # stayed on the GPU
    assert attempts["n"] == 2
    assert up.tile == DEFAULT_TILE_SIZE
    assert not up._tile_upgraded
    assert result.image.size == (64, 64)


def test_upscale_oom_ladder_falls_to_cpu_on_second_oom(tmp_path) -> None:
    up = Upscaler(
        model=UpscaleModel.ULTRASHARP_V2, scale=4, weights_dir=tmp_path,
        tile=768, tile_auto=True,
    )
    up._descriptor = MagicMock()
    up._tile_upgraded = True

    class _CudaDev:
        type = "cuda"

        def __str__(self) -> str:
            return "cuda"

    attempts = {"n": 0}

    def oom_twice(descriptor, tensor):
        attempts["n"] += 1
        if attempts["n"] <= 2:
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
        patch.object(up, "_run_inference", side_effect=oom_twice),
        patch.object(up, "_relocate_to_cpu", side_effect=relocate),
        patch("proxy_scaler.upscale._clear_device_cache"),
        patch("torchvision.transforms.functional.to_tensor", return_value=fake_rgb),
    ):
        up._device = _CudaDev()  # type: ignore[assignment]
        result = up.upscale(src)

    assert attempts["n"] == 3  # 768 OOM, 384 OOM, CPU success
    assert result.device == "cpu"


def test_upscale_bf16_output_converts_to_pil(tmp_path) -> None:
    """A bf16 inference result must be converted to float32 before
    to_pil_image (which can't take bf16), and the result reports its dtype."""
    up = Upscaler(model=UpscaleModel.ULTRASHARP_V2, scale=4, weights_dir=tmp_path, tile=0)
    up._descriptor = MagicMock()
    up._device = torch.device("cpu")
    up._dtype = torch.bfloat16

    def fake_inference(descriptor, tensor):
        _, _, h, w = tensor.shape
        return torch.zeros(1, 3, h * 4, w * 4, dtype=torch.bfloat16)

    src = Image.new("RGB", (16, 16), color=(10, 20, 30))
    with (
        patch.object(up, "_ensure_model", return_value=up._descriptor),
        patch.object(up, "_run_inference", side_effect=fake_inference),
    ):
        result = up.upscale(src)

    assert result.image.size == (64, 64)
    assert result.dtype == "bf16"


def test_relocate_to_cpu_resets_dtype() -> None:
    up = Upscaler(model=UpscaleModel.ULTRASHARP_V2, scale=4, weights_dir="w", tile=0)
    up._descriptor = MagicMock()
    up._descriptor.to.return_value.to.return_value.eval.return_value = up._descriptor
    up._device = torch.device("cpu")
    up._dtype = torch.bfloat16
    with patch("proxy_scaler.upscale._clear_device_cache"):
        up._relocate_to_cpu()
    assert up._dtype == torch.float32
