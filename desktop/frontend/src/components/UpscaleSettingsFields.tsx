// The upscale controls every place that queues model work shares: model,
// Target DPI and the "GPU VRAM" tier. Driven by value + onChange rather
// than by a settings context so the same block edits a project's settings
// on the Decklist and the app-global library settings on the Customs and
// Backs tabs (libraryUpscale.ts) — two different stores, one control.
//
// Reads the shared ["models"] query itself (React Query dedupes it across
// pages) and the server-readiness gate for the loading hint. Anything a
// page adds around it — Skip existing, the project's Custom images rule,
// the library tabs' Result mode — stays in that page.

import { useQuery } from "@tanstack/react-query";
import { generationApi } from "../api/generation";
import { DPI_OPTIONS } from "../constants";
import { useServerReadiness } from "../config";
import ModelSelect, { reconcileTileForModel } from "./ModelSelect";

export interface UpscaleFields {
  model: string;
  dpi_targets: number[];
  tile_size: number;
}

interface Props {
  value: UpscaleFields;
  /** Called with only the fields that changed. A model change also
   *  carries the reconciled tile_size (see reconcileTileForModel). */
  onChange: (patch: Partial<UpscaleFields>) => void;
  /** Prefix for the data-tour anchors, so two copies on one screen (or
   *  two pages' tours) never share an anchor name: "" gives the
   *  Decklist's model-select / dpi-targets / vram-select. */
  tourPrefix?: string;
}

export default function UpscaleSettingsFields({ value, onChange, tourPrefix = "" }: Props) {
  const readiness = useServerReadiness();
  // Always read this from the API, never hardcode — see
  // api/generation.ts's listModels comment for the regression this
  // replaced.
  const modelsQuery = useQuery({ queryKey: ["models"], queryFn: () => generationApi.listModels() });
  const selectedModel = modelsQuery.data?.find((m) => m.value === value.model);
  const vramPresets = selectedModel?.tile_presets ?? [];

  function toggleDpi(dpi: number) {
    onChange({
      dpi_targets: value.dpi_targets.includes(dpi)
        ? value.dpi_targets.filter((d) => d !== dpi)
        : [...value.dpi_targets, dpi].sort((a, b) => a - b),
    });
  }

  return (
    <>
      <label className="field" data-tour={`${tourPrefix}model-select`}>
        <span>Upscale model</span>
        <ModelSelect
          value={value.model}
          models={modelsQuery.data}
          disabled={modelsQuery.isLoading || modelsQuery.isError}
          onChange={(model) =>
            onChange({
              model,
              tile_size: reconcileTileForModel(
                modelsQuery.data?.find((m) => m.value === model),
                modelsQuery.data?.find((m) => m.value === value.model),
                value.tile_size,
              ),
            })
          }
        />
      </label>
      {/* Without this, a stuck/failed local-server start (or any other
          listModels() failure) rendered as a silently empty dropdown —
          indistinguishable from "there really are no models" — since
          the .map() above just produces zero <option>s either way. */}
      {modelsQuery.isLoading && (
        <p className="hint">
          {readiness.status === "starting"
            ? "Waiting for the local server to start…"
            : "Loading models…"}
        </p>
      )}
      {modelsQuery.isError && (
        <p className="error-text">
          Couldn't load models:{" "}
          {modelsQuery.error instanceof Error
            ? modelsQuery.error.message
            : String(modelsQuery.error)}
        </p>
      )}

      <div className="field" data-tour={`${tourPrefix}dpi-targets`}>
        <span>Target DPI</span>
        <div className="check-row">
          {DPI_OPTIONS.map((dpi) => (
            <label key={dpi} className="check">
              <input
                type="checkbox"
                checked={value.dpi_targets.includes(dpi)}
                onChange={() => toggleDpi(dpi)}
              />
              {dpi}
            </label>
          ))}
        </div>
      </div>

      {/* One "GPU VRAM" control for every model: the server's tiers
          (upscale.py::VRAM_TIERS), whose tile number rides the existing
          tile_size setting. torch models also offer Auto (tile 0: the
          worker measures free VRAM per task); Vulkan models can't
          probe VRAM, so they default to Medium instead. The raw number
          input remains only for a server older than the tiers. */}
      {vramPresets.length > 0 ? (
        <label className="field" data-tour={`${tourPrefix}vram-select`}>
          <span>GPU VRAM</span>
          <select
            value={vramPresets.find((p) => p.tile === value.tile_size)?.key ?? "custom"}
            onChange={(e) => {
              const preset = vramPresets.find((p) => p.key === e.target.value);
              if (preset) onChange({ tile_size: preset.tile });
            }}
          >
            {vramPresets.map((p) => (
              <option key={p.key} value={p.key}>
                {p.label}
              </option>
            ))}
            {!vramPresets.some((p) => p.tile === value.tile_size) && (
              <option value="custom">Custom ({value.tile_size} px tile)</option>
            )}
          </select>
        </label>
      ) : (
        <label className="field" data-tour={`${tourPrefix}vram-select`}>
          <span>Tile size (0 = auto)</span>
          <input
            type="number"
            min={0}
            step={32}
            value={value.tile_size}
            onChange={(e) => onChange({ tile_size: Number(e.target.value) })}
          />
        </label>
      )}
    </>
  );
}
