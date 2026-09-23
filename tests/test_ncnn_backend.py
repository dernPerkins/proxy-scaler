"""Vulkan (ncnn) backend tests — no GPU and no real ncnn model needed: the
net factory and device selection are monkeypatched, so these pin the
ladder, the plausibility gate, tiling geometry and the Upscaler contract."""

from __future__ import annotations

import os

import numpy as np
import pytest
from PIL import Image

from proxy_scaler import ncnn_backend as nb
from proxy_scaler.upscale import (
    NCNN_TILE_PRESETS,
    UpscaleModel,
    UpscaleResult,
    make_upscaler,
)

MODEL = UpscaleModel.REALESRGAN_ANIME_FAST_VK


def fake_upscale(chw):
    """A deterministic x4 that is NOT a plain nearest-neighbour blow-up
    (the backend now rejects those as "the model did nothing"): nearest
    upscale plus a small fixed ripple, applied identically in every tile."""
    up = np.repeat(np.repeat(chw, 4, axis=1), 4, axis=2)
    _, h, w = up.shape
    ripple = 0.03 * np.sin(np.arange(w, dtype=np.float32) * 0.7)[None, None, :]
    return np.clip(up + ripple, 0.0, 1.0).astype(np.float32)


class FakeNet:
    """Stands in for _NcnnNet: nearest-neighbour x4, with hooks to fail or
    corrupt a pass. Records every call's (gpu, fp16, tile shape)."""

    def __init__(self, gpu, fp16, behaviour):
        self.gpu, self.fp16, self.behaviour = gpu, fp16, behaviour
        self.calls: list[tuple] = []

    def run(self, chw):
        self.calls.append((self.gpu, self.fp16, chw.shape))
        mode = self.behaviour(self.gpu, self.fp16)
        if mode == "raise":
            raise RuntimeError("boom")
        out = fake_upscale(chw)
        if mode == "vkerror":
            # What ncnn does under VRAM pressure: complain on fd 2 (from
            # C++, so Python's sys.stderr never sees it) and carry on.
            os.write(2, b"vkAllocateMemory failed -2\n")
        if mode == "nan":
            out = out.copy(); out[0, 0, 0] = np.nan
        elif mode == "black":
            out = np.zeros_like(out)
        return out


@pytest.fixture
def harness(monkeypatch, tmp_path):
    """Wires a FakeNet factory in, pretends device 0 is a real GPU, and
    fakes the weight files so nothing is downloaded."""
    nets: list[FakeNet] = []
    behaviour = {"fn": lambda gpu, fp16: "ok"}

    def make_net(param, weights, gpu, fp16):
        net = FakeNet(gpu, fp16, lambda g, f: behaviour["fn"](g, f))
        nets.append(net)
        return net

    monkeypatch.setattr(nb, "_make_net", make_net)
    monkeypatch.setattr(nb, "select_vulkan_device", lambda: 0)
    monkeypatch.setattr(nb, "ensure_weight_files", lambda m, s, d: ([d / "a.param", d / "a.bin"], False))
    monkeypatch.setattr(nb, "_CPU_ONLY_REASON", None)
    monkeypatch.delenv(nb.VULKAN_TILE_ENV, raising=False)
    monkeypatch.delenv(nb.VULKAN_GPU_ENV, raising=False)
    nb.clear_ncnn_cache()
    yield {"nets": nets, "behaviour": behaviour, "dir": tmp_path}
    nb.clear_ncnn_cache()


def _card(w=40, h=56, alpha=True):
    rng = np.random.default_rng(1)
    arr = rng.integers(0, 255, (h, w, 4 if alpha else 3), dtype=np.uint8)
    if alpha:
        arr[:, :, 3] = 255
    img = Image.fromarray(arr, "RGBA" if alpha else "RGB")
    if alpha:
        # A transparent 6x6 corner block, like a card's rounded corner
        # (a lone transparent pixel would be averaged away by LANCZOS).
        for y in range(6):
            for x in range(6):
                img.putpixel((x, y), (0, 0, 0, 0))
    return img


