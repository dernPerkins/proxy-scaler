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
    _AUTO_TILE_LADDER,
    _LIGHT_MODEL_RETRY_TILE,
    _UNTILED_MIN_FREE,
    _VRAM_CAP_MARGIN,
    _allocator_fraction,
    _bf16_supported,
    _choose_auto_tile,
    _clear_device_cache,
    _current_headroom,
    _estimated_vram_need,
    _is_oom_error,
    _ladder_step_down,
    _max_padded_tile_px,
    _record_observed_headroom,
    _should_return_to_gpu,
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


_GiB = 1024**3
_MiB = 1024**2
# 745x1040 Scryfall card, pad 32 — the geometry the ladder was measured on.
_CARD = dict(width=745, height=1040, pad=32)


def test_max_padded_tile_px() -> None:
    assert _max_padded_tile_px(745, 1040, 0, 32) == 774_800
    assert _max_padded_tile_px(745, 1040, 640, 32) == 451_584
    assert _max_padded_tile_px(745, 1040, 512, 32) == 304_640
    assert _max_padded_tile_px(745, 1040, 384, 32) == 186_368
    assert _max_padded_tile_px(745, 1040, 256, 32) == 102_400
    # An image no bigger than the tile runs untiled (mirrors _run_inference).
    assert _max_padded_tile_px(745, 1040, 745, 32) == 774_800
    assert _max_padded_tile_px(745, 1040, 768, 32) == 774_800


def test_estimated_vram_need_tracks_measured_peaks() -> None:
    """Guards the cost-model constants against the 3080 Ti measurements
    (allocated peaks, bf16, headroom 1.0) to within 0.2 GiB."""
    measured = {640: 3.66, 512: 2.59, 384: 1.63, 256: 0.98}
    for tile, gib in measured.items():
        est = _estimated_vram_need(745, 1040, tile, 32, "bf16", 1.0) / _GiB
        assert abs(est - gib) < 0.2, (tile, est)
    # The untiled rung (6.27 GiB measured) is pinned to its explicit floor.
    assert _estimated_vram_need(745, 1040, 0, 32, "bf16", 1.0) == _UNTILED_MIN_FREE
    assert _estimated_vram_need(745, 1040, 0, 32, "bf16", 1.4) == _UNTILED_MIN_FREE
    # fp32 doubles the activation term: 3.24 GiB measured at 384.
    assert abs(_estimated_vram_need(745, 1040, 384, 32, "fp32", 1.0) / _GiB - 3.24) < 0.2


def test_choose_auto_tile() -> None:
    def pick(free_gib: float, dtype: str = "bf16", **kw) -> int:
        return _choose_auto_tile(
            int(free_gib * _GiB), dtype, DEFAULT_TILE_SIZE, headroom=1.4, **_CARD, **kw
        )

    assert pick(10) == 0  # untiled
    # 8.5 GiB: the formula alone (~8.8 at 1.4x) is close, but the explicit
    # 9 GiB untiled floor bites — a 10 GB card never gets the untiled pass.
    assert pick(8.5) == 640
    assert pick(4) == 512
    assert pick(2.5) == 384
    assert pick(2.5, "fp32") == 256  # fp32 activations are twice the size
    assert pick(1) == 256  # floor: never below the last GPU rung
    # First-task conservatism (2x headroom) holds the same GPU one rung lower.
    assert _choose_auto_tile(10 * _GiB, "bf16", DEFAULT_TILE_SIZE, headroom=2.0, **_CARD) == 640
    # OOM retry path: only rungs strictly below the failed one.
    assert pick(10, below=640) == 512
    assert pick(2.5, below=0) == 384
    # Unknown free VRAM: benefit of the doubt (base), never a blind change.
    assert _choose_auto_tile(None, "bf16", DEFAULT_TILE_SIZE, **_CARD) == DEFAULT_TILE_SIZE
    # Untiled light model (base 0) is never touched.
    assert _choose_auto_tile(10 * _GiB, "bf16", 0, **_CARD) == 0


def test_ladder_step_down() -> None:
    assert _ladder_step_down(0) == 640
    assert _ladder_step_down(640) == 512
    assert _ladder_step_down(384) == 256
    assert _ladder_step_down(256) is None


