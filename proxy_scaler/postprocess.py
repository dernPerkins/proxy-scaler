"""Post-download cleanup of Scryfall original PNGs.

Some of Scryfall's own renders ship with baked-in defects (measured across
eras and border styles, 2026-09 — see cards.bleed-samples.txt for the test
corpus):

- A single near-black, fully-opaque row on the bottom edge of otherwise
  bone-white cards (the SLZ and MB2 sets, at least) — a processing artifact
  on Scryfall's side, not part of the card.
- Black RGB underneath the transparent rounded corners of every PNG. The
  RGB-only upscale models split alpha off and resize it separately, so that
  hidden black smears into the first opaque rim pixels ("halo").

This module fixes such defects ONCE, at the download boundary, before the
bytes are cached or upscaled. Contract:

- clean_original_png() returns the INPUT bytes object unchanged (byte
  identity, no re-encode) when no fixup fires — the common case. Only
  defective images pay a re-encode.
- The alpha channel is never modified. Fixup B recolors only pixels with
  alpha == 0, so the cleaned PNG composites pixel-identically everywhere.
- Fixups are small, independent, and ordered; new ones (ICC normalization,
  etc.) slot into _FIXUPS.

The export-time scrubs in pdf_layout.py (corner halo, bleed edge vetting)
stay in place as defense-in-depth for originals cached before this module
existed — "new downloads only" is deliberate, with the per-card Re-Fetch
endpoint as the upgrade path (it overwrites the original and invalidates
every derived thumb/upscale cache).
"""

from __future__ import annotations

import io
from collections.abc import Callable
from typing import NamedTuple

from PIL import Image, ImageChops

# The artifact signature, shared with pdf_layout's export-time scrubs
# (_is_halo, _bleed_edge_index): a defective region is near-black AND
# markedly darker than the genuine card surface right next to it. Genuine
# black regions fail the delta test (their surroundings are just as dark),
# genuine light regions fail the luminance test.
DARK_EDGE_LUM_MAX = 60
DARK_EDGE_DELTA_MIN = 60

# The phantom row is 1px at Scryfall's ~300dpi render scale; allow a small
# run, but a dark band deeper than this is treated as card design (modern
# collector-info bars are ~70px) and left alone.
_MAX_PHANTOM_EDGE_PX = 3
# Clean-reference strip inset — must exceed _MAX_PHANTOM_EDGE_PX so a
# scrubbed run is always compared against a strip past the deepest
# possible artifact.
_EDGE_INSET_PROBE_PX = 4
# Border-color sampling inset for fixup B — past both the phantom rows and
# the corner arcs' anti-aliasing, inside the printed border.
_BORDER_SAMPLE_INSET_PX = 8

_OPAQUE_MIN = 200  # alpha at/above this counts as opaque
_MIN_OPAQUE_FRAC = 0.25  # skip an edge whose strip is mostly transparent
# Fixup B is skipped when the underlay already sits within this per-channel
# delta of the border color — keeps black-border cards (the vast majority)
# byte-identical instead of re-encoding them for an invisible change.
_UNDERLAY_DELTA_MIN = 24
# Tiny images (including the test suite's synthetic fakes) can't carry the
# artifacts and would break the sampling geometry — pass them through.
_MIN_DIM_FOR_CLEANING = 32


class CleanResult(NamedTuple):
    png_bytes: bytes  # the input bytes object itself when applied == ()
    applied: tuple[str, ...]  # names of the fixups that fired


def _lum(p: tuple[int, ...]) -> float:
    return 0.299 * p[0] + 0.587 * p[1] + 0.114 * p[2]


def _strip_coords(
    w: int, h: int, *, horizontal: bool, index: int
) -> list[tuple[int, int]]:
    if horizontal:
        return [(x, index) for x in range(w)]
    return [(index, y) for y in range(h)]


