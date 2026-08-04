// The generation server's HTTP API — Scryfall resolution, the
// download+upscale pipeline, the task queue, the gallery of completed
// images, and PDF assembly. Everything here is scoped by an opaque
// project_tag string the client mints per local project (see
// project.ts), never a server-side project id — see ARCHITECTURE.md.
import { getApiBaseUrl, waitForServerReady } from "../config";
import type {
  DeckEntryIn,
  GalleryItem,
  GenerateRequest,
  GenerateResult,
  ModelOption,
  PdfLayoutRequest,
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

async function downloadPdf(body: PdfLayoutRequest): Promise<Blob> {
  await waitForServerReady();
  const resp = await fetch(`${getApiBaseUrl()}/api/pdf`, {
    method: "POST",
    headers: { "Content-Type": "application/json" },
    body: JSON.stringify(body),
  });
  if (!resp.ok) {
    const detail = await resp.text();
    throw new ApiError(resp.status, detail || resp.statusText);
  }
  return resp.blob();
}

export const generationApi = {
  // The frontend must read this list, never hardcode it — a hand-typed
  // copy previously shipped here silently dropped two real models.
  // UpscaleModel (Python) is the only source of truth.
  listModels: () => request<ModelOption[]>("/api/models"),

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
  workerStatus: () => request<WorkerStatus>("/api/worker/status"),

  listGallery: (projectTag: string) =>
    request<GalleryItem[]>(`/api/gallery?${new URLSearchParams({ project_tag: projectTag })}`),
  imageUrl: (galleryItemId: number, variant: "full" | "original") =>
    `${getApiBaseUrl()}/api/gallery/${galleryItemId}/${variant}`,

  pdfPreview: (body: PdfLayoutRequest) =>
    request<PdfPreview>("/api/pdf/preview", {
      method: "POST",
      body: JSON.stringify(body),
    }),
  downloadPdf,

  clearGeneratedData: (outputDir: string, cacheDir: string, projectTag?: string) =>
    request<{ notes: string[] }>("/api/generated-data/clear", {
      method: "POST",
      body: JSON.stringify({
        output_dir: outputDir,
        cache_dir: cacheDir,
        project_tag: projectTag,
      }),
    }),

  health: () => request<{ status: string }>("/api/health"),
};
