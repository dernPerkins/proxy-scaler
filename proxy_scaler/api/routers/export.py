"""ZIP export of a project's generated images — the non-PDF way out.

Two formats, both built from the same matching pipeline the PDF uses
(match_quantities -> build_print_slots), so what lands in a ZIP is always
exactly what would have landed on a sheet:

- "default": every unique matched face once under FRONT/, plus the
  project's Selected Back (if one is synced) as the single BACK/ entry.
  Quantities are not expanded — this is an image dump, not a print run.
- "tcgplaytest": the vendor's paired layout. One FRONT/NNN + BACK/NNN
  file pair per physical copy (quantities expanded), paired by natural
  filename order with equal counts — a card's own Back Face when it has
  one, the Selected Back otherwise.

Two output options decide whether the stored files are copied or
re-rendered (see _needs_render):

- image_format "png" with no bleed ships the stored files byte-for-byte:
  no cover-fit, no resize, source suffix kept. The stored files are the
  highest-fidelity artifacts, and aspect correction is the consumer's
  job. "jpg" re-encodes every image at the PDF's JPEG quality — JPEG has
  no alpha, so the rounded-corner transparency is flattened first, the
  same way the PDF does it.
- with_bleed edge-extends a bleed border (bleed_mm per side) onto every
  image at its own native DPI, exactly like a card on a PDF sheet
  (pdf_layout.add_bleed). MakePlayingCards.com and similar vendors expect
  the bleed to be in the file: an upload without one is stretched to fill
  their bled box, which is the "my cards came out blown up" report this
  option exists to answer. Every entry then carries the chosen format's
  suffix, since the bytes are no longer the stored file's.

The verbatim path is disk-speed file copying, so the synchronous endpoint
just streams a FileResponse. Everything else decodes and re-encodes each
unique image — ~1s per 1200 DPI card for a bled PNG — so the desktop
client drives the job routes instead: the same pdf_jobs registry and poll
loop as the PDF, with the finished archive held as a temp file rather
than bytes (a bled 1200 DPI deck can run to a gigabyte). ZIP_STORED
throughout — PNGs and JPEGs don't recompress, so deflate would only burn
CPU.
"""

from __future__ import annotations

import os
import tempfile
import threading
import zipfile
from dataclasses import dataclass
from pathlib import Path
from typing import Callable

from fastapi import APIRouter, HTTPException
from fastapi.responses import FileResponse, Response
from PIL import Image
from starlette.background import BackgroundTask

from proxy_scaler import backs, db, pdf_jobs
from proxy_scaler.api.deps import get_db_path
from proxy_scaler.api.routers.pdf import _default_pdf_basename, _slugify, _to_deck_entry
from proxy_scaler.api.schemas import (
    ExportFormatIn,
    ExportImageFormatIn,
    ExportZipIn,
    ExportZipPreviewOut,
    PdfJobOut,
    PdfJobStatusOut,
)
from proxy_scaler.dpi import dpi_at_card_size
from proxy_scaler.pdf_jobs import PdfRenderCanceled
from proxy_scaler.pdf_layout import (
    _JPEG_QUALITY,
    PrintSlot,
    _bled_card,
    build_print_slots,
    fit_bled_image,
    flatten_corner_alpha,
    match_quantities,
    render_back_image,
)
from proxy_scaler import customs
from proxy_scaler.pipeline import FaceResult

router = APIRouter(prefix="/api/export", tags=["export"])

# PNG encode is the dominant cost of a bled export (~0.1s to bleed a 1200
# DPI card, 0.8s to encode it at this level vs 2.5s at Pillow's default 6),
# and the size difference on card art is modest. The archive is stored,
# not deflated, so nothing downstream re-compresses either.
_PNG_COMPRESS_LEVEL = 1


