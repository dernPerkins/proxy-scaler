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
