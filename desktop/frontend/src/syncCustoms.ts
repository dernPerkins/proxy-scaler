// Pushing Custom Image bytes to the generation server, just in time.
//
// This is the client half of "images stay on this machine until the server
// actually needs them". Nothing uploads on add; instead every action that
// requires the server to *see* the art — generating, downloading sources,
// rendering a PDF, building a ZIP — calls this first.
//
// Cheap to call unconditionally. The Rust side GETs /api/customs/{hash}
// and only POSTs on a miss, so an image the server already holds costs one
// small request. That also makes switching servers self-healing: the new
// host misses, and the next generate or export fills it.
import { generationApi } from "./api/generation";
import { projectApi } from "./api/project";
import type { CardRow } from "./api/project";
import { getApiBaseUrl, getConnectionMode, serverSupportsCustomImages } from "./config";
import { DEFAULT_GEN_PATHS } from "./constants";
import { cardToEntry } from "./deckEntries";
import { runCustomUploads, type UploadItem } from "./uploadProgress";

// Snapshot of the current project's cards + tag, maintained by
// ProjectContext (one effect) and read by connection.tsx::switchTo —
// which mounts ABOVE the project context (main.tsx) and so cannot call
// useProject(). Lives here rather than in connection.tsx because this is
// the custom-cards domain module, and the import direction
// (connection.tsx → syncCustoms.ts) stays acyclic.
let projectSnapshot: { cards: CardRow[]; projectTag: string | null } = {
  cards: [],
  projectTag: null,
};

export function setProjectSnapshot(snapshot: {
  cards: CardRow[];
  projectTag: string | null;
}): void {
  projectSnapshot = snapshot;
}

export function getProjectSnapshot(): { cards: CardRow[]; projectTag: string | null } {
  return projectSnapshot;
}

/** One entry per distinct custom image among `cards`, keyed by library id
 *  with the card name as its dialog label. */
function distinctCustoms(cards: CardRow[]): Array<{ id: number; hash: string | null; label: string }> {
  const seen = new Map<number, { id: number; hash: string | null; label: string }>();
  for (const c of cards) {
    if (c.custom_image_id != null && !seen.has(c.custom_image_id)) {
      seen.set(c.custom_image_id, {
        id: c.custom_image_id,
        hash: c.custom_hash ?? null,
        label: c.name,
      });
    }
  }
  return [...seen.values()];
}

/**
 * Which of these Custom Images the server at `baseUrl` does NOT hold —
 * the honest denominator for the upload-progress dialog, and the
 * substance of the switch-to-remote check.
 *
 * Same content-addressed probe the Rust sync itself performs before
 * POSTing: GET /api/customs/{sha256-of-the-bytes} answers {present}.
 * Parallel is fine here (tiny GETs), unlike the uploads. Any failure —
 * network error, non-200, a row with no hash — counts as a miss: the
 * upload path re-probes each one in Rust and surfaces the real error if
 * the server is genuinely gone, while a false "present" would silently
 * skip a needed upload.
 */
export async function probeMissingCustoms(
  cards: CardRow[],
  baseUrl: string,
): Promise<UploadItem[]> {
  const customs = distinctCustoms(cards);
  const missing = await Promise.all(
    customs.map(async ({ hash }) => {
      if (hash == null) return true;
      try {
        const resp = await fetch(`${baseUrl}/api/customs/${hash}`);
        if (!resp.ok) return true;
        return !((await resp.json()) as { present: boolean }).present;
      } catch {
        return true;
      }
    }),
  );
  return customs.filter((_, i) => missing[i]).map(({ id, label }) => ({ id, label }));
}

export interface SyncCustomOptions {
  /** Server to sync against. Default: the connected server. The
   *  switch-to-remote flow passes the TARGET host here, before the
   *  switch commits. */
  baseUrl?: string;
  /** Whether that server counts as remote — decides the blocking
   *  progress dialog. Default: the connected mode. Explicit true from
   *  the switch flow, where the app is still in local mode while
   *  uploading to the remote target. */
  remote?: boolean;
  /** Never show the dialog, regardless of mode. Escape hatch; no
   *  call site uses it today. */
  silent?: boolean;
}

/**
 * Make sure the server holds the art for every Custom Image card in
 * `cards`.
 *
 * Sequential, not Promise.all: each miss is a multi-MB upload, and firing
 * thirty of them at once at a server that is about to start doing GPU work
 * is how a bulk generate turns into a timeout. Deduplicated first, since
 * the same image can legitimately appear as several cards.
 *
 * Against a REMOTE server this holds all interaction behind the
 * upload-progress modal while bytes actually move (see uploadProgress.ts)
 * — first probing which images the server is missing, so the count is
 * honest and the zero-miss common case shows nothing at all. Local mode
 * skips the dialog entirely: the same sync runs, but a loopback file copy
 * is near-instant and a modal for it would be noise. May throw
 * UploadCanceled if the user cancels mid-run; callers treat that like
 * DownloadCanceled — a normal outcome, not an error to display.
 */
