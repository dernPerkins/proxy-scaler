"""Tests for print-sheet PDF layout: geometry, bleed, corner-flatten, matching."""

from __future__ import annotations

from pathlib import Path

import pytest
from fpdf import FPDF
from PIL import Image

from proxy_scaler.decklist import DeckEntry
from proxy_scaler.pdf_layout import (
    BLEED_MM,
    MM_PER_IN,
    PrintUnit,
    _cut_positions,
    add_bleed,
    build_pdf,
    expand_print_slots,
    flatten_corner_alpha,
    match_quantities,
    paginate,
    resolve_page_layout,
)
from proxy_scaler.pipeline import FaceResult


def _face(
    scryfall_id: str,
    face_index: int | None,
    face_name: str,
    card_name: str,
    set_code: str | None,
    collector: str | None,
    dpi: int,
    *,
    model: str = "swinir",
    face_label: str | None = None,
) -> FaceResult:
    return FaceResult(
        out_path=Path(f"/o/{scryfall_id}-{face_index}-{dpi}.png"),
        original_path=Path(f"/c/{scryfall_id}-{face_index}.png"),
        scryfall_id=scryfall_id,
        face_index=face_index,
        face_name=face_name,
        card_name=card_name,
        set_code=set_code,
        collector_number=collector,
        png_url="",
        dpi=dpi,
        model=model,
        face_label=face_label,
    )


# --- Geometry -----------------------------------------------------------


def test_page_layout_all_combos_fit() -> None:
    expected = {
        ("letter", "landscape"): (279.4, 215.9, 8.7, 17.05),
        ("letter", "portrait"): (215.9, 279.4, 9.7, 3.35),
        ("a4", "landscape"): (297.0, 210.0, 17.5, 14.1),
        ("a4", "portrait"): (210.0, 297.0, 6.75, 12.15),
    }
    for (paper, orientation), (page_w, page_h, margin_x, margin_y) in expected.items():
        layout = resolve_page_layout(orientation=orientation, paper=paper)
        assert layout.page_w_mm == pytest.approx(page_w, abs=0.01)
        assert layout.page_h_mm == pytest.approx(page_h, abs=0.01)
        assert layout.margin_x_mm == pytest.approx(margin_x, abs=0.01)
        assert layout.margin_y_mm == pytest.approx(margin_y, abs=0.01)
        assert layout.margin_x_mm > 0
        assert layout.margin_y_mm > 0


def test_fpdf2_page_size_matches_layout() -> None:
    for paper in ("letter", "a4"):
        for orientation in ("landscape", "portrait"):
            layout = resolve_page_layout(orientation=orientation, paper=paper)
            pdf = FPDF(orientation=layout.orientation, unit="mm", format=layout.paper)
            assert abs(pdf.w - layout.page_w_mm) < 0.01
            assert abs(pdf.h - layout.page_h_mm) < 0.01


def test_cut_positions() -> None:
    assert _cut_positions(2, 10, 1, 0) == [1, 10, 19]


def test_draw_cut_marks_uses_both_colors() -> None:
    from proxy_scaler.pdf_layout import _MARK_COLOR, _draw_cut_marks

    layout = resolve_page_layout(orientation="portrait", paper="letter")
    pdf = FPDF(orientation=layout.orientation, unit="mm", format=layout.paper)
    pdf.add_page()
    _draw_cut_marks(pdf, layout)  # should run both the outer-line and mark
    # blocks without error; final draw color is the green mark color (drawn
    # last), confirming that code path actually executed.
    r, g, b = (c / 255 for c in _MARK_COLOR)
    assert pdf.draw_color.r == pytest.approx(r, abs=1e-6)
    assert pdf.draw_color.g == pytest.approx(g, abs=1e-6)
    assert pdf.draw_color.b == pytest.approx(b, abs=1e-6)


# --- Corner flatten / bleed ----------------------------------------------


