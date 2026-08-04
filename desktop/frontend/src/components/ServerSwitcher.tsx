import { useState } from "react";
import { useConnection } from "../connection";
import { useProject } from "../context/ProjectContext";
import { isTauri } from "../tauri";
import SwitchServerDialog from "./SwitchServerDialog";

// Local/Remote toggle for the Decklist settings sidebar. The switch
// itself runs in ConnectionProvider, not here — a successful switch
// remounts this component's whole subtree, so any state it owned would
// vanish mid-operation.
export default function ServerSwitcher() {
  const connection = useConnection();
  const project = useProject();
  const [pending, setPending] = useState<"local" | "remote" | null>(null);
  const [busy, setBusy] = useState(false);
  const [error, setError] = useState<string | null>(null);

  // In a plain browser tab there's no sidecar to start or stop, so
  // switching to "local" would just fail at the invoke boundary.
  if (!isTauri()) return null;

  const mode = connection.mode;
  const canSave = project.projectId != null || project.projectName.trim().length > 0;

  function request(target: "local" | "remote") {
    if (target === mode) return;
    setError(null);
    setPending(target);
  }

  async function handleConfirm(save: boolean, host: string) {
    if (!pending) return;
    setBusy(true);
    setError(null);

    if (save) {
      try {
        await project.saveAsync();
      } catch (err) {
        setError(err instanceof Error ? err.message : String(err));
        setBusy(false);
        return;
      }
    }

    const message = await connection.switchTo(
      pending === "local" ? { mode: "local" } : { mode: "remote", host },
    );

    if (message) {
      setError(message);
      setBusy(false);
      return;
    }
    // Success: the connection change bumps the session key, which
    // remounts this component. Deliberately no state updates after here.
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
          {connection.host}
        </p>
      )}

      {pending && (
        <SwitchServerDialog
          target={pending}
          initialHost={connection.host}
          canSave={canSave}
          busy={busy}
          error={error}
          onCancel={() => {
            setPending(null);
            setError(null);
          }}
          onConfirm={handleConfirm}
        />
      )}
    </div>
  );
}
