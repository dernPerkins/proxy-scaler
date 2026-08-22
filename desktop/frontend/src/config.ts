import { useSyncExternalStore } from "react";
import type { Device } from "./api/types";

// Mutable: in a plain browser tab (no Tauri) this stays the dev default
// below. Inside Tauri, ConnectGate overwrites it once via setApiBaseUrl
// after resolving Local (the sidecar's own reported URL) or Remote (the
// user-entered host) — every api/generation.ts request reads the current
// value via getApiBaseUrl(), not a frozen import-time constant.
let apiBaseUrl: string =
  (import.meta.env.VITE_API_BASE_URL as string | undefined) ?? "http://127.0.0.1:13207";

export function getApiBaseUrl(): string {
  return apiBaseUrl;
}

export function setApiBaseUrl(url: string): void {
  apiBaseUrl = url;
}

// Set once by ConnectGate alongside setApiBaseUrl, right after Local/Remote
// is resolved. Used as a fallback signal for the default upscale model
// (see ProjectContext.tsx::recommendedDefaultModel()) when the actual
// device probe below hasn't answered yet — local runs on-device, so
// a fast/light model is assumed the safer default; remote is assumed to
// have real GPU headroom. Both are just guesses; the probed device is the real
// answer once it arrives.
let connectionMode: "local" | "remote" | null = null;

export function getConnectionMode(): "local" | "remote" | null {
  return connectionMode;
}

export function setConnectionMode(mode: "local" | "remote"): void {
  connectionMode = mode;
}

// Set by connection.tsx's fire-and-forget /api/device probe, fired once
// per successful connect()/switchTo() — so this always reflects whichever
// server is currently active, re-probed fresh on every Local<->Remote
// switch. null means "not known yet" (probe still in flight, or this
// connection never triggered one, e.g. before the first connect
// completes) — callers should treat null as "fall back to the
// connectionMode-based guess above," never as "assume CPU."
//
// Holds the whole Device response, not just a gpu-yes/no boolean, because
// `kind` alone can't tell Apple's MPS apart from CUDA — both report "gpu"
// — and they want different default models. See
// ProjectContext.tsx::recommendedDefaultModel().
let probedDevice: Device | null = null;

export function getProbedDevice(): Device | null {
  return probedDevice;
}

// Subscribers exist because this answer arrives *late* and something has
// to react when it does. /api/device pays torch's cold-import cost on its
// first call — measured at tens of seconds on a CUDA box, long after the
// server reports healthy (~1s) and the UI is already interactive. Anything
// that read this once, at mount, therefore only ever saw the null
// fallback. See ProjectContext.tsx::recommendedDefaultModel.
type DeviceListener = (device: Device | null) => void;
const deviceListeners = new Set<DeviceListener>();

export function subscribeProbedDevice(listener: DeviceListener): () => void {
  deviceListeners.add(listener);
  return () => {
    deviceListeners.delete(listener);
  };
}

export function setProbedDevice(device: Device): void {
  probedDevice = device;
  for (const listener of deviceListeners) listener(probedDevice);
}

// Called at the start of every applyTarget() — connection.tsx's probe is
// fire-and-forget, so without this a *new* connection's "not known yet"
// window would silently show the *previous* server's answer instead
// (e.g. Local's "no GPU" bleeding into the first moment of a freshly
// connected Remote box, until its own probe — which pays torch's full
// cold-import cost on its first call, a real multi-second wait — gets
// around to overwriting it). Resetting to null here means that gap
// always falls back to the honest mode-based guess instead of a stale,
// wrong-server answer.
export function clearProbedDevice(): void {
  probedDevice = null;
  for (const listener of deviceListeners) listener(probedDevice);
}

// Set by connection.tsx's fire-and-forget /api/version probe, cleared at
// the start of every applyTarget() for the same stale-answer reason as
// clearProbedDevice above. null means "not known" — the probe hasn't
// answered yet, or the server predates the /api/version endpoint (its
// 404 is swallowed) — and null must never trigger the drift warning:
// "unknown" is not "mismatched". VersionMismatchToast is the consumer.
let serverVersion: string | null = null;
let versionListeners: Array<() => void> = [];

function notifyServerVersion(): void {
  for (const listener of versionListeners) listener();
}

export function getServerVersion(): string | null {
  return serverVersion;
}

export function setServerVersion(version: string): void {
  serverVersion = version;
  notifyServerVersion();
}

export function clearServerVersion(): void {
  serverVersion = null;
  notifyServerVersion();
}

export function subscribeServerVersion(callback: () => void): () => void {
  versionListeners.push(callback);
  return () => {
    versionListeners = versionListeners.filter((l) => l !== callback);
  };
}

export function useServerVersion(): string | null {
  return useSyncExternalStore(subscribeServerVersion, getServerVersion);
}

