"""Tests for worker.py's building blocks: claim -> process -> mark done/failed.

worker.main()'s infinite polling loop itself isn't unit tested directly —
these drive the same pieces (db.claim_next_task, worker._process_one) it
calls internally, in a short deterministic sequence.
"""

from __future__ import annotations

import io
import threading
from pathlib import Path

from PIL import Image

from proxy_scaler.db import (
    claim_next_task,
    enqueue_task,
    get_task,
    init_db,
    release_worker_hold,
    set_worker_hold,
)
from proxy_scaler.worker import _process_one, _wait_while_held


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
        def __init__(self, model="ultrasharp_v2", scale=4, weights_dir="weights", **_kw):
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


def test_process_one_download_task_upserts_original_gallery_row(
    tmp_path, monkeypatch
) -> None:
    """A download task flows through the exact same worker path as an
    upscale — done + a gallery row — with the sentinel (300, "original")
    variant key and out_path pointing at the cached original itself."""
    from proxy_scaler import db as db_module
    from proxy_scaler.dpi import ORIGINAL_DPI, ORIGINAL_MODEL
    from proxy_scaler.upscale import original_cache_path

    db_path = tmp_path / "test.db"
    init_db(db_path)
    tid = _enqueue(db_path, project_tag="tag-a", dpi=ORIGINAL_DPI, model=ORIGINAL_MODEL)
    task = claim_next_task(db_path=db_path)
    assert task.id == tid

    buf = io.BytesIO()
    Image.new("RGB", (8, 8), color=(1, 2, 3)).save(buf, format="PNG")
    png_bytes = buf.getvalue()
    monkeypatch.setattr(
        "proxy_scaler.pipeline.download_png", lambda url, session=None: png_bytes
    )

    _process_one(task, db_path=db_path)

    done = get_task(tid, db_path=db_path)
    assert done.status == "done"

    [item] = db_module.list_gallery_items("tag-a", db_path=db_path)
    assert item["model"] == ORIGINAL_MODEL
    assert item["dpi"] == ORIGINAL_DPI
    expected = original_cache_path(Path(task.cache_dir), "sol-id", None)
    assert Path(item["out_path"]) == expected
    assert Path(item["original_path"]) == expected
    assert expected.is_file()


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


def test_wait_while_held_returns_immediately_when_not_held(tmp_path) -> None:
    db_path = tmp_path / "test.db"
    init_db(db_path)
    # No hold row at all (headless/remote shape) — must not block.
    _wait_while_held(db_path=db_path)


def test_wait_while_held_blocks_until_released(tmp_path) -> None:
    db_path = tmp_path / "test.db"
    init_db(db_path)
    set_worker_hold(True, db_path=db_path)

    # The release arrives from "the API server" (another thread here)
    # while the worker-side wait is polling.
    releaser = threading.Timer(0.1, release_worker_hold, kwargs={"db_path": db_path})
    releaser.start()
    try:
        _wait_while_held(db_path=db_path, poll_interval=0.02)
    finally:
        releaser.cancel()


# --- deferred finish / prefetch (see #4: overlap I/O with GPU work) ----------


def _patch_upscale_fakes(monkeypatch) -> bytes:
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
    return png_bytes


def test_start_one_leaves_task_running_until_finish_marks_done(
    tmp_path, monkeypatch
) -> None:
    from proxy_scaler import db as db_module
    from proxy_scaler.worker import _start_one

    db_path = tmp_path / "test.db"
    init_db(db_path)
    tid = _enqueue(db_path, project_tag="tag-defer")
    task = claim_next_task(db_path=db_path)
    _patch_upscale_fakes(monkeypatch)

    finish = _start_one(task, db_path=db_path)

    # Inference done, but nothing finalized yet.
    assert get_task(tid, db_path=db_path).status == "running"
    assert db_module.list_gallery_items("tag-defer", db_path=db_path) == []

    finish()

    done = get_task(tid, db_path=db_path)
    assert done.status == "done"
    # Two rows land at finish: the upscaled variant, and the cached
    # original registered alongside it (see upsert_gallery_item_for_task).
    items = db_module.list_gallery_items("tag-defer", db_path=db_path)
    models = sorted(i["model"] for i in items)
    assert models == ["original", task.model]
    for item in items:
        assert Path(item["out_path"]).is_file()


def test_start_one_inference_failure_fails_immediately(tmp_path) -> None:
    from proxy_scaler.worker import _start_one

    db_path = tmp_path / "test.db"
    init_db(db_path)
    tid = _enqueue(db_path, png_url="")  # download_png can't handle it
    task = claim_next_task(db_path=db_path)

    finish = _start_one(task, db_path=db_path)

    failed = get_task(tid, db_path=db_path)
    assert failed.status == "failed"
    assert failed.error
    finish()  # returned callable is a harmless no-op
    assert get_task(tid, db_path=db_path).status == "failed"


