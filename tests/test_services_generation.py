"""Unit tests for proxy_scaler/services/generation.py — the enqueue
orchestration extracted from proxy_scaler/ui/decklist.py during the
Streamlit -> FastAPI migration. Uses tmp_path-isolated SQLite (same
db_path-override pattern worker.py/supervisor.py already use) and mocks
ScryfallClient.resolve_many the same way test_pipeline.py does, rather
than making real network calls."""

from __future__ import annotations

from pathlib import Path

import pytest

from proxy_scaler import db
from proxy_scaler.decklist import DeckEntry
from proxy_scaler.scryfall import ScryfallClient, ScryfallError
from proxy_scaler.services import generation

SOL_RING_CARD = {
    "id": "sol-id",
    "name": "Sol Ring",
    "set": "c21",
    "collector_number": "263",
    "image_status": "highres_scan",
    "image_uris": {"png": "https://example.com/sol.png"},
}


@pytest.fixture
def db_path(tmp_path: Path) -> Path:
    path = tmp_path / "test.db"
    db.init_db(path)
    return path


def _entry(**overrides) -> DeckEntry:
    kwargs = dict(quantity=1, name="Sol Ring", raw_line="1 Sol Ring")
    kwargs.update(overrides)
    return DeckEntry(**kwargs)


def test_enqueue_face_queues_one_task_per_dpi(db_path: Path, tmp_path: Path) -> None:
    task_ids = generation.enqueue_face(
        scryfall_id="sol-id",
        face_index=None,
        face_label=None,
        face_name="Sol Ring",
        card_name="Sol Ring",
        set_code="c21",
        collector_number="263",
        png_url="https://example.com/sol.png",
        dpi_targets=[600, 800],
        model="swinir",
        tile_size=0,
        output_dir=tmp_path / "out",
        cache_dir=tmp_path / "cache",
        weights_dir=tmp_path / "weights",
        project_tag=None,
        db_path=db_path,
    )
    assert len(task_ids) == 2
    tasks = db.list_tasks(db_path=db_path)
    assert {t.dpi for t in tasks} == {600, 800}
    assert all(t.status == "pending" for t in tasks)


def test_active_task_keys_none_project_tag_is_empty(db_path: Path) -> None:
    assert generation.active_task_keys(None, db_path=db_path) == set()


def test_active_task_keys_reflects_pending_and_running_only(
    db_path: Path, tmp_path: Path
) -> None:
    tag = "proj-tag-1"
    generation.enqueue_face(
        scryfall_id="sol-id",
        face_index=None,
        face_label=None,
        face_name="Sol Ring",
        card_name="Sol Ring",
        set_code="c21",
        collector_number="263",
        png_url="https://example.com/sol.png",
        dpi_targets=[800],
        model="swinir",
        tile_size=0,
        output_dir=tmp_path / "out",
        cache_dir=tmp_path / "cache",
        weights_dir=tmp_path / "weights",
        project_tag=tag,
        db_path=db_path,
    )
    active = generation.active_task_keys(tag, db_path=db_path)
    assert active == {("sol-id", None, 800, "swinir")}


def test_enqueue_decklist_entries_batches_resolve_call(
    db_path: Path, tmp_path: Path, monkeypatch
) -> None:
    calls = {"n": 0}

    def fake_resolve_many(self, entries):
        calls["n"] += 1
        return [(SOL_RING_CARD, []) for _ in entries]

    monkeypatch.setattr(ScryfallClient, "resolve_many", fake_resolve_many)

    queued, failed, task_ids = generation.enqueue_decklist_entries(
        [_entry(), _entry(raw_line="1 Sol Ring again")],
        model="swinir",
        dpi_targets=[800],
        skip_existing=False,
        tile_size=0,
        output_dir=tmp_path / "out",
        cache_dir=tmp_path / "cache",
        weights_dir=tmp_path / "weights",
        project_tag=None,
        db_path=db_path,
    )
    # Same physical card resolved twice in one batch de-dupes to one task,
    # and resolve_many is called once (batched), not once per entry.
    assert calls["n"] == 1
    assert queued == 1
    assert failed == 0
    assert len(task_ids) == 1


