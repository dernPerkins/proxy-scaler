import { useSyncExternalStore } from "react";

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
// gpuAvailable probe below hasn't answered yet — local runs on-device, so
// a fast/light model is assumed the safer default; remote is assumed to
// have real GPU headroom. Both are just guesses; gpuAvailable is the real
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
let gpuAvailable: boolean | null = null;

export function getGpuAvailable(): boolean | null {
  return gpuAvailable;
}

export function setGpuAvailable(available: boolean): void {
  gpuAvailable = available;
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
  readyResolve?.();
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
