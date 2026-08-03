import { useEffect, useState, type ReactNode } from "react";
import { setApiBaseUrl } from "./config";
import { invokeStartLocalServer, isTauri } from "./tauri";

const CONFIG_KEY = "proxyScalerConfig";
const REMOTE_TIMEOUT_MS = 8000;
const API_PORT = 8000;

interface StoredConfig {
  mode: "local" | "remote";
  host?: string;
}

function getStoredConfig(): StoredConfig | null {
  try {
    const raw = localStorage.getItem(CONFIG_KEY);
    return raw ? (JSON.parse(raw) as StoredConfig) : null;
  } catch {
    return null;
  }
}

function saveConfig(config: StoredConfig): void {
  localStorage.setItem(CONFIG_KEY, JSON.stringify(config));
}

function clearConfig(): void {
  localStorage.removeItem(CONFIG_KEY);
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
// desktop/src/index.html into React so it can share localStorage config
// and the same dynamic API_BASE_URL (config.ts) the rest of the app
// depends on. Gates the whole app: nothing under it renders until
// connected (or we're in a plain browser tab, where there's nothing to
// gate — see the "not-tauri" status).
export default function ConnectGate({ children }: { children: ReactNode }) {
  const [status, setStatus] = useState<Status>(() =>
    isTauri() ? { kind: "picker" } : { kind: "not-tauri" },
  );
  const [host, setHost] = useState("");

  useEffect(() => {
    if (!isTauri()) return;
    const existing = getStoredConfig();
    if (existing) {
      connect(existing);
    }
    // Only ever run once on mount — connect() itself is stable enough
    // for this (it doesn't close over anything that changes in a way
    // that would make a stale closure here matter).
    // eslint-disable-next-line react-hooks/exhaustive-deps
  }, []);

  async function connect(config: StoredConfig) {
    setStatus({ kind: "connecting" });

    if (config.mode === "remote") {
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
      setStatus({ kind: "connected" });
      return;
    }

    try {
      const url = await invokeStartLocalServer();
      setApiBaseUrl(url);
      setStatus({ kind: "connected" });
    } catch (err) {
      setStatus({
        kind: "error",
        message: `Couldn't start the local server: ${err instanceof Error ? err.message : String(err)}`,
      });
    }
  }

  function pickLocal() {
    const config: StoredConfig = { mode: "local" };
    saveConfig(config);
    connect(config);
  }

  function submitRemote() {
    if (!host.trim()) return;
    const config: StoredConfig = { mode: "remote", host: host.trim() };
    saveConfig(config);
    connect(config);
  }

  function changeServer() {
    clearConfig();
    setHost("");
    setStatus({ kind: "picker" });
  }

  if (status.kind === "not-tauri" || status.kind === "connected") {
    return <>{children}</>;
  }

  return (
    <div style={{ fontFamily: "sans-serif", padding: 32, maxWidth: 420, margin: "0 auto" }}>
      <h1>Proxy Scaler</h1>

      {status.kind === "picker" && (
        <>
          <button
            style={{ display: "block", width: "100%", marginBottom: 8, padding: 12 }}
            onClick={pickLocal}
          >
            <strong>Use this device</strong>
            <div style={{ fontSize: 12, opacity: 0.7 }}>
              Runs everything locally — no setup needed.
            </div>
          </button>
          <button
            style={{ display: "block", width: "100%", padding: 12 }}
            onClick={() => setStatus({ kind: "remote-form" })}
          >
            <strong>Connect to a server</strong>
            <div style={{ fontSize: 12, opacity: 0.7 }}>
              Point this app at a proxy-scaler server running elsewhere.
            </div>
          </button>
        </>
      )}

      {status.kind === "remote-form" && (
        <>
          <button onClick={() => setStatus({ kind: "picker" })}>&larr; back</button>
          <p style={{ fontSize: 12, opacity: 0.7 }}>
            We recommend a tool like{" "}
            <a href="https://tailscale.com" target="_blank" rel="noreferrer">
              Tailscale
            </a>{" "}
            for connecting to your remote server.
          </p>
          <input
            value={host}
            onChange={(e) => setHost(e.target.value)}
            placeholder="IP or name (e.g. 100.x.x.x or my-server)"
            style={{ width: "100%", marginBottom: 8, padding: 8 }}
          />
          <button onClick={submitRemote} disabled={!host.trim()}>
            Connect
          </button>
        </>
      )}

      {status.kind === "connecting" && <p>Connecting…</p>}

      {status.kind === "error" && (
        <>
          <p style={{ color: "#c66" }}>{status.message}</p>
          <button onClick={changeServer}>change server</button>
        </>
      )}
    </div>
  );
}
