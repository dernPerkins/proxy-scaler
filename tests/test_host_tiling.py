"""host_tiling: the tiling loop and output checks shared by the ncnn and
ONNX backends. Fixed-shape mode is the ONNX exports' contract: every
model call sees exactly the export's (h, w), and the result must equal an
untiled pass."""

from __future__ import annotations

import numpy as np
import pytest

from proxy_scaler import host_tiling as ht


def _ripple_x4(chw: np.ndarray) -> np.ndarray:
    up = np.repeat(np.repeat(chw, 4, axis=1), 4, axis=2)
    return np.clip(up * 0.97 + 0.01, 0.0, 1.0).astype(np.float32)


def _img(h=70, w=45, seed=0):
    return np.random.default_rng(seed).random((3, h, w), dtype=np.float32)


def test_free_size_tiling_matches_untiled():
    img = _img()
    whole = ht.run_tiled(_ripple_x4, img, tile=512, pad=4, scale=4)
    tiled = ht.run_tiled(_ripple_x4, img, tile=16, pad=4, scale=4)
    assert whole.shape == (3, 280, 180)
    assert np.allclose(whole, tiled)


def test_fixed_shape_tiling_only_ever_calls_the_model_at_its_shape_and_matches():
    """Every call is exactly the export's (h, w), rectangular included, and
    the stitched image equals an untiled pass for a position-independent
    model."""
    img = _img(70, 45)
    seen = []

    def model(chw):
        seen.append(chw.shape)
        return _ripple_x4(chw)

    out = ht.run_fixed_tiled(model, img, shape=(24, 32), pad=4, scale=4)
    assert set(seen) == {(3, 24, 32)}
    assert np.allclose(out, _ripple_x4(img))


def test_fixed_shape_small_image_is_one_padded_call():
    img = _img(20, 13)
    seen = []

    def model(chw):
        seen.append(chw.shape)
        return _ripple_x4(chw)

    out = ht.run_fixed_tiled(model, img, shape=(24, 24), pad=4, scale=4)
    assert seen == [(3, 24, 24)]
    assert out.shape == (3, 80, 52) and np.allclose(out, _ripple_x4(img))


def test_fixed_shape_pads_only_the_axis_that_is_too_short():
    img = _img(10, 90)
    seen = []

    def model(chw):
        seen.append(chw.shape)
        return _ripple_x4(chw)

    out = ht.run_fixed_tiled(model, img, shape=(16, 32), pad=4, scale=4)
    assert set(seen) == {(3, 16, 32)} and len(seen) == 4  # ceil((90-8)/24)
    assert np.allclose(out, _ripple_x4(img))


@pytest.mark.parametrize("length", [256, 257, 300, 745, 744, 1040, 5000])
@pytest.mark.parametrize("size,pad", [(192, 32), (256, 32), (320, 32), (416, 32)])
def test_fixed_tile_spans_partition_the_axis_with_context(length, size, pad):
    spans = ht.fixed_tile_spans(length, size, pad)
    if length <= size:
        assert spans == [(0, 0, length)]
        return
    assert spans[0][1] == 0 and spans[-1][2] == length
    for (s0, _, hi0), (s1, lo1, _) in zip(spans, spans[1:]):
        assert hi0 == lo1  # kept spans tile the axis exactly
        assert s1 > s0
    for i, (start, lo, hi) in enumerate(spans):
        assert 0 <= start <= length - size  # every window is real pixels
        assert start <= lo < hi <= start + size
        if i:
            assert lo - start >= pad - 1  # context before (rounding: 1 px)
        if i < len(spans) - 1:
            assert start + size - hi >= pad - 1  # context after
    # No more calls than needed for 2*pad of overlap.
    assert len(spans) == -(-(length - 2 * pad) // (size - 2 * pad))


def test_fixed_shape_rejects_a_shape_with_no_room_inside_the_pad():
    with pytest.raises(ValueError):
        ht.run_fixed_tiled(_ripple_x4, _img(), shape=(8, 24), pad=4, scale=4)


def test_card_tile_counts_per_onnx_tier():
    """The tier shapes are chosen for the standard 745x1040 card; pin the
    number of model calls each one costs."""
    from proxy_scaler.upscale import ONNX_TILE_PAD, ONNX_TILE_PRESETS, onnx_input_shape

    counts = {}
    for p in ONNX_TILE_PRESETS:
        h, w = onnx_input_shape(p.tile)
        assert h % 32 == 0 and w % 32 == 0  # DAT's attention window
        counts[p.key] = len(ht.fixed_tile_spans(1040, h, ONNX_TILE_PAD)) * len(
            ht.fixed_tile_spans(745, w, ONNX_TILE_PAD)
        )
    assert counts == {"low": 48, "medium": 24, "high": 8}


def test_plausibility_and_ladder():
    src = _img(8, 8)
    good = _ripple_x4(src)
    assert ht.plausible(good, src, 4)
    assert not ht.plausible(np.zeros_like(good), src, 4)
    assert not ht.plausible(ht.nearest_upscale(src, 4), src, 4)  # did no work
    # The broadcast identity check equals the explicit blow-up formula.
    nn = ht.nearest_upscale(src, 4)
    explicit = 10 * np.log10(1 / np.mean((np.clip(good, 0, 1) - nn) ** 2))
    assert ht.identity_psnr(good, src, 4) == pytest.approx(explicit, abs=1e-3)
    assert ht.tile_ladder(320, [128, 192, 320, 448]) == [320, 192, 128]
    assert ht.tile_ladder(300, [128, 192, 320]) == [300, 192, 128]
