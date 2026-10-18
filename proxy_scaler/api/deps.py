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


def get_card_db_path() -> Path | str | None:
    """Same convention as get_db_path, for the Scryfall card corpus file
    (carddb.py). None falls back to carddb's own default under the data
    root — the env var exists for test isolation, not deployment."""
    return os.environ.get("PROXY_SCALER_CARD_DB_PATH") or None


def get_mpc_catalog_db_path() -> Path:
    """The MPC Fill art catalog file (mpc_catalog.py), which reads its
    own PROXY_SCALER_MPC_CATALOG_DB_PATH override so art_sources can find
    it outside a request."""
    from proxy_scaler import mpc_catalog

    return mpc_catalog.db_path_from_env()
