"""Small helpers shared across API routers."""

from __future__ import annotations

import os
from pathlib import Path


def get_db_path() -> Path | str | None:
    """Isolated-DB override for tests/supervisor — the same
    PROXY_SCALER_DB_PATH env var worker.py and supervisor.py already
    read. None falls back to db.py's own default path. Read fresh on
    every call (not cached at import time) so tests can monkeypatch the
    env var per-test."""
    return os.environ.get("PROXY_SCALER_DB_PATH") or None


def get_lock_path() -> Path | str | None:
    """Same convention as get_db_path, for the worker's flock file."""
    return os.environ.get("PROXY_SCALER_WORKER_LOCK_PATH") or None
