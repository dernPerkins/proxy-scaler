import { createContext, useContext, useEffect, useState, type ReactNode } from "react";
import { useQueryClient } from "@tanstack/react-query";
import { generationApi } from "./api/generation";
import { projectApi, type RecentHost } from "./api/project";
import {
  clearProbedDevice,
  clearServerVersion,
  getApiBaseUrl,
  getConnectionMode,
  setApiBaseUrl,
  setConnectionMode,
  setProbedDevice,
  setServerError,
  setServerReady,
  setServerStarting,
  setServerVersion,
} from "./config";
import { invokeStartLocalServer, invokeStopLocalServer, isTauri } from "./tauri";

const REMOTE_TIMEOUT_MS = 8000;
const HEALTH_PING_INTERVAL_MS = 30_000;
const HEALTH_PING_TIMEOUT_MS = 5000;
// Matches supervisor.py's DEFAULT_PORT — what a bare `proxy-scaler-serve`
// binds to, so this is what the remote-connect port field defaults to.
// Exported for SwitchServerDialog.tsx's own port field, so there's one
// source of truth rather than two literals that can drift apart. (13207
// is just M-T-G spelled out in letter positions — 13th, 20th, 7th — picked
// to dodge the usual 8000/8080/8888/9000/etc collisions.)
export const DEFAULT_REMOTE_PORT = 13207;
// Matches main.rs's fixed LOCAL_URL constant — known before the sidecar
// even reports ready, so this can be set optimistically the instant Local
// is picked instead of waiting on invokeStartLocalServer() to resolve.
const LOCAL_URL = "http://127.0.0.1:13207";

