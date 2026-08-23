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

Images are copied byte-for-byte from disk: no cover-fit, no resize, no
bleed. Those are render-time concerns needing a concrete pixel box
(see pdf_layout.fit_cover); the stored files are the highest-fidelity
artifacts, and aspect correction is the consumer's job. ZIP_STORED for
the same reason — PNGs don't recompress, so deflate would only burn CPU.

Unlike the PDF (a ~0.7s-per-image render behind pdf_jobs), zipping is
disk-speed file copying, so this is one synchronous endpoint streaming a
FileResponse — no job registry, no polling.
"""

from __future__ import annotations

import os
import tempfile
import zipfile

from fastapi import APIRouter, HTTPException
from fastapi.responses import FileResponse
from starlette.background import BackgroundTask

from proxy_scaler import backs, db
from proxy_scaler.api.deps import get_db_path
from proxy_scaler.api.routers.pdf import _default_pdf_basename, _slugify, _to_deck_entry
from proxy_scaler.api.schemas import ExportFormatIn, ExportZipIn, ExportZipPreviewOut
from proxy_scaler.pdf_layout import PrintSlot, build_print_slots, match_quantities
from proxy_scaler.pipeline import FaceResult

router = APIRouter(prefix="/api/export", tags=["export"])


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
# copying never blocks the event loop.
@router.post("/zip")
def export_zip(body: ExportZipIn) -> FileResponse:
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
    width = max(3, len(str(len(slots))))

    # delete=False + BackgroundTask cleanup: the response outlives this
    # function, so the file must too — starlette unlinks it after the last
    # byte is sent (or the client disconnects).
    tmp = tempfile.NamedTemporaryFile(suffix=".zip", delete=False)
    try:
        with zipfile.ZipFile(tmp, "w", zipfile.ZIP_STORED) as archive:
            for i, slot in enumerate(slots, start=1):
                src = slot.front.out_path
                archive.write(src, f"{deck}/FRONT/{i:0{width}d}{src.suffix}")
            if paired:
                for i, slot in enumerate(slots, start=1):
                    src = slot.reverse.out_path if slot.reverse is not None else back_path
                    archive.write(src, f"{deck}/BACK/{i:0{width}d}{src.suffix}")
            elif back_path is not None:
                archive.write(back_path, f"{deck}/BACK/{1:0{width}d}{back_path.suffix}")
        tmp.close()
    except BaseException:
        tmp.close()
        os.unlink(tmp.name)
        raise

    return FileResponse(
        tmp.name,
        media_type="application/zip",
        filename=f"{deck}.zip",
        background=BackgroundTask(os.unlink, tmp.name),
    )