def test_allocator_fraction() -> None:
    total = 12 * _GiB
    # Plenty free, nothing reserved: cap = free - margin.
    assert _allocator_fraction(11 * _GiB, 0, total) == (11 * _GiB - _VRAM_CAP_MARGIN) / total
    # This process's own arena is reusable, so it counts towards the cap.
    assert _allocator_fraction(4 * _GiB, 7 * _GiB, total) == (11 * _GiB - _VRAM_CAP_MARGIN) / total
    # Never above 1.0 (mem_get_info can report more free than expected).
    assert _allocator_fraction(13 * _GiB, 0, total) == 1.0
    # Never below the 1 GiB floor: light models still get to try.
    assert _allocator_fraction(100 * _MiB, 0, total) == _GiB / total
    # Unusable total.
    assert _allocator_fraction(_GiB, 0, 0) is None


def test_cap_allocator_to_free_sets_fraction_from_mem_get_info(tmp_path) -> None:
    up = Upscaler(model=UpscaleModel.ULTRASHARP_V2, scale=4, weights_dir=tmp_path, tile=0)
    up._device = _CudaDev()  # type: ignore[assignment]
    calls: list[float] = []
    with (
        patch("torch.cuda.mem_get_info", return_value=(8 * _GiB, 16 * _GiB)),
        patch("torch.cuda.memory_reserved", return_value=2 * _GiB),
        patch("torch.cuda.set_per_process_memory_fraction", side_effect=calls.append),
    ):
        fraction = up._cap_allocator_to_free()
    assert calls == [fraction]
    assert fraction == (10 * _GiB - _VRAM_CAP_MARGIN) / (16 * _GiB)
    # Non-CUDA devices (CPU, MPS, DirectML): no-op.
    up._device = torch.device("cpu")
    assert up._cap_allocator_to_free() is None


def test_upscale_caps_allocator_per_task_and_per_retry(tmp_path) -> None:
    """The cap is applied before every task's pick and again on each OOM
    retry (free VRAM may have moved), so a pass that doesn't fit raises
    inside torch instead of spilling to system RAM on Windows."""
    up = _heavy_auto(tmp_path)
    with patch.object(up, "_cap_allocator_to_free") as cap:
        _drive_upscale(up, oom_attempts=0, probe=lambda: 16 * _GiB)
    assert cap.call_count == 1
    up = _heavy_auto(tmp_path)
    with patch.object(up, "_cap_allocator_to_free") as cap:
        _drive_upscale(up, oom_attempts=2, probe=lambda: 16 * _GiB)
    assert cap.call_count == 3  # task start + two retries


def test_observed_headroom_calibration() -> None:
    with (
        patch("proxy_scaler.upscale._OBSERVED_HEADROOM", None),
        patch("proxy_scaler.upscale._PEAK_ALLOCATED_SEEN", 0),
    ):
        assert _current_headroom() == 2.0  # first task: conservative
        _record_observed_headroom(reserved=2 * _GiB, allocated=1 * _GiB)
        assert abs(_current_headroom() - 2.3) < 1e-9  # 2.0 x 1.15
        _record_observed_headroom(reserved=int(1.05 * _GiB), allocated=1 * _GiB)
        assert _current_headroom() == 1.4  # expandable segments: default floor
        # Small passes (light models) are too noisy to calibrate from.
        _record_observed_headroom(reserved=800 * 1024**2, allocated=400 * 1024**2)
        assert _current_headroom() == 1.4
        _record_observed_headroom(reserved=0, allocated=0)  # ignored
        assert _current_headroom() == 1.4


def test_observed_headroom_ignores_passes_below_the_peak() -> None:
    """A smaller pass reuses the arena an earlier, larger pass reserved, so
    its reserved/allocated is not a measurement of itself -- calibrating from
    it ratchets the gate up and walks the ladder down to its floor for the
    rest of the process. Only a new allocated high-water re-measures."""
    with (
        patch("proxy_scaler.upscale._OBSERVED_HEADROOM", None),
        patch("proxy_scaler.upscale._PEAK_ALLOCATED_SEEN", 0),
    ):
        # A big pass: 12.43 GiB reserved against 6.27 GiB allocated.
        _record_observed_headroom(reserved=12_430 * _MiB, allocated=6_270 * _MiB)
        calibrated = _current_headroom()
        assert calibrated != 2.0  # moved off the first-task default
        # Smaller passes still see the big pass's arena; none may calibrate.
        for allocated_mib in (3_660, 2_590, 1_630):
            _record_observed_headroom(
                reserved=12_430 * _MiB, allocated=allocated_mib * _MiB
            )
            assert _current_headroom() == calibrated
        # A new high-water does re-measure.
        _record_observed_headroom(reserved=13_000 * _MiB, allocated=7_000 * _MiB)
        assert abs(_current_headroom() - (13_000 / 7_000 * 1.15)) < 1e-9


