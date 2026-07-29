"""Print-sheet PDF layout: bleed extension, cut guides, grid pagination.

Pure logic, no Streamlit dependency — mirrors the pipeline.py/decklist.py
separation used elsewhere in this codebase.
"""

from __future__ import annotations

import io
from dataclasses import dataclass
from pathlib import Path

from fpdf import FPDF
from PIL import Image

from .decklist import DeckEntry
from .pipeline import FaceResult, _resize_to_dpi, group_by_face

MM_PER_IN = 25.4

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

CARD_WIDTH_MM = 63.5
CARD_HEIGHT_MM = 88.9

# Portrait-native (width, height) — a starting point for the UI's page-size
# preset dropdown; actual page dimensions are freely user-editable from here.
PAGE_SIZE_PRESETS_MM: dict[str, tuple[float, float]] = {
    "letter": (215.9, 279.4),
    "a4": (210.0, 297.0),
}

# Outer guide lines run from the page edge to the card grid block — full,
# dark, and continuous (there's no card content there to obscure, and a
# continuous line is what you actually align a paper cutter against).
_OUTER_LINE_WIDTH_MM = 0.1
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


def _draw_cut_marks(pdf: FPDF, layout: PageLayout) -> None:
    """Black lines from the page edge to the grid block (outer margins,
    nothing to obscure there) — two closely-spaced lines per interior gap,
    one per card's own trim edge; small green "+" crop marks at each card's
    own trim-corner (the grid is regular, so the full xs × ys cross product
    is exactly every card's 4 corners, no card ever gets a mark that isn't
    its own)."""
    xs = _card_trim_edges(layout.cols, layout.cell_w_mm, layout.bleed_mm, layout.margin_x_mm)
    ys = _card_trim_edges(layout.rows, layout.cell_h_mm, layout.bleed_mm, layout.margin_y_mm)
    grid_x0, grid_x1 = xs[0], xs[-1]
    grid_y0, grid_y1 = ys[0], ys[-1]

    pdf.set_line_width(_OUTER_LINE_WIDTH_MM)
    pdf.set_draw_color(*_OUTER_LINE_COLOR)
    for x in xs:
        if grid_y0 > 0:
            pdf.line(x, 0, x, grid_y0)
        if grid_y1 < layout.page_h_mm:
            pdf.line(x, grid_y1, x, layout.page_h_mm)
    for y in ys:
        if grid_x0 > 0:
            pdf.line(0, y, grid_x0, y)
        if grid_x1 < layout.page_w_mm:
            pdf.line(grid_x1, y, layout.page_w_mm, y)

    mark_width_mm = layout.guide_width_pt / 72 * MM_PER_IN
    pdf.set_line_width(mark_width_mm)
    pdf.set_draw_color(*_MARK_COLOR)
    for x in xs:
        for y in ys:
            pdf.line(x - layout.guide_length_mm, y, x + layout.guide_length_mm, y)
            pdf.line(x, y - layout.guide_length_mm, x, y + layout.guide_length_mm)


def _replicate_top_left_corner(img: Image.Image, r: int) -> None:
    """Mutates `img` in place: fills the (0,0)-(r,r) square by stretching
    the opaque column at x=r horizontally across each of the first r rows."""
    for y in range(r):
        src = img.crop((r, y, r + 1, y + 1))
        row_fill = src.resize((r, 1), Image.Resampling.NEAREST)
        img.paste(row_fill, (0, y))


def flatten_corner_alpha(
    image: Image.Image,
    *,
    probe_frac: float = 0.12,
    alpha_threshold: int = 250,
) -> Image.Image:
    """Flatten the 4 small rounded-corner alpha arcs to fully opaque.

    Physical proxy printing prints a full opaque rectangle then rounds the
    physical paper corners afterward with a punch tool — so the print should
    have zero transparent/unprinted regions. For each corner: crop a small
    probe square, reorient it to the canonical top-left case via a
    self-inverse transpose, measure the transparent bbox from the alpha
    channel, replicate-fill it, transpose back, and paste in place.
    """
    img = image.convert("RGBA")
    w, h = img.size
    probe = max(4, round(min(w, h) * probe_frac))

    corners = [
        (None, (0, 0)),
        (Image.Transpose.FLIP_LEFT_RIGHT, (w - probe, 0)),
        (Image.Transpose.FLIP_TOP_BOTTOM, (0, h - probe)),
        (Image.Transpose.ROTATE_180, (w - probe, h - probe)),
    ]
    for transpose_op, (px, py) in corners:
        patch = img.crop((px, py, px + probe, py + probe))
        # transpose_op may be Image.Transpose.FLIP_LEFT_RIGHT, whose IntEnum
        # value is 0 (falsy) — must check "is not None", not truthiness, or
        # that specific transpose silently gets skipped.
        oriented = patch.transpose(transpose_op) if transpose_op is not None else patch
        alpha = oriented.getchannel("A")
        mask = alpha.point(lambda p: 255 if p < alpha_threshold else 0)
        bbox = mask.getbbox()
        if bbox is None:
            continue
        r = min(probe - 1, max(bbox[2], bbox[3]) + 1)
        if r <= 0:
            continue
        _replicate_top_left_corner(oriented, r)
        result = oriented.transpose(transpose_op) if transpose_op is not None else oriented
        img.paste(result, (px, py))
    return img


