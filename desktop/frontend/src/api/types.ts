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
}

export interface RegenerateGalleryItemRequest {
  // Redo one exact existing variant unchanged — its scryfall_id/png_url/
  // model/dpi come from the stored gallery item server-side, not from
  // the client. tile_size is recalculated from the *current* sidebar
  // setting for that variant's model. output_dir/cache_dir/weights_dir
  // are generation-machine-local paths the server no longer has any
  // other way to know (see ARCHITECTURE.md).
  tile_size?: number;
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
}

export interface WorkerStatus {
  running: boolean;
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
  show_cut_lines?: boolean;
  preferred_dpi?: number | null;
  preferred_model?: string | null;
}

export interface PdfPreview {
  units: number;
  unmatched: string[];
  page_count: number;
}
