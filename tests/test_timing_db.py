"""Tests for timing_db.py: the dev-only per-phase timing instrumentation.

Covers the collector's accumulation math, the never-raises writer, the
stats aggregation, and — via the same FakeUpscaler pattern as
test_worker.py — that _process_one records a row only when a timing DB
path is configured.
"""

from __future__ import annotations

import io
import sqlite3
from pathlib import Path

import pytest
from PIL import Image

from proxy_scaler.db import claim_next_task, enqueue_task, init_db
from proxy_scaler.timing_db import (
    TIMING_DB_ENV_VAR,
    TimingCollector,
    compute_stats,
    record_task,
    resolve_timing_db_path,
)
from proxy_scaler.worker import _process_one


def _enqueue(db_path: Path, **overrides) -> int:
    kwargs = dict(
        project_tag=None,
        scryfall_id="sol-id",
        face_index=None,
        face_label=None,
        face_name="Sol Ring",
        card_name="Sol Ring",
        set_code="c21",
        collector_number="263",
        png_url="https://example.com/sol.png",
        dpi=800,
        model="ultrasharp_v2",
        output_dir=str(db_path.parent / "out"),
        cache_dir=str(db_path.parent / "cache"),
        weights_dir=str(db_path.parent / "weights"),
        db_path=db_path,
    )
    kwargs.update(overrides)
    return enqueue_task(kwargs.pop("project_tag"), **kwargs)


def _claimed_task(tmp_path: Path):
    db_path = tmp_path / "test.db"
    init_db(db_path)
    _enqueue(db_path)
    return db_path, claim_next_task(db_path=db_path)


def _patch_pipeline_fakes(monkeypatch) -> None:
    buf = io.BytesIO()
    Image.new("RGB", (8, 8), color=(1, 2, 3)).save(buf, format="PNG")
    png_bytes = buf.getvalue()
    monkeypatch.setattr(
        "proxy_scaler.pipeline.download_png", lambda url, session=None: png_bytes
    )

    class FakeUpscaler:
        def __init__(self, model="ultrasharp_v2", scale=4, weights_dir="weights", **_kw):
            from proxy_scaler.upscale import UpscaleModel

            self.model_id = UpscaleModel(model) if isinstance(model, str) else model
            self.scale = scale

        def upscale(self, image):
            from proxy_scaler.upscale import UpscaleResult

            return UpscaleResult(image=Image.new("RGB", (32, 32)), device="cpu")

    monkeypatch.setattr("proxy_scaler.pipeline.Upscaler", FakeUpscaler)


def _timing_rows(path: Path) -> list[sqlite3.Row]:
    conn = sqlite3.connect(path)
    conn.row_factory = sqlite3.Row
    try:
        return conn.execute("SELECT * FROM task_timings").fetchall()
    finally:
        conn.close()


def test_collector_accumulates_repeated_phases() -> None:
    c = TimingCollector()
    with c.phase("encode"):
        pass
    first = c.phases["encode"]
    with c.phase("encode"):
        pass
    assert c.phases["encode"] >= first  # summed, not replaced
    assert "download" not in c.phases  # unentered phases stay absent
    assert c.total() >= 0.0


def test_collector_records_partial_time_on_exception() -> None:
    c = TimingCollector()
    with pytest.raises(RuntimeError):
        with c.phase("download"):
            raise RuntimeError("boom")
    assert "download" in c.phases
    assert c.phases["download"] >= 0.0


def test_summary_line_omits_missing_phases() -> None:
    c = TimingCollector()
    with c.phase("inference"):
        pass
    line = c.summary_line()
    assert line.startswith("timings: ")
    assert "inference" in line
    assert "total" in line
    assert "download" not in line
    assert "model-load" not in line


def test_resolve_timing_db_path_disabled_without_env(monkeypatch) -> None:
    monkeypatch.delenv(TIMING_DB_ENV_VAR, raising=False)
    assert resolve_timing_db_path() is None
    assert resolve_timing_db_path("x.db") == Path("x.db")
    monkeypatch.setenv(TIMING_DB_ENV_VAR, "y.db")
    assert resolve_timing_db_path() == Path("y.db")


def test_record_task_writes_wide_row(tmp_path) -> None:
    db_path, task = _claimed_task(tmp_path)
    c = TimingCollector()
    c.phases = {"download": 1.5, "encode": 0.5}
    c.set_device("cpu")
    c.set_src_dims(745, 1040)

    timing_path = tmp_path / "timing.db"
    record_task(c, task, "done", db_path=timing_path)

    [row] = _timing_rows(timing_path)
    assert row["task_id"] == task.id
    assert row["model"] == "ultrasharp_v2"
    assert row["dpi"] == 800
    assert row["face_name"] == "Sol Ring"
    assert row["device"] == "cpu"
    assert row["status"] == "done"
    assert row["src_width"] == 745
    assert row["src_height"] == 1040
    assert row["download_s"] == 1.5
    assert row["encode_s"] == 0.5
    assert row["model_load_s"] is None
    assert row["inference_s"] is None
    assert row["total_s"] >= 0.0