def test_finish_failure_marks_task_failed(tmp_path, monkeypatch) -> None:
    from proxy_scaler.worker import _start_one

    db_path = tmp_path / "test.db"
    init_db(db_path)
    tid = _enqueue(db_path)
    task = claim_next_task(db_path=db_path)
    _patch_upscale_fakes(monkeypatch)

    def boom(**_kw):
        raise RuntimeError("disk full")

    monkeypatch.setattr("proxy_scaler.pipeline._write_dpi_variant", boom)

    finish = _start_one(task, db_path=db_path)
    assert get_task(tid, db_path=db_path).status == "running"

    finish()

    failed = get_task(tid, db_path=db_path)
    assert failed.status == "failed"
    assert "disk full" in failed.error


def test_prefetcher_warms_next_pending_original(tmp_path, monkeypatch) -> None:
    from proxy_scaler.upscale import original_cache_path
    from proxy_scaler.worker import _OriginalPrefetcher

    db_path = tmp_path / "test.db"
    init_db(db_path)
    _enqueue(db_path)  # will be claimed
    _enqueue(db_path, scryfall_id="bolt-id", face_name="Lightning Bolt")
    claimed = claim_next_task(db_path=db_path)
    _patch_upscale_fakes(monkeypatch)

    prefetcher = _OriginalPrefetcher(db_path=db_path)
    prefetcher.kick(current_face=(claimed.scryfall_id, claimed.face_index))
    prefetcher._thread.join(timeout=10)

    warmed = original_cache_path(tmp_path / "cache", "bolt-id", None)
    assert warmed.is_file()
    # The next task is still unclaimed — prefetch never claims.
    assert get_task(claimed.id + 1, db_path=db_path).status == "pending"


def test_prefetcher_skips_and_swallows(tmp_path, monkeypatch) -> None:
    from proxy_scaler.dpi import ORIGINAL_DPI, ORIGINAL_MODEL
    from proxy_scaler.upscale import original_cache_path
    from proxy_scaler.worker import _OriginalPrefetcher

    db_path = tmp_path / "test.db"
    init_db(db_path)
    calls = {"n": 0}

    def counting_download(url, session=None):
        calls["n"] += 1
        raise RuntimeError("network down")

    monkeypatch.setattr("proxy_scaler.pipeline.download_png", counting_download)
    prefetcher = _OriginalPrefetcher(db_path=db_path)

    # Same face as current -> skipped without a download.
    _enqueue(db_path)
    prefetcher.kick(current_face=("sol-id", None))
    prefetcher._thread.join(timeout=10)
    assert calls["n"] == 0

    # ORIGINAL_MODEL next -> skipped (that path overwrites unconditionally).
    claim_next_task(db_path=db_path)
    _enqueue(db_path, scryfall_id="dl-id", dpi=ORIGINAL_DPI, model=ORIGINAL_MODEL)
    prefetcher.kick(current_face=("other", None))
    prefetcher._thread.join(timeout=10)
    assert calls["n"] == 0

    # Download failure is swallowed; nothing cached, no exception escapes.
    claim_next_task(db_path=db_path)
    _enqueue(db_path, scryfall_id="boom-id")
    prefetcher.kick(current_face=("other", None))
    prefetcher._thread.join(timeout=10)
    assert calls["n"] == 1
    assert not original_cache_path(tmp_path / "cache", "boom-id", None).exists()


def test_peek_next_pending_matches_claim_order_without_claiming(tmp_path) -> None:
    from proxy_scaler.db import peek_next_pending

    db_path = tmp_path / "test.db"
    init_db(db_path)
    assert peek_next_pending(db_path=db_path) is None
    ids = [
        _enqueue(db_path),
        _enqueue(db_path, scryfall_id="bolt-id"),
        _enqueue(db_path, scryfall_id="third-id"),
    ]
    for expected_id in ids:
        peeked = peek_next_pending(db_path=db_path)
        assert peeked.id == expected_id
        assert peeked.status == "pending"  # peeking never claims
        claimed = claim_next_task(db_path=db_path)
        assert claimed.id == expected_id
    assert peek_next_pending(db_path=db_path) is None


def test_start_one_wires_cpu_fallback_flag(tmp_path, monkeypatch) -> None:
    """The on_cpu_fallback closure handed to process_task must set the
    worker_control flag with a parseable note the moment it's invoked."""
    import json

    from proxy_scaler import db as db_module
    from proxy_scaler import pipeline
    from proxy_scaler.worker import _start_one

    db_path = tmp_path / "test.db"
    init_db(db_path)
    tid = _enqueue(db_path)
    task = claim_next_task(db_path=db_path)

    class _DonePending:
        def finish(self):
            raise AssertionError("not needed")

    def fake_process_task(t, *, on_progress=None, timings=None, defer_finish=False,
                          on_cpu_fallback=None):
        assert on_cpu_fallback is not None
        on_cpu_fallback()  # simulate the OOM fallback firing mid-inference
        return _DonePending()

    monkeypatch.setattr(pipeline, "process_task", fake_process_task)

    _start_one(task, db_path=db_path)

    note = db_module.get_cpu_fallback(db_path=db_path)
    assert note is not None
    parsed = json.loads(note)
    assert parsed["task_id"] == tid
    assert parsed["face_name"] == "Sol Ring"
    assert parsed["model"] == "ultrasharp_v2"
    assert parsed["at"]
