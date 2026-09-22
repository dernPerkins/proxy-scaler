"""Print-sheet PDF layout: bleed extension, cut guides, grid pagination.

Pure logic, no Streamlit dependency — mirrors the pipeline.py/decklist.py
separation used elsewhere in this codebase.
"""

from __future__ import annotations

import io
from collections.abc import Callable, Sequence
from dataclasses import dataclass
from enum import Enum
from pathlib import Path

import numpy as np
from fpdf import FPDF
from PIL import Image

from .decklist import DeckEntry
from .dpi import (
    CARD_HEIGHT_MM,
    CARD_WIDTH_MM,
    CUSTOM_SOURCE_MODEL,
    MM_PER_IN,
    ORIGINAL_MODEL,
    bled_target_pixels,
    target_pixels,
)
from .pipeline import FaceResult, _resize_to_dpi, group_by_face
from .edge_extend import corner_radius_px, extend_edges

# fpdf2 embeds raw PIL images losslessly (FlateDecode/zlib), which compresses
# photographic card art poorly (~1.5-3x) and produces huge files — a 9-card
# page of 1200 DPI art can run 70+MB that way. Pre-encoding to JPEG lets
# fpdf2 take its fast DCTDecode passthrough path instead: far smaller files,
# and large embedded images are less likely to trigger a PDF viewer's own
# performance-driven downsampling (which can look like pixelation even
# though the underlying pixel data is untouched). Quality 92 is visually
# indistinguishable from lossless for photographic content at normal zoom.
_JPEG_QUALITY = 92
BLEED_MM = 1.0

# CARD_WIDTH_MM / CARD_HEIGHT_MM / MM_PER_IN live in dpi.py (imported above)
# so the mm trim box and the pixel raster can't drift apart.

# Portrait-native (width, height) — a starting point for the UI's page-size
# preset dropdown; actual page dimensions are freely user-editable from here.
PAGE_SIZE_PRESETS_MM: dict[str, tuple[float, float]] = {
    "letter": (215.9, 279.4),
    "a4": (210.0, 297.0),
    "a3": (297.0, 420.0),
}


class FlipEdge(str, Enum):
    """Which edge a duplex printer turns the sheet on. It decides which
    axis a Back Page mirrors, and it must match the printer's own duplex
    setting or every card gets someone else's back.

    Which axis that is depends on the page's orientation, because "long
    edge" names a physical edge of the paper, not a direction: a portrait
    sheet's long edges are its left and right sides, so flipping on them
    turns the sheet about a vertical axis and mirrors COLUMNS; a landscape
    sheet's long edges are top and bottom, so the same setting mirrors
    ROWS. See mirror_page_index.
    """

    LONG = "long"
    SHORT = "short"


class ReverseFill(str, Enum):
    """What goes on a Reverse that has no Back Face of its own.

    BACK_IMAGE prints the project's Selected Back there. BLANK prints
    nothing, leaving that side of the card empty — which is the whole
    point of it: someone printing a deck for its double-faced cards wants
    those transform sides on their own backs and does not want (or have)
    a card back for everything else.

    BLANK still emits the Back Page. The page has to exist for the sheet
    to stay in register: drop it and every later Front Page pairs with the
    wrong Back Page.
    """

    BACK_IMAGE = "back_image"
    BLANK = "blank"


class PageOrder(str, Enum):
    """DUPLEX (front 1, back 1, front 2, back 2, ...) is what a duplex
    printer driver expects, and is named for the thing the user is
    actually doing rather than for the interleaving that implements it.
    FRONTS_THEN_BACKS emits every Front Page first, then every Back Page in
    matching order, for hand-feeding a stack back through a single-sided
    printer. Mirroring applies to both: it is about how paper physically
    turns over, not about what order the pages come out in.
    """

    DUPLEX = "duplex"
    FRONTS_THEN_BACKS = "fronts_then_backs"


@dataclass(frozen=True)
class GuideVisibility:
    """Which guides to draw, per guide kind and per page kind.

    Stored as HIDE flags rather than show flags so the wire format, the
    stored setting, and the checkbox the user actually ticks all share one
    polarity — there is no `not` anywhere between the UI and here to invert
    by accident.

    The Back Page defaults are `True` (hidden) while the Front Page
    defaults are `False`. That asymmetry is deliberate and is NOT just
    "preserve the old behaviour": you cut a duplex sheet against the guides
    on its front, so guides on the back are ink you cannot use, printed on
    the side of the card that shows.
    """

    hide_card_guides_front: bool = False
    hide_page_guides_front: bool = False
    hide_card_guides_back: bool = True
    hide_page_guides_back: bool = True

# --- Registration marks (electronic cutters) -------------------------------
#
# Silhouette "Type 1" registration marks: a filled 5mm square in the
# top-left corner and L-shaped brackets in the top-right and bottom-left
# corners, each L's arms pointing in toward the page centre. The newer
# four-mark machines use an L in every corner instead — no square. The cutter's optical scanner
# finds these to align its cut file with the printed sheet, so the geometry
# is fixed here rather than user-editable: Silhouette Studio never scales
# the square, and the length/thickness below are what the UI tells users to
# enter in Studio. Thickness is Studio's maximum — faint or thin marks are
# the most common cause of "registration failed".
REG_SQUARE_MM = 5.0
REG_ARM_MM = 8.89  # 0.35 in
REG_THICKNESS_MM = 1.0  # 0.039 in, Silhouette Studio's maximum
# Clearance around every mark that must carry no other ink. Studio hatches
# a zone around each mark and the scanner cannot read through printed
# content there — a stray page guide in that zone fails the whole sheet.
REG_KEEP_OUT_MM = 3.0
REG_INSET_MIN_MM = 5.0
REG_INSET_DEFAULT_MM = 10.0  # Studio's "Standard" inset, 0.394 in
# 1/8 in — the corner radius a cut file rounds each card's trim box to.
CARD_CORNER_RADIUS_MM = 3.175


class CutterMarkStyle(str, Enum):
    THREE_POINT = "three_point"  # Cameo 4/5, Portrait: square + two L's
    FOUR_POINT = "four_point"  # Cameo 5α, Pro MK II: an L in all four corners


class CutterOrientation(str, Enum):
    """Which way the sheet is loaded into the cutter. Independent of the
    page's own orientation, which only says how it went through the
    printer: a portrait sheet can be loaded landscape into a Cameo, and the
    marks then have to be laid out for the landscape frame the cutter
    sees. See cutter_frame for the rotation convention."""

    PORTRAIT = "portrait"
    LANDSCAPE = "landscape"


@dataclass(frozen=True)
class Rect:
    """An axis-aligned box in mm, y down — page coordinates unless a
    function says otherwise."""

    x: float
    y: float
    w: float
    h: float

    @property
    def x1(self) -> float:
        return self.x + self.w

    @property
    def y1(self) -> float:
        return self.y + self.h

    def inflated(self, d: float) -> Rect:
        return Rect(self.x - d, self.y - d, self.w + 2 * d, self.h + 2 * d)


def rects_overlap(a: Rect, b: Rect) -> bool:
    """Strictly positive overlap area — boxes that merely touch don't count."""
    eps = 1e-9
    return (
        min(a.x1, b.x1) - max(a.x, b.x) > eps
        and min(a.y1, b.y1) - max(a.y, b.y) > eps
    )


@dataclass(frozen=True)
class RegistrationMarks:
    """The cutter registration marks a sheet carries. Same per-page-kind
    polarity as GuideVisibility, for the same reason: `hide_back` defaults
    True because the marks only matter on the side you cut from, and the
    back is the side of the card that shows."""

    style: CutterMarkStyle = CutterMarkStyle.THREE_POINT
    orientation: CutterOrientation = CutterOrientation.PORTRAIT
    inset_mm: float = REG_INSET_DEFAULT_MM
    hide_front: bool = False
    hide_back: bool = True

    def drawn_on(self, *, is_back: bool) -> bool:
        return not self.hide_back if is_back else not self.hide_front


