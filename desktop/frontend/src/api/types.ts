// Mirrors proxy_scaler/api/schemas.py — the generation server's request/
// response shapes only. Project types (mirroring the Rust project_store
// structs) live in api/project.ts instead; see ARCHITECTURE.md for the
// split. Hand-written for now; a later pass can generate these from
// FastAPI's OpenAPI schema (openapi-typescript) so the two sides can't
// silently drift.

export interface ModelOption {
  value: string;
  label: string;
}

// Mirrors DeviceOut — whether the connected server has a real GPU (CUDA
// or Apple MPS) to upscale on, used to pick a sensible default model
// instead of guessing from Local-vs-Remote mode alone. See
// connection.tsx's device probe and ProjectContext.tsx's
// recommendedDefaultModel().
export interface Device {
  kind: "gpu" | "cpu";
  // The actual torch backend behind `kind`. `kind` collapses every GPU
  // backend to "gpu", which isn't enough to choose a default model:
  // Apple Silicon is a real GPU but far slower than CUDA on the heavy
  // transformer models. Optional and widened to `string` on purpose — a
  // server older than this field simply omits it, and torch can grow
  // backend names we don't know about, so consumers must always have a
  // sane branch for "something else."
  backend?: "cuda" | "mps" | "privateuseone" | "cpu" | "unknown" | (string & {});
}

// What both /api/resolve, /api/generate, and /api/pdf take as their card
// list — the client's own raw/parsed decklist entries (see
// project.ts::CardRow), never a server-side project_id.
export interface DeckEntryIn {
  quantity: number;
  name: string;
  set_code?: string | null;
  collector_number?: string | null;
  raw_line?: string;
  // Pinned printing + language preference (see project.ts::CardRow).
  // scryfall_id makes resolution exact server-side; lang steers id-less
  // entries toward the project's preferred language.
  scryfall_id?: string | null;
  lang?: string | null;
}

export interface ResolvedFace {
  scryfall_id: string;
  face_index: number | null;
  face_label: string | null;
  face_name: string;
  card_name: string;
  set_code: string;
  collector_number: string;
  png_url: string;
  image_status: string | null;
  // Printing language — persisted (with scryfall_id) into the local card
  // row after a resolve. Optional: older servers don't send it.
  lang?: string;
  // Localized name as printed on a non-English card; null/absent for
  // English printings.
  printed_name?: string | null;
}

export interface ResolvedCard {
  raw_line: string;
  quantity: number;
  faces: ResolvedFace[];
  warnings: string[];
}

export interface ResolveFailure {
  raw_line: string;
  error: string;
}

export interface ResolveResult {
  resolved: ResolvedCard[];
  failed: ResolveFailure[];
}

export interface GenerateRequest {
  project_tag: string;
  entries: DeckEntryIn[];
  model: string;
  dpi_targets: number[];
  skip_existing?: boolean;
  tile_size?: number;
  output_dir: string;
  cache_dir: string;
  weights_dir: string;
  /** Where Back Images and their upscales live — a sibling of the two
   *  above, deliberately outside both wipe paths. Empty from servers
   *  older than back printing. */
  backs_dir?: string;
}

export interface RegenerateGalleryItemRequest {
  // Redo one exact existing variant unchanged — its scryfall_id/png_url/
  // model/dpi come from the stored gallery item server-side, not from
  // the client. tile_size is recalculated from the *current* sidebar
  // setting for that variant's model. output_dir/cache_dir/weights_dir
  // are generation-machine-local paths the server no longer has any
  // other way to know (see ARCHITECTURE.md).
  // project_tag says whose regeneration this is: gallery rows are global
  // registry entries shared across projects, so the item itself no
  // longer carries one.
  project_tag: string;
  tile_size?: number;
  output_dir: string;
  cache_dir: string;
  weights_dir: string;
}

export interface GenPathsInfo {
  // Absolute paths as resolved on the generation server's machine — in
  // Remote mode these describe the remote host's filesystem, not this
  // one's. See misc.py::get_paths.
  output_dir: string;
  cache_dir: string;
  weights_dir: string;
}

export interface GenerateResult {
  queued: number;
  failed: number;
  task_ids: number[];
  notes: string[];
}

export type TaskStatus = "pending" | "running" | "done" | "failed" | "canceled";

export interface Task {
  id: number;
  project_tag: string | null;
  status: TaskStatus;
  scryfall_id: string;
  face_index: number | null;
  face_label: string | null;
  face_name: string;
  card_name: string;
  set_code: string;
  collector_number: string;
  dpi: number;
  model: string;
  error: string | null;
  created_at: string;
  started_at: string | null;
  completed_at: string | null;
  lang?: string;
}

