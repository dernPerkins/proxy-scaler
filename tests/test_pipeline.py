"""Tests for pipeline.py: face grouping and multi-DPI regeneration."""

from __future__ import annotations

import io
from pathlib import Path

from PIL import Image

from proxy_scaler.db import TaskRow
from proxy_scaler.pipeline import (
    FaceResult,
    _THUMB_TARGET_BYTES,
    _upscalers_for_targets,
    ensure_original_thumbnail,
    expected_face_result,
    face_group_key,
    group_by_face,
    process_entries,
    process_task,
    regenerate_face_multi,
)
from proxy_scaler.scryfall import ScryfallClient
from proxy_scaler.upscale import DEFAULT_TILE_SIZE, UpscaleModel, original_thumb_path


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
    a = _face("sol-id", None, 800, "hat")
    b = _face("sol-id", None, 1200, "ultrasharp_v2")
    assert face_group_key(a) == face_group_key(b)
    [(key, _items)] = group_by_face([a, b])
    assert key == face_group_key(a)


def test_group_by_face_merges_across_models() -> None:
    items = [
        _face("sol-id", None, 800, "hat"),
        _face("sol-id", None, 800, "ultrasharp_v2"),
        _face("sol-id", None, 1200, "hat"),
    ]
    groups = group_by_face(items)
    assert len(groups) == 1
    _key, face_items = groups[0]
    assert len(face_items) == 3
    # sorted by (dpi, model)
    assert [(f.dpi, f.model) for f in face_items] == [
        (800, "hat"),
        (800, "ultrasharp_v2"),
        (1200, "hat"),
    ]


def test_group_by_face_merges_fresh_and_disk_recovered_entries() -> None:
    """A disk-recovered item (db.py::scan_gallery_from_output) never has a
    real scryfall_id — it must still merge with a freshly-resolved entry for
    the same physical printing (same set/collector), or match_quantities()
    would treat them as two different cards and print the card twice."""
    fresh = _face("sol-id", None, 800, "swinir")
    recovered = FaceResult(
        out_path=Path("/o/Sol_Ring-C21-263-swinir-1200dpi.png"),
        original_path=Path("/c/Sol_Ring-C21-263.png"),
        scryfall_id="",
        face_index=None,
        face_name="Sol Ring",
        card_name="Sol Ring",
        set_code="c21",
        collector_number="263",
        png_url="",
        dpi=1200,
        model="swinir",
    )
    groups = group_by_face([fresh, recovered])
    assert len(groups) == 1
    _key, face_items = groups[0]
    assert {f.dpi for f in face_items} == {800, 1200}


def test_group_by_face_keeps_different_faces_separate() -> None:
    items = [
        _face("dfc-id", 0, 800, "swinir", face_label="front"),
        _face("dfc-id", 1, 800, "swinir", face_label="back"),
    ]
    groups = group_by_face(items)
    assert len(groups) == 2


def test_regenerate_face_multi_shares_native_scale_pass(tmp_path, monkeypatch) -> None:
    """800 and 1200 DPI both resolve to native x4 for SwinIR — the AI
    upscale pass should only run once, not once per requested DPI."""
    original = tmp_path / "orig.png"
    buf = io.BytesIO()
    Image.new("RGB", (8, 8), color=(10, 20, 30)).save(buf, format="PNG")
    original.write_bytes(buf.getvalue())

    item = FaceResult(
        out_path=tmp_path / "out" / "Sol_Ring-C21-263-swinir-800dpi.png",
        original_path=original,
        scryfall_id="sol-id",
        face_index=None,
        face_name="Sol Ring",
        card_name="Sol Ring",
        set_code="c21",
        collector_number="263",
        png_url="",
        dpi=800,
        model="swinir",
    )

    fake_upscaled = Image.new("RGB", (32, 32), color=(40, 50, 60))
    call_count = {"n": 0}

    class FakeUpscaler:
        def __init__(self, model="swinir", scale=4, weights_dir="weights", **_kw):
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
        model="swinir",
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
    (out / "Sol_Ring-C21-263-swinir-800dpi.png").write_bytes(b"already-written")
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
        model="swinir",
        cache_dir=cache_dir,
        skip_existing=True,
    )

    assert resolve_many_calls["n"] == 1  # one bulk call, not one per entry
    assert result.skipped == 1
    assert len(result.wrote) == 1
    assert result.wrote[0].dpi == 800
    assert result.wrote[0].model == "swinir"
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
        model="swinir",
        tile_size=0,
        output_dir=str(tmp_path / "out"),
        cache_dir=str(tmp_path / "cache"),
        weights_dir=str(tmp_path / "weights"),
        error=None,
        created_at="2026-01-01T00:00:00Z",
        started_at=None,
        completed_at=None,
    )
    kwargs.update(overrides)
    return TaskRow(**kwargs)


