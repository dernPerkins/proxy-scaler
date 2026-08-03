import React from "react";
import ReactDOM from "react-dom/client";
import { QueryClient, QueryClientProvider } from "@tanstack/react-query";
import { BrowserRouter } from "react-router-dom";
import App from "./App";
import ConnectGate from "./ConnectGate";

const queryClient = new QueryClient();

ReactDOM.createRoot(document.getElementById("root")!).render(
  <React.StrictMode>
    <QueryClientProvider client={queryClient}>
      {/* Nothing under here mounts (so no query fires against the wrong
          base URL) until ConnectGate has actually resolved one — either
          by invoking the Tauri sidecar (Local) or confirming a remote
          host is reachable (Remote), or immediately in a plain browser
          tab where there's nothing to gate. */}
      <ConnectGate>
        <BrowserRouter>
          <App />
        </BrowserRouter>
      </ConnectGate>
    </QueryClientProvider>
  </React.StrictMode>,
);
