"""Tests for worker.py's building blocks: claim -> process -> mark done/failed.

worker.main()'s infinite polling loop itself isn't unit tested directly —
these drive the same pieces (db.claim_next_task, worker._process_one) it
calls internally, in a short deterministic sequence.
"""

from __future__ import annotations

import io
from pathlib import Path

from PIL import Image

from proxy_scaler.db import claim_next_task, enqueue_task, get_task, init_db
from proxy_scaler.worker import _process_one


def _enqueue(db_path: Path, **overrides) -> int:
    kwargs = dict(
        project_id=None,
        scryfall_id="sol-id",
        face_index=None,
        face_label=None,
        face_name="Sol Ring",
        card_name="Sol Ring",
        set_code="c21",
        collector_number="263",
        png_url="https://example.com/sol.png",
        dpi=800,
        model="swinir",
        output_dir=str(db_path.parent / "out"),
        cache_dir=str(db_path.parent / "cache"),
        weights_dir=str(db_path.parent / "weights"),
        db_path=db_path,
    )
    kwargs.update(overrides)
    return enqueue_task(kwargs.pop("project_id"), **kwargs)


def test_process_one_marks_task_done_on_success(tmp_path, monkeypatch) -> None:
    db_path = tmp_path / "test.db"
    init_db(db_path)
    tid = _enqueue(db_path)
    task = claim_next_task(db_path=db_path)
    assert task.id == tid

    buf = io.BytesIO()
    Image.new("RGB", (8, 8), color=(1, 2, 3)).save(buf, format="PNG")
    png_bytes = buf.getvalue()
    monkeypatch.setattr(
        "proxy_scaler.pipeline.download_png", lambda url, session=None: png_bytes
    )

    class FakeUpscaler:
        def __init__(self, model="swinir", scale=4, weights_dir="weights", **_kw):
            from proxy_scaler.upscale import UpscaleModel

            self.model_id = UpscaleModel(model) if isinstance(model, str) else model
            self.scale = scale

        def upscale(self, image):
            from proxy_scaler.upscale import UpscaleResult

            return UpscaleResult(image=Image.new("RGB", (32, 32)), device="cpu")

    monkeypatch.setattr("proxy_scaler.pipeline.Upscaler", FakeUpscaler)

    _process_one(task, db_path=db_path)

    done = get_task(tid, db_path=db_path)
    assert done.status == "done"
    assert done.completed_at is not None
    assert done.error is None

    # Queue is now empty — nothing left to claim.
    assert claim_next_task(db_path=db_path) is None


def test_process_one_marks_task_failed_on_exception(tmp_path, monkeypatch) -> None:
    db_path = tmp_path / "test.db"
    init_db(db_path)
    # A bad png_url that download_png can't handle causes process_task to
    # raise — the worker must not crash, just record the failure and move
    # on (see worker.py's `except Exception` around process_task).
    tid = _enqueue(db_path, png_url="")
    task = claim_next_task(db_path=db_path)

    _process_one(task, db_path=db_path)

    failed = get_task(tid, db_path=db_path)
    assert failed.status == "failed"
    assert failed.error
    assert failed.completed_at is not None
