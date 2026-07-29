"""Round-trip tests for SQLite project persistence."""

from __future__ import annotations

from pathlib import Path

import pytest

from proxy_scaler.db import (
    ProjectCardRow,
    ProjectSettings,
    TaskRow,
    acquire_worker_lock,
    add_cards_to_project,
    cancel_task,
    claim_next_task,
    delete_all_projects,
    delete_project,
    enqueue_task,
    ensure_worker_running,
    get_task,
    init_db,
    is_worker_running,
    list_gallery_items_for_project,
    list_project_cards,
    list_projects,
    list_tasks,
    load_project,
    mark_task_done,
    mark_task_failed,
    parse_output_filename,
    release_worker_lock,
    remove_project_card,
    save_project,
    scan_gallery_from_output,
    upsert_gallery_item_for_task,
)
from proxy_scaler.pipeline import FaceResult


@pytest.fixture()
def db_path(tmp_path: Path) -> Path:
    path = tmp_path / "test.db"
    init_db(path)
    return path


def _fake_task(project_id: int | None, **overrides) -> TaskRow:
    """A minimal TaskRow for exercising upsert_gallery_item_for_task()
    directly, without going through enqueue_task/claim_next_task — only
    the identity fields it reads (project_id/set_code/collector_number/
    card_name/face_name) matter for matching against project_cards."""
    kwargs = dict(
        id=1,
        project_id=project_id,
        status="running",
        scryfall_id="x",
        face_index=None,
        face_label=None,
        face_name="X",
        card_name="X",
        set_code="c21",
        collector_number="263",
        png_url="",
        dpi=800,
        model="swinir",
        tile_size=0,
        output_dir="",
        cache_dir="",
        weights_dir="",
        error=None,
        created_at="2026-01-01T00:00:00Z",
        started_at=None,
        completed_at=None,
    )
    kwargs.update(overrides)
    return TaskRow(**kwargs)


def _card_dict(
    *, scryfall_id: str, card_name: str, set_code: str, collector_number: str, quantity: int = 1
) -> dict:
    return {
        "scryfall_id": scryfall_id,
        "card_name": card_name,
        "set_code": set_code,
        "collector_number": collector_number,
        "quantity": quantity,
        "original_import_line": f"{quantity} {card_name} ({set_code}) {collector_number}",
    }


def test_save_load_round_trip(db_path: Path) -> None:
    deck = "1 Sol Ring (c21) 263\n1 Lightning Bolt\n"
    settings = ProjectSettings(
        model="realesrnet",
        dpi_targets=[600, 1200],
        page_size=4,
        skip_existing=False,
        output_dir="/tmp/out",
        cache_dir="/tmp/cache",
        weights_dir="/tmp/weights",
        tile_size=384,
    )

    pid = save_project(
        "Test Deck",
        import_decklist_text=deck,
        settings=settings,
        db_path=db_path,
    )
    assert pid > 0

    add_cards_to_project(
        pid,
        [_card_dict(scryfall_id="abc-123", card_name="Sol Ring", set_code="c21", collector_number="263")],
        db_path=db_path,
    )
    result = FaceResult(
        out_path=Path("/tmp/out/Sol_Ring-C21-263-realesrnet-1200dpi.png"),
        original_path=Path("/tmp/cache/orig.png"),
        scryfall_id="abc-123",
        face_index=None,
        face_name="Sol Ring",
        card_name="Sol Ring",
        set_code="c21",
        collector_number="263",
        png_url="https://example.com/x.png",
        dpi=1200,
        model="realesrnet",
    )
    upsert_gallery_item_for_task(
        _fake_task(
            pid,
            scryfall_id="abc-123",
            set_code="c21",
            collector_number="263",
            card_name="Sol Ring",
            face_name="Sol Ring",
        ),
        result,
        db_path=db_path,
    )

    projects = list_projects(db_path)
    assert len(projects) == 1
    assert projects[0].name == "Test Deck"

    loaded = load_project(pid, db_path=db_path)
    assert loaded.name == "Test Deck"
    assert loaded.import_decklist_text == deck
    assert loaded.settings.model == "realesrnet"
    assert loaded.settings.dpi_targets == [600, 1200]
    assert loaded.settings.page_size == 4
    assert loaded.settings.skip_existing is False
    assert loaded.settings.output_dir == "/tmp/out"
    assert loaded.settings.tile_size == 384
    assert len(loaded.cards) == 1
    assert loaded.cards[0].card_name == "Sol Ring"
    assert len(loaded.gallery) == 1
    assert loaded.gallery[0]["scryfall_id"] == "abc-123"
    assert loaded.gallery[0]["set_code"] == "c21"
    assert loaded.gallery[0]["dpi"] == 1200


