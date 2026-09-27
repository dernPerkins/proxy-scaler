"""Custom Image upscaling: the project's custom_upscale setting, and the
size checks that keep an oversized upload from crashing a pass.

Custom Images used to be blanket-excluded from upscaling because a high-DPI
upload walked the tile ladder into the CPU fallback. These pin the rules
that replaced that exclusion: which variants Generate queues for a custom
(dpi.custom_upscale_targets), how the worker writes them, and the checks in
upscale.py that refuse a source before any backend runs it.
"""

from __future__ import annotations

import io
from pathlib import Path
from types import SimpleNamespace

import pytest
from PIL import Image

from proxy_scaler import customs, db as db_module
from proxy_scaler.db import init_db, list_gallery_items
from proxy_scaler.decklist import DeckEntry
from proxy_scaler.dpi import (
    CUSTOM_SOURCE_MODEL,
    MPC_BLEED_MM,
    NATIVE_MAX_DPI,
    bled_target_pixels,
    custom_upscale_targets,
    native_output_dpi,
    target_pixels,
)
from proxy_scaler.upscale import (
    USUAL_PATH_MAX_PX,
    SourceTooLargeError,
    UpscaleModel,
    Upscaler,
    UpscaleResult,
    _estimated_vram_need,
    check_source_size,
    load_or_upscale,
)

_GiB = 1024**3


@pytest.fixture()
def db_path(tmp_path: Path) -> Path:
    path = tmp_path / "test.db"
    init_db(path)
    return path


def _png_bytes(size: tuple[int, int], colour=(200, 30, 30)) -> bytes:
    buf = io.BytesIO()
    Image.new("RGB", size, colour).save(buf, format="PNG")
    return buf.getvalue()


def _upload(size: tuple[int, int], *, bleed_mm: float = 0.0) -> str:
    content_hash, _ = customs.store_original(_png_bytes(size), bleed_mm=bleed_mm)
    return content_hash


def _enqueue(tmp_path: Path, db_path: Path, content_hash: str, mode: str, targets, **kw):
    from proxy_scaler.services import generation as gen

    return gen.enqueue_decklist_entries(
        [DeckEntry(quantity=1, name="My Alter", custom_hash=content_hash)],
        model="ultrasharp_v2",
        dpi_targets=targets,
        skip_existing=True,
        tile_size=0,
        output_dir=tmp_path / "out",
        cache_dir=tmp_path / "cache",
        weights_dir=tmp_path / "weights",
        project_tag="tag-a",
        db_path=db_path,
        custom_upscale=mode,
        **kw,
    )


def _tasks(db_path: Path, task_ids: list[int]) -> list[tuple[str, int]]:
    rows = [db_module.get_task(t, db_path=db_path) for t in task_ids]
    return sorted((r.model, r.dpi) for r in rows)


# --------------------------------------------------------------------
# Which variants Generate queues
# --------------------------------------------------------------------


@pytest.mark.parametrize(
    ("mode", "source", "targets", "expected"),
    [
        ("off", 300, [600, 1200], []),
        ("target", 300, [600, 1200], [600, 1200]),
        # Only targets the upload doesn't already reach.
        ("target", 600, [600, 1200], [1200]),
        ("target", 1400, [600, 1200], []),
        ("native", 300, [1200], [1200]),
        ("native", 600, [1200], [2400]),
        # Capped: a full 4x of 900 DPI would be 3600.
        ("native", 900, [1200], [NATIVE_MAX_DPI]),
        # Native only runs for an upload below some selected target.
        ("native", 1300, [600, 1200], []),
        ("native", 700, [600], []),
    ],
)
def test_custom_upscale_targets(mode, source, targets, expected) -> None:
    assert custom_upscale_targets(mode, source, targets) == expected


def test_custom_upscale_targets_rejects_an_unknown_mode() -> None:
    with pytest.raises(ValueError):
        custom_upscale_targets("sometimes", 300, [1200])


def test_native_output_dpi_is_capped() -> None:
    assert native_output_dpi(450) == 1800
    assert native_output_dpi(601) == NATIVE_MAX_DPI


