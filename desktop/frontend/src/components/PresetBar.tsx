import { useEffect, useState } from "react";

import { useProject } from "../context/ProjectContext";
import { matchingPresetId, useSettingsPresets, type PresetKind } from "../settingsPresets";
import { isTauri } from "../tauri";
import GearIcon from "./GearIcon";
import ModalOverlay from "./ModalOverlay";
import PresetManagerDialog from "./PresetManagerDialog";
import SelectMenu from "./SelectMenu";

// The first thing in the PDF and ZIP sidebars: pick a saved preset for
// this tab, save the current settings as one, or open the manager.
//
// Save and Manage sit as icon buttons to the right of the dropdown rather
// than on a row below it — the layout export-preset pickers (Lightroom,
// Media Encoder, OBS profiles) use, and one that costs the PDF sidebar no
// height on a screen where Generate already sits near the fold.
//
// The dropdown is a SelectMenu rather than a <select> so its list hangs
// from it instead of macOS's menu floating over it. It shows whichever
// preset the settings currently match, and "Custom" when they match none — the same convention as the page-size
// dropdown's Custom option, so editing a field after picking a preset
// reads as having left it rather than having changed it.
export default function PresetBar({ kind, tourId }: { kind: PresetKind; tourId: string }) {
  const { settings, setSettings } = useProject();
  const { presets, defaultId, create, rename, remove, setDefault } = useSettingsPresets(kind);
  const [saveOpen, setSaveOpen] = useState(false);
  const [managerOpen, setManagerOpen] = useState(false);

  // Presets live in the desktop app's store; a plain browser tab has none.
  if (!isTauri()) return null;

  const matched = matchingPresetId(presets, settings);

  function apply(id: string) {
    const preset = presets.find((p) => p.id === id);
    if (preset) setSettings((s) => ({ ...s, ...preset.settings }));
  }

  return (
    <div data-tour={tourId} style={{ marginBottom: 18 }}>
      <h3 style={{ marginBottom: 14 }}>Preset</h3>
      <div className="preset-row">
        <SelectMenu
          ariaLabel={`${kind.toUpperCase()} preset`}
          options={presets.map((p) => ({
            value: p.id,
            label: p.id === defaultId ? `${p.name} (default)` : p.name,
          }))}
          value={matched}
          placeholder={presets.length === 0 ? "No presets yet" : "Custom"}
          disabled={presets.length === 0}
          onChange={apply}
        />
        <button
          type="button"
          className="icon-btn"
          title="Save current settings as a preset"
          aria-label="Save current settings as a preset"
          onClick={() => setSaveOpen(true)}
        >
          <span aria-hidden="true">+</span>
        </button>
        <button
          type="button"
          className="icon-btn"
          title="Manage presets"
          aria-label="Manage presets"
          onClick={() => setManagerOpen(true)}
        >
          <GearIcon className="icon-btn-svg" />
        </button>
      </div>

      {saveOpen && (
        <SavePresetDialog
          kind={kind}
          isFirst={presets.length === 0}
          onSave={(name) => {
            if (create(name, settings)) setSaveOpen(false);
          }}
          onCancel={() => setSaveOpen(false)}
        />
      )}
      {managerOpen && (
        <PresetManagerDialog
          kind={kind}
          presets={presets}
          defaultId={defaultId}
          rename={rename}
          remove={remove}
          setDefault={setDefault}
          onClose={() => setManagerOpen(false)}
        />
      )}
    </div>
  );
}

function SavePresetDialog({
  kind,
  isFirst,
  onSave,
  onCancel,
}: {
  kind: PresetKind;
  isFirst: boolean;
  onSave: (name: string) => void;
  onCancel: () => void;
}) {
  const [name, setName] = useState("");
  // Shown only once the user has tried to save, so an empty field doesn't
  // open already scolding them.
  const [attempted, setAttempted] = useState(false);
  const error = name.trim() ? null : "Give the preset a name.";

  useEffect(() => {
    function onKeyDown(e: KeyboardEvent) {
      if (e.key === "Escape") onCancel();
    }
    document.addEventListener("keydown", onKeyDown);
    return () => document.removeEventListener("keydown", onKeyDown);
  }, [onCancel]);

  function submit() {
    setAttempted(true);
    if (!error) onSave(name);
  }

  return (
    <ModalOverlay onClick={onCancel}>
      <div
        className="modal modal-sm"
        role="dialog"
        aria-label="Save preset"
        onClick={(e) => e.stopPropagation()}
      >
        <div className="modal-head">
          <span className="modal-title">Save {kind.toUpperCase()} preset</span>
        </div>
        <form
          className="field-group"
          style={{ marginBottom: 16 }}
          onSubmit={(e) => {
            e.preventDefault();
            submit();
          }}
        >
          <label className="field">
            <span>Name</span>
            <input
              type="text"
              autoFocus
              value={name}
              maxLength={60}
              placeholder={kind === "pdf" ? "e.g. Home printer, Letter" : "e.g. MakePlayingCards"}
              onChange={(e) => setName(e.target.value)}
            />
          </label>
          {attempted && error && <p className="error-text">{error}</p>}
          <p className="hint">
            Saves this tab&apos;s current settings
            {kind === "pdf" ? " (layout, guides, back printing and cutter)" : " (format and bleed)"}.
            Source images and card sorting aren&apos;t included.
            {isFirst && " Your first preset becomes the default for new projects."}
          </p>
        </form>
        <div className="modal-actions">
          <button type="button" onClick={onCancel}>
            Cancel
          </button>
          <button type="button" className="btn-primary" onClick={submit}>
            Save preset
          </button>
        </div>
      </div>
    </ModalOverlay>
  );
}
