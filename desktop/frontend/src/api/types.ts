// Mirrors proxy_scaler/api/schemas.py. Hand-written for now; a later
// pass can generate these from FastAPI's OpenAPI schema
// (openapi-typescript) so the two sides can't silently drift.

export interface ModelOption {
  value: string;
  label: string;
}

export interface ProjectSettings {
  model: string;
  dpi_targets: number[];
  page_size: number;
  skip_existing: boolean;
  output_dir: string;
  cache_dir: string;
  weights_dir: string;
  tile_size: number;
}

export interface ProjectSummary {
  id: number;
  name: string;
  updated_at: string;
}

export interface ProjectDetail {
  id: number;
  name: string;
  import_decklist_text: string;
  settings: ProjectSettings;
  created_at: string;
  updated_at: string;
}

export interface ImportResult {
  added: number;
  skipped: number;
  failed: number;
}

export type TaskStatus = "pending" | "running" | "done" | "failed" | "canceled";

export interface Variant {
  dpi: number;
  model: string;
  status: TaskStatus;
  error: string | null;
  gallery_item_id: number | null;
}

export interface Face {
  face_index: number | null;
  face_label: string | null;
  face_name: string | null;
  variants: Variant[];
}

export interface Card {
  id: number;
  sort_order: number;
  original_import_line: string;
  quantity: number | null;
  card_name: string | null;
  set_code: string | null;
  collector_number: string | null;
  scryfall_id: string | null;
  faces: Face[];
}

export interface GenerateRequest {
  // Importing a decklist always requires a project, so unlike the old
  // Streamlit session-state model there's no "generate before ever
  // saving a project" path for card rows that exist at all.
  project_id: number;
  card_ids?: number[] | null;
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
  // the client. Only tile_size is client-supplied (recalculated from
  // the *current* sidebar setting for that variant's model).
  tile_size?: number;
}

export interface GenerateResult {
  queued: number;
  failed: number;
  task_ids: number[];
  notes: string[];
}

export interface Task {
  id: number;
  project_id: number | null;
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

export interface PdfLayoutRequest {
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
