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

import json
import sys
import threading
import time
from pathlib import Path
from typing import Callable

from . import db, pipeline, timing_db
from .dpi import ORIGINAL_MODEL

POLL_INTERVAL_S = 2.0
HOLD_POLL_INTERVAL_S = 0.5
# Upper bound on waiting for an in-flight finish (encode + DB writes) when
# the worker exits; a healthy finish takes a few seconds.
FINISH_JOIN_TIMEOUT_S = 60.0


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


def _start_one(
    task: db.TaskRow,
    *,
    db_path: Path | str | None = None,
    timing_db_path: Path | str | None = None,
) -> Callable[[], None]:
    """Run a task's download + GPU inference synchronously; return the
    finish step (encode + gallery upsert + mark done + timing record) as a
    zero-arg callable, so the main loop can run it on a finisher thread
    while the next task's inference starts. If the synchronous half raises,
    the task is marked failed here and the returned callable is a no-op.

    The TimingCollector hands off cleanly: this thread's last touch is
    inside process_task, and only the caller of the returned finish touches
    it afterwards — sequential, never concurrent."""
    # Timing instrumentation is dev-only: without an explicit path or
    # PROXY_SCALER_TIMING_DB_PATH in the environment (make worker-dev sets
    # it) no collector exists and this function behaves exactly as before.
    timing_path = timing_db.resolve_timing_db_path(timing_db_path)
    timings = timing_db.TimingCollector() if timing_path is not None else None

    def _record(status: str) -> None:
        if timings is not None:
            timing_db.record_task(timings, task, status, db_path=timing_path)
            print(f"  {timings.summary_line()}")

    def on_cpu_fallback() -> None:
        # Fired by the Upscaler the moment a GPU→CPU OOM fallback happens —
        # flags it for the client immediately (the desktop app polls
        # /api/worker/status and raises a "cancel pending tasks?" dialog)
        # instead of letting a slow CPU run pile up unnoticed.
        db.set_cpu_fallback(
            json.dumps(
                {
                    "at": db._utc_now(),
                    "task_id": task.id,
                    "face_name": task.face_name,
                    "model": task.model,
                }
            ),
            db_path=db_path,
        )

    try:
        pending = pipeline.process_task(
            task,
            on_progress=print,
            timings=timings,
            defer_finish=True,
            on_cpu_fallback=on_cpu_fallback,
        )
    except Exception as exc:  # noqa: BLE001 — one bad task must not kill the worker
        db.mark_task_failed(task.id, str(exc), db_path=db_path)
        print(f"Task {task.id} failed: {exc}", file=sys.stderr)
        _record("failed")
        return lambda: None

    def finish() -> None:
        try:
            result = pending.finish()
            db.upsert_gallery_item_for_task(task, result, db_path=db_path)
            db.mark_task_done(task.id, db_path=db_path)
            print(f"Task {task.id} done: {result.out_path}")
            _record("done")
        except Exception as exc:  # noqa: BLE001 — a bad finish must not kill the worker
            db.mark_task_failed(task.id, str(exc), db_path=db_path)
            print(f"Task {task.id} failed: {exc}", file=sys.stderr)
            _record("failed")

    return finish


def _process_one(
    task: db.TaskRow,
    *,
    db_path: Path | str | None = None,
    timing_db_path: Path | str | None = None,
) -> None:
    """Synchronous claim-to-done processing of one task (tests use this;
    main() overlaps the two halves across threads instead)."""
    _start_one(task, db_path=db_path, timing_db_path=timing_db_path)()


