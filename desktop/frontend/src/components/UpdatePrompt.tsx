import { useEffect, useState } from "react";
import { invokeCancelDownload, invokeDownloadToPath, isTauri, listenDownloadProgress } from "../tauri";
import {
  checkForUpdate,
  getUpdateSkippedVersion,
  launchInstaller,
  setUpdateSkippedVersion,
  type UpdateInfo,
} from "../update";

function formatGb(bytes: number): string {
  return `${(bytes / 1073741824).toFixed(1)} GB`;
}

/** Open a URL in the system browser. A synthesized anchor click rather
 *  than window.open(): plain target="_blank" anchors are the one
 *  external-link mechanism this app already relies on inside the webview
 *  (see ServerStatusToast's GitHub link), and this keeps button-shaped
 *  triggers on that same path. */
function openExternal(url: string): void {
  const a = document.createElement("a");
  a.href = url;
  a.target = "_blank";
  a.rel = "noreferrer";
  document.body.appendChild(a);
  a.click();
  a.remove();
}

type PromptState =
  | { kind: "hidden" }
  | { kind: "offer"; info: UpdateInfo }
  | { kind: "downloading"; info: UpdateInfo; downloaded: number; total: number | null }
  | { kind: "error"; info: UpdateInfo; message: string };

// The boot-time update offer. Mounted once in App.tsx; checks the release
// manifest exactly once per launch (Rust side — see update.rs) and shows
// nothing at all in the overwhelmingly common cases: up to date, offline,
// or the user already skipped this release. An in-app modal rather than a
// native dialog for the same reason as ConfirmDialog — native dialogs
// never appear inside Tauri's WKWebView on macOS.
//
// "Update now" is a real commitment (the installers are multi-GB, which
// is why the size is shown on the button's own line before anything
// starts), so the download runs inside this same modal with the shared
// download-progress machinery (main.rs::download_to_path), and the
// overlay stays up for the duration — blocking other download buttons the
// same way DownloadProgressModal's overlay does, since Tauri's async pool
// can't take overlapping native transfers. On success the app verifies
// and hands off to the installer Rust-side and exits; this component's
// job is over the moment launchInstaller resolves-by-not-returning.
export default function UpdatePrompt() {
  const [state, setState] = useState<PromptState>({ kind: "hidden" });

  // Esc while the offer is up means "Later" — the answer that changes
  // nothing (matching ConfirmDialog's reasoning). Never wired during the
  // download: dismissing that needs the deliberate Cancel button.
  useEffect(() => {
    if (state.kind !== "offer") return;
    function onKeyDown(e: KeyboardEvent) {
      if (e.key === "Escape") setState({ kind: "hidden" });
    }
    document.addEventListener("keydown", onKeyDown);
    return () => document.removeEventListener("keydown", onKeyDown);
  }, [state.kind]);

  useEffect(() => {
    if (!isTauri()) return;
    let cancelled = false;
    (async () => {
      try {
        const info = await checkForUpdate();
        if (!info || cancelled) return;
        // "Skip this version" is exactly one release wide: a newer
        // release than the skipped one comes through unhindered.
        const skipped = await getUpdateSkippedVersion().catch(() => null);
        if (cancelled || info.latest === skipped) return;
        setState({ kind: "offer", info });
      } catch {
        // A failed check is indistinguishable from "no update" by design.
      }
    })();
    return () => {
      cancelled = true;
    };
  }, []);

  if (state.kind === "hidden") return null;
  const { info } = state;

  async function startDownload() {
    const artifact = info.artifact;
    if (!artifact) return;
    const downloadId = `update-${info.latest}`;
    setState({ kind: "downloading", info, downloaded: 0, total: artifact.size });
    let unlisten: (() => void) | undefined;
    try {
      unlisten = await listenDownloadProgress((e) => {
        if (e.id !== downloadId) return;
        setState({
          kind: "downloading",
          info,
          downloaded: e.downloaded,
          total: e.total ?? artifact.size,
        });
      });
      const saved = await invokeDownloadToPath({
        url: artifact.url,
        path: artifact.save_path,
        downloadId,
      });
      if (!saved) {
        // Canceled — back to the offer, not an error.
        setState({ kind: "offer", info });
        return;
      }
      // Verifies size+sha256 and exits the app on success — an error here
      // is the only way back.
      await launchInstaller(artifact);
    } catch (err) {
      setState({
        kind: "error",
        info,
        message: err instanceof Error ? err.message : String(err),
      });
    } finally {
      unlisten?.();
    }
  }

  async function skipThisVersion() {
    setState({ kind: "hidden" });
    // Best-effort: a failed write just means the offer returns next
    // launch, which is the harmless direction to fail in.
    await setUpdateSkippedVersion(info.latest).catch(() => {});
  }

  if (state.kind === "downloading") {
    const { downloaded, total } = state;
    const fraction = total != null && total > 0 ? downloaded / total : null;
    return (
      <div className="modal-overlay">
        <div className="modal modal-sm" onClick={(e) => e.stopPropagation()}>
          <div className="modal-head">
            <span className="modal-title">Downloading update…</span>
          </div>
          <p className="hint">
            <strong>{info.artifact?.filename}</strong>
          </p>
          <div
            className={`progress${fraction == null ? " progress-indeterminate" : ""}`}
            role="progressbar"
            aria-valuemin={0}
            aria-valuemax={100}
            aria-valuenow={fraction == null ? undefined : Math.round(fraction * 100)}
          >
            <div
              className="progress-fill"
              style={fraction == null ? undefined : { width: `${Math.round(fraction * 100)}%` }}
            />
          </div>
          <p className="hint">
            {formatGb(downloaded)}
            {total != null && ` of ${formatGb(total)}`}
            {fraction != null && ` · ${Math.round(fraction * 100)}%`}
          </p>
          <div className="modal-actions">
            <button
              className="btn-sm"
              onClick={() => void invokeCancelDownload(`update-${info.latest}`)}
            >
              Cancel
            </button>
          </div>
        </div>
      </div>
    );
  }

  if (state.kind === "error") {
    return (
      <div className="modal-overlay">
        <div className="modal modal-sm" onClick={(e) => e.stopPropagation()}>
          <div className="modal-head">
            <span className="modal-title">Update failed</span>
          </div>
          <p style={{ marginBottom: 8 }}>{state.message}</p>
          <p className="hint" style={{ marginBottom: 16 }}>
            You can try again, or get the installer from{" "}
            <a href={info.notes_url} target="_blank" rel="noreferrer">
              the download page
            </a>{" "}
            instead.
          </p>
          <div className="modal-actions">
            <button onClick={() => setState({ kind: "hidden" })}>Close</button>
            <button onClick={() => void startDownload()}>Try again</button>
          </div>
        </div>
      </div>
    );
  }

  // The offer. Esc means "Later" — the answer that changes nothing.
  return (
    <div className="modal-overlay" onClick={() => setState({ kind: "hidden" })}>
      <div className="modal modal-sm" onClick={(e) => e.stopPropagation()}>
        <div className="modal-head">
          <span className="modal-title">Update available</span>
        </div>
        <p style={{ marginBottom: 8 }}>
          Proxy Scaler {info.latest} is available — you have {info.current}.
        </p>
        {info.notes && (
          <p className="hint" style={{ marginBottom: 8 }}>
            {info.notes}
          </p>
        )}
        <p className="hint" style={{ marginBottom: 16 }}>
          <a href={info.notes_url} target="_blank" rel="noreferrer">
            Release notes
          </a>
        </p>
        <div className="modal-actions">
          <button className="btn-sm" onClick={() => void skipThisVersion()}>
            Skip this version
          </button>
          <button autoFocus onClick={() => setState({ kind: "hidden" })}>
            Later
          </button>
          {info.artifact ? (
            <button className="btn-primary" onClick={() => void startDownload()}>
              Update now ({formatGb(info.artifact.size)})
            </button>
          ) : (
            // No installer for this build (Linux tarball, dev build) —
            // the download page is the update path.
            <button className="btn-primary" onClick={() => openExternal(info.notes_url)}>
              Open download page
            </button>
          )}
        </div>
      </div>
    </div>
  );
}
