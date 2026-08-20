// The generation server's HTTP API — Scryfall resolution, the
// download+upscale pipeline, the task queue, the gallery of completed
// images, and PDF assembly. Everything here is scoped by an opaque
// project_tag string the client mints per local project (see
// project.ts), never a server-side project id — see ARCHITECTURE.md.
import { getApiBaseUrl, waitForServerReady } from "../config";
import type {
  CardDataset,
  CardDbStatus,
  CardImportStatus,
  CardVariantsResult,
  DeckEntryIn,
  Device,
  GalleryItem,
  GenerateRequest,
  GenerateResult,
  GenPathsInfo,
  ModelOption,
  PdfJobRequest,
  PdfJobStarted,
  PdfJobStatus,
  PdfLayoutRequest,
  PdfPagePreview,
  PdfPreview,
  RegenerateGalleryItemRequest,
  ResolveResult,
  Task,
  WorkerStatus,
} from "./types";

export class ApiError extends Error {
  status: number;
  constructor(status: number, message: string) {
    super(message);
    this.status = status;
  }
}

async function request<T>(path: string, init?: RequestInit): Promise<T> {
  await waitForServerReady();
  const resp = await fetch(`${getApiBaseUrl()}${path}`, {
    headers: { "Content-Type": "application/json" },
    ...init,
  });
  if (!resp.ok) {
    const detail = await resp.text();
    throw new ApiError(resp.status, detail || resp.statusText);
  }
  if (resp.status === 204) {
    return undefined as T;
  }
  return (await resp.json()) as T;
}