class _CudaDev:
    type = "cuda"

    def __str__(self) -> str:
        return "cuda"


def _drive_upscale(up: Upscaler, *, oom_attempts: int, probe):
    """Run up.upscale() on a card-sized image with a fake cuda device
    (the model tensor is a tiny 16x16 stand-in; the ladder only reads the
    PIL image's size). The first `oom_attempts` inference calls OOM.
    Headroom is pinned at the post-calibration 1.4x. Returns
    (result, inference attempts, relocate mock)."""
    up._descriptor = MagicMock()
    up._dtype = torch.bfloat16
    attempts = {"n": 0}

    def inference(descriptor, tensor):
        attempts["n"] += 1
        if attempts["n"] <= oom_attempts:
            raise torch.cuda.OutOfMemoryError("CUDA out of memory")
        _, _, h, w = tensor.shape
        return torch.zeros(1, 3, h * 4, w * 4)

    def relocate():
        up._device = torch.device("cpu")
        return up._descriptor

    batch = torch.zeros(1, 3, 16, 16)
    fake_rgb = MagicMock()
    fake_rgb.unsqueeze.return_value.to.return_value = batch
    src = Image.new("RGB", (745, 1040), color=(1, 2, 3))
    with (
        patch.object(up, "_ensure_model", return_value=up._descriptor),
        patch.object(up, "_run_inference", side_effect=inference),
        patch.object(up, "_relocate_to_cpu", side_effect=relocate) as relocate_mock,
        patch.object(up, "_probe_free_vram", side_effect=probe),
        patch("proxy_scaler.upscale._clear_device_cache"),
        patch("proxy_scaler.upscale._OBSERVED_HEADROOM", 1.4),
        patch("proxy_scaler.upscale._PEAK_ALLOCATED_SEEN", 0),
        patch("torchvision.transforms.functional.to_tensor", return_value=fake_rgb),
    ):
        up._device = _CudaDev()  # type: ignore[assignment]
        result = up.upscale(src)
    return result, attempts["n"], relocate_mock


def _heavy_auto(tmp_path) -> Upscaler:
    return Upscaler(
        model=UpscaleModel.ULTRASHARP_V2, scale=4, weights_dir=tmp_path,
        tile=DEFAULT_TILE_SIZE, tile_auto=True,
    )


def test_upscale_oom_retries_smaller_tile_before_cpu(tmp_path) -> None:
    """An auto-chosen rung that OOMs drops down the ladder on the SAME
    device first; CPU relocation only happens if every rung OOMs."""
    up = _heavy_auto(tmp_path)
    result, attempts, relocate = _drive_upscale(up, oom_attempts=1, probe=lambda: 16 * _GiB)
    relocate.assert_not_called()  # stayed on the GPU
    assert attempts == 2  # untiled OOM, 640 ok
    assert up.tile == 640
    assert result.image.size == (64, 64)


def test_upscale_oom_ladder_falls_to_cpu_after_every_rung(tmp_path) -> None:
    up = _heavy_auto(tmp_path)
    result, attempts, relocate = _drive_upscale(
        up, oom_attempts=len(_AUTO_TILE_LADDER), probe=lambda: 16 * _GiB
    )
    assert attempts == len(_AUTO_TILE_LADDER) + 1  # every rung OOMs, CPU succeeds
    relocate.assert_called_once()
    assert result.device == "cpu"


def test_upscale_oom_retry_jumps_to_reprobed_rung(tmp_path) -> None:
    """The retry re-probes free VRAM and jumps to what fits instead of
    walking one rung at a time: untiled OOM with 2.5 GiB free → 384."""
    up = _heavy_auto(tmp_path)
    probes = iter([16 * _GiB, int(2.5 * _GiB)])
    result, attempts, relocate = _drive_upscale(up, oom_attempts=1, probe=lambda: next(probes))
    relocate.assert_not_called()
    assert attempts == 2
    assert up.tile == 384