def page_orientation(page_w_mm: float, page_h_mm: float) -> CutterOrientation:
    """Same rule PageLayout.orientation uses: a square page is portrait."""
    return CutterOrientation.PORTRAIT if page_h_mm >= page_w_mm else CutterOrientation.LANDSCAPE


def cutter_frame(
    page_w_mm: float, page_h_mm: float, orientation: CutterOrientation
) -> tuple[float, float, bool]:
    """(width, height, rotated) of the frame the cutter sees the sheet in.

    When the requested orientation matches the page's own, the frame is the
    page and nothing rotates. When it differs, the convention — chosen
    here once, and relied on by the PDF marks, the preview and the cut file
    alike — is that the sheet is loaded turned 90° CLOCKWISE: the page's
    bottom-left corner becomes the cutter's top-left, where the square
    goes. The user never has to know which way round: they load the sheet
    with the square mark top-left, and everything else follows from that.
    """
    if orientation is page_orientation(page_w_mm, page_h_mm):
        return page_w_mm, page_h_mm, False
    return page_h_mm, page_w_mm, True


def cutter_rect_to_page(rect: Rect, page_w_mm: float, page_h_mm: float, rotated: bool) -> Rect:
    """A cutter-frame box in page coordinates (see cutter_frame)."""
    if not rotated:
        return rect
    return Rect(rect.y, page_h_mm - rect.x - rect.w, rect.h, rect.w)


def page_rect_to_cutter(rect: Rect, page_w_mm: float, page_h_mm: float, rotated: bool) -> Rect:
    """Inverse of cutter_rect_to_page — a page box in cutter coordinates."""
    if not rotated:
        return rect
    return Rect(page_h_mm - rect.y - rect.h, rect.x, rect.h, rect.w)


def _mark_rects_in_frame(
    frame_w: float, frame_h: float, *, style: CutterMarkStyle, inset_mm: float
) -> list[Rect]:
    """The filled rectangles that make up the marks, in the cutter frame.
    Each L is two overlapping bars (harmless — both solid black). Every mark
    lies entirely inside the inset boundary: the square's outer corner and
    each L's outer vertex sit exactly `inset_mm` from their two edges."""
    i, a, t = inset_mm, REG_ARM_MM, REG_THICKNESS_MM
    if style is CutterMarkStyle.FOUR_POINT:
        # Top-left L: arms run right and down.
        rects = [Rect(i, i, a, t), Rect(i, i, t, a)]
    else:
        rects = [Rect(i, i, REG_SQUARE_MM, REG_SQUARE_MM)]
    rects += [
        # Top-right L: arms run left and down from the corner vertex.
        Rect(frame_w - i - a, i, a, t),
        Rect(frame_w - i - t, i, t, a),
        # Bottom-left L: arms run right and up.
        Rect(i, frame_h - i - t, a, t),
        Rect(i, frame_h - i - a, t, a),
    ]
    if style is CutterMarkStyle.FOUR_POINT:
        # Bottom-right L: arms run left and up.
        rects.append(Rect(frame_w - i - a, frame_h - i - t, a, t))
        rects.append(Rect(frame_w - i - t, frame_h - i - a, t, a))
    return rects


def _mark_bboxes_in_frame(
    frame_w: float, frame_h: float, *, style: CutterMarkStyle, inset_mm: float
) -> list[Rect]:
    """One bounding box per mark (not per bar), in the cutter frame."""
    i, a = inset_mm, REG_ARM_MM
    boxes = [
        Rect(i, i, a, a) if style is CutterMarkStyle.FOUR_POINT else Rect(i, i, REG_SQUARE_MM, REG_SQUARE_MM),
        Rect(frame_w - i - a, i, a, a),
        Rect(i, frame_h - i - a, a, a),
    ]
    if style is CutterMarkStyle.FOUR_POINT:
        boxes.append(Rect(frame_w - i - a, frame_h - i - a, a, a))
    return boxes


def registration_mark_rects(
    page_w_mm: float, page_h_mm: float, marks: RegistrationMarks
) -> list[Rect]:
    """The filled black rectangles to draw, in PAGE coordinates. Depends on
    the page size and the marks alone — never on the grid or its offsets,
    so front and back pages carry identical marks."""
    fw, fh, rotated = cutter_frame(page_w_mm, page_h_mm, marks.orientation)
    return [
        cutter_rect_to_page(r, page_w_mm, page_h_mm, rotated)
        for r in _mark_rects_in_frame(fw, fh, style=marks.style, inset_mm=marks.inset_mm)
    ]


def registration_mark_bboxes(
    page_w_mm: float, page_h_mm: float, marks: RegistrationMarks
) -> list[Rect]:
    fw, fh, rotated = cutter_frame(page_w_mm, page_h_mm, marks.orientation)
    return [
        cutter_rect_to_page(r, page_w_mm, page_h_mm, rotated)
        for r in _mark_bboxes_in_frame(fw, fh, style=marks.style, inset_mm=marks.inset_mm)
    ]


def registration_keep_out(
    page_w_mm: float, page_h_mm: float, marks: RegistrationMarks
) -> list[Rect]:
    """The zones (page coordinates) that must carry no ink but the mark."""
    return [b.inflated(REG_KEEP_OUT_MM) for b in registration_mark_bboxes(page_w_mm, page_h_mm, marks)]


def registration_conflict(layout: PageLayout, keep_out: list[Rect]) -> bool:
    """Does any card's bled box intrude on a keep-out zone? A warning, not
    an error, like the grid-overflow check: the marks stay where the cutter
    expects them and the user is told to change the grid."""
    for row in range(layout.rows):
        for col in range(layout.cols):
            card = Rect(
                layout.margin_x_mm + col * layout.cell_w_mm,
                layout.margin_y_mm + row * layout.cell_h_mm,
                layout.bled_card_w_mm,
                layout.bled_card_h_mm,
            )
            if any(rects_overlap(card, zone) for zone in keep_out):
                return True
    return False


def _clip_edge_segment(
    start: float,
    end: float,
    cross_lo: float,
    cross_hi: float,
    keep_out: Sequence[Rect],
    *,
    vertical: bool,
    from_edge: bool,
) -> tuple[float, float] | None:
    """Shorten one page guide so it never enters a keep-out zone.

    A page guide is axis-aligned and runs between a page edge and the grid;
    `start < end` are its coordinates along its own axis, and
    `[cross_lo, cross_hi]` is its stroke's extent across it. `from_edge`
    says the segment begins at the page edge (top/left) rather than ending
    at one (bottom/right). Any zone the stroke crosses cuts the segment
    back to the zone's far side, so nothing survives between the page edge
    and the mark — a stub of black line there is exactly what confuses the
    scanner. Returns None when nothing is left.
    """
    for zone in keep_out:
        cross0, cross1 = (zone.x, zone.x1) if vertical else (zone.y, zone.y1)
        along0, along1 = (zone.y, zone.y1) if vertical else (zone.x, zone.x1)
        if cross1 <= cross_lo or cross0 >= cross_hi:
            continue
        if along1 <= start or along0 >= end:
            continue
        if from_edge:
            start = max(start, along1)
        else:
            end = min(end, along0)
    return (start, end) if start < end else None


def _draw_registration_marks(
    pdf: FPDF, page_w_mm: float, page_h_mm: float, marks: RegistrationMarks
) -> None:
    """Solid black, filled (no stroke): a stroked outline would pick up the
    guide line width and blur the mark's edge the scanner locks on to."""
    pdf.set_fill_color(0, 0, 0)
    for r in registration_mark_rects(page_w_mm, page_h_mm, marks):
        pdf.rect(r.x, r.y, r.w, r.h, style="F")