export const generationApi = {
  // The frontend must read this list, never hardcode it — a hand-typed
  // copy previously shipped here silently dropped two real models.
  // UpscaleModel (Python) is the only source of truth.
  listModels: () => request<ModelOption[]>("/api/models"),

  // Whether the connected server has a real GPU — see connection.tsx's
  // fire-and-forget probe, which is the only caller. Never call this
  // before the server's readiness gate has already resolved (request()
  // waits on it, but the whole point of resolve_device() living outside
  // /api/health on the Python side is that nothing should route through
  // this until the server is already known to be up).
  getDevice: () => request<Device>("/api/device"),

  // The connected server's release version, for the drift warning
  // (connection.tsx's probe -> VersionMismatchToast). Older servers 404
  // here — the probe swallows that as "unknown", never as a mismatch.
  getServerVersion: () => request<{ version: string }>("/api/version"),

  // Where the fixed generation directories actually live on the server's
  // machine — the client only ever knows the relative names it sends
  // (DEFAULT_GEN_PATHS); the server resolves them against its own cwd.
  getPaths: () => request<GenPathsInfo>("/api/paths"),

  // strict_lang: the resolve-gated import's "strictly literal" language
  // mode — each entry's lang is a demand, not a preference (see
  // card_lookup.CardResolver). Omitted/false keeps the relaxed ladder.
  resolve: (entries: DeckEntryIn[], opts?: { strict_lang?: boolean }) =>
    request<ResolveResult>("/api/resolve", {
      method: "POST",
      body: JSON.stringify({ entries, strict_lang: opts?.strict_lang ?? false }),
    }),

  generate: (body: GenerateRequest) =>
    request<GenerateResult>("/api/generate", {
      method: "POST",
      body: JSON.stringify(body),
    }),
  regenerateGalleryItem: (galleryItemId: number, body: RegenerateGalleryItemRequest) =>
    request<GenerateResult>(`/api/gallery/${galleryItemId}/regenerate`, {
      method: "POST",
      body: JSON.stringify(body),
    }),

  listTasks: (params?: { project_tag?: string; status?: string }) => {
    const search = new URLSearchParams();
    if (params?.project_tag) search.set("project_tag", params.project_tag);
    if (params?.status) search.set("status", params.status);
    const qs = search.toString();
    return request<Task[]>(`/api/tasks${qs ? `?${qs}` : ""}`);
  },
  getTask: (id: number) => request<Task>(`/api/tasks/${id}`),
  cancelTask: (id: number) =>
    request<{ canceled: boolean }>(`/api/tasks/${id}/cancel`, { method: "POST" }),
  // includeRunning also cancels orphaned 'running' rows — the server only
  // allows it while the worker is held (409 otherwise), i.e. from
  // ResumeTasksPrompt's startup flow.
  cancelAllTasks: (includeRunning = false) =>
    request<{ canceled: number }>(
      `/api/tasks/cancel-all${includeRunning ? "?include_running=true" : ""}`,
      { method: "POST" },
    ),
  retryTask: (id: number) =>
    request<{ retried: boolean }>(`/api/tasks/${id}/retry`, { method: "POST" }),
  retryAllTasks: (projectTag: string, model: string, dpis: number[]) => {
    const search = new URLSearchParams({ project_tag: projectTag, model });
    dpis.forEach((d) => search.append("dpi", String(d)));
    return request<{ retried: number }>(`/api/tasks/retry-all?${search}`, { method: "POST" });
  },
  workerStatus: () => request<WorkerStatus>("/api/worker/status"),
  // Releases a worker the supervisor started held (--hold-worker).
  // Idempotent; released:false just means there was no hold to clear.
  releaseWorker: () =>
    request<{ released: boolean }>("/api/worker/release", { method: "POST" }),

  listGallery: (projectTag: string) =>
    request<GalleryItem[]>(`/api/gallery?${new URLSearchParams({ project_tag: projectTag })}`),
  // Reconciles this project's gallery with the server's disk, both ways:
  // prunes rows/done-tasks whose files are gone (stale green badges), then
  // registers already-existing images for these cards (other projects'
  // rows + an output-dir filename scan), so the deck list reflects
  // reality right after an import/load without a Generate request.
  adoptGallery: (projectTag: string, entries: DeckEntryIn[], outputDir: string) =>
    request<{ adopted: number; pruned: number }>("/api/gallery/adopt", {
      method: "POST",
      body: JSON.stringify({ project_tag: projectTag, entries, output_dir: outputDir }),
    }),
  imageUrl: (galleryItemId: number, variant: "full" | "original") =>
    `${getApiBaseUrl()}/api/gallery/${galleryItemId}/${variant}`,

  pdfPreview: (body: PdfLayoutRequest) =>
    request<PdfPreview>("/api/pdf/preview", {
      method: "POST",
      body: JSON.stringify(body),
    }),
  pdfPagePreview: (body: PdfLayoutRequest) =>
    request<PdfPagePreview>("/api/pdf/preview/page", {
      method: "POST",
      body: JSON.stringify(body),
    }),
  // PDF generation is a POST that streams the finished file back. These
  // expose the URL rather than fetching it, so downloads can be run
  // entirely in Rust (see download.ts::runDownload) — the finished PDF is
  // exactly the kind of large payload that must not transit the webview.
  pdfUrl: () => `${getApiBaseUrl()}/api/pdf`,

  // --- Render jobs ---
  // Rendering costs ~0.7s per unique card image server-side, so the UI
  // starts a job and polls it rather than blocking on one long POST with
  // nothing to show. The finished PDF is fetched from a plain GET, which
  // is what lets Rust download it without the bytes entering the webview.
  startPdfJob: (body: PdfJobRequest) =>
    request<PdfJobStarted>("/api/pdf/jobs", {
      method: "POST",
      body: JSON.stringify(body),
    }),
  pdfJobStatus: (jobId: string) => request<PdfJobStatus>(`/api/pdf/jobs/${jobId}`),
  cancelPdfJob: (jobId: string) =>
    request<void>(`/api/pdf/jobs/${jobId}/cancel`, { method: "POST" }),
  pdfJobResultUrl: (jobId: string) => `${getApiBaseUrl()}/api/pdf/jobs/${jobId}/result`,

  clearGeneratedData: (outputDir: string, cacheDir: string, projectTag?: string) =>
    request<{ notes: string[] }>("/api/generated-data/clear", {
      method: "POST",
      body: JSON.stringify({
        output_dir: outputDir,
        cache_dir: cacheDir,
        project_tag: projectTag,
      }),
    }),

  // "This session was thrown away": cancels the tag's pending tasks and
  // drops its generation records. Deletes no files — clearGeneratedData
  // is the file-deleting one; misc.py::discard_tag says why these can't
  // be the same call.
  discardTag: (projectTag: string) =>
    request<{ canceled: number }>(`/api/tags/${encodeURIComponent(projectTag)}/discard`, {
      method: "POST",
    }),

  // --- Card corpus (routers/cards.py) ---
  // The server's locally-imported Scryfall bulk data. Status feeds the
  // sidebar's staleness hint; import runs as a background job the client
  // polls (same idiom as the PDF render jobs above); variants feeds the
  // change-printing picker. All of it is per-server: in Remote mode the
  // corpus lives (and must be imported) on the connected machine.
  cardDbStatus: () => request<CardDbStatus>("/api/cards/status"),
  startCardImport: (dataset: CardDataset) =>
    request<{ job_id: string }>("/api/cards/import", {
      method: "POST",
      body: JSON.stringify({ dataset }),
    }),
  cardImportStatus: (jobId: string) =>
    request<CardImportStatus>(`/api/cards/import/${jobId}`),
  cancelCardImport: (jobId: string) =>
    request<void>(`/api/cards/import/${jobId}/cancel`, { method: "POST" }),
  cardLanguages: () => request<{ languages: string[] }>("/api/cards/languages"),
  cardVariants: (params: {
    scryfall_id?: string | null;
    set_code?: string | null;
    collector_number?: string | null;
    name?: string | null;
    include_digital?: boolean;
  }) => {
    const search = new URLSearchParams();
    if (params.scryfall_id) search.set("scryfall_id", params.scryfall_id);
    if (params.set_code) search.set("set_code", params.set_code);
    if (params.collector_number) search.set("collector_number", params.collector_number);
    if (params.name) search.set("name", params.name);
    if (params.include_digital) search.set("include_digital", "true");
    return request<CardVariantsResult>(`/api/cards/variants?${search}`);
  },

  health: () => request<{ status: string }>("/api/health"),
};
