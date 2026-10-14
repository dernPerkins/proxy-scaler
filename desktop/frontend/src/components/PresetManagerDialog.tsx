import { useEffect, useLayoutEffect, useRef, useState } from "react";

import { useProject } from "../context/ProjectContext";
import {
  BUILTIN_PRESET_ID,
  type PresetKind,
  type SettingsPreset,
  type SettingsPresetStore,
} from "../settingsPresets";
import ConfirmDialog from "./ConfirmDialog";
import ModalOverlay from "./ModalOverlay";

// "PDF Presets" / "ZIP Presets", opened from the gear beside the preset
// dropdown. Master–detail: the presets in a table on the left, the
// selected one's settings on the right (today its name and whether new
// projects start from it).
//
// Selecting a row does two things: it puts that preset on the tab, as
// picking it from the dropdown would, and it shows its settings. Default
// (the built-in values) is always the first row; it can be chosen for new
// projects but not renamed or deleted.
//
// The table scrolls inside a capped height rather than growing the
// dialog, so a long list never pushes the dialog past the window.
export default function PresetManagerDialog({
  kind,
  all,
  activeId,
  store,
  onApply,
  onClose,
}: {
  kind: PresetKind;
  /** Default first, then the saved presets. */
  all: SettingsPreset[];
  /** The preset the tab currently matches, or null for Custom. */
  activeId: string | null;
  store: SettingsPresetStore;
  onApply: (id: string) => void;
  onClose: () => void;
}) {
  const { settings } = useProject();
  const [selectedId, setSelectedId] = useState<string | null>(activeId);
  const [deleting, setDeleting] = useState<SettingsPreset | null>(null);
  const [newName, setNewName] = useState("");
  const tableRef = useRef<HTMLDivElement>(null);
  const title = `${kind.toUpperCase()} Presets`;
  const selected = all.find((p) => p.id === selectedId) ?? null;

  useEffect(() => {
    function onKeyDown(e: KeyboardEvent) {
      // While the delete confirm is up, Esc is its Cancel, not ours; an
      // Esc a dropdown already handled isn't ours either.
      if (e.key === "Escape" && !deleting && !e.defaultPrevented) onClose();
    }
    document.addEventListener("keydown", onKeyDown);
    return () => document.removeEventListener("keydown", onKeyDown);
  }, [onClose, deleting]);

  // A long list opens scrolled to the row that's selected.
  useLayoutEffect(() => {
    tableRef.current
      ?.querySelector<HTMLElement>(`[data-preset-id="${selectedId}"]`)
      ?.scrollIntoView({ block: "nearest" });
  }, [selectedId]);

  function select(id: string) {
    setSelectedId(id);
    onApply(id);
  }

  function createFromCurrent() {
    // Saved from what the tab shows now, so it is already the tab's
    // preset — selected, nothing to apply.
    const preset = store.create(newName, settings);
    if (!preset) return;
    setNewName("");
    setSelectedId(preset.id);
  }

  const isNewProjectDefault = (id: string) =>
    id === BUILTIN_PRESET_ID ? store.defaultId == null : id === store.defaultId;

  return (
    <ModalOverlay onClick={deleting ? undefined : onClose}>
      <div
        className="modal preset-manager"
        role="dialog"
        aria-label={title}
        onClick={(e) => e.stopPropagation()}
      >
        <div className="modal-head">
          <span className="modal-title">{title}</span>
          <button className="btn-sm" onClick={onClose} aria-label="Close">
            Close
          </button>
        </div>

        <div className="preset-split">
          <div className="preset-split-list">
            <div className="preset-table-wrap" ref={tableRef}>
              <table className="preset-table">
                <thead>
                  <tr>
                    <th>Name</th>
                    <th className="preset-table-flag">New projects</th>
                  </tr>
                </thead>
                <tbody>
                  {all.map((p) => (
                    <tr
                      key={p.id}
                      data-preset-id={p.id}
                      className={p.id === selectedId ? "selected" : undefined}
                      aria-selected={p.id === selectedId}
                      tabIndex={0}
                      onClick={() => select(p.id)}
                      onKeyDown={(e) => {
                        if (e.key === "Enter" || e.key === " ") {
                          e.preventDefault();
                          select(p.id);
                        }
                      }}
                    >
                      <td>
                        {p.name}
                        {p.id === BUILTIN_PRESET_ID && (
                          <span className="preset-table-note"> · built-in</span>
                        )}
                      </td>
                      <td className="preset-table-flag">
                        {isNewProjectDefault(p.id) ? "★" : ""}
                      </td>
                    </tr>
                  ))}
                </tbody>
              </table>
            </div>

            <form
              className="preset-new"
              onSubmit={(e) => {
                e.preventDefault();
                createFromCurrent();
              }}
            >
              <input
                type="text"
                aria-label="New preset name"
                placeholder="New preset from current settings"
                maxLength={60}
                value={newName}
                onChange={(e) => setNewName(e.target.value)}
              />
              <button type="submit" className="btn-sm" disabled={!newName.trim()}>
                Save
              </button>
            </form>
            <p className="hint">
              Saves this tab&apos;s current settings
              {kind === "pdf" ? " (layout, guides, back printing and cutter)" : " (format and bleed)"}.
              {" Presets do not save the Source Images related settings."}
              {store.presets.length === 0 &&
                " Your first preset becomes the starting point for new projects."}
            </p>
          </div>

          <div className="preset-split-detail">
            <h3>Settings</h3>
            {selected ? (
              <PresetSettings
                key={selected.id}
                preset={selected}
                isNewProjectDefault={isNewProjectDefault(selected.id)}
                onRename={(name) => store.rename(selected.id, name)}
                onSetDefault={(on) => store.setDefault(on ? selected.id : null)}
                onDelete={() => setDeleting(selected)}
              />
            ) : (
              <p className="hint">Select a preset to see its settings.</p>
            )}
          </div>
        </div>
      </div>

      {deleting && (
        <ConfirmDialog
          title="Delete preset?"
          confirmLabel="Delete"
          onConfirm={() => {
            store.remove(deleting.id);
            // The tab keeps the deleted preset's values, which now match
            // nothing; leave the settings pane empty rather than point at
            // a row the tab isn't on.
            setSelectedId(null);
            setDeleting(null);
          }}
          onCancel={() => setDeleting(null)}
        >
          &ldquo;{deleting.name}&rdquo; will be deleted. Projects that used it keep their
          settings.
          {isNewProjectDefault(deleting.id) && " New projects will start from Default."}
        </ConfirmDialog>
      )}
    </ModalOverlay>
  );
}

