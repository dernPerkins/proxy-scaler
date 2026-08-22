"""Pydantic request/response models for the FastAPI layer. TypeScript
types for the frontend are meant to be generated from these via FastAPI's
OpenAPI schema (openapi-typescript), so the Python<->TS boundary stays
checked as both sides evolve."""

from __future__ import annotations

from enum import Enum

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
    # Optional pinned identity + language preference (see DeckEntry in
    # decklist.py). Defaulted so older clients keep working unchanged.
    scryfall_id: str | None = None
    lang: str | None = None


class ResolveIn(BaseModel):
    entries: list[DeckEntryIn]
    # The resolve-gated import's "strictly literal" language mode: each
    # entry's lang is a demand, not a preference — a match in any other
    # language becomes that entry's failure. False (default) keeps the
    # relaxed preference ladder for legacy callers and the "All Languages"
    # import mode (entries with lang = null).
    strict_lang: bool = False


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
    # Printing language — the client persists this (with scryfall_id) into
    # its project cards after a resolve. Defaulted for older servers.
    lang: str = "en"
    # Localized name as printed on a non-English card; None for English
    # printings. Display-only on the client (English name stays the
    # matching identity).
    printed_name: str | None = None


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
    # project_tag scopes the regenerated task: registry rows are global
    # (shared by every project via memberships), so the requesting client
    # has to say which project the regeneration belongs to.
    project_tag: str
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
    lang: str = "en"


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
    lang: str = "en"


class PageOrderIn(str, Enum):
    INTERLEAVED = "interleaved"
    FRONTS_THEN_BACKS = "fronts_then_backs"


class FlipEdgeIn(str, Enum):
    LONG = "long"
    SHORT = "short"


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
    preferred_dpi: int | None = None
    preferred_model: str | None = None

    # --- Guides ----------------------------------------------------------
    #
    # Four independent HIDE flags, replacing the single `show_cut_lines`
    # boolean. Required, with NO defaults, and that is the enforcement
    # mechanism rather than an oversight: an older client still sending
    # `show_cut_lines` gets a 422 here instead of silently rendering with
    # guide settings the user never chose.
    #
    # The reverse drift direction cannot be caught here at all — Pydantic
    # ignores unknown fields, so a NEW client's flags sent to an OLD server
    # are dropped without a word and that server renders with its own
    # `show_cut_lines=True` default. The only place that break is
    # detectable is the client, which carries a version floor and refuses
    # to render against a server older than back printing. See
    # desktop/frontend/src/config.ts.
    #
    # Stored as `hide_*` to match the checkbox the user actually ticks —
    # one polarity from the UI through to pdf_layout.GuideVisibility, with
    # no `not` in between to invert by accident.
    hide_card_guides_front: bool
    hide_page_guides_front: bool
    hide_card_guides_back: bool
    hide_page_guides_back: bool

    # --- Back printing ---------------------------------------------------
    back_printing: bool = False
    # Whether a double-faced card's transform side prints on its own back
    # (True) or stays a separate card of its own (False, and the historical
    # behaviour). Changes the print-slot count and therefore the page
    # count, which is why it lives beside the layout rather than beside the
    # Back Image. Inert while back_printing is False.
    back_faces_as_reverse: bool = True
    page_order: PageOrderIn = PageOrderIn.INTERLEAVED
    flip_edge: FlipEdgeIn = FlipEdgeIn.LONG
    # Back Pages get their own position offset: duplex registration drifts,
    # and a single shared offset cannot express "the backs land 0.4mm left
    # of the fronts on this printer".
    back_offset_x_mm: float = 0.0
    back_offset_y_mm: float = 0.0
    # Content hash of the project's Selected Back. The bytes themselves are
    # synced separately (POST /api/backs/{hash}) and cached server-side, so
    # this stays a 64-char string on every preview call rather than a
    # multi-MB base64 blob.
    back_image_hash: str | None = None
    back_image_includes_bleed: bool = False
    # Preview-only: render the Back Page of page 1 instead of its front,
    # mirrored exactly as the renderer would. Ignored by the render routes.
    preview_back_page: bool = False


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

    # --- Back printing ---------------------------------------------------
    # How many Reverses would take the Back Image rather than a Back Face.
    # Zero with back printing on means an all-double-faced sheet, which
    # legitimately needs no Back Image at all — which is why "no back
    # selected" is only an error when this is non-zero.
    reverses_needing_back_image: int = 0
    # Back printing is on, at least one Reverse needs the Back Image, and
    # no usable one is present on this server. A blocking error: the
    # alternative is burning a full duplex pass printing blank card backs.
    missing_back_image: bool = False
    # The Back Image is being printed from its plain uploaded original
    # rather than an upscale — usually because it was upscaled on a
    # different generation server. A warning, never a block: printing
    # works, only quality varies.
    back_image_not_upscaled: bool = False
    # Pages the PDF will actually contain, Back Pages included. page_count
    # above stays the count of Front Pages, so the UI can say "9 sheets,
    # 18 pages" without recomputing the doubling itself.
    total_page_count: int = 0


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
    # This position shows the project's Back Image rather than a card.
    # The frontend labels it differently — "Card back" is not a card name,
    # and showing it in the same style would read as a card called that.
    is_back_image: bool = False


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
    # Resolved for the page kind actually being previewed, so the frontend
    # draws what that page will really carry rather than re-deriving which
    # of the four flags applies.
    hide_card_guides: bool
    hide_page_guides: bool
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
    # Where Back Images and their upscales live — a sibling of the two
    # above, deliberately outside both wipe paths. Defaulted so an older
    # server answering a newer client still validates.
    backs_dir: str = ""


