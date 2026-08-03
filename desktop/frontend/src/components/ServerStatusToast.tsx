import { useEffect, useState } from "react";
import { useServerReadiness } from "../config";

const READY_TOAST_MS = 4000;

// Local mode's server starts in the background (see ConnectGate.tsx) —
// this is the user-visible signal for when it actually becomes usable,
// or why it isn't. Renders nothing for "starting"/steady-state "ready":
// pages show their own "Starting local server…" placeholder for that.
export default function ServerStatusToast() {
  const readiness = useServerReadiness();
  const [showReadyToast, setShowReadyToast] = useState(false);
  const [dismissedError, setDismissedError] = useState(false);

  useEffect(() => {
    if (readiness.status !== "ready") return;
    setShowReadyToast(true);
    const timer = setTimeout(() => setShowReadyToast(false), READY_TOAST_MS);
    return () => clearTimeout(timer);
  }, [readiness.status]);

  useEffect(() => {
    if (readiness.status === "error") setDismissedError(false);
  }, [readiness.status]);

  if (readiness.status === "ready" && showReadyToast) {
    return (
      <div className="toast toast-ok">
        <span className="dot" />
        <span className="toast-body">Generation server is running.</span>
      </div>
    );
  }

  if (readiness.status === "error" && !dismissedError) {
    return (
      <div className="toast toast-err">
        <span className="dot" />
        <div className="toast-body">
          <strong>Couldn&apos;t start the local server.</strong>
          <div style={{ marginTop: 2 }}>
            Try restarting the app or check the logs. If it keeps happening, please{" "}
            <a
              href="https://github.com/dernPerkins/proxy-scaler/issues"
              target="_blank"
              rel="noreferrer"
            >
              report it on GitHub
            </a>
            .
          </div>
          <button className="btn-sm" style={{ marginTop: 8 }} onClick={() => setDismissedError(true)}>
            Dismiss
          </button>
        </div>
      </div>
    );
  }

  return null;
}