def test_save_does_not_touch_cards_or_gallery(db_path: Path) -> None:
    """Regression test: save_project() must NOT replace project_cards/
    project_gallery_items. It used to wipe-and-rebuild both from whatever
    import_decklist_text was in the box at save time — the actual root
    cause of generated images silently disappearing whenever the box
    didn't exactly match what had been generated. Cards/gallery are now
    persistent and survive repeated saves untouched, even with completely
    different text pasted in."""
    pid = save_project(
        "Persistent",
        import_decklist_text="1 Sol Ring (c21) 263\n",
        settings=ProjectSettings(),
        db_path=db_path,
    )
    add_cards_to_project(
        pid,
        [_card_dict(scryfall_id="a", card_name="Sol Ring", set_code="c21", collector_number="263")],
        db_path=db_path,
    )
    result = FaceResult(
        out_path=Path("/o/a.png"),
        original_path=Path("/c/a.png"),
        scryfall_id="a",
        face_index=None,
        face_name="Sol Ring",
        card_name="Sol Ring",
        set_code="c21",
        collector_number="263",
        png_url="",
        dpi=800,
        model="swinir",
    )
    upsert_gallery_item_for_task(
        _fake_task(pid, scryfall_id="a", set_code="c21", collector_number="263", card_name="Sol Ring", face_name="Sol Ring"),
        result,
        db_path=db_path,
    )

    pid2 = save_project(
        "Persistent",
        import_decklist_text="1 Lightning Bolt (lea) 161\n",
        settings=ProjectSettings(dpi_targets=[600]),
        project_id=pid,
        db_path=db_path,
    )
    assert pid2 == pid
    loaded = load_project(pid, db_path=db_path)
    assert "Lightning Bolt" in loaded.import_decklist_text
    assert loaded.settings.dpi_targets == [600]
    assert len(loaded.cards) == 1
    assert loaded.cards[0].card_name == "Sol Ring"
    assert len(loaded.gallery) == 1
    assert loaded.gallery[0]["scryfall_id"] == "a"


def test_delete_cascades(db_path: Path) -> None:
    pid = save_project(
        "Gone",
        import_decklist_text="1 Sol Ring (c21) 263\n",
        settings=ProjectSettings(),
        db_path=db_path,
    )
    add_cards_to_project(
        pid,
        [_card_dict(scryfall_id="a", card_name="Sol Ring", set_code="c21", collector_number="263")],
        db_path=db_path,
    )
    delete_project(pid, db_path=db_path)
    assert list_projects(db_path) == []
    with pytest.raises(ValueError, match="not found"):
        load_project(pid, db_path=db_path)


