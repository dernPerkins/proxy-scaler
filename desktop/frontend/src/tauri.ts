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
  // Rust-to-frontend events. Permitted by the `core:default` set already
  // listed in src-tauri/capabilities/default.json — no extra capability
  // needed. `listen` resolves to its own unlisten function.
  event: {
    listen: <T = unknown>(
      event: string,
      handler: (e: { payload: T }) => void,
    ) => Promise<() => void>;
  };
  // The window handle. Only onCloseRequested is used, and only by
  // QuitPrompt.tsx — see listenCloseRequested below. It is a listen()
  // underneath, so like the events above it needs no capability beyond the
  // `core:default` set in src-tauri/capabilities/default.json.
  window: {
    getCurrentWindow: () => {
      onCloseRequested: (
        handler: (event: CloseRequestedEvent) => void | Promise<void>,
      ) => Promise<() => void>;
    };
  };
}

/** The one member of Tauri's CloseRequestedEvent this app uses. */
export interface CloseRequestedEvent {
  preventDefault: () => void;
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

// Native "Save As" dialog. Returns null if the user canceled.
// Separate from the transfer below so a caller can ask where to save
// *before* starting slow work (a PDF render runs tens of seconds
// server-side) rather than interrupting with a dialog once it's done.
export async function invokePickSavePath(suggestedName: string): Promise<string | null> {
  if (!window.__TAURI__) {
    throw new Error("Not running inside Tauri");
  }
  return window.__TAURI__.core.invoke<string | null>("pick_save_path", { suggestedName });
}

// Rust fetches the URL and streams it to `path` (see
// main.rs::download_to_path), reporting via `download-progress` events.
// Only a URL crosses the IPC boundary — deliberately not the bytes: an
// earlier version passed `Array.from(uint8array)`, which turned a 15-25MB
// image into a multi-million-element JS array for Tauri to
// JSON-serialize, and that was the actual cause of downloads hanging for
// minutes or failing outright on larger files. Pass `body` to issue a
// JSON POST instead of a GET.
// Returns false if the transfer was canceled (not an error).
export async function invokeDownloadToPath(args: {
  url: string;
  path: string;
  downloadId: string;
  body?: unknown;
}): Promise<boolean> {
  if (!window.__TAURI__) {
    throw new Error("Not running inside Tauri");
  }
  return window.__TAURI__.core.invoke<boolean>("download_to_path", {
    url: args.url,
    path: args.path,
    downloadId: args.downloadId,
    body: args.body === undefined ? null : JSON.stringify(args.body),
  });
}

export async function invokeCancelDownload(downloadId: string): Promise<void> {
  if (!window.__TAURI__) return;
  await window.__TAURI__.core.invoke<void>("cancel_download", { downloadId });
}

/** Subscribe to the window's close button (the X, alt+F4, a WM close).
 *  Resolves to an unlisten fn. A no-op in a plain browser tab, which has
 *  no window of its own to close.
 *
 *  The handler MUST call `preventDefault()`. Tauri's wrapper around this
 *  event calls `destroy()` on the window as soon as a handler returns
 *  without it — which would tear the window down underneath the Rust-side
 *  shutdown this app runs from the same event (main.rs::on_window_event).
 *  Preventing here does not keep the app alive: Rust has already called
 *  prevent_close() and owns the teardown either way. */
export async function listenCloseRequested(
  handler: (event: CloseRequestedEvent) => void | Promise<void>,
): Promise<() => void> {
  if (!window.__TAURI__) return () => {};
  return window.__TAURI__.window.getCurrentWindow().onCloseRequested(handler);
}

/** Tells Rust a close-request handler is now mounted, so the close path
 *  has someone to ask. Until this lands, closing the window tears down
 *  immediately — which is what should happen while the connect gate is up
 *  and there is no project on screen to name. */
export async function invokeQuitPromptListening(): Promise<void> {
  if (!window.__TAURI__) return;
  await window.__TAURI__.core.invoke<void>("quit_prompt_listening");
}

/** The webview's side of the close handshake — see main.rs::QuitReply.
 *  "prompting" says the quit prompt is on screen and the teardown should
 *  wait for the user; "proceed" releases it. */
export async function invokeAnswerQuitPrompt(
  reply: "prompting" | "proceed",
): Promise<void> {
  if (!window.__TAURI__) return;
  await window.__TAURI__.core.invoke<void>("answer_quit_prompt", { reply });
}

export interface DownloadProgressEvent {
  id: string;
  downloaded: number;
  total: number | null;
}

/** Subscribe to Rust's transfer progress. Resolves to an unlisten fn. */
export async function listenDownloadProgress(
  handler: (e: DownloadProgressEvent) => void,
): Promise<() => void> {
  if (!window.__TAURI__) return () => {};
  return window.__TAURI__.event.listen<DownloadProgressEvent>(
    "download-progress",
    (e) => handler(e.payload),
  );
}