def test_record_task_never_raises(tmp_path, capsys) -> None:
    db_path, task = _claimed_task(tmp_path)
    blocker = tmp_path / "not-a-dir"
    blocker.write_text("file where a directory is needed")
    # Parent of the target is a plain file -> mkdir/connect must fail, but
    # only a stderr warning may escape.
    record_task(TimingCollector(), task, "done", db_path=blocker / "timing.db")
    assert "timing record failed" in capsys.readouterr().err


def test_compute_stats_groups_by_model_device(tmp_path) -> None:
    db_path, task = _claimed_task(tmp_path)
    timing_path = tmp_path / "timing.db"

    for inference in (2.0, 4.0, 6.0):
        c = TimingCollector()
        c.phases = {"inference": inference, "encode": 1.0}
        c.set_device("gpu")
        record_task(c, task, "done", db_path=timing_path)
    # Different device -> separate group; NULL inference must be skipped.
    c = TimingCollector()
    c.phases = {"encode": 3.0}
    c.set_device("cpu")
    record_task(c, task, "done", db_path=timing_path)
    # Failed row -> excluded from stats, counted separately.
    record_task(TimingCollector(), task, "failed", db_path=timing_path)

    result = compute_stats(timing_path)
    assert result["failed"] == 1
    by_key = {(g["model"], g["device"]): g for g in result["groups"]}
    gpu = by_key[("ultrasharp_v2", "gpu")]
    assert gpu["count"] == 3
    assert gpu["stats"]["inference_s"]["count"] == 3
    assert gpu["stats"]["inference_s"]["mean"] == pytest.approx(4.0)
    assert gpu["stats"]["inference_s"]["median"] == pytest.approx(4.0)
    cpu = by_key[("ultrasharp_v2", "cpu")]
    assert cpu["count"] == 1
    assert "inference_s" not in cpu["stats"]
    assert cpu["stats"]["encode_s"]["mean"] == pytest.approx(3.0)


def test_process_one_records_timing_row_when_path_given(tmp_path, monkeypatch) -> None:
    monkeypatch.delenv(TIMING_DB_ENV_VAR, raising=False)
    db_path, task = _claimed_task(tmp_path)
    _patch_pipeline_fakes(monkeypatch)

    timing_path = tmp_path / "timing.db"
    _process_one(task, db_path=db_path, timing_db_path=timing_path)

    [row] = _timing_rows(timing_path)
    assert row["task_id"] == task.id
    assert row["status"] == "done"
    assert row["device"] == "cpu"  # from FakeUpscaler's UpscaleResult
    assert row["download_s"] is not None
    assert row["encode_s"] is not None
    assert row["total_s"] is not None
    # FakeUpscaler never enters the real model-load/inference phases.
    assert row["model_load_s"] is None
    assert row["inference_s"] is None


def test_process_one_records_failed_row(tmp_path, monkeypatch) -> None:
    monkeypatch.delenv(TIMING_DB_ENV_VAR, raising=False)
    db_path = tmp_path / "test.db"
    init_db(db_path)
    _enqueue(db_path, png_url="")  # download_png can't handle it -> failure
    task = claim_next_task(db_path=db_path)

    timing_path = tmp_path / "timing.db"
    _process_one(task, db_path=db_path, timing_db_path=timing_path)

    [row] = _timing_rows(timing_path)
    assert row["status"] == "failed"
    assert row["total_s"] is not None


def test_process_one_records_nothing_when_disabled(tmp_path, monkeypatch) -> None:
    monkeypatch.delenv(TIMING_DB_ENV_VAR, raising=False)
    db_path, task = _claimed_task(tmp_path)
    _patch_pipeline_fakes(monkeypatch)

    _process_one(task, db_path=db_path)

    assert not list(tmp_path.glob("**/timing*.db"))


def test_process_one_uses_env_var_path(tmp_path, monkeypatch) -> None:
    db_path, task = _claimed_task(tmp_path)
    _patch_pipeline_fakes(monkeypatch)

    timing_path = tmp_path / "env-timing.db"
    monkeypatch.setenv(TIMING_DB_ENV_VAR, str(timing_path))
    _process_one(task, db_path=db_path)

    [row] = _timing_rows(timing_path)
    assert row["status"] == "done"
