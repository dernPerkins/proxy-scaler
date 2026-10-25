// The generation server's HTTP API — Scryfall resolution, the
// download+upscale pipeline, the task queue, the gallery of completed
// images, and PDF assembly. Everything here is scoped by an opaque
// project_tag string the client mints per local project (see
// project.ts), never a server-side project id — see ARCHITECTURE.md.
import { getApiBaseUrl, waitForServerReady } from "../config";
import { registerShortLabels } from "../constants";
import type {
  MpcCatalogCheck,
  MpcCatalogImportStatus,
  MpcCatalogStatus,
  CardDataset,
  CardDbStatus,
  CardImportStatus,
  CardVariantsResult,
  DeckEntryIn,
  Device,
  DownloadOriginalsRequest,
  ExportZipPreview,
  ExportZipRequest,
  ExportZipJobStarted,
  ExportZipJobStatus,
  GalleryItem,
  GalleryStatusResult,
  GenerateRequest,
  GenerateResult,
  GenPathsInfo,
  ImageOrigin,
  LibraryStatus,
  LibraryUpscaleRequest,
  ModelOption,
  PdfJobRequest,
  PdfJobStarted,
  PdfJobStatus,
  PdfLayoutRequest,
  PdfPagePreview,
  PdfPreview,
  RefetchOriginalRequest,
  RegenerateGalleryItemRequest,
  MpcBestResult,
  MpcCard,
  MpcSearchParams,
  MpcSearchResult,
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
  if (!resp.ok) await throwApiError(resp);
  if (resp.status === 204) {
    return undefined as T;
  }
  return (await resp.json()) as T;
}

async function throwApiError(resp: Response): Promise<never> {
  const body = await resp.text();
  // FastAPI puts the human-readable message in `detail` — surfacing the
  // raw body instead printed `{"detail":"Upload the back image..."}`
  // verbatim into the UI, JSON braces and all. A 422's `detail` is a
  // list of validation objects rather than a string, so only a string
  // is unwrapped; anything else falls back to the body text.
  let detail = body;
  try {
    const parsed = JSON.parse(body);
    if (typeof parsed?.detail === "string") detail = parsed.detail;
    else if (Array.isArray(parsed?.detail) && parsed.detail.length > 0) {
      // A request the server's schema refused — almost always a server
      // older than this app. One readable line, not the raw list.
      const first = parsed.detail[0];
      const where = Array.isArray(first?.loc) ? first.loc.slice(1).join(".") : "";
      detail =
        "The generation server didn't accept this request — it may be older than this app " +
        `(${parsed.detail.length} problem${parsed.detail.length === 1 ? "" : "s"}; first: ` +
        `${where ? `${where}: ` : ""}${first?.msg ?? "invalid"}).`;
    }
  } catch {
    // Not JSON (a proxy error page, an empty body) — use it as-is.
  }
  throw new ApiError(resp.status, detail || resp.statusText);
}