export type ConnectionTarget =
  | { mode: "local" }
  | { mode: "remote"; host: string; port: number };

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
  /** Last remote port entered/connected with — defaults to DEFAULT_REMOTE_PORT. */
  port: number;
  setPort: (port: number) => void;
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
   * Re-attempt a local sidecar start after one failed — ServerBootModal's
   * "Try again". Safe to call repeatedly: main.rs hard-kills the child and
   * clears its slot on a failed start, precisely so a retry respawns
   * rather than hitting its "already running" early return.
   */
  retryLocal: () => void;
  /**
   * Manual re-check of the current remote host, for a "Reconnect" button
   * rather than waiting up to HEALTH_PING_INTERVAL_MS for the next
   * automatic ping. Returns whether it's reachable now. A no-op (returns
   * true) outside remote mode.
   */
  reconnect: () => Promise<boolean>;
  /**
   * Remote host+port pairs successfully connected to before, most-recent-
   * first — see project_store.rs's recent_remote_hosts. Loaded once on
   * mount; connect()/switchTo() append to it themselves on a successful
   * remote connection, so callers never need to call add_recent_host
   * directly.
   */
  recentHosts: RecentHost[];
  /** Removes one saved entry (e.g. a bad address) from the list above. */
  removeRecentHost: (entry: RecentHost) => Promise<void>;
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
  const [port, setPort] = useState(DEFAULT_REMOTE_PORT);
  const [remoteHealthy, setRemoteHealthy] = useState(true);
  const [recentHosts, setRecentHosts] = useState<RecentHost[]>([]);

  // Loaded once — a plain browser dev tab (isTauri() false) has no invoke
  // boundary to call, so this stays empty there, matching every other
  // Tauri-only affordance in this app.
  useEffect(() => {
    if (!isTauri()) return;
    projectApi
      .listRecentHosts()
      .then(setRecentHosts)
      .catch(() => {
        // Best-effort — an empty list just means the picker shows no
        // suggestions, not a broken app.
      });
  }, []);

  async function removeRecentHost(entry: RecentHost): Promise<void> {
    try {
      setRecentHosts(await projectApi.removeRecentHost(entry.host, entry.port));
    } catch {
      // Best-effort — leave the list as-is if the write failed.
    }
  }

  // Records a successful remote connection so it shows up as a saved
  // entry next time. Best-effort and non-blocking: called after the
  // connection itself already succeeded, so a persistence failure here
  // must never surface as a connection error.
  async function rememberHost(target: ConnectionTarget): Promise<void> {
    if (target.mode !== "remote") return;
    try {
      setRecentHosts(await projectApi.addRecentHost(target.host, target.port));
    } catch {
      // Ignored — see comment above.
    }
  }

  // Best-effort, fire-and-forget — see ProjectContext.tsx's
  // recommendedDefaultModel(), the only consumer of the probed device. Only
  // ever called after setServerReady() (both call sites below), so this
  // can't race /api/health or anything else on the startup path. A
  // failure here (torch not importable, a slow/CPU-only box that errors,
  // network hiccup) just leaves the probed device at null forever for this
  // connection — recommendedDefaultModel() already treats null as "fall
  // back to the mode-based guess," so there's nothing to recover here.
  function probeGpu(): void {
    generationApi
      .getDevice()
      .then((d) => setProbedDevice(d))
      .catch(() => {});
  }

  // Same fire-and-forget discipline as probeGpu, same call sites. The
  // answer feeds VersionMismatchToast — in Remote mode the client and
  // server are updated on different machines, and this is the only thing
  // that tells the user they've diverged. A failure (older server with no
  // /api/version — it 404s — or a network hiccup) leaves the version
  // unknown, and unknown deliberately never warns.
  function probeServerVersion(): void {
    generationApi
      .getServerVersion()
      .then((v) => setServerVersion(v.version))
      .catch(() => {});
  }

  // Everything that fires the instant a connection goes ready, in the one
  // order that matters. /api/device imports torch — tens of seconds on a
  // real GPU box, holding the GIL in long stretches — and on the local
  // path every request parked on the readiness gate unblocks at this same
  // instant. Letting the card-db check (a local stat + a four-row read)
  // go first is the difference between it answering immediately and it
  // answering after torch finishes loading, which is exactly how the card
  // database used to look like the slow, last thing about launching.
  //
  // Nothing is lost by making the probe wait: it is fire-and-forget, only
  // refines the default-model guess, and ProjectContext already corrects
  // that guess whenever the answer lands late (subscribeProbedDevice).
  function probeAfterReady(): void {
    probeServerVersion();
    void queryClient
      .prefetchQuery({
        queryKey: ["card-db-status"],
        queryFn: () => generationApi.cardDbStatus(),
      })
      .finally(probeGpu);
  }

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
    // Every target switch starts from "GPU status unknown" — see
    // clearProbedDevice's own comment for why leaving the previous
    // server's answer in place until the new probe resolves is actively
    // wrong, not just stale. The server version resets for the same
    // reason: the old server's version must not be compared against a
    // connection it no longer describes.
    clearProbedDevice();
    clearServerVersion();

    if (target.mode === "remote") {
      setApiBaseUrl(`http://${target.host}:${target.port}`);
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
      probeAfterReady();
      setMode("remote");
      setHost(target.host);
      setPort(target.port);
      return;
    }

    setConnectionMode("local");
    setApiBaseUrl(LOCAL_URL);
    setMode("local");
    startLocal();
  }

  // The sidecar spawn itself, split out of applyTarget so retryLocal can
  // re-run exactly this and nothing else — a retry must not redo the
  // base-URL/mode bookkeeping, which is already correct and unchanged.
  function startLocal(): void {
    setServerStarting();
    invokeStartLocalServer()
      .then((url) => {
        setApiBaseUrl(url);
        setServerReady();
        probeAfterReady();
      })
      .catch((err) => {
        setServerError(err instanceof Error ? err.message : String(err));
      });
  }

  async function connect(target: ConnectionTarget) {
    if (target.mode === "remote") {
      setStatus({ kind: "connecting" });
      const url = `http://${target.host}:${target.port}`;
      if (!(await isReachable(`${url}/api/health`, REMOTE_TIMEOUT_MS))) {
        setStatus({
          kind: "error",
          message: `Couldn't reach ${target.host}:${target.port} — check the address and that the server is running.`,
        });
        return;
      }
    }
    applyTarget(target);
    void rememberHost(target);
    setStatus({ kind: "connected" });
  }

  async function switchTo(target: ConnectionTarget): Promise<string | null> {
    // Validate the destination *before* tearing anything down — an
    // unreachable host should leave the current connection untouched
    // rather than stranding the app with no server at all.
    if (target.mode === "remote") {
      const url = `http://${target.host}:${target.port}`;
      if (!(await isReachable(`${url}/api/health`, REMOTE_TIMEOUT_MS))) {
        return `Couldn't reach ${target.host}:${target.port} — check the address and that the server is running.`;
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
    void rememberHost(target);

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
    queryClient.invalidateQueries({ queryKey: ["gen-paths"] });
    return null;
  }

  function retryLocal(): void {
    startLocal();
  }

  async function reconnect(): Promise<boolean> {
    if (mode !== "remote" || !host) return true;
    const url = `http://${host}:${port}`;
    const ok = await isReachable(`${url}/api/health`, REMOTE_TIMEOUT_MS);
    if (ok) {
      setRemoteHealthy(true);
      queryClient.invalidateQueries({ queryKey: ["generation-status"] });
      queryClient.invalidateQueries({ queryKey: ["tasks"] });
      queryClient.invalidateQueries({ queryKey: ["worker-status"] });
      queryClient.invalidateQueries({ queryKey: ["pdf-preview"] });
      queryClient.invalidateQueries({ queryKey: ["models"] });
      queryClient.invalidateQueries({ queryKey: ["gen-paths"] });
    }
    return ok;
  }

  const value: ConnectionValue = {
    status,
    setStatus,
    mode,
    host,
    setHost,
    port,
    setPort,
    remoteHealthy,
    connect,
    switchTo,
    retryLocal,
    reconnect,
    recentHosts,
    removeRecentHost,
  };

  return <ConnectionContext.Provider value={value}>{children}</ConnectionContext.Provider>;
}
