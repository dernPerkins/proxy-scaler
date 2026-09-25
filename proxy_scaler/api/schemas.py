"""Pydantic request/response models for the FastAPI layer. TypeScript
types for the frontend are meant to be generated from these via FastAPI's
OpenAPI schema (openapi-typescript), so the Python<->TS boundary stays
checked as both sides evolve."""

from __future__ import annotations

from enum import Enum

from pydantic import BaseModel, Field


class TilePresetOut(BaseModel):
    """One "GPU VRAM" tier of a Vulkan model (upscale.NCNN_TILE_PRESETS).
    The client writes `tile` into the existing tile_size setting; nothing
    else about the request changes."""

    key: str
    label: str
    tile: int


class ModelOptionOut(BaseModel):
    value: str
    label: str
    # Relative-speed wording ("Best quality — slowest" / "Balanced" /
    # "Fastest") — served with the model so every dropdown labels the
    # trade-off consistently instead of each screen inventing its own.
    speed: str
    # Which runtime runs it ("torch" | "ncnn") and the dropdown header it
    # sits under ("Models" | "Vulkan Models"). Defaulted so an older
    # client ignoring them and an older server omitting them both work.
    backend: str = "torch"
    group: str = "Models"
    # Compact badge text ("USV2", "REAF-VK", "USV2-DX"). Per-OS where the
    # graphics API differs, which is why the server owns it. Empty from an
    # older server; the client then falls back to its own table.
    short_label: str = ""
    # The "GPU VRAM" tiers this model's dropdown offers (torch models also
    # get "auto", tile 0; Vulkan ones can't), plus which tier is the
    # default. Empty only from a server older than this field.
    tile_presets: list[TilePresetOut] = []
    default_tile_preset: str | None = None


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
    # Whether a real (non-software) Vulkan GPU is usable for the "Vulkan
    # Models" — independent of `kind`/`backend`, which describe torch's
    # device. False on an older server, so the client must treat absence
    # as "unknown", never as "no".
    vulkan: bool = False
    # ONNX Runtime's WebGPU provider is present (the DAT models on any GPU:
    # Vulkan on Linux, Direct3D 12 on Windows). False on macOS builds.
    webgpu: bool = False


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
    # Set instead of scryfall_id for a Custom Image — a card front the user
    # uploaded, identified by the sha256 of its bytes. `name` is then the
    # display name (normally the filename stem) and set_code/
    # collector_number are absent. See proxy_scaler/customs.py.
    custom_hash: str | None = None


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


class DownloadOriginalsIn(BaseModel):
    # Download-only batch: fetch and cache the ~300 DPI Scryfall originals
    # for each face, no upscaling — tasks are enqueued under the
    # (ORIGINAL_DPI, ORIGINAL_MODEL) sentinel variant (see dpi.py). A
    # separate route rather than a flag on GenerateIn so an older server
    # 404s loudly instead of silently enqueueing upscale work (Pydantic
    # ignores unknown fields). No model/dpi_targets/skip_existing:
    # downloads always target the sentinel and always skip existing.
    # output_dir/weights_dir are only here because generation_tasks
    # requires them NOT NULL — the download handler ignores both.
    project_tag: str
    entries: list[DeckEntryIn]
    output_dir: str
    cache_dir: str
    weights_dir: str


class RefetchOriginalIn(BaseModel):
    # Re-download one face's Scryfall original, overwriting the cached
    # copy (for when Scryfall updates a card's art/scan). Same shape as
    # RegenerateGalleryItemIn minus tile_size (no upscaling happens);
    # the face identity + png_url come from the stored gallery item
    # server-side — any of the face's variants (upscale or download row)
    # carries them.
    project_tag: str
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
    # Exactly one of these identifies the task's face (db migration 008).
    # scryfall_id stays a plain string for older clients that don't know
    # about custom_hash — "" rather than null keeps their parsing intact.
    scryfall_id: str = ""
    custom_hash: str | None = None
    face_index: int | None
    face_label: str | None
    face_name: str
    card_name: str
    set_code: str = ""
    collector_number: str = ""
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
    # Non-null when an unacknowledged GPU→CPU OOM fallback is pending: a
    # small JSON note ({at, task_id, face_name, model}) written by the
    # worker the moment the fallback fired. The client shows a "cancel
    # pending tasks?" dialog and clears it via POST
    # /api/worker/cpu-fallback/ack. Defaulted for older clients.
    cpu_fallback: str | None = None


class GalleryItemOut(BaseModel):
    id: int
    # Exactly one of these identifies the image (db migration 008); see
    # TaskOut above for why scryfall_id stays a "" -defaulted string.
    scryfall_id: str = ""
    custom_hash: str | None = None
    face_index: int | None
    face_label: str | None
    face_name: str
    card_name: str
    set_code: str = ""
    collector_number: str = ""
    dpi: int
    model: str
    image_filename: str
    lang: str = "en"
    # Where inference ran for this image: "gpu" | "cpu" | "unknown"
    # ("unknown" is real — pre-provenance rows and disk-scan recovery).
    # Lets the client mark CPU-fallback output on machines that have a
    # working GPU.
    device: str = "unknown"