export const generationApi = {
  // The frontend must read this list, never hardcode it — a hand-typed
  // copy previously shipped here silently dropped two real models.
  // UpscaleModel (Python) is the only source of truth.
  listModels: async () => {
    const models = await request<ModelOption[]>("/api/models");
    registerShortLabels(models);
    return models;
  },

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
  // Download-only batch — fetch and cache the ~300 DPI Scryfall originals
  // with no upscaling. A separate route on the server so an old one 404s
  // loudly instead of silently enqueueing upscale work.
  downloadOriginals: (body: DownloadOriginalsRequest) =>
    request<GenerateResult>("/api/generate/downloads", {
      method: "POST",
      body: JSON.stringify(body),
    }),
  // Upscale one library image (Customs tab, later Backs) with no project
  // involved — see LibraryUpscaleRequest.
  upscaleLibraryImage: (body: LibraryUpscaleRequest) =>
    request<GenerateResult>("/api/library/upscale", {
      method: "POST",
      body: JSON.stringify(body),
    }),
  // Status by identity, across every project and the library tag.
  libraryStatus: (kind: "custom" | "back", contentHash: string) =>
    request<LibraryStatus>(`/api/library/${kind}/${contentHash}/status`),
  regenerateGalleryItem: (galleryItemId: number, body: RegenerateGalleryItemRequest) =>
    request<GenerateResult>(`/api/gallery/${galleryItemId}/regenerate`, {
      method: "POST",
      body: JSON.stringify(body),
    }),
  // Re-download one face's Scryfall original, overwriting the cached copy
  // — works from any of the face's gallery item ids (upscale or download
  // row), see gallery.py::refetch_original.
  refetchOriginal: (galleryItemId: number, body: RefetchOriginalRequest) =>
    request<GenerateResult>(`/api/gallery/${galleryItemId}/refetch`, {
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
  // Acknowledges the pending GPU→CPU fallback notification (called by
  // CpuFallbackPrompt whichever button is chosen). Idempotent.
  ackCpuFallback: () =>
    request<{ cleared: boolean }>("/api/worker/cpu-fallback/ack", { method: "POST" }),

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
  // Which of these printings already have generated images at this model/
  // these DPIs — answered from the server's registry alone (cross-project,
  // no filesystem stats). Feeds the printing picker's coverage check marks.
  // With model + dpis: which printings have that model at those DPIs (the
  // grid's ✓). Without: every generated variant (the preview's badges) —
  // servers older than 0.5.0 require both.
  galleryStatus: (scryfallIds: string[], model?: string, dpis?: number[]) =>
    request<GalleryStatusResult>("/api/gallery/status", {
      method: "POST",
      body: JSON.stringify({ scryfall_ids: scryfallIds, model, dpis }),
    }),
  // "thumb" is a downscaled WebP for gallery tiles; "full" is the real
  // file (downloads, compare). Feeding /full into <img> tiles meant the
  // webview decoded and composited a ~50 MB bitmap per 1200 DPI card.
  imageUrl: (galleryItemId: number, variant: "full" | "original" | "thumb") =>
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

  // --- ZIP export ---
  exportZipPreview: (body: ExportZipRequest) =>
    request<ExportZipPreview>("/api/export/zip/preview", {
      method: "POST",
      body: JSON.stringify(body),
    }),
  // The synchronous route: URL rather than a fetch, same reason as pdfUrl
  // — the archive is exactly the kind of large payload that must not
  // transit the webview, so Rust POSTs the body and streams the response
  // to disk. Only used against servers without the job routes below.
  exportZipUrl: () => `${getApiBaseUrl()}/api/export/zip`,
  // Export jobs — the PDF job quartet's twin. Adding bleed or converting
  // to JPG re-renders every image (~1s each at 1200 DPI), so the UI
  // starts a job and polls it; a plain PNG export finishes at disk speed
  // and is simply "done" on the first poll. One client code path.
  startExportZipJob: (body: ExportZipRequest) =>
    request<ExportZipJobStarted>("/api/export/zip/jobs", {
      method: "POST",
      body: JSON.stringify(body),
    }),
  exportZipJobStatus: (jobId: string) =>
    request<ExportZipJobStatus>(`/api/export/zip/jobs/${jobId}`),
  cancelExportZipJob: (jobId: string) =>
    request<void>(`/api/export/zip/jobs/${jobId}/cancel`, { method: "POST" }),
  exportZipJobResultUrl: (jobId: string) =>
    `${getApiBaseUrl()}/api/export/zip/jobs/${jobId}/result`,

  // The cutter's cut file (SVG). Pure geometry server-side, no render
  // phase — Rust POSTs the layout body and streams the file to disk,
  // like the ZIP.
  pdfCutFileUrl: () => `${getApiBaseUrl()}/api/pdf/cut-file`,

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
  // Remove the imported corpus from the server entirely; 409 while an
  // import is running.
  deleteCardDb: () => request<void>("/api/cards/database", { method: "DELETE" }),
  // The MPC Fill art catalog (mpc_catalog.py), per-server like the card
  // corpus. status is local-only; check reads the published manifest
  // (cached on the server unless refresh) and 503s when it's unreachable.
  mpcCatalogStatus: () => request<MpcCatalogStatus>("/api/mpccatalog/status"),
  checkMpcCatalog: (refresh = false) =>
    request<MpcCatalogCheck>("/api/mpccatalog/check", {
      method: "POST",
      body: JSON.stringify({ refresh }),
    }),
  startMpcCatalogImport: () =>
    request<{ job_id: string }>("/api/mpccatalog/import", { method: "POST" }),
  mpcCatalogImportStatus: (jobId: string) =>
    request<MpcCatalogImportStatus>(`/api/mpccatalog/import/${jobId}`),
  cancelMpcCatalogImport: (jobId: string) =>
    request<void>(`/api/mpccatalog/import/${jobId}/cancel`, { method: "POST" }),
  deleteMpcCatalog: () => request<void>("/api/mpccatalog", { method: "DELETE" }),
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
  /** Remove a Custom Image's stored copy from this server (the client's
   *  library copy is canonical). */
  deleteServerCustom: (contentHash: string) =>
    request<{ removed: number }>(`/api/customs/${contentHash}`, { method: "DELETE" }),
  // An <img> src, not a fetch: one face of one printing as a JPEG, served
  // from the server's thumbnail cache or fetched through `via`. `version`
  // (CardVariant.image_version) makes the URL name exactly one image, which
  // the server then lets the webview cache forever. Only for servers
  // serverSupportsPrintingImages() accepts — older ones 404.
  cardThumbUrl: (
    scryfallId: string,
    face: number,
    via: ImageOrigin,
    version?: string | null,
  ) => {
    const search = new URLSearchParams({ face: String(face), via });
    if (version) search.set("v", version);
    return `${getApiBaseUrl()}/api/cards/${encodeURIComponent(scryfallId)}/thumb?${search}`;
  },
  // The full-size PNG Generate would download (~1.3 MB), for the grid's
  // preview pane. Loading it puts it in the server's originals cache, so
  // a later Generate or Download of that printing skips the fetch. Same
  // server floor as cardThumbUrl.
  cardOriginalUrl: (scryfallId: string, face: number, via: ImageOrigin) => {
    const search = new URLSearchParams({ face: String(face), via });
    return `${getApiBaseUrl()}/api/cards/${encodeURIComponent(scryfallId)}/original?${search}`;
  },

  // --- MPC Fill ------------------------------------------------------
  // Everything goes through the generation server: search from its
  // downloaded MPC Fill catalog (409 until there is one; it never calls
  // mpcfill.com) and images from Google Drive. Only for servers
  // serverSupportsMpcFill() accepts — older ones 404.

  mpcSearch: (params: MpcSearchParams) => {
    const search = new URLSearchParams();
    for (const [key, value] of Object.entries(params)) {
      if (value === undefined || value === null || value === "") continue;
      search.set(key, String(value));
    }
    return request<MpcSearchResult>(`/api/mpcfill/search?${search}`);
  },
  // MPC Fill's top match for every face of up to 300 cards, in one search.
  mpcBest: (body: { scryfall_ids: string[]; lang?: string | null; min_dpi?: number }) =>
    request<MpcBestResult>("/api/mpcfill/best", {
      method: "POST",
      body: JSON.stringify(body),
    }),
  mpcCards: (ids: string[]) =>
    request<{ cards: MpcCard[] }>("/api/mpcfill/cards", {
      method: "POST",
      body: JSON.stringify({ ids }),
    }),
  // An <img> src: a 400 px ("small") or 800 px ("large") tall JPEG preview,
  // from Google via the server's disk cache.
  mpcThumbUrl: (driveId: string, size: "small" | "large" = "small") =>
    `${getApiBaseUrl()}/api/mpcfill/thumb/${encodeURIComponent(driveId)}?size=${size}`,

  // A set's symbol as a monochrome SVG, used as a CSS mask (SetIcon.tsx).
  // Only for servers serverSupportsSetIcons() accepts — older ones 404.
  setIconUrl: (setCode: string) =>
    `${getApiBaseUrl()}/api/sets/${encodeURIComponent(setCode.toLowerCase())}/icon`,
  // scryfall_id -> face count for a deck's double-faced printings (single-
  // faced and unknown ids are absent). An older server 404s: rows then
  // learn their faces from their images alone.
  cardFaceCounts: (ids: string[]) =>
    request<{ face_counts: Record<string, number> }>("/api/cards/face-counts", {
      method: "POST",
      body: JSON.stringify({ ids }),
    }),
  // scryfall_id -> rarity for a deck's pinned printings; ids the corpus
  // lacks (or holds no rarity for) are absent. Same server floor.
  cardRarities: (ids: string[]) =>
    request<{ rarities: Record<string, string>; flavor_names?: Record<string, string> }>(
      "/api/cards/rarities",
      {
        method: "POST",
        body: JSON.stringify({ ids }),
      },
    ),

  health: () => request<{ status: string }>("/api/health"),

  // --- Back Images ---------------------------------------------------
  //
  // The server holds a content-addressed cache of Back Image bytes; a
  // back is upscaled only from the Backs tab (upscaleLibraryImage above,
  // see proxy_scaler/backs.py
  // for why that asymmetry with card art is deliberate). The library
  // itself is client-side, see api/project.ts. Uploading is Rust's job
  // (projectApi.syncBackImage): multi-MB bodies never cross the webview.
  /** Remove a back from the connected server entirely. The client's
   *  library copy is canonical and untouched. */
  deleteBackImageFromServer: (contentHash: string) =>
    request<{ removed: number }>(`/api/backs/${contentHash}`, {
      method: "DELETE",
    }),
};

