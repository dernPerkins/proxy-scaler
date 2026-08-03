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
      <div style={toastStyle}>
        Generation server is running.
      </div>
    );
  }

  if (readiness.status === "error" && !dismissedError) {
    return (
      <div style={{ ...toastStyle, background: "#4a2020", borderColor: "#a55" }}>
        Couldn't start the local server. Try restarting the app or check the
        logs. If it keeps happening, please{" "}
        <a
          href="https://github.com/dernPerkins/proxy-scaler/issues"
          target="_blank"
          rel="noreferrer"
          style={{ color: "#fff" }}
        >
          report it on GitHub
        </a>
        .
        <button
          onClick={() => setDismissedError(true)}
          style={{ marginLeft: 12 }}
        >
          Dismiss
        </button>
      </div>
    );
  }

  return null;
}

const toastStyle: React.CSSProperties = {
  position: "fixed",
  bottom: 16,
  right: 16,
  maxWidth: 360,
  padding: "10px 14px",
  borderRadius: 6,
  background: "#20302a",
  border: "1px solid #4a7a5a",
  color: "#e8e8ec",
  fontSize: 13,
  zIndex: 2000,
  boxShadow: "0 2px 8px rgba(0,0,0,0.4)",
};
