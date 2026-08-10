"""Alternate HTML->PDF pipeline via WeasyPrint — a second rendering path
alongside pdf_layout.py's default fpdf2 one, so the two can be compared
before deciding which to keep (see pyproject.toml's html-pdf extra for
why this is dev-loop-only for now, not bundled into the frozen sidecar).

Reuses pdf_layout.py's geometry (PageLayout) and bleed logic (add_bleed)
directly rather than duplicating either — the two pipelines should stay
visually comparable at the same layout settings, not drift apart.
"""

from __future__ import annotations

import html as html_escape
import tempfile
from collections.abc import Callable
from pathlib import Path

from PIL import Image

from .pdf_layout import PageLayout, add_bleed, flatten_corner_alpha, unique_image_count
from .pipeline import FaceResult, _resize_to_dpi


class WeasyPrintUnavailable(RuntimeError):
    """weasyprint isn't installed on this server — the API route maps
    this to a 503 rather than a raw import crash."""


def _page_css(layout: PageLayout) -> str:
    return (
        f"@page {{ size: {layout.page_w_mm}mm {layout.page_h_mm}mm; margin: 0; }}"
        f".page {{ position: relative; width: {layout.page_w_mm}mm; "
        f"height: {layout.page_h_mm}mm; page-break-after: always; }}"
        f".slot {{ position: absolute; width: {layout.bled_card_w_mm}mm; "
        f"height: {layout.bled_card_h_mm}mm; object-fit: cover; }}"
    )


def _slot_style(layout: PageLayout, idx: int) -> str:
    col, row = idx % layout.cols, idx // layout.cols
    x = layout.margin_x_mm + col * layout.cell_w_mm
    y = layout.margin_y_mm + row * layout.cell_h_mm
    return f"left: {x}mm; top: {y}mm;"


def build_html_document(
    pages: list[list[FaceResult]],
    *,
    layout: PageLayout,
    image_uri_by_out_path: dict[Path, str],
) -> str:
    """Cut-marks intentionally not replicated here — secondary to the
    primary fpdf2-vs-HTML output-quality comparison this exists for; CSS
    border/dashed-line guides are a natural follow-up if this method is
    kept. Placement math mirrors pdf_layout.py::build_pdf exactly (same
    col/row -> x/y formula) so the two outputs are comparable at the same
    layout settings."""
    body_parts = []
    for page in pages:
        tiles = [
            f'<img class="slot" style="{_slot_style(layout, idx)}" '
            f'src="{image_uri_by_out_path[face.out_path]}" '
            f'alt="{html_escape.escape(face.card_name)}">'
            for idx, face in enumerate(page)
        ]
        body_parts.append(f'<div class="page">{"".join(tiles)}</div>')
    return (
        f"<html><head><style>{_page_css(layout)}</style></head>"
        f"<body>{''.join(body_parts)}</body></html>"
    )


def build_pdf_html(
    pages: list[list[FaceResult]],
    *,
    layout: PageLayout,
    export_dpi: int,
    on_progress: Callable[[int, int], None] | None = None,
) -> bytes:
    """Render `pages` to PDF bytes via WeasyPrint, using full-resolution
    images (not the small PDF-preview thumbnails — this is a real
    print-quality comparison, not a fast on-screen preview). Raises
    WeasyPrintUnavailable if weasyprint isn't installed on this server.

    `on_progress(completed, total)` fires once per unique image, matching
    build_pdf's contract (see pdf_layout.build_pdf) so both pipelines can
    drive the same progress UI. May raise to abort the build.
    """
    try:
        from weasyprint import HTML  # lazy: optional extra, same convention as upscale.py's lazy torch import
    except ImportError as exc:
        raise WeasyPrintUnavailable(
            "WeasyPrint isn't installed on this server. Install the "
            'optional extra: pip install -e ".[html-pdf]"'
        ) from exc

    # out_path has no bleed baked in (bleed_mm is a per-request layout
    # parameter — pdf_layout.py::build_pdf applies add_bleed dynamically
    # per slot too, caching per unique out_path, same as here) — bled
    # images are materialized to a temp dir and referenced via file://
    # rather than base64-embedded: at 800-1200 DPI that would add ~33%
    # size and hold every image in memory at once, where WeasyPrint can
    # instead stream each from disk. The TemporaryDirectory guarantees
    # cleanup even on exception.
    with tempfile.TemporaryDirectory(prefix="proxy-scaler-html-pdf-") as tmp_dir:
        tmp_path = Path(tmp_dir)
        image_uri_by_out_path: dict[Path, str] = {}
        total_images = unique_image_count(pages)
        for page in pages:
            for face in page:
                if face.out_path in image_uri_by_out_path:
                    continue
                # Flatten the rounded-corner alpha to opaque BEFORE any
                # resize — resizing while corners are still transparent
                # lets the resample filter blend the transparent region's
                # RGB into the opaque body right at the boundary (alpha
                # fringing), baking in a visible smear at every corner.
                # This exact bug already got fixed once in build_pdf
                # (pdf_layout.py) — mirrored here rather than skipped,
                # since add_bleed()'s own internal flatten happens too
                # late if a resize sits between opening the file and
                # calling it.
                with Image.open(face.out_path) as raw:
                    img = flatten_corner_alpha(raw.convert("RGBA"))
                if export_dpi != face.dpi:
                    img = _resize_to_dpi(img, export_dpi)
                bled = add_bleed(img, dpi=export_dpi, bleed_mm=layout.bleed_mm)
                dest = tmp_path / f"{face.out_path.stem}_bled.png"
                bled.save(dest)
                image_uri_by_out_path[face.out_path] = dest.resolve().as_uri()
                if on_progress is not None:
                    on_progress(len(image_uri_by_out_path), total_images)

        document = build_html_document(
            pages, layout=layout, image_uri_by_out_path=image_uri_by_out_path
        )
        return HTML(string=document).write_pdf()
