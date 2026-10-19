// Port of proxy_scaler/services/decklist.py's identity-matching and
// status-aggregation logic. cards.py (which used to compute this
// server-side, joining project_cards against tasks/gallery) is gone —
// the generation server has no concept of "a project's cards" any more,
// so the client now merges its own local CardRow list (see api/project.ts)
// with the generation server's tasks + gallery items itself, using the
// exact same string-key scheme as the Python original so the two sides
// never drift. See ARCHITECTURE.md.
import type { GalleryItem, Task, TaskStatus } from "./api/types";

// Physical-card identity (no face_index/label) shared by CardRow,
// GalleryItem, and Task — used to match a local card to its faces'
// gallery items/tasks regardless of which one you start from.
//
// Deviates from the Python original (services/decklist.py::card_identity)
// by one addition: a `nameFallback` used when both set/collector *and*
// scryfallId are absent. The Python version never needed this because a
// name-only decklist entry always got a scryfall_id written back onto it
// server-side at import time, before ever being matched — so scryfall_id
// was always the last-resort key in practice. The client-side project
// store deliberately never calls Scryfall at all (see ARCHITECTURE.md),
// so a CardRow for a name-only line has neither set/collector nor a
// scryfall_id until generation actually runs — without a name fallback,
// every such card would collide into one "unknown" bucket and show each
// other's generated faces.
export function cardIdentity(
  setCode: string | null | undefined,
  collectorNumber: string | null | undefined,
  scryfallId: string | null | undefined,
  nameFallback?: string | null,
  lang?: string | null,
  customHash?: string | null,
): string {
  // Custom Images first, and never by any other tier. They have no
  // set/collector and no scryfall_id, so they would otherwise all fall
  // through to the name fallback — where two uploads sharing a filename
  // would merge into one card showing each other's images, and one named
  // after a real card would match that card's generated faces.
  //
  // Must stay byte-identical to the server's customs.identity_key() and
  // to the expression the generated_images unique index is built over, or
  // a generated image stops matching the card that asked for it.
  if (customHash) return `custom:${customHash}`;
  // Both lowercased: collector numbers are usually digits, but not always
  // (e.g. "DDG-14") — the client parses these verbatim from whatever case
  // the user typed, while Scryfall's resolved value uses its own
  // canonical case, which won't always agree.
  //
  // Language is part of the printing identity: the Italian and English
  // rows of one set/collector are different cards with their own images.
  // Absent lang normalizes to "en" — everything predating the lang field
  // was English by construction, so old rows keep matching exactly.
  if (setCode && collectorNumber) {
    return `${setCode.toLowerCase()}/${collectorNumber.toLowerCase()}/${(lang ?? "en").toLowerCase()}`;
  }
  if (scryfallId) return scryfallId;
  if (nameFallback) return `name:${nameFallback.toLowerCase()}`;
  return "unknown";
}

function faceGroupKey(item: GalleryItem): string {
  const identity = cardIdentity(
    item.set_code, item.collector_number, item.scryfall_id, item.card_name, item.lang,
    item.custom_hash,
  );
  return `${identity}:${item.face_index}:${item.face_label}`;
}

// Same identity scheme as faceGroupKey, read off a Task instead of a
// GalleryItem, so a face's in-flight task and its (once done) gallery
// item merge under the same key.
function faceKeyForTask(task: Task): string {
  const identity = cardIdentity(
    task.set_code, task.collector_number, task.scryfall_id, task.card_name, task.lang,
    task.custom_hash,
  );
  return `${identity}:${task.face_index}:${task.face_label}`;
}

export interface FaceRow {
  key: string;
  items: GalleryItem[];
  tasks: Task[];
}

