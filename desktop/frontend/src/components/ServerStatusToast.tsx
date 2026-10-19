import { useEffect, useState } from "react";
import { useServerReadiness } from "../config";
import { toast } from "../toasts";

// The "you're good to go" confirmation, and nothing else: a brief toast as
// the local server finishes starting. The wait itself and its failure both
// belong to ServerBootModal now — a toast was the wrong shape for them,
// because it left the app looking usable while every request was still
// parked on the readiness gate, and it offered no way forward from a
// failed start (there wasn't one to offer; the retry didn't exist).
//
// Raised on the toast stack (toasts.ts); renders nothing itself. Nothing
// for steady-state "ready", and nothing at all in Remote mode, which never
// passes through "starting".
export default function ServerStatusToast() {
  const readiness = useServerReadiness();
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
    setWasStarting(false);
    toast("Generation server is running.", "ok", { key: "server" });
  }, [readiness.status, wasStarting]);

  return null;
}