def test_target_mode_queues_only_the_targets_the_upload_misses(
    tmp_path: Path, db_path: Path, monkeypatch
) -> None:
    monkeypatch.chdir(tmp_path)
    content_hash = _upload(target_pixels(600))
    queued, failed, task_ids = _enqueue(tmp_path, db_path, content_hash, "target", [600, 1200])
    assert failed == 0
    # The source row always, plus one upscale — 600 is already met.
    assert _tasks(db_path, task_ids) == [(CUSTOM_SOURCE_MODEL, 600), ("ultrasharp_v2", 1200)]
    assert queued == 2


def test_target_mode_never_upscales_an_upload_that_reaches_every_target(
    tmp_path: Path, db_path: Path, monkeypatch
) -> None:
    monkeypatch.chdir(tmp_path)
    content_hash = _upload(target_pixels(1400))
    _, failed, task_ids = _enqueue(tmp_path, db_path, content_hash, "target", [600, 1200])
    assert failed == 0
    assert _tasks(db_path, task_ids) == [(CUSTOM_SOURCE_MODEL, 1400)]


@pytest.mark.parametrize(("source", "native"), [(600, 2400), (900, NATIVE_MAX_DPI)])
def test_native_mode_queues_one_capped_variant(
    tmp_path: Path, db_path: Path, monkeypatch, source, native
) -> None:
    monkeypatch.chdir(tmp_path)
    content_hash = _upload(target_pixels(source))
    _, _, task_ids = _enqueue(tmp_path, db_path, content_hash, "native", [600, 1200])
    assert _tasks(db_path, task_ids) == [(CUSTOM_SOURCE_MODEL, source), ("ultrasharp_v2", native)]


def test_off_mode_only_registers_the_source(tmp_path: Path, db_path: Path, monkeypatch) -> None:
    monkeypatch.chdir(tmp_path)
    content_hash = _upload(target_pixels(300))
    _, _, task_ids = _enqueue(tmp_path, db_path, content_hash, "off", [1200])
    assert _tasks(db_path, task_ids) == [(CUSTOM_SOURCE_MODEL, 300)]


def test_a_missing_upload_fails_once_not_twice(tmp_path: Path, db_path: Path, monkeypatch) -> None:
    """Registration already counts and notes the missing upload; the
    upscale loop must not report it a second time."""
    monkeypatch.chdir(tmp_path)
    notes: list[str] = []
    queued, failed, task_ids = _enqueue(
        tmp_path, db_path, "c" * 64, "target", [1200], on_note=notes.append
    )
    assert (queued, failed, task_ids) == (0, 1, [])
    assert sum("has not been uploaded" in n for n in notes) == 1


def test_an_unknown_mode_is_rejected(tmp_path: Path, db_path: Path, monkeypatch) -> None:
    monkeypatch.chdir(tmp_path)
    content_hash = _upload(target_pixels(300))
    with pytest.raises(ValueError):
        _enqueue(tmp_path, db_path, content_hash, "always", [1200])


# --------------------------------------------------------------------
# What the worker writes
# --------------------------------------------------------------------


def _fake_x4(monkeypatch) -> list[tuple[int, int]]:
    """Swap the torch upscaler for one that returns a plain 4x resize, and
    record the source sizes it was handed."""
    seen: list[tuple[int, int]] = []

    class FakeUpscaler:
        def __init__(self, model="ultrasharp_v2", scale=4, **_kw):
            self.model_id = UpscaleModel(model) if isinstance(model, str) else model
            self.scale = scale
            self.tile = 0

        def upscale(self, image):
            seen.append(image.size)
            w, h = image.size
            return UpscaleResult(image=image.convert("RGB").resize((w * 4, h * 4)), device="gpu")

    monkeypatch.setattr("proxy_scaler.pipeline.Upscaler", FakeUpscaler)
    return seen


def _run_upscale_tasks(db_path: Path, task_ids: list[int]):
    from proxy_scaler import pipeline

    results = []
    for task_id in task_ids:
        task = db_module.get_task(task_id, db_path=db_path)
        if task.model == CUSTOM_SOURCE_MODEL:
            continue
        result = pipeline.process_task(task)
        db_module.upsert_gallery_item_for_task(task, result, db_path=db_path)
        results.append(result)
    return results


