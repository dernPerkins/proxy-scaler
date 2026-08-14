import { useEffect, useState } from "react";
import { useConnection } from "../connection";

// Watches the health ping connection.tsx already runs and tells the user
// when it stops answering. It offers no save: project data lives in the
// local store and reaches it on change (see ARCHITECTURE.md), so a server
// that has gone away can't take any of it with it. Nothing here touches
// the project at all any more.
export default function ConnectionLostDialog() {
  const connection = useConnection();
  const { mode, remoteHealthy, host, port } = connection;
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
        <p style={{ marginBottom: 16 }}>
          Can&apos;t reach {host ? `${host}:${port}` : "the remote server"} anymore — it may have
          stopped, or your network dropped.
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
          <button className="btn-primary" onClick={handleReconnect} disabled={busy}>
            {busy ? "Reconnecting…" : "Reconnect"}
          </button>
        </div>
      </div>
    </div>
  );
}
