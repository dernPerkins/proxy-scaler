from __future__ import annotations

import re

from fastapi import APIRouter, HTTPException, Response

from proxy_scaler import db
from proxy_scaler.api.deps import get_db_path
from proxy_scaler.api.schemas import DeckEntryIn, PdfLayoutIn, PdfPreviewOut
from proxy_scaler.decklist import DeckEntry
from proxy_scaler.pdf_layout import (
    PageLayout,
    build_pdf,
    expand_print_slots,
    match_quantities,
    paginate,
    resolve_page_layout,
)
from proxy_scaler.pipeline import FaceResult

router = APIRouter(prefix="/api/pdf", tags=["pdf"])


def _slugify(name: str) -> str:
    slug = re.sub(r"[^A-Za-z0-9._-]+", "_", name).strip("_")
    return slug or "proxy-scaler"


def _to_deck_entry(e: DeckEntryIn) -> DeckEntry:
    return DeckEntry(
        quantity=e.quantity,
        name=e.name,
        set_code=e.set_code,
        collector_number=e.collector_number,
        raw_line=e.raw_line or e.name,
    )


def _prepare(body: PdfLayoutIn) -> tuple[PageLayout, list[list[FaceResult]], list[str]]:
    if not body.entries:
        raise HTTPException(status_code=400, detail="No cards to print.")
    db_path = get_db_path()
    raw_items = db.list_gallery_items(body.project_tag, db_path=db_path)
    items = [FaceResult.from_dict(d) for d in raw_items]
    entries = [_to_deck_entry(e) for e in body.entries]
    units, unmatched = match_quantities(
        entries,
        items,
        preferred_dpi=body.preferred_dpi,
        preferred_model=body.preferred_model,
    )
    layout = resolve_page_layout(
        page_w_mm=body.page_width_mm,
        page_h_mm=body.page_height_mm,
        cols=body.cols,
        rows=body.rows,
        bleed_mm=body.bleed_mm,
        spacing_x_mm=body.spacing_x_mm,
        spacing_y_mm=body.spacing_y_mm,
        offset_x_mm=body.offset_x_mm,
        offset_y_mm=body.offset_y_mm,
        guide_width_pt=body.guide_width_pt,
        guide_length_mm=body.guide_length_mm,
    )
    slots = expand_print_slots(units)
    pages = paginate(slots, layout.cards_per_page)
    return layout, pages, unmatched


@router.post("/preview", response_model=PdfPreviewOut)
def preview(body: PdfLayoutIn) -> PdfPreviewOut:
    _layout, pages, unmatched = _prepare(body)
    total_units = sum(len(p) for p in pages)
    return PdfPreviewOut(units=total_units, unmatched=unmatched, page_count=len(pages))


@router.post("")
def generate_pdf(body: PdfLayoutIn) -> Response:
    """Returns the PDF as a real file response — the concrete fix for
    st.download_button silently doing nothing inside Tauri's webview
    (a known WKWebView gap around the HTML download attribute). A client
    fetch() -> blob() -> <a download> click has none of that gap."""
    layout, pages, _unmatched = _prepare(body)
    if not pages:
        raise HTTPException(status_code=400, detail="Nothing to print — no matched cards.")
    pdf_bytes = build_pdf(
        pages,
        layout=layout,
        export_dpi=body.export_dpi,
        show_cut_lines=body.show_cut_lines,
    )
    filename = f"{_slugify(body.project_name or body.project_tag)}.pdf"
    return Response(
        content=pdf_bytes,
        media_type="application/pdf",
        headers={"Content-Disposition": f'attachment; filename="{filename}"'},
    )
