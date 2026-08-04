from __future__ import annotations

from pathlib import Path

from fastapi import APIRouter, HTTPException
from fastapi.responses import FileResponse

from proxy_scaler import db
from proxy_scaler.api.deps import get_db_path

router = APIRouter(prefix="/api/projects/{project_id}/images", tags=["images"])


def _find_item(project_id: int, gallery_item_id: int) -> dict:
    # Scoped to this project via list_gallery_items_for_project's own
    # JOIN on project_cards.project_id — a gallery_item_id belonging to a
    # different project simply won't show up here, so there's no
    # cross-project leakage to guard against separately. The path values
    # themselves are never taken from client input (only the integer id
    # is), so there's no path-injection surface to validate either —
    # they're always whatever this project's own DB records say.
    items = db.list_gallery_items_for_project(project_id, db_path=get_db_path())
    for item in items:
        if item["id"] == gallery_item_id:
            return item
    raise HTTPException(status_code=404, detail="Image not found")


def _resolve_existing(path_str: str) -> Path:
    p = Path(path_str)
    if not p.is_file():
        raise HTTPException(status_code=404, detail="File not found on disk")
    return p


@router.get("/{gallery_item_id}/original")
def get_original(project_id: int, gallery_item_id: int) -> FileResponse:
    item = _find_item(project_id, gallery_item_id)
    path = _resolve_existing(item["original_path"])
    return FileResponse(path, media_type="image/png")


@router.get("/{gallery_item_id}/full")
def get_full(project_id: int, gallery_item_id: int) -> FileResponse:
    item = _find_item(project_id, gallery_item_id)
    path = _resolve_existing(item["out_path"])
    return FileResponse(path, media_type="image/png", filename=item["image_filename"])
