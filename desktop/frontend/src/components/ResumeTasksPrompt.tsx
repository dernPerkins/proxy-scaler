import { useQueryClient } from "@tanstack/react-query";
import { useEffect, useRef, useState, useSyncExternalStore } from "react";
import { generationApi } from "../api/generation";
import { useConnection } from "../connection";
import ModalOverlay from "./ModalOverlay";
import { isTauri } from "../tauri";
import {
  getBootUpdateCheckSettled,
  getUpdatePromptOpen,
  setResumeTasksPromptOpen,
  setResumeTasksSettled,
  subscribeUpdateStore,
} from "../update";

// The connect-time choice about leftover tasks, in two flavors keyed on
// whether the worker is HELD:
//
// Held (the embedded local server — main.rs spawns it with --hold-worker):
// tasks left pending/running by the last session do NOT start processing
// on launch — the worker waits until this component releases it via
// POST /api/worker/release. Without the hold, a heavy leftover upscale
// would start hogging the machine seconds after launch, before any UI
// could offer a way out; and a task killed mid-flight would be re-queued
// on every launch, forever.
//
// Not held (remote servers, the standalone server app, older servers):
// those deployments keep processing immediately by design — there is no
// hold to release, so leftovers are already running by the time the
// client connects. The dialog still appears (with copy that says so),
// because "your remote box is quietly burning GPU on tasks you forgot
// about" deserves the same visibility as the local case: the choice is
// keep-going vs cancel rather than resume vs cancel. Bulk-canceling
// running rows is server-refused while not held (a 'running' row may be
// genuinely in flight — see generation.py::cancel_all_tasks), so cancel
// covers the queue and an in-flight task finishes first.
//
// The flow, once per connection:
//   - held with zero leftovers → release silently, no dialog.
//   - held with leftovers → ask: Resume (release) or Cancel all
//     (cancel pending AND orphaned running rows — allowed by the server
//     only while held — then release).
//   - not held with zero leftovers → nothing to do, no dialog.
//   - not held with leftovers → ask: Keep processing (just dismiss) or
//     Cancel all (pending only, see above).
//
// Sequenced behind the boot update dialog (see update.ts's boot-dialog
// sequencing section): the modal renders only once the update check has
// settled and no update modal is up. Deferring is free — a held worker
// stays held while we wait, and a running one was running anyway. Data
// fetching and the silent paths don't wait; only the modal does.
export default function ResumeTasksPrompt() {
  const { status } = useConnection();
  const queryClient = useQueryClient();
  // null = nothing to ask (not checked yet, or already decided).
  const [leftover, setLeftover] = useState<number | null>(null);
  const [held, setHeld] = useState(false);
  const [working, setWorking] = useState(false);
  const [error, setError] = useState<string | null>(null);

  const bootUpdateSettled = useSyncExternalStore(
    subscribeUpdateStore,
    getBootUpdateCheckSettled,
  );
  const updatePromptOpen = useSyncExternalStore(
    subscribeUpdateStore,
    getUpdatePromptOpen,
  );

  // Once per connection, not once per mount: a local↔remote switch (or a
  // switch between remote hosts) passes through a non-"connected" status
  // on its way to the new server, so resetting the ref there makes the
  // check run again against whatever was just connected to.
  const checkedThisConnection = useRef(false);

  // Publish this component's link in the boot-dialog chain (see
  // update.ts): CardDbPrompt waits for our verdict before offering the
  // corpus download. A browser tab can't run the flow at all — settle
  // immediately there; every Tauri path settles through the check below.
  useEffect(() => {
    if (!isTauri()) setResumeTasksSettled();
  }, []);

  useEffect(() => {
    if (status.kind !== "connected") {
      checkedThisConnection.current = false;
      return;
    }
    if (!isTauri()) return;
    if (checkedThisConnection.current) return;
    checkedThisConnection.current = true;

    let cancelled = false;
    // Set only when a run reaches one of its real outcomes (nothing to
    // ask / released / dialog up). StrictMode's dev-only
    // mount→unmount→remount cancels the first run mid-flight — that run
    // must hand the guard back in cleanup, or the remount's run bails on
    // it and the check never happens at all (a held worker would then
    // sit held forever, with no dialog).
    let settled = false;
    void (async () => {
      try {
        // generationApi.request() already awaits the server-ready gate,
        // so nothing here races the sidecar's startup.
        const worker = await generationApi.workerStatus();
        if (cancelled) return;
        const tasks = await generationApi.listTasks();
        if (cancelled) return;
        const count = tasks.filter(
          (t) => t.status === "pending" || t.status === "running",
        ).length;
        if (count === 0) {
          if (worker.held) {
            await generationApi.releaseWorker();
            void queryClient.invalidateQueries({ queryKey: ["worker-status"] });
          }
          settled = true;
          setResumeTasksSettled();
          return;
        }
        settled = true;
        // `held` is absent from servers predating the hold mechanism —
        // absent means nothing is held, same as false.
        setHeld(worker.held ?? false);
        setResumeTasksPromptOpen(true);
        setLeftover(count);
      } catch (err) {
        // Reaching here means the server stopped answering right after
        // connecting — in which case a release call would fail too.
        // Leaving a held worker held is the safe direction: nothing heavy
        // starts, and the next launch simply asks again. Deliberately
        // not `settled`: if the component remounts, the check retries.
        console.error("leftover-task check failed", err);
      }
    })();
    return () => {
      cancelled = true;
      if (!settled) checkedThisConnection.current = false;
    };
  }, [status.kind, queryClient]);

  if (leftover === null || !bootUpdateSettled || updatePromptOpen) return null;

  async function finish(action: () => Promise<unknown>) {
    setWorking(true);
    setError(null);
    try {
      await action();
    } catch (err) {
      // A held worker is still held (cancel-then-release means a failure
      // at either step leaves the release un-sent), so keeping the choice
      // on screen is safe — nothing starts processing behind the error.
      setError(err instanceof Error ? err.message : String(err));
      setWorking(false);
      return;
    }
    void queryClient.invalidateQueries({ queryKey: ["tasks"] });
    void queryClient.invalidateQueries({ queryKey: ["worker-status"] });
    setLeftover(null);
    setWorking(false);
    setResumeTasksPromptOpen(false);
    setResumeTasksSettled();
  }

  // Not held: the tasks are already processing, so "keep going" has
  // nothing to send — the choice just closes.
  const resume = () =>
    finish(() => (held ? generationApi.releaseWorker() : Promise.resolve()));

  // Held: cancel commits before the release, so the freed worker can
  // never claim a task the user just asked to cancel — and the Tasks
  // page's 2s poll can never watch a leftover start running mid-choice.
  // Not held: include_running is server-refused (409) outside a hold, so
  // this cancels the queue and lets an in-flight task finish.
  const cancelAll = () =>
    finish(async () => {
      await generationApi.cancelAllTasks(held);
      if (held) await generationApi.releaseWorker();
    });

  return (
    // Like QuitPrompt: the two buttons are the whole choice — no overlay
    // dismiss and no Esc, because a quiet close would strand a held
    // worker with no visible way to release it.
    <ModalOverlay>
      <div className="modal modal-sm">
        <div className="modal-head">
          <span className="modal-title">
            {held
              ? "Unfinished tasks from your last session"
              : "Unfinished tasks on this server"}
          </span>
        </div>

        <p style={{ marginBottom: 8 }}>
          {held ? (
            <>
              {leftover} {leftover === 1 ? "task was" : "tasks were"} still queued or
              in progress when the app last closed. Resuming starts processing them
              now, which can use significant CPU/GPU for a while.
            </>
          ) : (
            <>
              {leftover} {leftover === 1 ? "task is" : "tasks are"} queued or in
              progress on the connected server, and it is already processing them —
              which can use significant CPU/GPU for a while.
            </>
          )}
        </p>
        <p className="hint" style={{ marginBottom: 14 }}>
          {!held && "Canceling clears the queue; a task already mid-upscale finishes first. "}
          Canceled cards can be re-generated any time from the Decklist tab.
        </p>

        {error && (
          <p className="error-text" style={{ marginBottom: 12 }}>
            {error}
          </p>
        )}

        <div className="modal-actions">
          <button
            className="btn-danger"
            onClick={() => void cancelAll()}
            disabled={working}
          >
            Cancel all tasks
          </button>
          <button
            className="btn-primary"
            autoFocus
            onClick={() => void resume()}
            disabled={working}
          >
            {held ? "Resume tasks" : "Keep processing"}
          </button>
        </div>
      </div>
    </ModalOverlay>
  );
}
