"""Download-time cleanup of Scryfall originals (proxy_scaler/postprocess.py).

Synthetic cards cover the real-world categories measured in
cards.bleed-samples.txt: phantom-row bone-white renders (SLZ/MB2), the
phantom white column (Fellwar Stone), the gold-border template's white
corner rim and lone white edge pixels, modern white borders with a genuine
black collector bar, clean vintage scans, and ordinary black-border cards.
"""

from __future__ import annotations

import io

from PIL import Image

from proxy_scaler.postprocess import (
    FIXUP_NAME,
    CORNER_INSET_PX,
    EDGE_INSET_PX,
    clean_original_png,
)

_BORDER = (243, 239, 227)  # bone white
_PHANTOM = (19, 12, 12)  # measured SLZ bottom-row color
_GOLD = (170, 136, 72)  # measured gold-template border colour


def _card(
    w: int = 100,
    h: int = 140,
    border: tuple[int, int, int] = _BORDER,
    *,
    radius: int = 12,
    underlay: tuple[int, int, int] = (0, 0, 0),
    phantom_edges: tuple[str, ...] = (),
    phantom_px: int = 1,
    phantom_color: tuple[int, int, int] = _PHANTOM,
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
                    px[c] = (*phantom_color, 255)
    return img


def _png(img: Image.Image) -> bytes:
    buf = io.BytesIO()
    img.save(buf, format="PNG")
    return buf.getvalue()


def _lum(p) -> float:
    return 0.299 * p[0] + 0.587 * p[1] + 0.114 * p[2]


def _alpha_bytes(png_bytes: bytes) -> bytes:
    return Image.open(io.BytesIO(png_bytes)).getchannel("A").tobytes()


def _decode(png_bytes: bytes) -> Image.Image:
    return Image.open(io.BytesIO(png_bytes)).convert("RGBA")


def test_phantom_bottom_row_scrubbed_alpha_untouched() -> None:
    # underlay == border so the corners contribute nothing — isolates the
    # edge strip.
    raw = _png(_card(phantom_edges=("bottom",), underlay=_BORDER))
    result = clean_original_png(raw)
    assert result.applied == (FIXUP_NAME,)
    assert _alpha_bytes(result.png_bytes) == _alpha_bytes(raw)
    px = _decode(result.png_bytes).load()
    w, h = 100, 140
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
        assert result.applied == (FIXUP_NAME,), edge
        out = _decode(result.png_bytes)
        w, h = out.size
        assert _lum(out.getpixel(probe(w, h))) > 150, edge


def test_phantom_run_up_to_inset_fully_scrubbed() -> None:
    # The whole outer strip is re-sourced from just inside it, so any run
    # up to EDGE_INSET_PX deep disappears — no per-row colour test
    # involved — and the first row past it is left exactly as it was.
    raw = _png(
        _card(phantom_edges=("bottom",), phantom_px=EDGE_INSET_PX, underlay=_BORDER)
    )
    result = clean_original_png(raw)
    assert result.applied == (FIXUP_NAME,)
    px = _decode(result.png_bytes).load()
    w, h = 100, 140
    for k in range(EDGE_INSET_PX):
        assert px[w // 2, h - 1 - k][:3] == _BORDER, k


def test_straight_edge_strip_is_shallow_on_art() -> None:
    # Borderless art: a 1px reflection is invisible, a 3px one is a band.
    # Only the outermost EDGE_INSET_PX pixels of a straight edge may change.
    img = _card(border=(90, 60, 40), underlay=(90, 60, 40), radius=12)
    px = img.load()
    w, h = img.size
    for y in range(h):
        for x in range(w):
            if px[x, y][3] == 255:
                px[x, y] = ((x * 7) % 256, (y * 5) % 256, (x + y) % 256, 255)
    result = clean_original_png(_png(img))
    before, after = _decode(_png(img)).load(), _decode(result.png_bytes).load()
    for y in range(20, h - 20):
        for k in range(EDGE_INSET_PX, CORNER_INSET_PX + 1):
            assert after[k, y] == before[k, y], (k, y)
            assert after[w - 1 - k, y] == before[w - 1 - k, y], (k, y)


def test_phantom_white_column_on_dark_card_scrubbed() -> None:
    # Fellwar Stone (sld 7062): a near-white 1px column on the left edge of
    # a dark borderless print — the SLZ defect inverted. The old dark-only
    # signature never saw it; the geometric strip replacement does not care
    # which way the defect goes.
    dark = (30, 34, 40)
    raw = _png(
        _card(
            border=dark,
            underlay=dark,
            phantom_edges=("left",),
            phantom_color=(240, 240, 240),
        )
    )
    result = clean_original_png(raw)
    assert result.applied == (FIXUP_NAME,)
    px = _decode(result.png_bytes).load()
    assert px[0, 70][:3] == dark


def test_genuine_collector_bar_keeps_its_black_edge() -> None:
    # A real ~6mm black bar is far deeper than the inset strip: mirroring
    # within it changes nothing on the edge row. (The bytes still change —
    # the bone-white underlay beneath the bottom corners now mirrors the
    # bar's black, which is exactly what the upscaler should see there.)
    raw = _png(_card(bar_px=20, underlay=_BORDER))
    result = clean_original_png(raw)
    assert _alpha_bytes(result.png_bytes) == _alpha_bytes(raw)
    px = _decode(result.png_bytes).load()
    w, h = 100, 140
    for k in range(CORNER_INSET_PX + 1):
        assert px[w // 2, h - 1 - k][:3] == (10, 10, 10), k
    assert px[w // 2, h - 30][:3] == _BORDER


def test_clean_opaque_card_is_byte_identical() -> None:
    raw = _png(_card(radius=0))
    result = clean_original_png(raw)
    assert result.applied == ()
    assert result.png_bytes is raw


def test_black_border_card_with_black_underlay_is_byte_identical() -> None:
    # Border (10,10,10), underlay (0,0,0): inside the no-op delta gate, so
    # the vast majority of real cards never get re-encoded.
    raw = _png(_card(border=(10, 10, 10), underlay=(0, 0, 0)))
    result = clean_original_png(raw)
    assert result.applied == ()
    assert result.png_bytes is raw


def test_underlay_takes_border_colour_alpha_untouched() -> None:
    raw = _png(_card(underlay=(0, 0, 0)))
    result = clean_original_png(raw)
    assert result.applied == (FIXUP_NAME,)
    assert _alpha_bytes(result.png_bytes) == _alpha_bytes(raw)
    before = _decode(raw)
    after = _decode(result.png_bytes)
    bpx, apx = before.load(), after.load()
    w, h = after.size
    for x in range(w):
        for y in range(h):
            if bpx[x, y][3] == 0:
                assert apx[x, y][:3] == _BORDER, (x, y)
            else:
                # Flat border: the mirrored strip is the same colour, so
                # every opaque pixel is unchanged.
                assert apx[x, y] == bpx[x, y], (x, y)


def test_white_corner_rim_takes_border_colour() -> None:
    # The gold-border template: the arc's partial-alpha rim pixels are
    # near-white, and a bright fully-opaque line sits one pixel inside the
    # arc. Both must come out border-coloured so the upscaler never sees a
    # white arc to smear. Alpha stays byte-identical.
    img = _card(border=_GOLD, underlay=(254, 255, 255), radius=16)
    px = img.load()
    w, h = img.size
    rim: list[tuple[int, int]] = []
    for y in range(16):
        row = [x for x in range(w) if px[x, y][3] == 255]
        x0 = row[0]
        px[x0 - 1, y] = (255, 255, 255, 120)  # anti-aliased rim
        px[x0, y] = (230, 220, 200, 255)  # bright opaque highlight line
        rim.extend([(x0 - 1, y), (x0, y)])
    raw = _png(img)
    result = clean_original_png(raw)
    assert result.applied == (FIXUP_NAME,)
    assert _alpha_bytes(result.png_bytes) == _alpha_bytes(raw)
    out = _decode(result.png_bytes).load()
    for x, y in rim:
        assert out[x, y][:3] == _GOLD, (x, y, out[x, y])
    # Deep interior untouched.
    assert out[w // 2, h // 2][:3] == _GOLD


def test_lone_white_edge_pixel_scrubbed() -> None:
    # Older gold template: a single white opaque pixel on the top and
    # bottom edge rows. It used to stretch into a white streak across the
    # bleed.
    img = _card(border=_GOLD, underlay=_GOLD)
    px = img.load()
    w, h = img.size
    px[w // 2, 0] = (255, 255, 255, 255)
    px[w // 2, h - 1] = (255, 255, 255, 255)
    result = clean_original_png(_png(img))
    assert result.applied == (FIXUP_NAME,)
    out = _decode(result.png_bytes).load()
    assert out[w // 2, 0][:3] == _GOLD
    assert out[w // 2, h - 1][:3] == _GOLD


def test_rgb_png_stays_rgb() -> None:
    # A PNG that never had alpha must not come back with one grafted on.
    img = _card(radius=0, phantom_edges=("bottom",)).convert("RGB")
    result = clean_original_png(_png(img))
    assert result.applied == (FIXUP_NAME,)
    out = Image.open(io.BytesIO(result.png_bytes))
    assert out.mode == "RGB"
    assert out.getpixel((50, 139)) == _BORDER


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
