import { useState } from "react";
import type { RecentHost } from "../api/project";
import { DEFAULT_REMOTE_PORT } from "../connection";
import RecentHostsList from "./RecentHostsList";

export interface SwitchServerDialogProps {
  target: "local" | "remote";
  /** False when there's no project worth saving — hides the save option. */
  canSave: boolean;
  busy: boolean;
  error: string | null;
  onCancel: () => void;
  onConfirm: (save: boolean, host: string, port: number) => void;
  /** Previously-used remote servers — see connection.tsx::recentHosts. */
  recentHosts: RecentHost[];
  onRemoveHost: (entry: RecentHost) => void;
}

// Follows CompareDialog's pattern (overlay closes on click, inner panel
// stops propagation) rather than a native confirm(). Deliberate: native
// dialog behaviour inside Tauri's WKWebView is exactly the class of thing
// this app has already been bitten by twice — see main.rs::save_file for
// the download-attribute story.
export default function SwitchServerDialog({
  target,
  canSave,
  busy,
  error,
  onCancel,
  onConfirm,
  recentHosts,
  onRemoveHost,
}: SwitchServerDialogProps) {
  // Deliberately starts blank rather than prefilled with the current host
  // (e.g. from a prior connect) — a prefilled field made clicking a recent
  // entry that happened to match look like nothing had happened. Leaving it
  // blank means any selection, typed or clicked, is a visible change.
  const [host, setHost] = useState("");
  const [port, setPort] = useState(DEFAULT_REMOTE_PORT);
  const hostReady = target === "local" || (host.trim().length > 0 && port > 0);

  return (
    <div className="modal-overlay" onClick={busy ? undefined : onCancel}>
      <div className="modal modal-sm" onClick={(e) => e.stopPropagation()}>
        <div className="modal-head">
          <span className="modal-title">
            {target === "local" ? "Switch to this device" : "Switch to a remote server"}
          </span>
        </div>

        {target === "remote" && (
          <>
            <div style={{ display: "flex", gap: 8, marginBottom: recentHosts.length > 0 ? 8 : 14 }}>
              <label className="field" style={{ flex: 1 }}>
                <span>Server address</span>
                <input
                  value={host}
                  autoFocus
                  disabled={busy}
                  onChange={(e) => setHost(e.target.value)}
                  placeholder="IP or name (e.g. 100.x.x.x or my-server)"
                />
              </label>
              <label className="field" style={{ width: 90 }}>
                <span>Port</span>
                <input
                  type="number"
                  value={port}
                  disabled={busy}
                  onChange={(e) => setPort(Number(e.target.value) || 0)}
                />
              </label>
            </div>
            {recentHosts.length > 0 && (
              <div style={{ marginBottom: 14 }}>
                <span className="hint" style={{ marginBottom: 4, display: "block" }}>
                  Recent
                </span>
                {/* Fill-only: this dialog exists specifically to force the
                    save-before-switching choice below, so selecting a
                    saved host must not skip straight to switching.
                    `selected` highlights whichever one now matches the
                    (blank-by-default) address field, as a clearer sign a
                    click did something than the field alone. */}
                <RecentHostsList
                  hosts={recentHosts}
                  onSelect={(entry) => {
                    setHost(entry.host);
                    setPort(entry.port);
                  }}
                  onRemove={onRemoveHost}
                  selected={{ host, port }}
                  disabled={busy}
                />
              </div>
            )}
          </>
        )}

        <p style={{ marginBottom: 6 }}>
          Would you like to save the project before switching?
        </p>
        <p className="hint" style={{ marginBottom: 16 }}>
          Switching will cause a reset of the UI.
        </p>

        {error && (
          <p className="error-text" style={{ marginBottom: 12 }}>
            {error}
          </p>
        )}

        <div className="modal-actions">
          <button onClick={onCancel} disabled={busy}>
            Cancel
          </button>
          <button onClick={() => onConfirm(false, host.trim(), port)} disabled={busy || !hostReady}>
            {busy ? "Switching…" : "Switch without saving"}
          </button>
          {canSave && (
            <button
              className="btn-primary"
              onClick={() => onConfirm(true, host.trim(), port)}
              disabled={busy || !hostReady}
            >
              Save &amp; switch
            </button>
          )}
        </div>
      </div>
    </div>
  );
}
