"""In-memory registry for long-running render jobs — PDF sheets and
re-rendered ZIP exports alike.

Rendering a print sheet is slow in a way the user needs to see: the
per-image cost in pdf_layout.build_pdf's loop (open, corner-flatten,
resize to export DPI, bleed, JPEG encode) measures ~0.7s, so a 60-unique-
card deck spends ~45s rendering before a single byte can be sent. A ZIP
export that adds bleed or converts to JPG pays the same per-image price
(see routers/export.py). A plain synchronous POST gives the client nothing
to show for that. Instead the render runs on its own thread against a job
recorded here, and the client polls for (completed, total) while it works.

One registry for both kinds on purpose: active_count() is what keeps
renders to one at a time, and a PDF and a ZIP competing for the CPU (and
the disk) is exactly the pile-up that rule exists to prevent.

Deliberately in-memory rather than a `generation_tasks`-style table: that
table is shaped for card generation (scryfall_id/model/dpi/weights_dir)
and is the durable queue a separate worker process consumes. These jobs
are neither durable nor cross-process — a finished PDF is bytes held for
one client that is actively waiting, worthless after a restart. The API
runs as a single uvicorn process (supervisor.py's `uvicorn.run(app, ...)`
passes no `workers=`), so one process's dict is the whole world.

A finished job holds its result one of two ways. A PDF is bytes in
memory (`data`); a ZIP — which can run to a gigabyte of bled 1200 DPI
PNGs — is a temp file the registry owns (`path`). Either way eviction is
not optional: pop_result() / pop_result_file() drop a job as soon as its
result is handed over, and create_job() sweeps anything older than
_TTL_SECONDS for the case where the client never comes back for it. A
swept, cancelled or failed file job unlinks its file; a popped one hands
ownership to the caller (the response that streams it).
"""

from __future__ import annotations

import os
import threading
import time
import uuid
from dataclasses import dataclass, field
from pathlib import Path

# Long enough that a slow render followed by a slow save-dialog decision
# still finds its result, short enough that an abandoned 80MB buffer isn't
# held for the life of the process.
_TTL_SECONDS = 15 * 60

RENDERING = "rendering"
DONE = "done"
FAILED = "failed"
CANCELED = "canceled"


class PdfRenderCanceled(Exception):
    """Raised out of a render's progress callback once cancellation is
    requested, unwinding build_pdf from wherever it happens to be.

    Signalling through the callback (rather than passing build_pdf a job
    id or a flag to poll) keeps the render functions unaware that jobs
    exist at all — they stay plain "pages in, bytes out" and remain
    directly callable from the CLI and tests.
    """


@dataclass
class PdfJob:
    id: str
    filename: str
    total: int
    status: str = RENDERING
    completed: int = 0
    # Exactly one of these is set once DONE: bytes for a PDF, a temp file
    # the registry owns for a ZIP.
    data: bytes | None = None
    path: Path | None = None
    error: str | None = None
    cancel_requested: bool = False
    updated_at: float = field(default_factory=time.monotonic)


_JOBS: dict[str, PdfJob] = {}
_LOCK = threading.Lock()


def _unlink_quiet(path: Path | None) -> None:
    """Remove a job's temp file if it still exists. The file may already be
    gone (the response that streamed it unlinks too), and a leak here is
    not worth failing a status update over."""
    if path is None:
        return
    try:
        os.unlink(path)
    except OSError:
        pass


def _drop_file_locked(job: PdfJob) -> None:
    """Forget a job's file and delete it. Caller must hold _LOCK."""
    _unlink_quiet(job.path)
    job.path = None


def _sweep_expired_locked() -> None:
    """Drop jobs untouched for _TTL_SECONDS. Caller must hold _LOCK."""
    cutoff = time.monotonic() - _TTL_SECONDS
    for job_id in [jid for jid, job in _JOBS.items() if job.updated_at < cutoff]:
        _drop_file_locked(_JOBS[job_id])
        del _JOBS[job_id]


