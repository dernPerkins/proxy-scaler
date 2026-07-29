"""Tests for pipeline.py: face grouping and multi-DPI regeneration."""

from __future__ import annotations

import io
from pathlib import Path

from PIL import Image

from proxy_scaler.pipeline import (
    FaceResult,
    face_group_key,
    group_by_face,
    regenerate_face_multi,
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