def test_upscale_oom_fresh_384_steps_down_to_256_before_cpu(tmp_path) -> None:
    """The gap the ladder closes: a small GPU whose auto-384 OOMs used to
    fall STRAIGHT to CPU. With no VRAM reading to go on it still steps one
    rung down on the same device."""
    up = _heavy_auto(tmp_path)
    result, attempts, relocate = _drive_upscale(up, oom_attempts=1, probe=lambda: None)
    relocate.assert_not_called()
    assert attempts == 2
    assert up.tile == 256
    assert result.image.size == (64, 64)


def test_upscale_oom_light_model_gets_one_tiled_retry_before_cpu(tmp_path) -> None:
    """Untiled light models (auto tile 0) retry once at 384, then CPU."""
    def light() -> Upscaler:
        return Upscaler(
            model=UpscaleModel.REALESRGAN_ANIME_FAST, scale=4, weights_dir=tmp_path,
            tile=0, tile_auto=True,
        )

    up = light()
    result, attempts, relocate = _drive_upscale(up, oom_attempts=1, probe=lambda: 16 * _GiB)
    relocate.assert_not_called()
    assert attempts == 2
    assert up.tile == _LIGHT_MODEL_RETRY_TILE
    assert result.device == "gpu"

    up = light()
    result, attempts, relocate = _drive_upscale(up, oom_attempts=2, probe=lambda: 16 * _GiB)
    assert attempts == 3  # untiled OOM, 384 OOM, CPU ok
    relocate.assert_called_once()
    assert result.device == "cpu"


def test_apply_auto_tile_repicks_from_base_each_task(tmp_path) -> None:
    """A previous OOM step-down on the instance never pins a later pick."""
    up = _heavy_auto(tmp_path)
    up.tile = 256
    _, attempts, _ = _drive_upscale(up, oom_attempts=0, probe=lambda: 16 * _GiB)
    assert attempts == 1
    assert up.tile == 0
    # Light models and manual settings are left exactly as constructed.
    light = Upscaler(model=UpscaleModel.ULTRASHARP_V2_LITE, weights_dir=tmp_path, tile=0, tile_auto=True)
    _drive_upscale(light, oom_attempts=0, probe=lambda: 16 * _GiB)
    assert light.tile == 0
    manual = Upscaler(model=UpscaleModel.ULTRASHARP_V2, weights_dir=tmp_path, tile=512, tile_auto=False)
    _drive_upscale(manual, oom_attempts=0, probe=lambda: 16 * _GiB)
    assert manual.tile == 512


def test_upscale_oom_manual_tile_goes_straight_to_cpu(tmp_path) -> None:
    """An explicit user tile setting is never second-guessed: OOM at a
    manual tile skips the ladder and relocates to CPU as before."""
    up = Upscaler(
        model=UpscaleModel.ULTRASHARP_V2, scale=4, weights_dir=tmp_path,
        tile=512, tile_auto=False,
    )
    up._descriptor = MagicMock()

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

    def relocate():
        up._device = torch.device("cpu")
        return up._descriptor

    batch = torch.zeros(1, 3, 16, 16)
    fake_rgb = MagicMock()
    fake_rgb.unsqueeze.return_value.to.return_value = batch

    src = Image.new("RGB", (16, 16), color=(1, 2, 3))
    with (
        patch.object(up, "_ensure_model", return_value=up._descriptor),
        patch.object(up, "_run_inference", side_effect=oom_once),
        patch.object(up, "_relocate_to_cpu", side_effect=relocate),
        patch("proxy_scaler.upscale._clear_device_cache"),
        patch("torchvision.transforms.functional.to_tensor", return_value=fake_rgb),
    ):
        up._device = _CudaDev()  # type: ignore[assignment]
        result = up.upscale(src)

    assert attempts["n"] == 2  # manual 512 OOM, CPU success — no rungs
    assert up.tile == 512
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


# --- one-slot model cache (see #1: cache the loaded model across tasks) ------


