// Blocking-progress state for Custom Image uploads to a REMOTE generation
// server — download.ts's pattern applied to the other direction. A
// module-level singleton store rendered by UploadProgressModal (mounted
// once in App.tsx): while non-null, a full-screen undismissable overlay
// holds all interaction, which is the point — a multi-MB upload to a
// remote host used to run with the app looking inert (PdfPage's
// pre-export sync had zero UI), or worse, interactable in ways that raced
// the upload.
//
// Local mode never populates this store (see syncCustoms.ts — the
// loopback "upload" is a near-instant file copy and showing a dialog for
// it would be noise). Population is decided by the caller, not the modal:
// during a Local→Remote switch the app is still in local mode while the
// uploads target the new server, so a modal-side mode check would hide
// the dialog exactly when it's most needed.
import { useSyncExternalStore } from "react";
import { projectApi } from "./api/project";

export interface UploadPhase {
  /** Items fully uploaded so far (the current one is done+1th). */
  done: number;
  total: number;
  /** Card name of the image currently uploading. */
  label: string;
}

export interface UploadStatus {
  phase: UploadPhase;
  /** Present only while the run is still abortable. */
  cancel?: () => void;
}

let uploadStatus: UploadStatus | null = null;
let listeners: Array<() => void> = [];

function notify(): void {
  for (const listener of listeners) listener();
}

export function getUploadStatus(): UploadStatus | null {
  return uploadStatus;
}

export function subscribeUploadStatus(callback: () => void): () => void {
  listeners.push(callback);
  return () => {
    listeners = listeners.filter((l) => l !== callback);
  };
}

export function useUploadStatus(): UploadStatus | null {
  return useSyncExternalStore(subscribeUploadStatus, getUploadStatus);
}

/** Thrown when the user cancels; callers treat it as a normal outcome
 *  rather than an error to display — mirrors DownloadCanceled. */
export class UploadCanceled extends Error {
  constructor() {
    super("Upload canceled");
    this.name = "UploadCanceled";
  }
}

export interface UploadItem {
  /** custom_images.id in the client library (what sync_custom_image takes). */
  id: number;
  /** Card name, shown in the progress dialog. */
  label: string;
}

/**
 * Upload each item's bytes to the server at `baseUrl`, sequentially,
 * driving the blocking modal. Sequential for the same reason
 * syncCustomImages always was: each miss is a multi-MB POST, and firing
 * them all at once at a server about to do GPU work is how a bulk action
 * becomes a timeout.
 *
 * Callers pass pre-probed misses (see syncCustoms.probeMissingCustoms) so
 * the total is honest; the Rust side re-probes each one anyway
 * (GET-then-POST), which makes a stale miss harmless.
 *
 * Cancel is between-images only — the Rust command has no abort, so the
 * in-flight image finishes and the loop stops before the next one.
 * Throws UploadCanceled then; other failures propagate with the store
 * already cleared (finally), so the modal can never stick.
 */
export async function runCustomUploads(items: UploadItem[], baseUrl: string): Promise<void> {
  if (items.length === 0) return;
  if (uploadStatus) {
    throw new Error("A custom-image upload is already in progress — wait for it to finish.");
  }
  let canceled = false;
  uploadStatus = {
    phase: { done: 0, total: items.length, label: items[0].label },
    cancel: () => {
      canceled = true;
    },
  };
  notify();
  try {
    let done = 0;
    for (const item of items) {
      if (canceled) throw new UploadCanceled();
      uploadStatus = { ...uploadStatus, phase: { done, total: items.length, label: item.label } };
      notify();
      await projectApi.syncCustomImage(item.id, baseUrl);
      done++;
    }
  } finally {
    uploadStatus = null;
    notify();
  }
}
