from __future__ import annotations

from fastapi import APIRouter, HTTPException

from proxy_scaler import db
from proxy_scaler.api.deps import get_db_path
from proxy_scaler.api.schemas import (
    ProjectCreateIn,
    ProjectOut,
    ProjectSettingsIn,
    ProjectSummaryOut,
    ProjectUpdateIn,
)

router = APIRouter(prefix="/api/projects", tags=["projects"])


def _settings_from_in(s: ProjectSettingsIn | None) -> db.ProjectSettings:
    if s is None:
        return db.ProjectSettings()
    return db.ProjectSettings(
        model=s.model,
        dpi_targets=s.dpi_targets,
        page_size=s.page_size,
        skip_existing=s.skip_existing,
        output_dir=s.output_dir,
        cache_dir=s.cache_dir,
        weights_dir=s.weights_dir,
        tile_size=s.tile_size,
    )


def _settings_out(s: db.ProjectSettings) -> ProjectSettingsIn:
    return ProjectSettingsIn(
        model=s.model,
        dpi_targets=s.dpi_targets,
        page_size=s.page_size,
        skip_existing=s.skip_existing,
        output_dir=s.output_dir,
        cache_dir=s.cache_dir,
        weights_dir=s.weights_dir,
        tile_size=s.tile_size,
    )


@router.get("", response_model=list[ProjectSummaryOut])
def list_projects() -> list[ProjectSummaryOut]:
    return [
        ProjectSummaryOut(id=p.id, name=p.name, updated_at=p.updated_at)
        for p in db.list_projects(db_path=get_db_path())
    ]


@router.get("/last", response_model=dict)
def last_project() -> dict:
    return {"project_id": db.get_last_project_id(db_path=get_db_path())}


@router.post("", response_model=ProjectSummaryOut)
def create_project(body: ProjectCreateIn) -> ProjectSummaryOut:
    db_path = get_db_path()
    try:
        pid = db.save_project(
            body.name,
            import_decklist_text="",
            settings=_settings_from_in(body.settings),
            project_id=None,
            db_path=db_path,
        )
    except ValueError as exc:
        raise HTTPException(status_code=400, detail=str(exc)) from exc
    db.set_last_project_id(pid, db_path=db_path)
    loaded = db.load_project(pid, db_path=db_path)
    return ProjectSummaryOut(id=loaded.id, name=loaded.name, updated_at=loaded.updated_at)


@router.get("/{project_id}", response_model=ProjectOut)
def get_project(project_id: int) -> ProjectOut:
    try:
        loaded = db.load_project(project_id, db_path=get_db_path())
    except ValueError as exc:
        raise HTTPException(status_code=404, detail=str(exc)) from exc
    return ProjectOut(
        id=loaded.id,
        name=loaded.name,
        import_decklist_text=loaded.import_decklist_text,
        settings=_settings_out(loaded.settings),
        created_at=loaded.created_at,
        updated_at=loaded.updated_at,
    )


@router.put("/{project_id}", response_model=ProjectSummaryOut)
def update_project(project_id: int, body: ProjectUpdateIn) -> ProjectSummaryOut:
    db_path = get_db_path()
    try:
        existing = db.load_project(project_id, db_path=db_path)
    except ValueError as exc:
        raise HTTPException(status_code=404, detail=str(exc)) from exc
    try:
        pid = db.save_project(
            body.name,
            # Preserve whatever text was last imported — this endpoint
            # only updates name/settings, not the decklist itself (that's
            # the dedicated /import endpoint).
            import_decklist_text=existing.import_decklist_text,
            settings=_settings_from_in(body.settings),
            project_id=project_id,
            db_path=db_path,
        )
    except ValueError as exc:
        raise HTTPException(status_code=400, detail=str(exc)) from exc
    db.set_last_project_id(pid, db_path=db_path)
    loaded = db.load_project(pid, db_path=db_path)
    return ProjectSummaryOut(id=loaded.id, name=loaded.name, updated_at=loaded.updated_at)


@router.delete("/{project_id}", status_code=204)
def delete_project(project_id: int) -> None:
    db.delete_project(project_id, db_path=get_db_path())


@router.delete("", status_code=204)
def clear_all_projects(confirm: bool = False) -> None:
    if not confirm:
        raise HTTPException(
            status_code=400, detail="Pass confirm=true to clear all projects."
        )
    db.delete_all_projects(db_path=get_db_path())
