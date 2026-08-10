import { useDownloadStatus } from "../download";

// Mounted once in App.tsx. Native save dialogs don't report byte-level
// progress back to the app, so this is a lock + status indicator, not a
// real progress bar — its job is preventing the exact bug
// main.rs::download_to_file's own comment describes: overlapping downloads
// exhausting Tauri's shared async worker pool, which read to a user as
// "the app crashed with no error." No onClick on the overlay —
// deliberately not dismissable, since there's nothing to cancel here (the
// fetch/native dialog is already in flight and being awaited).
//
// Covers the whole operation, not just the write — the lock is held from
// the click through the transfer (see download.ts::runDownload), so this
// is the user's only feedback that a large image/PDF is actually moving
// rather than having silently done nothing.
export default function DownloadProgressModal() {
  const status = useDownloadStatus();
  if (!status) return null;

  return (
    <div className="modal-overlay">
      <div className="modal modal-sm" onClick={(e) => e.stopPropagation()}>
        <div className="modal-head">
          <span className="modal-title">Downloading…</span>
        </div>
        <p className="hint">
          Saving <strong>{status.filename}</strong>. Choose a location if prompted.
        </p>
      </div>
    </div>
  );
}