class ReverseFillIn(str, Enum):
    BACK_IMAGE = "back_image"
    BLANK = "blank"


class PageOrderIn(str, Enum):
    DUPLEX = "duplex"
    FRONTS_THEN_BACKS = "fronts_then_backs"


class FlipEdgeIn(str, Enum):
    LONG = "long"
    SHORT = "short"


class CutterIn(str, Enum):
    NONE = "none"
    SILHOUETTE = "silhouette"


class CutterMarkStyleIn(str, Enum):
    THREE_POINT = "three_point"
    FOUR_POINT = "four_point"


class CutterOrientationIn(str, Enum):
    PORTRAIT = "portrait"
    LANDSCAPE = "landscape"


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
    # Source the print run from the cached ~300 DPI Scryfall originals
    # (download-only variants) instead of upscaled outputs — when true the
    # preferred_dpi/preferred_model pair is ignored (the routers pass None
    # through), since there's exactly one original per face. See
    # pdf_layout.match_quantities. Defaulted so older clients are
    # unaffected; a NEW client's flag sent to an OLD server is silently
    # dropped (Pydantic ignores unknown fields), which the client guards
    # with a version floor — see desktop/frontend/src/config.ts.
    use_originals: bool = False

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

    # --- Electronic cutter -----------------------------------------------
    #
    # Registration marks for a cutting machine (pdf_layout.RegistrationMarks)
    # and the matching /cut-file export. All defaulted, so a client that
    # predates cutters gets exactly the sheet it always got. The reverse
    # drift — a new client against an old server — is the silent kind
    # again (Pydantic drops the unknown fields and the sheet comes back
    # without marks), and is gated client-side by a version floor, see
    # desktop/frontend/src/config.ts.
    cutter: CutterIn = CutterIn.NONE
    cutter_mark_style: CutterMarkStyleIn = CutterMarkStyleIn.THREE_POINT
    # The orientation the sheet is loaded into the cutter — independent of
    # the page's own orientation (see pdf_layout.cutter_frame).
    cutter_orientation: CutterOrientationIn = CutterOrientationIn.PORTRAIT
    cutter_inset_mm: float = 10.0
    # Same HIDE polarity as the guide flags above, same back-page default,
    # for the same reason: the marks only matter on the side you cut from.
    hide_cutter_marks_front: bool = False
    hide_cutter_marks_back: bool = True

    # --- Back printing ---------------------------------------------------
    back_printing: bool = False
    # Whether a double-faced card's transform side prints on its own back
    # (True) or stays a separate card of its own (False, and the historical
    # behaviour). Changes the print-slot count and therefore the page
    # count, which is why it lives beside the layout rather than beside the
    # Back Image. Inert while back_printing is False.
    back_faces_as_reverse: bool = True
    # What fills a Reverse belonging to a card with no transform side.
    # BLANK leaves it empty and needs no Back Image at all — the mode for
    # printing a deck purely so its double-faced cards get their own
    # backs. The Back Page is still emitted either way, or the sheet
    # falls out of register.
    reverse_fill: ReverseFillIn = ReverseFillIn.BACK_IMAGE
    page_order: PageOrderIn = PageOrderIn.DUPLEX
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
    # How much bleed (mm per side) the Selected Back carries when it
    # declares some. With it, the renderer trims the file's own bleed to
    # the project's, or tops it up — exact for any source. Without it
    # (older clients) the flag alone means "fit the whole file to the
    # bled box", the pre-0.3.3 behaviour.
    back_image_bleed_mm: float | None = Field(default=None, ge=0, le=10)
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


class RectOut(BaseModel):
    """An axis-aligned box on the page, mm, y down."""

    x_mm: float
    y_mm: float
    w_mm: float
    h_mm: float


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
    # Total size of the card grid. Reported so the client can warn when it
    # exceeds the page — resolve_page_layout deliberately does not raise
    # on that (a custom offset may push past an edge on purpose), and asks
    # callers to check instead. Defaulted so an older server still
    # validates against a newer client.
    grid_w_mm: float = 0
    grid_h_mm: float = 0
    # This page's images are drawn upside down. True only for a Back Page
    # whose sheet turns about a horizontal axis (portrait + short edge, or
    # landscape + long edge) — see pdf_layout.back_pages_are_rotated. The
    # preview exists to catch a wrong flip edge, so it has to show this.
    rotated: bool = False
    # Resolved for the page kind actually being previewed, so the frontend
    # draws what that page will really carry rather than re-deriving which
    # of the four flags applies.
    hide_card_guides: bool
    hide_page_guides: bool
    # Cutter registration marks this page carries (empty when there is no
    # cutter, or the marks are hidden on this page kind), the keep-out
    # zones around them, and whether any card intrudes on a zone — a
    # warning the client shows, like grid overflow. Defaulted so an older
    # server's response still validates against a newer client.
    registration_marks: list[RectOut] = []
    registration_keep_out: list[RectOut] = []
    registration_conflict: bool = False
    page_count: int
    slots: list[PdfPageSlotOut]


