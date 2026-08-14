from __future__ import annotations

import base64
import io
import re
import threading

from fastapi import APIRouter, HTTPException, Response
from PIL import Image

from proxy_scaler import db
from proxy_scaler.api.deps import get_db_path
from proxy_scaler.api.schemas import (
    DeckEntryIn,
    PdfJobIn,
    PdfJobOut,
    PdfJobStatusOut,
    PdfLayoutIn,
    PdfPagePreviewOut,
    PdfPageSlotOut,
    PdfPreviewOut,
)
from proxy_scaler.decklist import DeckEntry
from proxy_scaler import pdf_jobs
from proxy_scaler.pdf_jobs import PdfRenderCanceled
from proxy_scaler.pdf_layout import (
    CARD_HEIGHT_MM,
    CARD_WIDTH_MM,
    MM_PER_IN,
    PageLayout,
    add_bleed,
    build_pdf,
    expand_print_slots,
    match_quantities,
    paginate,
    resolve_page_layout,
    unique_image_count,
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


def _prepare(
    body: PdfLayoutIn,
) -> tuple[PageLayout, list[list[FaceResult]], list[str], list[str]]:
    if not body.entries:
        raise HTTPException(status_code=400, detail="No cards to print.")
    db_path = get_db_path()
    raw_items = db.list_gallery_items(body.project_tag, db_path=db_path)
    items = [FaceResult.from_dict(d) for d in raw_items]
    entries = [_to_deck_entry(e) for e in body.entries]
    units, missing, missing_at_dpi = match_quantities(
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
    return layout, pages, missing, missing_at_dpi


@router.post("/preview", response_model=PdfPreviewOut)
def preview(body: PdfLayoutIn) -> PdfPreviewOut:
    _layout, pages, missing, missing_at_dpi = _prepare(body)
    total_units = sum(len(p) for p in pages)
    return PdfPreviewOut(
        units=total_units,
        missing=missing,
        page_count=len(pages),
        missing_at_dpi=missing_at_dpi,
    )


@router.post("/preview/page", response_model=PdfPagePreviewOut)
def preview_page(body: PdfLayoutIn) -> PdfPagePreviewOut:
    """Page-1-only visual layout preview: small (<=~50KB) thumbnails of
    each slot's *original* card art, base64-embedded directly in this one
    response — page-1-only bounds the payload to at most cols*rows
    thumbnails, which is trivially small, and avoids needing a dedicated
    file-serving route (FaceResult carries no gallery_item_id to route
    through gallery.py's existing /full,/original endpoints)."""
    layout, pages, _missing, _missing_at_dpi = _prepare(body)
    first_page = pages[0] if pages else []
    slots: list[PdfPageSlotOut] = []
    for face in first_page:
        thumb_path = ensure_original_thumbnail(face.original_path)
        data_url = None
        if thumb_path is not None:
            # add_bleed here (not baked into the cached thumbnail, which
            # is bleed-agnostic on purpose) is what makes the preview's
            # bleed slider actually extend the art outward instead of
            # just growing the CSS box it's stretched into — matching
            # build_pdf()'s real per-page bleed step. Cheap: this runs on
            # an already-small (~220px) thumbnail, at most cols*rows of
            # them per request.
            with Image.open(thumb_path) as thumb:
                preview_dpi = max(thumb.width, thumb.height) / (
                    max(CARD_WIDTH_MM, CARD_HEIGHT_MM) / MM_PER_IN
                )
                bled = add_bleed(thumb, dpi=preview_dpi, bleed_mm=body.bleed_mm)
            buf = io.BytesIO()
            bled.save(buf, format="JPEG", quality=85)
            data_url = "data:image/jpeg;base64," + base64.b64encode(buf.getvalue()).decode("ascii")
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
        bleed_mm=layout.bleed_mm,
        guide_width_pt=layout.guide_width_pt,
        guide_length_mm=layout.guide_length_mm,
        show_cut_lines=body.show_cut_lines,
        page_count=len(pages),
        slots=slots,
    )


@router.post("")
def generate_pdf(body: PdfLayoutIn) -> Response:
    """Returns the PDF as a real file response — the concrete fix for
    st.download_button silently doing nothing inside Tauri's webview
    (a known WKWebView gap around the HTML download attribute). A client
    fetch() -> blob() -> <a download> click has none of that gap."""
    layout, pages, _missing, _missing_at_dpi = _prepare(body)
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
    layout, pages, _missing, _missing_at_dpi = _prepare(body)
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


# --- Render jobs -----------------------------------------------------------
#
# The synchronous routes above stay as they are (CLI/scripted callers, and
# the existing API tests). The desktop client uses the job routes below
# instead: rendering a sheet costs ~0.7s per unique image, so a real deck
# spends tens of seconds before any bytes exist to send, and a single
# blocking POST leaves the UI with nothing to show for it. Here the render
# runs on its own thread and the client polls (completed, total).


def _run_render(job_id: str, *, pages, layout: PageLayout, body: PdfJobIn) -> None:
    """Render thread body. Owns the job's terminal state: every exit path
    (success, cancel, failure) marks the job, or the client would poll a
    "rendering" job forever."""

    def on_progress(completed: int, _total: int) -> None:
        if pdf_jobs.is_cancel_requested(job_id):
            raise PdfRenderCanceled()
        pdf_jobs.set_progress(job_id, completed)

    try:
        render = build_pdf_html if body.method == "html" else build_pdf
        kwargs = {} if body.method == "html" else {"show_cut_lines": body.show_cut_lines}
        pdf_bytes = render(
            pages,
            layout=layout,
            export_dpi=body.export_dpi,
            on_progress=on_progress,
            **kwargs,
        )
        pdf_jobs.finish(job_id, pdf_bytes)
    except PdfRenderCanceled:
        pdf_jobs.mark_canceled(job_id)
    except Exception as exc:  # noqa: BLE001 — must reach the client as a status
        pdf_jobs.fail(job_id, str(exc))


@router.post("/jobs", response_model=PdfJobOut, status_code=202)
def start_pdf_job(body: PdfJobIn) -> PdfJobOut:
    """Start a background render and return its id.

    _prepare() runs synchronously here on purpose: it's cheap (a DB read
    plus matching) and it's what raises the 400s for an empty/unprintable
    request, so those still land on this call rather than surfacing much
    later as a failed job the user has already started waiting on.
    """
    layout, pages, _missing, _missing_at_dpi = _prepare(body)
    if not pages:
        raise HTTPException(status_code=400, detail="Nothing to print — no matched cards.")
    # One render at a time: each finished job pins a whole PDF in memory
    # until it's fetched, and the client single-flights downloads anyway.
    if pdf_jobs.active_count() > 0:
        raise HTTPException(
            status_code=409, detail="A PDF is already being generated — wait for it to finish."
        )

    suffix = "-html" if body.method == "html" else ""
    filename = f"{_slugify(body.project_name or body.project_tag)}{suffix}.pdf"
    job = pdf_jobs.create_job(filename=filename, total=unique_image_count(pages))
    threading.Thread(
        target=_run_render,
        args=(job.id,),
        kwargs={"pages": pages, "layout": layout, "body": body},
        daemon=True,
    ).start()
    return PdfJobOut(job_id=job.id, total=job.total)


@router.get("/jobs/{job_id}", response_model=PdfJobStatusOut)
def pdf_job_status(job_id: str) -> PdfJobStatusOut:
    job = pdf_jobs.get(job_id)
    if job is None:
        raise HTTPException(status_code=404, detail="Unknown or expired PDF job.")
    return PdfJobStatusOut(
        status=job.status, completed=job.completed, total=job.total, error=job.error
    )


@router.post("/jobs/{job_id}/cancel", status_code=204)
def cancel_pdf_job(job_id: str) -> Response:
    if not pdf_jobs.request_cancel(job_id):
        raise HTTPException(status_code=404, detail="Unknown or expired PDF job.")
    return Response(status_code=204)


@router.get("/jobs/{job_id}/result")
def pdf_job_result(job_id: str) -> Response:
    """Hand over a finished render's bytes, evicting the job as it goes —
    this is a plain GET so the desktop client can point Rust's downloader
    straight at it (see main.rs), keeping a large PDF out of the webview.
    """
    job = pdf_jobs.get(job_id)
    if job is None:
        raise HTTPException(status_code=404, detail="Unknown or expired PDF job.")
    if job.status != pdf_jobs.DONE:
        raise HTTPException(
            status_code=409, detail=f"PDF job is not ready (status: {job.status})."
        )
    result = pdf_jobs.pop_result(job_id)
    if result is None:  # raced another fetch between the check and the pop
        raise HTTPException(status_code=404, detail="Unknown or expired PDF job.")
    filename, pdf_bytes = result
    return Response(
        content=pdf_bytes,
        media_type="application/pdf",
        headers={"Content-Disposition": f'attachment; filename="{filename}"'},
    )