class _TinyDescriptor:
    """Stands in for a spandrel descriptor: just enough for the cache's
    device/dtype derivation (a .model with one real parameter)."""

    def __init__(self, device="cpu", dtype=torch.float32):
        self.model = torch.nn.Linear(1, 1).to(device=device, dtype=dtype)

    def to(self, target):  # spandrel's descriptor moves in place and returns self
        self.model = self.model.to(target)
        return self

    def eval(self):
        return self


def _fresh_upscaler(tmp_path, **kw) -> Upscaler:
    return Upscaler(
        model=UpscaleModel.ULTRASHARP_V2, scale=4, weights_dir=tmp_path, tile=0, **kw
    )


def test_model_cache_hit_skips_reload(tmp_path, monkeypatch) -> None:
    from proxy_scaler import upscale as upscale_module

    monkeypatch.setattr(upscale_module, "_MODEL_CACHE", {})
    # The stub loads onto the CPU; on a CUDA box the return-to-GPU path
    # would otherwise (rightly) try to move it.
    monkeypatch.setattr(upscale_module, "resolve_device", lambda: torch.device("cpu"))
    loads = {"n": 0}
    shared = _TinyDescriptor()

    def fake_load(self):
        loads["n"] += 1
        return shared

    monkeypatch.setattr(Upscaler, "_load_model", fake_load)

    first = _fresh_upscaler(tmp_path)
    assert first._ensure_model() is shared
    second = _fresh_upscaler(tmp_path)
    assert second._ensure_model() is shared

    assert loads["n"] == 1  # second instance hit the cache
    assert second._device == torch.device("cpu")
    assert second._dtype == torch.float32


def test_model_cache_one_slot_evicts_other_model(tmp_path, monkeypatch) -> None:
    from proxy_scaler import upscale as upscale_module

    monkeypatch.setattr(upscale_module, "_MODEL_CACHE", {})
    monkeypatch.setattr(upscale_module, "resolve_device", lambda: torch.device("cpu"))
    loads = {"n": 0}

    def fake_load(self):
        loads["n"] += 1
        return _TinyDescriptor()

    monkeypatch.setattr(Upscaler, "_load_model", fake_load)
    cleared = []
    monkeypatch.setattr(upscale_module, "_clear_device_cache", cleared.append)

    _fresh_upscaler(tmp_path)._ensure_model()
    other = Upscaler(
        model=UpscaleModel.REALESRGAN_ANIME_FAST, scale=4, weights_dir=tmp_path, tile=0
    )
    other._ensure_model()

    assert loads["n"] == 2
    assert len(upscale_module._MODEL_CACHE) == 1  # one slot, old model evicted
    assert cleared  # eviction released the old descriptor's device cache
    # Switching back re-loads (the point of one slot: bounded VRAM).
    _fresh_upscaler(tmp_path)._ensure_model()
    assert loads["n"] == 3


def test_cache_hit_derives_state_from_relocated_descriptor(
    tmp_path, monkeypatch
) -> None:
    """After _relocate_to_cpu() moved the shared descriptor to CPU/fp32, a
    later task's cache hit must describe it truthfully — not through stale
    metadata claiming it still lives on the GPU in bf16."""
    from proxy_scaler import upscale as upscale_module
    from proxy_scaler.upscale import _cache_key

    relocated = _TinyDescriptor(device="cpu", dtype=torch.float32)
    key = _cache_key(UpscaleModel.ULTRASHARP_V2, 4, tmp_path)
    monkeypatch.setattr(upscale_module, "_MODEL_CACHE", {key: relocated})
    # No GPU to return to (the return path has its own tests below); pinned
    # so this doesn't depend on whether the test box has a CUDA device.
    monkeypatch.setattr(upscale_module, "resolve_device", lambda: torch.device("cpu"))

    up = _fresh_upscaler(tmp_path)
    assert up._ensure_model() is relocated
    assert up._device.type == "cpu"
    assert up._dtype == torch.float32


# --- Returning to the GPU after an earlier task's CPU fallback ----------------


def test_should_return_to_gpu() -> None:
    assert not _should_return_to_gpu(None)  # no CUDA / probe failed
    assert not _should_return_to_gpu(4 * _GiB - 1)
    assert _should_return_to_gpu(4 * _GiB)


