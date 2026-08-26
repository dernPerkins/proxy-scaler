"""Dev-only per-phase timing instrumentation for worker tasks.

Records download / model-load / inference / encode wall-clock seconds per
generation task into a standalone, disposable SQLite file — separate from
the app DB so it needs no migrations and never surfaces in the API or UI.

Recording is opt-in: it happens only when PROXY_SCALER_TIMING_DB_PATH is
set (make worker-dev sets it) or an explicit path is passed. Production
runs — the supervisor and the packaged desktop app — never set the var,
so no collector is created and no file appears.

Phases never overlap: each task runs download → model_load → inference →
encode strictly in sequence, so summing the phase columns never
double-counts. total_s can exceed the phase sum — the residual is tensor
prep, alpha reattach, thumbnails, and DB bookkeeping.

Summarize collected rows with: python -m proxy_scaler.timing_db --stats
"""

from __future__ import annotations

import argparse
import os
import sqlite3
import statistics
import sys
import time
from contextlib import contextmanager
from pathlib import Path
from typing import TYPE_CHECKING, Iterator

if TYPE_CHECKING:
    from .db import TaskRow

TIMING_DB_ENV_VAR = "PROXY_SCALER_TIMING_DB_PATH"

# Matches the path make worker-dev exports; used only as the stats CLI
# default (asking for stats implies the file should exist there).
_DEV_DEFAULT_PATH = Path("data") / "timing_debug.db"

# Canonical phase order, used for column layout and summary rendering.
PHASES = ("download", "model_load", "inference", "encode")

_SCHEMA = """
CREATE TABLE IF NOT EXISTS task_timings (
  id           INTEGER PRIMARY KEY AUTOINCREMENT,
  task_id      INTEGER,
  model        TEXT NOT NULL,
  dpi          INTEGER,
  tile_size    INTEGER,
  face_name    TEXT,
  device       TEXT,
  dtype        TEXT,
  effective_tile INTEGER,
  status       TEXT NOT NULL,
  src_width    INTEGER,
  src_height   INTEGER,
  download_s   REAL,
  model_load_s REAL,
  inference_s  REAL,
  encode_s     REAL,
  total_s      REAL NOT NULL,
  recorded_at  TEXT NOT NULL DEFAULT (datetime('now'))
);
"""


def resolve_timing_db_path(db_path: Path | str | None = None) -> Path | None:
    """Explicit arg wins, else the env var; None means recording is off."""
    if db_path is not None:
        return Path(db_path)
    env = os.environ.get(TIMING_DB_ENV_VAR)
    return Path(env) if env else None


class TimingCollector:
    """Accumulates per-phase wall-clock seconds for one worker task.

    phase() adds into a running total per name, so a phase entered
    several times (e.g. one encode per DPI variant) sums into one value,
    and a phase interrupted by an exception still records the partial
    elapsed time — failed tasks get partial rows for free.
    """

    def __init__(self) -> None:
        self.phases: dict[str, float] = {}
        self.device: str | None = None
        self.dtype: str | None = None
        self.effective_tile: int | None = None
        self.src_width: int | None = None
        self.src_height: int | None = None
        self._started = time.monotonic()

    @contextmanager
    def phase(self, name: str) -> Iterator[None]:
        t0 = time.monotonic()
        try:
            yield
        finally:
            self.phases[name] = self.phases.get(name, 0.0) + (time.monotonic() - t0)

    def set_device(self, device: str | None) -> None:
        if device:
            self.device = device

    def set_dtype(self, dtype: str | None) -> None:
        if dtype:
            self.dtype = dtype

    def set_effective_tile(self, tile: int | None) -> None:
        if tile is not None:
            self.effective_tile = tile

    def set_src_dims(self, width: int, height: int) -> None:
        self.src_width = width
        self.src_height = height

    def total(self) -> float:
        return time.monotonic() - self._started

    def summary_line(self) -> str:
        parts = [
            f"{name.replace('_', '-')} {self.phases[name]:.1f}s"
            for name in PHASES
            if name in self.phases
        ]
        parts.append(f"total {self.total():.1f}s")
        return "timings: " + " · ".join(parts)


