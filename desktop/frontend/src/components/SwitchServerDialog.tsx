import { useState } from "react";
import type { RecentHost } from "../api/project";
import { DEFAULT_REMOTE_PORT } from "../connection";
import ModalOverlay from "./ModalOverlay";
import RecentHostsList from "./RecentHostsList";

export interface SwitchServerDialogProps {
  target: "local" | "remote";
  busy: boolean;
  error: string | null;
  onCancel: () => void;
  onConfirm: (host: string, port: number) => void;
  /** Previously-used remote servers — see connection.tsx::recentHosts. */
  recentHosts: RecentHost[];
  onRemoveHost: (entry: RecentHost) => void;
  /** Non-null while the in-flight switch is paused on "the target server
   *  is missing N of this project's custom images" (see
   *  connection.tsx::switchTo). Swaps the dialog body for an upload-or-
   *  cancel choice; onUploadDecision resumes the switch either way. */
  uploadPrompt?: { count: number } | null;
  onUploadDecision?: (accepted: boolean) => void;
}

// Follows CompareDialog's pattern (overlay closes on click, inner panel
// stops propagation) rather than a native confirm(). Deliberate: native
// dialog behaviour inside Tauri's WKWebView is exactly the class of thing
// this app has already been bitten by twice — see
// download.ts::downloadViaBrowser for the download-attribute story.
//
// Bitten a third time since, by a native confirm() that never appeared on
// macOS at all (issue 16). ConfirmDialog is that rule factored out; this
// stays its own component because it asks for a host and a port, not just
// yes or no.
export default function SwitchServerDialog({
  target,
  busy,
  error,
  onCancel,
  onConfirm,
  recentHosts,
  onRemoveHost,
  uploadPrompt,
  onUploadDecision,
}: SwitchServerDialogProps) {
  // Deliberately starts blank rather than prefilled with the current host
  // (e.g. from a prior connect) — a prefilled field made clicking a recent
  // entry that happened to match look like nothing had happened. Leaving it
  // blank means any selection, typed or clicked, is a visible change.
  const [host, setHost] = useState("");
  const [port, setPort] = useState(DEFAULT_REMOTE_PORT);
  const hostReady = target === "local" || (host.trim().length > 0 && port > 0);

  return (
    <ModalOverlay onClick={busy ? undefined : onCancel}>
      <div className="modal modal-sm" onClick={(e) => e.stopPropagation()}>
        <div className="modal-head">
          <span className="modal-title">
            {target === "local" ? "Switch to this device" : "Switch to a remote server"}
          </span>
        </div>

        {/* Mid-switch pause: the target server is missing some of this
            project's custom images, and switchTo is awaiting the answer.
            Swaps the whole body — the host/port form is settled by now,
            and re-showing it under a question invites edits the in-flight
            switch would ignore. Both buttons stay enabled despite `busy`:
            the switch is busy precisely because it's waiting on them. */}
        {uploadPrompt ? (
          <>
            <p className="hint" style={{ marginBottom: 16 }}>
              {uploadPrompt.count} custom image{uploadPrompt.count === 1 ? "" : "s"} in
              this project {uploadPrompt.count === 1 ? "is" : "are"} not on that server
              and must be uploaded before switching.
            </p>
            <div className="modal-actions">
              <button onClick={() => onUploadDecision?.(false)}>Cancel switch</button>
              <button className="btn-primary" onClick={() => onUploadDecision?.(true)}>
                Upload and switch
              </button>
            </div>
          </>
        ) : (
          <>
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
                {/* Fill-only: a click that switched servers outright would
                    be a trap, since the switch resets the UI, so selecting
                    a saved host fills the address field and waits for the
                    Switch button.
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
          <button
            className="btn-primary"
            onClick={() => onConfirm(host.trim(), port)}
            disabled={busy || !hostReady}
          >
            {busy ? "Switching…" : "Switch"}
          </button>
        </div>
          </>
        )}
      </div>
    </ModalOverlay>
  );
}