class DiscardTagOut(BaseModel):
    # How many pending tasks the discard canceled. The client fires
    # discard fire-and-forget and ignores the body; this is here so a
    # manual `curl` can see what actually happened.
    canceled: int


class CardDbLocalOut(BaseModel):
    # State of this server's imported card corpus (see carddb.py) — absent
    # entirely (CardDbStatusOut.local = None) until a first import has
    # fully finished, since import meta is only written on success.
    dataset_type: str  # "default_cards" | "all_cards"
    dataset_updated_at: str  # Scryfall's updated_at for the imported dump
    imported_at: str
    card_count: int


class CardDbStatusOut(BaseModel):
    # Purely local state — see card_db_status()'s docstring for why this
    # carries no "what does Scryfall have today" half any more.
    local: CardDbLocalOut | None = None
    import_running: bool
    active_job_id: str | None = None


class CardImportIn(BaseModel):
    dataset: str  # "default_cards" | "all_cards" — validated in the router


class CardImportStartedOut(BaseModel):
    job_id: str


class CardImportStatusOut(BaseModel):
    status: str  # "running" | "done" | "failed" | "canceled"
    phase: str  # "checking" | "downloading" | "importing" | "finalizing"
    dataset: str
    bytes_downloaded: int
    total_bytes: int | None = None
    rows_imported: int
    error: str | None = None


class CardLanguagesOut(BaseModel):
    # Languages actually present in the imported corpus, English first —
    # feeds the import-language dropdown, so an English-only corpus
    # naturally offers only English. ["en"] when nothing is imported.
    languages: list[str]


class CardVariantOut(BaseModel):
    scryfall_id: str
    name: str
    printed_name: str | None = None
    set_code: str
    set_name: str | None = None
    collector_number: str
    lang: str
    released_at: str | None = None
    digital: bool
    image_status: str | None = None
    highres_image: bool
    # How many output images one generation of this printing produces per
    # DPI (see scryfall.expected_face_count) — what the picker's coverage
    # indicator compares found gallery-status faces against. Defaulted so
    # older servers' responses still parse.
    face_count: int = 1


class CardVariantsOut(BaseModel):
    # The printing the query anchored on (resolved by scryfall_id, then
    # set+collector, then exact name) plus every printing sharing its
    # oracle_id, sorted for direct display (newest release first).
    anchor: CardVariantOut
    variants: list[CardVariantOut]
    total: int


class GalleryStatusIn(BaseModel):
    # The picker's "already generated?" batch lookup: which of these
    # printings have images in the generated_images registry at this
    # model, at which of these DPIs. POST, not GET — a card can have
    # hundreds of printings, well past sane URL lengths.
    scryfall_ids: list[str]
    model: str
    dpis: list[int]


class GeneratedPairOut(BaseModel):
    dpi: int
    # NULL face_index = single-faced (same convention as gallery rows).
    face_index: int | None = None


class GalleryStatusOut(BaseModel):
    # Keyed by scryfall_id; ids with nothing generated are simply absent.
    # Registry-wide by construction — an image generated under any
    # project counts, and no filesystem is consulted (a row can briefly
    # outlive a deleted file until the next adopt/prune reconcile).
    statuses: dict[str, list[GeneratedPairOut]]


class BackVariantOut(BaseModel):
    """One upscaled variant of a Back Image that exists on this server."""

    id: int
    dpi: int
    model: str
    created_at: str | None = None


class BackImageOut(BaseModel):
    content_hash: str
    # Whether this server holds the bytes. False means the client should
    # sync before rendering or upscaling — the normal state on a server
    # the user just switched to.
    present: bool
    # Effective print DPI of the stored original at card size, or None
    # when nothing is stored.
    source_dpi: float | None = None
    # Below what a decent printer resolves at card size. A warning the
    # client shows, never a block — plenty of people knowingly print a
    # flat logo at low DPI.
    low_resolution: bool = False
    variants: list[BackVariantOut] = []


class BackUpscaleIn(BaseModel):
    model: str
    dpi_targets: list[int]
    tile_size: int = 0
    weights_dir: str = ""


class BackUpscaleOut(BaseModel):
    queued: int
    # Variants that already existed at the requested model/DPI — not an
    # error, just nothing to do.
    skipped: int = 0
    task_ids: list[int] = []


class DeleteBackOut(BaseModel):
    removed: int
