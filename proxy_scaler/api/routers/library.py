"""Library upscaling — upscale a Custom Image or a Back Image straight
from its library tab, with no project involved.

The libraries belong to the machine, not to a project, so this work is
queued under dpi.LIBRARY_TAG rather than whichever project is open. The
registry row that results is global: a project the image later belongs to
adopts it on its normal reconcile (db.adopt_gallery_items), and the PDF and
ZIP paths merge library-made variants into a project's gallery by identity
(routers/pdf.py::_prepare, routers/export.py::_prepare_slots). Status is
likewise read by identity across every tag, so the tab's badges show what
any project ever made for the image too.

Customs deliberately reuse services.generation.enqueue_decklist_entries
with a one-line decklist: source registration, the "only targets the
upload doesn't reach" rule (dpi.custom_upscale_targets), in-flight dedup
and registry-first skipping are all the same code the Decklist's Generate
runs, just under the library tag and with the tab's own mode instead of
the project's custom_upscale setting. Backs go through
enqueue_back_upscale, the same rules on an image no decklist names.
"""

from __future__ import annotations

from pathlib import Path

from fastapi import APIRouter, HTTPException

from proxy_scaler import backs, customs, db
from proxy_scaler.api.deps import get_card_db_path, get_db_path
from proxy_scaler.api.routers.gallery import _gallery_item_out
from proxy_scaler.api.routers.generation import _task_out
from proxy_scaler.api.schemas import GenerateOut, LibraryStatusOut, LibraryUpscaleIn
from proxy_scaler.decklist import DeckEntry
from proxy_scaler.dpi import LIBRARY_TAG
from proxy_scaler.services import generation as generation_service

router = APIRouter(prefix="/api/library", tags=["library"])

# What a task and its output file are called when the client sends no
# label. Only ever a display name: identity is the content hash.
_DEFAULT_LABEL = "Custom card"


def _checked_custom(content_hash: str) -> str:
    try:
        return customs.validate_hash(content_hash)
    except customs.CustomImageError as exc:
        raise HTTPException(status_code=400, detail=str(exc)) from exc


def _checked_back(content_hash: str) -> str:
    try:
        return backs.validate_hash(content_hash)
    except backs.BackImageError as exc:
        raise HTTPException(status_code=400, detail=str(exc)) from exc


def _upscale_back(body: LibraryUpscaleIn) -> GenerateOut:
    content_hash = _checked_back(body.content_hash)
    if not backs.has_original(content_hash):
        raise HTTPException(
            status_code=400,
            detail="This back image hasn't been uploaded to the server yet — the "
            "client syncs it on demand, so retry the upscale.",
        )
    notes: list[str] = []
    queued, failed, task_ids = generation_service.enqueue_back_upscale(
        content_hash,
        label=body.label,
        model=body.model,
        dpi_targets=body.dpi_targets,
        mode=body.mode,
        tile_size=body.tile_size,
        output_dir=Path(body.output_dir),
        cache_dir=Path(body.cache_dir),
        weights_dir=Path(body.weights_dir),
        project_tag=LIBRARY_TAG,
        on_note=notes.append,
        db_path=get_db_path(),
    )
    return GenerateOut(queued=queued, failed=failed, task_ids=task_ids, notes=notes)


@router.post("/upscale", response_model=GenerateOut)
def upscale_library_image(body: LibraryUpscaleIn) -> GenerateOut:
    if not body.dpi_targets:
        raise HTTPException(status_code=400, detail="Select at least one target DPI.")
    if body.kind == "back":
        return _upscale_back(body)
    content_hash = _checked_custom(body.content_hash)
    if not customs.has_original(content_hash):
        raise HTTPException(
            status_code=400,
            detail="This image hasn't been uploaded to the server yet — the client "
            "syncs it on demand, so retry the upscale.",
        )
    entry = DeckEntry(
        quantity=1,
        name=(body.label or "").strip() or _DEFAULT_LABEL,
        custom_hash=content_hash,
    )
    notes: list[str] = []
    queued, failed, task_ids = generation_service.enqueue_decklist_entries(
        [entry],
        model=body.model,
        dpi_targets=body.dpi_targets,
        # Registry-first: a variant any project already made is adopted
        # into the library tag rather than made again.
        skip_existing=True,
        tile_size=body.tile_size,
        output_dir=Path(body.output_dir),
        cache_dir=Path(body.cache_dir),
        weights_dir=Path(body.weights_dir),
        project_tag=LIBRARY_TAG,
        on_note=notes.append,
        db_path=get_db_path(),
        card_db_path=get_card_db_path(),
        custom_upscale=body.mode,
    )
    return GenerateOut(queued=queued, failed=failed, task_ids=task_ids, notes=notes)


@router.get("/{kind}/{content_hash}/status", response_model=LibraryStatusOut)
def library_image_status(kind: str, content_hash: str) -> LibraryStatusOut:
    """Tasks and registry rows for one library image across every
    project_tag — the union is the image's real state, since a project's
    Generate and the library tab both write the same global registry."""
    db_path = get_db_path()
    # Reconcile against disk first, as a project's gallery is on load:
    # a version whose file was wiped must not keep its badge.
    if kind == "back":
        content_hash = _checked_back(content_hash)
        db.prune_stale_library_records(back_hash=content_hash, db_path=db_path)
        tasks = db.list_tasks(back_hash=content_hash, db_path=db_path)
        items = db.list_registry_items_for_back(content_hash, db_path=db_path)
    elif kind == "custom":
        content_hash = _checked_custom(content_hash)
        db.prune_stale_library_records(custom_hash=content_hash, db_path=db_path)
        tasks = db.list_tasks(custom_hash=content_hash, db_path=db_path)
        items = db.list_registry_items_for_custom(content_hash, db_path=db_path)
    else:
        raise HTTPException(status_code=404, detail=f"Unknown library kind {kind!r}.")
    return LibraryStatusOut(
        tasks=[_task_out(t) for t in tasks],
        gallery=[_gallery_item_out(i) for i in items],
    )
