import { useEffect, useState } from "react";
import { useServerReadiness } from "../config";

const READY_TOAST_MS = 4000;

// The "you're good to go" confirmation, and nothing else: a brief toast as
// the local server finishes starting. The wait itself and its failure both
// belong to ServerBootModal now — a toast was the wrong shape for them,
// because it left the app looking usable while every request was still
// parked on the readiness gate, and it offered no way forward from a
// failed start (there wasn't one to offer; the retry didn't exist).
//
// Renders nothing for steady-state "ready", and nothing at all in Remote
// mode, which never passes through "starting".
export default function ServerStatusToast() {
  const readiness = useServerReadiness();
  const [showReadyToast, setShowReadyToast] = useState(false);
  // Ready is also the *initial* state (config.ts's default, and what Remote
  // sets directly), so an unconditional toast on "ready" would greet every
  // launch with a message about a server nobody watched start. Only a real
  // starting -> ready transition earns one.
  const [wasStarting, setWasStarting] = useState(false);

  useEffect(() => {
    if (readiness.status === "starting") {
      setWasStarting(true);
      return;
    }
    if (readiness.status !== "ready" || !wasStarting) return;
    setShowReadyToast(true);
    const timer = setTimeout(() => setShowReadyToast(false), READY_TOAST_MS);
    return () => clearTimeout(timer);
  }, [readiness.status, wasStarting]);

  if (readiness.status === "ready" && showReadyToast) {
    return (
      <div className="toast toast-ok">
        <span className="dot" />
        <span className="toast-body">Generation server is running.</span>
      </div>
    );
  }

  return null;
}
