// The Decklist's import choices — Language, All Languages and Art From —
// as the user last set them, which a new project starts from instead of
// the built-in defaults (ProjectContext::newProjectSettings). Existing
// projects keep their own: these are saved with each project too, and
// opening one never changes what is remembered here. Only a change the
// user makes (ProjectContext::updateSettings) does.
//
// App-global: stored as one JSON blob in Rust's app_settings
// (project_store.rs::get_import_defaults), which never interprets it, so
// it is validated here on the way in.

import type { QueryClient } from "@tanstack/react-query";
import { projectApi, type ProjectSettings } from "./api/project";
import { isTauri } from "./tauri";

export const IMPORT_DEFAULT_FIELDS = ["preferred_lang", "lang_any", "import_art_source"] as const;

type ImportDefaults = Partial<Pick<ProjectSettings, (typeof IMPORT_DEFAULT_FIELDS)[number]>>;

const QUERY_KEY = ["import-defaults"] as const;

export function pickImportFields(settings: ProjectSettings): ImportDefaults {
  return {
    preferred_lang: settings.preferred_lang,
    lang_any: settings.lang_any,
    import_art_source: settings.import_art_source,
  };
}

/** Whether `a` and `b` differ in any remembered field. */
export function importFieldsChanged(a: ProjectSettings, b: ProjectSettings): boolean {
  return IMPORT_DEFAULT_FIELDS.some((key) => a[key] !== b[key]);
}

/** Only fields of the right type and value survive: a hand-edited or
 *  older blob drops what it can't vouch for rather than breaking New. */
function parse(raw: string | null): ImportDefaults {
  if (!raw) return {};
  let data: unknown;
  try {
    data = JSON.parse(raw);
  } catch {
    return {};
  }
  if (typeof data !== "object" || data == null) return {};
  const d = data as Record<string, unknown>;
  const out: ImportDefaults = {};
  if (typeof d.preferred_lang === "string" && d.preferred_lang) out.preferred_lang = d.preferred_lang;
  if (typeof d.lang_any === "boolean") out.lang_any = d.lang_any;
  if (d.import_art_source === "scryfall" || d.import_art_source === "mpc") {
    out.import_art_source = d.import_art_source;
  }
  return out;
}

export function importDefaultsQueryOptions() {
  return {
    queryKey: QUERY_KEY,
    queryFn: async (): Promise<ImportDefaults> =>
      isTauri() ? parse(await projectApi.getImportDefaults()) : {},
    staleTime: Infinity,
  };
}

/** What a new project lays over the built-in defaults. Reads the cache
 *  only (ProjectProvider prefetches it), so it can run inside synchronous
 *  createNew(); empty until something is remembered. */
export function importDefaultsOverlay(queryClient: QueryClient): ImportDefaults {
  return queryClient.getQueryData<ImportDefaults>(QUERY_KEY) ?? {};
}

/** Remember `settings`' import choices for the next new project. Best
 *  effort: a failed write only means the next new project starts from the
 *  previous choice. */
export function saveImportDefaults(queryClient: QueryClient, settings: ProjectSettings) {
  const next = pickImportFields(settings);
  queryClient.setQueryData(QUERY_KEY, next);
  if (isTauri()) void projectApi.setImportDefaults(JSON.stringify(next)).catch(() => {});
}