def test_delete_all_projects_cascades(db_path: Path) -> None:
    pid_a = save_project(
        "First",
        import_decklist_text="1 Sol Ring (c21) 263\n",
        settings=ProjectSettings(),
        db_path=db_path,
    )
    add_cards_to_project(
        pid_a,
        [_card_dict(scryfall_id="a", card_name="Sol Ring", set_code="c21", collector_number="263")],
        db_path=db_path,
    )
    result = FaceResult(
        out_path=Path("/o/a.png"),
        original_path=Path("/c/a.png"),
        scryfall_id="a",
        face_index=None,
        face_name="Sol Ring",
        card_name="Sol Ring",
        set_code="c21",
        collector_number="263",
        png_url="",
        dpi=800,
        model="swinir",
    )
    upsert_gallery_item_for_task(
        _fake_task(pid_a, scryfall_id="a", set_code="c21", collector_number="263", card_name="Sol Ring", face_name="Sol Ring"),
        result,
        db_path=db_path,
    )
    pid_b = save_project(
        "Second",
        import_decklist_text="1 Lightning Bolt (lea) 161\n",
        settings=ProjectSettings(),
        db_path=db_path,
    )
    add_cards_to_project(
        pid_b,
        [_card_dict(scryfall_id="b", card_name="Lightning Bolt", set_code="lea", collector_number="161")],
        db_path=db_path,
    )
    assert len(list_projects(db_path)) == 2

    delete_all_projects(db_path=db_path)

    assert list_projects(db_path) == []
    with pytest.raises(ValueError, match="not found"):
        load_project(pid_a, db_path=db_path)
    with pytest.raises(ValueError, match="not found"):
        load_project(pid_b, db_path=db_path)

    import sqlite3

    conn = sqlite3.connect(str(db_path))
    try:
        assert conn.execute("SELECT COUNT(*) FROM project_cards").fetchone()[0] == 0
        assert (
            conn.execute("SELECT COUNT(*) FROM project_gallery_items").fetchone()[0]
            == 0
        )
    finally:
        conn.close()


def test_save_requires_name(db_path: Path) -> None:
    with pytest.raises(ValueError, match="required"):
        save_project(
            "  ",
            import_decklist_text="",
            settings=ProjectSettings(),
            db_path=db_path,
        )


def test_rename_collision(db_path: Path) -> None:
    a = save_project(
        "Alpha",
        import_decklist_text="",
        settings=ProjectSettings(),
        db_path=db_path,
    )
    save_project(
        "Beta",
        import_decklist_text="",
        settings=ProjectSettings(),
        db_path=db_path,
    )
    with pytest.raises(ValueError, match="already exists"):
        save_project(
            "Beta",
            import_decklist_text="",
            settings=ProjectSettings(),
            project_id=a,
            db_path=db_path,
        )


def test_parse_output_filename() -> None:
    meta = parse_output_filename("Abandoned_Air_Temple-TLA-263-swinir-600dpi.png")
    assert meta is not None
    assert meta["set_code"] == "tla"
    assert meta["collector_number"] == "263"
    assert meta["model"] == "swinir"
    assert meta["dpi"] == 600
    assert meta["face_label"] is None

    dfc = parse_output_filename(
        "Dion_Bahamuts_Dominant-FIN-376-front-swinir-800dpi.png"
    )
    assert dfc is not None
    assert dfc["face_label"] == "front"
    assert dfc["face_index"] == 0

    hyphen_collector = parse_output_filename(
        "Knight_Exemplar-PLST-DDG-14-swinir-600dpi.png"
    )
    assert hyphen_collector is not None
    assert hyphen_collector["set_code"] == "plst"
    assert hyphen_collector["collector_number"] == "DDG-14"

    # Underscored model values (e.g. from the newer model options) must not
    # be truncated by a shorter alternative sharing a prefix
    # (realesrgan_anime vs realesrgan).
    anime = parse_output_filename("Sol_Ring-C21-263-realesrgan_anime-800dpi.png")
    assert anime is not None
    assert anime["model"] == "realesrgan_anime"
    assert anime["dpi"] == 800

    hat = parse_output_filename("Sol_Ring-C21-263-hat-1200dpi.png")
    assert hat is not None
    assert hat["model"] == "hat"


