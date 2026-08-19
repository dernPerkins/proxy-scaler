"""Pydantic request/response models for the FastAPI layer. TypeScript
types for the frontend are meant to be generated from these via FastAPI's
OpenAPI schema (openapi-typescript), so the Python<->TS boundary stays
checked as both sides evolve."""

from __future__ import annotations

from pydantic import BaseModel


class ModelOptionOut(BaseModel):
    value: str
    label: str


class VersionOut(BaseModel):
    # The server's own release version (proxy_scaler.__version__ — kept in
    # lockstep with every other copy by packaging/set-version.py). The
    # client compares this against its own version to warn about
    # client/server drift, which Remote mode makes possible: the two are
    # updated on different machines.
    version: str


class DeviceOut(BaseModel):
    kind: str  # "gpu" | "cpu" — see upscale.py's device_kind()
    # The real torch backend behind `kind`: "cuda" | "mps" | "privateuseone"
    # (torch-directml) | "cpu" | "unknown". Added because `kind` collapses
    # every GPU backend into one value, leaving the client unable to tell
    # Apple's MPS (a real GPU, but slow on the heavy models) from CUDA when
    # picking a default model. Deliberately *additive*: `kind` keeps its
    # exact existing vocabulary because those strings are persisted into
    # on-disk `.device` cache sidecars (upscale.py::write_cache_device) and
    # re-reading them is how the gallery reports provenance. Defaulted so
    # an older server answering a newer client still validates.
    backend: str = "unknown"


class DeckEntryIn(BaseModel):
    quantity: int = 1
    name: str
    set_code: str | None = None
    collector_number: str | None = None
    raw_line: str = ""


class ResolveIn(BaseModel):
    entries: list[DeckEntryIn]


class AdoptGalleryIn(BaseModel):
    project_tag: str
    entries: list[DeckEntryIn]
    # Generation-machine-local path (same meaning as GenerateIn.output_dir);
    # when present, adoption also scans it for images that exist on disk
    # with no gallery row anywhere (pre-reshape or CLI-produced files).
    output_dir: str | None = None


class AdoptGalleryOut(BaseModel):
    adopted: int
    # Stale records removed for this project_tag (gallery rows + done-task
    # records whose output file is gone) — see db.prune_stale_gallery_items.
    pruned: int


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
    # True while the worker is started-but-waiting (see db.py's worker
    # hold/release section) — `running` still reads true then, since the
    # held worker does hold its lock. Defaulted so older clients that
    # don't know the field parse fine.
    held: bool = False


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
    # Decklist entries with no generated image at all, or a DFC entry
    # missing one or more of its faces (known from whichever face did
    # generate — see FaceResult.total_faces). Does NOT include gallery
    # images that simply have no matching decklist entry any more — those
    # are silently left out of the print run, not reported as an error.
    missing: list[str]
    page_count: int
    # Cards with no generated image at the requested preferred_dpi. These
    # are excluded from the print run rather than substituted at another
    # resolution, so the UI must surface them as an error — otherwise they
    # would silently vanish from the sheet.
    missing_at_dpi: list[str] = []


class PdfJobOut(BaseModel):
    job_id: str
    # Unique source images to process — what the client sizes its progress
    # bar against. See pdf_layout.unique_image_count for why this isn't the
    # print-slot count.
    total: int


class PdfJobStatusOut(BaseModel):
    status: str  # "rendering" | "done" | "failed" | "canceled"
    completed: int
    total: int
    error: str | None = None


class PdfPageSlotOut(BaseModel):
    card_name: str
    face_label: str | None = None
    model: str | None = None
    dpi: int | None = None
    thumbnail_data_url: str | None = None  # "data:image/jpeg;base64,..."; None if unavailable


class PdfPagePreviewOut(BaseModel):
    """Page-1-only visual layout preview — distinct from PdfPreviewOut
    (that one's a settled, numbers-only summary; this one carries the
    actual geometry + small thumbnails the frontend renders as a CSS
    grid). See proxy_scaler/pdf_layout.py::PageLayout for what these
    fields mean."""

    page_w_mm: float
    page_h_mm: float
    cols: int
    rows: int
    margin_x_mm: float
    margin_y_mm: float
    cell_w_mm: float
    cell_h_mm: float
    bled_card_w_mm: float
    bled_card_h_mm: float
    bleed_mm: float
    guide_width_pt: float
    guide_length_mm: float
    show_cut_lines: bool
    page_count: int
    slots: list[PdfPageSlotOut]


class ClearGeneratedIn(BaseModel):
    output_dir: str
    cache_dir: str
    # Optional so a caller with no project context yet (or a future
    # global/all-projects clear) still works — but the client always sends
    # its current project_tag, since otherwise the deleted files' gallery/
    # task records survive and the UI keeps reporting them as generated.
    project_tag: str | None = None


class ClearGeneratedOut(BaseModel):
    notes: list[str]


class GenPathsOut(BaseModel):
    # Absolute paths as resolved on the generation server's machine — in
    # Remote mode these describe the remote host's filesystem, not the
    # client's.
    output_dir: str
    cache_dir: str
    weights_dir: str


class DiscardTagOut(BaseModel):
    # How many pending tasks the discard canceled. The client fires
    # discard fire-and-forget and ignores the body; this is here so a
    # manual `curl` can see what actually happened.
    canceled: int
