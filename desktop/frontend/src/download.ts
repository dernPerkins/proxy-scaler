import { useSyncExternalStore } from "react";
import { invokeSaveFile, isTauri } from "./tauri";

// Global single-flight lock across every downloadBlob() call (PDF
// downloads in PdfPage.tsx, per-image downloads in DecklistPage.tsx) —
// not just a UX nicety. Overlapping save_file invocations exhaust
// Tauri's shared async-runtime worker pool (see main.rs::save_file's own
// comment for the full mechanism), after which the whole app appears to
// hang with no error — downloads "sitting in a weird loop" is exactly
// that pool-exhaustion symptom. DownloadProgressModal (mounted once in
// App.tsx) renders whenever this is non-null; its full-screen overlay
// physically blocks clicks from reaching another download button while
// shown, and downloadBlob itself refuses a second concurrent call as a
// defensive backstop in case something bypasses the modal.
let downloadStatus: { filename: string } | null = null;
let listeners: Array<() => void> = [];

function notify(): void {
  for (const listener of listeners) listener();
}

export function getDownloadStatus(): { filename: string } | null {
  return downloadStatus;
}

export function subscribeDownloadStatus(callback: () => void): () => void {
  listeners.push(callback);
  return () => {
    listeners = listeners.filter((l) => l !== callback);
  };
}

export function useDownloadStatus(): { filename: string } | null {
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
export async function downloadBlob(blob: Blob, filename: string): Promise<void> {
  if (downloadStatus) {
    throw new Error(
      `A download (${downloadStatus.filename}) is already in progress — wait for it to finish.`,
    );
  }
  downloadStatus = { filename };
  notify();
  try {
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
  } finally {
    downloadStatus = null;
    notify();
  }
}
