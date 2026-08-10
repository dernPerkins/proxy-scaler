import { useSyncExternalStore } from "react";
import { waitForServerReady } from "./config";
import { invokeDownloadToFile, isTauri } from "./tauri";

// Global single-flight lock across every download (PDF downloads in
// PdfPage.tsx, per-image downloads in DecklistPage.tsx) — not just a UX
// nicety. Overlapping download_to_file invocations exhaust Tauri's shared
// async-runtime worker pool (see main.rs::download_to_file's own comment
// for the full mechanism), after which the whole app appears to hang with
// no error — downloads "sitting in a weird loop" is exactly that
// pool-exhaustion symptom. DownloadProgressModal (mounted once in
// App.tsx) renders whenever this is non-null; its full-screen overlay
// physically blocks clicks from reaching another download button while
// shown, and runDownload itself refuses a second concurrent call as a
// defensive backstop in case something bypasses the modal.
//
// The lock is held for the whole operation, from the click onward —
// locking only at the save step left the transfer itself (the slow part
// for a 15-25MB image) unprotected and invisible, so a second click
// during that window looked like nothing had happened.
interface DownloadStatus {
  filename: string;
}

let downloadStatus: DownloadStatus | null = null;
let listeners: Array<() => void> = [];

function notify(): void {
  for (const listener of listeners) listener();
}

export function getDownloadStatus(): DownloadStatus | null {
  return downloadStatus;
}

export function subscribeDownloadStatus(callback: () => void): () => void {
  listeners.push(callback);
  return () => {
    listeners = listeners.filter((l) => l !== callback);
  };
}

export function useDownloadStatus(): DownloadStatus | null {
  return useSyncExternalStore(subscribeDownloadStatus, getDownloadStatus);
}

export interface DownloadSource {
  /** Absolute URL on the generation server. */
  url: string;
  /** When present, sent as a JSON POST body instead of issuing a GET. */
  body?: unknown;
}

// Plain-browser path (`npm run dev` without Tauri). Inside Tauri this is
// never used: the HTML `download` attribute isn't reliably honored by
// WKWebView (a static image link navigated to the image instead of
// saving it; a blob-URL PDF link did nothing at all), and more
// importantly the bytes should never enter the webview in the first
// place — see main.rs::download_to_file.
async function downloadViaBrowser(source: DownloadSource, filename: string): Promise<void> {
  const resp = await fetch(source.url, {
    method: source.body === undefined ? "GET" : "POST",
    headers: source.body === undefined ? undefined : { "Content-Type": "application/json" },
    body: source.body === undefined ? undefined : JSON.stringify(source.body),
  });
  if (!resp.ok) {
    const detail = await resp.text();
    throw new Error(detail || `${resp.status} ${resp.statusText}`);
  }
  const blob = await resp.blob();

  const url = URL.createObjectURL(blob);
  const a = document.createElement("a");
  a.href = url;
  a.download = filename;
  document.body.appendChild(a);
  a.click();
  a.remove();
  URL.revokeObjectURL(url);
}

/** Download `source` to a user-chosen location, holding the global
 *  single-flight lock for the whole operation. Resolves normally when the
 *  user cancels the native save dialog. */
export async function runDownload(filename: string, source: DownloadSource): Promise<void> {
  if (downloadStatus) {
    throw new Error(
      `A download (${downloadStatus.filename}) is already in progress — wait for it to finish.`,
    );
  }
  downloadStatus = { filename };
  notify();
  try {
    await waitForServerReady();
    if (isTauri()) {
      // Only the URL crosses the IPC boundary; Rust does the HTTP and
      // writes the file itself.
      await invokeDownloadToFile(source.url, filename, source.body);
      return;
    }
    await downloadViaBrowser(source, filename);
  } finally {
    downloadStatus = null;
    notify();
  }
}
