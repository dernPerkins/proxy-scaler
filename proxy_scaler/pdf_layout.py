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
from .pipeline import FaceResult, group_by_face

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
BLED_WIDTH_MM = CARD_WIDTH_MM + 2 * BLEED_MM
BLED_HEIGHT_MM = CARD_HEIGHT_MM + 2 * BLEED_MM

# Portrait-native (width, height); orientation swaps these as needed.
PAGE_SIZES_MM: dict[str, tuple[float, float]] = {
    "letter": (215.9, 279.4),
    "a4": (210.0, 297.0),
}
GRID_LAYOUTS: dict[str, tuple[int, int]] = {
    "landscape": (4, 2),
    "portrait": (3, 3),
}

# Outer guide lines run from the page edge to the card grid block — full,
# dark, and continuous (there's no card content there to obscure, and a
# continuous line is what you actually align a paper cutter against).
_OUTER_LINE_WIDTH_MM = 0.1
_OUTER_LINE_COLOR = (0, 0, 0)

# Inner crop marks sit at each grid intersection — short green arms pointing
# only into the grid (never into the margin, where the outer line already
# marks the cut), so they don't draw a distracting line across card art.
_MARK_LENGTH_MM = 2.75
_MARK_WIDTH_MM = 1.0
_MARK_COLOR = (0, 170, 80)


@dataclass(frozen=True)
class PageLayout:
    orientation: str
    paper: str
    cols: int
    rows: int
    page_w_mm: float
    page_h_mm: float
    margin_x_mm: float
    margin_y_mm: float
    cards_per_page: int


def resolve_page_layout(*, orientation: str, paper: str) -> PageLayout:
    orientation = orientation.lower()
    paper = paper.lower()
    if orientation not in GRID_LAYOUTS:
        raise ValueError(f"Unknown orientation {orientation!r}")
    if paper not in PAGE_SIZES_MM:
        raise ValueError(f"Unknown paper size {paper!r}")

    cols, rows = GRID_LAYOUTS[orientation]
    base_w, base_h = PAGE_SIZES_MM[paper]
    page_w, page_h = (base_h, base_w) if orientation == "landscape" else (base_w, base_h)

    grid_w = cols * BLED_WIDTH_MM
    grid_h = rows * BLED_HEIGHT_MM
    if grid_w > page_w or grid_h > page_h:
        raise ValueError(f"{cols}x{rows} grid does not fit {paper} {orientation}")

    return PageLayout(
        orientation=orientation,
        paper=paper,
        cols=cols,
        rows=rows,
        page_w_mm=page_w,
        page_h_mm=page_h,
        margin_x_mm=(page_w - grid_w) / 2,
        margin_y_mm=(page_h - grid_h) / 2,
        cards_per_page=cols * rows,
    )


def _cut_positions(
    count: int, cell_mm: float, bleed_mm: float, origin_mm: float
) -> list[float]:
    """count+1 line coordinates along one axis: inset by bleed_mm at the two
    outer edges (so the outermost cards' bleed actually gets trimmed), exactly
    on the cell boundary everywhere in between (bisecting two neighbors'
    combined bleed symmetrically)."""
    positions = []
    for i in range(count + 1):
        if i == 0:
            positions.append(origin_mm + bleed_mm)
        elif i == count:
            positions.append(origin_mm + count * cell_mm - bleed_mm)
        else:
            positions.append(origin_mm + i * cell_mm)
    return positions


def _draw_cut_marks(pdf: FPDF, layout: PageLayout) -> None:
    """Black full lines from the page edge to the grid block (outer
    margins, nothing to obscure there); short green corner marks at each
    grid intersection inside the block (avoids drawing a solid line across
    printed card art)."""
    xs = _cut_positions(layout.cols, BLED_WIDTH_MM, BLEED_MM, layout.margin_x_mm)
    ys = _cut_positions(layout.rows, BLED_HEIGHT_MM, BLEED_MM, layout.margin_y_mm)
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

    pdf.set_line_width(_MARK_WIDTH_MM)
    pdf.set_draw_color(*_MARK_COLOR)
    for xi, x in enumerate(xs):
        for yi, y in enumerate(ys):
            if xi > 0:
                pdf.line(x - _MARK_LENGTH_MM, y, x, y)
            if xi < layout.cols:
                pdf.line(x, y, x + _MARK_LENGTH_MM, y)
            if yi > 0:
                pdf.line(x, y - _MARK_LENGTH_MM, x, y)
            if yi < layout.rows:
                pdf.line(x, y, x, y + _MARK_LENGTH_MM)


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
    face_items: list[FaceResult], preferred_dpi: int | None
) -> tuple[FaceResult, bool]:
    """Pick the source image for a face-group: the preferred DPI if that
    variant was generated, else the highest available. Returns (chosen,
    fell_back)."""
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
    """
    units: list[PrintUnit] = []
    unmatched: list[str] = []

    for key, face_items in group_by_face(gallery):
        rep = face_items[0]
        best, dpi_fallback = _pick_dpi_variant(face_items, preferred_dpi)
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
    show_cut_lines: bool = True,
) -> bytes:
    """Render pages of card slots into a print-ready PDF, in memory.

    Caches the bled+JPEG-encoded bytes per unique out_path so a card printed
    N times only pays the corner-flatten+bleed+encode cost once.
    """
    pdf = FPDF(orientation=layout.orientation, unit="mm", format=layout.paper)
    pdf.set_auto_page_break(False)
    pdf.set_margins(0, 0, 0)

    cache: dict[Path, bytes] = {}
    for page_slots in pages:
        pdf.add_page()
        for idx, face in enumerate(page_slots):
            col, row = idx % layout.cols, idx // layout.cols
            x = layout.margin_x_mm + col * BLED_WIDTH_MM
            y = layout.margin_y_mm + row * BLED_HEIGHT_MM
            jpeg_bytes = cache.get(face.out_path)
            if jpeg_bytes is None:
                with Image.open(face.out_path) as raw:
                    bled = add_bleed(raw.convert("RGBA"), dpi=face.dpi)
                buf = io.BytesIO()
                bled.save(buf, format="JPEG", quality=_JPEG_QUALITY)
                jpeg_bytes = buf.getvalue()
                cache[face.out_path] = jpeg_bytes
            # Fresh BytesIO per call — fpdf2's fast DCTDecode passthrough
            # path re-reads from position 0 each time, but a shared object
            # across repeated copies of the same card is an easy footgun to
            # avoid entirely by just re-wrapping the cached immutable bytes.
            pdf.image(
                io.BytesIO(jpeg_bytes), x=x, y=y, w=BLED_WIDTH_MM, h=BLED_HEIGHT_MM
            )

        if show_cut_lines:
            _draw_cut_marks(pdf, layout)

    return bytes(pdf.output())
