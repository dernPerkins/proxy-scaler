import { useEffect, useState } from "react";
import { useConnection } from "../connection";
import { useProject } from "../context/ProjectContext";

// Rendered inside ProjectProvider (see App.tsx) specifically so it can
// reach useProject()'s saveAsync — connection.tsx can't do that itself,
// since ConnectionProvider sits above ProjectProvider in the tree and has
// no project to save. This component's only job is bridging the two: it
// watches the health ping connection.tsx already runs and reacts to it.
export default function ConnectionLostDialog() {
  const connection = useConnection();
  const { mode, remoteHealthy, host } = connection;
  const project = useProject();
  const [open, setOpen] = useState(false);
  const [busy, setBusy] = useState(false);
  const [error, setError] = useState<string | null>(null);

  // Fires once per disconnect, not once per failed ping — remoteHealthy
  // going false to false again isn't a new React state value, so this
  // doesn't refire and spam a dialog every 30s while the server stays
  // down. Deliberately no auto-close on recovery either: if the user
  // hasn't acknowledged a gap in their work yet, a background reconnect
  // shouldn't silently wave it away.
  useEffect(() => {
    if (mode === "remote" && !remoteHealthy) {
      setOpen(true);
      setError(null);
    }
  }, [remoteHealthy, mode]);

  if (!open) return null;

  const canSave = project.projectId != null || project.projectName.trim().length > 0;

  async function handleSave() {
    setBusy(true);
    setError(null);
    try {
      await project.saveAsync();
      setOpen(false);
    } catch (err) {
      setError(
        (err instanceof Error ? err.message : String(err)) +
          " — the server may still be unreachable.",
      );
    } finally {
      setBusy(false);
    }
  }

  async function handleReconnect() {
    setBusy(true);
    setError(null);
    const ok = await connection.reconnect();
    setBusy(false);
    if (ok) {
      setOpen(false);
    } else {
      setError("Still unreachable — check that the server is running and try again.");
    }
  }

  return (
    <div className="modal-overlay" onClick={busy ? undefined : () => setOpen(false)}>
      <div className="modal modal-sm" onClick={(e) => e.stopPropagation()}>
        <div className="modal-head">
          <span className="modal-title">Lost connection to the server</span>
        </div>
        <p style={{ marginBottom: 6 }}>
          Can&apos;t reach {host || "the remote server"} anymore — it may have stopped, or your
          network dropped.
        </p>
        <p className="hint" style={{ marginBottom: 16 }}>
          {remoteHealthy
            ? "Connection recovered — safe to save now."
            : canSave
              ? "Would you like to try saving your work?"
              : "There's no project loaded yet to save."}
        </p>

        {error && (
          <p className="error-text" style={{ marginBottom: 12 }}>
            {error}
          </p>
        )}

        <div className="modal-actions">
          <button onClick={() => setOpen(false)} disabled={busy}>
            Dismiss
          </button>
          {canSave && (
            <button onClick={handleSave} disabled={busy}>
              {busy ? "Saving…" : "Try to save"}
            </button>
          )}
          <button className="btn-primary" onClick={handleReconnect} disabled={busy}>
            {busy ? "Reconnecting…" : "Reconnect"}
          </button>
        </div>
      </div>
    </div>
  );
}
