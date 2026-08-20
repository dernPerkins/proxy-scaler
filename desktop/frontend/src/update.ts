// Typed wrappers for the update-check commands (see
// desktop/src-tauri/src/update.rs, which owns the real logic: the
// manifest fetch happens in Rust so no CORS setup is ever needed on the
// website, and the integrity check happens in Rust because the bytes
// never enter the webview). UpdatePrompt.tsx is the only consumer.
//
// Field names are snake_case because they mirror the Rust structs
// serialized across the IPC boundary verbatim.

import { useSyncExternalStore } from "react";
import { invokeCommand, isTauri } from "./tauri";

export interface UpdateArtifact {
  url: string;
  /** Where Rust decided the installer should land (the user's Downloads
   *  directory) — passed straight back into invokeDownloadToPath. */
  save_path: string;
  filename: string;
  size: number;
  sha256: string;
}

export interface UpdateInfo {
  current: string;
  latest: string;
  notes: string;
  notes_url: string;
  /** null when there's nothing this app can install itself (Linux
   *  tarball, dev build, missing manifest entry) — offer the download
   *  page instead. */
  artifact: UpdateArtifact | null;
}

/** null = up to date, or the check couldn't run (offline, bad manifest —
 *  Rust logs the reason and reports "nothing to offer"; a boot-time check
 *  must never surface an error for having no internet). */
export async function checkForUpdate(): Promise<UpdateInfo | null> {
  if (!isTauri()) return null;
  return invokeCommand<UpdateInfo | null>("check_for_update");
}

/** Verifies the downloaded file against the manifest's size/sha256, hands
 *  it to the OS installer, and exits the app (sidecar stopped first).
 *  Only ever resolves with an error — success means the process is going
 *  away. */
export async function launchInstaller(artifact: UpdateArtifact): Promise<void> {
  await invokeCommand<void>("launch_installer", {
    path: artifact.save_path,
    expectedSize: artifact.size,
    expectedSha256: artifact.sha256,
  });
}

/** The one release the user said "Skip this version" to — the boot check
 *  stops offering exactly that release; the next one supersedes the skip
 *  by not matching it. */
export async function getUpdateSkippedVersion(): Promise<string | null> {
  return invokeCommand<string | null>("get_update_skipped_version");
}

export async function setUpdateSkippedVersion(version: string): Promise<void> {
  await invokeCommand<void>("set_update_skipped_version", { version });
}

/** This build's own version (compile-time constant from Cargo.toml). */
export async function getAppVersion(): Promise<string> {
  return invokeCommand<string>("get_app_version");
}

// --- Shared "an update exists" store ----------------------------------------
//
// The boot check runs once, in UpdatePrompt, but its answer matters in two
// places: the prompt itself and App.tsx's "Update to vX.Y.Z" button in the
// tab bar. The button is the persistent affordance — "Skip this version" /
// "Later" only suppress the boot MODAL, and without the button a skipped
// update would be unreachable except by going to the website. Same
// module-level-store shape as config.ts's serverVersion.

let availableUpdate: UpdateInfo | null = null;
// Bumped by requestUpdatePrompt(); UpdatePrompt watches it and re-opens
// the offer. A counter rather than a boolean so every click is a fresh
// signal — a boolean already `true` from a dismissed request would make
// the next click a no-op.
let promptRequestSeq = 0;
let updateListeners: Array<() => void> = [];

function notifyUpdateStore(): void {
  for (const listener of updateListeners) listener();
}

export function subscribeUpdateStore(callback: () => void): () => void {
  updateListeners.push(callback);
  return () => {
    updateListeners = updateListeners.filter((l) => l !== callback);
  };
}

export function setAvailableUpdate(info: UpdateInfo): void {
  availableUpdate = info;
  notifyUpdateStore();
}

export function getAvailableUpdate(): UpdateInfo | null {
  return availableUpdate;
}

export function useAvailableUpdate(): UpdateInfo | null {
  return useSyncExternalStore(subscribeUpdateStore, getAvailableUpdate);
}

/** Ask UpdatePrompt to show the offer again (the tab-bar button's click).
 *  A no-op when no update is known — the button only renders when one is. */
export function requestUpdatePrompt(): void {
  if (!availableUpdate) return;
  promptRequestSeq += 1;
  notifyUpdateStore();
}

export function getPromptRequestSeq(): number {
  return promptRequestSeq;
}

// --- Boot-dialog sequencing --------------------------------------------------
//
// Two dialogs can want the screen at launch: UpdatePrompt (boot update
// check, mounted above ConnectGate) and ResumeTasksPrompt (leftover tasks
// from the last session, mounted in App). They must never stack; the
// update comes first. These two flags are what ResumeTasksPrompt waits
// on — deferring it is free, because the worker stays held (nothing
// processes) until it acts. UpdatePrompt publishes both.

// True once the boot check has finished — an update was offered, there
// was none, the check failed (offline), or the version was skipped. In a
// plain browser tab (no Tauri) UpdatePrompt's boot effect bails without
// checking, so it settles the flag immediately there too.
let bootUpdateCheckSettled = false;
// True while any UpdatePrompt modal state (offer/downloading/error) is on
// screen — boot-triggered or re-opened from the tab-bar button.
let updatePromptOpen = false;

export function setBootUpdateCheckSettled(): void {
  if (bootUpdateCheckSettled) return;
  bootUpdateCheckSettled = true;
  notifyUpdateStore();
}

export function getBootUpdateCheckSettled(): boolean {
  return bootUpdateCheckSettled;
}

export function setUpdatePromptOpen(open: boolean): void {
  if (updatePromptOpen === open) return;
  updatePromptOpen = open;
  notifyUpdateStore();
}

export function getUpdatePromptOpen(): boolean {
  return updatePromptOpen;
}

// Third link in the boot-dialog chain (update → resume-tasks → card-db
// offer): ResumeTasksPrompt publishes these the same way UpdatePrompt
// publishes its pair above, and CardDbPrompt waits on all four so launch
// dialogs never stack. "Settled" means ResumeTasksPrompt has decided —
// either it had nothing to show, or its prompt was answered.
let resumeTasksSettled = false;
let resumeTasksPromptOpen = false;

export function setResumeTasksSettled(): void {
  if (resumeTasksSettled) return;
  resumeTasksSettled = true;
  notifyUpdateStore();
}

export function getResumeTasksSettled(): boolean {
  return resumeTasksSettled;
}

export function setResumeTasksPromptOpen(open: boolean): void {
  if (resumeTasksPromptOpen === open) return;
  resumeTasksPromptOpen = open;
  notifyUpdateStore();
}

export function getResumeTasksPromptOpen(): boolean {
  return resumeTasksPromptOpen;
}