def test_target_mode_writes_the_exact_bled_box(tmp_path: Path, db_path: Path, monkeypatch) -> None:
    """A 300 DPI upload that carries MPC bleed: the variant is the bled
    box at the target, as for any bled custom."""
    monkeypatch.chdir(tmp_path)
    seen = _fake_x4(monkeypatch)
    content_hash = _upload(bled_target_pixels(300, MPC_BLEED_MM), bleed_mm=MPC_BLEED_MM)
    _, _, task_ids = _enqueue(tmp_path, db_path, content_hash, "target", [1200])
    [result] = _run_upscale_tasks(db_path, task_ids)
    assert seen == [bled_target_pixels(300, MPC_BLEED_MM)]
    assert result.dpi == 1200 and result.custom_hash == content_hash
    with Image.open(result.out_path) as out:
        assert out.size == bled_target_pixels(1200, MPC_BLEED_MM)


def test_native_mode_keeps_the_models_own_pixels(tmp_path: Path, db_path: Path, monkeypatch) -> None:
    monkeypatch.chdir(tmp_path)
    seen = _fake_x4(monkeypatch)
    content_hash = _upload(target_pixels(600))
    _, _, task_ids = _enqueue(tmp_path, db_path, content_hash, "native", [1200])
    [result] = _run_upscale_tasks(db_path, task_ids)
    assert result.dpi == 2400
    # The stored upload (cover-cropped to card aspect, so a pixel off the
    # exact 600 DPI box) times 4, not resampled to the 2400 DPI box.
    [(w, h)] = seen
    with Image.open(result.out_path) as out:
        assert out.size == (w * 4, h * 4)
        assert out.size != target_pixels(2400)


def test_native_mode_resizes_down_to_the_cap(tmp_path: Path, db_path: Path, monkeypatch) -> None:
    monkeypatch.chdir(tmp_path)
    _fake_x4(monkeypatch)
    content_hash = _upload(target_pixels(700))
    _, _, task_ids = _enqueue(tmp_path, db_path, content_hash, "native", [1200])
    [result] = _run_upscale_tasks(db_path, task_ids)
    assert result.dpi == NATIVE_MAX_DPI
    with Image.open(result.out_path) as out:
        assert out.size == target_pixels(NATIVE_MAX_DPI)


def test_a_bleed_change_discards_upscaled_custom_variants(
    tmp_path: Path, db_path: Path, monkeypatch
) -> None:
    from proxy_scaler import pipeline

    monkeypatch.chdir(tmp_path)
    _fake_x4(monkeypatch)
    content_hash = _upload(target_pixels(300))
    _, _, task_ids = _enqueue(tmp_path, db_path, content_hash, "target", [1200])
    [result] = _run_upscale_tasks(db_path, task_ids)
    assert result.out_path.is_file()

    removed = pipeline.invalidate_custom_derivatives(
        content_hash, db_path=db_path, default_cache_dir=tmp_path / "cache"
    )
    assert removed >= 1
    assert not result.out_path.exists()
    assert not any(i["model"] == "ultrasharp_v2" for i in list_gallery_items("tag-a", db_path=db_path))


# --------------------------------------------------------------------
# Size checks (upscale.py)
# --------------------------------------------------------------------


def test_check_source_size_passes_the_usual_path_and_refuses_past_pillows_limit() -> None:
    check_source_size(745, 1040, 4)
    check_source_size(819, 1114, 4)
    check_source_size(2700, 3800, 4)  # ~10.3 Mpx -> ~164 Mpx out, under the limit
    with pytest.raises(SourceTooLargeError, match="still prints"):
        check_source_size(2900, 3900, 4)  # ~11.3 Mpx -> ~181 Mpx out


