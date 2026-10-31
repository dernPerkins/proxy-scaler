"""The cut file that accompanies a printed sheet.

An SVG for import into the cutter's software: one rounded rectangle per
card trim box on the "cut-lines" layer. Pure geometry — no images, no
database — so it can be produced without a single card having been
generated. The one exception is CornerRadius.ALPHA_CARDS: which cells get
Alpha's rounder corner depends on which cards sit where, so the caller
passes each cell's radius (pdf_layout.cell_corner_radii) and a deck whose
sheets differ gets one file per sheet.

With a cutter selected (Silhouette Studio) the file is emitted in the
CUTTER's frame, not the page's: when the marks are rotated (see
pdf_layout.cutter_frame) the file is the landscape-or-portrait sheet the
cutter actually sees, with the square top-left, and every card box is
rotated to match. The user sets Studio's page orientation to the cutter
orientation and everything lines up.

The registration marks themselves are NOT in the file. Studio draws its
own marks from Page Setup, and those are what it scans; any shape in an
imported SVG is a cut path, so marks in here were cut on every sheet
unless the user found the layer and switched it off in the Send panel.

With no cutter selected the file is the trim boxes alone, in the page's
own frame. That is the route for machines that cut against the mat rather
than scanning marks — a Cricut cannot read third-party marks at all, so
the user prints the plain sheet, loads it square in the mat's corner and
imports this file.
"""

from __future__ import annotations

from collections.abc import Sequence
from xml.etree import ElementTree as ET

from .dpi import CARD_HEIGHT_MM, CARD_WIDTH_MM
from .pdf_layout import (
    PageLayout,
    Rect,
    RegistrationMarks,
    cell_corner_radii,
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


def build_cut_file_svg(
    layout: PageLayout,
    marks: RegistrationMarks | None,
    corner_radii: Sequence[float] | None = None,
) -> str:
    """The SVG document, as text. Uses the FRONT layout: you cut from the
    side the marks are read on. Only `marks.orientation` matters here — it
    picks the frame; the marks are never drawn (see the module docstring).
    `marks` None (no cutter selected) means no rotation: the page frame as
    printed. `corner_radii` is each cell's corner radius, row-major; None
    is the layout's base radius everywhere (cell_corner_radii of an empty
    page), which is the whole answer unless Proper Alpha Cards is on."""
    if corner_radii is None:
        corner_radii = cell_corner_radii((), layout)
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
    cuts = ET.SubElement(root, "g", {"id": "cut-lines"})
    for page_rect, radius in zip(card_trim_rects(layout), corner_radii, strict=True):
        r = page_rect_to_cutter(page_rect, layout.page_w_mm, layout.page_h_mm, rotated)
        ET.SubElement(
            cuts,
            "rect",
            {
                "x": _fmt(r.x),
                "y": _fmt(r.y),
                "width": _fmt(r.w),
                "height": _fmt(r.h),
                "rx": _fmt(radius),
                "ry": _fmt(radius),
                "fill": "none",
                "stroke": "#f00",
                "stroke-width": "0.1",
            },
        )
    body = ET.tostring(root, encoding="unicode")
    return '<?xml version="1.0" encoding="UTF-8"?>\n' + body + "\n"
