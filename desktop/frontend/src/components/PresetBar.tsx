import { useState } from "react";

import { getDefaultSettings, useProject } from "../context/ProjectContext";
import type { ProjectSettings } from "../api/project";
import {
  BUILTIN_PRESET_ID,
  matchingPresetId,
  pickPresetFields,
  useSettingsPresets,
  type PresetKind,
  type SettingsPreset,
} from "../settingsPresets";
import { isTauri } from "../tauri";
import GearIcon from "./GearIcon";
import PresetManagerDialog from "./PresetManagerDialog";
import SelectMenu from "./SelectMenu";

/** "Default" as a preset: this kind's fields from the built-in settings. */
function builtinPreset(kind: PresetKind): SettingsPreset {
  return {
    id: BUILTIN_PRESET_ID,
    name: "Default",
    settings: pickPresetFields(kind, getDefaultSettings()),
  };
}

/** Which preset the tab is on: a saved one that matches, else Default if
 *  the built-in values match, else null (Custom). A saved preset wins over
 *  Default when both match — one saved with the built-in values. */
function activePresetId(
  kind: PresetKind,
  presets: SettingsPreset[],
  settings: ProjectSettings,
): string | null {
  return (
    matchingPresetId(presets, settings) ??
    (matchingPresetId([builtinPreset(kind)], settings) ? BUILTIN_PRESET_ID : null)
  );
}

// The first thing in the PDF and ZIP sidebars: pick a preset for this
// tab, or open the manager (the gear) to save, rename, delete, or choose
// which one new projects start from.
//
// "Default" is always the first choice — the built-in values, for when
// you want no preset at all. ★ marks the one new projects start from.
// The dropdown shows whichever choice the settings currently match, and
// "Custom" when they match none — the same convention as the page-size
// dropdown's Custom option, so editing a field after picking a preset
// reads as having left it rather than having changed it.
//
// The gear sits to the right of the dropdown rather than on a row below
// it — the layout export-preset pickers (Lightroom, Media Encoder, OBS
// profiles) use, and one that costs the PDF sidebar no height on a screen
// where Generate already sits near the fold.
export default function PresetBar({ kind, tourId }: { kind: PresetKind; tourId: string }) {
  const { settings, setSettings } = useProject();
  const presetStore = useSettingsPresets(kind);
  const [managerOpen, setManagerOpen] = useState(false);

  // Presets live in the desktop app's store; a plain browser tab has none.
  if (!isTauri()) return null;

  const { presets, defaultId } = presetStore;
  const all = [builtinPreset(kind), ...presets];
  const title = `${kind.toUpperCase()} Presets`;

  function apply(id: string) {
    const preset = all.find((p) => p.id === id);
    if (preset) setSettings((s) => ({ ...s, ...preset.settings }));
  }

  return (
    <div data-tour={tourId} style={{ marginBottom: 18 }}>
      <h3 style={{ marginBottom: 14 }}>Preset</h3>
      <div className="preset-row">
        <SelectMenu
          ariaLabel={`${kind.toUpperCase()} preset`}
          options={all.map((p) => ({
            value: p.id,
            label:
              (p.id === BUILTIN_PRESET_ID ? defaultId == null : p.id === defaultId)
                ? `★ ${p.name}`
                : p.name,
          }))}
          value={activePresetId(kind, presets, settings)}
          placeholder="Custom"
          onChange={apply}
        />
        <button
          type="button"
          className="icon-btn"
          title={title}
          aria-label={title}
          onClick={() => setManagerOpen(true)}
        >
          <GearIcon className="icon-btn-svg" />
        </button>
      </div>

      {managerOpen && (
        <PresetManagerDialog
          kind={kind}
          all={all}
          activeId={activePresetId(kind, presets, settings)}
          store={presetStore}
          onApply={apply}
          onClose={() => setManagerOpen(false)}
        />
      )}
    </div>
  );
}
