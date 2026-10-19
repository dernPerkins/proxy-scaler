// The app's notifications: short outcome messages ("Queued 12 task(s)",
// "Sol Ring: already 1195 DPI.", "Upscale failed: …") shown as a stack of
// toasts in the bottom-right corner (components/ToastStack.tsx), instead of
// a grey line under whatever button was clicked, where they were easy to
// miss. Same useSyncExternalStore idiom as update.ts: any module can raise
// one with toast(), no provider or hook needed.
//
// Info, success and warning toasts leave after TOAST_MS (hovering pauses
// that — the stack owns the timers). Errors, and anything raised sticky,
// stay until dismissed, so a failure is never missed. Messages that are
// state rather than an outcome (progress, a field's validation, the list
// of decklist lines that didn't match) stay inline where they belong.
import { useSyncExternalStore, type ReactNode } from "react";

export type ToastKind = "info" | "ok" | "warn" | "err";

export const TOAST_MS = 5000;
const MAX_TOASTS = 5;

export interface Toast {
  id: number;
  kind: ToastKind;
  message: ReactNode;
  title?: string;
  /** A later toast with the same key replaces this one (repeated clicks
   *  of one button keep one toast). */
  key?: string;
  /** Stays until dismissed. Always true for errors. */
  sticky: boolean;
  onDismiss?: () => void;
}

let toasts: Toast[] = [];
let nextId = 1;
const listeners = new Set<() => void>();

function emit(): void {
  for (const listener of listeners) listener();
}

function subscribe(listener: () => void): () => void {
  listeners.add(listener);
  return () => listeners.delete(listener);
}

function getToasts(): Toast[] {
  return toasts;
}

export function useToasts(): Toast[] {
  return useSyncExternalStore(subscribe, getToasts);
}

/** Raise a toast; returns its id. */
export function toast(
  message: ReactNode,
  kind: ToastKind = "info",
  opts: { key?: string; title?: string; sticky?: boolean; onDismiss?: () => void } = {},
): number {
  const entry: Toast = {
    id: nextId++,
    kind,
    message,
    title: opts.title,
    key: opts.key,
    sticky: kind === "err" || opts.sticky === true,
    onDismiss: opts.onDismiss,
  };
  let next = opts.key ? toasts.filter((t) => t.key !== opts.key) : [...toasts];
  next.push(entry);
  // Too many: the oldest ones that would leave anyway go first.
  while (next.length > MAX_TOASTS) {
    const oldest = next.findIndex((t) => !t.sticky);
    next = next.filter((_, i) => i !== (oldest === -1 ? 0 : oldest));
  }
  toasts = next;
  emit();
  return entry.id;
}

/** Remove a toast (its × or its timer). Runs its onDismiss. */
export function dismissToast(id: number): void {
  const found = toasts.find((t) => t.id === id);
  if (!found) return;
  toasts = toasts.filter((t) => t.id !== id);
  emit();
  found.onDismiss?.();
}

/** Remove a keyed toast without running its onDismiss (its reason went
 *  away, e.g. the versions now match). */
export function clearToast(key: string): void {
  if (!toasts.some((t) => t.key === key)) return;
  toasts = toasts.filter((t) => t.key !== key);
  emit();
}

/** A caught error as toast text. */
export function errorText(err: unknown): string {
  return err instanceof Error ? err.message : String(err);
}
