"""The cut file that accompanies a printed sheet.

An SVG for import into the cutter's software: one rounded rectangle per
card trim box on the "cut-lines" layer. Pure geometry — no images, no
database — so it can be produced without a single card having been
generated.

With a cutter selected (Silhouette Studio) the registration marks come
along on their own layer, so the user can line the import up against
Studio's own marks (and set that layer to no-cut), and the file is emitted
in the CUTTER's frame, not the page's: when the marks are rotated (see
pdf_layout.cutter_frame) the file is the landscape-or-portrait sheet the
cutter actually sees, with the square top-left, and every card box is
rotated to match. The user sets Studio's page orientation to the cutter
orientation and everything lines up.

With no cutter selected the file is the trim boxes alone, in the page's
own frame. That is the route for machines that cut against the mat rather
than scanning marks — a Cricut cannot read third-party marks at all, so
the user prints the plain sheet, loads it square in the mat's corner and
imports this file.
"""

from __future__ import annotations

from xml.etree import ElementTree as ET

from .dpi import CARD_HEIGHT_MM, CARD_WIDTH_MM
from .pdf_layout import (
    CARD_CORNER_RADIUS_MM,
    PageLayout,
    Rect,
    RegistrationMarks,
    _mark_rects_in_frame,
    cutter_frame,
    page_rect_to_cutter,
)

SVG_NS = "http://www.w3.org/2000/svg"


def _fmt(v: float) -> str:
    return f"{v:.3f}"


def card_trim_rects(layout: PageLayout) -> list[Rect]:
    """Every grid position's trim box (bleed excluded), page coordinates,
    row-major like build_pdf. The full grid, not just the occupied slots:
    a cut file is a per-sheet template, and a partial last page cuts the
    empty positions on blank paper harmlessly."""
    rects: list[Rect] = []
    for row in range(layout.rows):
        for col in range(layout.cols):
            rects.append(
                Rect(
                    layout.margin_x_mm + col * layout.cell_w_mm + layout.bleed_mm,
                    layout.margin_y_mm + row * layout.cell_h_mm + layout.bleed_mm,
                    CARD_WIDTH_MM,
                    CARD_HEIGHT_MM,
                )
            )
    return rects


def build_cut_file_svg(layout: PageLayout, marks: RegistrationMarks | None) -> str:
    """The SVG document, as text. Uses the FRONT layout: you cut from the
    side the marks are read on. Ignores the marks' hide flags — the file
    is for the cutter, which needs them regardless of which printed side
    carries them. `marks` None (no cutter selected) means no marks layer
    and no rotation: the page frame as printed."""
    if marks is None:
        frame_w, frame_h, rotated = layout.page_w_mm, layout.page_h_mm, False
    else:
        frame_w, frame_h, rotated = cutter_frame(
            layout.page_w_mm, layout.page_h_mm, marks.orientation
        )

    root = ET.Element(
        "svg",
        {
            "xmlns": SVG_NS,
            "width": f"{_fmt(frame_w)}mm",
            "height": f"{_fmt(frame_h)}mm",
            "viewBox": f"0 0 {_fmt(frame_w)} {_fmt(frame_h)}",
        },
    )
    if marks is not None:
        reg = ET.SubElement(root, "g", {"id": "registration-marks"})
        for r in _mark_rects_in_frame(frame_w, frame_h, style=marks.style, inset_mm=marks.inset_mm):
            ET.SubElement(
                reg,
                "rect",
                {"x": _fmt(r.x), "y": _fmt(r.y), "width": _fmt(r.w), "height": _fmt(r.h), "fill": "#000"},
            )
    cuts = ET.SubElement(root, "g", {"id": "cut-lines"})
    for page_rect in card_trim_rects(layout):
        r = page_rect_to_cutter(page_rect, layout.page_w_mm, layout.page_h_mm, rotated)
        ET.SubElement(
            cuts,
            "rect",
            {
                "x": _fmt(r.x),
                "y": _fmt(r.y),
                "width": _fmt(r.w),
                "height": _fmt(r.h),
                "rx": _fmt(CARD_CORNER_RADIUS_MM),
                "ry": _fmt(CARD_CORNER_RADIUS_MM),
                "fill": "none",
                "stroke": "#f00",
                "stroke-width": "0.1",
            },
        )
    body = ET.tostring(root, encoding="unicode")
    return '<?xml version="1.0" encoding="UTF-8"?>\n' + body + "\n"
