// Named settings presets for the PDF and ZIP tabs.
//
// A preset is a saved bundle of one tab's own settings — page size,
// offsets, guides, back printing and cutter on the PDF tab; format and
// bleed on the ZIP tab — that can be applied to the open project in one
// pick. The two kinds are separate stores: a PDF preset never touches the
// ZIP tab and vice versa. The settings the tabs share (Source images, Card
// sorting) are left out of both on purpose, so applying a preset on one
// tab can never change what the other shows.
//
// One preset per kind can be the default, which is what a new project
// starts from instead of the built-in defaults (ProjectContext::
// newProjectSettings). The first preset saved becomes the default; later
// ones don't.
//
// App-global, not per-project: stored as one JSON blob per kind in Rust's
// app_settings (project_store.rs::get_settings_presets), which never
// interprets it.

import { useMutation, useQuery, useQueryClient, type QueryClient } from "@tanstack/react-query";
import { projectApi, type ProjectSettings } from "./api/project";
import { isTauri } from "./tauri";

export type PresetKind = "pdf" | "zip";

/** The id "Default" goes by in the preset dropdown and manager. It isn't a
 *  stored preset: it is the tab's built-in values, i.e. no preset
 *  (PresetBar.tsx::builtinPreset). */
export const BUILTIN_PRESET_ID = "__builtin";

export interface SettingsPreset {
  id: string;
  name: string;
  /** Only this kind's fields. A field missing here — one added to the tab
   *  after the preset was saved — is simply left alone when applying. */
  settings: Partial<ProjectSettings>;
}

export interface PresetStore {
  version: 1;
  default_id: string | null;
  presets: SettingsPreset[];
}

type Validator = (v: unknown) => boolean;

const isNumber: Validator = (v) => typeof v === "number" && Number.isFinite(v);
const isBool: Validator = (v) => typeof v === "boolean";
const isHexColor: Validator = (v) => typeof v === "string" && /^#[0-9A-Fa-f]{6}$/.test(v);
const oneOf =
  (...values: string[]): Validator =>
  (v) => typeof v === "string" && values.includes(v);

// Each kind's fields, with what a stored value has to look like to be
// trusted. A value that fails (a blob from an older build, a hand edit) is
// dropped rather than applied, so a bad preset can only do less, never
// break the tab.
const PDF_FIELDS = {
  page_width_mm: isNumber,
  page_height_mm: isNumber,
  cols: isNumber,
  rows: isNumber,
  bleed_mm: isNumber,
  spacing_x_mm: isNumber,
  spacing_y_mm: isNumber,
  offset_x_mm: isNumber,
  offset_y_mm: isNumber,
  export_dpi: isNumber,
  guide_width_pt: isNumber,
  guide_length_mm: isNumber,
  hide_card_guides_front: isBool,
  hide_page_guides_front: isBool,
  hide_card_guides_back: isBool,
  hide_page_guides_back: isBool,
  page_guide_width_pt: isNumber,
  card_guide_color: isHexColor,
  page_guide_color: isHexColor,
  card_guide_line: oneOf("solid", "dashed"),
  page_guide_line: oneOf("solid", "dashed"),
  card_guide_corners: oneOf("square", "rounded"),
  back_printing: isBool,
  back_faces_as_reverse: isBool,
  reverse_fill: oneOf("back_image", "blank"),
  page_order: oneOf("duplex", "fronts_then_backs"),
  flip_edge: oneOf("long", "short"),
  back_offset_x_mm: isNumber,
  back_offset_y_mm: isNumber,
  cutter: oneOf("none", "silhouette"),
  cutter_mark_style: oneOf("three_point", "four_point"),
  cutter_orientation: oneOf("portrait", "landscape"),
  cutter_inset_mm: isNumber,
  hide_cutter_marks_front: isBool,
  hide_cutter_marks_back: isBool,
} satisfies Partial<Record<keyof ProjectSettings, Validator>>;