class _OriginalPrefetcher:
    """Warms the originals cache for the NEXT pending task while the GPU
    works on the current one. At most one in-flight prefetch; kick() is
    non-blocking and simply drops the request if one is already running.
    Daemon thread: every write it makes is atomic and idempotent, so dying
    mid-flight loses nothing."""

    def __init__(self, *, db_path: Path | str | None = None) -> None:
        self._db_path = db_path
        self._thread: threading.Thread | None = None

    def kick(self, *, current_face: tuple[str, int | None]) -> None:
        if self._thread is not None and self._thread.is_alive():
            return
        self._thread = threading.Thread(
            target=self._run,
            args=(current_face,),
            name="original-prefetch",
            daemon=True,
        )
        self._thread.start()

    def _run(self, current_face: tuple[str, int | None]) -> None:
        try:
            nxt = db.peek_next_pending(db_path=self._db_path)
            if nxt is None or nxt.model == ORIGINAL_MODEL:
                # Download tasks overwrite their original unconditionally —
                # a prefetch would just download twice.
                return
            if (nxt.scryfall_id, nxt.face_index) == current_face:
                # Multi-DPI sibling of the task being processed right now:
                # the main thread is already fetching this exact face.
                return
            pipeline.prefetch_original(nxt)
        except Exception as exc:  # noqa: BLE001 — best-effort; the task downloads for itself
            print(f"prefetch: {exc}", file=sys.stderr)


def main(
    *,
    db_path: Path | str | None = None,
    lock_path: Path | str | None = None,
) -> None:
    # The supervisor stops this process with SIGTERM (then SIGKILL after
    # SHUTDOWN_GRACE_S). Converting SIGTERM to SystemExit lets the finally
    # below drain an in-flight finish thread (a few seconds of encode + DB
    # writes) inside that grace window instead of losing it and redoing the
    # task on next start. Correctness never depends on this — atomic writes
    # plus the orphaned-running requeue cover the SIGKILL case.
    import signal

    try:
        signal.signal(signal.SIGTERM, lambda *_: sys.exit(0))
    except (ValueError, OSError):  # non-main thread / exotic platform
        pass
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
    # A fresh worker retries the GPU, so a stale CPU-fallback flag from a
    # previous run describes a condition that no longer holds (mirrors the
    # supervisor clearing stale holds); if the fallback recurs it re-fires
    # within the first task.
    db.clear_cpu_fallback(db_path=db_path)
    print("Worker started, polling for tasks…")
    prefetcher = _OriginalPrefetcher(db_path=db_path)
    finish_thread: threading.Thread | None = None
    prev_sibling_key: tuple[str, int | None, str] | None = None
    try:
        while True:
            task = db.claim_next_task(db_path=db_path)
            if task is None:
                if finish_thread is not None:
                    # Idle worker = fully quiesced: the last task's encode
                    # completes before we report an empty queue by sleeping.
                    finish_thread.join()
                    finish_thread = None
                time.sleep(POLL_INTERVAL_S)
                continue
            print(
                f"Processing task {task.id}: {task.face_name} "
                f"@ {task.dpi} DPI ({task.model})"
            )
            # While this task is on the GPU, warm the NEXT task's original…
            prefetcher.kick(current_face=(task.scryfall_id, task.face_index))
            # …and the PREVIOUS task's finish thread is still encoding —
            # UNLESS this task is a sibling DPI of that previous task: a
            # non-forced sibling reads the x4 cache PNG the finisher is
            # still writing, so wait for it to land and turn the sibling's
            # model pass into a cache hit. Only sibling pairs serialize
            # here, and they're exactly the pairs whose inference the
            # cache hit eliminates — a large net win.
            if finish_thread is not None and prev_sibling_key == (
                task.scryfall_id,
                task.face_index,
                task.model,
            ):
                finish_thread.join()
                finish_thread = None
            prev_sibling_key = (task.scryfall_id, task.face_index, task.model)
            finish = _start_one(task, db_path=db_path)
            if finish_thread is not None:
                # Backpressure: at most one outstanding finish (each holds a
                # full-size upscaled image, ~50MB, until written out).
                finish_thread.join()
            finish_thread = threading.Thread(
                target=finish, name=f"finish-task-{task.id}", daemon=False
            )
            finish_thread.start()
    finally:
        if finish_thread is not None:
            finish_thread.join(FINISH_JOIN_TIMEOUT_S)
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