# Outer guide lines run from the page edge to the card grid block — full,
# dark, and continuous (there's no card content there to obscure, and a
# continuous line is what you actually align a paper cutter against).
# Same stroke width as the green marks (PageLayout.guide_width_pt).
_OUTER_LINE_COLOR = (0, 0, 0)

# Inner crop marks sit at each card's own trim corner — a small green "+".
# Defaults only now — both are user-configurable (PageLayout.guide_width_pt/
# guide_length_mm), still 0.75pt (a print/PDF point, not a screen pixel) /
# 2.75mm out of the box.
_MARK_LENGTH_MM = 2.75
_MARK_WIDTH_PT = 0.75
_MARK_COLOR = (0, 170, 80)


@dataclass(frozen=True)
class PageLayout:
    orientation: str  # derived, display-only: "portrait" or "landscape"
    page_w_mm: float
    page_h_mm: float
    cols: int
    rows: int
    bleed_mm: float
    spacing_x_mm: float
    spacing_y_mm: float
    bled_card_w_mm: float  # CARD_WIDTH_MM + 2*bleed_mm
    bled_card_h_mm: float
    cell_w_mm: float  # bled_card_w_mm + spacing_x_mm — grid stride
    cell_h_mm: float
    margin_x_mm: float  # auto-centered, plus any position-adjustment offset
    margin_y_mm: float
    grid_w_mm: float  # so callers can warn (not block) if this exceeds the page
    grid_h_mm: float
    guide_width_pt: float
    guide_length_mm: float
    cards_per_page: int


def resolve_page_layout(
    *,
    page_w_mm: float,
    page_h_mm: float,
    cols: int,
    rows: int,
    bleed_mm: float = BLEED_MM,
    spacing_x_mm: float = 0.0,
    spacing_y_mm: float = 0.0,
    offset_x_mm: float = 0.0,
    offset_y_mm: float = 0.0,
    guide_width_pt: float = _MARK_WIDTH_PT,
    guide_length_mm: float = _MARK_LENGTH_MM,
) -> PageLayout:
    """Resolve a full print-grid geometry from freely user-configurable
    dimensions. Does NOT raise if the grid doesn't fit the page — unlike
    named-paper-size layouts, a custom offset can deliberately push the grid
    near or past an edge (e.g. to work around a specific printer's feed
    quirk); callers should check `grid_w_mm`/`grid_h_mm` against
    `page_w_mm`/`page_h_mm` themselves and surface a non-blocking warning.
    """
    if page_w_mm <= 0 or page_h_mm <= 0:
        raise ValueError("Page width/height must be positive")
    if cols <= 0 or rows <= 0:
        raise ValueError("Columns/rows must be positive")
    if bleed_mm < 0 or spacing_x_mm < 0 or spacing_y_mm < 0:
        raise ValueError("Bleed/spacing must not be negative")

    bled_card_w = CARD_WIDTH_MM + 2 * bleed_mm
    bled_card_h = CARD_HEIGHT_MM + 2 * bleed_mm
    cell_w = bled_card_w + spacing_x_mm
    cell_h = bled_card_h + spacing_y_mm
    grid_w = cols * cell_w - spacing_x_mm  # last card has no trailing gap
    grid_h = rows * cell_h - spacing_y_mm

    return PageLayout(
        orientation="portrait" if page_h_mm >= page_w_mm else "landscape",
        page_w_mm=page_w_mm,
        page_h_mm=page_h_mm,
        cols=cols,
        rows=rows,
        bleed_mm=bleed_mm,
        spacing_x_mm=spacing_x_mm,
        spacing_y_mm=spacing_y_mm,
        bled_card_w_mm=bled_card_w,
        bled_card_h_mm=bled_card_h,
        cell_w_mm=cell_w,
        cell_h_mm=cell_h,
        margin_x_mm=(page_w_mm - grid_w) / 2 + offset_x_mm,
        margin_y_mm=(page_h_mm - grid_h) / 2 + offset_y_mm,
        grid_w_mm=grid_w,
        grid_h_mm=grid_h,
        guide_width_pt=guide_width_pt,
        guide_length_mm=guide_length_mm,
        cards_per_page=cols * rows,
    )


def _card_trim_edges(
    count: int, cell_mm: float, bleed_mm: float, origin_mm: float
) -> list[float]:
    """Each card's own pair of trim-edge coordinates along one axis: card i's
    leading edge is origin + i*cell_mm + bleed_mm, its trailing edge is
    origin + (i+1)*cell_mm - bleed_mm. Two adjacent cards' facing edges are
    2*bleed_mm apart, not coincident — each card's bleed independently
    extends bleed_mm past its own trim edge, so the true cut line for card i
    and the true cut line for card i+1 are two distinct points close
    together, not one shared line down the middle of the gap. 2*count
    coordinates total (vs. a naive count+1 shared-boundary model)."""
    edges: list[float] = []
    for i in range(count):
        edges.append(origin_mm + i * cell_mm + bleed_mm)
        edges.append(origin_mm + (i + 1) * cell_mm - bleed_mm)
    return edges


def _draw_cut_marks(
    pdf: FPDF,
    layout: PageLayout,
    *,
    card_guides: bool = True,
    page_guides: bool = True,
    keep_out: Sequence[Rect] = (),
) -> None:
    """Draw Page Guides (black lines from the page edge to the grid block —
    outer margins, nothing to obscure there; two closely-spaced lines per
    interior gap, one per card's own trim edge) and Card Guides (small
    green "+" marks at each card's own trim corner — the grid is regular,
    so the full xs × ys cross product is exactly every card's 4 corners, no
    card ever gets a mark that isn't its own).

    The two kinds are independently switchable per page kind — see
    GuideVisibility. They share the geometry below, so the flags gate the
    drawing, not the computation.

    Every guide is nudged OUTWARD from its card by half its stroke width.
    A stroke centered on the trim coordinate puts half its ink inside the
    card, where it survives a clean cut as a hairline along the card's
    border; nudged, the stroke's inner edge sits exactly on the trim line
    (card edge, then guide) and a perfectly cut card carries no guide ink.
    Which way is outward falls out of _card_trim_edges' ordering: even
    indices are leading (left/top) edges, odd are trailing (right/bottom).

    `keep_out` (registration-mark zones, see registration_keep_out) clips
    Page Guides only: they are the ones that run out to the page edge and
    through the corners the cutter scans. Card Guides stay inside the grid."""
    if not card_guides and not page_guides:
        return

    width_mm = layout.guide_width_pt / 72 * MM_PER_IN
    half = width_mm / 2
    xs = [
        x - half if i % 2 == 0 else x + half
        for i, x in enumerate(
            _card_trim_edges(layout.cols, layout.cell_w_mm, layout.bleed_mm, layout.margin_x_mm)
        )
    ]
    ys = [
        y - half if i % 2 == 0 else y + half
        for i, y in enumerate(
            _card_trim_edges(layout.rows, layout.cell_h_mm, layout.bleed_mm, layout.margin_y_mm)
        )
    ]
    grid_x0, grid_x1 = xs[0], xs[-1]
    grid_y0, grid_y1 = ys[0], ys[-1]

    pdf.set_line_width(width_mm)

    if page_guides:
        pdf.set_draw_color(*_OUTER_LINE_COLOR)
        # start < end is the old "grid edge inside the page" test, and the
        # clip is a no-op with no keep-out zones.
        for x in xs:
            seg = _clip_edge_segment(
                0.0, grid_y0, x - half, x + half, keep_out, vertical=True, from_edge=True
            )
            if seg:
                pdf.line(x, seg[0], x, seg[1])
            seg = _clip_edge_segment(
                grid_y1, layout.page_h_mm, x - half, x + half, keep_out,
                vertical=True, from_edge=False,
            )
            if seg:
                pdf.line(x, seg[0], x, seg[1])
        for y in ys:
            seg = _clip_edge_segment(
                0.0, grid_x0, y - half, y + half, keep_out, vertical=False, from_edge=True
            )
            if seg:
                pdf.line(seg[0], y, seg[1], y)
            seg = _clip_edge_segment(
                grid_x1, layout.page_w_mm, y - half, y + half, keep_out,
                vertical=False, from_edge=False,
            )
            if seg:
                pdf.line(seg[0], y, seg[1], y)

    if card_guides:
        pdf.set_draw_color(*_MARK_COLOR)
        for x in xs:
            for y in ys:
                pdf.line(x - layout.guide_length_mm, y, x + layout.guide_length_mm, y)
                pdf.line(x, y - layout.guide_length_mm, x, y + layout.guide_length_mm)