class _FakeUpscaler:
    """Stands in for the real Upscaler — no model weights/GPU needed."""

    def __init__(self, model="swinir", scale=4, weights_dir="weights", **_kw):
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
    assert result.model == "swinir"
    assert result.scryfall_id == "sol-id"
    assert result.out_path.is_file()
    assert result.original_path.is_file()
    assert result.device == "gpu"


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
    assert expected.device == actual.device
    assert expected.dpi == actual.dpi
    assert expected.model == actual.model


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

    light = _upscalers_for_targets(UpscaleModel.SWINIR, [800], tmp_path, tile_size=0)
    [upscaler] = light.values()
    assert upscaler.tile == 0

    explicit = _upscalers_for_targets(UpscaleModel.SWINIR, [800], tmp_path, tile_size=128)
    [upscaler] = explicit.values()
    assert upscaler.tile == 128


def _rounded_rect_rgba(w: int, h: int, radius: int) -> Image.Image:
    """Realistic rounded-rect alpha: a quarter-circle cutout at all 4
    corners, matching real Scryfall/upscaled card geometry -- same fixture
    shape as test_pdf_layout.py's/test_pdf_html.py's own helper."""
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


def test_write_dpi_variant_flattens_corners_before_resizing(tmp_path: Path) -> None:
    """Regression guard for the corner-smearing bug: this generation-time
    resize (the one that produces the actual out_path PNG every PDF
    pipeline later reads) must flatten rounded-corner alpha to opaque
    BEFORE resizing to the target DPI, not after or never -- resizing an
    RGBA image while its corners are still transparent lets LANCZOS blend
    transparent RGB into the opaque body at the boundary, baking a visible
    smear directly into the saved file. This is a separate call site from
    (and was missed by) the export-time fix in pdf_layout.py::build_pdf --
    that fix can't undo damage already baked into out_path by the time it
    opens the file. Verified by call order, matching that fix's own test."""
    import proxy_scaler.pipeline as pipeline_module
    from proxy_scaler.scryfall import CardFaceImage

    call_order: list[str] = []
    real_flatten = pipeline_module.flatten_corner_alpha
    real_resize = pipeline_module._resize_to_dpi

    def spy_flatten(*args, **kwargs):
        call_order.append("flatten")
        return real_flatten(*args, **kwargs)

    def spy_resize(*args, **kwargs):
        call_order.append("resize")
        return real_resize(*args, **kwargs)

    pipeline_module.flatten_corner_alpha = spy_flatten
    pipeline_module._resize_to_dpi = spy_resize
    try:
        face = CardFaceImage(
            scryfall_id="abc",
            card_name="Test Card",
            face_name="Test Card",
            set_code="tst",
            collector_number="1",
            png_url="",
            face_index=None,
        )
        result = pipeline_module._write_dpi_variant(
            face=face,
            raw=_rounded_rect_rgba(100, 100, 15),
            original_path=tmp_path / "original.png",
            output_dir=tmp_path / "out",
            model_id=UpscaleModel.SWINIR,
            dpi=1200,  # differs from raw's native size, forcing the resize path
            native_scale=4,
        )
    finally:
        pipeline_module.flatten_corner_alpha = real_flatten
        pipeline_module._resize_to_dpi = real_resize

    assert call_order[:2] == ["flatten", "resize"]
    assert result.out_path.is_file()
