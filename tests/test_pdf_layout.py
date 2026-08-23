"""Tests for print-sheet PDF layout: geometry, bleed, corner-flatten, matching."""

from __future__ import annotations

from pathlib import Path

import pytest
from fpdf import FPDF
from PIL import Image

from proxy_scaler.decklist import DeckEntry
from proxy_scaler.dpi import target_pixels
from proxy_scaler.pdf_layout import (
    _bled_pixels,
    BLEED_MM,
    CARD_WIDTH_MM,
    MM_PER_IN,
    PAGE_SIZE_PRESETS_MM,
    PrintUnit,
    _card_trim_edges,
    add_bleed,
    build_pdf,
    FlipEdge,
    GuideVisibility,
    PageOrder,
    PrintSlot,
    ReverseFill,
    back_page_cells,
    back_pages_are_rotated,
    build_print_slots,
    fit_cover,
    mirror_page_index,
    render_back_image,
    flatten_corner_alpha,
    match_quantities,
    paginate,
    resolve_page_layout,
    unique_image_count,
)
from proxy_scaler.pdf_jobs import PdfRenderCanceled
from proxy_scaler.pipeline import FaceResult


def _a4_portrait_layout(**overrides):
    """3x3 grid on A4 portrait with default bleed/spacing — the app's own
    defaults — as a base for geometry tests."""
    kwargs = dict(
        page_w_mm=PAGE_SIZE_PRESETS_MM["a4"][0],
        page_h_mm=PAGE_SIZE_PRESETS_MM["a4"][1],
        cols=3,
        rows=3,
    )
    kwargs.update(overrides)
    return resolve_page_layout(**kwargs)


def _face(
    scryfall_id: str,
    face_index: int | None,
    face_name: str,
    card_name: str,
    set_code: str | None,
    collector: str | None,
    dpi: int,
    *,
    model: str = "ultrasharp_v2",
    face_label: str | None = None,
    total_faces: int | None = None,
    lang: str = "en",
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
        total_faces=total_faces,
        lang=lang,
    )


# --- Geometry -----------------------------------------------------------


def test_page_layout_letter_a4_defaults_fit() -> None:
    expected = {
        ("letter", "landscape"): (279.4, 215.9, 9.7, 17.95),
        ("letter", "portrait"): (215.9, 279.4, 10.45, 4.7),
        ("a4", "landscape"): (297.0, 210.0, 18.5, 15.0),
        ("a4", "portrait"): (210.0, 297.0, 7.5, 13.5),
    }
    for (paper, orientation), (page_w, page_h, margin_x, margin_y) in expected.items():
        w, h = PAGE_SIZE_PRESETS_MM[paper]
        if orientation == "landscape":
            w, h = h, w
        cols, rows = (4, 2) if orientation == "landscape" else (3, 3)
        layout = resolve_page_layout(page_w_mm=w, page_h_mm=h, cols=cols, rows=rows)
        assert layout.page_w_mm == pytest.approx(page_w, abs=0.01)
        assert layout.page_h_mm == pytest.approx(page_h, abs=0.01)
        assert layout.margin_x_mm == pytest.approx(margin_x, abs=0.01)
        assert layout.margin_y_mm == pytest.approx(margin_y, abs=0.01)
        assert layout.margin_x_mm > 0
        assert layout.margin_y_mm > 0


def test_page_layout_rejects_nonsense_but_not_overflow() -> None:
    with pytest.raises(ValueError):
        resolve_page_layout(page_w_mm=0, page_h_mm=297, cols=3, rows=3)
    with pytest.raises(ValueError):
        resolve_page_layout(page_w_mm=210, page_h_mm=297, cols=0, rows=3)
    with pytest.raises(ValueError):
        resolve_page_layout(page_w_mm=210, page_h_mm=297, cols=3, rows=3, bleed_mm=-1)

    # A grid that doesn't fit the page must NOT raise — a deliberate
    # position offset can intentionally push content near/past an edge
    # (e.g. to work around a specific printer's feed quirk); the caller
    # decides whether to warn, not resolve_page_layout itself.
    layout = resolve_page_layout(page_w_mm=50, page_h_mm=50, cols=5, rows=5)
    assert layout.grid_w_mm > layout.page_w_mm
    assert layout.grid_h_mm > layout.page_h_mm


def test_page_layout_spacing_increases_cell_stride() -> None:
    layout = _a4_portrait_layout(spacing_x_mm=3.0, spacing_y_mm=2.0)
    bled_w = CARD_WIDTH_MM + 2 * BLEED_MM
    assert layout.cell_w_mm == pytest.approx(bled_w + 3.0)
    assert layout.bled_card_w_mm == pytest.approx(bled_w)  # card's own drawn size unaffected


def test_page_layout_offset_shifts_margins() -> None:
    base = _a4_portrait_layout()
    shifted = _a4_portrait_layout(offset_x_mm=5.0, offset_y_mm=-9.0)
    assert shifted.margin_x_mm == pytest.approx(base.margin_x_mm + 5.0)
    assert shifted.margin_y_mm == pytest.approx(base.margin_y_mm - 9.0)


def test_fpdf2_page_size_matches_layout() -> None:
    for paper in ("letter", "a4"):
        for cols, rows in ((3, 3), (4, 2)):
            w, h = PAGE_SIZE_PRESETS_MM[paper]
            layout = resolve_page_layout(page_w_mm=w, page_h_mm=h, cols=cols, rows=rows)
            pdf = FPDF(orientation="portrait", unit="mm", format=(layout.page_w_mm, layout.page_h_mm))
            assert abs(pdf.w - layout.page_w_mm) < 0.01
            assert abs(pdf.h - layout.page_h_mm) < 0.01


def test_card_trim_edges() -> None:
    # 2 cards, cell=10, bleed=1, origin=0 — card 0's own edges at 1/9, card
    # 1's own edges at 11/19: 2mm apart at the interior gap, not coincident
    # at the shared cell boundary (10).
    assert _card_trim_edges(2, 10, 1, 0) == [1, 9, 11, 19]


def test_card_trim_edges_interior_gap_is_two_bleeds_wide() -> None:
    """Regression guard: adjacent cards' facing trim edges must be
    2*BLEED_MM apart (two distinct cut lines), never coincident at one
    shared line down the middle of the gap."""
    layout = _a4_portrait_layout()
    xs = _card_trim_edges(layout.cols, layout.cell_w_mm, layout.bleed_mm, layout.margin_x_mm)
    # xs holds 2 edges per card, e.g. [c0_left, c0_right, c1_left, c1_right, ...]
    interior_gap = xs[2] - xs[1]
    assert interior_gap == pytest.approx(2 * BLEED_MM)


