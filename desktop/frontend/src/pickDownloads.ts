// Downloading what was just picked, in the background.
//
// A pick in the change-printing modal (a Scryfall printing or MPC Fill
// art), or a decklist imported with MPC Fill art, closes the modal at once
// and fetches the full-size image behind it — so the next Generate, PDF or
// ZIP has it already, and the card's row shows it arriving. Both kinds end
// in the same place: a task in the server's queue registering the image
// for this project, whose badge the Decklist tab already shows.
//
// - A Scryfall printing is a download-only task (/api/generate/downloads),
//   exactly what the Download button queues.
// - MPC Fill art first has to reach the library (the server downloads it
//   and Rust stores the same bytes — mpcFill.importMpcArt); then it becomes
//   the face's art and is registered like any custom image. Until that
//   finishes, the face shows as downloading (usePendingFaceArt).
//
// MPC downloads run one at a time — each can be tens of MB — and survive
// the modal closing; cancelPendingFaceArt drops the ones not yet started.
import { useSyncExternalStore } from "react";
import { generationApi } from "./api/generation";
import type { CardRow } from "./api/project";
import type { CustomImage, MpcBestResult, MpcCard } from "./api/types";
import { DEFAULT_GEN_PATHS } from "./constants";
import { cardToEntry } from "./deckEntries";
import { importMpcArt } from "./mpcFill";
import { registerCustomCards, waitForTasks } from "./syncCustoms";

/**
 * Download a card's newly picked printing on the server, as a queued
 * download task (what the Download button queues). The card shows as
 * downloading from now until that task is done and the Decklist has
 * re-read the status, so its badge goes straight to "300 · Original".
 * Best-effort: Download/Generate cover it later if this fails.
 */
export function downloadPickedPrinting(
  card: CardRow,
  projectTag: string | null,
  refreshStatus: () => Promise<unknown>,
): void {
  if (projectTag == null || card.scryfall_id == null) return;
  const key = pendingPrintingKey(card.id);
  const mine: PendingFaceArt = {
    cardId: card.id,
    face: 0,
    totalFaces: 1,
    name: card.name,
    status: "downloading",
    source: "scryfall",
  };
  publish(new Map(pending).set(key, mine));
  void generationApi
    .downloadOriginals({
      project_tag: projectTag,
      entries: [cardToEntry(card)],
      output_dir: DEFAULT_GEN_PATHS.output_dir,
      cache_dir: DEFAULT_GEN_PATHS.cache_dir,
      weights_dir: DEFAULT_GEN_PATHS.weights_dir,
    })
    .then((result) => waitForTasks(result.task_ids))
    .then(refreshStatus)
    .catch(() => {})
    .finally(() => {
      // Unless the card was picked again meanwhile.
      if (pending.get(key) === mine) update(key, null);
    });
}

/** Something just picked for a card that is still on its way: MPC Fill
 *  art for one face, or (source "scryfall") a whole new printing. */
export interface PendingFaceArt {
  cardId: number;
  face: number;
  totalFaces: number;
  name: string;
  status: "queued" | "downloading" | "failed";
  source: "mpc" | "scryfall";
  error?: string;
}

interface Job {
  pending: PendingFaceArt;
  card: CardRow;
  art: MpcCard;
  setFaceArt: (cardId: number, face: number, totalFaces: number, image: CustomImage) => Promise<void>;
  projectTag: string | null;
  serverVersion: string | null;
  /** Re-fetch the Decklist's status, resolving once it has the new rows. */
  refreshStatus: () => Promise<unknown>;
  resolve: (ok: boolean) => void;
}

const keyOf = (cardId: number, face: number) => `${cardId}:${face}`;
let pending = new Map<string, PendingFaceArt>();
const queue: Job[] = [];
let running = false;
const listeners = new Set<() => void>();

function publish(next: Map<string, PendingFaceArt>) {
  pending = next;
  for (const l of listeners) l();
}

function update(key: string, change: Partial<PendingFaceArt> | null) {
  const next = new Map(pending);
  const current = next.get(key);
  if (change == null) next.delete(key);
  else if (current) next.set(key, { ...current, ...change });
  publish(next);
}

async function drain() {
  if (running) return;
  running = true;
  while (queue.length > 0) {
    const job = queue.shift() as Job;
    const key = keyOf(job.pending.cardId, job.pending.face);
    update(key, { status: "downloading" });
    try {
      const image = await importMpcArt("custom", job.art);
      await job.setFaceArt(job.pending.cardId, job.pending.face, job.pending.totalFaces, image);
      // Registered for this project straight away, like a dropped custom
      // (with the art on the card, since the caller's list predates it).
      const withArt: CardRow = {
        ...job.card,
        face_art: [
          ...(job.card.face_art ?? []).filter((fa) => fa.face !== job.pending.face),
          {
            face: job.pending.face,
            total_faces: job.pending.totalFaces,
            custom_image_id: image.id,
            custom_hash: image.content_hash,
            label: image.label,
            mpc_drive_id: image.mpc?.drive_id ?? null,
          },
        ],
      };
      // The face keeps showing as downloading until its registration has
      // landed and the Decklist has re-read the status, so its badge goes
      // straight to done rather than through "not downloaded yet". In the
      // background: the next download needn't wait for it.
      void finishRegistration(job, withArt, key);
      job.resolve(true);
    } catch (err) {
      update(key, { status: "failed", error: err instanceof Error ? err.message : String(err) });
      job.resolve(false);
    }
  }
  running = false;
}

