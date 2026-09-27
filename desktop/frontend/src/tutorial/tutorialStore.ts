// State for the first-view tutorials: which tours the user has already
// been through (persisted in app_settings — see project_store.rs's
// tutorials_completed), and which tour is on screen right now. Same
// module-level useSyncExternalStore idiom as update.ts, because the ?
// buttons, the pages that auto-start tours, and the one overlay that draws
// them live in different component trees (the connect screen's tour runs
// above the router).
//
// Finishing and skipping both mark a tour completed: either way it never
// auto-shows again, and the ? button replays it on demand.

import { useEffect, useRef, useSyncExternalStore } from "react";
import { projectApi } from "../api/project";
import { getCardDbImportJobId, subscribeCardDbImport } from "../cardDbImport";
import { getServerReadiness, subscribeServerReadiness } from "../config";
import { isTauri } from "../tauri";
import {
  getBootUpdateCheckSettled,
  getCardDbPromptOpen,
  getCardDbSettled,
  getPatchNotesPromptOpen,
  getPatchNotesSettled,
  getResumeTasksPromptOpen,
  getResumeTasksSettled,
  getUpdatePromptOpen,
  subscribeUpdateStore,
} from "../update";
import { TOURS, type TourId, type TourStep } from "./tours";

interface ActiveSegment {
  id: TourId;
  steps: TourStep[];
}

export interface ActiveTour {
  /** The tour plus, on a ? replay, any follow-ups whose piece is on
   *  screen — played back to back. */
  segments: ActiveSegment[];
  segment: number;
  step: number;
}

export interface TutorialState {
  /** null until the stored list has loaded — nothing auto-starts before. */
  completed: ReadonlySet<string> | null;
  active: ActiveTour | null;
}

let state: TutorialState = { completed: null, active: null };
let listeners: Array<() => void> = [];
// Which follow-up tours' pieces are currently on screen, published by
// useTourWhenPresent — lets a ? replay chain straight into them.
const present = new Map<TourId, boolean>();
let loadStarted = false;

function setState(next: Partial<TutorialState>): void {
  state = { ...state, ...next };
  for (const listener of listeners) listener();
}

function subscribe(callback: () => void): () => void {
  listeners.push(callback);
  return () => {
    listeners = listeners.filter((l) => l !== callback);
  };
}

function getState(): TutorialState {
  return state;
}

export function useTutorialState(): TutorialState {
  return useSyncExternalStore(subscribe, getState);
}

function ensureLoaded(): void {
  if (loadStarted) return;
  loadStarted = true;
  // Outside Tauri there is nowhere to persist to: completion lives in
  // memory, so a plain browser dev tab shows each tour once per reload.
  // Dev preview: `?tutorial` in the URL ignores the stored list for this
  // session, so every tour auto-shows again without touching the DB.
  const forceFresh =
    import.meta.env.DEV && new URLSearchParams(location.search).has("tutorial");
  if (!isTauri() || forceFresh) {
    setState({ completed: new Set() });
    return;
  }
  projectApi
    .getCompletedTutorials()
    .then((ids) => setState({ completed: new Set(ids) }))
    // Unreadable means we can't tell, and re-showing a tour beats never
    // showing one.
    .catch(() => setState({ completed: new Set() }));
}

function markCompleted(ids: TourId[]): void {
  const completed = new Set(state.completed ?? []);
  for (const id of ids) {
    if (completed.has(id)) continue;
    completed.add(id);
    if (isTauri()) projectApi.markTutorialCompleted(id).catch(() => {});
  }
  setState({ completed });
}

export function findTourTarget(name: string): HTMLElement | null {
  return document.querySelector<HTMLElement>(`[data-tour="${name}"]`);
}

// Steps whose target isn't rendered right now are dropped up front, so the
// step counter never promises a step that can't be shown.
function resolveSegment(id: TourId): ActiveSegment | null {
  const steps = TOURS[id].steps.filter((s) => !s.target || findTourTarget(s.target));
  return steps.length ? { id, steps } : null;
}

function start(ids: TourId[]): boolean {
  const segments = ids
    .map(resolveSegment)
    .filter((s): s is ActiveSegment => s != null);
  if (!segments.length) return false;
  setState({ active: { segments, segment: 0, step: 0 } });
  return true;
}

/** The ? button resets the screen's tutorial: it replays the tour
 *  regardless of completion, straight into any follow-up whose piece is on
 *  screen now — and marks the rest unseen again, so each auto-shows the
 *  next time its piece appears (a fresh project, before any import). */
export function replayTour(id: TourId): void {
  const followUps = TOURS[id].followUps ?? [];
  const onScreen = followUps.filter((f) => present.get(f));
  const later = followUps.filter((f) => !present.get(f));
  if (later.length) unmarkCompleted(later);
  start([id, ...onScreen]);
}