def test_draw_cut_marks_uses_both_colors() -> None:
    from proxy_scaler.pdf_layout import _MARK_COLOR, _draw_cut_marks

    layout = _a4_portrait_layout()
    pdf = FPDF(orientation="portrait", unit="mm", format=(layout.page_w_mm, layout.page_h_mm))
    pdf.add_page()
    _draw_cut_marks(pdf, layout)  # should run both the outer-line and mark
    # blocks without error; final draw color is the green mark color (drawn
    # last), confirming that code path actually executed.
    r, g, b = (c / 255 for c in _MARK_COLOR)
    assert pdf.draw_color.r == pytest.approx(r, abs=1e-6)
    assert pdf.draw_color.g == pytest.approx(g, abs=1e-6)
    assert pdf.draw_color.b == pytest.approx(b, abs=1e-6)


def test_draw_cut_marks_ink_stays_off_the_cards() -> None:
    """Every guide stroke must sit fully outside every card's trim box —
    flush against it is fine (that's the design: card edge, then guide),
    but any ink inside the box would survive a clean cut as a colored
    hairline on the card's border."""
    from proxy_scaler.pdf_layout import _draw_cut_marks

    class _RecordingPdf:
        def __init__(self) -> None:
            self.line_width = 0.0
            self.strokes: list[tuple[float, float, float, float]] = []

        def set_line_width(self, w: float) -> None:
            self.line_width = w

        def set_draw_color(self, *_: int) -> None:
            pass

        def line(self, x1: float, y1: float, x2: float, y2: float) -> None:
            half = self.line_width / 2
            self.strokes.append(
                (min(x1, x2) - half, min(y1, y2) - half, max(x1, x2) + half, max(y1, y2) + half)
            )

    layout = _a4_portrait_layout()
    pdf = _RecordingPdf()
    _draw_cut_marks(pdf, layout)  # type: ignore[arg-type]
    assert pdf.strokes

    xs = _card_trim_edges(layout.cols, layout.cell_w_mm, layout.bleed_mm, layout.margin_x_mm)
    ys = _card_trim_edges(layout.rows, layout.cell_h_mm, layout.bleed_mm, layout.margin_y_mm)
    eps = 1e-9
    for col in range(layout.cols):
        for row in range(layout.rows):
            bx0, bx1 = xs[2 * col], xs[2 * col + 1]
            by0, by1 = ys[2 * row], ys[2 * row + 1]
            for sx0, sy0, sx1, sy1 in pdf.strokes:
                overlap_x = min(sx1, bx1) - max(sx0, bx0)
                overlap_y = min(sy1, by1) - max(sy0, by0)
                assert not (overlap_x > eps and overlap_y > eps), (
                    f"stroke ({sx0:.4f}, {sy0:.4f})-({sx1:.4f}, {sy1:.4f}) "
                    f"overlaps card ({col},{row}) trim box "
                    f"({bx0:.4f}, {by0:.4f})-({bx1:.4f}, {by1:.4f})"
                )


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


def test_flatten_corner_fill_ignores_dark_halo_on_arc() -> None:
    """Regression guard for the black-smear-in-bleed bug on light cards:
    upscaling smears the black RGB under the transparent corner into the
    first opaque pixel or two along the arc, so the fill colour must be
    sampled past that halo, never from the boundary pixel itself —
    add_bleed() magnifies whatever lands on row 0 / column 0 ~50x into
    the bleed border."""
    w = h = 400
    radius = 40
    light = (230, 225, 210)
    img = Image.new("RGBA", (w, h), (*light, 255))
    px = img.load()
    # Transparent quarter-circle cutout at the top-left…
    for y in range(radius):
        for x in range(radius):
            center_dx, center_dy = radius - x, radius - y
            if center_dx * center_dx + center_dy * center_dy > radius * radius:
                px[x, y] = (0, 0, 0, 0)
    # …with a 2px near-black halo on the first opaque pixels along the
    # arc — in both directions, the way an upscaled light-bordered card
    # actually arrives. The column-top pixels near (0, radius) sit on
    # rows with no transparent run at all, so only the vertical scrub
    # ever reaches them.
    for y in range(radius):
        x0 = next(x for x in range(w) if px[x, y][3] == 255)
        for x in (x0, x0 + 1):
            px[x, y] = (25, 25, 25, 255)
    for x in range(radius):
        y0 = next(y for y in range(h) if px[x, y][3] == 255)
        for y in (y0, y0 + 1):
            px[x, y] = (25, 25, 25, 255)

    # A genuine art pixel sitting inside the scrub band, right past the
    # halo — clearly not near-black, so the scrub must leave it alone
    # (only pixels with the black-contamination signature get rewritten).
    art_y = 5
    art_x = next(x for x in range(w) if px[x, art_y][3] == 255) + 2
    px[art_x, art_y] = (255, 140, 0, 255)

    flattened = flatten_corner_alpha(img)

    assert flattened.getpixel((art_x, art_y)) == (255, 140, 0, 255), (
        "scrub overwrote genuine art detail next to the arc"
    )

    def lum(p):
        return 0.299 * p[0] + 0.587 * p[1] + 0.114 * p[2]

    # The whole corner region — filled arc, the arc line itself, and both
    # spots where the rounding starts — must be opaque and light: no fill
    # that took the halo colour, and no surviving halo pixels (they're what
    # add_bleed stretches into black streaks at the arc's endpoints).
    for y in range(radius + 4):
        for x in range(radius + 4):
            p = flattened.getpixel((x, y))
            assert p[3] == 255, f"({x},{y}) still transparent"
            assert lum(p) > 150, f"({x},{y}) is halo-dark: {p[:3]}"

    # And the bleed built from it stays light at the corner too.
    bled = add_bleed(img, dpi=300)
    assert lum(bled.getpixel((2, 2))) > 150


def test_build_pdf_flattens_corners_before_resizing_for_export_dpi(tmp_path) -> None:
    """Regression guard for the corner-smearing bug: resizing an RGBA image
    while its rounded corners are still transparent lets the resample
    filter (LANCZOS) blend the transparent region's RGB into the opaque
    body right at the boundary — a visible smear baked in before the
    corner ever gets flattened to opaque. build_pdf() must flatten before
    resizing for a different export_dpi, not after. Verified by call
    order, not pixel values — the actual ringing artifact is subtle and
    depends on filter internals, but the ordering is the entire fix."""
    import proxy_scaler.pdf_layout as pdf_layout_module

    img = _rounded_rect_rgba(100, 100, 15)
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
        dpi=800,  # differs from export_dpi below, forcing the resize path
    )
    layout = _a4_portrait_layout(cols=1, rows=1)
    pages = paginate([_slot(face)], layout.cards_per_page)

    call_order: list[str] = []
    real_flatten = pdf_layout_module.flatten_corner_alpha
    real_resize = pdf_layout_module._resize_to_dpi

    def spy_flatten(*args, **kwargs):
        call_order.append("flatten")
        return real_flatten(*args, **kwargs)

    def spy_resize(*args, **kwargs):
        call_order.append("resize")
        return real_resize(*args, **kwargs)

    pdf_layout_module.flatten_corner_alpha = spy_flatten
    pdf_layout_module._resize_to_dpi = spy_resize
    try:
        build_pdf(pages, layout=layout, export_dpi=1200, guides=_no_guides())
    finally:
        pdf_layout_module.flatten_corner_alpha = real_flatten
        pdf_layout_module._resize_to_dpi = real_resize

    # flatten (explicit, pre-resize) -> resize -> flatten (add_bleed()'s own
    # internal call, now a safe no-op since corners are already opaque).
    # The critical property is that resize never happens before the FIRST
    # flatten.
    assert call_order[:2] == ["flatten", "resize"]


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
    units, missing, _missing_at_dpi = match_quantities(entries, gallery)
    assert missing == []
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
    units, missing, _missing_at_dpi = match_quantities(entries, gallery)
    assert missing == []
    assert len(units) == 2
    assert all(u.quantity == 2 for u in units)