def test_refused_source_never_reaches_the_backend(tmp_path: Path) -> None:
    """The check runs before upscale(): an ncnn/ONNX backend must never
    see such a source, because failing all its rungs moves the rest of
    the session to the CPU."""

    class MustNotRun:
        model_id = UpscaleModel.ULTRASHARP_V2
        scale = 4

        def upscale(self, image):  # pragma: no cover - the assertion
            raise AssertionError("backend ran on a refused source")

    with pytest.raises(SourceTooLargeError):
        load_or_upscale(
            png_bytes=_png_bytes((2900, 3900)),
            upscaler=MustNotRun(),
            cache_dir=tmp_path,
            scryfall_id=None,
            face_index=None,
            custom_hash="d" * 64,
        )


def test_whole_image_term_leaves_card_sized_estimates_alone() -> None:
    for tile in (0, 640, 384, 256):
        base = _estimated_vram_need(745, 1040, tile, 32, "bf16", 1.4)
        assert _estimated_vram_need(819, 1114, tile, 32, "bf16", 1.4) >= base
    # Right at the threshold nothing extra is charged; above it, it grows
    # by the stitched output (24 B per bf16 output pixel, x16 at 4x).
    at = _estimated_vram_need(819, 1114, 256, 32, "bf16", 1.0)
    above = _estimated_vram_need(1638, 2228, 256, 32, "bf16", 1.0)
    extra_px = 1638 * 2228 - USUAL_PATH_MAX_PX
    assert above - at == pytest.approx(extra_px * 16 * 24, rel=0.01)


def _upscaler_on(device_type: str, model: str, free: int | None, monkeypatch) -> Upscaler:
    from proxy_scaler.upscale import effective_tile_size

    model_id = UpscaleModel(model)
    up = Upscaler(model_id, tile=effective_tile_size(model_id, 0), tile_auto=True)
    up._device = SimpleNamespace(type=device_type)
    monkeypatch.setattr(up, "_probe_free_vram", lambda: free)
    monkeypatch.setattr("proxy_scaler.upscale._current_headroom", lambda: 1.4)
    return up


def test_cuda_refuses_what_wont_fit_and_names_the_limit(monkeypatch) -> None:
    up = _upscaler_on("cuda", "ultrasharp_v2", 3 * _GiB, monkeypatch)
    up.tile = 256
    w, h = target_pixels(1100)
    with pytest.raises(SourceTooLargeError) as err:
        up._fit_oversized_source(w, h)
    msg = str(err.value)
    assert "GB is free" in msg and "up to about" in msg and "still prints" in msg


def test_cuda_accepts_what_fits(monkeypatch) -> None:
    up = _upscaler_on("cuda", "ultrasharp_v2", 15 * _GiB, monkeypatch)
    up.tile = 640
    assert up._fit_oversized_source(*target_pixels(600)) is None


def test_card_sized_sources_are_never_checked(monkeypatch) -> None:
    up = _upscaler_on("cuda", "ultrasharp_v2", 0, monkeypatch)
    assert up._fit_oversized_source(745, 1040) is None
    assert up._fit_oversized_source(819, 1114) is None


def test_light_model_is_tiled_for_an_oversized_source(monkeypatch) -> None:
    up = _upscaler_on("cuda", "realesrgan_anime_fast", 12 * _GiB, monkeypatch)
    assert up.tile == 0
    restore = up._fit_oversized_source(*target_pixels(600))
    assert up.tile == 384 and restore == 0


@pytest.mark.parametrize("device_type", ["privateuseone", "cpu"])
def test_host_stitched_devices_have_no_vram_refusal(device_type, monkeypatch) -> None:
    """DirectML stitches in system memory (and a refusal must never reach
    its worker-recycle handler); the CPU has no VRAM to run out of."""
    up = _upscaler_on(device_type, "ultrasharp_v2", 0, monkeypatch)
    assert up._fit_oversized_source(*target_pixels(1100)) is None


def test_mps_uses_a_fixed_ceiling(monkeypatch) -> None:
    up = _upscaler_on("mps", "ultrasharp_v2", None, monkeypatch)
    assert up._fit_oversized_source(*target_pixels(550)) is None
    with pytest.raises(SourceTooLargeError):
        up._fit_oversized_source(*target_pixels(700))
