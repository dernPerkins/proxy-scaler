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

from . import db, pipeline

POLL_INTERVAL_S = 2.0


def _process_one(task: db.TaskRow, *, db_path: Path | str | None = None) -> None:
    try:
        result = pipeline.process_task(task, on_progress=print)
        db.upsert_gallery_item_for_task(task, result, db_path=db_path)
        db.mark_task_done(task.id, db_path=db_path)
        print(f"Task {task.id} done: {result.out_path}")
    except Exception as exc:  # noqa: BLE001 — one bad task must not kill the worker
        db.mark_task_failed(task.id, str(exc), db_path=db_path)
        print(f"Task {task.id} failed: {exc}", file=sys.stderr)


def main(
    *,
    db_path: Path | str | None = None,
    lock_path: Path | str | None = None,
) -> None:
    fd = db.acquire_worker_lock(lock_path)
    if fd is None:
        print("Another worker is already running; exiting.")
        return
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
