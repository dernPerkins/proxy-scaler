"""Edge extension for rounded-rectangle card images.

One geometric operation serves two jobs that used to be separate, heuristic
scrubs: filling the transparent rounded corners of a Scryfall PNG (and the
anti-aliased rim around them), and growing the bleed border around a card
on a print sheet.

The card is modelled as a rounded rectangle whose corner radius is measured
from the alpha channel. Every pixel outside a copy of that rectangle inset
by a small margin takes its colour from that inset boundary:

- ``nearest``: the colour of the closest point on the inset boundary. Along
  a straight edge this is the perpendicular clamp-to-edge stretch; around a
  corner the closest point lies on the arc, so colours fan out radially from
  the arc's centre of curvature. This is the print-shop look (and what
  proxxied.com's JFA shader produces), and it is what the PDF bleed uses.
- ``mirror``: the colour found by reflecting the pixel across the inset
  boundary. The border texture continues into the filled region instead of
  smearing into rays. This is what the download-time cleanup uses, because
  the RGB-only upscalers see whatever sits under the transparent corners
  and sharpen rays into visible fibres, while mirrored texture reads as
  more of the same border.

The inset is the point of the whole thing. Scryfall's renders carry defects
in the outermost pixels: a near-black 1px row baked into some light cards
(SLZ, MB2), a white anti-aliased rim and a bright opaque highlight line on
the shared gold-border template (every ptc/wc97..wc04 card), lone white
pixels on that template's edge rows, a phantom white column on at least one
Secret Lair print. All of them live within 0.25mm of the edge, which is on
the trim line. Sampling from the inset boundary overwrites that strip with
the pixels just inside it, with no colour test at all — so it does not
matter whether the defect is dark, light, or grey, or which model
upscaled it. A genuine deep feature (the ~6mm collector-info bar on modern
white-bordered cards, a black border) is its own neighbour and survives
untouched.

Pure numpy; no scipy/OpenCV. The mapping is closed-form for a rounded
rectangle, so there is no distance transform to run, and it is only
evaluated over the outer ring (bleed + corner radius + inset) — the
interior is a block copy. The corner radius is whatever the image carries:
Alpha's rounder die cut (4.4mm on Scryfall's renders, against 2.7mm for
Beta onward) needs no special case.
"""

from __future__ import annotations

import numpy as np
from PIL import Image

# Alpha at/above this counts as opaque when measuring the corner radius.
OPAQUE_MIN = 250

# A measured radius larger than this fraction of the short side is not a
# rounded corner (a transparent margin, a mostly-transparent image, a
# fake) — treat the image as square-cornered rather than carve rays out of
# real content. Real cards measure ~4% (32px of 745); Alpha's rounder die
# cut (52px) is still well under this.
_MAX_RADIUS_FRAC = 0.25


def corner_radius_px(image: Image.Image) -> int:
    """Corner radius of a card image, from its alpha channel.

    The first opaque pixel along row 0 (and column 0) sits at x == radius
    for a quarter-circle arc tangent to both edges. Images with no alpha
    channel, or no transparent corner, measure 0 and get plain
    clamp-to-edge behaviour everywhere — including square-cornered Custom
    Images, whose corner pixels are real content and must not be overwritten.
    """
    if image.mode not in ("RGBA", "LA"):
        return 0
    alpha = np.asarray(image.getchannel("A"))
    if alpha.size == 0:
        return 0
    h, w = alpha.shape
    row = np.flatnonzero(alpha[0] >= OPAQUE_MIN)
    col = np.flatnonzero(alpha[:, 0] >= OPAQUE_MIN)
    if len(row) == 0 or len(col) == 0:
        return 0
    r = int(max(row[0], col[0]))
    if r > _MAX_RADIUS_FRAC * min(w, h):
        return 0
    return r


def _source_indices(
    xs: np.ndarray,
    ys: np.ndarray,
    *,
    w: int,
    h: int,
    core: int,
    rr: float,
    mode: str,
) -> tuple[np.ndarray, np.ndarray]:
    """For canvas pixels at card coordinates (xs, ys) — which may be
    negative or past the edge — the (row, col) of the card pixel each one
    takes its colour from."""
    xs = xs.astype(np.float32)
    ys = ys.astype(np.float32)
    cx = np.clip(xs, core, w - 1 - core)
    cy = np.clip(ys, core, h - 1 - core)
    dx = xs - cx
    dy = ys - cy
    d = np.hypot(dx, dy)
    outside = d > rr
    with np.errstate(invalid="ignore", divide="ignore"):
        ux = np.where(d > 0, dx / d, 0.0)
        uy = np.where(d > 0, dy / d, 0.0)
    # Nearest point on the inset boundary.
    bx = cx + ux * rr
    by = cy + uy * rr
    if mode == "mirror":
        # Reflect through that boundary point.
        bx = 2 * bx - xs
        by = 2 * by - ys
    sx = np.where(outside, bx, xs)
    sy = np.where(outside, by, ys)
    sx = np.clip(np.rint(sx), 0, w - 1).astype(np.intp)
    sy = np.clip(np.rint(sy), 0, h - 1).astype(np.intp)
    return sy, sx


