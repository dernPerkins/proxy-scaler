import { useSyncExternalStore } from "react";
import { waitForServerReady } from "./config";
import {
  invokeCancelDownload,
  invokeDownloadToPath,
  invokePickSavePath,
  isTauri,
  listenDownloadProgress,
} from "./tauri";

// Global single-flight lock across every download (PDF exports in
// PdfPage.tsx, per-image downloads in DecklistPage.tsx) — not just a UX
// nicety. Overlapping native-dialog invocations exhaust Tauri's shared
// async-runtime worker pool (see main.rs::pick_save_path's own comment
// for the full mechanism), after which the whole app appears to hang with
// no error — downloads "sitting in a weird loop" is exactly that
// pool-exhaustion symptom. DownloadProgressModal (mounted once in
// App.tsx) renders whenever this is non-null; its full-screen overlay
// physically blocks clicks from reaching another download button while
// shown, and runDownload itself refuses a second concurrent call as a
// defensive backstop in case something bypasses the modal.
//
// The lock is held for the whole operation, from the click onward. Two
// phases are worth distinguishing because they're slow for different
// reasons and measured by different machinery: a server-side PDF render
// costs ~0.7s per unique card image and is polled over HTTP, while the
// transfer is byte progress pushed from Rust. Both feed one bar.
export type DownloadPhase =
  | { kind: "preparing" }
  | { kind: "rendering"; completed: number; total: number }
  | { kind: "transferring"; downloaded: number; total: number | null };

export interface DownloadStatus {
  filename: string;
  phase: DownloadPhase;
  /** Present only while the operation is actually abortable. */
  cancel?: () => void;
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

/** Update the live download's phase. No-op once the download has ended,
 *  so a late poll/event can't resurrect a finished modal. */
export function setDownloadPhase(phase: DownloadPhase): void {
  if (!downloadStatus) return;
  downloadStatus = { ...downloadStatus, phase };
  notify();
}

export function setDownloadCancel(cancel: (() => void) | undefined): void {
  if (!downloadStatus) return;
  downloadStatus = { ...downloadStatus, cancel };
  notify();
}

export interface DownloadSource {
  /** Absolute URL on the generation server. */
  url: string;
  /** When present, sent as a JSON POST body instead of issuing a GET. */
  body?: unknown;
}

/** Thrown when the user cancels; callers treat it as a normal outcome
 *  rather than an error to display. */
export class DownloadCanceled extends Error {
  constructor() {
    super("Download canceled");
    this.name = "DownloadCanceled";
  }
}

// Plain-browser path (`npm run dev` without Tauri). Inside Tauri this is
// never used: the HTML `download` attribute isn't reliably honored by
// WKWebView (a static image link navigated to the image instead of
// saving it; a blob-URL PDF link did nothing at all), and more
// importantly the bytes should never enter the webview in the first
// place — see main.rs::download_to_path.
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

let nextDownloadId = 0;

/**
 * Download to a user-chosen location, holding the global single-flight
 * lock for the whole operation and driving the progress modal.
 *
 * `prepare` runs after the save location is chosen but before the
 * transfer — that's where a PDF export does its server-side render (and
 * reports "rendering" progress), returning the URL to actually fetch.
 * Asking for the location first means the user can walk away instead of
 * being interrupted by a dialog once the render finally finishes.
 *
 * Throws DownloadCanceled if the user aborts; resolves normally when they
 * dismiss the save dialog.
 */
export async function runDownload(
  filename: string,
  source: DownloadSource | (() => Promise<DownloadSource>),
): Promise<void> {
  if (downloadStatus) {
    throw new Error(
      `A download (${downloadStatus.filename}) is already in progress — wait for it to finish.`,
    );
  }
  downloadStatus = { filename, phase: { kind: "preparing" } };
  notify();

  const downloadId = `dl-${++nextDownloadId}`;
  let unlisten: (() => void) | undefined;
  try {
    await waitForServerReady();

    if (!isTauri()) {
      const resolved = typeof source === "function" ? await source() : source;
      await downloadViaBrowser(resolved, filename);
      return;
    }

    const path = await invokePickSavePath(filename);
    if (path == null) return; // dismissed the save dialog — not an error

    const resolved = typeof source === "function" ? await source() : source;

    setDownloadPhase({ kind: "transferring", downloaded: 0, total: null });
    setDownloadCancel(() => {
      void invokeCancelDownload(downloadId);
    });
    unlisten = await listenDownloadProgress((e) => {
      if (e.id !== downloadId) return;
      setDownloadPhase({
        kind: "transferring",
        downloaded: e.downloaded,
        total: e.total,
      });
    });

    const saved = await invokeDownloadToPath({
      url: resolved.url,
      path,
      downloadId,
      body: resolved.body,
    });
    if (!saved) throw new DownloadCanceled();
  } finally {
    unlisten?.();
    downloadStatus = null;
    notify();
  }
}
