"""Print-sheet PDF layout: bleed extension, cut guides, grid pagination.

Pure logic, no Streamlit dependency — mirrors the pipeline.py/decklist.py
separation used elsewhere in this codebase.
"""

from __future__ import annotations

import io
from collections.abc import Callable
from dataclasses import dataclass
from pathlib import Path

from fpdf import FPDF
from PIL import Image

from .decklist import DeckEntry
from .dpi import CARD_HEIGHT_MM, CARD_WIDTH_MM, MM_PER_IN
from .pipeline import FaceResult, _resize_to_dpi, group_by_face

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
}

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


def _draw_cut_marks(pdf: FPDF, layout: PageLayout) -> None:
    """Black lines from the page edge to the grid block (outer margins,
    nothing to obscure there) — two closely-spaced lines per interior gap,
    one per card's own trim edge; small green "+" crop marks at each card's
    own trim-corner (the grid is regular, so the full xs × ys cross product
    is exactly every card's 4 corners, no card ever gets a mark that isn't
    its own).

    Every guide is nudged OUTWARD from its card by half its stroke width.
    A stroke centered on the trim coordinate puts half its ink inside the
    card, where it survives a clean cut as a hairline along the card's
    border; nudged, the stroke's inner edge sits exactly on the trim line
    (card edge, then guide) and a perfectly cut card carries no guide ink.
    Which way is outward falls out of _card_trim_edges' ordering: even
    indices are leading (left/top) edges, odd are trailing (right/bottom)."""
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

    pdf.set_draw_color(*_MARK_COLOR)
    for x in xs:
        for y in ys:
            pdf.line(x - layout.guide_length_mm, y, x + layout.guide_length_mm, y)
            pdf.line(x, y - layout.guide_length_mm, x, y + layout.guide_length_mm)


def _replicate_top_left_corner(img: Image.Image, r: int, alpha_threshold: int) -> None:
    """Mutates `img` in place: for each of the first r rows, fill only that
    row's leading *transparent* run with a colour sampled just past the
    first opaque pixel it runs into.

    Per-row and transparency-aware on purpose. A previous version stretched
    one fixed column (x=r) across the full width of every row in the r×r
    square, which overwrote opaque card art that happened to fall inside
    that square — the rounded arc only covers part of it — and painted the
    result as horizontal bands. That was visible in the PDF as smeared
    streaks running out of every card corner. Only genuinely transparent
    pixels should ever be touched here.

    The fill colour comes from a few pixels *past* the arc boundary, not
    the boundary pixel itself: upscaling smears the black RGB that sits
    under the transparent corner into the first opaque pixel or two (the
    upscaled alpha and colour boundaries don't land on exactly the same
    pixel), so on a light-bordered card the boundary pixel is often
    near-black. add_bleed() then stretches row 0 / column 0 ~50x into the
    bleed border, magnifying that one contaminated pixel into a visible
    black smear sweeping out of the corner. Median-of-three sampling past
    the halo keeps a single stray art pixel from streaking a whole row.
    """
    px = img.load()
    w, h = img.size
    # Halo width scales with the upscale factor, and this patch scales
    # with the image (probe_frac of it) — ~3% of the patch clears the
    # halo at any DPI while staying well inside the border/art region.
    skip = max(2, round(min(w, h) * 0.03))

    def _lum(p) -> float:
        return 0.299 * p[0] + 0.587 * p[1] + 0.114 * p[2]

    def _median_color(pixels) -> tuple[int, int, int]:
        s = sorted(pixels, key=_lum)
        return s[1][:3]

    def _is_halo(p, sample_lum: float) -> bool:
        # The artifact signature, and only the artifact signature: the
        # upscaler blends the black under the transparent corner into the
        # arc's rim pixels, so a bad pixel is near-black AND markedly
        # darker than the interior right next to it. Genuinely black
        # borders fail the second test (their interior is just as dark,
        # and overwriting would be a no-op anyway), and genuine art
        # detail fails the first — it stays untouched.
        pl = _lum(p)
        return pl < 60 and sample_lum - pl > 60

    # Column-top offsets must be measured before any fill mutates alpha —
    # the vertical scrub below needs to know where each column's
    # transparent run originally ended.
    col_y0 = [
        next((y for y in range(h) if px[x, y][3] >= alpha_threshold), None)
        for x in range(min(r, w))
    ]

    for y in range(min(r, h)):
        # Scan the full row, not just the first r pixels: when the probe
        # window is narrower than the corner radius there is no opaque
        # pixel within r, and stopping early would leave the corner
        # transparent (add_bleed would then drop it to black).
        x0 = next((x for x in range(w) if px[x, y][3] >= alpha_threshold), None)
        if not x0:  # row fully opaque already (x0 == 0), or no opaque pixel found
            continue
        # Sample diagonally inward, not along the row: near the arc's
        # tangent point the halo band runs almost parallel to the row, so
        # stepping horizontally can stay inside it indefinitely — stepping
        # toward the card's interior crosses the band at its ~2px
        # thickness no matter where on the arc this row lands.
        color = _median_color(
            px[min(x0 + skip * k, w - 1), min(y + skip * k, h - 1)] for k in (1, 2, 3)
        )
        # Fill the transparent run, then scrub the rim just past it —
        # but only pixels carrying the black-contamination signature:
        # they're opaque, so they survive the fill — leaving a thin dark
        # arc line in the printed card, and (where the arc meets row 0) a
        # dark streak stretched into the bleed right where the rounding
        # starts. Anything that isn't the artifact keeps its art.
        for x in range(x0):
            px[x, y] = (*color, 255)
        sample_lum = _lum(color)
        for x in range(x0, min(x0 + skip, w)):
            if _is_halo(px[x, y], sample_lum):
                px[x, y] = (*color, 255)

    # Same scrub vertically: the arc's other endpoint meets column 0 on
    # rows that have no transparent run at all (the row loop skips them),
    # so their halo only shows up as a column-top run.
    for x in range(min(r, w)):
        y0 = col_y0[x]
        if not y0:
            continue
        color = _median_color(
            px[min(x + skip * k, w - 1), min(y0 + skip * k, h - 1)] for k in (1, 2, 3)
        )
        sample_lum = _lum(color)
        for y in range(y0, min(y0 + skip, h)):
            if _is_halo(px[x, y], sample_lum):
                px[x, y] = (*color, 255)


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

    Strictly an export-time step: it belongs to the PDF pipelines only,
    never to generation. The replicated pixels are a visible smear when
    viewed as an image rather than composited onto a print sheet, and
    baking them into the stored PNG is irreversible — see
    pipeline.py::_write_dpi_variant, which deliberately does not call this.
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
        _replicate_top_left_corner(oriented, r, alpha_threshold)
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
    # Retained for callers that inspect it, but always False now that a face
    # with no image at the requested DPI is excluded and reported instead of
    # being silently printed at another resolution.
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

    Returns (chosen, unavailable_at_preferred_dpi).
    """
    if preferred_dpi is not None:
        at_dpi = [item for item in face_items if item.dpi == preferred_dpi]
        if not at_dpi:
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

    Returns (units, missing, missing_at_dpi).
    """
    units: list[PrintUnit] = []
    missing_at_dpi: list[str] = []
    matched_face_counts = [0] * len(entries)
    # How many faces each entry's card actually has, learned from whichever
    # matched gallery group(s) happen to know it (see FaceResult.total_faces
    # / db migration 003) — None until a matched group with a known value is
    # seen, same "unknown, don't verify" meaning as on the row itself.
    expected_faces_by_entry: list[int | None] = [None] * len(entries)

    for key, face_items in group_by_face(gallery):
        rep = face_items[0]
        best, unavailable = _pick_dpi_variant(face_items, preferred_dpi, preferred_model)
        if best is None:
            # No image at the requested DPI. Reported to the caller and left
            # out of the print run entirely — never substituted with another
            # resolution (see _pick_dpi_variant).
            missing_at_dpi.append(_describe_face(rep))
            continue
        set_code = (rep.set_code or "").lower() or None
        collector = rep.collector_number
        card_name = (rep.card_name or rep.face_name or "").casefold()

        matched_qty = 0
        matched_indices: set[int] = set()
        if set_code and collector:
            for i, entry in enumerate(entries):
                if entry.set_code == set_code and entry.collector_number == str(collector):
                    matched_qty += entry.quantity
                    matched_indices.add(i)
        if not matched_indices and card_name:
            for i, entry in enumerate(entries):
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
                    matched_indices.add(i)

        if matched_indices:
            units.append(
                PrintUnit(face_key=key, quantity=matched_qty, best=best, dpi_fallback=unavailable)
            )
            for i in matched_indices:
                matched_face_counts[i] += 1
                if rep.total_faces is not None:
                    expected_faces_by_entry[i] = rep.total_faces
        # else: no current decklist entry wants this printing any more —
        # silently excluded from the print run, not reported.

    missing: list[str] = []
    for i, entry in enumerate(entries):
        matched = matched_face_counts[i]
        expected = expected_faces_by_entry[i]
        if matched == 0 or (expected is not None and matched < expected):
            missing.append(_describe_entry(entry, matched, expected))

    return units, missing, missing_at_dpi


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


