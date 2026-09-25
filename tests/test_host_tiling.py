"""host_tiling: the tiling loop and output checks shared by the ncnn and
ONNX backends. Fixed-size mode is the ONNX exports' contract: every model
call sees exactly N x N, and the result must equal an untiled pass."""

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


def test_fixed_size_tiling_only_ever_calls_the_model_at_n_and_matches():
    """Every call is exactly N x N (edge tiles padded up), the padding never
    leaks into the result, and the stitched image equals an untiled pass."""
    img = _img(70, 45)
    seen = []

    def model(chw):
        seen.append(chw.shape)
        return _ripple_x4(chw)

    out = ht.run_tiled(model, img, tile=16, pad=4, scale=4, fixed_size=24)
    assert set(seen) == {(3, 24, 24)}
    assert np.allclose(out, _ripple_x4(img))


def test_fixed_size_small_image_is_one_padded_call():
    img = _img(20, 13)
    seen = []

    def model(chw):
        seen.append(chw.shape)
        return _ripple_x4(chw)

    out = ht.run_tiled(model, img, tile=16, pad=4, scale=4, fixed_size=24)
    assert seen == [(3, 24, 24)]
    assert out.shape == (3, 80, 52) and np.allclose(out, _ripple_x4(img))


def test_fixed_size_rejects_a_tile_that_cannot_fit():
    with pytest.raises(ValueError):
        ht.run_tiled(_ripple_x4, _img(), tile=20, pad=4, scale=4, fixed_size=24)


def test_plausibility_and_ladder():
    src = _img(8, 8)
    good = _ripple_x4(src)
    assert ht.plausible(good, src, 4)
    assert not ht.plausible(np.zeros_like(good), src, 4)
    assert not ht.plausible(ht.nearest_upscale(src, 4), src, 4)  # did no work
    assert ht.tile_ladder(320, [128, 192, 320, 448]) == [320, 192, 128]
    assert ht.tile_ladder(300, [128, 192, 320]) == [300, 192, 128]
