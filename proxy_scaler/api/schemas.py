"""Pydantic request/response models for the FastAPI layer. TypeScript
types for the frontend are meant to be generated from these via FastAPI's
OpenAPI schema (openapi-typescript), so the Python<->TS boundary stays
checked as both sides evolve."""

from __future__ import annotations

from pydantic import BaseModel


class ModelOptionOut(BaseModel):
    value: str
    label: str


class DeckEntryIn(BaseModel):
    quantity: int = 1
    name: str
    set_code: str | None = None
    collector_number: str | None = None
    raw_line: str = ""


class ResolveIn(BaseModel):
    entries: list[DeckEntryIn]


class ResolvedFaceOut(BaseModel):
    scryfall_id: str
    face_index: int | None
    face_label: str | None
    face_name: str
    card_name: str
    set_code: str
    collector_number: str
    png_url: str
    image_status: str | None = None


class ResolvedCardOut(BaseModel):
    raw_line: str
    quantity: int
    faces: list[ResolvedFaceOut]
    warnings: list[str] = []


class ResolveFailureOut(BaseModel):
    raw_line: str
    error: str


class ResolveOut(BaseModel):
    resolved: list[ResolvedCardOut]
    failed: list[ResolveFailureOut]


class GenerateIn(BaseModel):
    # project_tag is an opaque string the client mints per local project —
    # purely a scoping label for tasks/gallery rows, not a foreign key
    # (see ARCHITECTURE.md). Entries are raw/unresolved decklist lines;
    # this endpoint resolves them against Scryfall internally as part of
    # one collapsed resolve -> download -> upscale step per face, rather
    # than requiring the client to resolve first via /api/resolve.
    project_tag: str
    entries: list[DeckEntryIn]
    model: str
    dpi_targets: list[int]
    skip_existing: bool = True
    tile_size: int = 0
    output_dir: str
    cache_dir: str
    weights_dir: str


class RegenerateGalleryItemIn(BaseModel):
    # Redo one exact existing variant unchanged — its own scryfall_id/
    # png_url/model/dpi come from the stored gallery item server-side (see
    # gallery.py's regenerate endpoint), not from the client. tile_size is
    # recalculated from whatever the *current* sidebar setting is for that
    # variant's model, not a stored per-item value. output_dir/cache_dir/
    # weights_dir are client-supplied per-request now that no project on
    # the server holds them (see ARCHITECTURE.md).
    tile_size: int = 0
    output_dir: str
    cache_dir: str
    weights_dir: str


class GenerateOut(BaseModel):
    queued: int
    failed: int
    task_ids: list[int]
    notes: list[str] = []


class TaskOut(BaseModel):
    id: int
    project_tag: str | None
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


class GalleryItemOut(BaseModel):
    id: int
    scryfall_id: str
    face_index: int | None
    face_label: str | None
    face_name: str
    card_name: str
    set_code: str
    collector_number: str
    dpi: int
    model: str
    image_filename: str


class PdfLayoutIn(BaseModel):
    # project_tag scopes which generated images to draw from; entries carry
    # the quantities (not persisted server-side any more — see
    # ARCHITECTURE.md) that match_quantities() needs to know how many of
    # each printing to lay out. project_name is cosmetic only, used for the
    # downloaded filename.
    project_tag: str
    entries: list[DeckEntryIn]
    project_name: str = ""
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
