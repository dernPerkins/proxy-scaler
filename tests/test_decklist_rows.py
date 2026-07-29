"""_face_key_for_task() must key-match pipeline.face_group_key() so a
face's in-flight task and its (once done) gallery item merge under the
same row in decklist.py's compact view."""

from __future__ import annotations

from pathlib import Path

from proxy_scaler.db import TaskRow
from proxy_scaler.pipeline import FaceResult, face_group_key
from proxy_scaler.ui.decklist import _face_key_for_task


def _task(**overrides) -> TaskRow:
    kwargs = dict(
        id=1,
        project_id=None,
        status="pending",
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
        output_dir="/tmp/out",
        cache_dir="/tmp/cache",
        weights_dir="/tmp/weights",
        error=None,
        created_at="2026-01-01T00:00:00Z",
        started_at=None,
        completed_at=None,
    )
    kwargs.update(overrides)
    return TaskRow(**kwargs)


def _face(**overrides) -> FaceResult:
    kwargs = dict(
        out_path=Path("/o/Sol_Ring-C21-263-swinir-800dpi.png"),
        original_path=Path("/c/Sol_Ring-C21-263.png"),
        scryfall_id="sol-id",
        face_index=None,
        face_name="Sol Ring",
        card_name="Sol Ring",
        set_code="c21",
        collector_number="263",
        png_url="https://example.com/sol.png",
        dpi=800,
        model="swinir",
    )
    kwargs.update(overrides)
    return FaceResult(**kwargs)


def test_face_key_matches_for_normal_printing() -> None:
    assert _face_key_for_task(_task()) == face_group_key(_face())


def test_face_key_matches_when_no_set_or_collector() -> None:
    task = _task(set_code="", collector_number="", scryfall_id="sol-id")
    face = _face(set_code="", collector_number="", scryfall_id="sol-id")
    assert _face_key_for_task(task) == face_group_key(face)


def test_face_key_includes_face_index_and_label() -> None:
    task = _task(face_index=1, face_label="back")
    face = _face(face_index=1, face_label="back")
    assert _face_key_for_task(task) == face_group_key(face)
    # And differs from the front face of the same printing.
    assert _face_key_for_task(task) != face_group_key(_face())