def _opaque_median_lum(px, coords: list[tuple[int, int]]) -> float | None:
    lums = sorted(_lum(px[c]) for c in coords if px[c][3] >= _OPAQUE_MIN)
    if len(lums) < max(1, round(_MIN_OPAQUE_FRAC * len(coords))):
        return None
    return lums[len(lums) // 2]


def _scrub_phantom_edges(img: Image.Image) -> Image.Image | None:
    """Fixup A: replace phantom near-black edge rows/columns with the
    nearest clean strip's RGB.

    Per edge: measure a reference strip _EDGE_INSET_PROBE_PX in, then count
    how many consecutive strips from the true edge carry the artifact
    signature against it. A run of zero (clean edges, and any genuinely
    black edge — the reference is just as dark, delta ≈ 0) leaves the edge
    untouched; a run deeper than _MAX_PHANTOM_EDGE_PX is card design, also
    untouched — partial scrubbing of a real black bar would be worse than
    none. Only opaque pixels are rewritten and only from opaque reference
    pixels, so the transparent corner ends of each strip keep their alpha
    AND their RGB (fixup B owns those).
    """
    px = img.load()
    w, h = img.size
    fired = False
    edges = (
        (True, tuple(range(_MAX_PHANTOM_EDGE_PX + 1)), _EDGE_INSET_PROBE_PX),
        (
            True,
            tuple(h - 1 - k for k in range(_MAX_PHANTOM_EDGE_PX + 1)),
            h - 1 - _EDGE_INSET_PROBE_PX,
        ),
        (False, tuple(range(_MAX_PHANTOM_EDGE_PX + 1)), _EDGE_INSET_PROBE_PX),
        (
            False,
            tuple(w - 1 - k for k in range(_MAX_PHANTOM_EDGE_PX + 1)),
            w - 1 - _EDGE_INSET_PROBE_PX,
        ),
    )
    for horizontal, candidates, ref_index in edges:
        ref_coords = _strip_coords(w, h, horizontal=horizontal, index=ref_index)
        ref_lum = _opaque_median_lum(px, ref_coords)
        if ref_lum is None:
            continue
        # candidates has one extra entry past _MAX_PHANTOM_EDGE_PX purely to
        # detect a too-deep run; that extra strip is never scrubbed.
        run = 0
        for index in candidates:
            coords = _strip_coords(w, h, horizontal=horizontal, index=index)
            strip_lum = _opaque_median_lum(px, coords)
            if (
                strip_lum is None
                or strip_lum >= DARK_EDGE_LUM_MAX
                or ref_lum - strip_lum <= DARK_EDGE_DELTA_MIN
            ):
                break
            run += 1
        if run == 0 or run > _MAX_PHANTOM_EDGE_PX:
            continue
        for index in candidates[:run]:
            coords = _strip_coords(w, h, horizontal=horizontal, index=index)
            for target, ref in zip(coords, ref_coords):
                tp = px[target]
                rp = px[ref]
                if tp[3] >= _OPAQUE_MIN and rp[3] >= _OPAQUE_MIN:
                    px[target] = (rp[0], rp[1], rp[2], tp[3])
        fired = True
    return img if fired else None


def _sample_border_color(img: Image.Image) -> tuple[int, int, int] | None:
    """Median border color from four full-length strips just inside the
    edges — the median (by luminance) ACTUAL pixel, never an invented
    average, in the style of pdf_layout._median_color. MTG borders are
    uniform around the perimeter, so one global color is enough (the same
    approach mpc-scryfall's formatter uses)."""
    px = img.load()
    w, h = img.size
    inset = _BORDER_SAMPLE_INSET_PX
    if min(w, h) <= 2 * inset:
        return None
    coords = (
        _strip_coords(w, h, horizontal=True, index=inset)
        + _strip_coords(w, h, horizontal=True, index=h - 1 - inset)
        + _strip_coords(w, h, horizontal=False, index=inset)
        + _strip_coords(w, h, horizontal=False, index=w - 1 - inset)
    )
    samples = sorted(
        (px[c] for c in coords if px[c][3] >= _OPAQUE_MIN), key=_lum
    )
    if not samples:
        return None
    p = samples[len(samples) // 2]
    return (p[0], p[1], p[2])


def _recolor_transparent_underlay(img: Image.Image) -> Image.Image | None:
    """Fixup B: set the RGB under fully-transparent pixels (the rounded
    corners) to the border color, leaving alpha byte-for-byte unchanged.

    Strictly alpha == 0: recoloring any partially-transparent pixel would
    change how the PNG composites. The anti-aliased arc rim keeps its dark
    RGB — a small residual smear source deliberately left to the
    export-time halo scrub. The result renders pixel-identically; what
    changes is what the upscaler sees when it resamples across the corner
    boundary."""
    alpha = img.getchannel("A")
    mask = alpha.point(lambda v: 255 if v == 0 else 0)
    if mask.getbbox() is None:
        return None
    color = _sample_border_color(img)
    if color is None:
        return None
    rgb = img.convert("RGB")
    recolored = Image.composite(Image.new("RGB", img.size, color), rgb, mask)
    diff = ImageChops.difference(recolored, rgb)
    if max(hi for _, hi in diff.getextrema()) <= _UNDERLAY_DELTA_MIN:
        return None  # underlay already ≈ border (black-border cards)
    return Image.merge("RGBA", (*recolored.split(), alpha))


_FIXUPS: tuple[tuple[str, Callable[[Image.Image], Image.Image | None]], ...] = (
    ("edge_row_scrub", _scrub_phantom_edges),
    ("underlay_recolor", _recolor_transparent_underlay),
)


def clean_original_png(png_bytes: bytes) -> CleanResult:
    """Run every fixup over a freshly-downloaded Scryfall PNG.

    Non-PNG payloads, undecodable bytes, and tiny images pass through
    untouched (whatever would have happened to them downstream still
    does). Byte identity when nothing fired; a single re-encode when
    something did, carrying the ICC profile forward if one exists."""
    try:
        decoded = Image.open(io.BytesIO(png_bytes))
        if decoded.format != "PNG":
            return CleanResult(png_bytes, ())
        original_mode = decoded.mode
        icc_profile = decoded.info.get("icc_profile")
        img = decoded.convert("RGBA") if original_mode != "RGBA" else decoded
        img.load()
    except Exception:
        return CleanResult(png_bytes, ())
    if min(img.size) < _MIN_DIM_FOR_CLEANING:
        return CleanResult(png_bytes, ())

    applied: list[str] = []
    for name, fixup in _FIXUPS:
        result = fixup(img)
        if result is not None:
            img = result
            applied.append(name)
    if not applied:
        return CleanResult(png_bytes, ())

    if original_mode == "RGB":
        # Don't graft an alpha channel onto a PNG that never had one.
        img = img.convert("RGB")
    buf = io.BytesIO()
    if icc_profile:
        img.save(buf, format="PNG", icc_profile=icc_profile)
    else:
        img.save(buf, format="PNG")
    return CleanResult(buf.getvalue(), tuple(applied))
