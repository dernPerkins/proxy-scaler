"""Rounded-rectangle edge extension (proxy_scaler/edge_extend.py) — the one
geometric operation behind both the download-time rim cleanup and the
export-time corner fill + bleed."""

from __future__ import annotations

import numpy as np
from PIL import Image

from proxy_scaler.edge_extend import OPAQUE_MIN, corner_radius_px, extend_edges


def _rounded_alpha(w: int, h: int, radius: int) -> np.ndarray:
    alpha = np.full((h, w), 255, dtype=np.uint8)
    yy, xx = np.mgrid[0:h, 0:w]
    for cy, cx in ((radius, radius), (radius, w - 1 - radius), (h - 1 - radius, radius), (h - 1 - radius, w - 1 - radius)):
        corner = (xx < radius) | (xx > w - 1 - radius)
        corner &= (yy < radius) | (yy > h - 1 - radius)
        outside = (xx - cx) ** 2 + (yy - cy) ** 2 > radius * radius
        alpha[corner & outside] = 0
    return alpha


def _gradient_rgb(w: int, h: int) -> np.ndarray:
    """Distinct colour per pixel, so a wrong source pixel is detectable."""
    yy, xx = np.mgrid[0:h, 0:w]
    return np.stack([xx % 256, yy % 256, (xx + yy) % 256], axis=-1).astype(np.uint8)


def test_corner_radius_measured_from_alpha() -> None:
    alpha = _rounded_alpha(300, 420, 24)
    img = Image.fromarray(np.dstack([np.zeros((420, 300, 3), np.uint8), alpha]), "RGBA")
    assert corner_radius_px(img) == 24


def test_corner_radius_is_zero_without_alpha_or_without_a_corner() -> None:
    assert corner_radius_px(Image.new("RGB", (100, 140))) == 0
    assert corner_radius_px(Image.new("RGBA", (100, 140), (1, 2, 3, 255))) == 0
    # A transparent margin all the way around is not a rounded corner.
    alpha = np.full((140, 100), 255, np.uint8)
    alpha[0, :] = 0
    img = Image.fromarray(np.dstack([np.zeros((140, 100, 3), np.uint8), alpha]), "RGBA")
    assert corner_radius_px(img) == 0


def test_interior_is_copied_verbatim_and_output_is_grown_by_the_bleed() -> None:
    w, h, r, inset, b = 120, 160, 16, 3, 10
    rgb = _gradient_rgb(w, h)
    out = extend_edges(rgb, radius_px=r, inset_px=inset, bleed_px=b, mode="nearest")
    assert out.shape == (h + 2 * b, w + 2 * b, 3)
    # Everything inside the inset rounded rect is untouched.
    core = out[b + r : b + h - r, b + inset : b + w - inset]
    assert np.array_equal(core, rgb[r : h - r, inset : w - inset])


def test_straight_edges_stretch_the_inset_row_and_column() -> None:
    w, h, r, inset, b = 120, 160, 16, 3, 10
    rgb = _gradient_rgb(w, h)
    out = extend_edges(rgb, radius_px=r, inset_px=inset, bleed_px=b, mode="nearest")
    # Bleed above the card and the outer inset rows both carry row `inset`.
    for row in range(0, b + inset):
        assert np.array_equal(out[row, b + r : b + w - r], rgb[inset, r : w - r]), row
    # Left of the card carries column `inset`.
    for col in range(0, b + inset):
        assert np.array_equal(out[b + r : b + h - r, col], rgb[r : h - r, inset]), col


def test_corner_bleed_is_radial_from_the_arc() -> None:
    w, h, r, inset, b = 200, 200, 40, 4, 30
    rgb = np.zeros((h, w, 3), np.uint8)
    rr = r - inset
    # Paint the inset arc's 45° point; the sheet corner's diagonal must
    # sample exactly that point all the way out.
    px = round(r - rr / 2**0.5)
    rgb[px - 1 : px + 2, px - 1 : px + 2] = (200, 10, 10)
    out = extend_edges(rgb, radius_px=r, inset_px=inset, bleed_px=b, mode="nearest")
    for k in range(0, b + px - 2):
        assert tuple(out[k, k]) == (200, 10, 10), k
    # Directly above the arc's centre the source is the arc's top point,
    # which is not the marker.
    assert tuple(out[0, b + r]) == (0, 0, 0)


