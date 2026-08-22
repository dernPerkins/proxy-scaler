import { useDownloadStatus, type DownloadPhase } from "../download";
import ModalOverlay from "./ModalOverlay";

function formatMb(bytes: number): string {
  return (bytes / 1048576).toFixed(1);
}

/** Label + 0..1 fraction (null = indeterminate) for the current phase. */
function describe(phase: DownloadPhase): { title: string; detail: string; fraction: number | null } {
  switch (phase.kind) {
    case "preparing":
      return { title: "Preparing…", detail: "Getting ready", fraction: null };
    case "rendering":
      return {
        title: "Rendering PDF…",
        detail: `Card image ${phase.completed} of ${phase.total}`,
        // Guard against a zero denominator rather than rendering NaN.
        fraction: phase.total > 0 ? phase.completed / phase.total : null,
      };
    case "transferring":
      return {
        title: "Downloading…",
        detail:
          phase.total != null
            ? `${formatMb(phase.downloaded)} of ${formatMb(phase.total)} MB`
            : `${formatMb(phase.downloaded)} MB`,
        // No content-length means no honest denominator — fall back to an
        // indeterminate bar instead of inventing one.
        fraction: phase.total != null && phase.total > 0 ? phase.downloaded / phase.total : null,
      };
  }
}

// Mounted once in App.tsx. Its job is preventing the exact bug
// main.rs::pick_save_path's own comment describes — overlapping downloads
// exhausting Tauri's shared async worker pool, which read to a user as
// "the app crashed with no error" — while giving the long waits something
// to show. A PDF export spends ~0.7s per unique card image server-side
// before any bytes exist, so without this the app looks hung for tens of
// seconds. No onClick on the overlay: dismissing it would unblock the UI
// while the work is still running, which is the thing being prevented.
// Cancel is the deliberate way out, and only appears when the current
// phase can actually be aborted.
export default function DownloadProgressModal() {
  const status = useDownloadStatus();
  if (!status) return null;

  const { title, detail, fraction } = describe(status.phase);

  return (
    <ModalOverlay>
      <div className="modal modal-sm" onClick={(e) => e.stopPropagation()}>
        <div className="modal-head">
          <span className="modal-title">{title}</span>
        </div>
        <p className="hint">
          <strong>{status.filename}</strong>
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
          {detail}
          {fraction != null && ` · ${Math.round(fraction * 100)}%`}
        </p>
        {status.cancel && (
          <div className="modal-actions">
            <button className="btn-sm" onClick={status.cancel}>
              Cancel
            </button>
          </div>
        )}
      </div>
    </ModalOverlay>
  );
}
