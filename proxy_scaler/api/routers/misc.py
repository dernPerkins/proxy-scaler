from __future__ import annotations

from pathlib import Path

from fastapi import APIRouter

from proxy_scaler.api.schemas import ClearGeneratedIn, ClearGeneratedOut
from proxy_scaler.pipeline import clear_generated_data

router = APIRouter(prefix="/api", tags=["misc"])


@router.get("/health")
def health() -> dict:
    """Replaces Streamlit's /_stcore/health for supervisor.py's health
    check."""
    return {"status": "ok"}


@router.post("/generated-data/clear", response_model=ClearGeneratedOut)
def clear_generated(body: ClearGeneratedIn) -> ClearGeneratedOut:
    notes = clear_generated_data(Path(body.output_dir), Path(body.cache_dir))
    return ClearGeneratedOut(notes=notes)
