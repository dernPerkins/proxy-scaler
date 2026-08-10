"""Tests for pdf_html.py: the alternate WeasyPrint HTML->PDF pipeline."""

from __future__ import annotations

from pathlib import Path

import pytest
from PIL import Image

from proxy_scaler.pdf_layout import PAGE_SIZE_PRESETS_MM, paginate, resolve_page_layout
from proxy_scaler.pipeline import FaceResult


def _a4_portrait_layout(**overrides):
    kwargs = dict(
        page_w_mm=PAGE_SIZE_PRESETS_MM["a4"][0],
        page_h_mm=PAGE_SIZE_PRESETS_MM["a4"][1],
        cols=3,
        rows=3,
    )
    kwargs.update(overrides)
    return resolve_page_layout(**kwargs)


def _rounded_rect_rgba(w: int, h: int, radius: int) -> Image.Image:
    """Realistic rounded-rect alpha: a quarter-circle cutout at all 4
    corners, matching real Scryfall/upscaled card geometry — same
    fixture shape as test_pdf_layout.py's own helper."""
    img = Image.new("RGBA", (w, h), (10, 20, 30, 255))
    px = img.load()
    corners = [(0, 0), (w - radius, 0), (0, h - radius), (w - radius, h - radius)]
    for cx, cy in corners:
        for dy in range(radius):
            for dx in range(radius):
                ax = dx if cx == 0 else radius - 1 - dx
                ay = dy if cy == 0 else radius - 1 - dy
                center_dx, center_dy = radius - ax, radius - ay
                if center_dx * center_dx + center_dy * center_dy > radius * radius:
                    px[cx + dx, cy + dy] = (0, 0, 0, 0)
    return img


def test_build_pdf_html_flattens_corners_before_resizing_for_export_dpi(tmp_path: Path) -> None:
    """Regression guard for the corner-smearing bug (already fixed once
    for the fpdf2 path in pdf_layout.py::build_pdf — see
    test_pdf_layout.py's identically-named test): resizing an RGBA image
    while its rounded corners are still transparent lets the resample
    filter blend the transparent region's RGB into the opaque body right
    at the corner boundary, baking in a visible smear. build_pdf_html()
    must flatten before resizing for a different export_dpi, not after.
    Verified by call order, matching the fpdf2-path test's own approach."""
    pytest.importorskip("weasyprint")
    import proxy_scaler.pdf_html as pdf_html_module

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
    pages = paginate([face], layout.cards_per_page)

    call_order: list[str] = []
    real_flatten = pdf_html_module.flatten_corner_alpha
    real_resize = pdf_html_module._resize_to_dpi

    def spy_flatten(*args, **kwargs):
        call_order.append("flatten")
        return real_flatten(*args, **kwargs)

    def spy_resize(*args, **kwargs):
        call_order.append("resize")
        return real_resize(*args, **kwargs)

    pdf_html_module.flatten_corner_alpha = spy_flatten
    pdf_html_module._resize_to_dpi = spy_resize
    try:
        pdf_bytes = pdf_html_module.build_pdf_html(pages, layout=layout, export_dpi=1200)
    finally:
        pdf_html_module.flatten_corner_alpha = real_flatten
        pdf_html_module._resize_to_dpi = real_resize

    assert call_order[:2] == ["flatten", "resize"]
    assert len(pdf_bytes) > 100
