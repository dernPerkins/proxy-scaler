import { useEffect, useState } from "react";
import { useServerVersion } from "../config";
import { isTauri } from "../tauri";
import { getAppVersion } from "../update";
import { clearToast, toast } from "../toasts";

// The client/server drift warning. Remote mode makes drift possible — the
// app and the server it talks to are updated on separate machines — and
// without this nothing ever says so: requests mostly keep working across
// a version gap until the one endpoint that changed misbehaves
// confusingly. Non-blocking on purpose (a toast on the stack, not a
// modal): a mismatch is a caution, not an outage, and the pairing may
// still work fine. Renders nothing itself.
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

  const mismatched =
    appVersion != null && serverVersion != null && serverVersion !== appVersion;
  useEffect(() => {
    if (!mismatched || dismissedFor === serverVersion) {
      clearToast("version");
      return;
    }
    // --wait (the warn kind) is the palette's caution colour: a heads-up,
    // not an error, so it stays until dismissed rather than expiring.
    toast(
      `This app is v${appVersion}, but the connected server is v${serverVersion}. Update both to the same release for best results.`,
      "warn",
      {
        key: "version",
        title: "Server version mismatch.",
        sticky: true,
        onDismiss: () => setDismissedFor(serverVersion),
      },
    );
  }, [mismatched, appVersion, serverVersion, dismissedFor]);

  return null;
}