def test_load_recovers_gallery_from_output(db_path: Path, tmp_path: Path) -> None:
    out = tmp_path / "output"
    out.mkdir()
    (out / "Sol_Ring-C21-263-swinir-800dpi.png").write_bytes(b"fake")
    (out / "unrelated.png").write_bytes(b"x")

    pid = save_project(
        "Recover",
        import_decklist_text="1 Sol Ring (c21) 263\n",
        settings=ProjectSettings(output_dir=str(out)),
        db_path=db_path,
    )
    add_cards_to_project(
        pid,
        [_card_dict(scryfall_id="sol-id", card_name="Sol Ring", set_code="c21", collector_number="263")],
        db_path=db_path,
    )
    loaded = load_project(pid, db_path=db_path)
    assert len(loaded.gallery) == 1
    assert loaded.gallery[0]["set_code"] == "c21"
    assert loaded.gallery[0]["dpi"] == 800
    assert "Sol_Ring" in loaded.gallery[0]["out_path"]


def test_load_merges_on_disk_variant_missing_from_saved_gallery(
    db_path: Path, tmp_path: Path
) -> None:
    """A project's saved gallery can lag behind disk — e.g. an interrupted
    generate run, or files written before this project's last save. Loading
    must still surface an on-disk 800dpi file even though the saved
    gallery only ever recorded the 1200dpi variant (previously this only
    triggered a disk scan when the whole gallery was empty)."""
    out = tmp_path / "output"
    out.mkdir()
    (out / "Sol_Ring-C21-263-swinir-800dpi.png").write_bytes(b"fake-800")
    (out / "Sol_Ring-C21-263-swinir-1200dpi.png").write_bytes(b"fake-1200")

    pid = save_project(
        "Partial",
        import_decklist_text="1 Sol Ring (c21) 263\n",
        settings=ProjectSettings(output_dir=str(out)),
        db_path=db_path,
    )
    add_cards_to_project(
        pid,
        [_card_dict(scryfall_id="sol-id", card_name="Sol Ring", set_code="c21", collector_number="263")],
        db_path=db_path,
    )
    result = FaceResult(
        out_path=Path(str(out / "Sol_Ring-C21-263-swinir-1200dpi.png")),
        original_path=Path("/c/x.png"),
        scryfall_id="sol-id",
        face_index=None,
        face_name="Sol Ring",
        card_name="Sol Ring",
        set_code="c21",
        collector_number="263",
        png_url="",
        dpi=1200,
        model="swinir",
    )
    upsert_gallery_item_for_task(
        _fake_task(pid, scryfall_id="sol-id", set_code="c21", collector_number="263", card_name="Sol Ring", face_name="Sol Ring"),
        result,
        db_path=db_path,
    )
    loaded = load_project(pid, db_path=db_path)
    dpis = {g["dpi"] for g in loaded.gallery}
    assert dpis == {800, 1200}
    # The 1200dpi entry keeps its DB-recorded scryfall_id (not clobbered by
    # the disk-scanned duplicate, which would have an empty scryfall_id).
    twelve_hundred = next(g for g in loaded.gallery if g["dpi"] == 1200)
    assert twelve_hundred["scryfall_id"] == "sol-id"
    # The disk-recovered 800dpi entry backfills original_path/scryfall_id
    # from its sibling 1200dpi entry — a filename alone can't tell it where
    # the cached original lives, but the two variants share the same
    # original card image, so reusing the sibling's path is correct. Without
    # this, a UI sorting variants by DPI would show this 800dpi entry first
    # and its "Original" column would incorrectly say the file is missing.
    eight_hundred = next(g for g in loaded.gallery if g["dpi"] == 800)
    assert eight_hundred["original_path"] == "/c/x.png"
    assert eight_hundred["scryfall_id"] == "sol-id"


def test_scan_gallery_from_output(tmp_path: Path) -> None:
    from proxy_scaler.decklist import parse_decklist_text

    out = tmp_path / "output"
    out.mkdir()
    (out / "Sol_Ring-C21-263-swinir-600dpi.png").write_bytes(b"a")
    (out / "Sol_Ring-C21-263-swinir-800dpi.png").write_bytes(b"b")
    entries = parse_decklist_text("1 Sol Ring (c21) 263\n")
    gallery = scan_gallery_from_output(out, entries)
    assert len(gallery) == 2
    assert {g["dpi"] for g in gallery} == {600, 800}


# --- Task queue -------------------------------------------------------