def _prepare_slots(body: ExportZipIn) -> tuple[list[PrintSlot], list[PrintSlot], list[str], list[str]]:
    """The _prepare subset the ZIP formats need: matched slots, no
    layout/pagination. Returns (default_slots, paired_slots, missing,
    missing_at_dpi) — both formats from one matching pass, since only the
    slot-building step differs.

    default_slots is deduped to one slot per unique source image,
    first-seen order: the default format ships each face once regardless
    of quantity. paired_slots keeps the full per-copy expansion the
    vendor's natural-order pairing needs to express quantity at all.
    """
    if not body.entries:
        raise HTTPException(status_code=400, detail="No cards to export.")
    db_path = get_db_path()
    raw_items = db.list_gallery_items(body.project_tag, db_path=db_path)
    items = [FaceResult.from_dict(d) for d in raw_items]
    customs.attach_bleed(items)
    entries = [_to_deck_entry(e) for e in body.entries]
    units, missing, missing_at_dpi = match_quantities(
        entries,
        items,
        # Same forcing as pdf.py::_prepare — with use_originals there's
        # exactly one variant per face, so the preferred pair is moot.
        preferred_dpi=None if body.use_originals else body.preferred_dpi,
        preferred_model=None if body.use_originals else body.preferred_model,
        use_originals=body.use_originals,
    )

    seen: set[str] = set()
    default_slots: list[PrintSlot] = []
    for slot in build_print_slots(units, pair_back_faces=False):
        key = str(slot.front.out_path)
        if key in seen:
            continue
        seen.add(key)
        default_slots.append(slot)

    paired_slots = build_print_slots(units, pair_back_faces=True)
    return default_slots, paired_slots, missing, missing_at_dpi


def _resolve_back(body: ExportZipIn):
    """Path of the Selected Back on this server, or None when the request
    names none. A hash that's invalid or not synced here is a loud 400
    rather than a silently back-less ZIP — the client believes a back is
    selected, so shipping without one would be a lie."""
    if not body.back_image_hash:
        return None
    try:
        path = backs.resolve_print_source(body.back_image_hash)
    except backs.BackImageError as exc:
        raise HTTPException(status_code=400, detail=str(exc)) from exc
    if path is None:
        raise HTTPException(
            status_code=400,
            detail="The selected back image is not synced to this server yet.",
        )
    return path


@dataclass(frozen=True)
class PreparedExport:
    """Everything an archive needs that comes from validating the request:
    the slots the chosen format ships, the Selected Back's path (None when
    none is selected), and the deck folder / filename stem."""

    slots: list[PrintSlot]
    paired: bool
    back_path: Path | None
    deck: str


def _prepare_export(body: ExportZipIn) -> PreparedExport:
    """Validate and match, raising the 400s a caller wants on the request
    itself rather than later from inside a render thread."""
    default_slots, paired_slots, _missing, _missing_at_dpi = _prepare_slots(body)
    paired = body.format is ExportFormatIn.TCGPLAYTEST
    slots = paired_slots if paired else default_slots
    if not slots:
        raise HTTPException(status_code=400, detail="Nothing to export — no matched images.")

    back_path = _resolve_back(body)
    if paired and back_path is None and any(s.reverse is None for s in slots):
        raise HTTPException(
            status_code=400,
            detail=(
                "The TCGPlaytest format pairs every front with a back — "
                "select a back image for this project first."
            ),
        )
    deck = _slugify(body.project_name or _default_pdf_basename())
    return PreparedExport(slots=slots, paired=paired, back_path=back_path, deck=deck)


def _needs_render(body: ExportZipIn) -> bool:
    """False is the verbatim path: the stored files, byte-for-byte."""
    return body.with_bleed or body.image_format is ExportImageFormatIn.JPG


def _unique_sources(prepared: PreparedExport) -> list[Path]:
    """Every distinct file the archive draws on, first-seen order — the
    job's progress denominator and the render cache's key set. The
    Selected Back counts once however many BACK/ entries it fills."""
    sources: dict[Path, None] = {}
    for slot in prepared.slots:
        sources.setdefault(slot.front.out_path)
    if prepared.paired:
        for slot in prepared.slots:
            if slot.reverse is not None:
                sources.setdefault(slot.reverse.out_path)
            elif prepared.back_path is not None:
                sources.setdefault(prepared.back_path)
    elif prepared.back_path is not None:
        sources.setdefault(prepared.back_path)
    return list(sources)


def _render_entry(source: Path, face: FaceResult | None, *, body: ExportZipIn) -> Image.Image:
    """One image as the archive will carry it, decoded and opaque. `face`
    is None for the Selected Back, which has no generated-image record.

    With bleed this is exactly the PDF's per-card treatment at the image's
    own DPI — a face's recorded dpi (so no resize ever happens), or for
    the back the DPI its pixel size implies at card size. Without bleed
    (only the JPG conversion reaches here) it's the corner flatten alone,
    which a card needs before losing its alpha; a back has no rounded-
    corner contract and is simply made opaque.
    """
    if body.with_bleed:
        if face is not None:
            return _bled_card(face, export_dpi=face.dpi, bleed_mm=body.bleed_mm)
        with Image.open(source) as raw:
            back_dpi = max(1, round(dpi_at_card_size(*raw.size)))
        return render_back_image(
            source,
            export_dpi=back_dpi,
            bleed_mm=body.bleed_mm,
            includes_bleed=body.back_image_includes_bleed,
            image_bleed_mm=body.back_image_bleed_mm,
        )
    with Image.open(source) as raw:
        if face is not None and face.custom_bleed_mm > 0:
            # A pre-bled Custom Image without bleed asked for: hand over
            # the trim-sized card, not the whole bled file.
            return fit_bled_image(
                raw.convert("RGB"),
                image_bleed_mm=face.custom_bleed_mm,
                export_dpi=face.dpi,
                bleed_mm=0.0,
            )
        if face is not None:
            return flatten_corner_alpha(raw.convert("RGBA")).convert("RGB")
        return raw.convert("RGB")