export interface WorkerStatus {
  running: boolean;
  // True while the worker is started-but-held (the desktop spawn passes
  // --hold-worker; ResumeTasksPrompt releases it). Optional because older
  // or remote servers don't send it — undefined reads as not held.
  held?: boolean;
}

export interface GalleryItem {
  id: number;
  scryfall_id: string;
  face_index: number | null;
  face_label: string | null;
  face_name: string;
  card_name: string;
  set_code: string;
  collector_number: string;
  dpi: number;
  model: string;
  image_filename: string;
  lang?: string;
}

// --- Card corpus (routers/cards.py) ---------------------------------------
// The generation server's locally-imported Scryfall bulk data: import
// lifecycle, languages present, and the printing-variants listing behind
// the change-printing picker.

export type CardDataset = "default_cards" | "all_cards";

export interface CardDbLocal {
  dataset_type: CardDataset;
  dataset_updated_at: string;
  imported_at: string;
  card_count: number;
}

export interface CardDbStatus {
  // null until a first import fully finishes.
  local: CardDbLocal | null;
  import_running: boolean;
  active_job_id: string | null;
}

export interface CardImportStatus {
  status: "running" | "done" | "failed" | "canceled";
  phase: "checking" | "downloading" | "importing" | "finalizing";
  dataset: CardDataset;
  bytes_downloaded: number;
  total_bytes: number | null;
  rows_imported: number;
  error: string | null;
}

export interface CardVariant {
  scryfall_id: string;
  name: string;
  printed_name: string | null;
  set_code: string;
  set_name: string | null;
  collector_number: string;
  lang: string;
  released_at: string | null;
  digital: boolean;
  image_status: string | null;
  highres_image: boolean;
  // How many output images one generation of this printing produces per
  // DPI (2 for a DFC, else 1) — the picker's coverage math compares
  // generated faces against it. Optional: older servers don't send it,
  // and absent reads as 1.
  face_count?: number;
}

export interface CardVariantsResult {
  anchor: CardVariant;
  variants: CardVariant[];
  total: number;
}

// One generated (dpi, face) pair found in the server's generated-images
// registry; face_index null = single-faced (same convention as
// GalleryItem).
export interface GeneratedPair {
  dpi: number;
  face_index: number | null;
}

export interface GalleryStatusResult {
  // Keyed by scryfall_id; ids with nothing generated are simply absent.
  // Cross-project by construction — an image generated under any project
  // counts — and answered from the registry alone (no filesystem stats),
  // so a just-deleted file can linger as generated until the next
  // adopt/prune reconcile.
  statuses: Record<string, GeneratedPair[]>;
}

export interface PdfLayoutRequest {
  project_tag: string;
  entries: DeckEntryIn[];
  project_name?: string;
  page_width_mm: number;
  page_height_mm: number;
  cols: number;
  rows: number;
  bleed_mm?: number;
  spacing_x_mm?: number;
  spacing_y_mm?: number;
  offset_x_mm?: number;
  offset_y_mm?: number;
  guide_width_pt?: number;
  guide_length_mm?: number;
  export_dpi?: number;
  preferred_dpi?: number | null;
  preferred_model?: string | null;

  // Guides — four independent HIDE flags, replacing show_cut_lines. NOT
  // optional: the server requires them and 422s without them, which is
  // deliberately how an out-of-date client finds out rather than silently
  // rendering with guide settings nobody chose. Kept as `hide_*` so the
  // wire, the stored setting and the checkbox all share one polarity.
  hide_card_guides_front: boolean;
  hide_page_guides_front: boolean;
  hide_card_guides_back: boolean;
  hide_page_guides_back: boolean;

  // Back printing.
  back_printing?: boolean;
  /** A double-faced card's transform side prints on its own back rather
   *  than as a separate card. Inert while back_printing is false. */
  back_faces_as_reverse?: boolean;
  /** What fills a Reverse for a card with no transform side. "blank"
   *  needs no Back Image at all. */
  reverse_fill?: ReverseFill;
  page_order?: PageOrder;
  flip_edge?: FlipEdge;
  back_offset_x_mm?: number;
  back_offset_y_mm?: number;
  /** Content hash of the Selected Back. The bytes are synced separately
   *  (sync_back_image), so this stays a short string on preview calls. */
  back_image_hash?: string | null;
  back_image_includes_bleed?: boolean;
  /** Preview only: render page 1's Back Page, mirrored. */
  preview_back_page?: boolean;
}

