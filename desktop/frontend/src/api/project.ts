// The local project store: project CRUD, decklist text, and the parsed
// card list, all via Tauri invoke() against desktop/src-tauri/src/
// project_store.rs — in-process, no network, no server-side project
// concept at all any more. See ARCHITECTURE.md.
import { invokeCommand } from "../tauri";

export interface ProjectSummary {
  id: number;
  tag: string;
  name: string;
  updated_at: string;
}

export interface ProjectSettings {
  model: string;
  dpi_targets: number[];
  skip_existing: boolean;
  tile_size: number;
  // PDF tab layout settings — mirrors PdfLayoutRequest (minus project_tag/
  // entries/project_name, which are per-request, not per-project). See
  // desktop/src-tauri/src/project_store.rs::ProjectSettings.
  page_width_mm: number;
  page_height_mm: number;
  cols: number;
  rows: number;
  bleed_mm: number;
  spacing_x_mm: number;
  spacing_y_mm: number;
  offset_x_mm: number;
  offset_y_mm: number;
  guide_width_pt: number;
  guide_length_mm: number;
  export_dpi: number;
  show_cut_lines: boolean;
  preferred_dpi: number | null;
  preferred_model: string | null;
  // Import-language preference (Scryfall code, "en" default): the language
  // the resolve-gated import demands ("strictly literal" — see
  // ARCHITECTURE.md's resolve flow).
  preferred_lang: string;
  // The import box's "All Languages" checkbox: best-effort matching across
  // languages instead of strictly preferred_lang.
  lang_any: boolean;
}

export interface CardRow {
  id: number;
  sort_order: number;
  original_import_line: string;
  quantity: number | null;
  name: string;
  set_code: string | null;
  collector_number: string | null;
  // Authoritative link to one exact Scryfall printing — null until the
  // post-import resolve pins it (or the user picks a printing). name/
  // set_code/collector_number stay as the offline display cache.
  scryfall_id: string | null;
  lang: string | null;
  // Localized name as printed on a non-English printing; null for English.
  // Display-only — `name` (English/oracle) stays the matching identity.
  printed_name: string | null;
}

// A parsed-but-unresolved decklist line, as returned by the Rust parser
// (parse_decklist). The first half of the resolve-gated import.
export interface ParsedDeckEntry {
  quantity: number;
  name: string;
  set_code: string | null;
  collector_number: string | null;
  raw_line: string;
}

// One card as it returns from a successful resolve — everything
// import_resolved_cards needs to insert a fully-pinned row. Field names
// are the Rust struct's snake_case (Tauri only camelCases top-level
// command arguments, not struct fields).
export interface ResolvedImportCard {
  raw_line: string;
  quantity: number;
  name: string;
  printed_name: string | null;
  set_code: string;
  collector_number: string;
  scryfall_id: string;
  lang: string;
}

export interface RecentHost {
  host: string;
  port: number;
}

export interface LoadedProject {
  id: number;
  tag: string;
  name: string;
  import_decklist_text: string;
  settings: ProjectSettings;
  cards: CardRow[];
  created_at: string;
  updated_at: string;
}

