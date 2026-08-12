"""Unit tests for proxy_scaler/services/decklist.py — the card/face
identity-matching and status-aggregation logic extracted from
proxy_scaler/ui/decklist.py during the Streamlit -> FastAPI migration.
This is the highest-risk translation in that migration, so it gets direct
tests independent of any router/API wiring."""

from __future__ import annotations

from pathlib import Path

from proxy_scaler.db import TaskRow
from proxy_scaler.pipeline import FaceResult, face_group_key
from proxy_scaler.services.decklist import (
    build_rows,
    card_identity,
    face_key_for_task,
    group_by_card,
    status_for_pairs,
)


def _task(**overrides) -> TaskRow:
    kwargs = dict(
        id=1,
        project_tag=None,
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
        total_faces=None,
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


def test_card_identity_prefers_set_and_collector() -> None:
    assert card_identity("C21", "263", "sol-id") == "c21/263"


def test_card_identity_falls_back_to_scryfall_id() -> None:
    assert card_identity(None, None, "sol-id") == "sol-id"
    assert card_identity("", "", "sol-id") == "sol-id"


def test_card_identity_falls_back_to_unknown() -> None:
    assert card_identity(None, None, None) == "unknown"


def test_face_key_for_task_matches_pipeline_face_group_key() -> None:
    """The extracted service must stay in lockstep with pipeline.py's own
    identity scheme — a face's in-flight task and its (once done) gallery
    item have to merge under the same key."""
    assert face_key_for_task(_task()) == face_group_key(_face())


def test_face_key_for_task_includes_face_index_and_label() -> None:
    task = _task(face_index=1, face_label="back")
    face = _face(face_index=1, face_label="back")
    assert face_key_for_task(task) == face_group_key(face)
    assert face_key_for_task(task) != face_group_key(_face())


def test_build_rows_merges_done_item_and_task_under_one_key() -> None:
    done = _face()
    pending_task = _task(id=2, status="pending", face_index=1, face_label="back")
    rows = build_rows([done], [pending_task])
    assert len(rows) == 2  # front (done) and back (pending) are separate faces
    keys = {key for key, _items, _tasks in rows}
    assert face_group_key(done) in keys
    assert face_key_for_task(pending_task) in keys


def test_build_rows_task_without_done_variant_still_gets_a_row() -> None:
    task = _task(status="failed", error="boom")
    rows = build_rows([], [task])
    assert len(rows) == 1
    key, items, tasks = rows[0]
    assert items == []
    assert tasks == [task]


def test_group_by_card_drops_face_index_distinction() -> None:
    front = _face(face_index=0, face_label="front")
    back = _face(face_index=1, face_label="back")
    gallery_by_card, tasks_by_card = group_by_card([front, back], [])
    assert list(gallery_by_card.keys()) == ["c21/263"]
    assert gallery_by_card["c21/263"] == [front, back]
    assert tasks_by_card == {}


def test_status_for_pairs_done_wins_over_task_history() -> None:
    """A done FaceResult always wins over any task history for the same
    (dpi, model) pair — a task record for a pair that's since succeeded is
    just history, not current state."""
    done = _face(dpi=800, model="swinir")
    stale_failed_task = _task(dpi=800, model="swinir", status="failed", error="old")
    rows = status_for_pairs([done], [stale_failed_task])
    assert rows == [(800, "swinir", "done", None)]


def test_status_for_pairs_uses_newest_task_when_no_done_variant() -> None:
    older = _task(id=1, dpi=800, model="swinir", status="failed", error="first")
    newer = _task(id=2, dpi=800, model="swinir", status="pending", error=None)
    # face_tasks is documented as already created_at DESC — newest first.
    rows = status_for_pairs([], [newer, older])
    assert rows == [(800, "swinir", "pending", None)]


def test_status_for_pairs_separates_distinct_dpi_model_pairs() -> None:
    done_800 = _face(dpi=800, model="swinir")
    task_1200 = _task(dpi=1200, model="realesrgan", status="running")
    rows = status_for_pairs([done_800], [task_1200])
    assert rows == [
        (800, "swinir", "done", None),
        (1200, "realesrgan", "running", None),
    ]