const ZIP_FIELDS = {
  export_image_format: oneOf("png", "jpg"),
  export_with_bleed: isBool,
  export_bleed_mm: isNumber,
  export_reverse_fill: oneOf("back_image", "blank"),
  export_back_faces_as_reverse: isBool,
} satisfies Partial<Record<keyof ProjectSettings, Validator>>;

const FIELDS: Record<PresetKind, Partial<Record<keyof ProjectSettings, Validator>>> = {
  pdf: PDF_FIELDS,
  zip: ZIP_FIELDS,
};

function fieldsOf(kind: PresetKind): (keyof ProjectSettings)[] {
  return Object.keys(FIELDS[kind]) as (keyof ProjectSettings)[];
}

/** This kind's fields out of a full settings object — what a preset saves. */
export function pickPresetFields(
  kind: PresetKind,
  settings: ProjectSettings,
): Partial<ProjectSettings> {
  const out: Record<string, unknown> = {};
  for (const key of fieldsOf(kind)) out[key] = settings[key];
  return out as Partial<ProjectSettings>;
}

function emptyStore(): PresetStore {
  return { version: 1, default_id: null, presets: [] };
}

/** Whatever Rust stored, cleaned up field by field — a blob written by an
 *  older build, or a corrupt one, never breaks the tab. */
function parse(raw: string | null, kind: PresetKind): PresetStore {
  if (!raw) return emptyStore();
  try {
    const obj = JSON.parse(raw) as Partial<PresetStore>;
    const validators = FIELDS[kind];
    const presets: SettingsPreset[] = [];
    for (const p of Array.isArray(obj.presets) ? obj.presets : []) {
      if (!p || typeof p.id !== "string" || typeof p.name !== "string" || !p.name.trim()) continue;
      const settings: Record<string, unknown> = {};
      const stored = (p.settings ?? {}) as Record<string, unknown>;
      for (const [key, valid] of Object.entries(validators)) {
        if (key in stored && valid(stored[key])) settings[key] = stored[key];
      }
      presets.push({ id: p.id, name: p.name.trim(), settings });
    }
    const defaultId =
      typeof obj.default_id === "string" && presets.some((p) => p.id === obj.default_id)
        ? obj.default_id
        : null;
    return { version: 1, default_id: defaultId, presets };
  } catch {
    return emptyStore();
  }
}

function sameValue(a: unknown, b: unknown): boolean {
  // Within a hair, as matchPagePreset does: number inputs round-trip
  // through text and SQLite REAL, so exact float equality is too strict.
  if (typeof a === "number" && typeof b === "number") return Math.abs(a - b) < 0.005;
  return a === b;
}

/** Does `preset` store exactly this kind's fields of `settings` — every
 *  one of them, at these values? Unlike matchingPresetId, a field the
 *  preset predates counts as a difference: this asks whether saving
 *  `settings` over the preset would change it. */
export function presetHoldsSettings(
  kind: PresetKind,
  preset: SettingsPreset,
  settings: ProjectSettings,
): boolean {
  return Object.entries(pickPresetFields(kind, settings)).every(
    ([key, value]) =>
      key in preset.settings &&
      sameValue(preset.settings[key as keyof ProjectSettings], value),
  );
}

/** The preset the current settings are an exact match for, if any — what
 *  the dropdown shows as selected. Fields a preset doesn't store are not
 *  compared. */
export function matchingPresetId(
  presets: SettingsPreset[],
  settings: ProjectSettings,
): string | null {
  const hit = presets.find((p) =>
    Object.entries(p.settings).every(([key, value]) =>
      sameValue(settings[key as keyof ProjectSettings], value),
    ),
  );
  return hit?.id ?? null;
}

export function presetQueryKey(kind: PresetKind) {
  return ["settings-presets", kind] as const;
}

export function presetQueryOptions(kind: PresetKind) {
  return {
    queryKey: presetQueryKey(kind),
    queryFn: async (): Promise<PresetStore> =>
      isTauri() ? parse(await projectApi.getSettingsPresets(kind), kind) : emptyStore(),
    staleTime: Infinity,
  };
}