export const projectApi = {
  createProject: (name: string) => invokeCommand<ProjectSummary>("create_project", { name }),
  // The Unnamed Project — an ordinary row with name = '', created on first
  // call and returned unchanged afterwards (project_store.rs). Called from
  // the write paths that need a row to write against, never speculatively:
  // an app installed and never used holds no row at all.
  getOrCreateUnnamedProject: () =>
    invokeCommand<ProjectSummary>("get_or_create_unnamed_project"),
  listProjects: () => invokeCommand<ProjectSummary[]>("list_projects"),
  getProject: (projectId: number) =>
    invokeCommand<LoadedProject>("get_project", { projectId }),
  updateProject: (projectId: number, name: string, settings: ProjectSettings) =>
    invokeCommand<ProjectSummary>("update_project", { projectId, name, settings }),
  deleteProject: (projectId: number) =>
    invokeCommand<void>("delete_project", { projectId }),
  clearAllProjects: () => invokeCommand<void>("clear_all_projects"),

  // Additive: adds any new cards parsed out of `text` to the project's
  // existing list, deduped against what's already there (see
  // project_store.rs::import_decklist_text) — never removes cards.
  // Legacy path: the resolve-gated import below replaced it in the UI.
  importDecklistText: (projectId: number, text: string) =>
    invokeCommand<CardRow[]>("import_decklist_text", { projectId, text }),
  // Parse only, no DB writes — the first half of the resolve-gated import.
  parseDecklist: (text: string) =>
    invokeCommand<ParsedDeckEntry[]>("parse_decklist", { text }),
  // The second half: insert already-resolved (fully pinned) cards in one
  // transaction, deduped like importDecklistText. Returns the full list.
  importResolvedCards: (projectId: number, text: string, cards: ResolvedImportCard[]) =>
    invokeCommand<CardRow[]>("import_resolved_cards", { projectId, text, cards }),
  removeCard: (cardId: number) => invokeCommand<void>("remove_card", { cardId }),
  // Clamped to a minimum of 1 on the Rust side; removal stays removeCard's job.
  setCardQuantity: (cardId: number, quantity: number) =>
    invokeCommand<void>("set_card_quantity", { cardId, quantity }),
  // Change one card to a different printing (picked from the server's
  // variants endpoint): pins scryfall_id and refreshes the display cache.
  setCardPrinting: (
    cardId: number,
    printing: {
      scryfallId: string;
      name: string;
      setCode: string;
      collectorNumber: string;
      lang: string;
      printedName: string | null;
    },
  ) =>
    invokeCommand<void>("set_card_printing", {
      cardId,
      scryfallId: printing.scryfallId,
      name: printing.name,
      setCode: printing.setCode,
      collectorNumber: printing.collectorNumber,
      lang: printing.lang,
      printedName: printing.printedName,
    }),
  // Batched persist of post-import resolve results — one transaction for
  // the whole decklist. Field names are the Rust struct's snake_case:
  // Tauri only camelCases top-level command arguments, not struct fields.
  setCardsResolution: (
    updates: {
      card_id: number;
      scryfall_id: string;
      name: string;
      set_code: string;
      collector_number: string;
      lang: string;
      printed_name: string | null;
    }[],
  ) => invokeCommand<void>("set_cards_resolution", { updates }),

  getLastProjectId: () => invokeCommand<number | null>("get_last_project_id"),
  setLastProjectId: (projectId: number) =>
    invokeCommand<void>("set_last_project_id", { projectId }),

  // The quit prompt's "Don't ask again" (see QuitPrompt.tsx). Same
  // app_settings-backed store as the two calls above; false until the box
  // is ticked, and until this store has ever been asked.
  getQuitPromptSuppressed: () => invokeCommand<boolean>("get_quit_prompt_suppressed"),
  setQuitPromptSuppressed: (suppressed: boolean) =>
    invokeCommand<void>("set_quit_prompt_suppressed", { suppressed }),

  // The printing picker's "Show digital" — an app-wide user preference
  // (same app_settings store), not per-popover state. False until ticked.
  getShowDigitalPrintings: () => invokeCommand<boolean>("get_show_digital_printings"),
  setShowDigitalPrintings: (show: boolean) =>
    invokeCommand<void>("set_show_digital_printings", { show }),

  // The boot card-database offer's "Don't ask again" (CardDbPrompt.tsx).
  getCardDbPromptDismissed: () =>
    invokeCommand<boolean>("get_card_db_prompt_dismissed"),
  setCardDbPromptDismissed: (dismissed: boolean) =>
    invokeCommand<void>("set_card_db_prompt_dismissed", { dismissed }),

  // Remembered remote server address+port pairs (see connection.tsx) — not
  // project data, but the same app_settings-backed store, so it lives here
  // alongside the other app_settings-backed calls above.
  listRecentHosts: () => invokeCommand<RecentHost[]>("list_recent_hosts"),
  addRecentHost: (host: string, port: number) =>
    invokeCommand<RecentHost[]>("add_recent_host", { host, port }),
  removeRecentHost: (host: string, port: number) =>
    invokeCommand<RecentHost[]>("remove_recent_host", { host, port }),
};