export async function syncCustomImages(
  cards: CardRow[],
  serverVersion?: string | null,
  opts?: SyncCustomOptions,
): Promise<void> {
  const customs = distinctCustoms(cards);
  if (customs.length === 0) return;
  // Checked here rather than at each call site because this is the one
  // place every custom-art action funnels through, and the failure it
  // guards is silent: /api/customs 404s, and the generate or export that
  // follows sends a custom_hash an older server's Pydantic model drops —
  // leaving an entry with no printing, which matches nothing, or matches
  // whatever real card happens to share the file's name.
  if (serverVersion !== undefined && !serverSupportsCustomImages(serverVersion)) {
    throw new Error(
      "This project has custom card images, which need a newer generation " +
        `server${serverVersion ? ` (it reports v${serverVersion})` : ""}. ` +
        "Update the server, or remove the custom cards.",
    );
  }
  const baseUrl = opts?.baseUrl ?? getApiBaseUrl();
  const showDialog = !opts?.silent && (opts?.remote ?? getConnectionMode() === "remote");
  if (!showDialog) {
    for (const { id } of customs) {
      await projectApi.syncCustomImage(id, baseUrl);
    }
    return;
  }
  const missingItems = await probeMissingCustoms(cards, baseUrl);
  if (missingItems.length === 0) return;
  await runCustomUploads(missingItems, baseUrl);
}

/** Whether any of these cards is a Custom Image — used to skip the sync
 *  entirely (and to decide whether the server-version floor matters) for
 *  the common all-Scryfall project. */
export function hasCustomCards(cards: CardRow[]): boolean {
  return cards.some((c) => c.custom_image_id != null);
}

/**
 * Make every Custom Image card in `cards` printable on the connected
 * server: sync the bytes, then register each one as a custom_source
 * gallery variant at its measured native DPI (via the downloads endpoint
 * — see services/generation.py::enqueue_download_entries).
 *
 * This is what makes "upload it and it's ready to print" true without a
 * separate Download/Generate click. Called after every add (drop on the
 * Decklist tab, Add to project on the Customs tab) and again from the
 * PDF/ZIP export paths — deliberately with the project's WHOLE card list
 * each time, not just the newly added rows: registration is idempotent
 * server-side (content-addressed, dedup on the variant key), so each call
 * also heals customs that were added earlier while no server was
 * reachable.
 *
 * Returns the ids of any registration tasks the server queued (an upload
 * it has never cached needs a worker copy); already-registered customs
 * queue nothing. Callers that are about to render from the registry
 * (PDF/ZIP) should waitForTasks() on the result.
 */
export async function registerCustomCards(
  cards: CardRow[],
  projectTag: string | null,
  serverVersion?: string | null,
  opts?: SyncCustomOptions,
): Promise<number[]> {
  const customs = cards.filter((c) => c.custom_image_id != null);
  if (customs.length === 0 || projectTag == null) return [];
  await syncCustomImages(customs, serverVersion, opts);
  const result = await generationApi.downloadOriginals({
    project_tag: projectTag,
    entries: customs.map(cardToEntry),
    output_dir: DEFAULT_GEN_PATHS.output_dir,
    cache_dir: DEFAULT_GEN_PATHS.cache_dir,
    weights_dir: DEFAULT_GEN_PATHS.weights_dir,
  });
  return result.task_ids;
}

/**
 * Poll until every task id reaches a terminal state (done/failed/
 * canceled), or the timeout passes. Registration tasks are a file copy
 * plus a thumbnail, so in practice this resolves in well under a second
 * per image; the timeout is a safety valve, not an expected path — on
 * expiry we return rather than throw, and the render that follows
 * reports anything genuinely missing.
 */
export async function waitForTasks(taskIds: number[], timeoutMs = 30_000): Promise<void> {
  const deadline = Date.now() + timeoutMs;
  let pending = [...taskIds];
  while (pending.length > 0 && Date.now() < deadline) {
    const tasks = await Promise.all(pending.map((id) => generationApi.getTask(id)));
    pending = tasks
      .filter((t) => t.status === "pending" || t.status === "running")
      .map((t) => t.id);
    if (pending.length === 0) return;
    await new Promise((resolve) => setTimeout(resolve, 400));
  }
}
