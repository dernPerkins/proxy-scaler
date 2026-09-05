import { useState } from "react";
import { useConnection } from "../connection";
import { isTauri } from "../tauri";
import SwitchServerDialog from "./SwitchServerDialog";

// Local/Remote toggle for the Decklist settings sidebar. The switch
// itself runs in ConnectionProvider, not here — a successful switch
// remounts this component's whole subtree, so any state it owned would
// vanish mid-operation.
export default function ServerSwitcher() {
  const connection = useConnection();
  const [pending, setPending] = useState<"local" | "remote" | null>(null);
  const [busy, setBusy] = useState(false);
  const [error, setError] = useState<string | null>(null);
  // Non-null while switchTo is paused on "the target server is missing N
  // of this project's custom images — upload them?" (see
  // connection.tsx::switchTo). The resolve callback is the paused
  // promise's other half; answering it resumes the switch either way.
  const [uploadPrompt, setUploadPrompt] = useState<{
    count: number;
    resolve: (accepted: boolean) => void;
  } | null>(null);

  // In a plain browser tab there's no sidecar to start or stop, so
  // switching to "local" would just fail at the invoke boundary.
  if (!isTauri()) return null;

  const mode = connection.mode;

  function request(target: "local" | "remote") {
    if (target === mode) return;
    setError(null);
    setPending(target);
  }

  // No save step ahead of the switch: the project is in the local store
  // already, whether or not it has a name, and the generation server on
  // the other side of this toggle never held it.
  async function handleConfirm(host: string, port: number) {
    if (!pending) return;
    setBusy(true);
    setError(null);

    const message = await connection.switchTo(
      pending === "local" ? { mode: "local" } : { mode: "remote", host, port },
      {
        confirmCustomsUpload: (count) =>
          new Promise<boolean>((resolve) => setUploadPrompt({ count, resolve })),
      },
    );

    setBusy(false);
    if (message) {
      setError(message);
      return;
    }
    // Success — close the dialog. This component itself doesn't remount
    // on a switch (project data no longer needs to; see connection.tsx),
    // so this has to be done explicitly rather than relying on unmount.
    setPending(null);
  }

  async function handleReconnect() {
    setBusy(true);
    setError(null);
    const ok = await connection.reconnect();
    setBusy(false);
    if (!ok) {
      setError("Still unreachable — check that the server is running.");
    }
  }

  return (
    <div style={{ marginBottom: 16 }}>
      <div className="field" style={{ marginBottom: 6 }}>
        <span>Generation server</span>
        <div className="segmented">
          <button
            className={mode === "local" ? "active" : ""}
            onClick={() => request("local")}
            disabled={busy}
          >
            This device
          </button>
          <button
            className={mode === "remote" ? "active" : ""}
            onClick={() => request("remote")}
            disabled={busy}
          >
            Remote
          </button>
        </div>
      </div>
      {mode === "remote" && connection.host && (
        <p className="hint mono" style={{ wordBreak: "break-all" }}>
          {connection.host}:{connection.port}
        </p>
      )}

      {mode === "remote" && !connection.remoteHealthy && (
        <>
          <p className="error-text" style={{ marginTop: 4 }}>
            Disconnected from the remote server.
          </p>
          <button
            className="btn-sm btn-danger btn-block"
            onClick={handleReconnect}
            disabled={busy}
            style={{ marginTop: 4 }}
          >
            {busy ? "Reconnecting…" : "Reconnect"}
          </button>
        </>
      )}

      {error && !pending && <p className="error-text" style={{ marginTop: 6 }}>{error}</p>}

      {pending && (
        <SwitchServerDialog
          target={pending}
          busy={busy}
          error={error}
          onCancel={() => {
            setPending(null);
            setError(null);
          }}
          onConfirm={handleConfirm}
          recentHosts={connection.recentHosts}
          onRemoveHost={connection.removeRecentHost}
          uploadPrompt={uploadPrompt && { count: uploadPrompt.count }}
          onUploadDecision={(accepted) => {
            uploadPrompt?.resolve(accepted);
            setUploadPrompt(null);
          }}
        />
      )}
    </div>
  );
}
