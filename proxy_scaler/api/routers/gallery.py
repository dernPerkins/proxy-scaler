from __future__ import annotations

from pathlib import Path

from fastapi import APIRouter, HTTPException
from fastapi.responses import FileResponse

from proxy_scaler import db
from proxy_scaler.api.deps import get_db_path
from proxy_scaler.api.schemas import (
    AdoptGalleryIn,
    AdoptGalleryOut,
    GalleryItemOut,
    GenerateOut,
    RegenerateGalleryItemIn,
)
from proxy_scaler.decklist import DeckEntry
from proxy_scaler.services import generation as generation_service

router = APIRouter(prefix="/api/gallery", tags=["gallery"])


def _find_item(gallery_item_id: int) -> dict:
    item = db.get_gallery_item(gallery_item_id, db_path=get_db_path())
    if item is None:
        raise HTTPException(status_code=404, detail="Image not found")
    return item


def _resolve_existing(path_str: str) -> Path:
    p = Path(path_str)
    if not p.is_file():
        raise HTTPException(status_code=404, detail="File not found on disk")
    return p


@router.get("", response_model=list[GalleryItemOut])
def list_gallery(project_tag: str) -> list[GalleryItemOut]:
    items = db.list_gallery_items(project_tag, db_path=get_db_path())
    return [
        GalleryItemOut(
            id=i["id"],
            scryfall_id=i["scryfall_id"],
            face_index=i["face_index"],
            face_label=i["face_label"],
            face_name=i["face_name"],
            card_name=i["card_name"],
            set_code=i["set_code"],
            collector_number=i["collector_number"],
            dpi=i["dpi"],
            model=i["model"],
            image_filename=i["image_filename"],
            lang=i["lang"],
        )
        for i in items
    ]


@router.post("/adopt", response_model=AdoptGalleryOut)
def adopt_gallery(body: AdoptGalleryIn) -> AdoptGalleryOut:
    """Reconcile this project's gallery with what actually exists on disk,
    in both directions. First prune: drop this tag's gallery rows and done-
    task records whose output file is gone (db.prune_stale_gallery_items) —
    pruning runs first so a stale row can't block adopting a live
    replacement. Then adopt (db.adopt_gallery_items): other projects'
    gallery rows, plus — when output_dir is sent — an on-disk filename
    scan for images with no row anywhere. Called by the client after an
    import / on project load, so badges reflect reality without waiting
    for a Generate request. Idempotent and cheap: SQL plus file stats, no
    Scryfall, no upscaling."""
    db_path = get_db_path()
    pruned = db.prune_stale_gallery_items(body.project_tag, db_path=db_path)
    entries = [
        DeckEntry(
            quantity=e.quantity,
            name=e.name,
            set_code=e.set_code,
            collector_number=e.collector_number,
            raw_line=e.raw_line,
            scryfall_id=e.scryfall_id,
            lang=e.lang,
        )
        for e in body.entries
    ]
    adopted = db.adopt_gallery_items(
        body.project_tag,
        entries,
        db_path=db_path,
        output_dir=Path(body.output_dir) if body.output_dir else None,
    )
    return AdoptGalleryOut(adopted=adopted, pruned=pruned)


@router.get("/{gallery_item_id}/original")
def get_original(gallery_item_id: int) -> FileResponse:
    item = _find_item(gallery_item_id)
    path = _resolve_existing(item["original_path"])
    return FileResponse(path, media_type="image/png")


@router.get("/{gallery_item_id}/full")
def get_full(gallery_item_id: int) -> FileResponse:
    item = _find_item(gallery_item_id)
    path = _resolve_existing(item["out_path"])
    return FileResponse(path, media_type="image/png", filename=item["image_filename"])


@router.post("/{gallery_item_id}/regenerate", response_model=GenerateOut)
def regenerate(gallery_item_id: int, body: RegenerateGalleryItemIn) -> GenerateOut:
    """Redo one exact existing variant unchanged — its own scryfall_id/
    png_url/model/dpi come from the stored gallery item, not the client."""
    db_path = get_db_path()
    item = _find_item(gallery_item_id)
    task_ids = generation_service.enqueue_face(
        scryfall_id=item["scryfall_id"],
        face_index=item["face_index"],
        face_label=item["face_label"],
        face_name=item["face_name"],
        card_name=item["card_name"],
        set_code=item["set_code"],
        collector_number=item["collector_number"],
        png_url=item["png_url"],
        dpi_targets=[item["dpi"]],
        model=item["model"],
        tile_size=body.tile_size,
        output_dir=Path(body.output_dir),
        cache_dir=Path(body.cache_dir),
        weights_dir=Path(body.weights_dir),
        project_tag=item["project_tag"],
        total_faces=item["total_faces"],
        lang=item["lang"],
        db_path=db_path,
    )
    return GenerateOut(queued=len(task_ids), failed=0, task_ids=task_ids, notes=[])