class ExportFormatIn(str, Enum):
    # "default": every unique matched face once in FRONT/, plus the
    # project's Selected Back (if any) as the only BACK/ entry.
    # "tcgplaytest": the vendor's paired layout — one FRONT/BACK file pair
    # per physical copy, matched by natural filename order, counts equal.
    DEFAULT = "default"
    TCGPLAYTEST = "tcgplaytest"


class ExportImageFormatIn(str, Enum):
    # "png": the stored files as they are (byte-for-byte when no bleed is
    # added). "jpg": every image re-encoded — smaller files, no alpha, so
    # the rounded-corner transparency is flattened first.
    PNG = "png"
    JPG = "jpg"


class ExportZipIn(BaseModel):
    # Same scoping/quantity story as PdfLayoutIn: project_tag picks the
    # gallery, entries carry the quantities, project_name is cosmetic
    # (deck folder + Content-Disposition filename).
    project_tag: str
    entries: list[DeckEntryIn]
    project_name: str = ""
    # Same selector semantics as PdfLayoutIn: preferred_dpi is a hard
    # filter (a face without that DPI is excluded and reported, never
    # substituted); preferred_model wins among the eligible variants.
    preferred_dpi: int | None = None
    preferred_model: str | None = None
    # Same semantics as PdfLayoutIn.use_originals: export the cached ~300
    # DPI Scryfall originals instead of upscaled outputs; the preferred
    # pair is ignored when set.
    use_originals: bool = False
    format: ExportFormatIn = ExportFormatIn.DEFAULT
    # Content hash of the project's Selected Back (see PdfLayoutIn's
    # back_image_hash) — bytes are synced separately via POST /api/backs.
    back_image_hash: str | None = None
    # Output options. Defaults reproduce the original export exactly (the
    # stored PNGs, verbatim), so an older client that sends neither gets
    # what it always got. Everything else re-renders each image — see
    # routers/export.py for the copy-vs-render matrix.
    image_format: ExportImageFormatIn = ExportImageFormatIn.PNG
    # Edge-extend a bleed border of bleed_mm per side onto every image, at
    # its own native DPI — what MakePlayingCards.com and similar vendors
    # expect. 3.0 mm is MPC's spec (63x88 mm trim -> 69x94 mm with bleed).
    # Capped at 10 mm: anything past that is a typo, not a print spec.
    with_bleed: bool = False
    bleed_mm: float = Field(default=3.0, gt=0, le=10)
    # Same meaning as PdfLayoutIn.back_image_includes_bleed: the Selected
    # Back already carries its own bleed, so with_bleed cover-fits it to
    # the bled size instead of edge-extending a second border.
    back_image_includes_bleed: bool = False
    back_image_bleed_mm: float | None = Field(default=None, ge=0, le=10)


class ExportZipPreviewOut(BaseModel):
    # FRONT/ file count of the default format: unique faces, quantities
    # NOT expanded.
    fronts: int
    # FRONT/ (== BACK/) file count of the tcgplaytest format: one pair per
    # physical copy, quantities expanded.
    paired_fronts: int
    # Same meanings as PdfPreviewOut.missing / missing_at_dpi.
    missing: list[str]
    missing_at_dpi: list[str]
    # tcgplaytest slots whose BACK/ entry would be the Selected Back
    # rather than the card's own Back Face. Zero for an all-double-faced
    # deck — but the tcgplaytest export still requires a Selected Back
    # only when this is non-zero.
    reverses_needing_back_image: int


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


class BackImageOut(BaseModel):
    content_hash: str
    # Whether this server holds the bytes. False means the client should
    # sync before rendering — the normal state on a server the user just
    # switched to.
    present: bool
    # Effective print DPI of the stored original at card size, or None
    # when nothing is stored.
    source_dpi: float | None = None
    # Below what a decent printer resolves at card size. A warning the
    # client shows, never a block. Back Images are never upscaled, so this
    # is the only thing standing between a soft source and a soft print —
    # it has to be visible without being a refusal.
    low_resolution: bool = False


class DeleteBackOut(BaseModel):
    removed: int


class CustomImageOut(BaseModel):
    content_hash: str
    # Whether this server holds the bytes. False means the client should
    # sync before generating or exporting — the normal state on a server
    # the user just switched to, and the reason uploads can be lazy.
    present: bool
    # Effective print DPI of the stored original at card size, or None
    # when nothing is stored. Also what the `custom_source` registry row
    # records as its dpi, so the PDF tab can rank it against upscales.
    source_dpi: float | None = None
    # Below what a decent printer resolves at card size. A warning the
    # client shows, never a block — and unlike a Back Image, the user has
    # a real remedy beyond finding a better file: upscale it.
    low_resolution: bool = False
    # The bleed (mm per side) the stored PNG was cropped to carry, by the
    # client's declaration at upload. 0.0 for a plain card-aspect upload.
    # The client compares this with its own declaration on every sync and
    # re-uploads on a mismatch.
    bleed_mm: float = 0.0


class DeleteCustomOut(BaseModel):
    removed: int