/** Both kinds' default presets merged into one patch — what a new project
 *  lays over the built-in defaults. Reads the cache only (ProjectProvider
 *  prefetches it), so it can run inside synchronous createNew(); empty if
 *  nothing has loaded or no default is set. */
export function defaultPresetOverlay(queryClient: QueryClient): Partial<ProjectSettings> {
  let overlay: Partial<ProjectSettings> = {};
  for (const kind of ["pdf", "zip"] as const) {
    const store = queryClient.getQueryData<PresetStore>(presetQueryKey(kind));
    const preset = store?.presets.find((p) => p.id === store.default_id);
    if (preset) overlay = { ...overlay, ...preset.settings };
  }
  return overlay;
}

function newId(): string {
  return typeof crypto !== "undefined" && "randomUUID" in crypto
    ? crypto.randomUUID()
    : `${Date.now().toString(36)}-${Math.random().toString(36).slice(2)}`;
}

/** One tab's presets: read once, written through on every change (a
 *  preset edit is a click, not a drag, so no debounce is needed). */
export function useSettingsPresets(kind: PresetKind) {
  const queryClient = useQueryClient();
  const query = useQuery(presetQueryOptions(kind));
  const store = query.data ?? emptyStore();
  const write = useMutation({
    mutationFn: (next: PresetStore) => projectApi.setSettingsPresets(kind, JSON.stringify(next)),
    // A failed write leaves the cache ahead of the store; reread it so
    // the UI shows what actually persisted.
    onError: () => queryClient.invalidateQueries({ queryKey: presetQueryKey(kind) }),
  });

  // Each change starts from the cache rather than the `store` this render
  // closed over: a rename lands from a debounce timer, and building it on
  // a render-old store would undo whatever changed in between (a new
  // default picked while the name was still settling).
  function update(change: (current: PresetStore) => PresetStore) {
    const current = queryClient.getQueryData<PresetStore>(presetQueryKey(kind)) ?? store;
    const next = change(current);
    queryClient.setQueryData(presetQueryKey(kind), next);
    write.mutate(next);
  }

  /** Saves the current settings as a new preset and returns it, or null
   *  for a blank name. Names needn't be unique. The first preset becomes
   *  the default. */
  function create(name: string, settings: ProjectSettings): SettingsPreset | null {
    if (!name.trim()) return null;
    const preset: SettingsPreset = {
      id: newId(),
      name: name.trim(),
      settings: pickPresetFields(kind, settings),
    };
    update((current) => ({
      ...current,
      default_id: current.presets.length === 0 ? preset.id : current.default_id,
      presets: [...current.presets, preset],
    }));
    return preset;
  }

  /** Replaces a preset's settings with this kind's fields of `settings`,
   *  keeping its name and whether it is the default. Also how a preset
   *  saved before a field existed picks that field up. */
  function overwrite(id: string, settings: ProjectSettings) {
    const fields = pickPresetFields(kind, settings);
    update((current) => ({
      ...current,
      presets: current.presets.map((p) => (p.id === id ? { ...p, settings: fields } : p)),
    }));
  }

  /** A blank name is ignored, as the project name field ignores one. */
  function rename(id: string, name: string) {
    const trimmed = name.trim();
    if (!trimmed) return;
    update((current) => ({
      ...current,
      presets: current.presets.map((p) => (p.id === id ? { ...p, name: trimmed } : p)),
    }));
  }

  /** Deleting the default leaves no default — new projects go back to the
   *  built-in values — rather than quietly promoting some other preset. */
  function remove(id: string) {
    update((current) => ({
      ...current,
      default_id: current.default_id === id ? null : current.default_id,
      presets: current.presets.filter((p) => p.id !== id),
    }));
  }

  function setDefault(id: string | null) {
    update((current) => ({ ...current, default_id: id }));
  }

  return {
    presets: store.presets,
    defaultId: store.default_id,
    isLoading: query.isLoading,
    create,
    overwrite,
    rename,
    remove,
    setDefault,
  };
}

export type SettingsPresetStore = ReturnType<typeof useSettingsPresets>;
