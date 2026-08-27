"""Tests for pipeline.py: face grouping and multi-DPI regeneration."""

from __future__ import annotations

import io
from pathlib import Path

from PIL import Image

from proxy_scaler.db import TaskRow
from proxy_scaler.dpi import ORIGINAL_DPI, ORIGINAL_MODEL
from proxy_scaler.pipeline import (
    FaceResult,
    _THUMB_TARGET_BYTES,
    _upscalers_for_targets,
    ensure_original_thumbnail,
    expected_face_result,
    face_group_key,
    group_by_face,
    process_download_task,
    process_entries,
    process_task,
    regenerate_face_multi,
)
from proxy_scaler.scryfall import ScryfallClient
from proxy_scaler.upscale import (
    DEFAULT_TILE_SIZE,
    UpscaleModel,
    cache_path,
    original_cache_path,
    original_thumb_path,
)


def _face(
    scryfall_id: str,
    face_index: int | None,
    dpi: int,
    model: str,
    *,
    face_label: str | None = None,
) -> FaceResult:
    return FaceResult(
        out_path=Path(f"/o/{scryfall_id}-{face_index}-{model}-{dpi}.png"),
        original_path=Path(f"/c/{scryfall_id}-{face_index}.png"),
        scryfall_id=scryfall_id,
        face_index=face_index,
        face_name="Sol Ring",
        card_name="Sol Ring",
        set_code="c21",
        collector_number="263",
        png_url="",
        dpi=dpi,
        model=model,
        face_label=face_label,
    )


def test_face_group_key_matches_group_by_face_grouping() -> None:
    """face_group_key() is the same identity group_by_face() uses — the UI
    relies on this to mark a face as "loaded" the moment it's generated,
    using the exact key it'll later be looked up under in the gallery."""
    a = _face("sol-id", None, 800, "illustrationjanai")
    b = _face("sol-id", None, 1200, "ultrasharp_v2")
    assert face_group_key(a) == face_group_key(b)
    [(key, _items)] = group_by_face([a, b])
    assert key == face_group_key(a)


def test_group_by_face_merges_across_models() -> None:
    items = [
        _face("sol-id", None, 800, "illustrationjanai"),
        _face("sol-id", None, 800, "ultrasharp_v2"),
        _face("sol-id", None, 1200, "illustrationjanai"),
    ]
    groups = group_by_face(items)
    assert len(groups) == 1
    _key, face_items = groups[0]
    assert len(face_items) == 3
    # sorted by (dpi, model)
    assert [(f.dpi, f.model) for f in face_items] == [
        (800, "illustrationjanai"),
        (800, "ultrasharp_v2"),
        (1200, "illustrationjanai"),
    ]


def test_group_by_face_merges_fresh_and_disk_recovered_entries() -> None:
    """A disk-recovered item (db.py::scan_gallery_from_output) never has a
    real scryfall_id — it must still merge with a freshly-resolved entry for
    the same physical printing (same set/collector), or match_quantities()
    would treat them as two different cards and print the card twice."""
    fresh = _face("sol-id", None, 800, "ultrasharp_v2")
    recovered = FaceResult(
        out_path=Path("/o/Sol_Ring-C21-263-ultrasharp_v2-1200dpi.png"),
        original_path=Path("/c/Sol_Ring-C21-263.png"),
        scryfall_id="",
        face_index=None,
        face_name="Sol Ring",
        card_name="Sol Ring",
        set_code="c21",
        collector_number="263",
        png_url="",
        dpi=1200,
        model="ultrasharp_v2",
    )
    groups = group_by_face([fresh, recovered])
    assert len(groups) == 1
    _key, face_items = groups[0]
    assert {f.dpi for f in face_items} == {800, 1200}


def test_group_by_face_keeps_different_faces_separate() -> None:
    items = [
        _face("dfc-id", 0, 800, "ultrasharp_v2", face_label="front"),
        _face("dfc-id", 1, 800, "ultrasharp_v2", face_label="back"),
    ]
    groups = group_by_face(items)
    assert len(groups) == 2