def test_mirror_reflects_across_the_inset_boundary() -> None:
    w, h, r, inset = 120, 160, 0, 3
    rgb = _gradient_rgb(w, h)
    out = extend_edges(rgb, radius_px=r, inset_px=inset, bleed_px=0, mode="mirror")
    # Square corners: row k (k < inset) becomes row 2*inset - k.
    for k in range(inset):
        assert np.array_equal(out[k, inset : w - inset], rgb[2 * inset - k, inset : w - inset]), k
        assert np.array_equal(out[inset : h - inset, k], rgb[inset : h - inset, 2 * inset - k]), k
    assert np.array_equal(out[inset : h - inset, inset : w - inset], rgb[inset : h - inset, inset : w - inset])


def test_mirror_fills_the_transparent_corner_with_border_texture() -> None:
    w, h, r, inset = 200, 260, 32, 3
    alpha = _rounded_alpha(w, h, r)
    rgb = _gradient_rgb(w, h)
    rgb[alpha == 0] = (255, 255, 255)  # a white underlay, as on the gold template
    out = extend_edges(rgb, radius_px=r, inset_px=inset, bleed_px=0, mode="mirror", alpha=alpha)
    corner = out[:r, :r][alpha[:r, :r] == 0]
    assert not (corner == 255).all(axis=1).any(), "underlay colour leaked through"
    # Every filled pixel is a genuine card pixel (the gradient is unique per
    # position, so this checks membership, not just "not white").
    card_pixels = {tuple(p) for p in rgb[alpha == 255].reshape(-1, 3)}
    assert all(tuple(p) in card_pixels for p in corner)


def test_alpha_aware_fallback_never_samples_a_transparent_pixel() -> None:
    # A square 6x6 transparent notch (not an arc): the radius model would
    # pick sources inside the notch. With alpha given, every output pixel
    # is a real opaque colour.
    w, h = 80, 100
    rgb = np.full((h, w, 3), (10, 20, 30), np.uint8)
    alpha = np.full((h, w), 255, np.uint8)
    rgb[:6, :6] = (0, 0, 0)
    alpha[:6, :6] = 0
    out = extend_edges(rgb, radius_px=6, inset_px=1, bleed_px=4, mode="nearest", alpha=alpha)
    assert (out == (10, 20, 30)).all()


def test_rejects_bad_mode_and_shapes() -> None:
    rgb = np.zeros((10, 10, 3), np.uint8)
    for bad in ("blur", "average"):
        try:
            extend_edges(rgb, radius_px=0, inset_px=1, mode=bad)
        except ValueError:
            pass
        else:
            raise AssertionError(bad)
    try:
        extend_edges(np.zeros((10, 10), np.uint8), radius_px=0, inset_px=1)
    except ValueError:
        pass
    else:
        raise AssertionError("2-D input accepted")
    assert OPAQUE_MIN == 250


def test_corner_inset_can_be_deeper_than_the_edge_inset() -> None:
    # Straight edges re-source a 1px strip, the arcs a 4px one: a pixel
    # 2px in from a straight edge is untouched, a pixel 2px inside the arc
    # (on the 45° diagonal) is not.
    w, h, r = 200, 260, 32
    rgb = _gradient_rgb(w, h)
    out = extend_edges(rgb, radius_px=r, inset_px=1, corner_inset_px=4, bleed_px=0, mode="nearest")
    assert np.array_equal(out[h // 2, 1 : w - 1], rgb[h // 2, 1 : w - 1])
    assert np.array_equal(out[h // 2, 0], rgb[h // 2, 1])
    assert np.array_equal(out[h // 2, w - 1], rgb[h // 2, w - 2])
    k = round(r - (r - 2) / 2**0.5)
    assert not np.array_equal(out[k, k], rgb[k, k])
    k_in = round(r - (r - 6) / 2**0.5)
    assert np.array_equal(out[k_in, k_in], rgb[k_in, k_in])