function unmarkCompleted(ids: TourId[]): void {
  const completed = new Set(state.completed ?? []);
  for (const id of ids) completed.delete(id);
  setState({ completed });
  if (isTauri()) projectApi.unmarkTutorialsCompleted(ids).catch(() => {});
}

export function nextStep(): void {
  const active = state.active;
  if (!active) return;
  const seg = active.segments[active.segment];
  if (active.step + 1 < seg.steps.length) {
    setState({ active: { ...active, step: active.step + 1 } });
    return;
  }
  markCompleted([seg.id]);
  if (active.segment + 1 < active.segments.length) {
    setState({ active: { ...active, segment: active.segment + 1, step: 0 } });
  } else {
    setState({ active: null });
  }
}

export function prevStep(): void {
  const active = state.active;
  if (!active) return;
  if (active.step > 0) {
    setState({ active: { ...active, step: active.step - 1 } });
  } else if (active.segment > 0) {
    const segment = active.segment - 1;
    setState({
      active: { ...active, segment, step: active.segments[segment].steps.length - 1 },
    });
  }
}

/** Skip ends the whole run — every tour in it counts as seen. */
export function skipTour(): void {
  const active = state.active;
  if (!active) return;
  markCompleted(active.segments.map((s) => s.id));
  setState({ active: null });
}

// --- When a tour may auto-start ---------------------------------------------
//
// Never over a launch dialog: tours are the last link of update.ts's boot
// chain (update -> patch notes -> resume-tasks -> card-db), and also wait
// out the local server's boot modal and a card-database import.

function useUpdateFlag(get: () => boolean): boolean {
  return useSyncExternalStore(subscribeUpdateStore, get);
}

/** The connect screen sits above everything but the update offer and the
 *  patch notes (both mounted above ConnectGate in main.tsx). */
function useGateIdle(): boolean {
  const updateSettled = useUpdateFlag(getBootUpdateCheckSettled);
  const updateOpen = useUpdateFlag(getUpdatePromptOpen);
  const notesSettled = useUpdateFlag(getPatchNotesSettled);
  const notesOpen = useUpdateFlag(getPatchNotesPromptOpen);
  return updateSettled && !updateOpen && notesSettled && !notesOpen;
}

function useAppIdle(): boolean {
  const gateIdle = useGateIdle();
  const resumeSettled = useUpdateFlag(getResumeTasksSettled);
  const resumeOpen = useUpdateFlag(getResumeTasksPromptOpen);
  const cardDbSettled = useUpdateFlag(getCardDbSettled);
  const cardDbOpen = useUpdateFlag(getCardDbPromptOpen);
  const readiness = useSyncExternalStore(subscribeServerReadiness, getServerReadiness);
  const importJob = useSyncExternalStore(subscribeCardDbImport, getCardDbImportJobId);
  return (
    gateIdle &&
    resumeSettled &&
    !resumeOpen &&
    cardDbSettled &&
    !cardDbOpen &&
    readiness.status === "ready" &&
    importJob == null
  );
}

// Long enough for a freshly mounted page to lay out (and for a query that
// resolves right after mount to fill it) before the spotlight measures it.
const START_DELAY_MS = 450;

function useAutoStart(id: TourId, eligible: boolean): void {
  const tutorial = useTutorialState();
  useEffect(ensureLoaded, []);
  const ready =
    eligible &&
    tutorial.completed != null &&
    !tutorial.completed.has(id) &&
    tutorial.active == null;
  // The timer re-checks through a ref, so a dialog that opens during the
  // delay still wins.
  const readyRef = useRef(ready);
  readyRef.current = ready;
  useEffect(() => {
    if (!ready) return;
    const timer = window.setTimeout(() => {
      if (readyRef.current && state.active == null) start([id]);
    }, START_DELAY_MS);
    return () => window.clearTimeout(timer);
  }, [id, ready]);
}

/** Auto-starts a screen's base tour the first time it's viewed. `present`
 *  holds it back while the screen shows only a placeholder (PDF and
 *  ZIP with no project yet). */
export function useTourOnFirstView(
  id: TourId,
  options: { scope?: "gate" | "app"; present?: boolean } = {},
): void {
  const gateIdle = useGateIdle();
  const appIdle = useAppIdle();
  const idle = options.scope === "gate" ? gateIdle : appIdle;
  useAutoStart(id, idle && (options.present ?? true));
}

/** Auto-starts a follow-up tour the first time its piece is on screen —
 *  and only once its base tour is done, so it never jumps the queue. */
export function useTourWhenPresent(id: TourId, isPresent: boolean, after: TourId): void {
  const idle = useAppIdle();
  const tutorial = useTutorialState();
  useEffect(() => {
    present.set(id, isPresent);
    return () => {
      present.set(id, false);
    };
  }, [id, isPresent]);
  const afterDone = tutorial.completed?.has(after) ?? false;
  useAutoStart(id, idle && isPresent && afterDone);
}