def test_regenerate_face_multi_shares_native_scale_pass(tmp_path, monkeypatch) -> None:
    """800 and 1200 DPI both resolve to native x4 — the AI upscale pass
    should only run once, not once per requested DPI."""
    original = tmp_path / "orig.png"
    buf = io.BytesIO()
    Image.new("RGB", (8, 8), color=(10, 20, 30)).save(buf, format="PNG")
    original.write_bytes(buf.getvalue())

    item = FaceResult(
        out_path=tmp_path / "out" / "Sol_Ring-C21-263-ultrasharp_v2-800dpi.png",
        original_path=original,
        scryfall_id="sol-id",
        face_index=None,
        face_name="Sol Ring",
        card_name="Sol Ring",
        set_code="c21",
        collector_number="263",
        png_url="",
        dpi=800,
        model="ultrasharp_v2",
    )

    fake_upscaled = Image.new("RGB", (32, 32), color=(40, 50, 60))
    call_count = {"n": 0}

    class FakeUpscaler:
        def __init__(self, model="ultrasharp_v2", scale=4, weights_dir="weights", **_kw):
            from proxy_scaler.upscale import UpscaleModel

            self.model_id = UpscaleModel(model) if isinstance(model, str) else model
            self.scale = scale

        def upscale(self, image):
            from proxy_scaler.upscale import UpscaleResult

            call_count["n"] += 1
            return UpscaleResult(image=fake_upscaled, device="gpu")

    monkeypatch.setattr("proxy_scaler.pipeline.Upscaler", FakeUpscaler)

    results = regenerate_face_multi(
        item,
        dpi_targets=[800, 1200],
        output_dir=tmp_path / "out",
        cache_dir=tmp_path / "cache",
        weights_dir=tmp_path / "weights",
        model="ultrasharp_v2",
    )

    assert len(results) == 2
    assert {r.dpi for r in results} == {800, 1200}
    assert call_count["n"] == 1


def test_process_entries_skip_existing_uses_batched_resolve(tmp_path, monkeypatch) -> None:
    """process_entries() resolves via the new batched resolve_many() (one
    call, not one per entry) and the existing skip_existing branch still
    works correctly behind it: an already-written output file with a
    missing cached original still downloads just the original and skips
    upscaling entirely."""
    from proxy_scaler.decklist import parse_decklist_text

    out = tmp_path / "out"
    out.mkdir()
    (out / "Sol_Ring-C21-263-ultrasharp_v2-800dpi.png").write_bytes(b"already-written")
    cache_dir = tmp_path / "cache"

    sol_ring_card = {
        "id": "sol-id",
        "name": "Sol Ring",
        "set": "c21",
        "collector_number": "263",
        "image_status": "highres_scan",
        "image_uris": {"png": "https://example.com/sol.png"},
    }

    resolve_many_calls = {"n": 0}

    def fake_resolve_many(self, entries):
        resolve_many_calls["n"] += 1
        return [(sol_ring_card, []) for _ in entries]

    monkeypatch.setattr(ScryfallClient, "resolve_many", fake_resolve_many)

    download_calls = {"n": 0}

    def fake_download_png(url, session=None):
        download_calls["n"] += 1
        buf = io.BytesIO()
        Image.new("RGB", (8, 8), color=(1, 2, 3)).save(buf, format="PNG")
        return buf.getvalue()

    monkeypatch.setattr("proxy_scaler.pipeline.download_png", fake_download_png)

    class UpscalerNotExpected:
        def __init__(self, *_a, **_kw):
            pass

        def upscale(self, image):
            raise AssertionError("upscaling should be skipped for an existing output file")

    monkeypatch.setattr("proxy_scaler.pipeline.Upscaler", UpscalerNotExpected)

    entries = parse_decklist_text("1 Sol Ring (c21) 263\n")
    result = process_entries(
        entries,
        output_dir=out,
        dpi_targets=[800],
        model="ultrasharp_v2",
        cache_dir=cache_dir,
        skip_existing=True,
    )

    assert resolve_many_calls["n"] == 1  # one bulk call, not one per entry
    assert result.skipped == 1
    assert len(result.wrote) == 1
    assert result.wrote[0].dpi == 800
    assert result.wrote[0].model == "ultrasharp_v2"
    # The original was missing from cache — skip_existing's existing logic
    # still downloads it even though the upscale itself is skipped.
    assert download_calls["n"] == 1
    assert result.wrote[0].original_path.is_file()


