import { useEffect } from "react";
import { useServerReadiness } from "../config";
import { useConnection } from "../connection";
import ModalOverlay from "./ModalOverlay";

// Owns the local sidecar's startup wait, and its failure.
//
// This used to be a corner toast (ServerStatusToast, which now keeps only
// the "it's running" confirmation). The problem with a toast was never
// that it was quiet — it was that the app behind it looked ready and
// wasn't: applyTarget marks the connection "connected" the moment Local is
// picked, without awaiting the spawn, so ConnectGate renders everything
// while every generation request sits parked inside waitForServerReady().
// Buttons that do nothing are worse than a wait you can see, so the wait
// is now held.
//
// Local-only by construction — setServerStarting() is reached on no other
// path, since Remote confirms reachability before the gate ever opens.
//
// Mounted inside App, which is also how it is dismissed: "Change server"
// puts the connection back to the picker, ConnectGate stops rendering its
// children, and this goes with them. Re-picking then resets the readiness
// gate on its own (Local calls setServerStarting, which replaces the
// rejected promise; Remote calls setServerReady), so there is no gate
// state to unwind here.
export default function ServerBootModal() {
  const readiness = useServerReadiness();
  const { setStatus, retryLocal } = useConnection();
  const failed = readiness.status === "error";

  // Esc means "let me out" — but only once there is something to be let
  // out of. During the start there is deliberately no way to dismiss.
  useEffect(() => {
    if (!failed) return;
    function onKeyDown(e: KeyboardEvent) {
      if (e.key === "Escape") setStatus({ kind: "picker" });
    }
    document.addEventListener("keydown", onKeyDown);
    return () => document.removeEventListener("keydown", onKeyDown);
  }, [failed, setStatus]);

  if (readiness.status === "ready") return null;

  if (readiness.status === "starting") {
    // No onClick: dismissing would hand back an app whose every server
    // request is still parked, which is the state this exists to stop
    // anyone reaching. Same contract as DownloadProgressModal.
    return (
      <ModalOverlay className="modal-overlay-boot">
        <div className="modal modal-sm">
          <div className="modal-head">
            <span className="modal-title">Starting the generation server</span>
          </div>
          <p style={{ marginBottom: 4 }}>
            Getting this device ready to resolve and upscale cards. The first
            launch after an update takes longer than the rest.
          </p>
          <div className="progress progress-indeterminate">
            <div className="progress-fill" />
          </div>
        </div>
      </ModalOverlay>
    );
  }

  return (
    <ModalOverlay className="modal-overlay-boot" onClick={() => setStatus({ kind: "picker" })}>
      <div className="modal modal-sm" onClick={(e) => e.stopPropagation()}>
        <div className="modal-head">
          <span className="modal-title">Couldn&apos;t start the generation server</span>
        </div>

        {/* main.rs keeps the sidecar's last stderr lines for exactly this —
            it is usually the specific reason, not a generic timeout. */}
        <p className="error-text" style={{ marginBottom: 12 }}>
          {readiness.message}
        </p>

        <p className="hint" style={{ marginBottom: 16 }}>
          Try again, or point this app at a proxy-scaler server running
          elsewhere. If it keeps happening, please{" "}
          <a
            href="https://github.com/dernPerkins/proxy-scaler/issues"
            target="_blank"
            rel="noreferrer"
          >
            report it on GitHub
          </a>
          .
        </p>

        <div className="modal-actions">
          <button onClick={() => setStatus({ kind: "picker" })}>Change server</button>
          <button className="btn-primary" autoFocus onClick={retryLocal}>
            Try again
          </button>
        </div>
      </div>
    </ModalOverlay>
  );
}
