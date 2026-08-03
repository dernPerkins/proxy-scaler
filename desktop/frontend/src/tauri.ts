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

export async function invokeStartLocalServer(): Promise<string> {
  if (!window.__TAURI__) {
    throw new Error("Not running inside Tauri");
  }
  return window.__TAURI__.core.invoke<string>("start_local_server");
}

// Native "Save As" dialog + write to disk (see main.rs::save_file).
// Confirmed by real testing that the HTML `download` attribute isn't
// reliably honored by Tauri's webview on macOS — see downloadBlob in
// download.ts for the full story and the plain-browser fallback.
// Returns false if the user canceled the save dialog (not an error).
export async function invokeSaveFile(suggestedName: string, bytes: Uint8Array): Promise<boolean> {
  if (!window.__TAURI__) {
    throw new Error("Not running inside Tauri");
  }
  return window.__TAURI__.core.invoke<boolean>("save_file", {
    suggestedName,
    data: Array.from(bytes),
  });
}