def _task(tmp_path: Path, **overrides) -> TaskRow:
    kwargs = dict(
        id=1,
        project_tag=None,
        status="running",
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
        tile_size=0,
        output_dir=str(tmp_path / "out"),
        cache_dir=str(tmp_path / "cache"),
        weights_dir=str(tmp_path / "weights"),
        error=None,
        created_at="2026-01-01T00:00:00Z",
        started_at=None,
        completed_at=None,
        total_faces=2,
    )
    kwargs.update(overrides)
    return TaskRow(**kwargs)


class _FakeUpscaler:
    """Stands in for the real Upscaler — no model weights/GPU needed."""

    def __init__(self, model="ultrasharp_v2", scale=4, weights_dir="weights", **_kw):
        from proxy_scaler.upscale import UpscaleModel

        self.model_id = UpscaleModel(model) if isinstance(model, str) else model
        self.scale = scale

    def upscale(self, image):
        from proxy_scaler.upscale import UpscaleResult

        return UpscaleResult(image=Image.new("RGB", (32, 32), color=(40, 50, 60)), device="gpu")


def _fake_png_bytes() -> bytes:
    buf = io.BytesIO()
    Image.new("RGB", (8, 8), color=(10, 20, 30)).save(buf, format="PNG")
    return buf.getvalue()


def test_process_task_writes_face_result(tmp_path, monkeypatch) -> None:
    task = _task(tmp_path)
    monkeypatch.setattr(
        "proxy_scaler.pipeline.download_png", lambda url, session=None: _fake_png_bytes()
    )
    monkeypatch.setattr("proxy_scaler.pipeline.Upscaler", _FakeUpscaler)

    result = process_task(task)

    assert result.dpi == 800
    assert result.model == "ultrasharp_v2"
    assert result.scryfall_id == "sol-id"
    assert result.out_path.is_file()
    assert result.original_path.is_file()
    assert result.device == "gpu"
    # Carried through from the task row (set at enqueue time from Scryfall's
    # card data), not recomputed — see db migration 003.
    assert result.total_faces == 2


def test_expected_face_result_matches_process_task_output(tmp_path, monkeypatch) -> None:
    """expected_face_result() reconstructs the same FaceResult process_task()
    actually produces, purely from the task row — used to sync the gallery
    before any project/DB gallery row exists."""
    task = _task(tmp_path)
    monkeypatch.setattr(
        "proxy_scaler.pipeline.download_png", lambda url, session=None: _fake_png_bytes()
    )
    monkeypatch.setattr("proxy_scaler.pipeline.Upscaler", _FakeUpscaler)

    actual = process_task(task)
    expected = expected_face_result(task)

    assert expected.out_path == actual.out_path
    assert expected.original_path == actual.original_path
    assert expected.native_scale == actual.native_scale
    assert expected.total_faces == actual.total_faces == 2
    assert expected.device == actual.device
    assert expected.dpi == actual.dpi
    assert expected.model == actual.model


def test_process_task_dispatches_download_task_on_sentinel(tmp_path, monkeypatch) -> None:
    """A download task (model == ORIGINAL_MODEL) must branch before
    parse_model — the sentinel isn't an UpscaleModel and would raise."""
    task = _task(tmp_path, dpi=ORIGINAL_DPI, model=ORIGINAL_MODEL)
    monkeypatch.setattr(
        "proxy_scaler.pipeline.download_png", lambda url, session=None: _fake_png_bytes()
    )

    result = process_task(task)

    assert result.model == ORIGINAL_MODEL
    assert result.dpi == ORIGINAL_DPI
    # The download's artifact IS the cached original — one file, both roles.
    assert result.out_path == result.original_path
    assert result.out_path == original_cache_path(Path(task.cache_dir), "sol-id", None)
    assert result.out_path.is_file()
    assert original_thumb_path(result.out_path).is_file()


def test_process_download_task_overwrites_cached_original(tmp_path, monkeypatch) -> None:
    """Downloads always overwrite (skip-existing lives at enqueue time) —
    that's what makes Re-Fetch just "enqueue a download task". The derived
    thumbnail must be regenerated too, not left describing the old art."""
    task = _task(tmp_path, dpi=ORIGINAL_DPI, model=ORIGINAL_MODEL)
    original = original_cache_path(Path(task.cache_dir), "sol-id", None)
    original.parent.mkdir(parents=True, exist_ok=True)
    Image.new("RGB", (8, 8), color=(200, 0, 0)).save(original, format="PNG")
    stale_bytes = original.read_bytes()
    stale_thumb = original_thumb_path(original)
    stale_thumb.write_bytes(b"stale thumb")

    fresh = _fake_png_bytes()
    monkeypatch.setattr("proxy_scaler.pipeline.download_png", lambda url, session=None: fresh)

    result = process_download_task(task)

    assert result.original_path.read_bytes() == fresh
    assert result.original_path.read_bytes() != stale_bytes
    assert stale_thumb.is_file()
    assert stale_thumb.read_bytes() != b"stale thumb"


