import { createContext, useContext, useState, type ReactNode } from "react";
import { useQueryClient } from "@tanstack/react-query";
import {
  getConnectionMode,
  setApiBaseUrl,
  setConnectionMode,
  setServerError,
  setServerReady,
  setServerStarting,
} from "./config";
import { invokeStartLocalServer, invokeStopLocalServer, isTauri } from "./tauri";

const REMOTE_TIMEOUT_MS = 8000;
const API_PORT = 8000;
// Matches main.rs's fixed LOCAL_URL constant — known before the sidecar
// even reports ready, so this can be set optimistically the instant Local
// is picked instead of waiting on invokeStartLocalServer() to resolve.
const LOCAL_URL = "http://127.0.0.1:8000";

export type ConnectionTarget = { mode: "local" } | { mode: "remote"; host: string };

export type ConnectionStatus =
  | { kind: "not-tauri" } // plain browser dev tab: skip the picker, use config.ts's default
  | { kind: "picker" }
  | { kind: "remote-form" }
  | { kind: "connecting" }
  | { kind: "connected" }
  | { kind: "error"; message: string };

// Cross-origin fetches to an arbitrary remote host won't have CORS headers
// to read a real response, so this uses mode: "no-cors" — the response
// itself is opaque and unreadable, but the fetch promise still rejects on
// a genuine network failure (unreachable host, connection refused, DNS
// failure), which is all this needs: "is anything there at all" before
// committing the app to talking to it.
async function isReachable(url: string, timeoutMs: number): Promise<boolean> {
  const controller = new AbortController();
  const timer = setTimeout(() => controller.abort(), timeoutMs);
  try {
    await fetch(url, { mode: "no-cors", signal: controller.signal });
    return true;
  } catch {
    return false;
  } finally {
    clearTimeout(timer);
  }
}

interface ConnectionValue {
  status: ConnectionStatus;
  setStatus: (status: ConnectionStatus) => void;
  /** Which server the app is currently pointed at, once connected. */
  mode: "local" | "remote" | null;
  /** Last remote host entered, remembered so the switch dialog can prefill it. */
  host: string;
  setHost: (host: string) => void;
  /** Bumped on every successful switch; used as a remount key (see App.tsx). */
  sessionKey: number;
  /** First connect, from the launch picker. Drives `status`. */
  connect: (target: ConnectionTarget) => Promise<void>;
  /** Mid-session switch. Returns an error message, or null on success. */
  switchTo: (target: ConnectionTarget) => Promise<string | null>;
}

const ConnectionContext = createContext<ConnectionValue | null>(null);

export function useConnection(): ConnectionValue {
  const ctx = useContext(ConnectionContext);
  if (!ctx) throw new Error("useConnection must be used within ConnectionProvider");
  return ctx;
}

// Owns the Local/Remote decision for the whole app. This lives above
// ConnectGate rather than inside it because the choice is no longer
// once-per-launch: the Decklist settings sidebar can switch servers
// mid-session, and the component that triggers a switch gets unmounted by
// the remount that switch causes — so the logic can't live there.
export function ConnectionProvider({ children }: { children: ReactNode }) {
  const queryClient = useQueryClient();
  const [status, setStatus] = useState<ConnectionStatus>(() =>
    isTauri() ? { kind: "picker" } : { kind: "not-tauri" },
  );
  const [mode, setMode] = useState<"local" | "remote" | null>(null);
  const [host, setHost] = useState("");
  const [sessionKey, setSessionKey] = useState(0);

  // Points the API client at a target that's already been validated.
  // Local deliberately doesn't block on readiness — the app renders
  // immediately and ServerStatusToast reports when the sidecar is
  // actually up (api/client.ts's requests wait on the same gate).
  function applyTarget(target: ConnectionTarget) {
    if (target.mode === "remote") {
      setApiBaseUrl(`http://${target.host}:${API_PORT}`);
      setConnectionMode("remote");
      // Remote must resolve the readiness gate explicitly. Every request
      // in api/client.ts awaits waitForServerReady(); if a previous local
      // session left the gate "starting" (or rejected), skipping this
      // hangs or fails every single request against the remote host with
      // no visible cause.
      setServerReady();
      setMode("remote");
      setHost(target.host);
      return;
    }

    setConnectionMode("local");
    setApiBaseUrl(LOCAL_URL);
    setServerStarting();
    setMode("local");
    invokeStartLocalServer()
      .then((url) => {
        setApiBaseUrl(url);
        setServerReady();
      })
      .catch((err) => {
        setServerError(err instanceof Error ? err.message : String(err));
      });
  }

  async function connect(target: ConnectionTarget) {
    if (target.mode === "remote") {
      setStatus({ kind: "connecting" });
      const url = `http://${target.host}:${API_PORT}`;
      if (!(await isReachable(`${url}/api/health`, REMOTE_TIMEOUT_MS))) {
        setStatus({
          kind: "error",
          message: `Couldn't reach ${target.host}:${API_PORT} — check the address and that the server is running.`,
        });
        return;
      }
    }
    applyTarget(target);
    setStatus({ kind: "connected" });
  }

  async function switchTo(target: ConnectionTarget): Promise<string | null> {
    // Validate the destination *before* tearing anything down — an
    // unreachable host should leave the current connection untouched
    // rather than stranding the app with no server at all.
    if (target.mode === "remote") {
      const url = `http://${target.host}:${API_PORT}`;
      if (!(await isReachable(`${url}/api/health`, REMOTE_TIMEOUT_MS))) {
        return `Couldn't reach ${target.host}:${API_PORT} — check the address and that the server is running.`;
      }
    }

    // Read the live value rather than the `mode` state: this may be
    // called from a handler that closed over a stale render.
    if (getConnectionMode() === "local" && isTauri()) {
      try {
        await invokeStopLocalServer();
      } catch {
        // Best effort — a server we failed to stop shouldn't block the
        // switch, and the user's next action is more important than a
        // stray process we've already stopped talking to.
      }
    }

    applyTarget(target);

    // Query keys carry no host discriminator, so without this the new
    // server would be served the previous one's cached projects/cards.
    queryClient.clear();
    setSessionKey((k) => k + 1);
    return null;
  }

  const value: ConnectionValue = {
    status,
    setStatus,
    mode,
    host,
    setHost,
    sessionKey,
    connect,
    switchTo,
  };

  return <ConnectionContext.Provider value={value}>{children}</ConnectionContext.Provider>;
}
