from __future__ import annotations

from pathlib import Path

from fastapi import APIRouter, HTTPException

from proxy_scaler import db
from proxy_scaler.api.deps import get_db_path
from proxy_scaler.api.schemas import (
    CardOut,
    FaceOut,
    GenerateOut,
    ImportIn,
    ImportOut,
    RegenerateGalleryItemIn,
    VariantOut,
)
from proxy_scaler.decklist import parse_decklist_text
from proxy_scaler.pipeline import FaceResult
from proxy_scaler.services import decklist as decklist_service
from proxy_scaler.services import generation as generation_service

router = APIRouter(prefix="/api/projects/{project_id}", tags=["cards"])


@router.post("/import", response_model=ImportOut)
def import_decklist(project_id: int, body: ImportIn) -> ImportOut:
    db_path = get_db_path()
    entries = parse_decklist_text(body.text)
    if not entries:
        return ImportOut(added=0, skipped=0, failed=0)
    added, skipped, failed = generation_service.import_entries(
        project_id, entries, db_path=db_path
    )
    # Keep "last imported text" in sync — mirrors the old Streamlit
    # version, where Save always persisted whatever was in the text box.
    try:
        loaded = db.load_project(project_id, db_path=db_path)
        db.save_project(
            loaded.name,
            import_decklist_text=body.text,
            settings=loaded.settings,
            project_id=project_id,
            db_path=db_path,
        )
    except ValueError:
        pass
    return ImportOut(added=added, skipped=skipped, failed=failed)


@router.get("/cards", response_model=list[CardOut])
def list_cards(project_id: int) -> list[CardOut]:
    """The important endpoint: cards annotated with faces, each face's
    known dpi/model variants + status, merged with in-flight
    generation_tasks — this is server-side view aggregation (ports
    services.decklist's build_rows/group_by_card/status_for_pairs) so the
    frontend never has to reimplement that matching logic in TypeScript."""
    db_path = get_db_path()
    cards = db.list_project_cards(project_id, db_path=db_path)
    raw_items = db.list_gallery_items_for_project(project_id, db_path=db_path)
    gallery_id_by_key = {
        (d["scryfall_id"], d["face_index"], d["dpi"], d["model"]): d["id"]
        for d in raw_items
    }
    items = [FaceResult.from_dict(d) for d in raw_items]
    tasks = db.list_tasks(project_id=project_id, db_path=db_path)
    gallery_by_card, tasks_by_card = decklist_service.group_by_card(items, tasks)

    out: list[CardOut] = []
    for card in cards:
        identity = decklist_service.card_identity(
            card.set_code, card.collector_number, card.scryfall_id
        )
        face_groups = decklist_service.build_rows(
            gallery_by_card.get(identity, []), tasks_by_card.get(identity, [])
        )
        faces: list[FaceOut] = []
        for _key, face_items, face_tasks in face_groups:
            source = face_items[0] if face_items else face_tasks[0]
            pairs = decklist_service.status_for_pairs(face_items, face_tasks)
            variants = [
                VariantOut(
                    dpi=dpi,
                    model=model,
                    status=status,
                    error=error,
                    gallery_item_id=(
                        gallery_id_by_key.get(
                            (source.scryfall_id, source.face_index, dpi, model)
                        )
                        if status == "done"
                        else None
                    ),
                )
                for dpi, model, status, error in pairs
            ]
            faces.append(
                FaceOut(
                    face_index=source.face_index,
                    face_label=source.face_label,
                    face_name=source.face_name,
                    variants=variants,
                )
            )
        out.append(
            CardOut(
                id=card.id,
                sort_order=card.sort_order,
                original_import_line=card.original_import_line,
                quantity=card.quantity,
                card_name=card.card_name,
                set_code=card.set_code,
                collector_number=card.collector_number,
                scryfall_id=card.scryfall_id,
                faces=faces,
            )
        )
    return out


@router.delete("/cards/{card_id}", status_code=204)
def remove_card(project_id: int, card_id: int) -> None:  # noqa: ARG001
    db.remove_project_card(card_id, db_path=get_db_path())


@router.post("/regenerate/{gallery_item_id}", response_model=GenerateOut)
def regenerate_gallery_item(
    project_id: int, gallery_item_id: int, body: RegenerateGalleryItemIn
) -> GenerateOut:
    """Redo one exact existing variant unchanged — mirrors the old
    Streamlit "Regen" button, which redid a known FaceResult's own
    scryfall_id/png_url/model/dpi rather than the sidebar's current
    settings for those fields. The frontend only ever has a
    gallery_item_id for an existing variant (not those low-level
    fields), so this looks them up server-side instead of requiring the
    client to resupply them."""
    db_path = get_db_path()
    items = db.list_gallery_items_for_project(project_id, db_path=db_path)
    item = next((i for i in items if i["id"] == gallery_item_id), None)
    if item is None:
        raise HTTPException(status_code=404, detail="Gallery item not found")

    loaded = db.load_project(project_id, db_path=db_path)
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
        output_dir=Path(loaded.settings.output_dir),
        cache_dir=Path(loaded.settings.cache_dir),
        weights_dir=Path(loaded.settings.weights_dir),
        project_id=project_id,
        db_path=db_path,
    )
    return GenerateOut(queued=len(task_ids), failed=0, task_ids=task_ids, notes=[])