def _enqueue_sol_ring(db_path: Path, **overrides) -> int:
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
        output_dir="/tmp/out",
        cache_dir="/tmp/cache",
        weights_dir="/tmp/weights",
        db_path=db_path,
    )
    kwargs.update(overrides)
    return enqueue_task(kwargs.pop("project_id"), **kwargs)


def test_enqueue_and_claim_task(db_path: Path) -> None:
    tid = _enqueue_sol_ring(db_path)
    [pending] = list_tasks(db_path=db_path)
    assert pending.id == tid
    assert pending.status == "pending"

    claimed = claim_next_task(db_path=db_path)
    assert claimed is not None
    assert claimed.id == tid
    assert claimed.status == "running"
    assert claimed.started_at is not None

    # Nothing else pending — a second claim finds nothing.
    assert claim_next_task(db_path=db_path) is None


def test_claim_returns_oldest_pending_first(db_path: Path) -> None:
    first = _enqueue_sol_ring(db_path, collector_number="1")
    _enqueue_sol_ring(db_path, collector_number="2")
    claimed = claim_next_task(db_path=db_path)
    assert claimed.id == first


def test_mark_task_done_and_failed(db_path: Path) -> None:
    tid_done = _enqueue_sol_ring(db_path, collector_number="1")
    tid_failed = _enqueue_sol_ring(db_path, collector_number="2")

    mark_task_done(tid_done, db_path=db_path)
    mark_task_failed(tid_failed, "boom", db_path=db_path)

    done = get_task(tid_done, db_path=db_path)
    failed = get_task(tid_failed, db_path=db_path)
    assert done.status == "done"
    assert done.completed_at is not None
    assert failed.status == "failed"
    assert failed.error == "boom"
    assert failed.completed_at is not None


def test_cancel_task_only_affects_pending(db_path: Path) -> None:
    tid = _enqueue_sol_ring(db_path)
    assert cancel_task(tid, db_path=db_path) is True
    assert get_task(tid, db_path=db_path).status == "canceled"
    # Already canceled — a second cancel is a no-op.
    assert cancel_task(tid, db_path=db_path) is False

    tid2 = _enqueue_sol_ring(db_path, collector_number="99")
    claim_next_task(db_path=db_path)  # now running, not pending
    assert cancel_task(tid2, db_path=db_path) is False
    assert get_task(tid2, db_path=db_path).status == "running"


def test_list_tasks_filters_by_project_and_status(db_path: Path) -> None:
    pid_a = save_project(
        "A", import_decklist_text="", settings=ProjectSettings(), db_path=db_path
    )
    pid_b = save_project(
        "B", import_decklist_text="", settings=ProjectSettings(), db_path=db_path
    )
    _enqueue_sol_ring(db_path, project_id=pid_a, collector_number="1")
    t2 = _enqueue_sol_ring(db_path, project_id=pid_a, collector_number="2")
    _enqueue_sol_ring(db_path, project_id=pid_b, collector_number="3")
    mark_task_done(t2, db_path=db_path)

    a_tasks = list_tasks(project_id=pid_a, db_path=db_path)
    assert len(a_tasks) == 2
    assert all(t.project_id == pid_a for t in a_tasks)

    a_pending = list_tasks(project_id=pid_a, statuses=["pending"], db_path=db_path)
    assert len(a_pending) == 1

    all_tasks = list_tasks(db_path=db_path)
    assert len(all_tasks) == 3