def test_match_quantities_name_fallback() -> None:
    gallery = [_face("x-id", None, "Lightning Bolt", "Lightning Bolt", "lea", "161", 800)]
    entries = [DeckEntry(quantity=4, name="Lightning Bolt")]
    units, missing, _missing_at_dpi = match_quantities(entries, gallery)
    assert missing == []
    assert units[0].quantity == 4


def test_match_quantities_exact_printing_entry_not_double_counted_by_name_fallback() -> None:
    """A decklist with both an exact printing of a card AND a name-only
    line for the same card name (resolving to a *different* printing, e.g.
    Scryfall's default pick) must give each printing quantity 1, not let
    the exact-printing entry's quantity leak into the other printing's
    fuzzy name match — this is exactly the app's own default decklist text
    (Sol Ring (c21) 263 + a bare "Sol Ring" line)."""
    gallery = [
        _face("sol-c21-id", None, "Sol Ring", "Sol Ring", "c21", "263", 800),
        _face("sol-msc-id", None, "Sol Ring", "Sol Ring", "msc", "211", 800),
    ]
    entries = [
        DeckEntry(quantity=1, name="Sol Ring", set_code="c21", collector_number="263"),
        DeckEntry(quantity=1, name="Sol Ring"),
    ]
    units, missing, _missing_at_dpi = match_quantities(entries, gallery)
    assert missing == []
    assert len(units) == 2
    assert all(u.quantity == 1 for u in units)


def test_match_quantities_orphan_gallery_image_silently_excluded() -> None:
    """A gallery image with no matching current decklist entry (e.g. a card
    removed from the decklist after generation) is left out of the print
    run entirely — not printed, not reported."""
    gallery = [_face("y-id", None, "Counterspell", "Counterspell", "lea", "55", 800)]
    units, missing, _missing_at_dpi = match_quantities([], gallery)
    assert units == []
    assert missing == []


def test_match_quantities_entry_with_no_image_is_missing() -> None:
    entries = [DeckEntry(quantity=1, name="Sol Ring", set_code="c21", collector_number="263")]
    units, missing, _missing_at_dpi = match_quantities(entries, [])
    assert units == []
    assert len(missing) == 1
    assert "Sol Ring" in missing[0]


def test_match_quantities_use_originals_picks_only_download_variants() -> None:
    """With use_originals, only the (300, "original") download rows are
    eligible — an upscaled variant for the same face is invisible, never a
    substitute."""
    from proxy_scaler.dpi import ORIGINAL_DPI, ORIGINAL_MODEL

    gallery = [
        _face("sol-id", None, "Sol Ring", "Sol Ring", "c21", "263", 1200),
        _face(
            "sol-id", None, "Sol Ring", "Sol Ring", "c21", "263",
            ORIGINAL_DPI, model=ORIGINAL_MODEL,
        ),
    ]
    entries = [
        DeckEntry(quantity=2, name="Sol Ring", set_code="c21", collector_number="263")
    ]
    units, missing, missing_at_dpi = match_quantities(
        entries, gallery, use_originals=True
    )
    assert missing == []
    assert missing_at_dpi == []
    [unit] = units
    assert unit.quantity == 2
    assert unit.best.model == ORIGINAL_MODEL
    assert unit.best.dpi == ORIGINAL_DPI


def test_match_quantities_use_originals_reports_face_without_download() -> None:
    """A face with only upscaled variants has no original to print — it
    reads as plain `missing`, pointing at Download images, not silently
    substituted with an upscale."""
    gallery = [_face("sol-id", None, "Sol Ring", "Sol Ring", "c21", "263", 1200)]
    entries = [
        DeckEntry(quantity=1, name="Sol Ring", set_code="c21", collector_number="263")
    ]
    units, missing, _missing_at_dpi = match_quantities(
        entries, gallery, use_originals=True
    )
    assert units == []
    assert len(missing) == 1
    assert "Sol Ring" in missing[0]


def test_match_quantities_download_only_face_is_missing_for_upscale_run() -> None:
    """The reverse direction: with use_originals off (the default), a face
    that only has a download must read as missing — originals never mix
    into an upscale print run via the "highest available" pick."""
    from proxy_scaler.dpi import ORIGINAL_DPI, ORIGINAL_MODEL

    gallery = [
        _face(
            "sol-id", None, "Sol Ring", "Sol Ring", "c21", "263",
            ORIGINAL_DPI, model=ORIGINAL_MODEL,
        )
    ]
    entries = [
        DeckEntry(quantity=1, name="Sol Ring", set_code="c21", collector_number="263")
    ]
    units, missing, _missing_at_dpi = match_quantities(entries, gallery)
    assert units == []
    assert len(missing) == 1


def test_match_quantities_dfc_partial_generation_reported_when_total_faces_known() -> None:
    """Only the front face was ever generated — its own total_faces=2 (set
    at generation time from Scryfall's card data, see db migration 003)
    lets match_quantities know the back face was never attempted, so the
    entry is reported even though its front face prints."""
    name = "Delver of Secrets // Insectile Aberration"
    gallery = [
        _face(
            "dfc-id", 0, "Delver of Secrets", name, "isd", "51", 800,
            face_label="front", total_faces=2,
        )
    ]
    entries = [DeckEntry(quantity=1, name=name, set_code="isd", collector_number="51")]
    units, missing, _missing_at_dpi = match_quantities(entries, gallery)
    assert len(units) == 1
    assert units[0].quantity == 1
    assert len(missing) == 1
    assert "1 of 2 faces generated" in missing[0]


def test_match_quantities_dfc_partial_generation_ignored_when_total_faces_unknown() -> None:
    """Same as above, but the matched face's total_faces is None (a row
    predating migration 003) — degrades to the lenient any-match-counts
    behavior rather than guessing."""
    name = "Delver of Secrets // Insectile Aberration"
    gallery = [_face("dfc-id", 0, "Delver of Secrets", name, "isd", "51", 800, face_label="front")]
    entries = [DeckEntry(quantity=1, name=name, set_code="isd", collector_number="51")]
    units, missing, _missing_at_dpi = match_quantities(entries, gallery)
    assert len(units) == 1
    assert missing == []


