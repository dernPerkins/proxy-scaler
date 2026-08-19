// Typed wrappers for the update-check commands (see
// desktop/src-tauri/src/update.rs, which owns the real logic: the
// manifest fetch happens in Rust so no CORS setup is ever needed on the
// website, and the integrity check happens in Rust because the bytes
// never enter the webview). UpdatePrompt.tsx is the only consumer.
//
// Field names are snake_case because they mirror the Rust structs
// serialized across the IPC boundary verbatim.

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
