from __future__ import annotations

from pathlib import Path

from fastapi import APIRouter

from proxy_scaler import db
from proxy_scaler.api.deps import get_db_path
from proxy_scaler.api.schemas import ClearGeneratedIn, ClearGeneratedOut, DeviceOut, ModelOptionOut
from proxy_scaler.pipeline import clear_generated_data
from proxy_scaler.upscale import (
    UpscaleModel,
    device_backend,
    device_kind,
    resolve_device,
)

router = APIRouter(prefix="/api", tags=["misc"])


@router.get("/health")
def health() -> dict:
    """Replaces Streamlit's /_stcore/health for supervisor.py's health
    check."""
    return {"status": "ok"}


@router.get("/models", response_model=list[ModelOptionOut])
def list_models() -> list[ModelOptionOut]:
    """The frontend's model dropdown must read this list, never hardcode
    it — a hardcoded copy in the React rewrite silently dropped two
    models (missing from a list typed from memory instead of enumerating
    UpscaleModel, the way the old Streamlit selectbox did via
    `[m.value for m in UpscaleModel]`). This endpoint is the fix: the
    enum is the only source of truth, structurally, not just by
    convention."""
    return [ModelOptionOut(value=m.value, label=m.label) for m in UpscaleModel]


@router.get("/device", response_model=DeviceOut)
def get_device() -> DeviceOut:
    """Whether this server has a real GPU (CUDA or Apple MPS) to upscale
    on — the client uses this to pick a sensible default model (heavy/
    quality-first vs. light/CPU-friendly) instead of guessing from
    Local-vs-Remote mode alone, which says nothing about the actual
    hardware behind either. Deliberately not called from /api/health:
    resolve_device() imports torch, and upscale.py's module docstring
    explains why that import must never sit on the startup-readiness
    path. First call here pays torch's import cost; every call after
    that (in this process) is cheap.

    Reports the specific backend alongside the coarse gpu/cpu answer:
    'gpu' alone can't distinguish Apple MPS from CUDA, and they want
    different default models (see ProjectContext.tsx's
    recommendedDefaultModel)."""
    device = resolve_device()
    return DeviceOut(kind=device_kind(device), backend=device_backend(device))


@router.post("/generated-data/clear", response_model=ClearGeneratedOut)
def clear_generated(body: ClearGeneratedIn) -> ClearGeneratedOut:
    notes = clear_generated_data(Path(body.output_dir), Path(body.cache_dir))
    if body.project_tag:
        # The files these records point at are gone now — without this,
        # the client keeps reporting every card as already generated
        # (gallery rows say so directly; even without them, a completed
        # task's own history reports "done" too — see
        # db.py::clear_project_generation_records).
        db.clear_project_generation_records(body.project_tag, db_path=get_db_path())
        notes.append("cleared generation records for this project")
    return ClearGeneratedOut(notes=notes)
