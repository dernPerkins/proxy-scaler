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
}

export interface CardRow {
  id: number;
  sort_order: number;
  original_import_line: string;
  quantity: number | null;
  name: string;
  set_code: string | null;
  collector_number: string | null;
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
  listProjects: () => invokeCommand<ProjectSummary[]>("list_projects"),
  getProject: (projectId: number) =>
    invokeCommand<LoadedProject>("get_project", { projectId }),
  updateProject: (projectId: number, name: string, settings: ProjectSettings) =>
    invokeCommand<ProjectSummary>("update_project", { projectId, name, settings }),
  deleteProject: (projectId: number) =>
    invokeCommand<void>("delete_project", { projectId }),
  clearAllProjects: () => invokeCommand<void>("clear_all_projects"),

  setDecklistText: (projectId: number, text: string) =>
    invokeCommand<CardRow[]>("set_decklist_text", { projectId, text }),
  removeCard: (cardId: number) => invokeCommand<void>("remove_card", { cardId }),

  getLastProjectId: () => invokeCommand<number | null>("get_last_project_id"),
  setLastProjectId: (projectId: number) =>
    invokeCommand<void>("set_last_project_id", { projectId }),

  // Remembered remote server addresses (see connection.tsx) — not project
  // data, but the same app_settings-backed store, so it lives here
  // alongside the other app_settings-backed calls above.
  listRecentHosts: () => invokeCommand<string[]>("list_recent_hosts"),
  addRecentHost: (host: string) => invokeCommand<string[]>("add_recent_host", { host }),
  removeRecentHost: (host: string) => invokeCommand<string[]>("remove_recent_host", { host }),
};
