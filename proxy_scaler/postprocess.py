"""Post-download cleanup of Scryfall original PNGs.

Scryfall's own renders ship with baked-in defects in their outermost
pixels (measured across eras and border styles, 2026-09 — see
cards.bleed-samples.txt for the test corpus):

- A single near-black, fully-opaque row on the bottom edge of otherwise
  bone-white cards (the SLZ and MB2 sets, at least).
- A phantom near-white column on the left edge of a dark borderless print
  (Fellwar Stone, sld 7062) — the same defect, inverted.
- On the shared gold-border template every ptc/wc97..wc04 card is
  composited onto: rounded-corner arcs anti-aliased against WHITE (the
  partial-alpha rim pixels are ~(255,255,255)), a bright fully-opaque
  highlight line one pixel inside each arc, and a lone white pixel on the
  top and bottom edge rows of the older template.
- Black (or white, or grey) RGB underneath the transparent rounded corners
  of every PNG. The RGB-only upscale models split alpha off and resize it
  separately, so whatever hides under the corner smears into the first
  opaque rim pixels ("halo") — a dark arc on light cards, a white arc on
  the gold template.

Every one of these lives within ~0.25mm of the edge, on the trim line. So
rather than a colour-signature scrub per defect, this module applies ONE
geometric fixup (edge_extend.extend_edges in ``mirror`` mode): the RGB of
every pixel outside a rounded rectangle inset 3px from the true edge — the
transparent corners, the anti-aliased rim, and the outer 3px strip — is
replaced by the border texture mirrored across that inset boundary. No
colour test means no dependence on whether a defect is dark, light or
grey. Mirroring (rather than the print-style nearest-point rays) is
deliberate: the upscaler sees the RGB under the corners and sharpens rays
into visible fibres across the arc, while mirrored texture reads as more
border and leaves the arc clean.

Contract:

- clean_original_png() returns the INPUT bytes object unchanged (byte
  identity, no re-encode) when the fixup would change nothing visible —
  flat black borders over a black underlay, the common case. Only images
  that actually change pay a re-encode.
- The alpha channel is never modified, so the cleaned PNG composites the
  same everywhere except the anti-aliased rim, which now blends the border
  colour instead of the matte Scryfall happened to use.
- A genuine deep edge feature (modern ~6mm collector-info bars, black
  borders) is its own mirror image and survives untouched.

The export-time extension in pdf_layout.py (flatten_corner_alpha /
add_bleed) re-sources the same 0.25mm strip from the upscaled image, so
originals cached before this module existed still print cleanly — "new
downloads only" is deliberate, with the per-card Re-Fetch endpoint as the
upgrade path (it overwrites the original and invalidates every derived
thumb/upscale cache).
"""

from __future__ import annotations

import io
from typing import NamedTuple

import numpy as np
from PIL import Image

from .edge_extend import corner_radius_px, extend_edges

# How far inside the true edge the mirror boundary sits, at Scryfall's
# ~300dpi render scale: 3px ≈ 0.25mm, past the deepest measured defect
# (the phantom rows are 1px, the gold template's rim + highlight line are
# 2px) and far inside anything genuine (collector bars are ~70px).
RIM_INSET_PX = 3
# Skip the re-encode when no channel of any pixel moves more than this —
# keeps black-border cards over a black underlay byte-identical instead of
# re-encoding them for an invisible change.
_NOOP_DELTA_MAX = 24
# Tiny images (including the test suite's synthetic fakes) can't carry the
# defects and would break the geometry — pass them through.
_MIN_DIM_FOR_CLEANING = 32

FIXUP_NAME = "rim_extend"


class CleanResult(NamedTuple):
    png_bytes: bytes  # the input bytes object itself when applied == ()
    applied: tuple[str, ...]  # names of the fixups that fired


def _extend_rim(img: Image.Image) -> Image.Image | None:
    """Mirror the border texture into the transparent corners, the
    anti-aliased rim and the outer RIM_INSET_PX strip. Alpha untouched.
    None when the result is visually identical to the input."""
    arr = np.asarray(img.convert("RGBA"))
    rgb = arr[..., :3]
    radius = corner_radius_px(img)
    extended = extend_edges(
        rgb,
        radius_px=radius,
        inset_px=RIM_INSET_PX,
        bleed_px=0,
        mode="mirror",
        alpha=arr[..., 3],
    )
    delta = np.abs(extended.astype(np.int16) - rgb.astype(np.int16)).max()
    if delta <= _NOOP_DELTA_MAX:
        return None
    out = arr.copy()
    out[..., :3] = extended
    return Image.fromarray(out, "RGBA")


def clean_original_png(png_bytes: bytes) -> CleanResult:
    """Run the fixup over a freshly-downloaded Scryfall PNG.

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

    result = _extend_rim(img)
    if result is None:
        return CleanResult(png_bytes, ())
    img = result

    if original_mode == "RGB":
        # Don't graft an alpha channel onto a PNG that never had one.
        img = img.convert("RGB")
    buf = io.BytesIO()
    if icc_profile:
        img.save(buf, format="PNG", icc_profile=icc_profile)
    else:
        img.save(buf, format="PNG")
    return CleanResult(buf.getvalue(), (FIXUP_NAME,))
