import { type ReactNode } from "react";
import RecentHostsList from "./components/RecentHostsList";
import { useConnection } from "./connection";

// First-launch Local/Remote picker, ported from the old plain-JS
// desktop/src/index.html into React. Gates the whole app: nothing under
// it renders until connected (or we're in a plain browser tab, where
// there's nothing to gate — see the "not-tauri" status).
//
// The connection logic itself lives in connection.tsx, not here — the
// Decklist settings sidebar can also switch servers mid-session, so it
// can't be owned by the launch screen.
//
// Deliberately not persisted across launches (no localStorage) — every
// app start asks again. An earlier version remembered the last choice
// and silently reconnected, which meant there was no way to switch
// Local/Remote at all. That's no longer the only way to switch, but
// asking on a cold start is still the honest default.
export default function ConnectGate({ children }: { children: ReactNode }) {
  const { status, setStatus, host, setHost, connect, recentHosts, removeRecentHost } =
    useConnection();

  function submitRemote() {
    if (!host.trim()) return;
    connect({ mode: "remote", host: host.trim() });
  }

  function backToPicker() {
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
          <button className="gate-option" onClick={() => connect({ mode: "local" })}>
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

          {recentHosts.length > 0 && (
            <>
              <span className="hint" style={{ marginBottom: 4, display: "block" }}>
                Recent
              </span>
              <RecentHostsList
                hosts={recentHosts}
                onSelect={(h) => connect({ mode: "remote", host: h })}
                onRemove={removeRecentHost}
              />
            </>
          )}

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
