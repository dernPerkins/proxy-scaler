import { useSyncExternalStore } from "react";
import { invokeSaveFile, isTauri } from "./tauri";

// Global single-flight lock across every download (PDF downloads in
// PdfPage.tsx, per-image downloads in DecklistPage.tsx) — not just a UX
// nicety. Overlapping save_file invocations exhaust Tauri's shared
// async-runtime worker pool (see main.rs::save_file's own comment for the
// full mechanism), after which the whole app appears to hang with no
// error — downloads "sitting in a weird loop" is exactly that
// pool-exhaustion symptom. DownloadProgressModal (mounted once in
// App.tsx) renders whenever this is non-null; its full-screen overlay
// physically blocks clicks from reaching another download button while
// shown, and runDownload itself refuses a second concurrent call as a
// defensive backstop in case something bypasses the modal.
//
// "fetching" vs "saving": the lock must engage the instant a download
// starts, not just once a Blob is already in hand. Locking only at the
// save step left the actual network fetch (the slow, unpredictable part
// for a large high-DPI image or a PDF still being generated server-side)
// completely unprotected — no lock, no visible state — so a second click
// during that window looked like nothing had happened, and could kick off
// a genuinely overlapping, unprotected download.
export type DownloadPhase = "fetching" | "saving";

interface DownloadStatus {
  filename: string;
  phase: DownloadPhase;
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

// Confirmed by real testing (not a theoretical webview quirk): the HTML
// `download` attribute is not reliably honored by Tauri's webview on
// macOS (WKWebView) — a static <a download> link to an image just
// navigated to a larger view of the image instead of saving it, and a
// blob-URL <a download> link (the usual JS-driven download trick) for a
// generated PDF did nothing at all. Inside Tauri this goes through a
// native "Save As" dialog + Rust-side std::fs::write instead (see
// tauri.ts::invokeSaveFile / main.rs::save_file). In a plain browser tab
// (e.g. `npm run dev` without Tauri) the standard anchor+download trick
// works fine, so that path is kept as the fallback.
async function saveBlob(blob: Blob, filename: string): Promise<void> {
  if (isTauri()) {
    const bytes = new Uint8Array(await blob.arrayBuffer());
    await invokeSaveFile(filename, bytes);
    return;
  }

  const url = URL.createObjectURL(blob);
  const a = document.createElement("a");
  a.href = url;
  a.download = filename;
  document.body.appendChild(a);
  a.click();
  a.remove();
  URL.revokeObjectURL(url);
}

// Acquires the lock for the *whole* operation, starting from before
// fetchBlob() runs (see the "fetching" vs "saving" comment above) — this
// is the entry point callers that still need to fetch bytes should use
// (per-image downloads in DecklistPage.tsx, PDF generation in
// PdfPage.tsx), rather than fetching first and locking only for the save.
export async function runDownload(
  filename: string,
  fetchBlob: () => Promise<Blob>,
): Promise<void> {
  if (downloadStatus) {
    throw new Error(
      `A download (${downloadStatus.filename}) is already in progress — wait for it to finish.`,
    );
  }
  downloadStatus = { filename, phase: "fetching" };
  notify();
  try {
    const blob = await fetchBlob();
    downloadStatus = { filename, phase: "saving" };
    notify();
    await saveBlob(blob, filename);
  } finally {
    downloadStatus = null;
    notify();
  }
}
