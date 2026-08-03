from __future__ import annotations

from pathlib import Path

from fastapi import APIRouter

from proxy_scaler.api.schemas import ClearGeneratedIn, ClearGeneratedOut, ModelOptionOut
from proxy_scaler.pipeline import clear_generated_data
from proxy_scaler.upscale import UpscaleModel

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


@router.post("/generated-data/clear", response_model=ClearGeneratedOut)
def clear_generated(body: ClearGeneratedIn) -> ClearGeneratedOut:
    notes = clear_generated_data(Path(body.output_dir), Path(body.cache_dir))
    return ClearGeneratedOut(notes=notes)
