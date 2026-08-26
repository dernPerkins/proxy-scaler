"""Background generation worker.

Claims one task at a time from the `generation_tasks` queue (see db.py)
and processes it — a single-consumer loop that enforces "one GPU / one set
of loaded model weights at a time" by construction, not by locking.

Run via `python -m proxy_scaler.worker`. Normally you don't need to start
this yourself — `proxy_scaler.supervisor` spawns it as a managed child,
using an flock-based lock file so only one worker process is ever active
regardless of how many times something tries to (re-)spawn it.
"""

from __future__ import annotations

import sys
import time
from pathlib import Path

from . import db, pipeline, timing_db

POLL_INTERVAL_S = 2.0
HOLD_POLL_INTERVAL_S = 0.5


def _wait_while_held(
    db_path: Path | str | None = None,
    *,
    poll_interval: float = HOLD_POLL_INTERVAL_S,
) -> None:
    """Block while the supervisor-set hold flag is up (see db.py's
    worker hold/release section). Called with the worker lock already
    held — deliberately, so is_worker_running() stays true and no second
    worker can start while this one waits — and before the orphan reset,
    so leftover 'running' rows stay provable orphans that the client's
    held-mode cancel-all is allowed to cancel."""
    if not db.get_worker_hold(db_path=db_path):
        return
    print("Worker held: waiting for the client to resume or cancel leftover tasks…")
    while db.get_worker_hold(db_path=db_path):
        time.sleep(poll_interval)
    print("Worker released.")


def _process_one(
    task: db.TaskRow,
    *,
    db_path: Path | str | None = None,
    timing_db_path: Path | str | None = None,
) -> None:
    # Timing instrumentation is dev-only: without an explicit path or
    # PROXY_SCALER_TIMING_DB_PATH in the environment (make worker-dev sets
    # it) no collector exists and this function behaves exactly as before.
    timing_path = timing_db.resolve_timing_db_path(timing_db_path)
    timings = timing_db.TimingCollector() if timing_path is not None else None
    try:
        result = pipeline.process_task(task, on_progress=print, timings=timings)
        db.upsert_gallery_item_for_task(task, result, db_path=db_path)
        db.mark_task_done(task.id, db_path=db_path)
        print(f"Task {task.id} done: {result.out_path}")
        if timings is not None:
            timing_db.record_task(timings, task, "done", db_path=timing_path)
            print(f"  {timings.summary_line()}")
    except Exception as exc:  # noqa: BLE001 — one bad task must not kill the worker
        db.mark_task_failed(task.id, str(exc), db_path=db_path)
        print(f"Task {task.id} failed: {exc}", file=sys.stderr)
        if timings is not None:
            timing_db.record_task(timings, task, "failed", db_path=timing_path)
            print(f"  {timings.summary_line()}")


def main(
    *,
    db_path: Path | str | None = None,
    lock_path: Path | str | None = None,
) -> None:
    fd = db.acquire_worker_lock(lock_path)
    if fd is None:
        # A previous worker (e.g. orphaned by a force-quit mid-task) can
        # outlive its supervisor and keep the lock for minutes. Exiting here
        # would make the supervisor tear down the healthy API server too, so
        # wait instead — the OS releases the flock the moment that process
        # dies, and until then the API stays up with tasks simply queued.
        print(
            "Another worker is still running (likely shutting down); "
            "waiting for it to exit…"
        )
        while fd is None:
            time.sleep(POLL_INTERVAL_S)
            fd = db.acquire_worker_lock(lock_path)
        print("Previous worker exited; taking over.")
    _wait_while_held(db_path=db_path)
    # Holding the lock proves no other worker is mid-task, so any
    # 'running' row is an orphan from a dead worker — re-queue them.
    requeued = db.reset_orphaned_running_tasks(db_path=db_path)
    if requeued:
        print(f"Re-queued {requeued} task(s) orphaned by a previous worker.")
    print("Worker started, polling for tasks…")
    try:
        while True:
            task = db.claim_next_task(db_path=db_path)
            if task is None:
                time.sleep(POLL_INTERVAL_S)
                continue
            print(
                f"Processing task {task.id}: {task.face_name} "
                f"@ {task.dpi} DPI ({task.model})"
            )
            _process_one(task, db_path=db_path)
    finally:
        db.release_worker_lock(fd)


if __name__ == "__main__":
    import os

    # Env var overrides (unset -> None -> normal default-path behavior) so
    # a supervisor/test harness spawning this as `python -m
    # proxy_scaler.worker` can point it at an isolated DB/lock file
    # without needing a CLI arg parser here.
    main(
        db_path=os.environ.get("PROXY_SCALER_DB_PATH") or None,
        lock_path=os.environ.get("PROXY_SCALER_WORKER_LOCK_PATH") or None,
    )
