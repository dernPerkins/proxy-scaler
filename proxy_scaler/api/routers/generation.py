from __future__ import annotations

from pathlib import Path

from fastapi import APIRouter, HTTPException

from proxy_scaler import db
from proxy_scaler.api.deps import get_db_path, get_lock_path
from proxy_scaler.api.schemas import (
    DeckEntryIn,
    GenerateIn,
    GenerateOut,
    TaskOut,
    WorkerStatusOut,
)
from proxy_scaler.decklist import DeckEntry
from proxy_scaler.services import generation as generation_service

router = APIRouter(prefix="/api", tags=["generation"])


def _task_out(t: db.TaskRow) -> TaskOut:
    return TaskOut(
        id=t.id,
        project_tag=t.project_tag,
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


def _to_deck_entry(e: DeckEntryIn) -> DeckEntry:
    return DeckEntry(
        quantity=e.quantity,
        name=e.name,
        set_code=e.set_code,
        collector_number=e.collector_number,
        raw_line=e.raw_line or e.name,
    )


@router.post("/generate", response_model=GenerateOut)
def generate(body: GenerateIn) -> GenerateOut:
    db_path = get_db_path()
    if not body.dpi_targets:
        raise HTTPException(status_code=400, detail="Select at least one target DPI.")
    if not body.entries:
        raise HTTPException(status_code=400, detail="No cards to generate.")
    entries = [_to_deck_entry(e) for e in body.entries]

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
        project_tag=body.project_tag,
        on_note=notes.append,
        db_path=db_path,
    )
    return GenerateOut(queued=queued, failed=failed, task_ids=task_ids, notes=notes)


@router.get("/tasks", response_model=list[TaskOut])
def list_tasks(project_tag: str | None = None, status: str | None = None) -> list[TaskOut]:
    statuses = [status] if status else None
    tasks = db.list_tasks(project_tag=project_tag, statuses=statuses, db_path=get_db_path())
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