def _seed_relocated_cache(tmp_path, monkeypatch, *, device, free):
    """A cached descriptor already parked on the CPU by an earlier task's
    fallback, with resolve_device()/free VRAM pinned to the scenario."""
    from proxy_scaler import upscale as upscale_module
    from proxy_scaler.upscale import _cache_key

    relocated = _TinyDescriptor(device="cpu", dtype=torch.float32)
    key = _cache_key(UpscaleModel.ULTRASHARP_V2, 4, tmp_path)
    monkeypatch.setattr(upscale_module, "_MODEL_CACHE", {key: relocated})
    monkeypatch.setattr(upscale_module, "resolve_device", lambda: torch.device(device))
    monkeypatch.setattr(upscale_module, "_free_cuda_vram", lambda: free)
    return relocated


def test_cache_hit_returns_relocated_descriptor_to_gpu(tmp_path, monkeypatch) -> None:
    """Plenty of VRAM again and a CUDA device to go to: the cache hit moves
    the parked descriptor back before deriving this task's device."""
    relocated = _seed_relocated_cache(tmp_path, monkeypatch, device="cuda", free=12 * _GiB)
    up = _fresh_upscaler(tmp_path)
    with patch.object(Upscaler, "_return_to_gpu", return_value=relocated) as ret:
        assert up._ensure_model() is relocated
    ret.assert_called_once()
    assert ret.call_args.args[0] is relocated
    assert ret.call_args.args[1] == torch.device("cuda")


def test_cache_hit_stays_on_cpu_when_vram_is_still_tight(tmp_path, monkeypatch) -> None:
    _seed_relocated_cache(tmp_path, monkeypatch, device="cuda", free=1 * _GiB)
    up = _fresh_upscaler(tmp_path)
    with patch.object(Upscaler, "_return_to_gpu") as ret:
        up._ensure_model()
    ret.assert_not_called()
    assert up._device.type == "cpu"
    assert up._dtype == torch.float32


def test_cache_hit_stays_on_cpu_without_cuda(tmp_path, monkeypatch) -> None:
    """MPS/DirectML/CPU-only boxes have no free-VRAM probe; the return
    path is CUDA-only and must not fire even with 'free' reported."""
    _seed_relocated_cache(tmp_path, monkeypatch, device="cpu", free=12 * _GiB)
    up = _fresh_upscaler(tmp_path)
    with patch.object(Upscaler, "_return_to_gpu") as ret:
        up._ensure_model()
    ret.assert_not_called()
    assert up._device.type == "cpu"


def test_return_to_gpu_survives_a_failed_move(tmp_path, monkeypatch, capsys) -> None:
    """The move itself OOMing (or anything else) must leave the descriptor
    usable on the CPU and never break the task that tried."""
    from proxy_scaler import upscale as upscale_module

    class _Stuck(_TinyDescriptor):
        def to(self, target):
            if isinstance(target, torch.device) and target.type == "cuda":
                raise torch.cuda.OutOfMemoryError("CUDA out of memory")
            return super().to(target)

    stuck = _Stuck(device="cpu", dtype=torch.float32)
    monkeypatch.setattr(upscale_module, "_free_cuda_vram", lambda: 12 * _GiB)
    up = _fresh_upscaler(tmp_path)
    with (
        patch.object(up, "_cap_allocator_to_free", return_value=None) as cap,
        patch("proxy_scaler.upscale._clear_device_cache") as clear_mock,
    ):
        out = up._return_to_gpu(stuck, torch.device("cuda"))
    assert out is stuck
    assert up._device.type == "cpu"
    assert next(out.model.parameters()).device.type == "cpu"
    cap.assert_called_once()  # cap recomputed before the move was attempted
    clear_mock.assert_called_once_with(torch.device("cuda"))
    assert "staying on CPU" in capsys.readouterr().out


def test_upscale_no_longer_clears_cache_per_image(tmp_path) -> None:
    up = _fresh_upscaler(tmp_path)
    up._descriptor = MagicMock()
    up._device = torch.device("cpu")

    def fake_inference(descriptor, tensor):
        _, _, h, w = tensor.shape
        return torch.zeros(1, 3, h * 4, w * 4)

    src = Image.new("RGB", (16, 16), color=(10, 20, 30))
    with (
        patch.object(up, "_ensure_model", return_value=up._descriptor),
        patch.object(up, "_run_inference", side_effect=fake_inference),
        patch("proxy_scaler.upscale._clear_device_cache") as clear_mock,
    ):
        up.upscale(src)

    clear_mock.assert_not_called()  # happy path leaves the allocator warm