def test_upsert_gallery_item_for_task_writes_and_updates(db_path: Path) -> None:
    pid = save_project(
        "P",
        import_decklist_text="1 Sol Ring (c21) 263\n",
        settings=ProjectSettings(),
        db_path=db_path,
    )
    add_cards_to_project(
        pid,
        [_card_dict(scryfall_id="sol-id", card_name="Sol Ring", set_code="c21", collector_number="263")],
        db_path=db_path,
    )
    tid = _enqueue_sol_ring(db_path, project_id=pid)
    task = claim_next_task(db_path=db_path)
    assert task.id == tid

    result = FaceResult(
        out_path=Path("/o/Sol_Ring-C21-263-swinir-800dpi.png"),
        original_path=Path("/c/orig.png"),
        scryfall_id="sol-id",
        face_index=None,
        face_name="Sol Ring",
        card_name="Sol Ring",
        set_code="c21",
        collector_number="263",
        png_url="https://example.com/sol.png",
        dpi=800,
        model="swinir",
        device="gpu",
    )
    upsert_gallery_item_for_task(task, result, db_path=db_path)
    items = list_gallery_items_for_project(pid, db_path=db_path)
    assert len(items) == 1
    assert items[0]["device"] == "gpu"

    # Re-upserting the same (card, scryfall_id, face_index, model, dpi)
    # updates in place rather than duplicating — e.g. a later regen of the
    # same variant.
    updated_result = FaceResult(**{**result.__dict__, "device": "cpu"})
    upsert_gallery_item_for_task(task, updated_result, db_path=db_path)
    items2 = list_gallery_items_for_project(pid, db_path=db_path)
    assert len(items2) == 1
    assert items2[0]["device"] == "cpu"


