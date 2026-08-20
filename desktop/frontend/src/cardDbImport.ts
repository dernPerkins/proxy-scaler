// Tiny cross-component store for the one card-database import job the app
// may be watching — same useSyncExternalStore idiom as update.ts. The
// import can be STARTED from two places (the sidebar's CardDbPanel, the
// boot-time CardDbPrompt) but is WATCHED in exactly one: the blocking
// CardDbImportModal mounted in App.tsx. Starters push the job id here so
// the modal appears immediately, without waiting a status-poll roundtrip;
// the modal also adopts jobs it discovers via /api/cards/status (an import
// started by another window entirely).

let activeJobId: string | null = null;
const listeners = new Set<() => void>();

function notify(): void {
  for (const listener of listeners) listener();
}

export function subscribeCardDbImport(listener: () => void): () => void {
  listeners.add(listener);
  return () => listeners.delete(listener);
}

export function getCardDbImportJobId(): string | null {
  return activeJobId;
}

export function setCardDbImportJobId(jobId: string | null): void {
  if (activeJobId === jobId) return;
  activeJobId = jobId;
  notify();
}