# --- CPU-fallback notification hook ------------------------------------------


def test_relocate_to_cpu_fires_fallback_hook() -> None:
    fired = {"n": 0}
    up = Upscaler(
        model=UpscaleModel.ULTRASHARP_V2, scale=4, weights_dir="w", tile=0,
        on_cpu_fallback=lambda: fired.__setitem__("n", fired["n"] + 1),
    )
    up._descriptor = MagicMock()
    up._descriptor.to.return_value.to.return_value.eval.return_value = up._descriptor
    up._device = torch.device("cpu")
    with patch("proxy_scaler.upscale._clear_device_cache"):
        up._relocate_to_cpu()
    assert fired["n"] == 1


def test_load_model_oom_fallback_fires_hook(tmp_path, monkeypatch) -> None:
    """The model-load OOM branch (previously untested) also notifies."""
    from proxy_scaler import upscale as upscale_module

    fired = {"n": 0}
    up = Upscaler(
        model=UpscaleModel.ULTRASHARP_V2, scale=4, weights_dir=tmp_path, tile=0,
        on_cpu_fallback=lambda: fired.__setitem__("n", fired["n"] + 1),
    )

    weights = tmp_path / "w.safetensors"
    weights.write_bytes(b"x")
    monkeypatch.setattr(upscale_module, "ensure_weights", lambda *a, **k: weights)
    monkeypatch.setattr(upscale_module, "resolve_device", lambda: torch.device("cuda"))
    monkeypatch.setattr(upscale_module, "resolve_dtype", lambda d, dev: torch.float32)
    monkeypatch.setattr(upscale_module, "_clear_device_cache", lambda d: None)

    class _FakeDescriptor:
        def to(self, target):
            if getattr(target, "type", None) == "cuda":
                raise torch.cuda.OutOfMemoryError("CUDA out of memory")
            return self

        def eval(self):
            return self

    fake = _FakeDescriptor()

    class _FakeLoader:
        def load_from_file(self, path):
            return fake

    import spandrel

    monkeypatch.setattr(spandrel, "ModelLoader", _FakeLoader)
    monkeypatch.setattr(
        spandrel, "ImageModelDescriptor", _FakeDescriptor, raising=False
    )
    monkeypatch.setattr(
        upscale_module, "ImageModelDescriptor", _FakeDescriptor, raising=False
    )

    result = up._load_model()
    assert result is fake
    assert fired["n"] == 1


def test_fallback_hook_errors_never_break_relocation() -> None:
    def boom():
        raise RuntimeError("hook exploded")

    up = Upscaler(
        model=UpscaleModel.ULTRASHARP_V2, scale=4, weights_dir="w", tile=0,
        on_cpu_fallback=boom,
    )
    up._descriptor = MagicMock()
    up._descriptor.to.return_value.to.return_value.eval.return_value = up._descriptor
    up._device = torch.device("cpu")
    with patch("proxy_scaler.upscale._clear_device_cache"):
        up._relocate_to_cpu()  # must not raise
    assert up._dtype == torch.float32


# --- bf16 that returns NaN falls back to fp32 (ROCm gfx1102 report) --------


def test_dtype_policy_env_override(monkeypatch) -> None:
    from proxy_scaler import upscale as up

    assert up.dtype_policy({}) == "auto"
    assert up.dtype_policy({up.DTYPE_ENV: " FP32 "}) == "fp32"
    assert up.dtype_policy({up.DTYPE_ENV: "bf16"}) == "bf16"
    assert up.dtype_policy({up.DTYPE_ENV: "nonsense"}) == "auto"

    dev = torch.device("cuda")
    yes = MagicMock(supports_bfloat16=True)
    monkeypatch.setattr(up, "_BF16_DISABLED_REASON", None)
    with patch("proxy_scaler.upscale._bf16_supported", return_value=True):
        monkeypatch.setenv(up.DTYPE_ENV, "fp32")
        assert resolve_dtype(yes, dev) == torch.float32
        monkeypatch.setenv(up.DTYPE_ENV, "bf16")
        assert resolve_dtype(yes, dev) == torch.bfloat16
        monkeypatch.delenv(up.DTYPE_ENV)
        assert resolve_dtype(yes, dev) == torch.bfloat16


