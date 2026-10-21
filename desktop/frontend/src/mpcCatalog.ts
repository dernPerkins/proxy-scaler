// Shared pieces of the MPC Fill art catalog UI (MpcCatalogPrompt, the
// launch-time "a newer catalog is available" dialog, and MpcCatalogPanel
// in the Decklist sidebar): the update job the app is watching, the
// prompt's per-server memory, and what to refresh once an update lands.
//
// The catalog lives on the generation server (mpc_catalog.py). Like the
// card database, an import holds the app with a modal until it finishes
// (MpcCatalogPrompt shows it, whoever started the import).
import { useSyncExternalStore } from "react";
import { useQuery, type QueryClient } from "@tanstack/react-query";
import { generationApi } from "./api/generation";
import { projectApi } from "./api/project";
import { serverSupportsMpcFill, useServerReadiness, useServerVersion } from "./config";
import { useConnection } from "./connection";

/** The prompt asks a server to check the published manifest at most this
 *  often (the server caches the manifest for hours on top). */
export const CHECK_INTERVAL_MS = 24 * 60 * 60 * 1000;

// --- The update job being watched -----------------------------------------

let activeJobId: string | null = null;
const listeners = new Set<() => void>();

function subscribe(listener: () => void): () => void {
  listeners.add(listener);
  return () => listeners.delete(listener);
}

function getJobId(): string | null {
  return activeJobId;
}

export function setMpcCatalogJobId(jobId: string | null): void {
  if (activeJobId === jobId) return;
  activeJobId = jobId;
  for (const listener of listeners) listener();
}

export function useMpcCatalogJobId(): string | null {
  return useSyncExternalStore(subscribe, getJobId);
}

/** After an update: the status, and every MPC Fill search and listing, so
 *  pickers already open re-run against the new catalog. */
export function refreshAfterCatalogUpdate(queryClient: QueryClient): void {
  void queryClient.invalidateQueries({ queryKey: ["mpc-catalog-status"] });
  void queryClient.invalidateQueries({
    predicate: (query) => {
      const head = query.queryKey[0];
      return (
        typeof head === "string" &&
        head.startsWith("mpc-") &&
        head !== "mpc-fill-language" &&
        !head.startsWith("mpc-catalog")
      );
    },
  });
}

/** Is the MPC Fill catalog downloaded on the connected server? MPC Fill
 *  art search works from it alone (users never call mpcfill.com), so every
 *  MPC Fill feature asks this first and offers the download when not.
 *  Shares the status query with MpcCatalogPanel and the prompt, which an
 *  import or delete refreshes (refreshAfterCatalogUpdate). */
export function useMpcCatalogReady(): { ready: boolean; importing: boolean; known: boolean } {
  const connection = useConnection();
  const readiness = useServerReadiness();
  const supported = serverSupportsMpcFill(useServerVersion());
  const serverUnavailable =
    connection.mode === "remote" ? !connection.remoteHealthy : readiness.status !== "ready";
  const jobId = useMpcCatalogJobId();
  const status = useQuery({
    queryKey: ["mpc-catalog-status"],
    queryFn: () => generationApi.mpcCatalogStatus(),
    enabled: !serverUnavailable && supported,
    refetchInterval: 60_000,
    retry: false,
  });
  return {
    ready: status.data?.local != null,
    importing: (status.data?.import_running ?? false) || jobId != null,
    known: status.data != null,
  };
}

// --- The prompt's per-server memory (app_settings, see project_store.rs) --

export interface ServerPromptState {
  /** When this server was last asked to check (ms since epoch). */
  checkedAt?: number;
  /** A published version the user chose to Skip; asked again only for a
   *  newer one. */
  skippedVersion?: string;
}

export type PromptState = Record<string, ServerPromptState>;

/** Which server a remembered answer belongs to: the catalog is per server,
 *  so skipping it on one machine says nothing about another. */
export function serverKey(mode: "local" | "remote" | null, host: string, port: number): string {
  return mode === "remote" ? `${host}:${port}` : "local";
}

export async function loadPromptState(): Promise<PromptState> {
  try {
    const json = await projectApi.getMpcCatalogPromptState();
    const parsed: unknown = json ? JSON.parse(json) : {};
    return parsed && typeof parsed === "object" ? (parsed as PromptState) : {};
  } catch {
    return {};
  }
}

export async function updatePromptState(
  key: string,
  change: ServerPromptState,
): Promise<void> {
  const state = await loadPromptState();
  state[key] = { ...state[key], ...change };
  await projectApi.setMpcCatalogPromptState(JSON.stringify(state));
}

export function formatMb(bytes: number): string {
  const mb = bytes / 1_000_000;
  return mb < 1 ? "under 1 MB" : `${mb.toFixed(mb < 10 ? 1 : 0)} MB`;
}