def _write_fake_original(tmp_path: Path, *, size: tuple[int, int] = (600, 840)) -> Path:
    path = tmp_path / "originals" / "sol-id_single.png"
    path.parent.mkdir(parents=True, exist_ok=True)
    Image.new("RGBA", size, (200, 30, 30, 255)).save(path, format="PNG")
    return path


def test_ensure_original_thumbnail_generates_under_target_size(tmp_path: Path) -> None:
    original = _write_fake_original(tmp_path)
    thumb = ensure_original_thumbnail(original)
    assert thumb is not None
    assert thumb == original_thumb_path(original)
    assert thumb.is_file()
    assert thumb.stat().st_size <= _THUMB_TARGET_BYTES


def test_ensure_original_thumbnail_is_self_healing(tmp_path: Path) -> None:
    """Covers the real gap this exists for: an original cached via the
    services/generation.py skip_existing fast path (which stores
    original_path without ever touching the file) never goes through the
    eager _regenerate_face_from_card hook -- this lazy accessor is what
    backfills it on first actual need."""
    original = _write_fake_original(tmp_path)
    assert not original_thumb_path(original).is_file()

    thumb = ensure_original_thumbnail(original)
    assert thumb is not None
    assert thumb.is_file()

    # Idempotent: calling again reuses the existing file rather than
    # erroring or silently regenerating.
    mtime = thumb.stat().st_mtime
    again = ensure_original_thumbnail(original)
    assert again == thumb
    assert thumb.stat().st_mtime == mtime


def test_ensure_original_thumbnail_missing_original_returns_none(tmp_path: Path) -> None:
    missing = tmp_path / "originals" / "nope_single.png"
    assert ensure_original_thumbnail(missing) is None


def test_upscalers_for_targets_auto_tiles_heavy_models(tmp_path: Path) -> None:
    """Regression test: tile_size=0 ("auto" from the client) must still
    resolve to a real tile size for memory-hungry models like UltraSharpV2
    -- this is the actual choke point every generation path builds its
    Upscaler instances through, so a bug here silently disables tiling for
    every call site at once (previously effective_tile_size() existed but
    nothing called it, so heavy models OOM'd on GPU under "auto")."""
    heavy = _upscalers_for_targets(UpscaleModel.ULTRASHARP_V2, [800], tmp_path, tile_size=0)
    [upscaler] = heavy.values()
    assert upscaler.tile == DEFAULT_TILE_SIZE

    light = _upscalers_for_targets(UpscaleModel.REALESRGAN_ANIME_FAST, [800], tmp_path, tile_size=0)
    [upscaler] = light.values()
    assert upscaler.tile == 0

    explicit = _upscalers_for_targets(UpscaleModel.REALESRGAN_ANIME_FAST, [800], tmp_path, tile_size=128)
    [upscaler] = explicit.values()
    assert upscaler.tile == 128


def _rounded_rect_rgba(w: int, h: int, radius: int) -> Image.Image:
    """Realistic rounded-rect alpha: a quarter-circle cutout at all 4
    corners, matching real Scryfall/upscaled card geometry -- same fixture
    shape as test_pdf_layout.py's own helper."""
    img = Image.new("RGBA", (w, h), (10, 20, 30, 255))
    px = img.load()
    corners = [(0, 0), (w - radius, 0), (0, h - radius), (w - radius, h - radius)]
    for cx, cy in corners:
        for dy in range(radius):
            for dx in range(radius):
                ax = dx if cx == 0 else radius - 1 - dx
                ay = dy if cy == 0 else radius - 1 - dy
                center_dx, center_dy = radius - ax, radius - ay
                if center_dx * center_dx + center_dy * center_dy > radius * radius:
                    px[cx + dx, cy + dy] = (0, 0, 0, 0)
    return img


