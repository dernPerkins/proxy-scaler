"""In-memory registry for MPC Fill catalog update jobs.

The same shape as card_jobs.py (a thread does the work, the client polls
phase + progress, finished jobs linger for _TTL_SECONDS), kept separate so
a catalog update never reads as a card-database import: the client
blocks the whole app during the latter, but searches keep working during
the former (they read the old catalog until the new one commits).
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

# Phases, in order: reading the manifest, downloading the changed source
# shards, writing them into the catalog.
CHECKING = "checking"
DOWNLOADING = "downloading"
IMPORTING = "importing"


class CatalogImportCanceled(Exception):
    """Raised inside the import thread when cancellation is requested."""


@dataclass
class CatalogImportJob:
    id: str
    status: str = RUNNING
    phase: str = CHECKING
    bytes_downloaded: int = 0
    total_bytes: int | None = None
    rows_imported: int = 0
    error: str | None = None
    cancel_requested: bool = False
    updated_at: float = field(default_factory=time.monotonic)


_JOBS: dict[str, CatalogImportJob] = {}
_LOCK = threading.Lock()


def _sweep_expired_locked() -> None:
    cutoff = time.monotonic() - _TTL_SECONDS
    for job_id in [jid for jid, job in _JOBS.items() if job.updated_at < cutoff]:
        del _JOBS[job_id]


def create_job() -> CatalogImportJob:
    job = CatalogImportJob(id=uuid.uuid4().hex)
    with _LOCK:
        _sweep_expired_locked()
        _JOBS[job.id] = job
    return job


def get(job_id: str) -> CatalogImportJob | None:
    with _LOCK:
        return _JOBS.get(job_id)


def update(job_id: str, **changes: object) -> None:
    """Set any of the job's progress fields (phase, bytes_downloaded,
    total_bytes, rows_imported)."""
    with _LOCK:
        job = _JOBS.get(job_id)
        if job is None:
            return
        for name, value in changes.items():
            setattr(job, name, value)
        job.updated_at = time.monotonic()


def finish(job_id: str) -> None:
    update(job_id, status=DONE)


def fail(job_id: str, error: str) -> None:
    update(job_id, status=FAILED, error=error)


def mark_canceled(job_id: str) -> None:
    update(job_id, status=CANCELED)


def request_cancel(job_id: str) -> bool:
    """Ask a running job to stop; False for an unknown job. The thread
    notices at its next shard or batch boundary."""
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


def active_job() -> CatalogImportJob | None:
    with _LOCK:
        for job in _JOBS.values():
            if job.status == RUNNING:
                return job
        return None


def _reset_for_tests() -> None:
    with _LOCK:
        _JOBS.clear()
