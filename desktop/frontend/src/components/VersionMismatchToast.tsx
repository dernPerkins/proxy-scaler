import { useEffect, useState } from "react";
import { useServerVersion } from "../config";
import { isTauri } from "../tauri";
import { getAppVersion } from "../update";

// The client/server drift warning. Remote mode makes drift possible — the
// app and the server it talks to are updated on separate machines — and
// without this nothing ever says so: requests mostly keep working across
// a version gap until the one endpoint that changed misbehaves
// confusingly. Non-blocking on purpose (a toast, not a modal): a mismatch
// is a caution, not an outage, and the pairing may still work fine.
//
// Renders nothing until BOTH versions are known. The server version stays
// null against servers that predate /api/version (the probe swallows
// their 404 — see connection.tsx::probeServerVersion), so old servers
// produce silence, not warning spam. Dismissal is per server version, not
// forever: switching to a different mismatched server re-raises it.
export default function VersionMismatchToast() {
  const serverVersion = useServerVersion();
  const [appVersion, setAppVersion] = useState<string | null>(null);
  const [dismissedFor, setDismissedFor] = useState<string | null>(null);

  useEffect(() => {
    if (!isTauri()) return;
    getAppVersion()
      .then(setAppVersion)
      .catch(() => {
        // No version, no comparison — the toast just never shows.
      });
  }, []);

  if (
    appVersion == null ||
    serverVersion == null ||
    serverVersion === appVersion ||
    dismissedFor === serverVersion
  ) {
    return null;
  }

  return (
    // toast-secondary: stacks above ServerStatusToast's slot, which can be
    // occupied at exactly the moment this first appears (both fire on
    // connect).
    <div className="toast toast-secondary">
      {/* --wait is the palette's caution color (see styles.css) — this is
          a heads-up, not an error. */}
      <span className="dot" style={{ background: "var(--wait)" }} />
      <div className="toast-body">
        <strong>Server version mismatch.</strong>
        <div style={{ marginTop: 2 }}>
          This app is v{appVersion}, but the connected server is v{serverVersion}. Update
          both to the same release for best results.
        </div>
        <button
          className="btn-sm"
          style={{ marginTop: 8 }}
          onClick={() => setDismissedFor(serverVersion)}
        >
          Dismiss
        </button>
      </div>
    </div>
  );
}
