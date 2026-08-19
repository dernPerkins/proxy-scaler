import { useQueryClient } from "@tanstack/react-query";
import { useEffect, useRef, useState, useSyncExternalStore } from "react";
import { generationApi } from "../api/generation";
import { useConnection } from "../connection";
import { isTauri } from "../tauri";
import {
  getBootUpdateCheckSettled,
  getUpdatePromptOpen,
  subscribeUpdateStore,
} from "../update";

// The launch-time choice about leftover tasks. The embedded local server
// is spawned with --hold-worker (main.rs), so tasks left pending/running
// by the last session do NOT start processing on launch — the worker
// waits until this component releases it via POST /api/worker/release.
// Without the hold, a heavy leftover upscale would start hogging the
// machine seconds after launch, before any UI could offer a way out; and
// a task killed mid-flight would be re-queued on every launch, forever.
//
// The flow, once per local connection:
//   - not held (remote server, standalone server app, older server) →
//     nothing to do, no dialog — those deployments keep processing
//     immediately, by design.
//   - held with zero leftover tasks → release silently, no dialog.
//   - held with leftovers → ask: Resume (release) or Cancel all
//     (cancel pending AND orphaned running rows — allowed by the server
//     only while held — then release).
//
// Sequenced behind the boot update dialog (see update.ts's boot-dialog
// sequencing section): the modal renders only once the update check has
// settled and no update modal is up. Deferring is free — the worker
// stays held, nothing processes while we wait. Data fetching and the
// silent-release path don't wait; only the modal does.
export default function ResumeTasksPrompt() {
  const { mode, status } = useConnection();
  const queryClient = useQueryClient();
  // null = nothing to ask (not checked yet, not held, or already decided).
  const [leftover, setLeftover] = useState<number | null>(null);
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

  // Once per local connection, not once per mount: a remote→local switch
  // (connection.tsx::switchTo) spawns a fresh held sidecar mid-session,
  // so the check must run again then — hence a ref reset whenever the
  // mode leaves "local", rather than a run-once flag.
  const checkedThisConnection = useRef(false);

  useEffect(() => {
    if (mode !== "local") {
      checkedThisConnection.current = false;
      return;
    }
    if (!isTauri() || status.kind !== "connected") return;
    if (checkedThisConnection.current) return;
    checkedThisConnection.current = true;

    let cancelled = false;
    void (async () => {
      try {
        // generationApi.request() already awaits the server-ready gate,
        // so nothing here races the sidecar's startup.
        const worker = await generationApi.workerStatus();
        if (cancelled || !worker.held) return;
        const tasks = await generationApi.listTasks();
        if (cancelled) return;
        const count = tasks.filter(
          (t) => t.status === "pending" || t.status === "running",
        ).length;
        if (count === 0) {
          await generationApi.releaseWorker();
          void queryClient.invalidateQueries({ queryKey: ["worker-status"] });
          return;
        }
        setLeftover(count);
      } catch (err) {
        // Reaching here means the server stopped answering right after
        // connecting — in which case a release call would fail too.
        // Leaving the worker held is the safe direction: nothing heavy
        // starts, and the next launch simply asks again.
        console.error("leftover-task check failed; worker stays held", err);
      }
    })();
    return () => {
      cancelled = true;
    };
  }, [mode, status.kind, queryClient]);

  if (leftover === null || !bootUpdateSettled || updatePromptOpen) return null;

  async function finish(action: () => Promise<unknown>) {
    setWorking(true);
    setError(null);
    try {
      await action();
    } catch (err) {
      // The worker is still held (cancel-then-release means a failure at
      // either step leaves the release un-sent), so keeping the choice on
      // screen is safe — nothing starts processing behind the error.
      setError(err instanceof Error ? err.message : String(err));
      setWorking(false);
      return;
    }
    void queryClient.invalidateQueries({ queryKey: ["tasks"] });
    void queryClient.invalidateQueries({ queryKey: ["worker-status"] });
    setLeftover(null);
    setWorking(false);
  }

  const resume = () => finish(() => generationApi.releaseWorker());

  // Cancel commits before the release, so the freed worker can never
  // claim a task the user just asked to cancel — and the Tasks page's 2s
  // poll can never watch a leftover start running mid-choice.
  const cancelAll = () =>
    finish(async () => {
      await generationApi.cancelAllTasks(true);
      await generationApi.releaseWorker();
    });

  return (
    // Like QuitPrompt: the two buttons are the whole choice — no overlay
    // dismiss and no Esc, because a quiet close would strand a held
    // worker with no visible way to release it.
    <div className="modal-overlay">
      <div className="modal modal-sm">
        <div className="modal-head">
          <span className="modal-title">Unfinished tasks from your last session</span>
        </div>

        <p style={{ marginBottom: 8 }}>
          {leftover} {leftover === 1 ? "task was" : "tasks were"} still queued or in
          progress when the app last closed. Resuming starts processing them now,
          which can use significant CPU/GPU for a while.
        </p>
        <p className="hint" style={{ marginBottom: 14 }}>
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
            Resume tasks
          </button>
        </div>
      </div>
    </div>
  );
}
