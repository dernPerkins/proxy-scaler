import { getApiBaseUrl, waitForServerReady } from "../config";
import type {
  Card,
  GenerateRequest,
  GenerateResult,
  ImportResult,
  ModelOption,
  PdfLayoutRequest,
  PdfPreview,
  ProjectDetail,
  ProjectSettings,
  ProjectSummary,
  RegenerateGalleryItemRequest,
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

async function downloadPdf(projectId: number, body: PdfLayoutRequest): Promise<Blob> {
  await waitForServerReady();
  const resp = await fetch(`${getApiBaseUrl()}/api/projects/${projectId}/pdf`, {
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

export const api = {
  // The frontend must read this list, never hardcode it — a hand-typed
  // copy previously shipped here silently dropped two real models.
  // UpscaleModel (Python) is the only source of truth.
  listModels: () => request<ModelOption[]>("/api/models"),

  listProjects: () => request<ProjectSummary[]>("/api/projects"),
  createProject: (name: string, settings?: ProjectSettings) =>
    request<ProjectSummary>("/api/projects", {
      method: "POST",
      body: JSON.stringify({ name, settings }),
    }),
  getProject: (id: number) => request<ProjectDetail>(`/api/projects/${id}`),
  updateProject: (id: number, name: string, settings: ProjectSettings) =>
    request<ProjectSummary>(`/api/projects/${id}`, {
      method: "PUT",
      body: JSON.stringify({ name, settings }),
    }),
  deleteProject: (id: number) =>
    request<void>(`/api/projects/${id}`, { method: "DELETE" }),
  clearAllProjects: () =>
    request<void>("/api/projects?confirm=true", { method: "DELETE" }),
  lastProject: () => request<{ project_id: number | null }>("/api/projects/last"),

  importDecklist: (projectId: number, text: string) =>
    request<ImportResult>(`/api/projects/${projectId}/import`, {
      method: "POST",
      body: JSON.stringify({ text }),
    }),
  listCards: (projectId: number) => request<Card[]>(`/api/projects/${projectId}/cards`),
  removeCard: (projectId: number, cardId: number) =>
    request<void>(`/api/projects/${projectId}/cards/${cardId}`, { method: "DELETE" }),

  generate: (body: GenerateRequest) =>
    request<GenerateResult>("/api/generate", {
      method: "POST",
      body: JSON.stringify(body),
    }),
  regenerateGalleryItem: (
    projectId: number,
    galleryItemId: number,
    body: RegenerateGalleryItemRequest = {},
  ) =>
    request<GenerateResult>(`/api/projects/${projectId}/regenerate/${galleryItemId}`, {
      method: "POST",
      body: JSON.stringify(body),
    }),
  listTasks: (params?: { project_id?: number; status?: string }) => {
    const search = new URLSearchParams();
    if (params?.project_id != null) search.set("project_id", String(params.project_id));
    if (params?.status) search.set("status", params.status);
    const qs = search.toString();
    return request<Task[]>(`/api/tasks${qs ? `?${qs}` : ""}`);
  },
  getTask: (id: number) => request<Task>(`/api/tasks/${id}`),
  cancelTask: (id: number) =>
    request<{ canceled: boolean }>(`/api/tasks/${id}/cancel`, { method: "POST" }),
  workerStatus: () => request<WorkerStatus>("/api/worker/status"),

  pdfPreview: (projectId: number, body: PdfLayoutRequest) =>
    request<PdfPreview>(`/api/projects/${projectId}/pdf/preview`, {
      method: "POST",
      body: JSON.stringify(body),
    }),
  downloadPdf,

  imageUrl: (projectId: number, galleryItemId: number, variant: "full" | "original") =>
    `${getApiBaseUrl()}/api/projects/${projectId}/images/${galleryItemId}/${variant}`,

  clearGeneratedData: (outputDir: string, cacheDir: string) =>
    request<{ notes: string[] }>("/api/generated-data/clear", {
      method: "POST",
      body: JSON.stringify({ output_dir: outputDir, cache_dir: cacheDir }),
    }),

  health: () => request<{ status: string }>("/api/health"),
};