# How far inside the true edge the export-time extension samples from, in
# mm. Straight edges: 0.085mm, one pixel at Scryfall's render scale — the
# depth of every measured straight-edge defect, and as thin as possible
# because on borderless art the re-sourced strip is a visible band. Corner
# arcs: 0.25mm, past the gold template's rim and highlight line plus
# whatever the upscaler smeared across the alpha boundary. Both strips sit
# on the trim line and far inside anything genuine (the modern
# collector-info bar is ~6mm deep). See postprocess.py for the same split
# at download time.
_EDGE_INSET_MM = 0.085
_CORNER_INSET_MM = 0.25


def _mm_px(width: int, height: int, mm: float) -> int:
    """`mm` in pixels for an image that is a whole card wide."""
    return max(1, round(min(width, height) / CARD_WIDTH_MM * mm))


def _inset_px(width: int, height: int) -> int:
    """_EDGE_INSET_MM in pixels (straight edges)."""
    return _mm_px(width, height, _EDGE_INSET_MM)


def _corner_inset_px(width: int, height: int) -> int:
    """_CORNER_INSET_MM in pixels (the arcs)."""
    return _mm_px(width, height, _CORNER_INSET_MM)


def flatten_corner_alpha(image: Image.Image) -> Image.Image:
    """Flatten the rounded-corner alpha to fully opaque, and re-source the
    outer 0.25mm strip (rim included) from just inside it.

    Physical proxy printing prints a full opaque rectangle then rounds the
    physical paper corners afterward with a punch tool — so the print should
    have zero transparent/unprinted regions. The transparent arcs are filled
    with the nearest-point extension (edge_extend.extend_edges), the same
    geometry add_bleed() uses for the bleed border, so the corner and the
    bleed beyond it read as one continuous fan. The same pass overwrites the
    anti-aliased rim and whatever the upscaler smeared into it (a dark arc
    on light cards, a white arc on gold-bordered ones).

    Strictly an export-time step: it belongs to the PDF/export pipelines
    only, never to generation. Baking the fill into the stored PNG is
    irreversible and visible when the image is viewed on its own — see
    pipeline.py::_write_dpi_variant, which deliberately does not call this.
    """
    rgba = image.convert("RGBA")
    radius = corner_radius_px(rgba)
    arr = np.asarray(rgba)
    extended = _extend_card(arr[..., :3], alpha=arr[..., 3], radius_px=radius, bleed_px=0)
    return Image.fromarray(extended, "RGB").convert("RGBA")


def _extend_card(
    rgb: np.ndarray, *, alpha: np.ndarray, radius_px: int, bleed_px: int
) -> np.ndarray:
    """Two passes of edge_extend.extend_edges, so the visible card edge is
    the trim line.

    1. Mirror the border into the outer strip (_EDGE_INSET_MM along the
       straight edges, _CORNER_INSET_MM around the arcs: rim, transparent
       corners and the trim-line strip). That strip is where every known
       edge defect lives; mirroring replaces it with more of the border's
       own texture instead of a streak, so on a textured border nothing
       visibly changes at the inset boundary.
    2. Fan the bleed out from the TRUE edge (inset 0) of that result. The
       rays start exactly on the trim line — which is where the cut guides
       put their inner edge — rather than 0.25mm inside it. Mirror first
       is idempotent, so running this at native resolution (flatten) and
       again after the export resize (add_bleed) changes nothing.
    """
    h, w = rgb.shape[:2]
    face = extend_edges(
        rgb,
        radius_px=radius_px,
        inset_px=_inset_px(w, h),
        corner_inset_px=_corner_inset_px(w, h),
        bleed_px=0,
        mode="mirror",
        alpha=alpha,
    )
    return extend_edges(face, radius_px=radius_px, inset_px=0, bleed_px=bleed_px, mode="nearest")


def add_bleed(
    image: Image.Image,
    *,
    dpi: int,
    bleed_mm: float = BLEED_MM,
    radius_px: int | None = None,
) -> Image.Image:
    """Extend a bleed border on all sides. Returns an opaque RGB image.

    Every bleed pixel takes the colour of the nearest point on the card's
    rounded rectangle, inset by _EDGE_INSET_MM: straight edges stretch
    perpendicularly, corners fan out radially from the arc. The transparent
    corners themselves are filled the same way, so this is complete on its
    own for an RGBA card image.

    `radius_px` is the corner radius at this image's scale. Callers that
    already flattened the corners (so the alpha no longer carries the arc)
    must pass it; otherwise it is measured from the alpha channel, and an
    image with no alpha — the preview thumbnails, square-cornered Custom
    Images — gets 0, i.e. plain clamp-to-edge replication.
    """
    rgba = image.convert("RGBA")
    if radius_px is None:
        radius_px = corner_radius_px(rgba)
    arr = np.asarray(rgba)
    bleed_px = max(1, round(dpi / MM_PER_IN * bleed_mm))
    extended = _extend_card(
        arr[..., :3], alpha=arr[..., 3], radius_px=radius_px, bleed_px=bleed_px
    )
    return Image.fromarray(extended, "RGB")


@dataclass
class PrintUnit:
    face_key: str
    quantity: int
    best: FaceResult  # chosen image variant for this face (see _pick_dpi_variant)
    # True only for a Custom Image printed at a resolution other than the
    # requested one. A Scryfall face with no image at the requested DPI is
    # excluded and reported rather than silently printed at another
    # resolution, so this is always False for those — see _pick_dpi_variant
    # for why customs are the deliberate exception.
    dpi_fallback: bool = False


def _describe_face(face: FaceResult) -> str:
    """Human-readable identification for error reporting — the print/PDF
    surface is where a user finds out something never generated, so this
    names the card the way they'd recognise it rather than exposing an
    internal face_group_key."""
    name = face.card_name or face.face_name or "Unknown card"
    if face.face_label:
        name = f"{name} ({face.face_label})"
    if face.set_code and face.collector_number:
        return f"{name} [{face.set_code.upper()} {face.collector_number}]"
    return name


def _describe_entry(entry: DeckEntry, matched: int, expected: int | None) -> str:
    """Human-readable identification for a decklist entry that has no
    generated image at all, or (when `expected` is known) is missing one
    or more of a multi-face card's faces."""
    name = entry.name
    if entry.set_code and entry.collector_number:
        name = f"{entry.name} [{entry.set_code.upper()} {entry.collector_number}]"
    if expected and 0 < matched < expected:
        return f"{name} — {matched} of {expected} faces generated"
    return name


def _recency_key(item: FaceResult) -> tuple[int, str]:
    """Sort key for "most recently produced wins". created_at is None on
    gallery rows written before db migration 002 added the column; those
    sort below anything timestamped rather than being dropped, so a
    regenerated image beats an undated one."""
    return (0, "") if item.created_at is None else (1, item.created_at)


