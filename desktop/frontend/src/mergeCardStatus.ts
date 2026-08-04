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
): string {
  if (setCode && collectorNumber) return `${setCode.toLowerCase()}/${collectorNumber}`;
  if (scryfallId) return scryfallId;
  if (nameFallback) return `name:${nameFallback.toLowerCase()}`;
  return "unknown";
}

function faceGroupKey(item: GalleryItem): string {
  const identity = cardIdentity(item.set_code, item.collector_number, item.scryfall_id, item.card_name);
  return `${identity}:${item.face_index}:${item.face_label}`;
}

// Same identity scheme as faceGroupKey, read off a Task instead of a
// GalleryItem, so a face's in-flight task and its (once done) gallery
// item merge under the same key.
function faceKeyForTask(task: Task): string {
  const identity = cardIdentity(task.set_code, task.collector_number, task.scryfall_id, task.card_name);
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
    const id = cardIdentity(item.set_code, item.collector_number, item.scryfall_id, item.card_name);
    if (!galleryByCard.has(id)) galleryByCard.set(id, []);
    galleryByCard.get(id)!.push(item);
  }
  const tasksByCard = new Map<string, Task[]>();
  for (const task of tasks) {
    const id = cardIdentity(task.set_code, task.collector_number, task.scryfall_id, task.card_name);
    if (!tasksByCard.has(id)) tasksByCard.set(id, []);
    tasksByCard.get(id)!.push(task);
  }
  return { galleryByCard, tasksByCard };
}

export interface VariantStatus {
  dpi: number;
  model: string;
  status: TaskStatus;
  error: string | null;
  galleryItemId: number | null;
}

function pairKey(dpi: number, model: string): string {
  return `${dpi}:${model}`;
}

// One (dpi, model, status, error) entry per (dpi, model) pair this face
// has ever had a done variant or a task for. A done GalleryItem always
// wins over any task history for the same pair — task records for a pair
// that's since succeeded are just history, not current state. Otherwise
// the newest task for that pair (faceTasks is expected created_at DESC,
// same as the Python original) determines the status.
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
    if (done) {
      rows.push({ dpi: done.dpi, model: done.model, status: "done", error: null, galleryItemId: done.id });
    } else {
      const task = taskPairs.get(key)!;
      rows.push({ dpi: task.dpi, model: task.model, status: task.status, error: task.error, galleryItemId: null });
    }
  }
  rows.sort((a, b) => a.dpi - b.dpi || a.model.localeCompare(b.model));
  return rows;
}
