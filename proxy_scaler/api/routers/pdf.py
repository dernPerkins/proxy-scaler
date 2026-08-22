from __future__ import annotations

import base64
import io
import re
import threading
from dataclasses import dataclass
from datetime import date
from pathlib import Path

from fastapi import APIRouter, HTTPException, Response
from PIL import Image

from proxy_scaler import db
from proxy_scaler.api.deps import get_db_path
from proxy_scaler.api.schemas import (
    DeckEntryIn,
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
from proxy_scaler import backs
from proxy_scaler.pdf_layout import (
    CARD_HEIGHT_MM,
    CARD_WIDTH_MM,
    MM_PER_IN,
    FlipEdge,
    GuideVisibility,
    PageLayout,
    PageOrder,
    PrintSlot,
    add_bleed,
    back_page_cells,
    build_pdf,
    build_print_slots,
    match_quantities,
    paginate,
    resolve_page_layout,
    unique_image_count,
)
from proxy_scaler.pipeline import FaceResult, ensure_original_thumbnail

router = APIRouter(prefix="/api/pdf", tags=["pdf"])


def _slugify(name: str) -> str:
    slug = re.sub(r"[^A-Za-z0-9._-]+", "_", name).strip("_")
    return slug or "proxy-scaler"


def _default_pdf_basename(today: date | None = None) -> str:
    """Filename stem for a PDF whose project has no name (an Unnamed
    Project). Never the project_tag: that's an opaque 32-char hex string,
    which is no name to hand a user saving a file. The desktop client
    builds the same string for its save-dialog default
    (pdfFilename.ts::defaultPdfBasename) so the offered name and the one
    in Content-Disposition agree — bar a remote generation host sitting on
    the other side of local midnight from the client, where the two dates
    can differ by a day."""
    return f"proxy-scaler-{(today or date.today()).isoformat()}"


def _to_deck_entry(e: DeckEntryIn) -> DeckEntry:
    return DeckEntry(
        quantity=e.quantity,
        name=e.name,
        set_code=e.set_code,
        collector_number=e.collector_number,
        raw_line=e.raw_line or e.name,
        scryfall_id=e.scryfall_id,
        lang=e.lang,
    )


@dataclass(frozen=True)
class PreparedRender:
    """Everything both the preview and the render need, resolved once.

    Bundled rather than returned as a widening tuple: back printing added
    four more things to carry, and a seven-element tuple is a bug waiting
    for someone to unpack it in the wrong order.
    """

    layout: PageLayout
    back_layout: PageLayout
    pages: list[list[PrintSlot]]
    missing: list[str]
    missing_at_dpi: list[str]
    back_image_path: Path | None
    back_image_not_upscaled: bool
    reverses_needing_back_image: int

    @property
    def missing_back_image(self) -> bool:
        """Back printing is on, at least one Reverse would take the Back
        Image, and there is no usable one. Only an error when a Reverse
        would actually come up empty — an all-double-faced sheet
        legitimately needs no Back Image."""
        return self.reverses_needing_back_image > 0 and self.back_image_path is None

    @property
    def total_page_count(self) -> int:
        return len(self.pages)


def _guides(body: PdfLayoutIn) -> GuideVisibility:
    return GuideVisibility(
        hide_card_guides_front=body.hide_card_guides_front,
        hide_page_guides_front=body.hide_page_guides_front,
        hide_card_guides_back=body.hide_card_guides_back,
        hide_page_guides_back=body.hide_page_guides_back,
    )


def _prepare(body: PdfLayoutIn) -> PreparedRender:
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

    def layout_with(offset_x: float, offset_y: float) -> PageLayout:
        return resolve_page_layout(
            page_w_mm=body.page_width_mm,
            page_h_mm=body.page_height_mm,
            cols=body.cols,
            rows=body.rows,
            bleed_mm=body.bleed_mm,
            spacing_x_mm=body.spacing_x_mm,
            spacing_y_mm=body.spacing_y_mm,
            offset_x_mm=offset_x,
            offset_y_mm=offset_y,
            guide_width_pt=body.guide_width_pt,
            guide_length_mm=body.guide_length_mm,
        )

    layout = layout_with(body.offset_x_mm, body.offset_y_mm)
    # Back Pages carry their own offset ON TOP of nothing — not on top of
    # the front's. The two are independent calibrations of two physical
    # passes through the printer, and adding them would make nudging the
    # fronts silently move the backs too.
    back_layout = layout_with(body.back_offset_x_mm, body.back_offset_y_mm)

    # Pairing only means anything while back printing is on: with it off,
    # a Back Face has no Reverse to live on and stays its own card, which
    # is exactly the historical behaviour.
    slots = build_print_slots(
        units, pair_back_faces=body.back_printing and body.back_faces_as_reverse
    )
    pages = paginate(slots, layout.cards_per_page)

    back_image_path: Path | None = None
    back_image_not_upscaled = False
    reverses_needing_back_image = 0
    if body.back_printing:
        reverses_needing_back_image = sum(
            1 for page in pages for slot in page if slot.reverse is None
        )
        if reverses_needing_back_image and body.back_image_hash:
            try:
                back_image_path, back_image_not_upscaled = backs.resolve_print_source(
                    body.back_image_hash,
                    preferred_model=body.preferred_model,
                    db_path=db_path,
                )
            except backs.BackImageError as exc:
                raise HTTPException(status_code=400, detail=str(exc)) from exc

    return PreparedRender(
        layout=layout,
        back_layout=back_layout,
        pages=pages,
        missing=missing,
        missing_at_dpi=missing_at_dpi,
        back_image_path=back_image_path,
        back_image_not_upscaled=back_image_not_upscaled,
        reverses_needing_back_image=reverses_needing_back_image,
    )


def _render_kwargs(body: PdfLayoutIn, prepared: PreparedRender) -> dict:
    """The back-printing half of build_pdf's arguments, shared by the
    synchronous route and the job thread so the two can't drift."""
    return dict(
        guides=_guides(body),
        back_printing=body.back_printing,
        back_layout=prepared.back_layout,
        back_image_path=prepared.back_image_path,
        back_image_includes_bleed=body.back_image_includes_bleed,
        flip_edge=FlipEdge(body.flip_edge.value),
        page_order=PageOrder(body.page_order.value),
    )


def _guard_printable(prepared: PreparedRender) -> None:
    """Refuse a render that would produce blank Reverses. Stated as an
    error rather than silently printing empty backs: the user finds out
    either way, but this way it costs no cardstock."""
    if not prepared.pages:
        raise HTTPException(status_code=400, detail="Nothing to print — no matched cards.")
    if prepared.missing_back_image:
        raise HTTPException(
            status_code=400,
            detail=(
                "Back printing is on, but no back image is available for "
                f"{prepared.reverses_needing_back_image} card(s). Pick one on the "
                "Backs tab, or turn back printing off."
            ),
        )


@router.post("/preview", response_model=PdfPreviewOut)
def preview(body: PdfLayoutIn) -> PdfPreviewOut:
    prepared = _prepare(body)
    total_units = sum(len(p) for p in prepared.pages)
    # page_count stays the number of Front Pages — the count of sheets you
    # feed the printer — while total_page_count is what the PDF actually
    # contains. Back printing doubles the second and leaves the first
    # alone, and conflating them would double the "slots left on your last
    # page" arithmetic the client does from it.
    return PdfPreviewOut(
        units=total_units,
        missing=prepared.missing,
        page_count=len(prepared.pages),
        missing_at_dpi=prepared.missing_at_dpi,
        reverses_needing_back_image=prepared.reverses_needing_back_image,
        missing_back_image=prepared.missing_back_image,
        back_image_not_upscaled=prepared.back_image_not_upscaled,
        total_page_count=len(prepared.pages) * (2 if body.back_printing else 1),
    )


def _preview_cell(
    cell: PrintSlot | None, *, is_back: bool, back_image_path: Path | None
) -> tuple[FaceResult | None, bool]:
    """What one grid position shows in the page preview: (face, is_back_image).
    (None, False) is a genuinely empty position."""
    if cell is None:
        return None, False
    if not is_back:
        return cell.front, False
    if cell.reverse is not None:
        return cell.reverse, False
    return None, back_image_path is not None


def _back_image_thumbnail(path: Path | None, *, bleed_mm: float) -> str | None:
    """A small preview of the Back Image, built on the fly.

    Unlike a card, a Back Image has no cached original thumbnail to reuse
    (nothing generated it), so this decodes and downsamples the stored
    original each call. Bounded work: it happens at most once per preview
    request no matter how many Reverses the Back Image fills, because
    every one of them shows the same picture.
    """
    if path is None or not path.is_file():
        return None
    try:
        with Image.open(path) as raw:
            thumb = raw.convert("RGB")
            thumb.thumbnail((220, 220), Image.Resampling.LANCZOS)
            preview_dpi = max(thumb.width, thumb.height) / (
                max(CARD_WIDTH_MM, CARD_HEIGHT_MM) / MM_PER_IN
            )
            bled = add_bleed(thumb, dpi=preview_dpi, bleed_mm=bleed_mm)
        buf = io.BytesIO()
        bled.save(buf, format="JPEG", quality=85)
        return "data:image/jpeg;base64," + base64.b64encode(buf.getvalue()).decode("ascii")
    except Exception:  # noqa: BLE001 — a preview thumbnail is never worth a 500
        return None


@router.post("/preview/page", response_model=PdfPagePreviewOut)
def preview_page(body: PdfLayoutIn) -> PdfPagePreviewOut:
    """Page-1-only visual layout preview: small (<=~50KB) thumbnails of
    each slot's *original* card art, base64-embedded directly in this one
    response — page-1-only bounds the payload to at most cols*rows
    thumbnails, which is trivially small, and avoids needing a dedicated
    file-serving route (FaceResult carries no gallery_item_id to route
    through gallery.py's existing /full,/original endpoints)."""
    prepared = _prepare(body)
    layout = prepared.back_layout if body.preview_back_page else prepared.layout
    pages = prepared.pages
    # The Back Page preview is the cheapest way to catch a wrong Flip Edge
    # before a sheet of cardstock pays for it, so it renders the real
    # mirrored grid rather than a mirrored-looking approximation: same
    # back_page_cells() the renderer uses, same full-grid mirroring on a
    # partial page.
    if body.preview_back_page and pages:
        # Full grid, empty positions included: a Back Page's cells are
        # placed by mirrored index, so dropping the empty ones would shift
        # every later card and the preview would stop matching the sheet.
        cells = back_page_cells(
            pages[0], layout=prepared.layout, flip_edge=FlipEdge(body.flip_edge.value)
        )
    elif pages:
        # Fronts fill left-to-right from position 0, so the occupied
        # prefix IS the layout — no padding, keeping this response the
        # same shape it has always had for front previews.
        cells = list(pages[0])
    else:
        cells = []

    slots: list[PdfPageSlotOut] = []
    for cell in cells:
        # Three states per grid position, not two: a card, the Back Image
        # filling a Reverse that has no Back Face, or a position no card
        # occupies at all (a partial last page). The empty one has to be
        # rendered as an empty cell rather than skipped, or every slot
        # after it shifts and the mirrored preview stops matching the
        # sheet it is previewing.
        face, is_back_image = _preview_cell(
            cell, is_back=body.preview_back_page, back_image_path=prepared.back_image_path
        )
        if face is None and not is_back_image:
            slots.append(PdfPageSlotOut(card_name="", face_label=None, model="", dpi=0))
            continue
        if is_back_image:
            slots.append(
                PdfPageSlotOut(
                    card_name="Card back",
                    face_label=None,
                    model="",
                    dpi=0,
                    thumbnail_data_url=_back_image_thumbnail(
                        prepared.back_image_path, bleed_mm=body.bleed_mm
                    ),
                    is_back_image=True,
                )
            )
            continue
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
        hide_card_guides=(
            body.hide_card_guides_back if body.preview_back_page else body.hide_card_guides_front
        ),
        hide_page_guides=(
            body.hide_page_guides_back if body.preview_back_page else body.hide_page_guides_front
        ),
        page_count=len(pages),
        slots=slots,
    )