def test_match_quantities_picks_highest_dpi() -> None:
    gallery = [
        _face("sol-id", None, "Sol Ring", "Sol Ring", "c21", "263", 600),
        _face("sol-id", None, "Sol Ring", "Sol Ring", "c21", "263", 1200),
        _face("sol-id", None, "Sol Ring", "Sol Ring", "c21", "263", 800),
    ]
    entries = [
        DeckEntry(quantity=1, name="Sol Ring", set_code="c21", collector_number="263")
    ]
    units, _, _missing = match_quantities(entries, gallery)
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
    units, _, _missing = match_quantities(entries, gallery, preferred_dpi=800)
    assert units[0].best.dpi == 800
    assert units[0].dpi_fallback is False


def test_match_quantities_preferred_model_used_when_available() -> None:
    gallery = [
        _face("sol-id", None, "Sol Ring", "Sol Ring", "c21", "263", 800, model="illustrationjanai"),
        _face(
            "sol-id",
            None,
            "Sol Ring",
            "Sol Ring",
            "c21",
            "263",
            800,
            model="ultrasharp_v2",
        ),
    ]
    entries = [
        DeckEntry(quantity=1, name="Sol Ring", set_code="c21", collector_number="263")
    ]
    units, _, _missing = match_quantities(
        entries, gallery, preferred_dpi=800, preferred_model="ultrasharp_v2"
    )
    assert units[0].best.model == "ultrasharp_v2"

    units_janai, _, _ = match_quantities(
        entries, gallery, preferred_dpi=800, preferred_model="illustrationjanai"
    )
    assert units_janai[0].best.model == "illustrationjanai"


def test_match_quantities_no_duplicate_units_across_models() -> None:
    """A face generated under two models must still yield exactly one
    PrintUnit — previously group_by_face split by model, causing double
    physical copies of the same face in the PDF."""
    gallery = [
        _face("sol-id", None, "Sol Ring", "Sol Ring", "c21", "263", 800, model="illustrationjanai"),
        _face(
            "sol-id",
            None,
            "Sol Ring",
            "Sol Ring",
            "c21",
            "263",
            800,
            model="ultrasharp_v2",
        ),
    ]
    entries = [
        DeckEntry(quantity=3, name="Sol Ring", set_code="c21", collector_number="263")
    ]
    units, missing, _missing_at_dpi = match_quantities(entries, gallery)
    assert missing == []
    assert len(units) == 1
    assert units[0].quantity == 3


def test_match_quantities_unwanted_group_not_reported_missing_at_dpi() -> None:
    """A gallery group no current entry references (card removed, or
    repointed to a different printing after this one generated) is silently
    excluded even when it also lacks the preferred DPI — the deck stopped
    wanting it, so a 'missing at DPI' report for it is a phantom error.
    Regression: the DPI filter used to run before decklist matching, so
    swapping printings left stale groups complaining about resolutions
    nobody asked to print."""
    gallery = [
        # The printing currently picked, present at the preferred DPI.
        _face("sol-msc", None, "Sol Ring", "Sol Ring", "msc", "211", 1200),
        # A leftover from a previously-picked printing, 800 only.
        _face("sol-sld", None, "Sol Ring", "Sol Ring", "sld", "2807", 800),
    ]
    entries = [
        DeckEntry(quantity=4, name="Sol Ring", set_code="msc", collector_number="211")
    ]
    units, missing, missing_at_dpi = match_quantities(entries, gallery, preferred_dpi=1200)

    assert [u.best.scryfall_id for u in units] == ["sol-msc"]
    assert missing == []
    assert missing_at_dpi == []


def test_match_quantities_matched_entry_lacking_dpi_not_double_reported() -> None:
    """An entry whose image exists but not at the preferred DPI belongs in
    missing_at_dpi only — 'no generated image yet' would be a lie."""
    gallery = [
        _face("sol-id", None, "Sol Ring", "Sol Ring", "c21", "263", 800),
    ]
    entries = [
        DeckEntry(quantity=1, name="Sol Ring", set_code="c21", collector_number="263")
    ]
    units, missing, missing_at_dpi = match_quantities(entries, gallery, preferred_dpi=1200)

    assert units == []
    assert missing == []
    assert missing_at_dpi == ["Sol Ring [C21 263]"]


def test_match_quantities_preferred_dpi_excludes_and_reports_when_missing() -> None:
    """A preferred DPI is a hard filter, not a preference: a card with no
    image at it must be left out of the print run and reported, never
    silently substituted at another resolution (which would print visibly
    inconsistent cards and hide that something never generated)."""
    gallery = [
        _face("sol-id", None, "Sol Ring", "Sol Ring", "c21", "263", 600),
        _face("sol-id", None, "Sol Ring", "Sol Ring", "c21", "263", 800),
    ]
    entries = [
        DeckEntry(quantity=1, name="Sol Ring", set_code="c21", collector_number="263")
    ]
    units, _, missing = match_quantities(entries, gallery, preferred_dpi=1200)

    assert units == []
    assert missing == ["Sol Ring [C21 263]"]


def test_match_quantities_preferred_dpi_picks_most_recent_at_that_dpi() -> None:
    """Within the requested DPI and absent a model preference, the most
    recently produced image wins — regenerating a card is how a user says
    "use this one now"."""
    older = _face("sol-id", None, "Sol Ring", "Sol Ring", "c21", "263", 800, model="ultrasharp_v2")
    newer = _face(
        "sol-id", None, "Sol Ring", "Sol Ring", "c21", "263", 800, model="ultrasharp_v2"
    )
    older.created_at = "2026-01-01T00:00:00+00:00"
    newer.created_at = "2026-08-01T00:00:00+00:00"
    entries = [
        DeckEntry(quantity=1, name="Sol Ring", set_code="c21", collector_number="263")
    ]

    units, _, missing = match_quantities([*entries], [older, newer], preferred_dpi=800)
    assert missing == []
    assert units[0].best.model == "ultrasharp_v2"

    # Order of the gallery list must not decide it.
    units, _, _missing = match_quantities([*entries], [newer, older], preferred_dpi=800)
    assert units[0].best.model == "ultrasharp_v2"


def test_match_quantities_untimestamped_rows_lose_to_timestamped() -> None:
    """Rows predating the created_at column (db migration 002) carry None.
    They must sort as oldest rather than winning ties by accident, so a
    freshly regenerated image beats a legacy one."""
    legacy = _face("sol-id", None, "Sol Ring", "Sol Ring", "c21", "263", 800, model="ultrasharp_v2")
    fresh = _face(
        "sol-id", None, "Sol Ring", "Sol Ring", "c21", "263", 800, model="ultrasharp_v2"
    )
    fresh.created_at = "2026-08-01T00:00:00+00:00"
    entries = [
        DeckEntry(quantity=1, name="Sol Ring", set_code="c21", collector_number="263")
    ]
    units, _, _missing = match_quantities(entries, [legacy, fresh], preferred_dpi=800)
    assert units[0].best.model == "ultrasharp_v2"