/** What goes on a Reverse belonging to a card with no transform side.
 *  "blank" leaves it empty — the mode for printing a deck purely so its
 *  double-faced cards get their own backs. The Back Page is still
 *  emitted either way, or the sheet falls out of register. */
export type ReverseFill = "back_image" | "blank";

/** Interleaved is what a duplex driver expects; fronts-then-backs is for
 *  hand-feeding a stack through a single-sided printer. */
export type PageOrder = "interleaved" | "fronts_then_backs";

/** Which edge the printer turns the sheet on. Must match the printer's own
 *  duplex setting — it decides whether a Back Page mirrors its columns or
 *  its rows, and getting it wrong puts every back on the wrong card. */
export type FlipEdge = "long" | "short";

// Mirrors PdfJobOut/PdfJobStatusOut. A render job exists because building
// a sheet costs ~0.7s per unique card image, so the client needs something
// to poll instead of one long opaque POST.
export type PdfJobRequest = PdfLayoutRequest;

export interface PdfJobStarted {
  job_id: string;
  /** Unique source images to process — the progress bar's denominator. */
  total: number;
}

export interface PdfJobStatus {
  status: "rendering" | "done" | "failed" | "canceled";
  completed: number;
  total: number;
  error: string | null;
}

export interface PdfPreview {
  units: number;
  // Decklist entries with no generated image at all, or a DFC entry
  // missing one or more of its faces. Does NOT include gallery images
  // with no matching decklist entry any more — those are silently left
  // out of the print run, not an error.
  missing: string[];
  page_count: number;
  // Cards with no generated image at the selected Preferred DPI. They are
  // excluded from the sheet rather than printed at another resolution, so
  // this must be shown — otherwise they silently disappear.
  missing_at_dpi: string[];

  /** Reverses that would take the Back Image rather than a Back Face.
   *  Zero with back printing on means an all-double-faced sheet, which
   *  legitimately needs no Back Image at all. */
  reverses_needing_back_image: number;
  /** At least one Reverse would come up blank. Blocks the render. */
  missing_back_image: boolean;
  /** Pages the PDF will contain, Back Pages included. page_count stays
   *  the number of sheets you feed the printer. */
  total_page_count: number;
}

// Mirrors PdfPageSlotOut/PdfPagePreviewOut — a distinct, page-1-only
// visual layout preview (geometry + small embedded thumbnails), not to
// be confused with the numbers-only PdfPreview above.
export interface PdfPageSlot {
  card_name: string;
  face_label: string | null;
  model: string | null;
  dpi: number | null;
  thumbnail_data_url: string | null;
  /** This position shows the Back Image rather than a card. Labelled
   *  differently — "Card back" is not a card name. */
  is_back_image?: boolean;
}

export interface PdfPagePreview {
  page_w_mm: number;
  page_h_mm: number;
  cols: number;
  rows: number;
  margin_x_mm: number;
  margin_y_mm: number;
  cell_w_mm: number;
  cell_h_mm: number;
  bled_card_w_mm: number;
  bled_card_h_mm: number;
  bleed_mm: number;
  guide_width_pt: number;
  guide_length_mm: number;
  /** Already resolved for the page kind being previewed, so this doesn't
   *  re-derive which of the four flags applies. */
  hide_card_guides: boolean;
  hide_page_guides: boolean;
  page_count: number;
  /** A Back Page preview returns the FULL grid including empty positions
   *  (cells are placed by mirrored index); a front preview returns only
   *  the occupied prefix. */
  slots: PdfPageSlot[];
}

// --- The Back Library ------------------------------------------------------

/** One uploaded Back Image. Mirrors back_images::BackImage (Rust). The
 *  library is client-side and app-global; a project points at one by id. */
export interface BackImage {
  id: number;
  content_hash: string;
  label: string;
  original_filename: string;
  includes_bleed: boolean;
  width: number;
  height: number;
  created_at: string;
  /** Effective print DPI at card size. Warned about below ~300, never
   *  blocked — plenty of people knowingly print a flat logo at low DPI. */
  source_dpi: number;
}

/** What the connected generation server holds for one Back Image — just
 *  the bytes; Back Images are never upscaled. Mirrors BackImageOut. */
export interface BackImageServerStatus {
  content_hash: string;
  present: boolean;
  source_dpi: number | null;
  /** Below what a decent printer resolves at card size. Since there is no
   *  upscaling, this warning is the only quality signal there is. */
  low_resolution: boolean;
}

export interface BackSyncResult {
  content_hash: string;
  uploaded: boolean;
}
