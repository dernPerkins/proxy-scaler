"""Download-time cleanup of Scryfall originals (proxy_scaler/postprocess.py).

Synthetic cards cover the four real-world categories measured in
cards.bleed-samples.txt: phantom-row bone-white renders (SLZ/MB2), modern
white borders with a genuine black collector bar, clean vintage scans, and
ordinary black-border cards.
"""

from __future__ import annotations

import io

from PIL import Image

from proxy_scaler.postprocess import (
    _MAX_PHANTOM_EDGE_PX,
    clean_original_png,
)

_BORDER = (243, 239, 227)  # bone white
_PHANTOM = (19, 12, 12)  # measured SLZ bottom-row color


def _card(
    w: int = 100,
    h: int = 140,
    border: tuple[int, int, int] = _BORDER,
    *,
    radius: int = 12,
    underlay: tuple[int, int, int] = (0, 0, 0),
    phantom_edges: tuple[str, ...] = (),
    phantom_px: int = 1,
    bar_px: int = 0,
) -> Image.Image:
    img = Image.new("RGBA", (w, h), (*border, 255))
    px = img.load()
    if radius:
        for cy, cx, sy, sx in ((0, 0, 1, 1), (0, w - 1, 1, -1), (h - 1, 0, -1, 1), (h - 1, w - 1, -1, -1)):
            for dy in range(radius):
                for dx in range(radius):
                    if (radius - dx) ** 2 + (radius - dy) ** 2 > radius * radius:
                        px[cx + sx * dx, cy + sy * dy] = (*underlay, 0)
    if bar_px:
        for y in range(h - bar_px, h):
            for x in range(w):
                if px[x, y][3] == 255:
                    px[x, y] = (10, 10, 10, 255)
    for edge in phantom_edges:
        for k in range(phantom_px):
            if edge == "bottom":
                coords = [(x, h - 1 - k) for x in range(w)]
            elif edge == "top":
                coords = [(x, k) for x in range(w)]
            elif edge == "left":
                coords = [(k, y) for y in range(h)]
            else:
                coords = [(w - 1 - k, y) for y in range(h)]
            for c in coords:
                if px[c][3] == 255:
                    px[c] = (*_PHANTOM, 255)
    return img


def _png(img: Image.Image) -> bytes:
    buf = io.BytesIO()
    img.save(buf, format="PNG")
    return buf.getvalue()


def _lum(p) -> float:
    return 0.299 * p[0] + 0.587 * p[1] + 0.114 * p[2]


def _alpha_bytes(png_bytes: bytes) -> bytes:
    return Image.open(io.BytesIO(png_bytes)).getchannel("A").tobytes()


def test_phantom_bottom_row_scrubbed_alpha_untouched() -> None:
    # underlay == border so only fixup A can fire — isolates the scrub.
    raw = _png(_card(phantom_edges=("bottom",), underlay=_BORDER))
    result = clean_original_png(raw)
    assert result.applied == ("edge_row_scrub",)
    assert _alpha_bytes(result.png_bytes) == _alpha_bytes(raw)
    out = Image.open(io.BytesIO(result.png_bytes))
    px = out.load()
    w, h = out.size
    assert _lum(px[w // 2, h - 1]) > 150
    assert px[w // 2, h - 1][:3] == _BORDER


def test_phantom_rows_scrubbed_on_every_edge() -> None:
    for edge, probe in (
        ("top", lambda w, h: (w // 2, 0)),
        ("left", lambda w, h: (0, h // 2)),
        ("right", lambda w, h: (w - 1, h // 2)),
    ):
        raw = _png(_card(phantom_edges=(edge,), underlay=_BORDER))
        result = clean_original_png(raw)
        assert result.applied == ("edge_row_scrub",), edge
        out = Image.open(io.BytesIO(result.png_bytes))
        w, h = out.size
        assert _lum(out.getpixel(probe(w, h))) > 150, edge


def test_two_px_phantom_run_fully_scrubbed() -> None:
    raw = _png(_card(phantom_edges=("bottom",), phantom_px=2, underlay=_BORDER))
    result = clean_original_png(raw)
    assert result.applied == ("edge_row_scrub",)
    out = Image.open(io.BytesIO(result.png_bytes))
    px = out.load()
    w, h = out.size
    assert _lum(px[w // 2, h - 1]) > 150
    assert _lum(px[w // 2, h - 2]) > 150


def test_dark_band_deeper_than_max_is_left_alone() -> None:
    # A run past _MAX_PHANTOM_EDGE_PX is card design, not the artifact —
    # partial scrubbing of a real black bar would be worse than none.
    raw = _png(
        _card(
            phantom_edges=("bottom",),
            phantom_px=_MAX_PHANTOM_EDGE_PX + 1,
            underlay=_BORDER,
        )
    )
    result = clean_original_png(raw)
    assert result.applied == ()
    assert result.png_bytes is raw


def test_genuine_collector_bar_is_byte_identical() -> None:
    raw = _png(_card(bar_px=20, underlay=_BORDER))
    result = clean_original_png(raw)
    assert result.applied == ()
    assert result.png_bytes is raw


def test_clean_opaque_card_is_byte_identical() -> None:
    raw = _png(_card(radius=0))
    result = clean_original_png(raw)
    assert result.applied == ()
    assert result.png_bytes is raw


def test_black_border_card_with_black_underlay_is_byte_identical() -> None:
    # Sampled border ≈ (10,10,10), underlay (0,0,0): inside the delta gate,
    # so the vast majority of real cards never get re-encoded.
    raw = _png(_card(border=(10, 10, 10), underlay=(0, 0, 0)))
    result = clean_original_png(raw)
    assert result.applied == ()
    assert result.png_bytes is raw


def test_underlay_recolored_to_border_alpha_untouched() -> None:
    raw = _png(_card(underlay=(0, 0, 0)))
    result = clean_original_png(raw)
    assert result.applied == ("underlay_recolor",)
    assert _alpha_bytes(result.png_bytes) == _alpha_bytes(raw)
    before = Image.open(io.BytesIO(raw))
    after = Image.open(io.BytesIO(result.png_bytes))
    bpx, apx = before.load(), after.load()
    w, h = after.size
    for x in range(w):
        for y in range(h):
            if bpx[x, y][3] == 0:
                assert apx[x, y][:3] == _BORDER, (x, y)
            else:
                assert apx[x, y] == bpx[x, y], (x, y)


def test_tiny_image_passes_through_byte_identical() -> None:
    # Mirrors the test suite's synthetic _fake_png_bytes fakes: anything
    # below the min-dimension guard must survive the pipeline untouched
    # (test_api_export asserts exact original bytes in the ZIP).
    buf = io.BytesIO()
    Image.new("RGB", (8, 8), color=(0, 100, 200)).save(buf, format="PNG")
    raw = buf.getvalue()
    result = clean_original_png(raw)
    assert result.applied == ()
    assert result.png_bytes is raw


def test_non_png_bytes_pass_through() -> None:
    raw = b"not an image at all"
    result = clean_original_png(raw)
    assert result.applied == ()
    assert result.png_bytes is raw