def test_match_quantities_preferred_model_beats_recency_at_that_dpi() -> None:
    """An explicit model choice outranks recency — otherwise picking a model
    would appear to do nothing whenever a different one was generated later."""
    chosen = _face("sol-id", None, "Sol Ring", "Sol Ring", "c21", "263", 800, model="ultrasharp_v2")
    newer_other = _face(
        "sol-id", None, "Sol Ring", "Sol Ring", "c21", "263", 800, model="ultrasharp_v2"
    )
    chosen.created_at = "2026-01-01T00:00:00+00:00"
    newer_other.created_at = "2026-08-01T00:00:00+00:00"
    entries = [
        DeckEntry(quantity=1, name="Sol Ring", set_code="c21", collector_number="263")
    ]
    units, _, _missing = match_quantities(
        entries, [chosen, newer_other], preferred_dpi=800, preferred_model="ultrasharp_v2"
    )
    assert units[0].best.model == "ultrasharp_v2"


# --- Expansion / pagination ------------------------------------------------


def test_build_print_slots_and_paginate() -> None:
    face = _face("z-id", None, "Plains", "Plains", "mh2", "482", 800)
    units = [PrintUnit(face_key="k", quantity=4, best=face)]
    slots = build_print_slots(units, pair_back_faces=False)
    assert len(slots) == 4
    assert all(s.front is face for s in slots)
    # Nothing pairs a single-faced card, so every Reverse falls to the
    # Back Image.
    assert all(s.reverse is None for s in slots)

    pages = paginate(slots + [_slot(face)] * 5, per_page=8)  # 9 total
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
    w, h = PAGE_SIZE_PRESETS_MM["letter"]
    layout = resolve_page_layout(page_w_mm=h, page_h_mm=w, cols=4, rows=2)
    pages = paginate([_slot(face)], layout.cards_per_page)
    pdf_bytes = build_pdf(pages, layout=layout, export_dpi=800)

    assert pdf_bytes.startswith(b"%PDF")
    assert pdf_bytes.rstrip().endswith(b"%%EOF")
    assert len(pdf_bytes) > 500


def test_build_pdf_export_dpi_resizes_source(tmp_path) -> None:
    """PDF export DPI is independent of the source image's own dpi — a
    higher export_dpi should embed visibly more pixel data (and therefore
    produce a larger file) than a lower one, from the same source image."""
    img = Image.new("RGBA", (200, 280), (10, 20, 30, 255))
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
    layout = _a4_portrait_layout(cols=1, rows=1)
    pages = paginate([_slot(face)], layout.cards_per_page)

    small = build_pdf(pages, layout=layout, export_dpi=800, guides=_no_guides())
    large = build_pdf(pages, layout=layout, export_dpi=1200, guides=_no_guides())
    assert len(large) > len(small)


def test_flatten_corner_alpha_only_touches_transparent_pixels() -> None:
    """flatten_corner_alpha must fill the transparent rounded-corner arc and
    nothing else. A previous version stretched one fixed column across the
    full width of every row in the r x r corner square, which overwrote the
    opaque card art sharing that square and rendered it as horizontal bands
    -- visible in the PDF as smears running out of every card corner."""
    # Radius must stay inside the probe window (probe_frac=0.12 of the
    # short edge = 24px here); real cards are ~5% of their width, so this
    # mirrors them. A radius wider than the probe leaves the patch with no
    # opaque pixel to sample at all.
    img = _rounded_rect_rgba(200, 280, 15)
    before = list(img.convert("RGB").getdata())
    before_alpha = list(img.getchannel("A").getdata())

    flattened = flatten_corner_alpha(img)
    after = list(flattened.convert("RGB").getdata())

    assert flattened.size == img.size
    overwritten = [
        i
        for i, (was, now, alpha) in enumerate(zip(before, after, before_alpha))
        if alpha >= 250 and was != now
    ]
    assert not overwritten, f"{len(overwritten)} opaque pixels were overwritten"

    # ...and the corners really did become opaque.
    alpha = flattened.getchannel("A")
    w, h = flattened.size
    for xy in ((0, 0), (w - 1, 0), (0, h - 1), (w - 1, h - 1)):
        assert alpha.getpixel(xy) == 255, f"corner {xy} left transparent"


# --- Render progress -------------------------------------------------------


def _no_guides() -> GuideVisibility:
    """Every guide off, on both page kinds — the old `show_cut_lines=False`."""
    return GuideVisibility(
        hide_card_guides_front=True,
        hide_page_guides_front=True,
        hide_card_guides_back=True,
        hide_page_guides_back=True,
    )


def _slot(face: FaceResult, reverse: FaceResult | None = None) -> PrintSlot:
    return PrintSlot(front=face, reverse=reverse)


def _pdf_source_face(tmp_path: Path, name: str, dpi: int = 800) -> FaceResult:
    """A FaceResult backed by a real (tiny) PNG on disk, so build_pdf can
    actually decode/resize/encode it."""
    path = tmp_path / f"{name}.png"
    _rounded_rect_rgba(60, 84, 8).save(path, format="PNG")
    return FaceResult(
        out_path=path,
        original_path=path,
        scryfall_id=name,
        face_index=None,
        face_name=name,
        card_name=name,
        set_code="tst",
        collector_number="1",
        png_url="",
        dpi=dpi,
    )


def test_build_pdf_reports_progress_once_per_unique_image(tmp_path: Path) -> None:
    """Progress is measured in unique source images, not print slots: the
    per-image decode/resize/bleed/encode is cached per out_path, so a card
    printed several times costs one unit of real work and the rest are
    near-free placements. A slot-based count would stall the bar on
    duplicates and overstate the remaining work."""
    a = _pdf_source_face(tmp_path, "a")
    b = _pdf_source_face(tmp_path, "b")
    # 5 slots, 2 unique images (a appears three times).
    pages = [[_slot(a), _slot(b), _slot(a)], [_slot(a)]]
    layout = _a4_portrait_layout(cols=3, rows=1)

    calls: list[tuple[int, int]] = []
    build_pdf(pages, layout=layout, export_dpi=800, on_progress=lambda c, t: calls.append((c, t)))

    assert calls == [(1, 2), (2, 2)]
    assert unique_image_count(pages) == 2


def test_build_pdf_progress_is_optional(tmp_path: Path) -> None:
    """Omitting on_progress must keep the CLI/test call shape working."""
    pages = [[_slot(_pdf_source_face(tmp_path, "solo"))]]
    pdf_bytes = build_pdf(pages, layout=_a4_portrait_layout(cols=1, rows=1), export_dpi=800)
    assert pdf_bytes.startswith(b"%PDF")


