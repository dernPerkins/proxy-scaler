import { useDownloadStatus } from "../download";

// Mounted once in App.tsx. Native save dialogs don't report byte-level
// progress back to the app, so this is a lock + status indicator, not a
// real progress bar — its job is preventing the exact bug
// main.rs::save_file's own comment describes: overlapping downloads
// exhausting Tauri's shared async worker pool, which read to a user as
// "the app crashed with no error." No onClick on the overlay —
// deliberately not dismissable, since there's nothing to cancel here
// (the OS dialog is already open and being awaited).
export default function DownloadProgressModal() {
  const status = useDownloadStatus();
  if (!status) return null;

  return (
    <div className="modal-overlay">
      <div className="modal modal-sm" onClick={(e) => e.stopPropagation()}>
        <div className="modal-head">
          <span className="modal-title">Saving…</span>
        </div>
        <p className="hint">
          Waiting for you to choose where to save <strong>{status.filename}</strong>.
        </p>
      </div>
    </div>
  );
}