// Merge gallery items (done variants) and tasks (in-flight/failed/
// canceled) into one row per face. A face with a task but no done variant
// yet still gets a row — that's how "task exists, here's its status"
// shows up before anything has actually finished generating.
export function buildRows(items: GalleryItem[], tasks: Task[]): FaceRow[] {
  const galleryGroups = new Map<string, GalleryItem[]>();
  const order: string[] = [];
  for (const item of items) {
    const key = faceGroupKey(item);
    if (!galleryGroups.has(key)) {
      galleryGroups.set(key, []);
      order.push(key);
    }
    galleryGroups.get(key)!.push(item);
  }

  const taskGroups = new Map<string, Task[]>();
  for (const task of tasks) {
    const key = faceKeyForTask(task);
    if (!galleryGroups.has(key) && !taskGroups.has(key)) {
      order.push(key);
    }
    if (!taskGroups.has(key)) taskGroups.set(key, []);
    taskGroups.get(key)!.push(task);
  }

  return order.map((key) => ({
    key,
    items: galleryGroups.get(key) ?? [],
    tasks: taskGroups.get(key) ?? [],
  }));
}

// Coarser than buildRows: group by physical card (drop face_index), for
// matching a card row's identity to all of its faces at once.
export function groupByCard(
  items: GalleryItem[],
  tasks: Task[],
): { galleryByCard: Map<string, GalleryItem[]>; tasksByCard: Map<string, Task[]> } {
  const galleryByCard = new Map<string, GalleryItem[]>();
  for (const item of items) {
    const id = cardIdentity(
      item.set_code, item.collector_number, item.scryfall_id, item.card_name, item.lang,
      item.custom_hash,
    );
    if (!galleryByCard.has(id)) galleryByCard.set(id, []);
    galleryByCard.get(id)!.push(item);
  }
  const tasksByCard = new Map<string, Task[]>();
  for (const task of tasks) {
    const id = cardIdentity(
      task.set_code, task.collector_number, task.scryfall_id, task.card_name, task.lang,
      task.custom_hash,
    );
    if (!tasksByCard.has(id)) tasksByCard.set(id, []);
    tasksByCard.get(id)!.push(task);
  }
  return { galleryByCard, tasksByCard };
}

// Secondary index, by card_name alone (casefolded). Needed because a
// name-only local card (see cardIdentity's doc comment) computes a
// "name:x" identity, but the generation server *always* resolves a
// name-only decklist line to one concrete printing with a real set +
// collector_number — so its own gallery/task rows never fall into the
// "name:x" bucket themselves; they land in a real "set/collector" bucket
// instead, and groupByCard alone would never connect the two. Callers
// should only consult this for a local card that itself has no set/
// collector — matching everything by name would risk conflating two
// different printings that happen to share a card name. Only Scryfall
// rows are indexed: a Custom Image or Back Image is matched by its hash
// alone, so a custom named "Sol Ring" never stands in for the card.
export function groupByCardName(
  items: GalleryItem[],
  tasks: Task[],
): { galleryByName: Map<string, GalleryItem[]>; tasksByName: Map<string, Task[]> } {
  const galleryByName = new Map<string, GalleryItem[]>();
  for (const item of items) {
    if (item.custom_hash || item.back_hash) continue;
    const key = item.card_name.toLowerCase();
    if (!galleryByName.has(key)) galleryByName.set(key, []);
    galleryByName.get(key)!.push(item);
  }
  const tasksByName = new Map<string, Task[]>();
  for (const task of tasks) {
    if (task.custom_hash || task.back_hash) continue;
    const key = task.card_name.toLowerCase();
    if (!tasksByName.has(key)) tasksByName.set(key, []);
    tasksByName.get(key)!.push(task);
  }
  return { galleryByName, tasksByName };
}

export interface VariantStatus {
  dpi: number;
  model: string;
  status: TaskStatus;
  error: string | null;
  galleryItemId: number | null;
  // Where the existing image's inference ran ("gpu" | "cpu" | "unknown");
  // null when no image exists yet. Lets the deck list mark CPU-fallback
  // output on machines that have a working GPU.
  device: string | null;
}

function pairKey(dpi: number, model: string): string {
  return `${dpi}:${model}`;
}

