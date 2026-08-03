import { useState, type ReactNode } from "react";
import {
  setApiBaseUrl,
  setConnectionMode,
  setServerError,
  setServerReady,
  setServerStarting,
} from "./config";
import { invokeStartLocalServer, isTauri } from "./tauri";

const REMOTE_TIMEOUT_MS = 8000;
const API_PORT = 8000;
// Matches main.rs's fixed LOCAL_URL constant — known before the sidecar
// even reports ready, so this can be set optimistically the instant Local
// is picked instead of waiting on invokeStartLocalServer() to resolve.
const LOCAL_URL = "http://127.0.0.1:8000";

interface ChosenConfig {
  mode: "local" | "remote";
  host?: string;
}

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

type Status =
  | { kind: "not-tauri" } // plain browser dev tab: skip the picker, use config.ts's default
  | { kind: "picker" }
  | { kind: "remote-form" }
  | { kind: "connecting" }
  | { kind: "connected" }
  | { kind: "error"; message: string };

// First-launch Local/Remote picker, ported from the old plain-JS
// desktop/src/index.html into React so it can share the same dynamic
// API_BASE_URL (config.ts) the rest of the app depends on. Gates the
// whole app: nothing under it renders until connected (or we're in a
// plain browser tab, where there's nothing to gate — see the
// "not-tauri" status).
//
// Deliberately not persisted across launches (no localStorage) — every
// app start asks again. An earlier version remembered the last choice
// and silently reconnected, which meant there was no way to switch
// Local/Remote without manually clearing devtools storage.
export default function ConnectGate({ children }: { children: ReactNode }) {
  const [status, setStatus] = useState<Status>(() =>
    isTauri() ? { kind: "picker" } : { kind: "not-tauri" },
  );
  const [host, setHost] = useState("");

  async function connect(config: ChosenConfig) {
    if (config.mode === "remote") {
      setStatus({ kind: "connecting" });
      const url = `http://${config.host}:${API_PORT}`;
      const reachable = await isReachable(`${url}/api/health`, REMOTE_TIMEOUT_MS);
      if (!reachable) {
        setStatus({
          kind: "error",
          message: `Couldn't reach ${config.host}:${API_PORT} — check the address and that the server is running.`,
        });
        return;
      }
      setApiBaseUrl(url);
      setConnectionMode("remote");
      setStatus({ kind: "connected" });
      return;
    }

    // Local doesn't block on the sidecar becoming ready — render the app
    // immediately and let ServerStatusToast (driven by config.ts's
    // readiness gate) tell the user once it's actually up, or if it
    // failed. api/client.ts's request()/downloadPdf() await the same
    // gate, so any queries fired before then just wait in place.
    setConnectionMode("local");
    setApiBaseUrl(LOCAL_URL);
    setServerStarting();
    setStatus({ kind: "connected" });

    invokeStartLocalServer()
      .then((url) => {
        setApiBaseUrl(url);
        setServerReady();
      })
      .catch((err) => {
        setServerError(err instanceof Error ? err.message : String(err));
      });
  }

  function pickLocal() {
    connect({ mode: "local" });
  }

  function submitRemote() {
    if (!host.trim()) return;
    connect({ mode: "remote", host: host.trim() });
  }

  function backToPicker() {
    setHost("");
    setStatus({ kind: "picker" });
  }

  if (status.kind === "not-tauri" || status.kind === "connected") {
    return <>{children}</>;
  }

  return (
    <div className="gate">
      <h1>Proxy Scaler</h1>
      <p className="gate-sub">Where should generation run?</p>

      {status.kind === "picker" && (
        <>
          <button className="gate-option" onClick={pickLocal}>
            <strong>Use this device</strong>
            <span>Runs everything locally — no setup needed.</span>
          </button>
          <button className="gate-option" onClick={() => setStatus({ kind: "remote-form" })}>
            <strong>Connect to a server</strong>
            <span>Point this app at a proxy-scaler server running elsewhere.</span>
          </button>
        </>
      )}

      {status.kind === "remote-form" && (
        <>
          <button className="btn-sm gate-back" onClick={backToPicker}>
            &larr; Back
          </button>
          <p className="hint" style={{ marginBottom: 12 }}>
            We recommend a tool like{" "}
            <a href="https://tailscale.com" target="_blank" rel="noreferrer">
              Tailscale
            </a>{" "}
            for connecting to your remote server.
          </p>
          <label className="field" style={{ marginBottom: 12 }}>
            <span>Server address</span>
            <input
              value={host}
              onChange={(e) => setHost(e.target.value)}
              onKeyDown={(e) => e.key === "Enter" && submitRemote()}
              placeholder="IP or name (e.g. 100.x.x.x or my-server)"
            />
          </label>
          <button className="btn-primary" onClick={submitRemote} disabled={!host.trim()}>
            Connect
          </button>
        </>
      )}

      {status.kind === "connecting" && <p className="hint">Connecting…</p>}

      {status.kind === "error" && (
        <>
          <p className="error-text" style={{ marginBottom: 12 }}>
            {status.message}
          </p>
          <button onClick={backToPicker}>Change server</button>
        </>
      )}
    </div>
  );
}