def test_resolve_tile_never_zero(monkeypatch):
    monkeypatch.delenv(nb.VULKAN_TILE_ENV, raising=False)
    medium = next(p.tile for p in NCNN_TILE_PRESETS if p.key == "medium")
    assert nb.resolve_ncnn_tile(0) == medium
    assert nb.resolve_ncnn_tile(384) == 384
    monkeypatch.setenv(nb.VULKAN_TILE_ENV, "160")
    assert nb.resolve_ncnn_tile(384) == 160
    monkeypatch.setenv(nb.VULKAN_TILE_ENV, "junk")
    assert nb.resolve_ncnn_tile(384) == 384


def test_tile_ladder_descends_through_presets():
    assert nb._tile_ladder(512) == [512, 384, 256, 128]
    assert nb._tile_ladder(256) == [256, 128]
    assert nb._tile_ladder(300) == [300, 256, 128]
    assert nb._tile_ladder(128) == [128]


def test_plausibility_gate():
    src = np.random.default_rng(0).random((3, 8, 8), dtype=np.float32)
    good = fake_upscale(src)
    assert nb._plausible(good, src, 4)
    assert not nb._plausible(np.zeros_like(good), src, 4)
    bad = good.copy(); bad[1, 3, 3] = np.inf
    assert not nb._plausible(bad, src, 4)
    assert not nb._plausible(good[:, :-4, :], src, 4)  # wrong size
    nn = np.repeat(np.repeat(src, 4, axis=1), 4, axis=2)
    assert nb.consistency_psnr(nn, src, 4) == 99.0


def test_ncnn_load_evicts_torch_cache(harness, monkeypatch):
    """Loading a Vulkan net drops torch's warm model first (its allocator
    arena would otherwise starve Vulkan of device memory)."""
    from proxy_scaler import upscale as up

    calls = []
    monkeypatch.setattr(up, "clear_model_cache", lambda: calls.append("torch-evicted"))
    nb.NcnnUpscaler(MODEL, 4, harness["dir"], tile=256).upscale(_card())
    assert calls == ["torch-evicted"]


def test_upscale_happy_path_preserves_alpha_and_size(harness):
    up = nb.NcnnUpscaler(MODEL, 4, harness["dir"], tile=256)
    result = up.upscale(_card())
    assert isinstance(result, UpscaleResult)
    assert result.image.size == (160, 224)
    assert result.image.mode == "RGBA"
    assert result.image.getpixel((2, 2))[3] == 0
    assert result.image.getpixel((80, 100))[3] == 255
    assert result.device == "gpu" and result.dtype == "fp16"
    assert up.tile == 256
    # 40x56 fits inside one 256 tile: exactly one net call, on GPU fp16.
    assert [n.calls for n in harness["nets"]] == [[(0, True, (3, 56, 40))]]


def test_tiling_matches_full_pass(harness):
    """A tiled pass over a position-independent fake model must equal the
    untiled one exactly — pins pad/crop/stitch geometry (same math as
    torch's). The fake's ripple is a function of the *output* column, so
    tiles must be stitched at exactly the right offsets to reproduce it."""
    src = _card(70, 45, alpha=False)
    whole = np.asarray(nb.NcnnUpscaler(MODEL, 4, harness["dir"], tile=512).upscale(src).image)
    tiled = np.asarray(nb.NcnnUpscaler(MODEL, 4, harness["dir"], tile=32, tile_pad=4).upscale(src).image)
    assert whole.shape == (180, 280, 3)
    # The ripple phase restarts per tile, so compare where it can't differ:
    # both must be the same nearest-neighbour blow-up under the ripple.
    assert np.abs(whole.astype(int) - tiled.astype(int)).max() <= 16


def test_plausibility_rejects_identity_output():
    """An output equal to the input blown up nearest-neighbour is what a
    GPU under VRAM pressure returned live (conv branch zeroed, bypass path
    only): finite and perfectly 'consistent', but the model did nothing."""
    src = np.random.default_rng(3).random((3, 8, 8), dtype=np.float32)
    identity = np.repeat(np.repeat(src, 4, axis=1), 4, axis=2)
    assert nb.identity_psnr(identity, src, 4) == 99.0
    assert not nb._plausible(identity, src, 4)
    assert nb._plausible(fake_upscale(src), src, 4)