def unique_image_count(pages: list[list[FaceResult]]) -> int:
    """How many source images build_pdf will actually process.

    The per-image work is cached per out_path, so this — not the number of
    print slots — is the real unit of progress: a card printed eight times
    costs one decode/resize/bleed/encode and seven near-free placements.
    Exposed so a caller can size a progress bar before starting the build.
    """
    return len({face.out_path for page in pages for face in page})


def build_pdf(
    pages: list[list[FaceResult]],
    *,
    layout: PageLayout,
    export_dpi: int,
    show_cut_lines: bool = True,
    on_progress: Callable[[int, int], None] | None = None,
) -> bytes:
    """Render pages of card slots into a print-ready PDF, in memory.

    Caches the bled+JPEG-encoded bytes per unique out_path so a card printed
    N times only pays the resize+corner-flatten+bleed+encode cost once.
    `export_dpi` is one value for the whole PDF (not per-card) — when it
    differs from a source image's own native `face.dpi`, that image is
    resized to `export_dpi`'s pixel density before bleed is added, so e.g. a
    1200 DPI generated source can still be exported into an 800 DPI PDF for
    a smaller file.

    `on_progress(completed, total)` fires once per *unique* image, right
    after that image's expensive work lands in the cache — see
    unique_image_count for why that's the honest unit. It may raise to
    abort the build (pdf_jobs.PdfRenderCanceled does exactly that for a
    user-requested cancel); nothing here catches it, so the exception
    unwinds to the caller with no partial PDF produced.
    """
    # Pre-oriented tuple — always pass orientation="portrait" to fpdf2 here,
    # since FPDF._set_orientation() swaps w_pt/h_pt for any non-portrait
    # orientation even with a tuple format, which would silently undo our
    # own already-correct page_w_mm/page_h_mm ordering.
    pdf = FPDF(orientation="portrait", unit="mm", format=(layout.page_w_mm, layout.page_h_mm))
    pdf.set_auto_page_break(False)
    pdf.set_margins(0, 0, 0)

    cache: dict[Path, bytes] = {}
    total_images = unique_image_count(pages)
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
                if on_progress is not None:
                    on_progress(len(cache), total_images)
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