def _rounded_rect_rgba(w: int, h: int, radius: int) -> Image.Image:
    """Realistic rounded-rect alpha: a quarter-circle cutout at all 4
    corners, matching real Scryfall/upscaled card geometry."""
    img = Image.new("RGBA", (w, h), (10, 20, 30, 255))
    px = img.load()
    corners = [(0, 0), (w - radius, 0), (0, h - radius), (w - radius, h - radius)]
    for cx, cy in corners:
        for dy in range(radius):
            for dx in range(radius):
                # Distance from the quarter-circle's own center, mirrored
                # to whichever corner this is.
                ax = dx if cx == 0 else radius - 1 - dx
                ay = dy if cy == 0 else radius - 1 - dy
                center_dx, center_dy = radius - ax, radius - ay
                if center_dx * center_dx + center_dy * center_dy > radius * radius:
                    px[cx + dx, cy + dy] = (0, 0, 0, 0)
    return img


def test_flatten_corner_alpha() -> None:
    # A naive implementation using Python truthiness on Image.Transpose
    # (whose FLIP_LEFT_RIGHT member is IntEnum value 0, i.e. falsy) silently
    # skips the top-right corner's flip — this covers all 4 corners with
    # real quarter-circle geometry specifically to catch that regression.
    w, h, radius = 400, 400, 40
    img = _rounded_rect_rgba(w, h, radius)
    flattened = flatten_corner_alpha(img)

    probe = radius + 8
    corners = {
        "top-left": [(x, y) for y in range(probe) for x in range(probe)],
        "top-right": [(w - 1 - x, y) for y in range(probe) for x in range(probe)],
        "bottom-left": [(x, h - 1 - y) for y in range(probe) for x in range(probe)],
        "bottom-right": [
            (w - 1 - x, h - 1 - y) for y in range(probe) for x in range(probe)
        ],
    }
    for name, points in corners.items():
        bad = [p for p in points if flattened.getpixel(p)[3] != 255]
        assert not bad, f"{name} corner still has {len(bad)} transparent pixel(s)"

    # The extreme corner pixel of each corner (fully opaque now).
    assert flattened.getpixel((0, 0))[3] == 255
    assert flattened.getpixel((w - 1, 0))[3] == 255
    assert flattened.getpixel((0, h - 1))[3] == 255
    assert flattened.getpixel((w - 1, h - 1))[3] == 255
    # Interior pixels are unaffected.
    assert flattened.getpixel((w // 2, h // 2)) == (10, 20, 30, 255)


def test_add_bleed_dimensions_and_replicate() -> None:
    w, h = 40, 40
    img = Image.new("RGBA", (w, h), (0, 0, 0, 255))
    for x in range(w):
        img.putpixel((x, 0), (255, 0, 0, 255))
        img.putpixel((x, h - 1), (0, 255, 0, 255))
    for y in range(h):
        img.putpixel((0, y), (0, 0, 255, 255))
        img.putpixel((w - 1, y), (255, 255, 0, 255))

    dpi = 100
    bleed_px = round(dpi / MM_PER_IN * BLEED_MM)
    assert bleed_px == 4

    result = add_bleed(img, dpi=dpi)
    assert result.size == (w + 2 * bleed_px, h + 2 * bleed_px)
    assert result.mode == "RGB"

    assert result.getpixel((bleed_px + 5, 0)) == (255, 0, 0)  # top strip
    assert result.getpixel((bleed_px + 5, result.size[1] - 1)) == (0, 255, 0)  # bottom
    assert result.getpixel((0, bleed_px + 5)) == (0, 0, 255)  # left strip
    assert result.getpixel((result.size[0] - 1, bleed_px + 5)) == (255, 255, 0)  # right


# --- Quantity matching -----------------------------------------------------


def test_match_quantities_exact_printing() -> None:
    gallery = [_face("sol-id", None, "Sol Ring", "Sol Ring", "c21", "263", 800)]
    entries = [
        DeckEntry(quantity=3, name="Sol Ring", set_code="c21", collector_number="263")
    ]
    units, unmatched = match_quantities(entries, gallery)
    assert unmatched == []
    assert len(units) == 1
    assert units[0].quantity == 3
    assert units[0].best.scryfall_id == "sol-id"


def test_match_quantities_dfc_both_faces_get_quantity() -> None:
    name = "Dion, Bahamut's Dominant // Bahamut, Warden of Light"
    gallery = [
        _face("dfc-id", 0, "Dion, Bahamut's Dominant", name, "fin", "376", 800, face_label="front"),
        _face("dfc-id", 1, "Bahamut, Warden of Light", name, "fin", "376", 800, face_label="back"),
    ]
    entries = [DeckEntry(quantity=2, name=name, set_code="fin", collector_number="376")]
    units, unmatched = match_quantities(entries, gallery)
    assert unmatched == []
    assert len(units) == 2
    assert all(u.quantity == 2 for u in units)


def test_match_quantities_name_fallback() -> None:
    gallery = [_face("x-id", None, "Lightning Bolt", "Lightning Bolt", "lea", "161", 800)]
    entries = [DeckEntry(quantity=4, name="Lightning Bolt")]
    units, unmatched = match_quantities(entries, gallery)
    assert unmatched == []
    assert units[0].quantity == 4


def test_match_quantities_unmatched_defaults_to_one() -> None:
    gallery = [_face("y-id", None, "Counterspell", "Counterspell", "lea", "55", 800)]
    units, unmatched = match_quantities([], gallery)
    assert len(unmatched) == 1
    assert units[0].quantity == 1


def test_match_quantities_picks_highest_dpi() -> None:
    gallery = [
        _face("sol-id", None, "Sol Ring", "Sol Ring", "c21", "263", 600),
        _face("sol-id", None, "Sol Ring", "Sol Ring", "c21", "263", 1200),
        _face("sol-id", None, "Sol Ring", "Sol Ring", "c21", "263", 800),
    ]
    entries = [
        DeckEntry(quantity=1, name="Sol Ring", set_code="c21", collector_number="263")
    ]
    units, _ = match_quantities(entries, gallery)
    assert units[0].best.dpi == 1200
    assert units[0].dpi_fallback is False


def test_match_quantities_preferred_dpi_used_when_available() -> None:
    gallery = [
        _face("sol-id", None, "Sol Ring", "Sol Ring", "c21", "263", 600),
        _face("sol-id", None, "Sol Ring", "Sol Ring", "c21", "263", 800),
        _face("sol-id", None, "Sol Ring", "Sol Ring", "c21", "263", 1200),
    ]
    entries = [
        DeckEntry(quantity=1, name="Sol Ring", set_code="c21", collector_number="263")
    ]
    units, _ = match_quantities(entries, gallery, preferred_dpi=800)
    assert units[0].best.dpi == 800
    assert units[0].dpi_fallback is False


def test_match_quantities_preferred_dpi_falls_back_when_missing() -> None:
    gallery = [
        _face("sol-id", None, "Sol Ring", "Sol Ring", "c21", "263", 600),
        _face("sol-id", None, "Sol Ring", "Sol Ring", "c21", "263", 800),
    ]
    entries = [
        DeckEntry(quantity=1, name="Sol Ring", set_code="c21", collector_number="263")
    ]
    units, _ = match_quantities(entries, gallery, preferred_dpi=1200)
    assert units[0].best.dpi == 800  # highest available, since 1200 wasn't generated
    assert units[0].dpi_fallback is True


# --- Expansion / pagination ------------------------------------------------


def test_expand_print_slots_and_paginate() -> None:
    face = _face("z-id", None, "Plains", "Plains", "mh2", "482", 800)
    units = [PrintUnit(face_key="k", quantity=4, best=face)]
    slots = expand_print_slots(units)
    assert len(slots) == 4
    assert all(s is face for s in slots)

    pages = paginate(slots + [face] * 5, per_page=8)  # 9 total
    assert [len(p) for p in pages] == [8, 1]


# --- PDF assembly ------------------------------------------------------


def test_build_pdf_smoke(tmp_path) -> None:
    img = Image.new("RGBA", (100, 140), (10, 20, 30, 255))
    out_path = tmp_path / "card.png"
    img.save(out_path, format="PNG")

    face = FaceResult(
        out_path=out_path,
        original_path=out_path,
        scryfall_id="abc",
        face_index=None,
        face_name="Test Card",
        card_name="Test Card",
        set_code="tst",
        collector_number="1",
        png_url="",
        dpi=800,
    )
    layout = resolve_page_layout(orientation="landscape", paper="letter")
    pages = paginate([face], layout.cards_per_page)
    pdf_bytes = build_pdf(pages, layout=layout, show_cut_lines=True)

    assert pdf_bytes.startswith(b"%PDF")
    assert pdf_bytes.rstrip().endswith(b"%%EOF")
    assert len(pdf_bytes) > 500