def test_build_pdf_progress_callback_can_abort_the_build(tmp_path: Path) -> None:
    """Cancellation rides out through the callback rather than a flag
    build_pdf has to poll — so the render function stays unaware that jobs
    exist. Nothing catches it here: the exception reaches the caller and no
    partial PDF is produced."""
    pages = [[_slot(_pdf_source_face(tmp_path, "a")), _slot(_pdf_source_face(tmp_path, "b"))]]

    def stop_after_first(completed: int, _total: int) -> None:
        if completed >= 1:
            raise PdfRenderCanceled()

    with pytest.raises(PdfRenderCanceled):
        build_pdf(
            pages,
            layout=_a4_portrait_layout(cols=2, rows=1),
            export_dpi=800,
            on_progress=stop_after_first,
        )


def test_match_quantities_language_completes_printing_identity() -> None:
    """An Italian entry must not print the English image of the same
    set/collector — and lang-less entries (pre-language decks) still match
    English rows, since absent lang normalizes to en on both sides."""
    gallery = [
        _face("sol-en", None, "Sol Ring", "Sol Ring", "c21", "263", 800, lang="en"),
        _face("sol-it", None, "Sol Ring", "Sol Ring", "c21", "263", 800, lang="it"),
    ]
    entries = [
        DeckEntry(quantity=2, name="Sol Ring", set_code="c21", collector_number="263", lang="it"),
    ]
    units, missing, _ = match_quantities(entries, gallery)
    assert missing == []
    assert len(units) == 1
    assert units[0].best.scryfall_id == "sol-it"
    assert units[0].quantity == 2

    # Lang-less entry matches only the English row.
    legacy = [DeckEntry(quantity=1, name="Sol Ring", set_code="c21", collector_number="263")]
    units, missing, _ = match_quantities(legacy, gallery)
    assert missing == []
    assert [u.best.scryfall_id for u in units] == ["sol-en"]


def test_match_quantities_missing_language_is_reported() -> None:
    gallery = [_face("sol-en", None, "Sol Ring", "Sol Ring", "c21", "263", 800, lang="en")]
    entries = [
        DeckEntry(quantity=1, name="Sol Ring", set_code="c21", collector_number="263", lang="ja"),
    ]
    units, missing, _ = match_quantities(entries, gallery)
    assert units == []
    assert len(missing) == 1


# --- Back printing: pairing ------------------------------------------------


def _dfc_pair(sid: str = "dfc") -> tuple[FaceResult, FaceResult]:
    front = _face(sid, 0, "Front", "Front // Back", "mom", "1", 800)
    back = _face(sid, 1, "Back", "Front // Back", "mom", "1", 800)
    return front, back


def test_back_faces_pair_into_one_card_when_enabled() -> None:
    """The whole point of the pairing mode: a double-faced card stops being
    two cards on the sheet and becomes one card with two sides. The slot
    count halves for that card, which is why this setting lives next to the
    page count rather than next to the Back Image."""
    front, back = _dfc_pair()
    units = [
        PrintUnit(face_key="f", quantity=2, best=front),
        PrintUnit(face_key="b", quantity=2, best=back),
    ]
    slots = build_print_slots(units, pair_back_faces=True)
    assert len(slots) == 2
    assert all(s.front is front and s.reverse is back for s in slots)


def test_back_faces_stay_separate_cards_when_disabled() -> None:
    """Pairing off is exactly the historical behaviour: each face is its own
    printed card, and each of their Reverses falls to the Back Image."""
    front, back = _dfc_pair()
    units = [
        PrintUnit(face_key="f", quantity=2, best=front),
        PrintUnit(face_key="b", quantity=2, best=back),
    ]
    slots = build_print_slots(units, pair_back_faces=False)
    assert len(slots) == 4
    assert all(s.reverse is None for s in slots)


def test_pairing_never_crosses_two_different_printings() -> None:
    """scryfall_id identifies a printing including its language, so two
    different double-faced cards can never borrow each other's backs."""
    a_front, a_back = _dfc_pair("dfc-a")
    b_front, b_back = _dfc_pair("dfc-b")
    units = [
        PrintUnit(face_key="1", quantity=1, best=a_front),
        PrintUnit(face_key="2", quantity=1, best=b_back),
        PrintUnit(face_key="3", quantity=1, best=b_front),
        PrintUnit(face_key="4", quantity=1, best=a_back),
    ]
    slots = build_print_slots(units, pair_back_faces=True)
    assert {(s.front.scryfall_id, s.reverse.scryfall_id) for s in slots} == {
        ("dfc-a", "dfc-a"),
        ("dfc-b", "dfc-b"),
    }


def test_a_card_with_three_faces_never_pairs() -> None:
    """A guard, not a feature — Scryfall doesn't produce three printable
    faces. If one ever appeared, guessing which is "the back" would be
    wrong more often than useful, so every face falls back to being its own
    card: today's behaviour, which is safe rather than broken."""
    units = [
        PrintUnit(face_key=str(i), quantity=1, best=_face("tri", i, f"F{i}", "Tri", "x", "1", 800))
        for i in range(3)
    ]
    slots = build_print_slots(units, pair_back_faces=True)
    assert len(slots) == 3
    assert all(s.reverse is None for s in slots)


def test_a_front_whose_back_never_generated_does_not_pair() -> None:
    """A half-generated double-faced card takes the Back Image rather than
    a half-formed pairing. match_quantities separately reports it in
    `missing`, which is where the user finds out."""
    front, _back = _dfc_pair()
    slots = build_print_slots(
        [PrintUnit(face_key="f", quantity=1, best=front)], pair_back_faces=True
    )
    assert [s.reverse for s in slots] == [None]


# --- Back printing: mirroring ----------------------------------------------


def test_long_edge_flip_mirrors_columns_on_a_portrait_sheet() -> None:
    layout = _a4_portrait_layout(cols=3, rows=3)
    got = [mirror_page_index(i, layout=layout, flip_edge=FlipEdge.LONG) for i in range(9)]
    assert got == [2, 1, 0, 5, 4, 3, 8, 7, 6]


def test_short_edge_flip_mirrors_rows_on_a_portrait_sheet() -> None:
    layout = _a4_portrait_layout(cols=3, rows=3)
    got = [mirror_page_index(i, layout=layout, flip_edge=FlipEdge.SHORT) for i in range(9)]
    assert got == [6, 7, 8, 3, 4, 5, 0, 1, 2]


def test_flip_axis_follows_the_sheet_not_the_setting_name() -> None:
    """"Long edge" names a physical edge of the paper, so which axis it
    mirrors depends on orientation: a portrait sheet's long edges are its
    sides (columns mirror), a landscape sheet's are top and bottom (rows
    mirror). Treating the setting as "always mirror columns" puts every
    back on the wrong card for landscape pages."""
    landscape = resolve_page_layout(page_w_mm=297.0, page_h_mm=210.0, cols=3, rows=3)
    assert landscape.orientation == "landscape"
    got = [mirror_page_index(i, layout=landscape, flip_edge=FlipEdge.LONG) for i in range(9)]
    assert got == [6, 7, 8, 3, 4, 5, 0, 1, 2]


