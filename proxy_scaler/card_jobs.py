"""In-memory registry for long-running card-database import jobs.

Importing a Scryfall bulk dump is minutes of work (a 77–400MB download
followed by parsing a couple of million JSONL rows into SQLite), so it runs
on its own thread and the client polls for phase + progress — the same
shape as pdf_jobs.py, and deliberately a parallel module rather than a
generalization of it: the two jobs share nothing but "a dict and a lock"
(a PDF job's payload is result bytes popped once; this job's payload is
counters and its result is the corpus database itself on disk).

Like pdf_jobs, this registry is in-memory and single-process: the API runs
as a single uvicorn process (supervisor.py passes no `workers=`), so one
process's dict is the whole world. Finished jobs linger for _TTL_SECONDS
so a polling client always sees the terminal state before eviction.
"""

from __future__ import annotations

import threading
import time
import uuid
from dataclasses import dataclass, field

_TTL_SECONDS = 15 * 60

RUNNING = "running"
DONE = "done"
FAILED = "failed"
CANCELED = "canceled"

# Phases, in order. "checking" = asking Scryfall's catalog where today's
# dump lives + disk guardrails; then the download, the row import, and the
# final meta write.
CHECKING = "checking"
DOWNLOADING = "downloading"
IMPORTING = "importing"
FINALIZING = "finalizing"


class CardImportCanceled(Exception):
    """Raised inside the import thread when cancellation is requested, so
    the job reaches CANCELED through the same unwinding path as a failure
    rather than being torn down from outside."""


@dataclass
class CardImportJob:
    id: str
    dataset: str  # "default_cards" | "all_cards"
    status: str = RUNNING
    phase: str = CHECKING
    bytes_downloaded: int = 0
    total_bytes: int | None = None  # compressed_size from the bulk catalog
    rows_imported: int = 0
    error: str | None = None
    cancel_requested: bool = False
    updated_at: float = field(default_factory=time.monotonic)


_JOBS: dict[str, CardImportJob] = {}
_LOCK = threading.Lock()


def _sweep_expired_locked() -> None:
    cutoff = time.monotonic() - _TTL_SECONDS
    for job_id in [jid for jid, job in _JOBS.items() if job.updated_at < cutoff]:
        del _JOBS[job_id]


def create_job(*, dataset: str) -> CardImportJob:
    job = CardImportJob(id=uuid.uuid4().hex, dataset=dataset)
    with _LOCK:
        _sweep_expired_locked()
        _JOBS[job.id] = job
    return job


def get(job_id: str) -> CardImportJob | None:
    with _LOCK:
        return _JOBS.get(job_id)


def set_phase(job_id: str, phase: str) -> None:
    with _LOCK:
        job = _JOBS.get(job_id)
        if job is None:
            return
        job.phase = phase
        job.updated_at = time.monotonic()


def set_download_progress(job_id: str, bytes_downloaded: int, total_bytes: int | None) -> None:
    with _LOCK:
        job = _JOBS.get(job_id)
        if job is None:
            return
        job.bytes_downloaded = bytes_downloaded
        job.total_bytes = total_bytes
        job.updated_at = time.monotonic()


def set_import_progress(job_id: str, rows_imported: int) -> None:
    with _LOCK:
        job = _JOBS.get(job_id)
        if job is None:
            return
        job.rows_imported = rows_imported
        job.updated_at = time.monotonic()


def finish(job_id: str) -> None:
    with _LOCK:
        job = _JOBS.get(job_id)
        if job is None:
            return
        job.status = DONE
        job.updated_at = time.monotonic()


def fail(job_id: str, error: str) -> None:
    with _LOCK:
        job = _JOBS.get(job_id)
        if job is None:
            return
        job.status = FAILED
        job.error = error
        job.updated_at = time.monotonic()


def mark_canceled(job_id: str) -> None:
    with _LOCK:
        job = _JOBS.get(job_id)
        if job is None:
            return
        job.status = CANCELED
        job.updated_at = time.monotonic()


def request_cancel(job_id: str) -> bool:
    """Ask a running import to stop. Returns False for an unknown job. Only
    sets the flag — the import thread notices at its next chunk/batch
    boundary and raises CardImportCanceled itself."""
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


def active_job() -> CardImportJob | None:
    """The one running import, if any — used both to refuse a second
    concurrent import and to let /api/cards/status point a fresh client at
    a job some other window started."""
    with _LOCK:
        for job in _JOBS.values():
            if job.status == RUNNING:
                return job
        return None


def _reset_for_tests() -> None:
    with _LOCK:
        _JOBS.clear()