@router.post("")
def generate_pdf(body: PdfLayoutIn) -> Response:
    """Returns the PDF as a real file response — the concrete fix for
    st.download_button silently doing nothing inside Tauri's webview
    (a known WKWebView gap around the HTML download attribute). A client
    fetch() -> blob() -> <a download> click has none of that gap."""
    prepared = _prepare(body)
    _guard_printable(prepared)
    pdf_bytes = build_pdf(
        prepared.pages,
        layout=prepared.layout,
        export_dpi=body.export_dpi,
        **_render_kwargs(body, prepared),
    )
    filename = f"{_slugify(body.project_name or _default_pdf_basename())}.pdf"
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


def _run_render(job_id: str, *, prepared: PreparedRender, body: PdfLayoutIn) -> None:
    """Render thread body. Owns the job's terminal state: every exit path
    (success, cancel, failure) marks the job, or the client would poll a
    "rendering" job forever."""

    def on_progress(completed: int, _total: int) -> None:
        if pdf_jobs.is_cancel_requested(job_id):
            raise PdfRenderCanceled()
        pdf_jobs.set_progress(job_id, completed)

    try:
        pdf_bytes = build_pdf(
            prepared.pages,
            layout=prepared.layout,
            export_dpi=body.export_dpi,
            on_progress=on_progress,
            **_render_kwargs(body, prepared),
        )
        pdf_jobs.finish(job_id, pdf_bytes)
    except PdfRenderCanceled:
        pdf_jobs.mark_canceled(job_id)
    except Exception as exc:  # noqa: BLE001 — must reach the client as a status
        pdf_jobs.fail(job_id, str(exc))


@router.post("/jobs", response_model=PdfJobOut, status_code=202)
def start_pdf_job(body: PdfLayoutIn) -> PdfJobOut:
    """Start a background render and return its id.

    _prepare() runs synchronously here on purpose: it's cheap (a DB read
    plus matching) and it's what raises the 400s for an empty/unprintable
    request, so those still land on this call rather than surfacing much
    later as a failed job the user has already started waiting on.
    """
    prepared = _prepare(body)
    _guard_printable(prepared)
    # One render at a time: each finished job pins a whole PDF in memory
    # until it's fetched, and the client single-flights downloads anyway.
    if pdf_jobs.active_count() > 0:
        raise HTTPException(
            status_code=409, detail="A PDF is already being generated — wait for it to finish."
        )

    filename = f"{_slugify(body.project_name or _default_pdf_basename())}.pdf"
    job = pdf_jobs.create_job(
        filename=filename,
        total=unique_image_count(
            prepared.pages,
            back_printing=body.back_printing,
            back_image_path=prepared.back_image_path,
        ),
    )
    threading.Thread(
        target=_run_render,
        args=(job.id,),
        kwargs={"prepared": prepared, "body": body},
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