def add_bleed(image: Image.Image, *, dpi: int, bleed_mm: float = BLEED_MM) -> Image.Image:
    """Corner-flatten, then edge-extend a bleed border on all sides.

    Returns an opaque RGB image (alpha dropped — safe once corners are
    flattened to opaque). Uses NEAREST resampling to stretch true 1px source
    edge strips — exact clamp-to-edge replication regardless of filter.
    """
    rgb = flatten_corner_alpha(image).convert("RGB")
    w, h = rgb.size
    bleed_px = max(1, round(dpi / MM_PER_IN * bleed_mm))

    canvas = Image.new("RGB", (w + 2 * bleed_px, h + 2 * bleed_px))
    canvas.paste(rgb, (bleed_px, bleed_px))

    R = Image.Resampling.NEAREST
    top = rgb.crop((0, 0, w, 1)).resize((w, bleed_px), R)
    bottom = rgb.crop((0, h - 1, w, h)).resize((w, bleed_px), R)
    left = rgb.crop((0, 0, 1, h)).resize((bleed_px, h), R)
    right = rgb.crop((w - 1, 0, w, h)).resize((bleed_px, h), R)
    canvas.paste(top, (bleed_px, 0))
    canvas.paste(bottom, (bleed_px, bleed_px + h))
    canvas.paste(left, (0, bleed_px))
    canvas.paste(right, (bleed_px + w, bleed_px))

    tl = rgb.crop((0, 0, 1, 1)).resize((bleed_px, bleed_px), R)
    tr = rgb.crop((w - 1, 0, w, 1)).resize((bleed_px, bleed_px), R)
    bl = rgb.crop((0, h - 1, 1, h)).resize((bleed_px, bleed_px), R)
    br = rgb.crop((w - 1, h - 1, w, h)).resize((bleed_px, bleed_px), R)
    canvas.paste(tl, (0, 0))
    canvas.paste(tr, (bleed_px + w, 0))
    canvas.paste(bl, (0, bleed_px + h))
    canvas.paste(br, (bleed_px + w, bleed_px + h))
    return canvas


@dataclass
class PrintUnit:
    face_key: str
    quantity: int
    best: FaceResult  # chosen image variant for this face (see _pick_dpi_variant)
    dpi_fallback: bool = False  # True if preferred_dpi was requested but unavailable


def _pick_dpi_variant(
    face_items: list[FaceResult],
    preferred_dpi: int | None,
    preferred_model: str | None = None,
) -> tuple[FaceResult, bool]:
    """Pick the source image for a face-group: an exact (preferred_dpi,
    preferred_model) match if both are given and available, else the
    preferred DPI under any model, else the highest available DPI. Returns
    (chosen, fell_back)."""
    if preferred_dpi is not None and preferred_model is not None:
        for item in face_items:
            if item.dpi == preferred_dpi and item.model == preferred_model:
                return item, False
    if preferred_dpi is not None:
        for item in face_items:
            if item.dpi == preferred_dpi:
                return item, False
    return max(face_items, key=lambda x: x.dpi), preferred_dpi is not None


def match_quantities(
    entries: list[DeckEntry],
    gallery: list[FaceResult],
    *,
    preferred_dpi: int | None = None,
    preferred_model: str | None = None,
) -> tuple[list[PrintUnit], list[str]]:
    """Match decklist quantities to gallery face-groups.

    Quantity isn't persisted on FaceResult/gallery items — re-derive it from
    freshly-parsed decklist entries. For each face-group, sum quantity across
    every entry whose (set_code, collector_number) matches — applied to ALL
    matching groups, not just the first, since a single DFC decklist line
    legitimately matches two groups (front/back faces), each independently
    getting the full quantity. Falls back to name containment (mirrors
    db.py::_match_card_id's pattern) when no exact-printing match exists.
    Unmatched groups default to quantity=1 and are returned separately for a
    UI warning — never silently dropped.

    `preferred_dpi`, when given, selects that generated DPI variant as the
    source image per face-group; a group missing that variant falls back to
    its highest available DPI (flagged via PrintUnit.dpi_fallback).
    `preferred_model`, when also given, is used as a tiebreak so a face
    generated under multiple models picks the matching one at that DPI.
    """
    units: list[PrintUnit] = []
    unmatched: list[str] = []

    for key, face_items in group_by_face(gallery):
        rep = face_items[0]
        best, dpi_fallback = _pick_dpi_variant(face_items, preferred_dpi, preferred_model)
        set_code = (rep.set_code or "").lower() or None
        collector = rep.collector_number
        card_name = (rep.card_name or rep.face_name or "").casefold()

        matched_qty = 0
        matched = False
        if set_code and collector:
            for entry in entries:
                if entry.set_code == set_code and entry.collector_number == str(collector):
                    matched_qty += entry.quantity
                    matched = True
        if not matched and card_name:
            for entry in entries:
                if entry.has_exact_printing:
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
                    matched = True

        if matched:
            units.append(
                PrintUnit(
                    face_key=key, quantity=matched_qty, best=best, dpi_fallback=dpi_fallback
                )
            )
        else:
            unmatched.append(key)
            units.append(
                PrintUnit(face_key=key, quantity=1, best=best, dpi_fallback=dpi_fallback)
            )

    return units, unmatched