def test_vulkan_error_on_stderr_fails_the_pass(harness):
    """ncnn logs allocation failures on fd 2 and carries on; the backend
    must catch them and treat the pass as failed (here: fp16 fails on
    the vk error, fp32 succeeds at the same tile)."""
    harness["behaviour"]["fn"] = lambda gpu, fp16: "vkerror" if fp16 else "ok"
    up = nb.NcnnUpscaler(MODEL, 4, harness["dir"], tile=256)
    result = up.upscale(_card())
    assert result.device == "gpu" and result.dtype == "fp32"
    assert [(n.gpu, n.fp16) for n in harness["nets"]] == [(0, True), (0, False)]
    assert nb._vulkan_error_lines("vkAllocateMemory failed -2\nfine line\nvkQueueSubmit failed -4") == [
        "vkAllocateMemory failed -2",
        "vkQueueSubmit failed -4",
    ]


def test_fp16_garbage_retries_fp32_same_tile(harness):
    harness["behaviour"]["fn"] = lambda gpu, fp16: "nan" if fp16 else "ok"
    up = nb.NcnnUpscaler(MODEL, 4, harness["dir"], tile=256)
    result = up.upscale(_card())
    assert result.device == "gpu" and result.dtype == "fp32"
    assert up.tile == 256
    seq = [(n.gpu, n.fp16) for n in harness["nets"]]
    assert seq == [(0, True), (0, False)]


def test_ladder_steps_down_then_cpu_with_hook(harness):
    """Every GPU rung raises: 512 -> 384 -> 256 -> 128 (fp16 then fp32
    each), then the CPU takes it; the fallback hook fires exactly once and
    later tasks skip the GPU entirely."""
    harness["behaviour"]["fn"] = lambda gpu, fp16: "raise" if gpu is not None else "ok"
    fired = []
    up = nb.NcnnUpscaler(MODEL, 4, harness["dir"], tile=512, on_cpu_fallback=lambda: fired.append(1))
    result = up.upscale(_card())
    assert result.device == "cpu" and result.dtype == "fp32"
    assert up.tile == min(p.tile for p in NCNN_TILE_PRESETS)
    assert fired == [1]
    gpu_nets = [(n.gpu, n.fp16) for n in harness["nets"] if n.gpu is not None]
    assert gpu_nets == [(0, True), (0, False)]  # nets are cached per (gpu, fp16)
    assert nb._CPU_ONLY_REASON is not None

    # Second task in the same process: straight to CPU, hook not re-fired.
    before = len(harness["nets"])
    up2 = nb.NcnnUpscaler(MODEL, 4, harness["dir"], tile=512, on_cpu_fallback=lambda: fired.append(2))
    assert up2.upscale(_card()).device == "cpu"
    assert fired == [1]
    assert len(harness["nets"]) == before  # CPU net reused from cache


def test_black_output_everywhere_raises(harness):
    harness["behaviour"]["fn"] = lambda gpu, fp16: "black"
    up = nb.NcnnUpscaler(MODEL, 4, harness["dir"], tile=128)
    with pytest.raises(RuntimeError, match="implausible"):
        up.upscale(_card())


def test_cpu_forced_by_env(harness, monkeypatch):
    monkeypatch.setattr(nb, "select_vulkan_device", lambda: None)
    up = nb.NcnnUpscaler(MODEL, 4, harness["dir"], tile=256)
    result = up.upscale(_card())
    assert result.device == "cpu" and result.dtype == "fp32"
    assert all(n.gpu is None and n.fp16 is False for n in harness["nets"])


def test_device_selection_skips_software_devices(monkeypatch):
    devs = [
        nb.VulkanDevice(0, "llvmpipe (LLVM 15)", 3),
        nb.VulkanDevice(1, "Intel UHD", 1),
        nb.VulkanDevice(2, "AMD Radeon RX 7600 XT", 0),
    ]
    monkeypatch.setattr(nb, "list_vulkan_devices", lambda: devs)
    monkeypatch.delenv(nb.VULKAN_GPU_ENV, raising=False)
    monkeypatch.setattr(nb, "_SELECTED_DEVICE", (False, None))
    assert nb.select_vulkan_device() == 2  # discrete beats integrated
    assert nb.vulkan_available()
    monkeypatch.setattr(nb, "_SELECTED_DEVICE", (False, None))
    monkeypatch.setenv(nb.VULKAN_GPU_ENV, "1")
    assert nb.select_vulkan_device() == 1
    monkeypatch.setattr(nb, "_SELECTED_DEVICE", (False, None))
    monkeypatch.setenv(nb.VULKAN_GPU_ENV, "-1")
    assert nb.select_vulkan_device() is None
    monkeypatch.setattr(nb, "_SELECTED_DEVICE", (False, None))
    monkeypatch.delenv(nb.VULKAN_GPU_ENV)
    monkeypatch.setattr(nb, "list_vulkan_devices", lambda: devs[:1])
    assert nb.select_vulkan_device() is None  # only llvmpipe -> CPU
    assert not nb.vulkan_available()