// Same feel as the project bar's name field (ProjectBar.tsx): written once
// typing stops, Enter skips the wait.
const NAME_COMMIT_DEBOUNCE_MS = 500;

/** The selected preset's settings. Keyed by preset id, so switching rows
 *  starts a fresh name draft (after saving any pending one). */
function PresetSettings({
  preset,
  isNewProjectDefault,
  onRename,
  onSetDefault,
  onDelete,
}: {
  preset: SettingsPreset;
  isNewProjectDefault: boolean;
  onRename: (name: string) => void;
  onSetDefault: (on: boolean) => void;
  onDelete: () => void;
}) {
  const builtin = preset.id === BUILTIN_PRESET_ID;
  const [draft, setDraft] = useState(preset.name);
  const commitTimer = useRef<ReturnType<typeof setTimeout> | null>(null);
  // The name waiting on the timer, so switching rows or closing the dialog
  // mid-debounce still saves it rather than dropping what was typed.
  const queuedName = useRef<string | null>(null);
  const onRenameRef = useRef(onRename);
  onRenameRef.current = onRename;

  function cancelPendingCommit() {
    if (commitTimer.current != null) {
      clearTimeout(commitTimer.current);
      commitTimer.current = null;
    }
    queuedName.current = null;
  }

  // Restarted per keystroke, so a name is written once, when typing stops.
  function scheduleCommit(name: string) {
    cancelPendingCommit();
    queuedName.current = name;
    commitTimer.current = setTimeout(() => commitName(name), NAME_COMMIT_DEBOUNCE_MS);
  }

  // A blank name is never written (useSettingsPresets.rename ignores it);
  // blur puts the stored name back.
  function commitName(name: string) {
    cancelPendingCommit();
    if (name.trim() && name.trim() !== preset.name) onRenameRef.current(name);
  }

  useEffect(
    () => () => {
      const queued = queuedName.current;
      if (commitTimer.current != null) clearTimeout(commitTimer.current);
      if (queued != null && queued.trim()) onRenameRef.current(queued);
    },
    [],
  );

  return (
    <div className="field-group">
      <label className="field">
        <span>Name</span>
        <input
          type="text"
          value={draft}
          maxLength={60}
          disabled={builtin}
          placeholder="Preset name"
          onChange={(e) => {
            setDraft(e.target.value);
            scheduleCommit(e.target.value);
          }}
          onKeyDown={(e) => {
            if (e.key === "Enter") commitName(draft);
          }}
          onBlur={() => {
            if (draft.trim()) return;
            cancelPendingCommit();
            setDraft(preset.name);
          }}
        />
      </label>
      <label className="check">
        <input
          type="checkbox"
          checked={isNewProjectDefault}
          // Default is the fallback: it stops being the new-project choice
          // when another preset is ticked, not by being unticked itself.
          disabled={builtin && isNewProjectDefault}
          onChange={(e) => onSetDefault(e.target.checked)}
        />
        Use for new projects
      </label>
      {builtin ? (
        <p className="hint">The built-in values. Choose this to have no preset on the tab.</p>
      ) : (
        <div>
          <button type="button" className="btn-sm btn-danger" onClick={onDelete}>
            Delete preset
          </button>
        </div>
      )}
    </div>
  );
}
