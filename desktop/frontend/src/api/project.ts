// The local project store: project CRUD, decklist text, and the parsed
// card list, all via Tauri invoke() against desktop/src-tauri/src/
// project_store.rs — in-process, no network, no server-side project
// concept at all any more. See ARCHITECTURE.md.
import { invokeCommand } from "../tauri";
import type {
  BackImage,
  BackSyncResult,
  CustomImage,
  CustomSyncResult,
  FlipEdge,
  PageOrder,
  ReverseFill,
} from "./types";

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
  preferred_dpi: number | null;
  preferred_model: string | null;
  // Source PDF/export runs from the cached ~300 DPI Scryfall originals
  // instead of upscaled outputs; the preferred pair above is inert while
  // set. Shared by the PDF and Export tabs like the pair itself.
  use_originals: boolean;
  // Import-language preference (Scryfall code, "en" default): the language
  // the resolve-gated import demands ("strictly literal" — see
  // ARCHITECTURE.md's resolve flow).
  preferred_lang: string;
  // The import box's "All Languages" checkbox: best-effort matching across
  // languages instead of strictly preferred_lang.
  lang_any: boolean;
  // Guides: one HIDE flag per guide kind per page kind, replacing the old
  // single show_cut_lines. Back Pages default to hidden — you cut a duplex
  // sheet against the guides on its front, so guides on the back are ink
  // you can't use printed on the side that shows.
  hide_card_guides_front: boolean;
  hide_page_guides_front: boolean;
  hide_card_guides_back: boolean;
  hide_page_guides_back: boolean;
  // Back printing.
  back_printing: boolean;
  back_faces_as_reverse: boolean;
  reverse_fill: ReverseFill;
  page_order: PageOrder;
  flip_edge: FlipEdge;
  back_offset_x_mm: number;
  back_offset_y_mm: number;
  /** This project's Selected Back — an id into the Back Library, or null. */
  back_image_id: number | null;
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
  // Set instead of scryfall_id when this card is a Custom Image — art the
  // user uploaded, which has no Scryfall printing. `custom_image_id` is
  // the local library row; `custom_hash` is the sha256 the *generation
  // server* identifies it by, joined in so the identity string can be
  // built without a second lookup. Both null for a normal card.
  //
  // The frontend keys off these for three things: rendering a "Custom"
  // chip instead of the printing picker, skipping the background
  // re-resolve (which would otherwise see a null scryfall_id and try to
  // look the card up on Scryfall), and syncing the bytes to the server
  // before a generate or an export.
  custom_image_id: number | null;
  custom_hash: string | null;
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
  // Deletes the Unnamed Project row if there is one, returning its tag (for
  // the generation server's discard) or null if there was none. Never
  // creates one — this clears the way for a blank slate, it doesn't mint.
  discardUnnamedProject: () =>
    invokeCommand<string | null>("discard_unnamed_project"),
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

  // --- The Back Library ------------------------------------------------
  //
  // App-global and client-owned: every uploaded Back Image is visible in
  // every project, and a project points at one by id. The bytes never
  // cross this boundary on the way out — Rust hashes the file, writes it,
  // and (syncBackImage) uploads it to the generation server directly.
  // See docs/adr/0003.
  listBackImages: () => invokeCommand<BackImage[]>("list_back_images"),

  /**
   * Add a file to the library. `thumbnail` is a small JPEG the caller
   * already rendered for its own preview — generating it in Rust would
   * mean adding an image-decoding crate to a build that ships in six
   * platform variants, for one 220px picture.
   *
   * Content-addressed: re-adding identical bytes returns the existing
   * entry rather than a duplicate.
   */
  addBackImage: (args: {
    bytes: number[];
    thumbnail: number[];
    originalFilename: string;
    label: string;
    width: number;
    height: number;
  }) => invokeCommand<BackImage>("add_back_image", { ...args }),

  setBackImageLabel: (id: number, label: string) =>
    invokeCommand<void>("set_back_image_label", { id, label }),

  setBackImageIncludesBleed: (id: number, includesBleed: boolean) =>
    invokeCommand<void>("set_back_image_includes_bleed", { id, includesBleed }),

  /** How many projects have this back selected — the delete confirmation
   *  says so out loud, because those projects end up with NO back rather
   *  than inheriting a replacement. */
  countProjectsUsingBackImage: (id: number) =>
    invokeCommand<number>("count_projects_using_back_image", { id }),

  deleteBackImage: (id: number) =>
    invokeCommand<void>("delete_back_image", { id }),

  backImageThumbnail: (id: number) =>
    invokeCommand<string | null>("back_image_thumbnail", { id }),

  getDefaultBackImageId: () =>
    invokeCommand<number | null>("get_default_back_image_id"),

  /** Set the back NEW projects start with. Existing projects keep the id
   *  they copied at creation — a project you printed last month must print
   *  identically today, so the default never reaches back into them. */
  setDefaultBackImageId: (id: number | null) =>
    invokeCommand<void>("set_default_back_image_id", { id }),

  /** Make sure a generation server holds this back's bytes. Cheap to call
   *  unconditionally: Rust GETs first and only uploads on a miss, so
   *  switching servers self-heals through exactly this path. */
  syncBackImage: (id: number, serverBaseUrl: string) =>
    invokeCommand<BackSyncResult>("sync_back_image", { id, serverBaseUrl }),

  // --- The Custom Image library ----------------------------------------
  //
  // The Back Library's shape (app-global, client-owned, content-addressed,
  // bytes never crossing this boundary) applied to card *fronts*. The
  // difference is that these become cards: addCustomCards turns library
  // entries into rows in a project's decklist.
  listCustomImages: () => invokeCommand<CustomImage[]>("list_custom_images"),

  /** Add a file to the library. Content-addressed: re-adding identical
   *  bytes returns the existing entry rather than a duplicate, so the same
   *  art dropped into two projects is one upload and one upscale. */
  addCustomImage: (args: {
    bytes: number[];
    thumbnail: number[];
    originalFilename: string;
    width: number;
    height: number;
  }) => invokeCommand<CustomImage>("add_custom_image", { ...args }),

  setCustomImageLabel: (id: number, label: string) =>
    invokeCommand<void>("set_custom_image_label", { id, label }),

  /** How many project cards use this image — the delete confirmation says
   *  so out loud, because those cards are deleted with it. Unlike a Back
   *  Image, which a project merely selects, a Custom Image IS the card. */
  countCardsUsingCustomImage: (id: number) =>
    invokeCommand<number>("count_cards_using_custom_image", { id }),

  deleteCustomImage: (id: number) =>
    invokeCommand<void>("delete_custom_image", { id }),

  customImageThumbnail: (id: number) =>
    invokeCommand<string | null>("custom_image_thumbnail", { id }),

  /** Add one card per image to a project, appended at the end. Not
   *  resolve-gated, unlike every other import path — there is nothing to
   *  resolve, which is what lets this work with no server reachable. */
  addCustomCards: (projectId: number, customImageIds: number[]) =>
    invokeCommand<CardRow[]>("add_custom_cards", { projectId, customImageIds }),

  /** Make sure a generation server holds this image's bytes. Cheap to call
   *  unconditionally: Rust GETs first and only uploads on a miss. This is
   *  the whole "don't upload until the server needs it" mechanism — call
   *  it before generating and before exporting, never on add. */
  syncCustomImage: (id: number, serverBaseUrl: string) =>
    invokeCommand<CustomSyncResult>("sync_custom_image", { id, serverBaseUrl }),
};