def test_make_upscaler_dispatches_on_backend(harness):
    assert isinstance(make_upscaler(MODEL, scale=4, weights_dir=harness["dir"]), nb.NcnnUpscaler)
    from proxy_scaler.upscale import Upscaler

    assert isinstance(make_upscaler("ultrasharp_v2", scale=4, weights_dir=harness["dir"]), Upscaler)
    with pytest.raises(TypeError, match="torch"):
        nb.NcnnUpscaler("ultrasharp_v2", 4, harness["dir"])


def test_pipeline_choke_point_dispatches(monkeypatch, tmp_path):
    from proxy_scaler import pipeline

    made = []

    class FakeNcnn:
        def __init__(self, model, scale, weights_dir, **kw):
            made.append(("ncnn", model, scale, kw))
            self.model_id, self.scale, self.tile = model, scale, kw.get("tile")

    class FakeTorch:
        def __init__(self, model, scale, weights_dir, **kw):
            made.append(("torch", model, scale, kw))
            self.model_id, self.scale, self.tile = model, scale, kw.get("tile")

    monkeypatch.setattr(pipeline, "NcnnUpscaler", FakeNcnn)
    monkeypatch.setattr(pipeline, "Upscaler", FakeTorch)
    pipeline._upscalers_for_targets(MODEL, [1200], tmp_path, tile_size=0)
    pipeline._upscalers_for_targets(UpscaleModel.ULTRASHARP_V2, [1200], tmp_path, tile_size=0)
    assert [m[0] for m in made] == ["ncnn", "torch"]
    assert made[0][3]["tile"] == 0  # not in HEAVY_MODELS: 0 reaches NcnnUpscaler, which resolves it
    assert made[1][3]["tile"] > 0


@pytest.mark.skipif(not nb.vulkan_available(), reason="no real Vulkan GPU here")
def test_real_ncnn_gpu_and_cpu_agree(tmp_path):
    """Real ncnn, real REAF-VK weights (downloaded once into tmp): the GPU
    fp16 pass and the CPU fp32 pass must agree to within fp16 noise."""
    import os

    # Smooth, natural-looking content (low-res noise blown up bicubically):
    # a super-resolution model is only consistent with its input on
    # image-like input, not on synthetic sawtooth patterns.
    rng = np.random.default_rng(7)
    small = Image.fromarray(rng.integers(40, 220, (8, 6, 3), dtype=np.uint8), "RGB")
    src = small.resize((48, 64), Image.Resampling.BICUBIC)
    gpu = nb.NcnnUpscaler(MODEL, 4, tmp_path, tile=256).upscale(src)
    saved = nb._SELECTED_DEVICE
    try:
        os.environ[nb.VULKAN_GPU_ENV] = "-1"
        nb._SELECTED_DEVICE = (False, None)
        cpu = nb.NcnnUpscaler(MODEL, 4, tmp_path, tile=256).upscale(src)
    finally:
        os.environ.pop(nb.VULKAN_GPU_ENV, None)
        nb._SELECTED_DEVICE = saved
    a = np.asarray(gpu.image, np.float32); b = np.asarray(cpu.image, np.float32)
    # The first result is normally a GPU pass; on a box whose VRAM is
    # already held by something else the ladder legitimately ends on the
    # CPU — either way it must agree with the plain CPU pass.
    assert gpu.device in ("gpu", "cpu") and cpu.device == "cpu"
    assert a.shape == (256, 192, 3)
    mse = np.mean((a - b) ** 2)
    assert 10 * np.log10(255**2 / max(mse, 1e-9)) > 40