def _pick_dpi_variant(
    face_items: list[FaceResult],
    preferred_dpi: int | None,
    preferred_model: str | None = None,
) -> tuple[FaceResult | None, bool]:
    """Pick the source image for a face-group.

    With `preferred_dpi` set, only that DPI is eligible — a face with no
    image at it returns (None, True) and the caller drops it from the print
    run and reports it, rather than silently substituting a different DPI.
    Mixing resolutions across one sheet isn't a useful default: it prints
    visibly inconsistent cards and hides the fact that something never
    generated. Within the eligible set, `preferred_model` wins if present,
    otherwise the most recently produced image does.

    With `preferred_dpi` unset every variant is eligible, and the highest
    DPI wins (ties broken by recency) — "give me the best I have".

    A Custom Image is the one exception, and deliberately so. The hard
    filter exists to stop one *generated* card silently printing at a
    different resolution from its neighbours — the user asked for 1200 DPI
    and every Scryfall card can be regenerated at 1200 DPI, so a face
    without one means "this never generated", which is worth surfacing as
    an error. A custom front the user uploaded and chose not to upscale has
    exactly one image in existence at whatever resolution their file
    happened to be; excluding it doesn't reveal a problem, it just prints a
    hole in the sheet where the card they explicitly supplied should be.
    So for a custom face the preference degrades to "best available" and
    the unit is flagged dpi_fallback instead of dropped.

    Returns (chosen, unavailable_at_preferred_dpi).
    """
    if preferred_dpi is not None:
        at_dpi = [item for item in face_items if item.dpi == preferred_dpi]
        if not at_dpi:
            if face_items and face_items[0].is_custom:
                return max(face_items, key=lambda x: (x.dpi, _recency_key(x))), True
            return None, True
        if preferred_model is not None:
            matching = [item for item in at_dpi if item.model == preferred_model]
            if matching:
                return max(matching, key=_recency_key), False
        return max(at_dpi, key=_recency_key), False

    if preferred_model is not None:
        matching = [item for item in face_items if item.model == preferred_model]
        if matching:
            return max(matching, key=lambda x: (x.dpi, _recency_key(x))), False
    return max(face_items, key=lambda x: (x.dpi, _recency_key(x))), False


def match_quantities(
    entries: list[DeckEntry],
    gallery: list[FaceResult],
    *,
    preferred_dpi: int | None = None,
    preferred_model: str | None = None,
    use_originals: bool = False,
) -> tuple[list[PrintUnit], list[str], list[str]]:
    """Match gallery face-groups to decklist quantities — decklist-driven:
    the current decklist decides what's eligible to print, not the
    project's full generation history.

    Quantity isn't persisted on FaceResult/gallery items — re-derive it from
    freshly-parsed decklist entries. For each face-group, sum quantity across
    every entry whose (set_code, collector_number) matches — applied to ALL
    matching groups, not just the first, since a single DFC decklist line
    legitimately matches two groups (front/back faces), each independently
    getting the full quantity. Falls back to name containment (mirrors
    db.py::_match_card_id's pattern) when no exact-printing match exists.

    A gallery face-group matching no current entry (e.g. a card removed
    from the decklist after it was generated) is silently excluded from the
    print run — it's not the caller's problem, and re-including it at
    quantity 1 would print cards nobody asked for any more.

    Conversely, an entry matching zero gallery face-groups genuinely has no
    image and is reported in `missing` — that's the case worth surfacing as
    an error, since it usually means the card was never generated. A
    multi-face entry (DFC/transform) that matched *some* but not all of its
    faces is reported too: whichever face group DID match carries its own
    `total_faces` (captured once at generation time from Scryfall's card
    data — see FaceResult.total_faces / db migration 003), so a front face
    generated with no back face anywhere in the gallery is visible without
    this function ever calling Scryfall itself. `total_faces=None` (rows
    predating that migration) skips this check entirely for that entry,
    matching the old lenient any-match-counts behavior.

    `preferred_dpi`, when given, is a hard filter: only images at that DPI
    are printable, and any face without one is excluded from the run and
    returned in the third element for the caller to surface as an error —
    never substituted with a different resolution. `preferred_model`, when
    given, wins among the eligible images; otherwise the most recently
    produced one does. See _pick_dpi_variant.

    Units come back ordered by the first entry each face-group matched —
    the caller's `entries` order is authoritative for print/export order
    (the client sends entries pre-sorted by its shared sort control, and
    "(none)" there is the decklist's own order). Two face-groups matching
    the same entry (a DFC's front and back faces) keep their relative
    gallery order, with lower face_index first, so a front never trails
    its own back.

    `use_originals` flips which world of variants is visible at all: True
    makes only the download-only rows (model == ORIGINAL_MODEL, the cached
    ~300 DPI Scryfall originals) eligible — callers pass preferred_dpi/
    preferred_model as None then, since there's exactly one original per
    face. False (the default) hides those rows, so a face with only a
    download reads as `missing` for an upscale print run, and originals
    can never silently mix into one via the "highest available" pick.

    Returns (units, missing, missing_at_dpi).
    """
    def _eligible(item: FaceResult) -> bool:
        if item.is_custom:
            # Custom Images live outside the originals/upscales split.
            # Their uploaded source (CUSTOM_SOURCE_MODEL) is always
            # printable, because unlike a Scryfall card there is no
            # download world to fall back to — it is the only image that
            # exists until the user upscales it. Their upscales then behave
            # like any other upscale and drop out when the user asks for
            # sources only.
            return item.model == CUSTOM_SOURCE_MODEL or not use_originals
        return (item.model == ORIGINAL_MODEL) == use_originals

    gallery = [item for item in gallery if _eligible(item)]
    units: list[PrintUnit] = []
    # Parallel to `units`: (first matched entry index, face_index) per
    # unit, for the entries-order sort at the end.
    unit_order_keys: list[tuple[int, int]] = []
    missing_at_dpi: list[str] = []
    matched_face_counts = [0] * len(entries)
    # How many faces each entry's card actually has, learned from whichever
    # matched gallery group(s) happen to know it (see FaceResult.total_faces
    # / db migration 003) — None until a matched group with a known value is
    # seen, same "unknown, don't verify" meaning as on the row itself.
    expected_faces_by_entry: list[int | None] = [None] * len(entries)

    for key, face_items in group_by_face(gallery):
        rep = face_items[0]
        set_code = (rep.set_code or "").lower() or None
        collector = rep.collector_number
        card_name = (rep.card_name or rep.face_name or "").casefold()

        matched_qty = 0
        matched_indices: set[int] = set()
        if rep.is_custom:
            # Matched on content hash alone, and never by name: two
            # uploads can easily share a filename, and a custom front
            # named "Sol Ring" must not soak up the quantity of a real
            # Sol Ring line (or vice versa).
            for i, entry in enumerate(entries):
                if entry.custom_hash == rep.custom_hash:
                    matched_qty += entry.quantity
                    matched_indices.add(i)
        elif set_code and collector:
            rep_lang = (rep.lang or "en").lower()
            for i, entry in enumerate(entries):
                if (
                    entry.set_code == set_code
                    and entry.collector_number == str(collector)
                    # Language completes the printing identity — an
                    # Italian entry must not print the English image of
                    # the same set/collector. Absent lang reads as "en"
                    # on both sides, so pre-language decks are unchanged.
                    and (entry.lang or "en").lower() == rep_lang
                ):
                    matched_qty += entry.quantity
                    matched_indices.add(i)
        if not matched_indices and card_name and not rep.is_custom:
            for i, entry in enumerate(entries):
                if entry.is_custom or entry.has_exact_printing:
                    # Already fully accounted for by the exact-match branch
                    # above (for its own face-group) — an entry pinned to
                    # one printing must not also count toward a
                    # different printing that merely shares its name (e.g.
                    # "Sol Ring (c21) 263" vs a separate name-only
                    # "Sol Ring" line resolving to a different printing).
                    continue
                ename = entry.name.casefold()
                if (
                    card_name == ename
                    or card_name in ename
                    or ename.split(" // ")[0] == card_name
                ):
                    matched_qty += entry.quantity
                    matched_indices.add(i)

        if not matched_indices:
            # No current decklist entry wants this printing any more (e.g.
            # a card removed, or repointed to a different printing after
            # generating this one) — silently excluded from the print run,
            # not reported. Matching runs BEFORE the preferred-DPI filter
            # on purpose: an unwanted group that also lacks the requested
            # DPI is still just unwanted, and reporting it would surface
            # phantom "missing at DPI" errors for printings the deck no
            # longer references.
            continue

        for i in matched_indices:
            # Counted regardless of the DPI filter below: an image for
            # this entry exists, so it must not ALSO be reported as "never
            # generated" — a group excluded at the requested DPI gets its
            # own, more precise report instead.
            matched_face_counts[i] += 1
            if rep.total_faces is not None:
                expected_faces_by_entry[i] = rep.total_faces

        best, unavailable = _pick_dpi_variant(face_items, preferred_dpi, preferred_model)
        if best is None:
            # No image at the requested DPI. Reported to the caller and left
            # out of the print run entirely — never substituted with another
            # resolution (see _pick_dpi_variant).
            missing_at_dpi.append(_describe_face(rep))
            continue

        units.append(
            PrintUnit(face_key=key, quantity=matched_qty, best=best, dpi_fallback=unavailable)
        )
        unit_order_keys.append((min(matched_indices), best.face_index or 0))

    # Entries order is authoritative for output order (see docstring).
    # Stable, so ties beyond the (entry, face_index) key keep gallery
    # order (dpi ASC, face_index ASC from db.list_gallery_items).
    units = [units[i] for i in sorted(range(len(units)), key=lambda i: unit_order_keys[i])]

    missing: list[str] = []
    for i, entry in enumerate(entries):
        matched = matched_face_counts[i]
        expected = expected_faces_by_entry[i]
        if matched == 0 or (expected is not None and matched < expected):
            missing.append(_describe_entry(entry, matched, expected))

    return units, missing, missing_at_dpi