// One (dpi, model, status, error) entry per (dpi, model) pair this face
// has ever had a done variant or a task for. A done GalleryItem wins over
// FINISHED task history for the same pair — records for a pair that's
// since succeeded are just history, not current state. But an ACTIVE
// (pending/running) task wins over a done item: that's a regeneration in
// flight, and the deck list must show it as queued, not silently "done"
// (the Tasks tab shouldn't be the only place a Regen is visible). The
// done item's id is kept either way so the existing image stays rendered
// while its replacement generates. Otherwise the newest task for the pair
// (faceTasks is expected created_at DESC, same as the Python original)
// determines the status.
export function statusForPairs(faceItems: GalleryItem[], faceTasks: Task[]): VariantStatus[] {
  const donePairs = new Map<string, GalleryItem>();
  for (const item of faceItems) donePairs.set(pairKey(item.dpi, item.model), item);

  const taskPairs = new Map<string, Task>();
  for (const task of faceTasks) {
    const key = pairKey(task.dpi, task.model);
    if (!taskPairs.has(key)) taskPairs.set(key, task);
  }

  const allKeys = new Set([...donePairs.keys(), ...taskPairs.keys()]);
  const rows: VariantStatus[] = [];
  for (const key of allKeys) {
    const done = donePairs.get(key);
    const task = taskPairs.get(key);
    const activeTask = task && (task.status === "pending" || task.status === "running") ? task : null;
    if (activeTask) {
      rows.push({
        dpi: activeTask.dpi,
        model: activeTask.model,
        status: activeTask.status,
        error: activeTask.error,
        galleryItemId: done?.id ?? null,
        device: done?.device ?? null,
      });
    } else if (done) {
      rows.push({
        dpi: done.dpi,
        model: done.model,
        status: "done",
        error: null,
        galleryItemId: done.id,
        device: done.device ?? "unknown",
      });
    } else {
      rows.push({
        dpi: task!.dpi,
        model: task!.model,
        status: task!.status,
        error: task!.error,
        galleryItemId: null,
        device: null,
      });
    }
  }
  rows.sort((a, b) => a.dpi - b.dpi || a.model.localeCompare(b.model));
  return rows;
}

/** One face of a card row as the Decklist shows it. `art` is set when the
 *  face prints a Custom Image (MPC Fill art) in place of the printing's
 *  own: its variants are that image's then. */
export interface CardFaceStatus<A> {
  faceLabel: string | null;
  variants: VariantStatus[];
  art?: A;
}

/**
 * Swap each overridden face's Scryfall variants for those of the Custom
 * Image it prints instead. The custom's own rows live under its
 * `custom:<hash>` identity, single-faced and unlabelled, so they are
 * fetched from there and labelled as the face they stand in for. Faces
 * come back front first; an overridden face shows even before anything
 * has been registered for it (with no variants), so the row says where
 * its art comes from.
 */
export function applyFaceArt<
  A extends { face: number; total_faces: number; custom_hash: string | null },
>(
  scryfallFaces: { faceIndex: number | null; face: CardFaceStatus<A> }[],
  faceArt: A[],
  galleryByCard: Map<string, GalleryItem[]>,
  tasksByCard: Map<string, Task[]>,
): CardFaceStatus<A>[] {
  if (faceArt.length === 0) return scryfallFaces.map((f) => f.face);
  const overridden = new Set(faceArt.map((fa) => fa.face));
  const out: { position: number; face: CardFaceStatus<A> }[] = scryfallFaces
    .filter((f) => !overridden.has(f.faceIndex ?? 0))
    .map((f) => ({ position: f.faceIndex ?? 0, face: f.face }));
  for (const art of faceArt) {
    // Chosen but not downloaded yet: nothing on the server to show.
    const identity = art.custom_hash
      ? cardIdentity(null, null, null, null, null, art.custom_hash)
      : null;
    const variants = identity
      ? statusForPairs(
          galleryByCard.get(identity) ?? [],
          tasksByCard.get(identity) ?? [],
        ).filter((v) => v.status !== "canceled")
      : [];
    out.push({
      position: art.face,
      face: {
        faceLabel: art.total_faces > 1 ? (art.face === 0 ? "front" : "back") : null,
        variants,
        art,
      },
    });
  }
  return out.sort((a, b) => a.position - b.position).map((f) => f.face);
}
