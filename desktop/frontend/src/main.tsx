import React from "react";
import ReactDOM from "react-dom/client";
import { QueryClient, QueryClientProvider } from "@tanstack/react-query";
import { BrowserRouter } from "react-router-dom";
import App from "./App";
import PatchNotesPrompt from "./components/PatchNotesPrompt";
import UpdatePrompt from "./components/UpdatePrompt";
import ConnectGate from "./ConnectGate";
import { ConnectionProvider } from "./connection";
import "./styles.css";

const queryClient = new QueryClient();

ReactDOM.createRoot(document.getElementById("root")!).render(
  <React.StrictMode>
    <QueryClientProvider client={queryClient}>
      {/* Nothing under here mounts (so no query fires against the wrong
          base URL) until ConnectGate has actually resolved one — either
          by invoking the Tauri sidecar (Local) or confirming a remote
          host is reachable (Remote), or immediately in a plain browser
          tab where there's nothing to gate. */}
      {/* ConnectionProvider sits above the gate because the connection can
          also be switched mid-session from the Decklist settings sidebar —
          it isn't a launch-only decision any more. */}
      <ConnectionProvider>
        {/* Above ConnectGate on purpose: the update check must fire on
            LAUNCH, not on picking Local/Remote — the offer should be on
            screen even while the picker is. Safe up here because it
            talks only to Tauri commands (the manifest fetch happens in
            Rust), so it needs no connection, router, or query client —
            the wrong-base-URL hazard the gate exists for can't touch it. */}
        <UpdatePrompt />
        {/* Same reasoning as UpdatePrompt: the notes auto-open on LAUNCH,
            over the picker, and touch only Tauri commands. Mounted after
            it on purpose — portals stack in mount order, and the update
            offer outranks notes that installing it would make stale. */}
        <PatchNotesPrompt />
        <ConnectGate>
          <BrowserRouter>
            <App />
          </BrowserRouter>
        </ConnectGate>
      </ConnectionProvider>
    </QueryClientProvider>
  </React.StrictMode>,
);
