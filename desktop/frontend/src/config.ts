// During Phase 2 development this just points at a manually-started
// `uvicorn proxy_scaler.api:app` for fast browser-tab iteration — no
// Tauri needed yet. Phase 3 (Tauri integration) wires this to whatever
// URL the Local/Remote picker resolves (invoke("start_local_server")'s
// return value, or the user-entered remote host) instead of this
// hardcoded dev default.
export const API_BASE_URL: string =
  (import.meta.env.VITE_API_BASE_URL as string | undefined) ?? "http://127.0.0.1:8000";