def test_write_dpi_variant_preserves_transparent_corners(tmp_path: Path) -> None:
    """Generation must NOT flatten/bleed the rounded corners -- that is a
    print-time concern owned by the PDF pipelines
    (pdf_layout.flatten_corner_alpha, applied per export). A previous
    version called it here, which replicated edge pixels across every
    corner of the stored PNG: a visible smear in the saved file,
    irreversible, and wrong for anyone downloading the image directly.
    The written variant should stay a faithful upscale of the source art,
    transparent corners intact."""
    from proxy_scaler.pipeline import _write_dpi_variant
    from proxy_scaler.scryfall import CardFaceImage

    face = CardFaceImage(
        scryfall_id="abc",
        card_name="Test Card",
        face_name="Test Card",
        set_code="tst",
        collector_number="1",
        png_url="",
        face_index=None,
    )
    result = _write_dpi_variant(
        face=face,
        raw=_rounded_rect_rgba(100, 100, 15),
        original_path=tmp_path / "original.png",
        output_dir=tmp_path / "out",
        model_id=UpscaleModel.ULTRASHARP_V2,
        dpi=1200,  # differs from raw's native size, forcing the resize path
        native_scale=4,
    )

    assert result.out_path.is_file()
    with Image.open(result.out_path) as written:
        assert written.mode == "RGBA", "alpha channel must survive generation"
        alpha = written.getchannel("A")
        w, h = written.size
        # Every corner keeps its transparent arc; the extreme corner pixel
        # is the deepest part of the cutout.
        for x, y in ((0, 0), (w - 1, 0), (0, h - 1), (w - 1, h - 1)):
            assert alpha.getpixel((x, y)) == 0, f"corner ({x},{y}) was flattened to opaque"


# --- deferred finish seam (see #4: overlap I/O with GPU work) ----------------


def test_process_task_defer_finish_writes_nothing_until_finish(
    tmp_path, monkeypatch
) -> None:
    from proxy_scaler.pipeline import PendingTask, expected_face_result
    from proxy_scaler.upscale import cache_device_path

    task = _task(tmp_path)
    monkeypatch.setattr(
        "proxy_scaler.pipeline.download_png", lambda url, session=None: _fake_png_bytes()
    )
    monkeypatch.setattr("proxy_scaler.pipeline.Upscaler", _FakeUpscaler)

    pending = process_task(task, defer_finish=True)
    assert isinstance(pending, PendingTask)

    expected = expected_face_result(task)
    cached = cache_path(
        Path(task.cache_dir), task.scryfall_id, task.face_index,
        expected.native_scale, task.model,
    )
    # Inference is done, but no output artifact exists yet.
    assert not expected.out_path.exists()
    assert not cached.exists()
    assert not cache_device_path(cached).exists()

    result = pending.finish()

    assert result.out_path.is_file()
    assert cached.is_file()
    assert cache_device_path(cached).is_file()
    assert result == expected_face_result(task)  # incl. device from sidecar


def test_process_task_defer_finish_download_task_already_finished(
    tmp_path, monkeypatch
) -> None:
    from proxy_scaler.pipeline import PendingTask

    task = _task(tmp_path, dpi=ORIGINAL_DPI, model=ORIGINAL_MODEL)
    monkeypatch.setattr(
        "proxy_scaler.pipeline.download_png", lambda url, session=None: _fake_png_bytes()
    )

    pending = process_task(task, defer_finish=True)
    assert isinstance(pending, PendingTask)
    # No GPU phase to hide behind: the file exists before finish().
    expected = original_cache_path(Path(task.cache_dir), task.scryfall_id, task.face_index)
    assert expected.is_file()
    result = pending.finish()
    assert result.out_path == expected


def test_save_cache_png_writes_sidecar_only_after_png(tmp_path, monkeypatch) -> None:
    import pytest

    from proxy_scaler import upscale as upscale_module
    from proxy_scaler.upscale import cache_device_path, save_cache_png

    def boom(image, path, *, compress_level=6):
        raise OSError("disk full")

    monkeypatch.setattr(upscale_module, "atomic_save_png", boom)
    target = tmp_path / "cached.png"
    with pytest.raises(OSError):
        save_cache_png(Image.new("RGB", (4, 4)), target, "gpu")
    assert not target.exists()
    assert not cache_device_path(target).exists()  # never describes a missing PNG