@dataclass(frozen=True)
class PrintSlot:
    """One physical printed card: a front, and what goes on its Reverse.

    `reverse` is the Back Face image when this card's transform side is
    being printed on its own back, and None when the Reverse should take
    the project's Back Image instead. None does NOT mean "nothing gets
    printed there" — an empty cell on a partial page is represented by the
    absence of a PrintSlot, not by a PrintSlot with no reverse.
    """

    front: FaceResult
    reverse: FaceResult | None = None


def _pairable_faces(units: list[PrintUnit]) -> dict[str, tuple[PrintUnit, PrintUnit]]:
    """Find the (front, back) unit pairs eligible to share one card.

    Keyed on scryfall_id, which already identifies a printing *including*
    its language — an Italian and an English copy of the same set/collector
    are different ids, so they can never pair with each other by accident.

    A card whose faces don't form a clean 0/1 pair is excluded entirely:
    every one of its faces stays its own card. Scryfall doesn't currently
    produce a printing with three printable faces, so this is a guard
    rather than a feature — and guessing which of three faces is "the back"
    would be wrong more often than useful. Excluding falls back to exactly
    today's behaviour, which is a safe answer rather than a broken one.
    """
    by_id: dict[str, dict[int | None, PrintUnit]] = {}
    for unit in units:
        by_id.setdefault(unit.best.scryfall_id, {})[unit.best.face_index] = unit

    pairs: dict[str, tuple[PrintUnit, PrintUnit]] = {}
    for scryfall_id, faces in by_id.items():
        if len(faces) != 2:
            continue
        front, back = faces.get(0), faces.get(1)
        if front is None or back is None:
            continue
        pairs[scryfall_id] = (front, back)
    return pairs


def build_print_slots(units: list[PrintUnit], *, pair_back_faces: bool) -> list[PrintSlot]:
    """Flatten matched units into one PrintSlot per physical printed card.

    With `pair_back_faces` off this is the historical behaviour: every face
    is its own card, and every Reverse takes the Back Image. With it on, a
    double-faced card's two faces collapse into ONE card carrying both — so
    the same decklist produces roughly half as many print slots, and the
    page count changes with it. That is why the setting lives next to the
    page count it changes.

    The paired slot takes the front's position in the ordering and the
    front's quantity; the back's own unit is consumed. Quantities are
    re-derived per face group from the same decklist entries, so the two
    agree except in the degenerate case where only one face matched an
    entry — and there the front is the one that decides how many cards
    exist to have backs at all.
    """
    pairs = _pairable_faces(units) if pair_back_faces else {}
    consumed_backs = {id(back) for _front, back in pairs.values()}

    slots: list[PrintSlot] = []
    for unit in units:
        if id(unit) in consumed_backs:
            continue
        pair = pairs.get(unit.best.scryfall_id)
        reverse = pair[1].best if pair is not None and pair[0] is unit else None
        slots.extend([PrintSlot(front=unit.best, reverse=reverse)] * unit.quantity)
    return slots


def paginate(slots: list[PrintSlot], per_page: int) -> list[list[PrintSlot]]:
    """Chunk into per-page lists; last page may be shorter."""
    if per_page <= 0:
        return [slots] if slots else []
    return [slots[i : i + per_page] for i in range(0, len(slots), per_page)]


def _mirrors_columns(layout: PageLayout, flip_edge: FlipEdge) -> bool:
    """Does the sheet turn about a VERTICAL axis?

    True means columns reverse and "up" is preserved; False means rows
    reverse and up/down inverts. Every other back-page decision follows
    from this one boolean, so it lives in one place rather than being
    re-derived (and eventually re-derived differently) per caller.

    "Long edge" names a physical edge of the paper, not a direction: a
    portrait sheet's long edges are its sides, so flipping on them turns
    the sheet about a vertical axis; a landscape sheet's long edges are
    top and bottom, so the same setting turns it about a horizontal one.
    """
    return (flip_edge is FlipEdge.LONG) == (layout.orientation == "portrait")


def back_pages_are_rotated(layout: PageLayout, flip_edge: FlipEdge) -> bool:
    """Must Back Page images be drawn upside down?

    Yes exactly when the sheet turns about a HORIZONTAL axis — a portrait
    sheet flipped on its short edge, or a landscape sheet flipped on its
    long one.

    Why: turning a sheet about a horizontal axis inverts up/down, so the
    front and back of any one card end up with opposite "up" directions in
    the paper. Cut that card out and turn it over the way you actually
    hold a card — about its own vertical axis, which preserves up — and a
    back drawn the right way up on the sheet reads upside down in your
    hand. Drawing it rotated 180° cancels the sheet's inversion.

    Turning about a vertical axis preserves up, so nothing is rotated
    there. That is the common case (portrait paper, long-edge duplex) and
    is why this correction is easy to miss.
    """
    return not _mirrors_columns(layout, flip_edge)


def mirror_page_index(index: int, *, layout: PageLayout, flip_edge: FlipEdge) -> int:
    """Where a front's slot index lands on the Back Page.

    Mirrored across the axis the sheet physically turns about — columns
    reverse for a portrait sheet flipped on its long edge, rows reverse for
    a landscape one (see FlipEdge). Always computed over the FULL grid, so
    a partial last page mirrors into the positions its complete grid would
    give: four cards on a nine-slot page occupy mirrored cells and leave
    five empty. Mirroring over just the four filled cells is the classic
    duplex bug — it lines the backs up against the wrong fronts.

    Position is only half of it: when rows are the mirrored axis the
    images must also be drawn upside down. See back_pages_are_rotated.
    """
    row, col = divmod(index, layout.cols)
    if _mirrors_columns(layout, flip_edge):
        col = layout.cols - 1 - col
    else:
        row = layout.rows - 1 - row
    return row * layout.cols + col


