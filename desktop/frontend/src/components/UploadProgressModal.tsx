import { useUploadStatus } from "../uploadProgress";
import ModalOverlay from "./ModalOverlay";

// Mounted once in App.tsx, beside DownloadProgressModal, and shaped like
// it: renders whenever the upload store is non-null, with no onClick on
// the overlay — dismissing it would unblock the UI while multi-MB uploads
// to a remote server are still running, which is the thing being
// prevented. Cancel is the deliberate way out, aborting between images
// (the in-flight one finishes; see uploadProgress.runCustomUploads).
export default function UploadProgressModal() {
  const status = useUploadStatus();
  if (!status) return null;

  const { done, total, label } = status.phase;
  const fraction = total > 0 ? done / total : null;

  return (
    <ModalOverlay>
      <div className="modal modal-sm" onClick={(e) => e.stopPropagation()}>
        <div className="modal-head">
          <span className="modal-title">Uploading custom images…</span>
        </div>
        <p className="hint">
          <strong>{label}</strong> — {Math.min(done + 1, total)} of {total}
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