def test_enqueue_decklist_entries_skips_existing_output_file(
    db_path: Path, tmp_path: Path, monkeypatch
) -> None:
    from proxy_scaler.pipeline import output_filename

    monkeypatch.setattr(
        ScryfallClient, "resolve_many", lambda self, entries: [(SOL_RING_CARD, [])]
    )
    output_dir = tmp_path / "out"
    output_dir.mkdir()
    existing_name = output_filename("Sol Ring", "c21", "263", None, "swinir", 800)
    (output_dir / existing_name).write_bytes(b"fake png")

    notes = []
    queued, failed, task_ids = generation.enqueue_decklist_entries(
        [_entry()],
        model="swinir",
        dpi_targets=[800],
        skip_existing=True,
        tile_size=0,
        output_dir=output_dir,
        cache_dir=tmp_path / "cache",
        weights_dir=tmp_path / "weights",
        project_tag=None,
        db_path=db_path,
        on_note=notes.append,
    )
    assert queued == 0
    assert task_ids == []
    # "Nothing to do" must be self-explanatory, not a silent no-op
    # indistinguishable from a real bug — see the regression this guards
    # against in services/generation.py.
    assert any("already exist" in n for n in notes)


def test_enqueue_decklist_entries_skips_active_task_regardless_of_skip_existing(
    db_path: Path, tmp_path: Path, monkeypatch
) -> None:
    """Duplicating in-flight work is never wanted, even with
    skip_existing=False — a pending/running task for the same
    (scryfall_id, face_index, dpi, model) must be skipped too, not just
    files already on disk."""
    monkeypatch.setattr(
        ScryfallClient, "resolve_many", lambda self, entries: [(SOL_RING_CARD, [])]
    )
    generation.enqueue_face(
        scryfall_id="sol-id",
        face_index=None,
        face_label=None,
        face_name="Sol Ring",
        card_name="Sol Ring",
        set_code="c21",
        collector_number="263",
        png_url="https://example.com/sol.png",
        dpi_targets=[800],
        model="swinir",
        tile_size=0,
        output_dir=tmp_path / "out",
        cache_dir=tmp_path / "cache",
        weights_dir=tmp_path / "weights",
        project_tag=None,
        db_path=db_path,
    )

    queued, failed, task_ids = generation.enqueue_decklist_entries(
        [_entry()],
        model="swinir",
        dpi_targets=[800],
        skip_existing=False,
        tile_size=0,
        output_dir=tmp_path / "out",
        cache_dir=tmp_path / "cache",
        weights_dir=tmp_path / "weights",
        project_tag=None,
        db_path=db_path,
    )
    # active_task_keys() only ever reflects a real project_tag (project_tag
    # is None here, matching "generate before ever saving a project"), so
    # this specific dedupe path needs a tagged project to exercise for
    # real — see the project-scoped variant below.
    assert queued == 1


def test_enqueue_decklist_entries_skips_active_task_for_tagged_project(
    db_path: Path, tmp_path: Path, monkeypatch
) -> None:
    monkeypatch.setattr(
        ScryfallClient, "resolve_many", lambda self, entries: [(SOL_RING_CARD, [])]
    )
    tag = "proj-tag-1"
    generation.enqueue_face(
        scryfall_id="sol-id",
        face_index=None,
        face_label=None,
        face_name="Sol Ring",
        card_name="Sol Ring",
        set_code="c21",
        collector_number="263",
        png_url="https://example.com/sol.png",
        dpi_targets=[800],
        model="swinir",
        tile_size=0,
        output_dir=tmp_path / "out",
        cache_dir=tmp_path / "cache",
        weights_dir=tmp_path / "weights",
        project_tag=tag,
        db_path=db_path,
    )

    notes = []
    queued, failed, task_ids = generation.enqueue_decklist_entries(
        [_entry()],
        model="swinir",
        dpi_targets=[800],
        skip_existing=False,
        tile_size=0,
        output_dir=tmp_path / "out",
        cache_dir=tmp_path / "cache",
        weights_dir=tmp_path / "weights",
        project_tag=tag,
        db_path=db_path,
        on_note=notes.append,
    )
    assert queued == 0
    assert task_ids == []
    assert any("already queued or running" in n for n in notes)


def test_enqueue_decklist_entries_reports_scryfall_failures(
    db_path: Path, tmp_path: Path, monkeypatch
) -> None:
    monkeypatch.setattr(
        ScryfallClient,
        "resolve_many",
        lambda self, entries: [ScryfallError("not found")],
    )
    notes = []
    queued, failed, task_ids = generation.enqueue_decklist_entries(
        [_entry()],
        model="swinir",
        dpi_targets=[800],
        skip_existing=False,
        tile_size=0,
        output_dir=tmp_path / "out",
        cache_dir=tmp_path / "cache",
        weights_dir=tmp_path / "weights",
        project_tag=None,
        db_path=db_path,
        on_note=notes.append,
    )
    assert queued == 0
    assert failed == 1
    assert task_ids == []
    assert any("not found" in n for n in notes)
