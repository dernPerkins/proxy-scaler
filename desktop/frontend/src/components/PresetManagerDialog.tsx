import { useEffect, useRef, useState } from "react";

import type { PresetKind, SettingsPreset } from "../settingsPresets";
import ConfirmDialog from "./ConfirmDialog";
import ModalOverlay from "./ModalOverlay";

// One tab's presets, opened from the gear beside the preset dropdown:
// rename, choose which one new projects start from, or delete. "Built-in defaults" is a default
// choice of its own, for going back to the app's shipped values without
// deleting anything.
export default function PresetManagerDialog({
  kind,
  presets,
  defaultId,
  rename,
  remove,
  setDefault,
  onClose,
}: {
  kind: PresetKind;
  presets: SettingsPreset[];
  defaultId: string | null;
  rename: (id: string, name: string) => void;
  remove: (id: string) => void;
  setDefault: (id: string | null) => void;
  onClose: () => void;
}) {
  const [deleting, setDeleting] = useState<SettingsPreset | null>(null);
  const title = `${kind.toUpperCase()} presets`;

  useEffect(() => {
    function onKeyDown(e: KeyboardEvent) {
      // While the delete confirm is up, Esc is its Cancel, not ours.
      if (e.key === "Escape" && !deleting) onClose();
    }
    document.addEventListener("keydown", onKeyDown);
    return () => document.removeEventListener("keydown", onKeyDown);
  }, [onClose, deleting]);

  const radioName = `preset-default-${kind}`;

  return (
    <ModalOverlay onClick={deleting ? undefined : onClose}>
      <div
        className="modal modal-sm preset-manager"
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

        <div className="modal-body">
          {presets.length === 0 ? (
            <p className="hint" style={{ marginBottom: 12 }}>
              No presets yet. Use the + beside the preset dropdown to save this tab&apos;s
              current settings as one.
            </p>
          ) : (
            <>
              <p className="hint" style={{ marginBottom: 10 }}>
                The selected default is what new projects start with.
              </p>
              <ul className="preset-list">
                <li className="preset-item">
                  <input
                    type="radio"
                    name={radioName}
                    aria-label="Use built-in defaults for new projects"
                    checked={defaultId == null}
                    onChange={() => setDefault(null)}
                  />
                  <span className="preset-builtin">Built-in defaults</span>
                </li>
                {presets.map((p) => (
                  <PresetRow
                    key={p.id}
                    preset={p}
                    radioName={radioName}
                    isDefault={p.id === defaultId}
                    onSetDefault={() => setDefault(p.id)}
                    onRename={(name) => rename(p.id, name)}
                    onDelete={() => setDeleting(p)}
                  />
                ))}
              </ul>
            </>
          )}
        </div>
      </div>

      {deleting && (
        <ConfirmDialog
          title="Delete preset?"
          confirmLabel="Delete"
          onConfirm={() => {
            remove(deleting.id);
            setDeleting(null);
          }}
          onCancel={() => setDeleting(null)}
        >
          &ldquo;{deleting.name}&rdquo; will be deleted. Projects that used it keep their
          settings.
          {deleting.id === defaultId && " New projects will start from the built-in defaults."}
        </ConfirmDialog>
      )}
    </ModalOverlay>
  );
}

// Same feel as the project bar's name field (ProjectBar.tsx): written once
// typing stops, Enter skips the wait.
const NAME_COMMIT_DEBOUNCE_MS = 500;

function PresetRow({
  preset,
  radioName,
  isDefault,
  onSetDefault,
  onRename,
  onDelete,
}: {
  preset: SettingsPreset;
  radioName: string;
  isDefault: boolean;
  onSetDefault: () => void;
  onRename: (name: string) => void;
  onDelete: () => void;
}) {
  const [draft, setDraft] = useState(preset.name);
  const commitTimer = useRef<ReturnType<typeof setTimeout> | null>(null);
  // The name waiting on the timer, so closing the dialog mid-debounce
  // still saves it rather than dropping what was typed.
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
    <li className="preset-item">
      <input
        type="radio"
        name={radioName}
        aria-label={`Use ${preset.name} for new projects`}
        checked={isDefault}
        onChange={onSetDefault}
      />
      <div className="preset-name">
        <input
          type="text"
          aria-label="Preset name"
          value={draft}
          maxLength={60}
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
      </div>
      <button type="button" className="btn-sm btn-danger" onClick={onDelete}>
        Delete
      </button>
    </li>
  );
}