def _encode(image: Image.Image, image_format: ExportImageFormatIn, dest: Path) -> None:
    if image_format is ExportImageFormatIn.JPG:
        image.save(dest, format="JPEG", quality=_JPEG_QUALITY)
    else:
        image.save(dest, format="PNG", compress_level=_PNG_COMPRESS_LEVEL)


def _write_zip(
    prepared: PreparedExport,
    *,
    body: ExportZipIn,
    on_progress: Callable[[int, int], None] | None = None,
) -> Path:
    """Build the archive into a temp file and return its path. The caller
    owns the file from here (and deletes it once streamed).

    On the verbatim path every entry is the stored file. Otherwise each
    unique source is rendered once into a scratch directory and that file
    is archived under every entry that uses it — the tcgplaytest layout
    repeats the Selected Back per copy, and a bytes cache of bled 1200 DPI
    PNGs would be the whole archive again in RAM. So a rendering export
    transiently uses about twice the archive's size on disk; the scratch
    files go with the `with` block, before this returns.

    `on_progress(completed, total)` fires after each unique render, and
    may raise to abort (pdf_jobs.PdfRenderCanceled does) — the half-built
    archive is deleted on the way out and nothing is returned.
    """
    slots = prepared.slots
    deck = prepared.deck
    width = max(3, len(str(len(slots))))
    render = _needs_render(body)
    rendered_suffix = f".{body.image_format.value}"
    total = len(_unique_sources(prepared))

    # delete=False + explicit cleanup: on success the caller streams the
    # file and unlinks it afterwards; on any failure it's gone before the
    # exception leaves.
    tmp = tempfile.NamedTemporaryFile(suffix=".zip", delete=False)
    try:
        with (
            tempfile.TemporaryDirectory(prefix="proxy-scaler-export-") as scratch,
            zipfile.ZipFile(tmp, "w", zipfile.ZIP_STORED) as archive,
        ):
            rendered: dict[Path, Path] = {}

            def entry(source: Path, face: FaceResult | None) -> tuple[Path, str]:
                """(file to archive, suffix its entry name carries)."""
                if not render:
                    return source, source.suffix
                out = rendered.get(source)
                if out is None:
                    out = Path(scratch) / f"{len(rendered):05d}{rendered_suffix}"
                    _encode(_render_entry(source, face, body=body), body.image_format, out)
                    rendered[source] = out
                    if on_progress is not None:
                        on_progress(len(rendered), total)
                return out, rendered_suffix

            for i, slot in enumerate(slots, start=1):
                path, suffix = entry(slot.front.out_path, slot.front)
                archive.write(path, f"{deck}/FRONT/{i:0{width}d}{suffix}")
            if prepared.paired:
                for i, slot in enumerate(slots, start=1):
                    if slot.reverse is not None:
                        path, suffix = entry(slot.reverse.out_path, slot.reverse)
                    else:
                        assert prepared.back_path is not None  # _prepare_export guarantees
                        path, suffix = entry(prepared.back_path, None)
                    archive.write(path, f"{deck}/BACK/{i:0{width}d}{suffix}")
            elif prepared.back_path is not None:
                path, suffix = entry(prepared.back_path, None)
                archive.write(path, f"{deck}/BACK/{1:0{width}d}{suffix}")
        tmp.close()
    except BaseException:
        tmp.close()
        os.unlink(tmp.name)
        raise
    return Path(tmp.name)


@router.post("/zip/preview", response_model=ExportZipPreviewOut)
def export_zip_preview(body: ExportZipIn) -> ExportZipPreviewOut:
    default_slots, paired_slots, missing, missing_at_dpi = _prepare_slots(body)
    return ExportZipPreviewOut(
        fronts=len(default_slots),
        paired_fronts=len(paired_slots),
        missing=missing,
        missing_at_dpi=missing_at_dpi,
        reverses_needing_back_image=sum(1 for s in paired_slots if s.reverse is None),
    )