def record_task(
    collector: TimingCollector,
    task: "TaskRow",
    status: str,
    *,
    db_path: Path | str | None = None,
) -> None:
    """Write one row for a finished task. Never raises — a timing-DB
    problem must not fail (or retroactively "unfail") the task itself."""
    try:
        path = resolve_timing_db_path(db_path)
        if path is None:
            return
        path.parent.mkdir(parents=True, exist_ok=True)
        conn = sqlite3.connect(path)
        try:
            conn.execute(_SCHEMA)
            # Files created before these columns existed get them added in
            # place — this is a disposable dev DB, but keeping the
            # already-collected rows makes before/after comparison free.
            for migration in (
                "ALTER TABLE task_timings ADD COLUMN dtype TEXT",
                "ALTER TABLE task_timings ADD COLUMN effective_tile INTEGER",
            ):
                try:
                    conn.execute(migration)
                except sqlite3.OperationalError:
                    pass  # column already there (the CREATE above or a prior run)
            conn.execute(
                """
                INSERT INTO task_timings (
                  task_id, model, dpi, tile_size, face_name, device, dtype,
                  effective_tile, status, src_width, src_height,
                  download_s, model_load_s, inference_s, encode_s, total_s
                ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
                """,
                (
                    task.id,
                    task.model,
                    task.dpi,
                    task.tile_size,
                    task.face_name,
                    collector.device,
                    collector.dtype,
                    collector.effective_tile,
                    status,
                    collector.src_width,
                    collector.src_height,
                    collector.phases.get("download"),
                    collector.phases.get("model_load"),
                    collector.phases.get("inference"),
                    collector.phases.get("encode"),
                    collector.total(),
                ),
            )
            conn.commit()
        finally:
            conn.close()
    except Exception as exc:  # noqa: BLE001 — instrumentation must never fail a task
        print(f"warning: timing record failed: {exc}", file=sys.stderr)


def compute_stats(db_path: Path | str) -> dict:
    """Aggregate task_timings grouped by (model, device).

    Returns {"groups": [...], "failed": int}. Each group has count plus
    count/mean/median/p90 per phase column and total_s, skipping NULLs.
    Python-side aggregation: SQLite lacks median/percentile, and deck-scale
    row counts (hundreds) make this a non-issue.
    """
    conn = sqlite3.connect(db_path)
    conn.row_factory = sqlite3.Row
    try:
        rows = conn.execute("SELECT * FROM task_timings").fetchall()
    finally:
        conn.close()

    failed = sum(1 for r in rows if r["status"] != "done")
    columns = [f"{name}_s" for name in PHASES] + ["total_s"]

    grouped: dict[tuple[str, str, str], list[sqlite3.Row]] = {}
    for row in rows:
        if row["status"] != "done":
            continue
        # Rows written before the dtype column existed read as fp32 — that
        # was the only dtype back then, so old/new rows group correctly.
        # effective_tile similarly reads as "?" for pre-column rows.
        cols = row.keys()
        dtype = (row["dtype"] if "dtype" in cols else None) or "fp32"
        tile = row["effective_tile"] if "effective_tile" in cols else None
        key = (row["model"], row["device"] or "?", dtype, str(tile if tile is not None else "?"))
        grouped.setdefault(key, []).append(row)

    groups = []
    for (model, device, dtype, tile), members in sorted(grouped.items()):
        stats: dict[str, dict] = {}
        for col in columns:
            values = [r[col] for r in members if r[col] is not None]
            if not values:
                continue
            stats[col] = {
                "count": len(values),
                "mean": statistics.mean(values),
                "median": statistics.median(values),
                "p90": (
                    statistics.quantiles(values, n=10)[8]
                    if len(values) >= 2
                    else values[0]
                ),
            }
        groups.append(
            {
                "model": model,
                "device": device,
                "dtype": dtype,
                "tile": tile,
                "count": len(members),
                "stats": stats,
            }
        )
    return {"groups": groups, "failed": failed}


def format_stats(result: dict) -> str:
    lines: list[str] = []
    if not result["groups"]:
        lines.append("No completed tasks recorded.")
    for group in result["groups"]:
        lines.append(
            f"{group['model']} on {group['device']} ({group['dtype']}, "
            f"tile {group['tile']}) — {group['count']} task(s)"
        )
        lines.append(f"  {'phase':<12} {'n':>4} {'mean':>8} {'median':>8} {'p90':>8}")
        for col, s in group["stats"].items():
            label = col.removesuffix("_s").replace("_", "-")
            lines.append(
                f"  {label:<12} {s['count']:>4} {s['mean']:>7.1f}s"
                f" {s['median']:>7.1f}s {s['p90']:>7.1f}s"
            )
        lines.append("")
    if result["failed"]:
        lines.append(f"{result['failed']} failed task(s) excluded from stats.")
    return "\n".join(lines).rstrip()


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(
        prog="python -m proxy_scaler.timing_db",
        description="Summarize the dev timing-debug database.",
    )
    parser.add_argument(
        "--stats", action="store_true", help="print aggregate stats (default action)"
    )
    parser.add_argument(
        "--db",
        type=Path,
        default=None,
        help=f"timing DB path (default: ${TIMING_DB_ENV_VAR} or {_DEV_DEFAULT_PATH})",
    )
    parser.add_argument(
        "--reset", action="store_true", help="delete the timing DB file"
    )
    args = parser.parse_args(argv)

    path = args.db or resolve_timing_db_path() or _DEV_DEFAULT_PATH
    if args.reset:
        if path.exists():
            path.unlink()
            print(f"Deleted {path}")
        else:
            print(f"Nothing to delete at {path}")
        return 0
    if not path.exists():
        print(f"No timing DB at {path} (run the worker via 'make worker-dev' first).")
        return 1
    print(format_stats(compute_stats(path)))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