// --- The back-printing version floor ---------------------------------------
//
// Back printing replaced PdfLayoutIn's `show_cut_lines` with four required
// hide flags. An OLD client talking to a NEW server fails loudly on its
// own: the flags are required, so the request 422s.
//
// This direction cannot fail loudly by itself. Pydantic ignores unknown
// fields, so a NEW client's flags sent to an OLD server are dropped
// without a word and that server renders with its own `show_cut_lines`
// default — guides on every page, no error, no clue. The only place the
// break is detectable is here, before the request goes out.
//
// Unlike VersionMismatchToast (a dismissible caution about drift in
// general), this is a hard block on one operation, because its failure
// mode is a wrong PDF rather than a confusing one.
//
// 0.2.0 is the release that shipped back printing. packaging/set-version.py
// deliberately does NOT rewrite this constant, and it must not start
// tracking the current version: it names one specific historical release
// forever, and bumping it in lockstep would block every server that is
// merely a release behind rather than actually too old.
export const BACK_PRINTING_MIN_SERVER_VERSION = "0.2.0";

function parseVersion(version: string): number[] | null {
  const parts = version.trim().split(".");
  if (parts.length === 0 || parts.length > 4) return null;
  const numbers = parts.map((p) => Number.parseInt(p, 10));
  return numbers.some((n) => Number.isNaN(n)) ? null : numbers;
}

/** Negative if a < b, 0 if equal, positive if a > b. */
export function compareVersions(a: string, b: string): number | null {
  const left = parseVersion(a);
  const right = parseVersion(b);
  if (left == null || right == null) return null;
  for (let i = 0; i < Math.max(left.length, right.length); i++) {
    const diff = (left[i] ?? 0) - (right[i] ?? 0);
    if (diff !== 0) return diff;
  }
  return 0;
}

/**
 * Is the connected server too old to be sent a back-printing render?
 *
 * A null server version means the server predates `/api/version` entirely
 * (connection.tsx swallows that 404) — which puts it far below this floor,
 * so it counts as too old rather than as unknown. An unparseable version
 * is treated as fine: refusing to print because a version string had an
 * unexpected shape would be worse than the drift it's guarding against.
 */
export function serverSupportsBackPrinting(serverVersion: string | null): boolean {
  if (serverVersion == null) return false;
  const comparison = compareVersions(serverVersion, BACK_PRINTING_MIN_SERVER_VERSION);
  return comparison == null || comparison >= 0;
}

// Server-readiness gate. Local mode's sidecar can take real time to
// start (cold model-loading, disk I/O) — rather than block the whole UI
// behind a "Connecting…" screen, ConnectGate renders the app immediately
// and drives this gate through "starting" -> "ready"/"error". api/generation.ts
// awaits waitForServerReady() before every real request, so in-flight
// queries just wait in place (staying in their normal loading state)
// instead of firing doomed fetches against a server that isn't up yet.
// Remote mode never touches this — its reachability check already
// resolves synchronously before the app renders, so the gate stays at
// its default "ready" state for that path.
export type ServerReadiness =
  | { status: "ready" }
  | { status: "starting" }
  | { status: "error"; message: string };

let readiness: ServerReadiness = { status: "ready" };
let readyPromise: Promise<void> = Promise.resolve();
let readyResolve: (() => void) | null = null;
let readyReject: ((err: Error) => void) | null = null;
let listeners: Array<() => void> = [];

function notify(): void {
  for (const listener of listeners) listener();
}

export function setServerStarting(): void {
  readiness = { status: "starting" };
  readyPromise = new Promise((resolve, reject) => {
    readyResolve = resolve;
    readyReject = reject;
  });
  // Swallow here so a rejection with nothing yet awaiting it (e.g. the
  // server errors before any query is in flight) doesn't surface as an
  // unhandled rejection — real callers still get it via their own await,
  // since this is a separate derived chain off the same promise.
  readyPromise.catch(() => {});
  notify();
}

export function setServerReady(): void {
  readiness = { status: "ready" };
  // Two halves, and the second is easy to miss. Resolving wakes whoever is
  // already parked. Replacing the promise is what stops a gate that
  // REJECTED from staying rejected forever: setServerStarting is the only
  // other place readyPromise is assigned, so after a failed local start,
  // every later "we're ready now" left the dead server's rejection in
  // place and every request on the connection that replaced it failed with
  // its error message. That is precisely the recovery route out of a failed
  // boot — give up on Local, connect to a remote server instead — which
  // must not inherit the failure it is escaping.
  readyResolve?.();
  readyResolve = null;
  readyReject = null;
  readyPromise = Promise.resolve();
  notify();
}

export function setServerError(message: string): void {
  readiness = { status: "error", message };
  readyReject?.(new Error(message));
  notify();
}

export function getServerReadiness(): ServerReadiness {
  return readiness;
}

export function subscribeServerReadiness(callback: () => void): () => void {
  listeners.push(callback);
  return () => {
    listeners = listeners.filter((l) => l !== callback);
  };
}

export function waitForServerReady(): Promise<void> {
  return readyPromise;
}

// For components that need to render differently while starting (e.g. a
// "Starting local server…" placeholder instead of a misleading "no
// projects yet" empty state) rather than just reacting to ready/error.
export function useServerReadiness(): ServerReadiness {
  return useSyncExternalStore(subscribeServerReadiness, getServerReadiness);
}