async function finishRegistration(job: Job, withArt: CardRow, key: string) {
  try {
    const taskIds = await registerCustomCards([withArt], job.projectTag, job.serverVersion);
    await waitForTasks(taskIds);
    await job.refreshStatus();
  } catch {
    // Registration retries with the next Download, Generate, PDF or ZIP;
    // the face then reads "not downloaded yet", which is the truth.
  } finally {
    // Unless the face was picked again meanwhile.
    if (pending.get(key)?.status === "downloading") update(key, null);
  }
}

/**
 * Make `art` the art of `face` of `card` once it has downloaded, in the
 * background. Resolves true when it's in place, false if it failed (the
 * face then shows the failure until dismissed or picked again).
 */
export function queueFaceArtPick(args: {
  card: CardRow;
  face: number;
  totalFaces: number;
  art: MpcCard;
  setFaceArt: Job["setFaceArt"];
  projectTag: string | null;
  serverVersion: string | null;
  refreshStatus: Job["refreshStatus"];
}): Promise<boolean> {
  const key = keyOf(args.card.id, args.face);
  // A newer pick for the same face replaces one still waiting.
  const waiting = queue.findIndex((j) => keyOf(j.pending.cardId, j.pending.face) === key);
  if (waiting >= 0) queue.splice(waiting, 1)[0].resolve(false);
  return new Promise((resolve) => {
    const job: Job = {
      ...args,
      pending: {
        cardId: args.card.id,
        face: args.face,
        totalFaces: args.totalFaces,
        name: args.art.name,
        status: "queued",
        source: "mpc",
      },
      resolve,
    };
    queue.push(job);
    const next = new Map(pending);
    next.set(key, job.pending);
    publish(next);
    void drain();
  });
}

/** Drop every MPC download not yet started (the one in flight finishes). */
export function cancelPendingFaceArt(): void {
  const dropped = queue.splice(0, queue.length);
  const next = new Map(pending);
  for (const job of dropped) {
    next.delete(keyOf(job.pending.cardId, job.pending.face));
    job.resolve(false);
  }
  publish(next);
}

/** Forget a failed download, e.g. once the user picks again. */
export function dismissPendingFaceArt(cardId: number, face: number): void {
  if (pending.get(keyOf(cardId, face))?.status === "failed") update(keyOf(cardId, face), null);
}

function subscribe(listener: () => void) {
  listeners.add(listener);
  return () => listeners.delete(listener);
}

/** Every face with MPC art on its way (or failed), keyed `cardId:face`. */
export function usePendingFaceArt(): ReadonlyMap<string, PendingFaceArt> {
  return useSyncExternalStore(subscribe, () => pending);
}

export function pendingFaceArtKey(cardId: number, face: number): string {
  return keyOf(cardId, face);
}

/** The key a picked printing's download is pending under. */
export function pendingPrintingKey(cardId: number): string {
  return `${cardId}:printing`;
}

export interface AutoPickResult {
  /** Faces now printing MPC Fill art. */
  picked: number;
  /** Cards MPC Fill had nothing for, on any face (Scryfall art kept). */
  unmatched: string[];
  /** Downloads that failed (their faces keep Scryfall art). */
  failed: number;
}

// One /api/mpcfill/best call covers this many cards (the server's cap).
const BEST_BATCH = 300;

/**
 * Give freshly imported Scryfall cards MPC Fill art: MPC Fill's top match
 * for every face (one search per 300 cards, on the server), each queued
 * like a pick in the modal. Faces with no match keep their Scryfall art;
 * cards that already have face art are left alone. `onProgress` follows
 * the downloads. Throws only if the matching itself fails.
 */
export async function autoPickMpcArt(
  cards: CardRow[],
  opts: {
    setFaceArt: Job["setFaceArt"];
    projectTag: string | null;
    serverVersion: string | null;
    refreshStatus: Job["refreshStatus"];
    /** The import's language (Scryfall code): only MPC Fill art in it
     *  counts, so a card without any keeps its printing. null: any. */
    lang: string | null;
    onProgress: (done: number, total: number) => void;
  },
): Promise<AutoPickResult> {
  const eligible = cards.filter(
    (c) => c.scryfall_id != null && c.custom_image_id == null && !c.face_art?.length,
  );
  const ids = [...new Set(eligible.map((c) => c.scryfall_id as string))];
  const best = new Map<string, MpcBestResult["results"][number]>();
  for (let i = 0; i < ids.length; i += BEST_BATCH) {
    const resp = await generationApi.mpcBest({
      scryfall_ids: ids.slice(i, i + BEST_BATCH),
      lang: opts.lang,
    });
    for (const r of resp.results) best.set(r.scryfall_id, r);
  }

  const unmatched: string[] = [];
  const picks: Promise<boolean>[] = [];
  for (const card of eligible) {
    const match = best.get(card.scryfall_id as string);
    if (!match || !match.faces.some((f) => f != null)) {
      unmatched.push(card.name);
      continue;
    }
    match.faces.forEach((art, face) => {
      if (!art) return;
      picks.push(
        queueFaceArtPick({
          card,
          face,
          totalFaces: match.total_faces,
          art,
          setFaceArt: opts.setFaceArt,
          projectTag: opts.projectTag,
          serverVersion: opts.serverVersion,
          refreshStatus: opts.refreshStatus,
        }),
      );
    });
  }
  let done = 0;
  opts.onProgress(0, picks.length);
  const results = await Promise.all(
    picks.map((p) =>
      p.then((ok) => {
        done += 1;
        opts.onProgress(done, picks.length);
        return ok;
      }),
    ),
  );
  const picked = results.filter(Boolean).length;
  return { picked, unmatched, failed: results.length - picked };
}