def create_job(*, filename: str, total: int) -> PdfJob:
    """Register a new job in the RENDERING state and return it."""
    job = PdfJob(id=uuid.uuid4().hex, filename=filename, total=total)
    with _LOCK:
        _sweep_expired_locked()
        _JOBS[job.id] = job
    return job


def get(job_id: str) -> PdfJob | None:
    with _LOCK:
        return _JOBS.get(job_id)


def set_progress(job_id: str, completed: int) -> None:
    with _LOCK:
        job = _JOBS.get(job_id)
        if job is None:
            return
        job.completed = completed
        job.updated_at = time.monotonic()


def finish(job_id: str, data: bytes) -> None:
    with _LOCK:
        job = _JOBS.get(job_id)
        if job is None:
            return
        job.status = DONE
        job.data = data
        job.completed = job.total
        job.updated_at = time.monotonic()


def finish_file(job_id: str, path: Path) -> bool:
    """Record a finished render whose result lives in a temp file the
    registry now owns. Returns False for an unknown (evicted) job — the
    caller still holds the file in that case and must delete it, or a
    cancel that raced the last write leaks an archive in $TMPDIR."""
    with _LOCK:
        job = _JOBS.get(job_id)
        if job is None:
            return False
        job.status = DONE
        job.path = path
        job.completed = job.total
        job.updated_at = time.monotonic()
        return True


def fail(job_id: str, error: str) -> None:
    with _LOCK:
        job = _JOBS.get(job_id)
        if job is None:
            return
        job.status = FAILED
        job.error = error
        _drop_file_locked(job)
        job.updated_at = time.monotonic()


def mark_canceled(job_id: str) -> None:
    with _LOCK:
        job = _JOBS.get(job_id)
        if job is None:
            return
        job.status = CANCELED
        job.data = None
        _drop_file_locked(job)
        job.updated_at = time.monotonic()


def request_cancel(job_id: str) -> bool:
    """Ask a running render to stop. Returns False for an unknown job.

    Only sets the flag — the render thread notices at its next progress
    callback and raises PdfRenderCanceled, so the job reaches CANCELED
    through the same path as any other failure rather than being torn
    down from underneath.
    """
    with _LOCK:
        job = _JOBS.get(job_id)
        if job is None:
            return False
        job.cancel_requested = True
        job.updated_at = time.monotonic()
        return True


def is_cancel_requested(job_id: str) -> bool:
    with _LOCK:
        job = _JOBS.get(job_id)
        return job is not None and job.cancel_requested


def pop_result(job_id: str) -> tuple[str, bytes] | None:
    """Take a finished job's (filename, bytes) and evict it.

    Evicting on read is the main defence against accumulating multi-MB
    buffers: the client asks for the result exactly once, immediately
    after seeing status DONE. Returns None if the job is unknown or not
    finished, leaving an unfinished job in place.
    """
    with _LOCK:
        job = _JOBS.get(job_id)
        if job is None or job.status != DONE or job.data is None:
            return None
        del _JOBS[job_id]
        return job.filename, job.data


def pop_result_file(job_id: str) -> tuple[str, Path] | None:
    """File-job twin of pop_result: take a finished job's (filename, path)
    and evict it. Ownership of the file moves to the caller — the registry
    will not unlink it after this — so the caller must, once it has been
    streamed. Returns None if the job is unknown, not finished, or holds
    bytes rather than a file."""
    with _LOCK:
        job = _JOBS.get(job_id)
        if job is None or job.status != DONE or job.path is None:
            return None
        del _JOBS[job_id]
        return job.filename, job.path


def active_count() -> int:
    """How many renders are currently in flight — used to refuse a second
    concurrent job rather than letting several large buffers pile up."""
    with _LOCK:
        return sum(1 for job in _JOBS.values() if job.status == RENDERING)


def _reset_for_tests() -> None:
    with _LOCK:
        for job in _JOBS.values():
            _drop_file_locked(job)
        _JOBS.clear()
