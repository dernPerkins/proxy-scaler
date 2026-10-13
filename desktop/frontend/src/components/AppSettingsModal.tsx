// App Settings: client-side, app-wide preferences, opened from the gear in
// the tab bar's right-hand cluster. Only settings that belong to this
// machine's client live here. Anything that depends on the connected
// generation server (server switcher, card database, directories, upscale
// settings, danger zone) stays in the Decklist sidebar, so switching
// servers updates it where the user expects.
import { useEffect, useState } from "react";
import { useImageCacheSetting } from "../imageOrigin";
import { isTauri } from "../tauri";
import { getUpdateCheckEnabled, setUpdateCheckEnabled } from "../update";
import ModalOverlay from "./ModalOverlay";

// The off switch for the boot-time update check (UpdatePrompt.tsx reads
// the same setting before calling check_for_update). Tauri-only: in a
// plain browser tab there's no check to disable. Reads as unchecked until
// the setting loads — a wrong-for-a-frame checkbox beats an on-by-default
// flash for people who turned it off.
function UpdateCheckToggle() {
  const [enabled, setEnabled] = useState(false);
  useEffect(() => {
    getUpdateCheckEnabled()
      .then(setEnabled)
      .catch(() => setEnabled(true));
  }, []);
  if (!isTauri()) return null;
  return (
    <label className="check">
      <input
        type="checkbox"
        checked={enabled}
        onChange={(e) => {
          const next = e.target.checked;
          setEnabled(next);
          // Best-effort, same reasoning as skipThisVersion: a failed
          // write just means the old behavior next launch.
          void setUpdateCheckEnabled(next).catch(() => {});
        }}
      />
      Check For Updates At Launch
    </label>
  );
}

function ImageCacheToggle() {
  const { enabled, setEnabled } = useImageCacheSetting();
  // Tauri-only like the update toggle: a plain browser tab has no
  // settings store to write to.
  if (!isTauri()) return null;
  return (
    <div className="field">
      <label className="check">
        <input
          type="checkbox"
          checked={enabled ?? true}
          disabled={enabled == null}
          onChange={(e) => setEnabled(e.target.checked)}
        />
        Use Proxy Scaler Image Cache (Recommended)
      </label>
      <p className="hint">
        When Off, sends card image requests straight to Scryfall, with no Proxy Scaler server
        in between. Images are still cached on your generation server.
      </p>
    </div>
  );
}

export default function AppSettingsModal({ onClose }: { onClose: () => void }) {
  useEffect(() => {
    function onKeyDown(e: KeyboardEvent) {
      if (e.key === "Escape") onClose();
    }
    document.addEventListener("keydown", onKeyDown);
    return () => document.removeEventListener("keydown", onKeyDown);
  }, [onClose]);

  return (
    <ModalOverlay onClick={onClose}>
      <div
        className="modal modal-sm app-settings"
        role="dialog"
        aria-label="App Settings"
        onClick={(e) => e.stopPropagation()}
      >
        <div className="modal-head">
          <span className="modal-title">App Settings</span>
          <button className="btn-sm" onClick={onClose} aria-label="Close">
            Close
          </button>
        </div>
        <div className="modal-body app-settings-body">
          <UpdateCheckToggle />
          <ImageCacheToggle />
        </div>
      </div>
    </ModalOverlay>
  );
}
