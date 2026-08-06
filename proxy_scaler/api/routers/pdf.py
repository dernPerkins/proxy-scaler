from __future__ import annotations

import base64
import re

from fastapi import APIRouter, HTTPException, Response

from proxy_scaler import db
from proxy_scaler.api.deps import get_db_path
from proxy_scaler.api.schemas import (
    DeckEntryIn,
    PdfLayoutIn,
    PdfPagePreviewOut,
    PdfPageSlotOut,
    PdfPreviewOut,
)
from proxy_scaler.decklist import DeckEntry
from proxy_scaler.pdf_layout import (
    PageLayout,
    build_pdf,
    expand_print_slots,
    match_quantities,
    paginate,
    resolve_page_layout,
)
from proxy_scaler.pdf_html import WeasyPrintUnavailable, build_pdf_html
from proxy_scaler.pipeline import FaceResult, ensure_original_thumbnail

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


@router.post("/preview/page", response_model=PdfPagePreviewOut)
def preview_page(body: PdfLayoutIn) -> PdfPagePreviewOut:
    """Page-1-only visual layout preview: small (<=~50KB) thumbnails of
    each slot's *original* card art, base64-embedded directly in this one
    response — page-1-only bounds the payload to at most cols*rows
    thumbnails, which is trivially small, and avoids needing a dedicated
    file-serving route (FaceResult carries no gallery_item_id to route
    through gallery.py's existing /full,/original endpoints)."""
    layout, pages, _unmatched = _prepare(body)
    first_page = pages[0] if pages else []
    slots: list[PdfPageSlotOut] = []
    for face in first_page:
        thumb_path = ensure_original_thumbnail(face.original_path)
        data_url = (
            "data:image/jpeg;base64," + base64.b64encode(thumb_path.read_bytes()).decode("ascii")
            if thumb_path is not None
            else None
        )
        slots.append(
            PdfPageSlotOut(
                card_name=face.card_name,
                face_label=face.face_label,
                model=face.model,
                dpi=face.dpi,
                thumbnail_data_url=data_url,
            )
        )
    return PdfPagePreviewOut(
        page_w_mm=layout.page_w_mm,
        page_h_mm=layout.page_h_mm,
        cols=layout.cols,
        rows=layout.rows,
        margin_x_mm=layout.margin_x_mm,
        margin_y_mm=layout.margin_y_mm,
        cell_w_mm=layout.cell_w_mm,
        cell_h_mm=layout.cell_h_mm,
        bled_card_w_mm=layout.bled_card_w_mm,
        bled_card_h_mm=layout.bled_card_h_mm,
        page_count=len(pages),
        slots=slots,
    )


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


@router.post("/html")
def generate_pdf_html(body: PdfLayoutIn) -> Response:
    """Alternate HTML->PDF pipeline via WeasyPrint (see pdf_html.py) — a
    second rendering path for comparing against the default fpdf2 one
    above. 503s with a clear message rather than crashing when weasyprint
    isn't installed on this server (see pyproject.toml's html-pdf extra)."""
    layout, pages, _unmatched = _prepare(body)
    if not pages:
        raise HTTPException(status_code=400, detail="Nothing to print — no matched cards.")
    try:
        pdf_bytes = build_pdf_html(pages, layout=layout, export_dpi=body.export_dpi)
    except WeasyPrintUnavailable as exc:
        raise HTTPException(status_code=503, detail=str(exc)) from exc
    filename = f"{_slugify(body.project_name or body.project_tag)}-html.pdf"
    return Response(
        content=pdf_bytes,
        media_type="application/pdf",
        headers={"Content-Disposition": f'attachment; filename="{filename}"'},
    )
