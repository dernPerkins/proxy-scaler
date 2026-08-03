from __future__ import annotations

from pathlib import Path

from fastapi import APIRouter, HTTPException

from proxy_scaler import db
from proxy_scaler.api.deps import get_db_path, get_lock_path
from proxy_scaler.api.schemas import GenerateIn, GenerateOut, TaskOut, WorkerStatusOut
from proxy_scaler.services import generation as generation_service

router = APIRouter(prefix="/api", tags=["generation"])


def _task_out(t: db.TaskRow) -> TaskOut:
    return TaskOut(
        id=t.id,
        project_id=t.project_id,
        status=t.status,
        scryfall_id=t.scryfall_id,
        face_index=t.face_index,
        face_label=t.face_label,
        face_name=t.face_name,
        card_name=t.card_name,
        set_code=t.set_code,
        collector_number=t.collector_number,
        dpi=t.dpi,
        model=t.model,
        error=t.error,
        created_at=t.created_at,
        started_at=t.started_at,
        completed_at=t.completed_at,
    )


@router.post("/generate", response_model=GenerateOut)
def generate(body: GenerateIn) -> GenerateOut:
    db_path = get_db_path()
    if not body.dpi_targets:
        raise HTTPException(status_code=400, detail="Select at least one target DPI.")

    all_cards = db.list_project_cards(body.project_id, db_path=db_path)
    if body.card_ids is not None:
        wanted = set(body.card_ids)
        cards = [c for c in all_cards if c.id in wanted]
    else:
        cards = all_cards
    if not cards:
        raise HTTPException(status_code=400, detail="No matching cards to generate.")
    entries = [c.to_deck_entry() for c in cards]

    notes: list[str] = []
    queued, failed, task_ids = generation_service.enqueue_decklist_entries(
        entries,
        model=body.model,
        dpi_targets=body.dpi_targets,
        skip_existing=body.skip_existing,
        tile_size=body.tile_size,
        output_dir=Path(body.output_dir),
        cache_dir=Path(body.cache_dir),
        weights_dir=Path(body.weights_dir),
        project_id=body.project_id,
        on_note=notes.append,
        db_path=db_path,
    )
    return GenerateOut(queued=queued, failed=failed, task_ids=task_ids, notes=notes)


@router.get("/tasks", response_model=list[TaskOut])
def list_tasks(project_id: int | None = None, status: str | None = None) -> list[TaskOut]:
    statuses = [status] if status else None
    tasks = db.list_tasks(project_id=project_id, statuses=statuses, db_path=get_db_path())
    return [_task_out(t) for t in tasks]


@router.get("/tasks/{task_id}", response_model=TaskOut)
def get_task(task_id: int) -> TaskOut:
    task = db.get_task(task_id, db_path=get_db_path())
    if task is None:
        raise HTTPException(status_code=404, detail="Task not found")
    return _task_out(task)


@router.post("/tasks/{task_id}/cancel")
def cancel_task(task_id: int) -> dict:
    canceled = db.cancel_task(task_id, db_path=get_db_path())
    return {"canceled": canceled}


@router.get("/worker/status", response_model=WorkerStatusOut)
def worker_status() -> WorkerStatusOut:
    return WorkerStatusOut(running=db.is_worker_running(lock_path=get_lock_path()))