def back_page_cells(
    page_slots: list[PrintSlot], *, layout: PageLayout, flip_edge: FlipEdge
) -> list[PrintSlot | None]:
    """The Back Page for one Front Page: a full-grid list of cells, each
    holding the PrintSlot whose Reverse belongs there, or None for a grid
    position no card occupies."""
    cells: list[PrintSlot | None] = [None] * layout.cards_per_page
    for i, slot in enumerate(page_slots):
        cells[mirror_page_index(i, layout=layout, flip_edge=flip_edge)] = slot
    return cells


def unique_image_count(
    pages: list[list[PrintSlot]],
    *,
    back_printing: bool = False,
    back_image_path: Path | None = None,
    reverse_fill: ReverseFill = ReverseFill.BACK_IMAGE,
) -> int:
    """How many source images build_pdf will actually process.

    The per-image work is cached per source path, so this — not the number
    of print slots — is the real unit of progress: a card printed eight
    times costs one decode/resize/bleed/encode and seven near-free
    placements. Exposed so a caller can size a progress bar before starting
    the build.

    With back printing on, Back Face images count too (they are distinct
    images with their own cost), and the Back Image counts once no matter
    how many Reverses it fills.
    """
    paths: set[Path] = {slot.front.out_path for page in pages for slot in page}
    if back_printing:
        paths |= {
            slot.reverse.out_path
            for page in pages
            for slot in page
            if slot.reverse is not None
        }
        if (
            reverse_fill is ReverseFill.BACK_IMAGE
            and back_image_path is not None
            and any(slot.reverse is None for page in pages for slot in page)
        ):
            paths.add(back_image_path)
    return len(paths)


def fit_cover(image: Image.Image, target: tuple[int, int]) -> Image.Image:
    """Scale to fill `target` and centre-crop the overflow, preserving
    aspect ratio.

    Card art from Scryfall is already 63:88 and needs none of this, but a
    Back Image is whatever file the user picked. _resize_to_dpi would
    stretch a square logo into a distorted rectangle; cover-cropping
    distorts nothing and loses only the edges, which is the right trade for
    art that is going to be printed full-bleed and then cut anyway.
    """
    tw, th = target
    w, h = image.size
    if (w, h) == (tw, th):
        return image
    scale = max(tw / w, th / h)
    scaled = image.resize((max(1, round(w * scale)), max(1, round(h * scale))), Image.Resampling.LANCZOS)
    sw, sh = scaled.size
    left = (sw - tw) // 2
    top = (sh - th) // 2
    return scaled.crop((left, top, left + tw, top + th))


# Kept as a name: tests and the export router import it from here.
_bled_pixels = bled_target_pixels


def fit_bled_image(
    image: Image.Image,
    *,
    image_bleed_mm: float,
    export_dpi: float,
    bleed_mm: float,
) -> Image.Image:
    """Prepare a file that already carries `image_bleed_mm` of bleed per
    side as the bled card the sheet wants, with `bleed_mm` per side.

    The file's own bleed is used up to the requested amount: the surplus
    is cropped away proportionally (a 3.175 mm MPC file printed at 1 mm
    keeps 1 mm of its border and its trim content stays exactly card
    sized), and if the sheet asks for more than the file has, the
    difference is edge-extended from the file's outer pixels with square
    corners — a pre-bled file has no transparent arcs. `bleed_mm=0`
    yields the trim-sized card.

    Exactly what fitting the whole file to the bled box gets wrong: that
    scales the trim content by (63+2b)/(63+2i) whenever the two bleeds
    differ.
    """
    img = image.convert("RGB")
    keep = max(0.0, min(image_bleed_mm, bleed_mm))
    w, h = img.size
    scale_x = w / (CARD_WIDTH_MM + 2 * image_bleed_mm)
    scale_y = h / (CARD_HEIGHT_MM + 2 * image_bleed_mm)
    surplus = image_bleed_mm - keep
    cx = round(surplus * scale_x)
    cy = round(surplus * scale_y)
    if cx or cy:
        img = img.crop((cx, cy, w - cx, h - cy))
    img = fit_cover(img, bled_target_pixels(export_dpi, keep))
    if bleed_mm - keep > 1e-6:
        img = add_bleed(img, dpi=export_dpi, bleed_mm=bleed_mm - keep, radius_px=0)
    target = bled_target_pixels(export_dpi, bleed_mm)
    if img.size != target:
        # add_bleed rounds its own pixel count; absorb the ±1px so the
        # result is exactly the bled box the sheet lays out.
        img = fit_cover(img, target)
    return img


def render_back_image(
    path: Path,
    *,
    export_dpi: int,
    bleed_mm: float,
    includes_bleed: bool,
    image_bleed_mm: float | None = None,
) -> Image.Image:
    """Prepare a Back Image for placement, as an opaque bled RGB image the
    same size add_bleed would produce for a card.

    Per the user's declaration about their own file. When the art does
    NOT include bleed, it is cover-fitted to the trim size and then
    edge-extended exactly like card art. When it DOES and the client said
    how much (`image_bleed_mm`), fit_bled_image trims that bleed to the
    sheet's or tops it up, so the trim content stays card sized. A
    declaration with no amount (clients before 0.3.3) keeps the original
    behaviour: the whole file cover-fitted to the bled box.
    """
    with Image.open(path) as raw:
        img = raw.convert("RGB")
        if includes_bleed and image_bleed_mm is not None:
            return fit_bled_image(
                img, image_bleed_mm=image_bleed_mm, export_dpi=export_dpi, bleed_mm=bleed_mm
            )
        if includes_bleed:
            return fit_cover(img, _bled_pixels(export_dpi, bleed_mm))
        trimmed = fit_cover(img, target_pixels(export_dpi))
    return add_bleed(trimmed, dpi=export_dpi, bleed_mm=bleed_mm)


def _bled_card(face: FaceResult, *, export_dpi: int, bleed_mm: float) -> Image.Image:
    """One card image, corner-flattened, resized and bled, ready for
    placement. Encoding is the caller's job — it also owns the optional
    180° rotation, and rotating after encoding would mean a decode."""
    if face.custom_bleed_mm > 0:
        # A Custom Image declared to carry bleed: its file (and every
        # upscaled variant) is the bled box, square-cornered and opaque, so
        # there is no arc to flatten — trim its bleed to the sheet's.
        with Image.open(face.out_path) as raw:
            return fit_bled_image(
                raw.convert("RGB"),
                image_bleed_mm=face.custom_bleed_mm,
                export_dpi=export_dpi,
                bleed_mm=bleed_mm,
            )
    with Image.open(face.out_path) as raw:
        rgba = raw.convert("RGBA")
        # Measured before flattening: once the corners are opaque the alpha
        # no longer carries the arc, and add_bleed() needs the radius to fan
        # the bleed out of the corners rather than clamp to a square one.
        radius = corner_radius_px(rgba)
        # Flatten the rounded-corner alpha to opaque BEFORE any resize —
        # resizing while corners are still transparent lets the resample
        # filter (LANCZOS) blend the transparent region's RGB into the
        # opaque body right at the boundary (classic alpha fringing),
        # baking in a visible smear at every corner.
        img = flatten_corner_alpha(rgba)
        native_width = rgba.width
    if export_dpi != face.dpi:
        img = _resize_to_dpi(img, export_dpi)
        radius = round(radius * img.width / native_width)
    return add_bleed(img, dpi=export_dpi, bleed_mm=bleed_mm, radius_px=radius)


