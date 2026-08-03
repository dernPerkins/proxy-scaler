// Mutable: in a plain browser tab (no Tauri) this stays the dev default
// below. Inside Tauri, ConnectGate overwrites it once via setApiBaseUrl
// after resolving Local (the sidecar's own reported URL) or Remote (the
// user-entered host) — every api/client.ts request reads the current
// value via getApiBaseUrl(), not a frozen import-time constant.
let apiBaseUrl: string =
  (import.meta.env.VITE_API_BASE_URL as string | undefined) ?? "http://127.0.0.1:8000";

export function getApiBaseUrl(): string {
  return apiBaseUrl;
}

export function setApiBaseUrl(url: string): void {
  apiBaseUrl = url;
}

// Set once by ConnectGate alongside setApiBaseUrl, right after Local/Remote
// is resolved. Used to pick a sensible default upscale model per mode (see
// ProjectContext.tsx) — local runs on-device, so a fast/light model is the
// better default; a remote server is assumed to have real GPU headroom.
let connectionMode: "local" | "remote" | null = null;

export function getConnectionMode(): "local" | "remote" | null {
  return connectionMode;
}

export function setConnectionMode(mode: "local" | "remote"): void {
  connectionMode = mode;
}