# Sync def on purpose: FastAPI runs it in the threadpool, so the file
# copying (or rendering) never blocks the event loop. Honours every option
# the job routes do — this is the CLI/scripted route and what the desktop
# client falls back to against a server without the job routes — but with
# nothing to show while a rendering export works.
@router.post("/zip")
def export_zip(body: ExportZipIn) -> FileResponse:
    prepared = _prepare_export(body)
    path = _write_zip(prepared, body=body)
    # The response outlives this function, so the file must too —
    # starlette unlinks it after the last byte is sent (or the client
    # disconnects).
    return FileResponse(
        path,
        media_type="application/zip",
        filename=f"{prepared.deck}.zip",
        background=BackgroundTask(os.unlink, path),
    )


# --- Export jobs -----------------------------------------------------------
#
# Same shape as pdf.py's render jobs, sharing its registry: start, poll,
# cancel, fetch-from-a-plain-GET (so Rust can stream the archive to disk
# without it entering the webview). A verbatim export finishes at disk
# speed and the client just sees "done" on its first poll — one client
# code path, whatever the options.


def _run_export(job_id: str, *, prepared: PreparedExport, body: ExportZipIn) -> None:
    """Render thread body. Owns the job's terminal state: every exit path
    (success, cancel, failure) marks the job, or the client would poll a
    "rendering" job forever."""

    def on_progress(completed: int, _total: int) -> None:
        if pdf_jobs.is_cancel_requested(job_id):
            raise PdfRenderCanceled()
        pdf_jobs.set_progress(job_id, completed)

    try:
        path = _write_zip(prepared, body=body, on_progress=on_progress)
        if not pdf_jobs.finish_file(job_id, path):
            # The job was evicted under us (a cancel that raced the final
            # write, or a TTL sweep) — nobody will ever fetch this.
            os.unlink(path)
    except PdfRenderCanceled:
        pdf_jobs.mark_canceled(job_id)
    except Exception as exc:  # noqa: BLE001 — must reach the client as a status
        pdf_jobs.fail(job_id, str(exc))


@router.post("/zip/jobs", response_model=PdfJobOut, status_code=202)
def start_export_job(body: ExportZipIn) -> PdfJobOut:
    """Start a background export and return its id. Validation runs here,
    synchronously, so an empty or back-less request 400s on the click
    rather than as a failed job the user has already started waiting on."""
    prepared = _prepare_export(body)
    # One render at a time, PDF or ZIP: each finished job pins its result
    # until fetched, and the client single-flights downloads anyway.
    if pdf_jobs.active_count() > 0:
        raise HTTPException(
            status_code=409, detail="An export is already being generated — wait for it to finish."
        )
    job = pdf_jobs.create_job(
        filename=f"{prepared.deck}.zip", total=len(_unique_sources(prepared))
    )
    threading.Thread(
        target=_run_export,
        args=(job.id,),
        kwargs={"prepared": prepared, "body": body},
        daemon=True,
    ).start()
    return PdfJobOut(job_id=job.id, total=job.total)


@router.get("/zip/jobs/{job_id}", response_model=PdfJobStatusOut)
def export_job_status(job_id: str) -> PdfJobStatusOut:
    job = pdf_jobs.get(job_id)
    if job is None:
        raise HTTPException(status_code=404, detail="Unknown or expired export job.")
    return PdfJobStatusOut(
        status=job.status, completed=job.completed, total=job.total, error=job.error
    )


@router.post("/zip/jobs/{job_id}/cancel", status_code=204)
def cancel_export_job(job_id: str) -> Response:
    if not pdf_jobs.request_cancel(job_id):
        raise HTTPException(status_code=404, detail="Unknown or expired export job.")
    return Response(status_code=204)


@router.get("/zip/jobs/{job_id}/result")
def export_job_result(job_id: str) -> FileResponse:
    """Stream a finished export, evicting the job as it goes. Ownership of
    the temp file moves from the registry to this response, which unlinks
    it after the last byte."""
    job = pdf_jobs.get(job_id)
    if job is None:
        raise HTTPException(status_code=404, detail="Unknown or expired export job.")
    if job.status != pdf_jobs.DONE:
        raise HTTPException(
            status_code=409, detail=f"Export job is not ready (status: {job.status})."
        )
    result = pdf_jobs.pop_result_file(job_id)
    if result is None:  # raced another fetch between the check and the pop
        raise HTTPException(status_code=404, detail="Unknown or expired export job.")
    filename, path = result
    return FileResponse(
        path,
        media_type="application/zip",
        filename=filename,
        background=BackgroundTask(os.unlink, path),
    )