def _opaque_fallback(
    sy: np.ndarray, sx: np.ndarray, *, opaque: np.ndarray, inset: int
) -> np.ndarray:
    """Replace source columns that land on a transparent pixel with the
    nearest opaque pixel in that row, `inset` further in."""
    bad = ~opaque[sy, sx]
    if not bad.any():
        return sx
    h, w = opaque.shape
    has = opaque.any(axis=1)
    first = np.where(has, opaque.argmax(axis=1), 0)
    last = np.where(has, w - 1 - opaque[:, ::-1].argmax(axis=1), w - 1)
    rows = sy[bad]
    cols = sx[bad]
    from_left = cols - first[rows] <= last[rows] - cols
    fx = np.where(
        from_left,
        np.minimum(first[rows] + inset, last[rows]),
        np.maximum(last[rows] - inset, first[rows]),
    )
    sx = sx.copy()
    # A row with no opaque pixel at all has nothing to offer; leave the
    # geometric pick rather than invent a colour.
    sx[bad] = np.where(has[rows], fx, cols)
    return sx


def extend_edges(
    rgb: np.ndarray,
    *,
    radius_px: int,
    inset_px: int,
    bleed_px: int = 0,
    mode: str = "nearest",
    alpha: np.ndarray | None = None,
) -> np.ndarray:
    """Return an (H + 2*bleed) x (W + 2*bleed) x 3 array: `rgb` with every
    pixel outside the inset rounded rectangle re-sourced from that
    rectangle's boundary. Pixels inside it are copied verbatim.

    `radius_px` is the card's corner radius (0 for square corners) and
    `inset_px` how far inside the true edge the sampling boundary sits.
    When the radius is smaller than the inset the inset rectangle simply has
    square corners.

    `alpha` (HxW, same size as `rgb`) makes the sampling alpha-aware: a
    source pixel that is itself transparent — the alpha shape was not the
    arc the radius describes, e.g. a square notch — falls back to the
    nearest opaque pixel in its row, `inset_px` further in. Real Scryfall
    corners are arcs and never take this path; it keeps the flatten from
    ever leaking the transparent underlay colour for other shapes.
    """
    if mode not in ("nearest", "mirror"):
        raise ValueError(f"unknown mode {mode!r}")
    if rgb.ndim != 3 or rgb.shape[2] != 3:
        raise ValueError("rgb must be an HxWx3 array")
    h, w = rgb.shape[:2]
    if alpha is not None and alpha.shape != (h, w):
        raise ValueError("alpha must match rgb's height and width")
    r = max(0, int(radius_px))
    inset = max(0, int(inset_px))
    b = max(0, int(bleed_px))

    # Core rectangle: the inset rounded rect minus its arcs. Its corners
    # are the arcs' centres of curvature; rr is the arc radius after inset.
    core = max(r, inset)
    rr = float(max(r - inset, 0))
    opaque = None if alpha is None else alpha >= OPAQUE_MIN

    out_h, out_w = h + 2 * b, w + 2 * b
    out = np.empty((out_h, out_w, 3), dtype=rgb.dtype)
    ring = b + core  # canvas pixels within this of an edge may be re-sourced

    def fill(y0: int, y1: int, x0: int, x1: int) -> None:
        if y1 <= y0 or x1 <= x0:
            return
        ys, xs = np.mgrid[y0 - b : y1 - b, x0 - b : x1 - b]
        sy, sx = _source_indices(xs, ys, w=w, h=h, core=core, rr=rr, mode=mode)
        if opaque is not None:
            sx = _opaque_fallback(sy, sx, opaque=opaque, inset=inset)
        out[y0:y1, x0:x1] = rgb[sy, sx]

    if 2 * ring >= min(out_h, out_w):
        fill(0, out_h, 0, out_w)
        return out
    # Interior: every pixel here clamps to itself (d == 0), and an opaque
    # arc leaves it opaque — a straight block copy.
    inner = rgb[core : h - core, core : w - core]
    if opaque is not None and not opaque[core : h - core, core : w - core].all():
        fill(0, out_h, 0, out_w)
        return out
    out[ring : out_h - ring, ring : out_w - ring] = inner
    fill(0, ring, 0, out_w)  # top band, full width
    fill(out_h - ring, out_h, 0, out_w)  # bottom band, full width
    fill(ring, out_h - ring, 0, ring)  # left band
    fill(ring, out_h - ring, out_w - ring, out_w)  # right band
    return out