def expand_print_slots(units: list[PrintUnit]) -> list[FaceResult]:
    """Flatten to one FaceResult per physical printed card."""
    slots: list[FaceResult] = []
    for unit in units:
        slots.extend([unit.best] * unit.quantity)
    return slots


def paginate(slots: list[FaceResult], per_page: int) -> list[list[FaceResult]]:
    """Chunk into per-page lists; last page may be shorter."""
    if per_page <= 0:
        return [slots] if slots else []
    return [slots[i : i + per_page] for i in range(0, len(slots), per_page)]


def build_pdf(
    pages: list[list[FaceResult]],
    *,
    layout: PageLayout,
    export_dpi: int,
    show_cut_lines: bool = True,
) -> bytes:
    """Render pages of card slots into a print-ready PDF, in memory.

    Caches the bled+JPEG-encoded bytes per unique out_path so a card printed
    N times only pays the resize+corner-flatten+bleed+encode cost once.
    `export_dpi` is one value for the whole PDF (not per-card) — when it
    differs from a source image's own native `face.dpi`, that image is
    resized to `export_dpi`'s pixel density before bleed is added, so e.g. a
    1200 DPI generated source can still be exported into an 800 DPI PDF for
    a smaller file.
    """
    # Pre-oriented tuple — always pass orientation="portrait" to fpdf2 here,
    # since FPDF._set_orientation() swaps w_pt/h_pt for any non-portrait
    # orientation even with a tuple format, which would silently undo our
    # own already-correct page_w_mm/page_h_mm ordering.
    pdf = FPDF(orientation="portrait", unit="mm", format=(layout.page_w_mm, layout.page_h_mm))
    pdf.set_auto_page_break(False)
    pdf.set_margins(0, 0, 0)

    cache: dict[Path, bytes] = {}
    for page_slots in pages:
        pdf.add_page()
        for idx, face in enumerate(page_slots):
            col, row = idx % layout.cols, idx // layout.cols
            x = layout.margin_x_mm + col * layout.cell_w_mm
            y = layout.margin_y_mm + row * layout.cell_h_mm
            jpeg_bytes = cache.get(face.out_path)
            if jpeg_bytes is None:
                with Image.open(face.out_path) as raw:
                    # Flatten the rounded-corner alpha to opaque BEFORE any
                    # resize — resizing while corners are still transparent
                    # lets the resample filter (LANCZOS) blend the
                    # transparent region's RGB into the opaque body right
                    # at the boundary (classic alpha fringing), baking in a
                    # visible smear at every corner. add_bleed() below also
                    # flattens internally, but that's now a safe no-op
                    # (already-opaque corners have nothing left to flatten).
                    img = flatten_corner_alpha(raw.convert("RGBA"))
                if export_dpi != face.dpi:
                    img = _resize_to_dpi(img, export_dpi)
                bled = add_bleed(img, dpi=export_dpi, bleed_mm=layout.bleed_mm)
                buf = io.BytesIO()
                bled.save(buf, format="JPEG", quality=_JPEG_QUALITY)
                jpeg_bytes = buf.getvalue()
                cache[face.out_path] = jpeg_bytes
            # Fresh BytesIO per call — fpdf2's fast DCTDecode passthrough
            # path re-reads from position 0 each time, but a shared object
            # across repeated copies of the same card is an easy footgun to
            # avoid entirely by just re-wrapping the cached immutable bytes.
            pdf.image(
                io.BytesIO(jpeg_bytes),
                x=x,
                y=y,
                w=layout.bled_card_w_mm,
                h=layout.bled_card_h_mm,
            )

        if show_cut_lines:
            _draw_cut_marks(pdf, layout)

    return bytes(pdf.output())