def test_prefetch_original_warms_cache_and_is_idempotent(tmp_path, monkeypatch) -> None:
    from proxy_scaler.pipeline import prefetch_original
    from proxy_scaler.upscale import original_thumb_path

    task = _task(tmp_path)
    calls = {"n": 0}

    def counting_download(url, session=None):
        calls["n"] += 1
        return _fake_png_bytes()

    monkeypatch.setattr("proxy_scaler.pipeline.download_png", counting_download)

    path = prefetch_original(task)
    assert path is not None and path.is_file()
    assert original_thumb_path(path).is_file()
    assert calls["n"] == 1

    again = prefetch_original(task)
    assert again == path
    assert calls["n"] == 1  # cached -> no second download


# --- sibling-DPI cache reuse (see #3: stop re-running inference per DPI) -----


class _CountingUpscaler(_FakeUpscaler):
    calls = 0

    def upscale(self, image):
        type(self).calls += 1
        return super().upscale(image)


def test_process_task_siblings_share_one_model_pass(tmp_path, monkeypatch) -> None:
    """Two non-forced tasks for the same face at different DPIs: the second
    hits the x4 cache PNG the first wrote — zero model passes — and still
    writes its own DPI output."""
    _CountingUpscaler.calls = 0
    monkeypatch.setattr(
        "proxy_scaler.pipeline.download_png", lambda url, session=None: _fake_png_bytes()
    )
    monkeypatch.setattr("proxy_scaler.pipeline.Upscaler", _CountingUpscaler)

    first = process_task(_task(tmp_path, dpi=600, force=False))
    assert _CountingUpscaler.calls == 1
    assert first.out_path.is_file()

    second = process_task(_task(tmp_path, id=2, dpi=800, force=False))
    assert _CountingUpscaler.calls == 1  # cache hit — no second pass
    assert second.out_path.is_file()
    assert second.out_path != first.out_path
    assert second.device == "gpu"  # provenance carried by the .device sidecar


def test_process_task_force_bypasses_cache(tmp_path, monkeypatch) -> None:
    _CountingUpscaler.calls = 0
    monkeypatch.setattr(
        "proxy_scaler.pipeline.download_png", lambda url, session=None: _fake_png_bytes()
    )
    monkeypatch.setattr("proxy_scaler.pipeline.Upscaler", _CountingUpscaler)

    process_task(_task(tmp_path, dpi=600, force=False))
    process_task(_task(tmp_path, id=2, dpi=600, force=True))
    assert _CountingUpscaler.calls == 2  # regeneration re-ran the model


def test_finish_skips_cache_rewrite_on_hit(tmp_path, monkeypatch) -> None:
    """A cache-hit task must not re-encode the x4 cache PNG it just read."""
    monkeypatch.setattr(
        "proxy_scaler.pipeline.download_png", lambda url, session=None: _fake_png_bytes()
    )
    monkeypatch.setattr("proxy_scaler.pipeline.Upscaler", _FakeUpscaler)
    process_task(_task(tmp_path, dpi=600, force=False))  # warms the cache

    writes = {"n": 0}

    def counting_save(image, path, device, **kw):
        writes["n"] += 1

    monkeypatch.setattr("proxy_scaler.pipeline.save_cache_png", counting_save)
    process_task(_task(tmp_path, id=2, dpi=800, force=False))
    assert writes["n"] == 0


def test_process_download_task_invalidates_x4_cache(tmp_path, monkeypatch) -> None:
    """Re-Fetch replaces the original, so every derived x4 cache PNG (and
    its .device sidecar) must go — otherwise a later non-forced generation
    would upscale the OLD art from cache."""
    from proxy_scaler.upscale import cache_device_path

    monkeypatch.setattr(
        "proxy_scaler.pipeline.download_png", lambda url, session=None: _fake_png_bytes()
    )
    cache_dir = tmp_path / "cache"
    cache_dir.mkdir()
    # Two models seeded — pins that the invalidation iterates the whole
    # enum, not a hardcoded model list.
    stale_paths = []
    for model in (UpscaleModel.ULTRASHARP_V2, UpscaleModel.ULTRASHARP_V2_LITE):
        stale = cache_path(cache_dir, "sol-id", None, 4, model)
        stale.write_bytes(b"old art x4")
        cache_device_path(stale).write_text("gpu\n")
        stale_paths.append(stale)

    task = _task(tmp_path, dpi=ORIGINAL_DPI, model=ORIGINAL_MODEL)
    process_download_task(task)

    for stale in stale_paths:
        assert not stale.exists()
        assert not cache_device_path(stale).exists()