def test_resolve_dtype_sticks_to_fp32_after_disable(monkeypatch) -> None:
    from proxy_scaler import upscale as up

    monkeypatch.setattr(up, "_BF16_DISABLED_REASON", None)
    yes = MagicMock(supports_bfloat16=True)
    with patch("proxy_scaler.upscale._bf16_supported", return_value=True):
        assert resolve_dtype(yes, torch.device("cuda")) == torch.bfloat16
        up.disable_bf16("test")
        assert resolve_dtype(yes, torch.device("cuda")) == torch.float32


def test_is_finite_output() -> None:
    from proxy_scaler.upscale import _is_finite_output

    assert _is_finite_output(torch.zeros(2, 2))
    assert not _is_finite_output(torch.tensor([1.0, float("nan")]))
    assert not _is_finite_output(torch.tensor([1.0, float("inf")]))


def test_upscale_nonfinite_bf16_retries_in_fp32(tmp_path, monkeypatch) -> None:
    """A bf16 pass that comes back NaN (the all-black-card signature) is
    redone in fp32 on the same device, the weights are converted in
    place, and bf16 stays off for the rest of the process."""
    from proxy_scaler import upscale as up

    monkeypatch.setattr(up, "_BF16_DISABLED_REASON", None)
    upsc = Upscaler(model=UpscaleModel.ULTRASHARP_V2_LITE, scale=4, weights_dir=tmp_path, tile=0)
    descriptor = MagicMock()
    descriptor.to.return_value.eval.return_value = descriptor
    upsc._descriptor = descriptor
    upsc._device = torch.device("cpu")
    upsc._dtype = torch.bfloat16
    seen: list[torch.dtype] = []

    def fake_inference(_descriptor, tensor):
        seen.append(tensor.dtype)
        _, _, h, w = tensor.shape
        if tensor.dtype == torch.bfloat16:
            return torch.full((1, 3, h * 4, w * 4), float("nan"), dtype=torch.bfloat16)
        return torch.full((1, 3, h * 4, w * 4), 0.5)

    src = Image.new("RGB", (16, 16), color=(10, 20, 30))
    with (
        patch.object(upsc, "_ensure_model", return_value=descriptor),
        patch.object(upsc, "_run_inference", side_effect=fake_inference),
        patch("proxy_scaler.upscale._clear_device_cache"),
    ):
        result = upsc.upscale(src)

    assert seen == [torch.bfloat16, torch.float32]
    assert result.dtype == "fp32"
    assert result.image.getpixel((0, 0)) != (0, 0, 0)
    descriptor.to.assert_called_with(torch.float32)
    assert up._BF16_DISABLED_REASON is not None
    yes = MagicMock(supports_bfloat16=True)
    with patch("proxy_scaler.upscale._bf16_supported", return_value=True):
        assert resolve_dtype(yes, torch.device("cuda")) == torch.float32


def test_upscale_finite_bf16_is_left_alone(tmp_path, monkeypatch) -> None:
    from proxy_scaler import upscale as up

    monkeypatch.setattr(up, "_BF16_DISABLED_REASON", None)
    upsc = Upscaler(model=UpscaleModel.ULTRASHARP_V2_LITE, scale=4, weights_dir=tmp_path, tile=0)
    upsc._descriptor = MagicMock()
    upsc._device = torch.device("cpu")
    upsc._dtype = torch.bfloat16
    calls = 0

    def fake_inference(_descriptor, tensor):
        nonlocal calls
        calls += 1
        _, _, h, w = tensor.shape
        return torch.full((1, 3, h * 4, w * 4), 0.25, dtype=torch.bfloat16)

    with (
        patch.object(upsc, "_ensure_model", return_value=upsc._descriptor),
        patch.object(upsc, "_run_inference", side_effect=fake_inference),
    ):
        result = upsc.upscale(Image.new("RGB", (8, 8)))
    assert calls == 1
    assert result.dtype == "bf16"
    assert up._BF16_DISABLED_REASON is None