def build_pdf(
    pages: list[list[PrintSlot]],
    *,
    layout: PageLayout,
    export_dpi: int,
    guides: GuideVisibility | None = None,
    back_printing: bool = False,
    back_layout: PageLayout | None = None,
    back_image_path: Path | None = None,
    back_image_includes_bleed: bool = False,
    back_image_bleed_mm: float | None = None,
    reverse_fill: ReverseFill = ReverseFill.BACK_IMAGE,
    flip_edge: FlipEdge = FlipEdge.LONG,
    page_order: PageOrder = PageOrder.DUPLEX,
    registration: RegistrationMarks | None = None,
    on_progress: Callable[[int, int], None] | None = None,
) -> bytes:
    """Render pages of PrintSlots into a print-ready PDF, in memory.

    Caches the bled+JPEG-encoded bytes per unique source path so a card
    printed N times only pays the resize+corner-flatten+bleed+encode cost
    once. `export_dpi` is one value for the whole PDF (not per-card) — when
    it differs from a source image's own native `face.dpi`, that image is
    resized to `export_dpi`'s pixel density before bleed is added, so e.g.
    a 1200 DPI generated source can still be exported into an 800 DPI PDF
    for a smaller file.

    With `back_printing` on, each Front Page is followed (DUPLEX) or
    trailed (FRONTS_THEN_BACKS) by its Back Page: the same grid mirrored
    across the sheet's flip axis, each cell carrying either that card's
    Back Face or the Back Image. `back_layout` carries the Back Pages' own
    position offsets — duplex registration drifts, and calibrating it is
    the whole reason those offsets exist — and defaults to the front
    layout when not given. Guides are drawn per page kind (see
    GuideVisibility).

    `registration` adds cutter registration marks (see RegistrationMarks),
    again per page kind. They depend on the page size alone, so the front
    and back offsets never move them and a Back Page's marks are never
    mirrored — the cutter reads whichever side is face up, in that side's
    own frame. Page Guides on a page that carries marks are clipped out of
    the marks' keep-out zones.

    `on_progress(completed, total)` fires once per *unique* image, right
    after that image's expensive work lands in the cache — see
    unique_image_count for why that's the honest unit. It may raise to
    abort the build (pdf_jobs.PdfRenderCanceled does exactly that for a
    user-requested cancel); nothing here catches it, so the exception
    unwinds to the caller with no partial PDF produced.
    """
    guides = guides or GuideVisibility()
    back_layout = back_layout or layout
    # One place to collapse "blank" into "no image to draw", so the
    # drawing code below has a single condition rather than two that can
    # disagree.
    if reverse_fill is ReverseFill.BLANK:
        back_image_path = None

    # Pre-oriented tuple — always pass orientation="portrait" to fpdf2
    # here, since FPDF._set_orientation() swaps w_pt/h_pt for any
    # non-portrait orientation even with a tuple format, which would
    # silently undo our own already-correct page_w_mm/page_h_mm ordering.
    pdf = FPDF(orientation="portrait", unit="mm", format=(layout.page_w_mm, layout.page_h_mm))
    pdf.set_auto_page_break(False)
    pdf.set_margins(0, 0, 0)

    cache: dict[tuple[Path, bool], bytes] = {}
    total_images = unique_image_count(
        pages, back_printing=back_printing, back_image_path=back_image_path
    )

    def encoded(source: Path, face: FaceResult | None, *, rotate: bool = False) -> bytes:
        """Cached bled JPEG bytes for one source image. `face` is None for
        the Back Image, which has no generated-image record to carry a
        native DPI and takes the cover-fit path instead.

        `rotate` draws the image upside down, for Back Pages whose sheet
        turns about a horizontal axis (see back_pages_are_rotated). It is
        part of the cache key, not just the render: the same file can
        legitimately be needed both ways in one document, and keying on
        the path alone would serve whichever orientation happened to be
        encoded first.
        """
        key = (source, rotate)
        cached = cache.get(key)
        if cached is not None:
            return cached
        if face is not None:
            image = _bled_card(face, export_dpi=export_dpi, bleed_mm=layout.bleed_mm)
        else:
            image = render_back_image(
                source,
                export_dpi=export_dpi,
                bleed_mm=layout.bleed_mm,
                includes_bleed=back_image_includes_bleed,
                image_bleed_mm=back_image_bleed_mm,
            )
        if rotate:
            image = image.transpose(Image.Transpose.ROTATE_180)
        buf = io.BytesIO()
        image.save(buf, format="JPEG", quality=_JPEG_QUALITY)
        data = buf.getvalue()
        cache[key] = data
        if on_progress is not None:
            on_progress(len(cache), total_images)
        return data

    # Whether Back Page images are drawn upside down. Derived from the
    # front layout: it is a property of how the sheet turns over, and the
    # back layout differs only by its position offsets.
    rotate_backs = back_pages_are_rotated(layout, flip_edge)

    def draw_page(cells: list[PrintSlot | None], *, is_back: bool) -> None:
        page_layout = back_layout if is_back else layout
        pdf.add_page()
        for idx, slot in enumerate(cells):
            if slot is None:
                continue
            if is_back:
                face = slot.reverse
                source = face.out_path if face is not None else back_image_path
                if source is None:
                    # Nothing to print on this Reverse. Either deliberate
                    # (reverse_fill=BLANK — a card with no transform side
                    # gets an empty back), or the missing-Back-Image case
                    # the router refuses up front. Both draw nothing; the
                    # page itself is still emitted, which is what keeps
                    # the sheet in register.
                    continue
            else:
                face = slot.front
                source = face.out_path

            col, row = idx % page_layout.cols, idx // page_layout.cols
            x = page_layout.margin_x_mm + col * page_layout.cell_w_mm
            y = page_layout.margin_y_mm + row * page_layout.cell_h_mm
            # Fresh BytesIO per call — fpdf2's fast DCTDecode passthrough
            # path re-reads from position 0 each time, but a shared object
            # across repeated copies of the same card is an easy footgun
            # to avoid entirely by just re-wrapping the cached immutable
            # bytes.
            pdf.image(
                io.BytesIO(encoded(source, face, rotate=is_back and rotate_backs)),
                x=x,
                y=y,
                w=page_layout.bled_card_w_mm,
                h=page_layout.bled_card_h_mm,
            )

        draw_marks = registration is not None and registration.drawn_on(is_back=is_back)
        keep_out = (
            registration_keep_out(page_layout.page_w_mm, page_layout.page_h_mm, registration)
            if registration is not None and draw_marks
            else ()
        )
        if is_back:
            _draw_cut_marks(
                pdf,
                page_layout,
                card_guides=not guides.hide_card_guides_back,
                page_guides=not guides.hide_page_guides_back,
                keep_out=keep_out,
            )
        else:
            _draw_cut_marks(
                pdf,
                page_layout,
                card_guides=not guides.hide_card_guides_front,
                page_guides=not guides.hide_page_guides_front,
                keep_out=keep_out,
            )
        if registration is not None and draw_marks:
            _draw_registration_marks(
                pdf, page_layout.page_w_mm, page_layout.page_h_mm, registration
            )

    def front_cells(page_slots: list[PrintSlot]) -> list[PrintSlot | None]:
        padded: list[PrintSlot | None] = list(page_slots)
        padded += [None] * (layout.cards_per_page - len(padded))
        return padded

    if not back_printing:
        for page_slots in pages:
            draw_page(front_cells(page_slots), is_back=False)
    elif page_order is PageOrder.FRONTS_THEN_BACKS:
        for page_slots in pages:
            draw_page(front_cells(page_slots), is_back=False)
        for page_slots in pages:
            draw_page(
                back_page_cells(page_slots, layout=layout, flip_edge=flip_edge), is_back=True
            )
    else:
        for page_slots in pages:
            draw_page(front_cells(page_slots), is_back=False)
            draw_page(
                back_page_cells(page_slots, layout=layout, flip_edge=flip_edge), is_back=True
            )

    return bytes(pdf.output())