def test_partial_last_page_mirrors_over_the_full_grid() -> None:
    """The classic duplex bug this guards: four cards on a nine-slot page
    must mirror into the positions their COMPLETE grid gives them, leaving
    five cells empty. Mirroring over only the four filled cells lines every
    back up against the wrong front."""
    layout = _a4_portrait_layout(cols=3, rows=3)
    faces = [_face(f"s{i}", None, f"c{i}", f"c{i}", "x", "1", 800) for i in range(4)]
    cells = back_page_cells(
        [PrintSlot(front=f) for f in faces], layout=layout, flip_edge=FlipEdge.LONG
    )
    assert len(cells) == 9
    names = [None if c is None else c.front.card_name for c in cells]
    assert names == ["c2", "c1", "c0", None, None, "c3", None, None, None]


# --- Back printing: assembly ----------------------------------------------


def _back_image(tmp_path: Path, size: tuple[int, int] = (400, 400)) -> Path:
    """A back image with a mark in one corner.

    Asymmetric on purpose: a solid colour is identical after a 180°
    rotation, so a flat fixture silently passes every rotation test
    whether the code rotates or not.
    """
    path = tmp_path / "back.png"
    img = Image.new("RGB", size, (200, 30, 30))
    corner = Image.new("RGB", (size[0] // 4, size[1] // 4), (255, 255, 0))
    img.paste(corner, (0, 0))
    img.save(path, format="PNG")
    return path


def _page_count(pdf_bytes: bytes) -> int:
    """Real page objects, excluding the /Pages tree node."""
    return pdf_bytes.count(b"/Type /Page\n") + pdf_bytes.count(b"/Type /Page/")


def test_duplex_back_printing_doubles_the_page_count(tmp_path: Path) -> None:
    pages = [[_slot(_pdf_source_face(tmp_path, "a")), _slot(_pdf_source_face(tmp_path, "b"))]]
    layout = _a4_portrait_layout(cols=2, rows=1)
    single = build_pdf(pages, layout=layout, export_dpi=600)
    duplex = build_pdf(
        pages,
        layout=layout,
        export_dpi=600,
        back_printing=True,
        back_image_path=_back_image(tmp_path),
    )
    assert _page_count(duplex) == 2 * _page_count(single)


def test_fronts_then_backs_emits_the_same_pages_in_a_different_order(tmp_path: Path) -> None:
    pages = [
        [_slot(_pdf_source_face(tmp_path, "a"))],
        [_slot(_pdf_source_face(tmp_path, "b"))],
    ]
    layout = _a4_portrait_layout(cols=1, rows=1)
    kwargs = dict(
        layout=layout,
        export_dpi=600,
        back_printing=True,
        back_image_path=_back_image(tmp_path),
    )
    duplex = build_pdf(pages, page_order=PageOrder.DUPLEX, **kwargs)
    stacked = build_pdf(pages, page_order=PageOrder.FRONTS_THEN_BACKS, **kwargs)
    assert _page_count(duplex) == _page_count(stacked) == 4


def test_back_printing_off_leaves_output_untouched(tmp_path: Path) -> None:
    """Every back-printing argument is inert while back_printing is False —
    the historical single-sided output must be byte-identical, or an
    existing user's PDF silently changes the day they upgrade."""
    pages = [[_slot(_pdf_source_face(tmp_path, "a"))]]
    layout = _a4_portrait_layout(cols=1, rows=1)
    plain = build_pdf(pages, layout=layout, export_dpi=600)
    with_unused_back_settings = build_pdf(
        pages,
        layout=layout,
        export_dpi=600,
        back_printing=False,
        back_image_path=_back_image(tmp_path),
        flip_edge=FlipEdge.SHORT,
        page_order=PageOrder.FRONTS_THEN_BACKS,
    )
    assert _page_count(plain) == 1
    assert len(plain) == len(with_unused_back_settings)


def test_unique_image_count_counts_the_back_image_once(tmp_path: Path) -> None:
    """A Back Image filling nine Reverses is one decode, not nine — the
    progress bar has to size itself by real work or it stalls."""
    faces = [_pdf_source_face(tmp_path, name) for name in ("a", "b", "c")]
    pages = [[_slot(f) for f in faces]]
    back = _back_image(tmp_path)
    assert unique_image_count(pages) == 3
    assert unique_image_count(pages, back_printing=True, back_image_path=back) == 4


def test_back_faces_count_toward_the_image_total(tmp_path: Path) -> None:
    front = _pdf_source_face(tmp_path, "front")
    reverse = _pdf_source_face(tmp_path, "reverse")
    pages = [[PrintSlot(front=front, reverse=reverse)]]
    # No Reverse needs the Back Image here, so it isn't counted even though
    # one was supplied.
    assert unique_image_count(
        pages, back_printing=True, back_image_path=_back_image(tmp_path)
    ) == 2


# --- Guides ----------------------------------------------------------------


def test_the_four_guide_flags_are_independent(tmp_path: Path) -> None:
    """Each flag has to move the output on its own. A single shared switch
    (what show_cut_lines was) can't express "guides on the fronts I cut
    against, none on the backs that show"."""
    pages = [[_slot(_pdf_source_face(tmp_path, "a"))]]
    layout = _a4_portrait_layout(cols=1, rows=1)
    common = dict(
        layout=layout,
        export_dpi=600,
        back_printing=True,
        back_image_path=_back_image(tmp_path),
    )
    everything = build_pdf(
        pages,
        guides=GuideVisibility(False, False, False, False),
        **common,
    )
    nothing = build_pdf(pages, guides=GuideVisibility(True, True, True, True), **common)
    fronts_only = build_pdf(pages, guides=GuideVisibility(False, False, True, True), **common)
    assert len(everything) > len(fronts_only) > len(nothing)


def test_default_guide_visibility_hides_back_page_guides() -> None:
    """Deliberately NOT "preserve the old behaviour on both sides": you cut
    a duplex sheet against the front, so guides on the back are ink you
    can't use printed on the side that shows."""
    guides = GuideVisibility()
    assert (guides.hide_card_guides_front, guides.hide_page_guides_front) == (False, False)
    assert (guides.hide_card_guides_back, guides.hide_page_guides_back) == (True, True)


# --- Back Image preparation ------------------------------------------------


def test_fit_cover_preserves_aspect_ratio_on_a_square_source() -> None:
    """_resize_to_dpi stretches, which is fine for Scryfall art (already
    63:88) and wrong for an arbitrary upload. Cover-cropping distorts
    nothing and loses only the edges, which get cut off anyway."""
    square = Image.new("RGB", (400, 400))
    # Mark a horizontal band so a vertical squash would be detectable.
    fitted = fit_cover(square, (300, 420))
    assert fitted.size == (300, 420)


def test_render_back_image_respects_the_includes_bleed_declaration(tmp_path: Path) -> None:
    """Edge-extending art that already carries bleed would add a duplicate
    border and shrink the visible design, so the declared case is fitted
    straight to the bled size instead."""
    path = _back_image(tmp_path, size=(1000, 1000))
    already_bled = render_back_image(path, export_dpi=600, bleed_mm=1.0, includes_bleed=True)
    assert already_bled.size == _bled_pixels(600, 1.0)
    needs_bleed = render_back_image(path, export_dpi=600, bleed_mm=1.0, includes_bleed=False)
    # Larger than the trim box on both axes: bleed really was added.
    trim_w, trim_h = target_pixels(600)
    assert needs_bleed.width > trim_w and needs_bleed.height > trim_h


def test_blank_reverse_fill_still_emits_the_back_page(tmp_path: Path) -> None:
    """The mode for "print my transform cards' backs and nothing else".

    The Back Page has to exist even when every cell on it is empty:
    dropping it would pair every later Front Page with the wrong Back
    Page, which is worse than a blank sheet by a wide margin.
    """
    pages = [[_slot(_pdf_source_face(tmp_path, "a"))]]
    layout = _a4_portrait_layout(cols=1, rows=1)
    blank = build_pdf(
        pages,
        layout=layout,
        export_dpi=600,
        back_printing=True,
        reverse_fill=ReverseFill.BLANK,
    )
    assert _page_count(blank) == 2


def test_blank_reverse_fill_ignores_a_supplied_back_image(tmp_path: Path) -> None:
    """Choosing blank backs means blank backs — a Back Image that happens
    to still be selected must not sneak onto the sheet."""
    pages = [[_slot(_pdf_source_face(tmp_path, "a"))]]
    layout = _a4_portrait_layout(cols=1, rows=1)
    common = dict(layout=layout, export_dpi=600, back_printing=True)
    with_image = build_pdf(pages, back_image_path=_back_image(tmp_path), **common)
    blank = build_pdf(
        pages,
        back_image_path=_back_image(tmp_path),
        reverse_fill=ReverseFill.BLANK,
        **common,
    )
    assert len(blank) < len(with_image)
    # And it is not counted as work to do, either — a progress bar sized
    # for an image that never gets drawn stalls one tick short.
    assert (
        unique_image_count(
            pages,
            back_printing=True,
            back_image_path=_back_image(tmp_path),
            reverse_fill=ReverseFill.BLANK,
        )
        == 1
    )


def test_blank_fill_still_prints_back_faces(tmp_path: Path) -> None:
    """The point of the mode: transform sides still print on their own
    backs. Only the cards that HAVE no transform side come out blank."""
    dfc = PrintSlot(
        front=_pdf_source_face(tmp_path, "front"),
        reverse=_pdf_source_face(tmp_path, "transform"),
    )
    plain = _slot(_pdf_source_face(tmp_path, "plain"))
    pages = [[dfc, plain]]
    assert (
        unique_image_count(
            [[dfc, plain]], back_printing=True, reverse_fill=ReverseFill.BLANK
        )
        == 3
    )
    pdf = build_pdf(
        pages,
        layout=_a4_portrait_layout(cols=2, rows=1),
        export_dpi=600,
        back_printing=True,
        reverse_fill=ReverseFill.BLANK,
    )
    assert _page_count(pdf) == 2


# --- Back Page rotation ----------------------------------------------------


def test_rotation_follows_the_flip_axis_not_the_orientation() -> None:
    """Back images are drawn upside down exactly when the sheet turns about
    a HORIZONTAL axis, because that inverts up/down between the two sides
    of any one card. Turning about a vertical axis preserves up, so nothing
    is rotated — and that is the common case (portrait paper, long-edge
    duplex), which is why the correction is easy to miss."""
    portrait = _a4_portrait_layout(cols=3, rows=3)
    landscape = resolve_page_layout(page_w_mm=297.0, page_h_mm=210.0, cols=3, rows=3)

    assert back_pages_are_rotated(portrait, FlipEdge.LONG) is False
    assert back_pages_are_rotated(portrait, FlipEdge.SHORT) is True
    assert back_pages_are_rotated(landscape, FlipEdge.LONG) is True
    assert back_pages_are_rotated(landscape, FlipEdge.SHORT) is False


def test_rotation_and_mirroring_always_agree_on_the_axis() -> None:
    """The two halves of the same fact: whichever axis is mirrored decides
    whether up is preserved. Rows mirrored means rotate; columns mirrored
    means don't. Letting these disagree would put every back on the right
    card the wrong way up."""
    for page in ((210.0, 297.0), (297.0, 210.0)):
        layout = resolve_page_layout(page_w_mm=page[0], page_h_mm=page[1], cols=2, rows=2)
        for flip_edge in (FlipEdge.LONG, FlipEdge.SHORT):
            # Slot 0 mirrors to slot 1 when columns reverse, slot 2 when
            # rows reverse, on a 2×2 grid.
            mirrored_to = mirror_page_index(0, layout=layout, flip_edge=flip_edge)
            rows_mirrored = mirrored_to == 2
            assert back_pages_are_rotated(layout, flip_edge) is rows_mirrored


def test_a_rotated_back_page_actually_renders_differently(tmp_path: Path) -> None:
    """The table above is only worth anything if it reaches the page.

    Landscape sheet: long edge rotates, short edge does not, so the two
    renders of the same deck must differ in their bytes.
    """
    pages = [[_slot(_pdf_source_face(tmp_path, "a"))]]
    landscape = resolve_page_layout(page_w_mm=297.0, page_h_mm=210.0, cols=1, rows=1)
    common = dict(
        layout=landscape,
        export_dpi=600,
        back_printing=True,
        back_image_path=_back_image(tmp_path),
    )
    rotated = build_pdf(pages, flip_edge=FlipEdge.LONG, **common)
    upright = build_pdf(pages, flip_edge=FlipEdge.SHORT, **common)
    assert rotated != upright


def test_rotating_a_back_image_does_not_break_its_caching(tmp_path: Path) -> None:
    """The render cache is keyed on (path, rotated) rather than the path
    alone. Widening the key must not stop it being a cache: one back image
    filling four Reverses is still one decode, not four."""
    faces = [_pdf_source_face(tmp_path, name) for name in ("a", "b", "c", "d")]
    pages = [[_slot(f) for f in faces]]
    landscape = resolve_page_layout(page_w_mm=297.0, page_h_mm=210.0, cols=4, rows=1)
    calls: list[tuple[int, int]] = []
    build_pdf(
        pages,
        layout=landscape,
        export_dpi=600,
        back_printing=True,
        back_image_path=_back_image(tmp_path),
        # The rotating case, so the widened key is the one in use.
        flip_edge=FlipEdge.LONG,
        on_progress=lambda c, t: calls.append((c, t)),
    )
    # Four distinct fronts plus the one back image, each encoded once.
    assert calls == [(1, 5), (2, 5), (3, 5), (4, 5), (5, 5)]
