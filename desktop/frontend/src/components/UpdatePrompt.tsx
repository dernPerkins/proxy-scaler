import { useEffect, useState, useSyncExternalStore } from "react";
import ModalOverlay from "./ModalOverlay";
import { invokeCancelDownload, isTauri, listenDownloadProgress } from "../tauri";
import {
  checkForUpdate,
  downloadUpdate,
  getAvailableUpdate,
  getPromptRequestSeq,
  getUpdateCheckEnabled,
  getUpdateSkippedVersion,
  launchInstaller,
  setAvailableUpdate,
  setBootUpdateCheckSettled,
  setUpdatePromptOpen,
  setUpdateSkippedVersion,
  subscribeUpdateStore,
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

// The boot-time update offer. Mounted once in main.tsx, ABOVE
// ConnectGate — the check fires on launch, not on picking Local/Remote,
// so the offer can be on screen while the picker still is. It checks the
// release manifest exactly once per launch (Rust side — see update.rs)
// and shows nothing at all in the overwhelmingly common cases: up to
// date, offline, or the user already skipped this release. An in-app modal rather than a
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

  // ResumeTasksPrompt sequences itself behind this modal (update first,
  // tasks second — see update.ts's boot-dialog sequencing section), so
  // every state change publishes to the store: open/closed continuously,
  // and "the boot check is settled" on every exit path of the check.
  useEffect(() => {
    setUpdatePromptOpen(state.kind !== "hidden");
  }, [state.kind]);

  useEffect(() => {
    if (!isTauri()) {
      setBootUpdateCheckSettled();
      return;
    }
    let cancelled = false;
    (async () => {
      try {
        // The settings sidebar's "Check for updates at launch" switch.
        // Failing open (an unreadable setting still checks) keeps the
        // default behavior; the check itself stays fail-silent below.
        const enabled = await getUpdateCheckEnabled().catch(() => true);
        if (!enabled || cancelled) return;
        const info = await checkForUpdate();
        if (!info || cancelled) return;
        // Published unconditionally — even for a skipped release: the
        // tab bar's "Update to vX.Y.Z" button (App.tsx) is the persistent
        // way back to a dismissed or skipped update, so it must know
        // regardless of what the modal does.
        setAvailableUpdate(info);
        // "Skip this version" is exactly one release wide, and only
        // suppresses this automatic boot-time MODAL — never the button.
        const skipped = await getUpdateSkippedVersion().catch(() => null);
        if (cancelled || info.latest === skipped) return;
        setState({ kind: "offer", info });
      } catch {
        // A failed check is indistinguishable from "no update" by design.
      } finally {
        // Settled regardless of outcome — offered, none, skipped, or
        // failed. If the offer opened, updatePromptOpen (above) is what
        // keeps ResumeTasksPrompt waiting from here on.
        setBootUpdateCheckSettled();
      }
    })();
    return () => {
      cancelled = true;
    };
  }, []);

  // The tab-bar button's re-open signal (update.ts::requestUpdatePrompt).
  // Functional setState so a click can never clobber an in-flight
  // download or an error the user is reading — it only opens from hidden.
  const promptRequestSeq = useSyncExternalStore(subscribeUpdateStore, getPromptRequestSeq);
  useEffect(() => {
    if (promptRequestSeq === 0) return;
    const info = getAvailableUpdate();
    if (!info) return;
    setState((prev) => (prev.kind === "hidden" ? { kind: "offer", info } : prev));
  }, [promptRequestSeq]);

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
      // No URL or path here on purpose: Rust already decided both when
      // check_for_update stored the pending artifact (see
      // update.rs::PendingUpdate) — this side only pulls the trigger.
      const saved = await downloadUpdate(downloadId);
      if (!saved) {
        // Canceled — back to the offer, not an error.
        setState({ kind: "offer", info });
        return;
      }
      // Verifies size+sha256 against the signed manifest and exits the
      // app on success — an error here is the only way back.
      await launchInstaller();
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
      <ModalOverlay>
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
      </ModalOverlay>
    );
  }

  if (state.kind === "error") {
    return (
      <ModalOverlay>
        <div className="modal modal-sm" onClick={(e) => e.stopPropagation()}>
          <div className="modal-head">
            <span className="modal-title">Update failed</span>
          </div>
          <p style={{ marginBottom: 8 }}>{state.message}</p>
          {/* notes_url is https-or-blank (Rust filters it — see
              check_for_update); blank just drops the link. */}
          {info.notes_url ? (
            <p className="hint" style={{ marginBottom: 16 }}>
              You can try again, or get the installer from{" "}
              <a href={info.notes_url} target="_blank" rel="noreferrer">
                the download page
              </a>{" "}
              instead.
            </p>
          ) : (
            <p className="hint" style={{ marginBottom: 16 }}>
              You can try again.
            </p>
          )}
          <div className="modal-actions">
            <button onClick={() => setState({ kind: "hidden" })}>Close</button>
            <button onClick={() => void startDownload()}>Try again</button>
          </div>
        </div>
      </ModalOverlay>
    );
  }

  // The offer. Esc means "Later" — the answer that changes nothing.
  return (
    <ModalOverlay onClick={() => setState({ kind: "hidden" })}>
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
        {info.notes_url && (
          <p className="hint" style={{ marginBottom: 16 }}>
            <a href={info.notes_url} target="_blank" rel="noreferrer">
              Release notes
            </a>
          </p>
        )}
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
          ) : info.notes_url ? (
            // No installer for this build (Linux tarball, dev build) —
            // the download page is the update path.
            <button className="btn-primary" onClick={() => openExternal(info.notes_url)}>
              Open download page
            </button>
          ) : null}
        </div>
      </div>
    </ModalOverlay>
  );
}
