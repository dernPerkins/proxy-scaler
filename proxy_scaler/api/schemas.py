"""Pydantic request/response models for the FastAPI layer. TypeScript
types for the frontend are meant to be generated from these via FastAPI's
OpenAPI schema (openapi-typescript), so the Python<->TS boundary stays
checked as both sides evolve."""

from __future__ import annotations

from pydantic import BaseModel


class ProjectSettingsIn(BaseModel):
    model: str
    dpi_targets: list[int]
    page_size: int = 6
    skip_existing: bool = True
    output_dir: str = ""
    cache_dir: str = ""
    weights_dir: str = ""
    tile_size: int = 0


class ProjectSummaryOut(BaseModel):
    id: int
    name: str
    updated_at: str


class ProjectCreateIn(BaseModel):
    name: str
    settings: ProjectSettingsIn | None = None


class ProjectUpdateIn(BaseModel):
    name: str
    settings: ProjectSettingsIn


class ProjectOut(BaseModel):
    id: int
    name: str
    import_decklist_text: str
    settings: ProjectSettingsIn
    created_at: str
    updated_at: str


class ImportIn(BaseModel):
    text: str


class ImportOut(BaseModel):
    added: int
    skipped: int
    failed: int


class VariantOut(BaseModel):
    dpi: int
    model: str
    status: str
    error: str | None = None
    gallery_item_id: int | None = None


class FaceOut(BaseModel):
    face_index: int | None
    face_label: str | None
    face_name: str | None
    variants: list[VariantOut]


class CardOut(BaseModel):
    id: int
    sort_order: int
    original_import_line: str
    quantity: int | None
    card_name: str | None
    set_code: str | None
    collector_number: str | None
    scryfall_id: str | None
    faces: list[FaceOut]


class GenerateIn(BaseModel):
    # Importing a decklist always requires a project (see the /import
    # endpoint), so unlike the old Streamlit session-state model there's
    # no longer a "generate before ever saving a project" path for card
    # rows that exist at all — project_id is always real here.
    project_id: int
    card_ids: list[int] | None = None  # None/omitted = every card in the project
    model: str
    dpi_targets: list[int]
    skip_existing: bool = True
    tile_size: int = 0
    output_dir: str
    cache_dir: str
    weights_dir: str


class RegenerateGalleryItemIn(BaseModel):
    # Redo one exact existing variant unchanged — its own scryfall_id/
    # png_url/model/dpi come from the stored gallery item server-side
    # (see cards.py's regenerate_gallery_item), not from the client. Only
    # tile_size is client-supplied, since — like the old Streamlit
    # version — it's recalculated from whatever the *current* sidebar
    # setting is for that variant's model, not a stored per-item value.
    tile_size: int = 0


class GenerateOut(BaseModel):
    queued: int
    failed: int
    task_ids: list[int]
    notes: list[str] = []


class TaskOut(BaseModel):
    id: int
    project_id: int | None
    status: str
    scryfall_id: str
    face_index: int | None
    face_label: str | None
    face_name: str
    card_name: str
    set_code: str
    collector_number: str
    dpi: int
    model: str
    error: str | None
    created_at: str
    started_at: str | None
    completed_at: str | None


class WorkerStatusOut(BaseModel):
    running: bool


class PdfLayoutIn(BaseModel):
    page_width_mm: float
    page_height_mm: float
    cols: int
    rows: int
    bleed_mm: float = 1.0
    spacing_x_mm: float = 0.0
    spacing_y_mm: float = 0.0
    offset_x_mm: float = 0.0
    offset_y_mm: float = 0.0
    guide_width_pt: float = 0.75
    guide_length_mm: float = 2.75
    export_dpi: int = 1200
    show_cut_lines: bool = True
    preferred_dpi: int | None = None
    preferred_model: str | None = None


class PdfPreviewOut(BaseModel):
    units: int
    unmatched: list[str]
    page_count: int


class ClearGeneratedIn(BaseModel):
    output_dir: str
    cache_dir: str


class ClearGeneratedOut(BaseModel):
    notes: list[str]
