from __future__ import annotations

from pathlib import Path

from fastapi import APIRouter

import proxy_scaler
from proxy_scaler import backs, db
from proxy_scaler.api.deps import get_db_path
from proxy_scaler.api.schemas import (
    ClearGeneratedIn,
    ClearGeneratedOut,
    DeviceOut,
    DiscardTagOut,
    GenPathsOut,
    ModelOptionOut,
    VersionOut,
)
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


@router.get("/version", response_model=VersionOut)
def get_version() -> VersionOut:
    """This server's release version, for the client's drift warning —
    a Remote-mode client and its server are updated on different
    machines, and nothing else tells the user they've diverged. Reads
    proxy_scaler.__version__ rather than importlib.metadata because the
    frozen PyInstaller build ships no dist-info for this package (see
    desktop/pyinstaller/proxy-scaler-serve.spec's copy_metadata note);
    packaging/set-version.py keeps it in lockstep with pyproject.toml.
    Clients tolerate this endpoint's absence (older servers 404 here)."""
    return VersionOut(version=proxy_scaler.__version__)


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


# Must match DEFAULT_GEN_PATHS in desktop/frontend/src/pages/DecklistPage.tsx —
# the client sends these same relative names in every generate/regenerate/
# clear request, and this endpoint reports where they actually land.
DEFAULT_OUTPUT_DIR = "output"
DEFAULT_CACHE_DIR = "imgcache"
DEFAULT_WEIGHTS_DIR = "weights"
# A SIBLING of output/ and cache/, never inside them. clear_generated_data
# empties those two and prune_registry_under_dir drops every registry row
# found under output/ — living outside both is what makes "Back Images
# survive the wipe" a property of where the files are rather than a
# condition someone has to remember to re-check. See proxy_scaler/backs.py.
DEFAULT_BACKS_DIR = backs.BACKS_DIR_NAME


@router.get("/paths", response_model=GenPathsOut)
def get_paths() -> GenPathsOut:
    """Absolute resolved locations of the generation directories. The
    relative defaults resolve against this process's cwd (supervisor.py
    sets that to db.default_data_dir() for frozen runs; dev runs use the
    repo root), so only the server can answer this — and in Remote mode
    the answer describes the remote machine's filesystem."""
    return GenPathsOut(
        output_dir=str(Path(DEFAULT_OUTPUT_DIR).resolve()),
        cache_dir=str(Path(DEFAULT_CACHE_DIR).resolve()),
        weights_dir=str(Path(DEFAULT_WEIGHTS_DIR).resolve()),
        backs_dir=str(Path(DEFAULT_BACKS_DIR).resolve()),
    )


@router.post("/generated-data/clear", response_model=ClearGeneratedOut)
def clear_generated(body: ClearGeneratedIn) -> ClearGeneratedOut:
    notes = clear_generated_data(Path(body.output_dir), Path(body.cache_dir))
    # Every registry row pointing under the just-emptied output dir is now
    # a lie, for every project — and the registry answers existence
    # queries (skip-existing, the picker's /api/gallery/status) without
    # ever statting disk, so it must be told, not left to notice. The
    # membership cascade clears the affected galleries along the way.
    removed = db.prune_registry_under_dir(Path(body.output_dir), db_path=get_db_path())
    if removed:
        notes.append(f"unregistered {removed} generated image record(s)")
    if body.project_tag:
        # A completed task's own history reports "done" too, even with
        # its registry row gone — see db.py::clear_project_generation_records.
        db.clear_project_generation_records(body.project_tag, db_path=get_db_path())
        notes.append("cleared generation records for this project")
    return ClearGeneratedOut(notes=notes)


@router.post("/tags/{project_tag}/discard", response_model=DiscardTagOut)
def discard_tag(project_tag: str) -> DiscardTagOut:
    """One route meaning "this session was thrown away": stop the tag's
    queued work and forget its generation records.

    It never deletes files, and that's the whole reason it isn't a flag on
    /api/generated-data/clear (which unconditionally empties the output
    and cache dirs first). Output filenames carry no tag — output_filename
    takes none — so the images are shared across every Project, and
    deleting them per-tag would delete files other Projects' gallery rows
    point at."""
    canceled = db.cancel_pending_tasks_for_tag(project_tag, db_path=get_db_path())
    db.clear_project_generation_records(project_tag, db_path=get_db_path())
    return DiscardTagOut(canceled=canceled)
