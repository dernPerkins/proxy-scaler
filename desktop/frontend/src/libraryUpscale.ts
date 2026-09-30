// Upscaling straight from the Customs and Backs tabs.
//
// No project is involved: the libraries belong to the machine, so the
// settings here are app-global (one JSON blob in Rust's app_settings,
// separate from any project's Decklist settings and from the project's
// "Custom images" bulk rule), the server queues the work under its fixed
// library tag, and status is read by identity across every tag — the
// badges show whatever any project or this tab ever made for the image.

import { useMutation, useQuery, useQueryClient } from "@tanstack/react-query";
import { generationApi } from "./api/generation";
import { projectApi } from "./api/project";
import type { BackImage, CustomImage, LibraryStatus, GenerateResult } from "./api/types";
import type { ModelOption } from "./api/types";
import { getApiBaseUrl } from "./config";
import { DEFAULT_GEN_PATHS, DPI_OPTIONS } from "./constants";
import { recommendedDefaultModel } from "./context/ProjectContext";
import { syncCustomItems } from "./syncCustoms";
import { reconcileTileForModel } from "./components/ModelSelect";
import { statusForPairs, type VariantStatus } from "./mergeCardStatus";

export type LibraryUpscaleMode = "target" | "native";

export interface LibraryUpscaleSettings {
  model: string;
  dpi_targets: number[];
  tile_size: number;
  /** "target" resizes to each selected DPI the upload doesn't reach;
   *  "native" keeps the model's 4x result, capped at 2400 DPI. */
  mode: LibraryUpscaleMode;
}

const SETTINGS_KEY = ["library-upscale-settings"] as const;

function defaults(): LibraryUpscaleSettings {
  return { model: recommendedDefaultModel(), dpi_targets: [1200], tile_size: 0, mode: "target" };
}

/** Whatever Rust stored, with every field defaulted — a blob written by
 *  an older build, or a corrupt one, never breaks the tab. */
function parse(raw: string | null): LibraryUpscaleSettings {
  const base = defaults();
  if (!raw) return base;
  try {
    const obj = JSON.parse(raw) as Partial<LibraryUpscaleSettings>;
    const dpis = Array.isArray(obj.dpi_targets)
      ? obj.dpi_targets.filter((d): d is number => DPI_OPTIONS.includes(d as number))
      : base.dpi_targets;
    return {
      model: typeof obj.model === "string" && obj.model ? obj.model : base.model,
      dpi_targets: dpis.length ? dpis : base.dpi_targets,
      tile_size: typeof obj.tile_size === "number" ? obj.tile_size : base.tile_size,
      mode: obj.mode === "native" ? "native" : "target",
    };
  } catch {
    return base;
  }
}

/** The library tabs' upscale settings: read once, written through on
 *  every change (these are dropdowns and checkboxes, not sliders, so no
 *  debounce is needed). `update` takes a patch, as UpscaleSettingsFields
 *  hands out. */
export function useLibraryUpscaleSettings(models: ModelOption[] | undefined): {
  settings: LibraryUpscaleSettings;
  update: (patch: Partial<LibraryUpscaleSettings>) => void;
} {
  const queryClient = useQueryClient();
  const query = useQuery({
    queryKey: SETTINGS_KEY,
    queryFn: async () => parse(await projectApi.getLibraryUpscaleSettings()),
    staleTime: Infinity,
  });
  const settings = query.data ?? defaults();
  const write = useMutation({
    mutationFn: (next: LibraryUpscaleSettings) =>
      projectApi.setLibraryUpscaleSettings(JSON.stringify(next)),
  });
  function update(patch: Partial<LibraryUpscaleSettings>) {
    const next = { ...settings, ...patch };
    // A model switch across runtimes resets the tier, exactly as the
    // Decklist's control does; UpscaleSettingsFields already sends the
    // reconciled tile with the model, this only covers callers that don't.
    if (patch.model !== undefined && patch.tile_size === undefined) {
      next.tile_size = reconcileTileForModel(
        models?.find((m) => m.value === patch.model),
        models?.find((m) => m.value === settings.model),
        settings.tile_size,
      );
    }
    queryClient.setQueryData(SETTINGS_KEY, next);
    write.mutate(next);
  }
  return { settings, update };
}

export type LibraryKind = "custom" | "back";

/** Sync the image to the connected server (customs: the remote upload
 *  dialog included; backs: the Rust sync), then queue its upscale under
 *  the library tag. Throws UploadCanceled if the user cancels an upload. */
export async function upscaleLibraryImage(
  kind: LibraryKind,
  image: CustomImage | BackImage,
  settings: LibraryUpscaleSettings,
  serverVersion: string | null,
): Promise<GenerateResult> {
  if (kind === "custom") {
    await syncCustomItems(
      [{ id: image.id, hash: image.content_hash, label: image.label }],
      serverVersion,
    );
  } else {
    await projectApi.syncBackImage(image.id, getApiBaseUrl());
  }
  return generationApi.upscaleLibraryImage({
    kind,
    content_hash: image.content_hash,
    label: image.label,
    model: settings.model,
    dpi_targets: settings.dpi_targets,
    tile_size: settings.tile_size,
    mode: settings.mode,
    ...DEFAULT_GEN_PATHS,
  });
}

export function libraryStatusKey(kind: LibraryKind, contentHash: string) {
  return ["library-status", kind, contentHash] as const;
}

/** Live status for one library image, polled while `enabled` (the image
 *  is selected and the server is reachable). Same 3 s cadence as the
 *  Decklist's status poll. */
export function useLibraryStatus(
  kind: LibraryKind,
  contentHash: string | null,
  enabled: boolean,
): { variants: VariantStatus[]; status: LibraryStatus | undefined } {
  const query = useQuery({
    queryKey: libraryStatusKey(kind, contentHash ?? ""),
    queryFn: () => generationApi.libraryStatus(kind, contentHash as string),
    enabled: enabled && contentHash != null,
    refetchInterval: 3000,
  });
  const status = query.data;
  // One image is one face, so the pair merge needs no per-face grouping.
  const variants = status ? statusForPairs(status.gallery, status.tasks).filter((v) => v.status !== "canceled") : [];
  return { variants, status };
}

/** The DPI the server measures for this image: a custom's bleed-aware
 *  source_dpi (the server crops it to its declared box), but a back's
 *  plain long-edge measure — the server holds no bleed for a back and
 *  measures it uncropped (proxy_scaler/backs.py::source_dpi). */
export function serverSourceDpi(kind: LibraryKind, image: CustomImage | BackImage): number {
  if (kind === "custom") return image.source_dpi;
  return Math.max(image.width, image.height) / (88 / 25.4);
}

/** Which of the selected targets this image would actually be upscaled
 *  to — the server's rule (only targets above the upload's DPI, or one
 *  native variant), mirrored so the button can say "nothing to do"
 *  before a round-trip. */
export function targetsFor(
  kind: LibraryKind,
  image: CustomImage | BackImage,
  settings: LibraryUpscaleSettings,
): number[] {
  const source = Math.round(serverSourceDpi(kind, image));
  const below = settings.dpi_targets.filter((t) => t > source).sort((a, b) => a - b);
  if (below.length === 0) return [];
  return settings.mode === "native" ? [Math.min(source * 4, 2400)] : below;
}