def test_upsert_gallery_item_for_task_noop_without_project(db_path: Path) -> None:
    tid = _enqueue_sol_ring(db_path, project_id=None)
    task = get_task(tid, db_path=db_path)
    result = FaceResult(
        out_path=Path("/o/x.png"),
        original_path=Path("/c/x.png"),
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
    # Should not raise even though there's no project to attach to.
    upsert_gallery_item_for_task(task, result, db_path=db_path)


def test_worker_lock_prevents_double_acquire(tmp_path: Path) -> None:
    lock_path = tmp_path / "worker.lock"
    assert is_worker_running(lock_path) is False

    fd = acquire_worker_lock(lock_path)
    assert fd is not None
    assert is_worker_running(lock_path) is True
    assert acquire_worker_lock(lock_path) is None  # already held

    release_worker_lock(fd)
    assert is_worker_running(lock_path) is False


def test_ensure_worker_running_spawns_only_when_not_already_running(
    tmp_path: Path, monkeypatch
) -> None:
    lock_path = tmp_path / "worker.lock"
    log_path = tmp_path / "worker.log"
    calls = []
    monkeypatch.setattr(
        "proxy_scaler.db.subprocess.Popen",
        lambda *a, **kw: calls.append((a, kw)),
    )

    ensure_worker_running(lock_path, log_path)
    assert len(calls) == 1

    # A live lock (simulating an already-running worker) must prevent a
    # second spawn.
    fd = acquire_worker_lock(lock_path)
    ensure_worker_running(lock_path, log_path)
    assert len(calls) == 1
    release_worker_lock(fd)


# --- Persistent project_cards -------------------------------------------


def _new_project(db_path: Path, name: str = "Cards Test") -> int:
    return save_project(
        name, import_decklist_text="", settings=ProjectSettings(), db_path=db_path
    )


def test_add_cards_to_project_inserts_in_order(db_path: Path) -> None:
    pid = _new_project(db_path)
    added = add_cards_to_project(
        pid,
        [
            _card_dict(scryfall_id="a", card_name="Sol Ring", set_code="c21", collector_number="263"),
            _card_dict(scryfall_id="b", card_name="Lightning Bolt", set_code="lea", collector_number="161"),
        ],
        db_path=db_path,
    )
    assert added == 2
    cards = list_project_cards(pid, db_path=db_path)
    assert [c.card_name for c in cards] == ["Sol Ring", "Lightning Bolt"]
    assert [c.sort_order for c in cards] == [0, 1]
    assert all(isinstance(c, ProjectCardRow) for c in cards)


def test_add_cards_to_project_dedupes_by_scryfall_id(db_path: Path) -> None:
    pid = _new_project(db_path)
    add_cards_to_project(
        pid,
        [_card_dict(scryfall_id="a", card_name="Sol Ring", set_code="c21", collector_number="263")],
        db_path=db_path,
    )
    # Re-importing the same card (even under a different nominal name) is a
    # no-op — scryfall_id is the strongest identity signal.
    added = add_cards_to_project(
        pid,
        [_card_dict(scryfall_id="a", card_name="Sol Ring (again)", set_code="c21", collector_number="263")],
        db_path=db_path,
    )
    assert added == 0
    assert len(list_project_cards(pid, db_path=db_path)) == 1


def test_add_cards_to_project_dedupes_by_set_and_collector(db_path: Path) -> None:
    """No scryfall_id known (unresolved) but the same printing — still
    deduped via set+collector, matching _match_card_id's precedence."""
    pid = _new_project(db_path)
    add_cards_to_project(
        pid,
        [{"scryfall_id": None, "card_name": "Sol Ring", "set_code": "c21", "collector_number": "263", "quantity": 1, "original_import_line": "1 Sol Ring (c21) 263"}],
        db_path=db_path,
    )
    added = add_cards_to_project(
        pid,
        [{"scryfall_id": None, "card_name": "Sol Ring", "set_code": "c21", "collector_number": "263", "quantity": 1, "original_import_line": "1 Sol Ring (c21) 263"}],
        db_path=db_path,
    )
    assert added == 0


def test_add_cards_to_project_dedupes_within_same_batch(db_path: Path) -> None:
    """The same card pasted twice in one Import must only be added once."""
    pid = _new_project(db_path)
    added = add_cards_to_project(
        pid,
        [
            _card_dict(scryfall_id="a", card_name="Sol Ring", set_code="c21", collector_number="263"),
            _card_dict(scryfall_id="a", card_name="Sol Ring", set_code="c21", collector_number="263"),
        ],
        db_path=db_path,
    )
    assert added == 1
    assert len(list_project_cards(pid, db_path=db_path)) == 1


def test_add_cards_to_project_different_cards_both_added(db_path: Path) -> None:
    pid = _new_project(db_path)
    added = add_cards_to_project(
        pid,
        [
            _card_dict(scryfall_id="a", card_name="Sol Ring", set_code="c21", collector_number="263"),
            _card_dict(scryfall_id="b", card_name="Lightning Bolt", set_code="lea", collector_number="161"),
        ],
        db_path=db_path,
    )
    assert added == 2


def test_remove_project_card_cascades_gallery_not_tasks(db_path: Path) -> None:
    pid = _new_project(db_path)
    add_cards_to_project(
        pid,
        [_card_dict(scryfall_id="a", card_name="Sol Ring", set_code="c21", collector_number="263")],
        db_path=db_path,
    )
    [card] = list_project_cards(pid, db_path=db_path)
    result = FaceResult(
        out_path=Path("/o/a.png"),
        original_path=Path("/c/a.png"),
        scryfall_id="a",
        face_index=None,
        face_name="Sol Ring",
        card_name="Sol Ring",
        set_code="c21",
        collector_number="263",
        png_url="",
        dpi=800,
        model="swinir",
    )
    upsert_gallery_item_for_task(
        _fake_task(pid, scryfall_id="a", set_code="c21", collector_number="263", card_name="Sol Ring", face_name="Sol Ring"),
        result,
        db_path=db_path,
    )
    tid = enqueue_task(
        pid,
        scryfall_id="a",
        face_index=None,
        face_label=None,
        face_name="Sol Ring",
        card_name="Sol Ring",
        set_code="c21",
        collector_number="263",
        png_url="",
        dpi=1200,
        model="swinir",
        output_dir="/tmp/out",
        cache_dir="/tmp/cache",
        weights_dir="/tmp/weights",
        db_path=db_path,
    )
    assert len(list_gallery_items_for_project(pid, db_path=db_path)) == 1

    remove_project_card(card.id, db_path=db_path)

    assert list_project_cards(pid, db_path=db_path) == []
    assert list_gallery_items_for_project(pid, db_path=db_path) == []
    # The in-flight task itself is untouched (no FK to project_cards) —
    # it'll just fail to attach a gallery row once it completes.
    assert get_task(tid, db_path=db_path) is not None
