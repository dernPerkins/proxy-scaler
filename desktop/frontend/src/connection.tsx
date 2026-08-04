import { createContext, useContext, useEffect, useState, type ReactNode } from "react";
import { useQueryClient } from "@tanstack/react-query";
import {
  getApiBaseUrl,
  getConnectionMode,
  setApiBaseUrl,
  setConnectionMode,
  setServerError,
  setServerReady,
  setServerStarting,
} from "./config";
import { invokeStartLocalServer, invokeStopLocalServer, isTauri } from "./tauri";

const REMOTE_TIMEOUT_MS = 8000;
const HEALTH_PING_INTERVAL_MS = 30_000;
const HEALTH_PING_TIMEOUT_MS = 5000;
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
  /**
   * Whether the last health ping to a remote server succeeded. Always
   * true outside remote mode. See the 30s ping effect below — a remote
   * server can vanish (crash, network drop, someone closing the server
   * app) with nothing in this app noticing on its own, since every
   * request just... stops happening once the user stops interacting.
   */
  remoteHealthy: boolean;
  /** First connect, from the launch picker. Drives `status`. */
  connect: (target: ConnectionTarget) => Promise<void>;
  /** Mid-session switch. Returns an error message, or null on success. */
  switchTo: (target: ConnectionTarget) => Promise<string | null>;
  /**
   * Manual re-check of the current remote host, for a "Reconnect" button
   * rather than waiting up to HEALTH_PING_INTERVAL_MS for the next
   * automatic ping. Returns whether it's reachable now. A no-op (returns
   * true) outside remote mode.
   */
  reconnect: () => Promise<boolean>;
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
  const [remoteHealthy, setRemoteHealthy] = useState(true);

  // Heartbeat for remote mode only — local's liveness is already implied
  // by the sidecar process this app itself spawned and watches. A remote
  // server has no such signal: if it stops, nothing here notices unless
  // something actively checks, and the app would otherwise just sit there
  // looking normal while every request silently goes nowhere.
  useEffect(() => {
    if (status.kind !== "connected" || mode !== "remote") {
      setRemoteHealthy(true);
      return;
    }

    let cancelled = false;

    async function ping() {
      const controller = new AbortController();
      const timer = setTimeout(() => controller.abort(), HEALTH_PING_TIMEOUT_MS);
      try {
        const resp = await fetch(`${getApiBaseUrl()}/api/health`, { signal: controller.signal });
        if (!cancelled) setRemoteHealthy(resp.ok);
      } catch {
        if (!cancelled) setRemoteHealthy(false);
      } finally {
        clearTimeout(timer);
      }
    }

    ping();
    const interval = setInterval(ping, HEALTH_PING_INTERVAL_MS);
    return () => {
      cancelled = true;
      clearInterval(interval);
    };
  }, [status.kind, mode]);

  // Points the API client at a target that's already been validated.
  // Local deliberately doesn't block on readiness — the app renders
  // immediately and ServerStatusToast reports when the sidecar is
  // actually up (api/generation.ts's requests wait on the same gate).
  function applyTarget(target: ConnectionTarget) {
    if (target.mode === "remote") {
      setApiBaseUrl(`http://${target.host}:${API_PORT}`);
      setConnectionMode("remote");
      // Reset explicitly rather than waiting for the next scheduled ping:
      // both callers (connect() and switchTo()) already confirmed this
      // target is reachable before getting here. Without this, switching
      // away from a host that had gone unhealthy — even to a perfectly
      // fine new one — leaves remoteHealthy stuck at its old value (the
      // ping effect only re-runs on a local↔remote mode change, not on a
      // remote→remote host swap) until the next 30s tick, during which
      // ConnectionLostDialog wouldn't reopen even if the new host also
      // turns out to be down.
      setRemoteHealthy(true);
      // Remote must resolve the readiness gate explicitly. Every request
      // in api/generation.ts awaits waitForServerReady(); if a previous local
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

    // Project data (ProjectContext, api/project.ts) is local-only and
    // untouched by which generation server is connected — only
    // generation-scoped queries can be stale against the new host, so
    // this invalidates those specifically rather than queryClient.clear()
    // wiping locally-cached project state right along with them, and
    // rather than remounting ProjectProvider (which used to be the only
    // way to force its own project list to re-fetch against the new
    // host, back when project data lived server-side too).
    queryClient.invalidateQueries({ queryKey: ["generation-status"] });
    queryClient.invalidateQueries({ queryKey: ["tasks"] });
    queryClient.invalidateQueries({ queryKey: ["worker-status"] });
    queryClient.invalidateQueries({ queryKey: ["pdf-preview"] });
    queryClient.invalidateQueries({ queryKey: ["models"] });
    return null;
  }

  async function reconnect(): Promise<boolean> {
    if (mode !== "remote" || !host) return true;
    const url = `http://${host}:${API_PORT}`;
    const ok = await isReachable(`${url}/api/health`, REMOTE_TIMEOUT_MS);
    if (ok) {
      setRemoteHealthy(true);
      queryClient.invalidateQueries({ queryKey: ["generation-status"] });
      queryClient.invalidateQueries({ queryKey: ["tasks"] });
      queryClient.invalidateQueries({ queryKey: ["worker-status"] });
      queryClient.invalidateQueries({ queryKey: ["pdf-preview"] });
      queryClient.invalidateQueries({ queryKey: ["models"] });
    }
    return ok;
  }

  const value: ConnectionValue = {
    status,
    setStatus,
    mode,
    host,
    setHost,
    remoteHealthy,
    connect,
    switchTo,
    reconnect,
  };

  return <ConnectionContext.Provider value={value}>{children}</ConnectionContext.Provider>;
}
