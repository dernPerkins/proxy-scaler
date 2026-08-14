// The generation server's HTTP API — Scryfall resolution, the
// download+upscale pipeline, the task queue, the gallery of completed
// images, and PDF assembly. Everything here is scoped by an opaque
// project_tag string the client mints per local project (see
// project.ts), never a server-side project id — see ARCHITECTURE.md.
import { getApiBaseUrl, waitForServerReady } from "../config";
import type {
  DeckEntryIn,
  Device,
  GalleryItem,
  GenerateRequest,
  GenerateResult,
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

  resolve: (entries: DeckEntryIn[]) =>
    request<ResolveResult>("/api/resolve", {
      method: "POST",
      body: JSON.stringify({ entries }),
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
  cancelAllTasks: () =>
    request<{ canceled: number }>("/api/tasks/cancel-all", { method: "POST" }),
  retryTask: (id: number) =>
    request<{ retried: boolean }>(`/api/tasks/${id}/retry`, { method: "POST" }),
  retryAllTasks: (projectTag: string, model: string, dpis: number[]) => {
    const search = new URLSearchParams({ project_tag: projectTag, model });
    dpis.forEach((d) => search.append("dpi", String(d)));
    return request<{ retried: number }>(`/api/tasks/retry-all?${search}`, { method: "POST" });
  },
  workerStatus: () => request<WorkerStatus>("/api/worker/status"),

  listGallery: (projectTag: string) =>
    request<GalleryItem[]>(`/api/gallery?${new URLSearchParams({ project_tag: projectTag })}`),
  // Registers already-existing images for these cards into this project's
  // gallery (other projects' rows + an output-dir filename scan), so
  // already-generated cards show as done right after an import instead of
  // waiting for a Generate request.
  adoptGallery: (projectTag: string, entries: DeckEntryIn[], outputDir: string) =>
    request<{ adopted: number }>("/api/gallery/adopt", {
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

  health: () => request<{ status: string }>("/api/health"),
};
