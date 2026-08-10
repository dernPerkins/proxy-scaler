// Thin wrapper around Tauri's injected global (window.__TAURI__, present
// because tauri.conf.json sets app.withGlobalTauri: true — avoids needing
// the @tauri-apps/api npm package / a bundler-level Tauri dependency just
// for this one command call). Undefined entirely in a plain browser tab
// (e.g. `npm run dev` without Tauri), which is how ConnectGate decides
// whether to show the Local/Remote picker at all.
interface TauriGlobal {
  core: {
    invoke: <T = unknown>(cmd: string, args?: Record<string, unknown>) => Promise<T>;
  };
}

declare global {
  interface Window {
    __TAURI__?: TauriGlobal;
  }
}

export function isTauri(): boolean {
  return typeof window !== "undefined" && window.__TAURI__ != null;
}

// Generic invoke wrapper for app-defined commands (as opposed to the
// specific plugin-backed ones below) — used by api/project.ts to call
// the Rust-side local project store. Argument keys are camelCase here;
// Tauri v2 maps them to the snake_case Rust parameter names itself (e.g.
// `projectId` -> `project_id`), the same convention invokeDownloadToFile below
// already relies on for `suggestedName` -> `suggested_name`.
export async function invokeCommand<T>(
  cmd: string,
  args?: Record<string, unknown>,
): Promise<T> {
  if (!window.__TAURI__) {
    throw new Error("Not running inside Tauri");
  }
  return window.__TAURI__.core.invoke<T>(cmd, args);
}

export async function invokeStartLocalServer(): Promise<string> {
  if (!window.__TAURI__) {
    throw new Error("Not running inside Tauri");
  }
  return window.__TAURI__.core.invoke<string>("start_local_server");
}

// Stops the local sidecar without exiting the app (see
// main.rs::stop_local_server) — used when switching to a remote server so
// the local worker stops holding memory for a server nobody's using.
// A no-op if nothing is running.
export async function invokeStopLocalServer(): Promise<void> {
  if (!window.__TAURI__) {
    throw new Error("Not running inside Tauri");
  }
  await window.__TAURI__.core.invoke<void>("stop_local_server");
}

// Native "Save As" dialog, then Rust fetches the URL and writes it to
// disk (see main.rs::download_to_file). Only a URL crosses the IPC
// boundary — deliberately not the bytes: the previous version passed
// `Array.from(uint8array)`, which turned a 15-25MB image into a
// multi-million-element JS array for Tauri to JSON-serialize, and that
// was the actual cause of downloads hanging for minutes or failing
// outright on larger files. Pass `body` to issue a JSON POST (the PDF
// routes) instead of a GET.
// Returns false if the user canceled the save dialog (not an error).
export async function invokeDownloadToFile(
  url: string,
  suggestedName: string,
  body?: unknown,
): Promise<boolean> {
  if (!window.__TAURI__) {
    throw new Error("Not running inside Tauri");
  }
  return window.__TAURI__.core.invoke<boolean>("download_to_file", {
    url,
    suggestedName,
    body: body === undefined ? null : JSON.stringify(body),
  });
}
