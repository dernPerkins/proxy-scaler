"""Round-trip tests for the generation server's SQLite persistence: the
task queue and the gallery of completed images, both scoped by an opaque
project_tag string (see ARCHITECTURE.md). Project management itself
(decklist text, project CRUD, settings) lives in the desktop app now, not
here — there's nothing to test on the Python side for that any more."""

from __future__ import annotations

import sqlite3
from pathlib import Path

import pytest

from proxy_scaler import db as db_module
from proxy_scaler.db import (
    TaskRow,
    acquire_worker_lock,
    cancel_pending_tasks_for_tag,
    cancel_task,
    claim_next_task,
    enqueue_task,
    get_gallery_item,
    get_task,
    init_db,
    is_worker_running,
    list_gallery_items,
    list_tasks,
    mark_task_done,
    mark_task_failed,
    parse_output_filename,
    release_worker_lock,
    scan_gallery_from_output,
    upsert_gallery_item_for_task,
)
from proxy_scaler.pipeline import FaceResult


@pytest.fixture()
def db_path(tmp_path: Path) -> Path:
    path = tmp_path / "test.db"
    init_db(path)
    return path


def _fake_task(project_tag: str | None, **overrides) -> TaskRow:
    """A minimal TaskRow for exercising upsert_gallery_item_for_task()
    directly, without going through enqueue_task/claim_next_task."""
    kwargs = dict(
        id=1,
        project_tag=project_tag,
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
        model="ultrasharp_v2",
        tile_size=0,
        output_dir="",
        cache_dir="",
        weights_dir="",
        error=None,
        created_at="2026-01-01T00:00:00Z",
        started_at=None,
        completed_at=None,
        total_faces=None,
    )
    kwargs.update(overrides)
    return TaskRow(**kwargs)


def _result(**overrides) -> FaceResult:
    kwargs = dict(
        out_path=Path("/o/Sol_Ring-C21-263-ultrasharp_v2-800dpi.png"),
        original_path=Path("/c/orig.png"),
        scryfall_id="sol-id",
        face_index=None,
        face_name="Sol Ring",
        card_name="Sol Ring",
        set_code="c21",
        collector_number="263",
        png_url="https://example.com/sol.png",
        dpi=800,
        model="ultrasharp_v2",
    )
    kwargs.update(overrides)
    return FaceResult(**kwargs)


def test_parse_output_filename() -> None:
    meta = parse_output_filename("Abandoned_Air_Temple-TLA-263-ultrasharp_v2-600dpi.png")
    assert meta is not None
    assert meta["set_code"] == "tla"
    assert meta["collector_number"] == "263"
    assert meta["model"] == "ultrasharp_v2"
    assert meta["dpi"] == 600
    assert meta["face_label"] is None

    dfc = parse_output_filename(
        "Dion_Bahamuts_Dominant-FIN-376-front-ultrasharp_v2-800dpi.png"
    )
    assert dfc is not None
    assert dfc["face_label"] == "front"
    assert dfc["face_index"] == 0

    hyphen_collector = parse_output_filename(
        "Knight_Exemplar-PLST-DDG-14-ultrasharp_v2-600dpi.png"
    )
    assert hyphen_collector is not None
    assert hyphen_collector["set_code"] == "plst"
    assert hyphen_collector["collector_number"] == "DDG-14"

    # Underscored model slugs must parse whole, never truncated at an
    # underscore (this regressed when realesrgan_anime_fast was missing
    # from the alternation and half-matched realesrgan_anime).
    anime_fast = parse_output_filename(
        "Sol_Ring-C21-263-realesrgan_anime_fast-800dpi.png"
    )
    assert anime_fast is not None
    assert anime_fast["model"] == "realesrgan_anime_fast"
    assert anime_fast["dpi"] == 800

    janai = parse_output_filename("Sol_Ring-C21-263-illustrationjanai-1200dpi.png")
    assert janai is not None
    assert janai["model"] == "illustrationjanai"

    # Set codes may start with a digit (40K, 2X2).
    digit_set = parse_output_filename(
        "And_They_Shall_Know_No_Fear-40K-9-ultrasharp_v2-1200dpi.png"
    )
    assert digit_set is not None
    assert digit_set["set_code"] == "40k"
    assert digit_set["collector_number"] == "9"

    # Removed models no longer parse.
    assert parse_output_filename("Sol_Ring-C21-263-swinir-600dpi.png") is None


def test_scan_gallery_from_output(tmp_path: Path) -> None:
    from proxy_scaler.decklist import parse_decklist_text

    out = tmp_path / "output"
    out.mkdir()
    (out / "Sol_Ring-C21-263-ultrasharp_v2-600dpi.png").write_bytes(b"a")
    (out / "Sol_Ring-C21-263-ultrasharp_v2-800dpi.png").write_bytes(b"b")
    entries = parse_decklist_text("1 Sol Ring (c21) 263\n")
    gallery = scan_gallery_from_output(out, entries)
    assert len(gallery) == 2
    assert {g["dpi"] for g in gallery} == {600, 800}


def test_adopt_gallery_items_copies_matching_rows(db_path: Path, tmp_path: Path) -> None:
    from proxy_scaler.decklist import DeckEntry

    img = tmp_path / "sol.png"
    img.write_bytes(b"png")
    db_module.upsert_gallery_item(
        "tag-a", _result(out_path=img, original_path=img), db_path=db_path
    )

    entry = DeckEntry(quantity=1, name="Sol Ring", set_code="c21", collector_number="263")
    assert db_module.adopt_gallery_items("tag-b", [entry], db_path=db_path) == 1

    [item] = list_gallery_items("tag-b", db_path=db_path)
    assert item["card_name"] == "Sol Ring"
    assert item["model"] == "ultrasharp_v2"
    assert item["out_path"] == str(img)

    # Idempotent: the variant now exists under tag-b, so nothing re-copies.
    assert db_module.adopt_gallery_items("tag-b", [entry], db_path=db_path) == 0
    # And the source rows are untouched.
    assert len(list_gallery_items("tag-a", db_path=db_path)) == 1


def test_adopt_gallery_items_matches_name_only_entries(db_path: Path, tmp_path: Path) -> None:
    from proxy_scaler.decklist import DeckEntry

    img = tmp_path / "sol.png"
    img.write_bytes(b"png")
    db_module.upsert_gallery_item(
        "tag-a", _result(out_path=img, original_path=img), db_path=db_path
    )

    # A bare "1 Sol Ring" line has no printing to match on — name it is.
    entry = DeckEntry(quantity=1, name="sol ring")
    assert db_module.adopt_gallery_items("tag-b", [entry], db_path=db_path) == 1

    # A different card adopts nothing.
    other = DeckEntry(quantity=1, name="Lightning Bolt")
    assert db_module.adopt_gallery_items("tag-c", [other], db_path=db_path) == 0


def test_adopt_gallery_items_scans_output_dir_for_rowless_files(
    db_path: Path, tmp_path: Path
) -> None:
    """Files with no gallery row anywhere (pre-reshape or CLI-produced)
    register from their filename alone, and a later real generation
    replaces the placeholder instead of duplicating it."""
    from proxy_scaler.decklist import DeckEntry

    out = tmp_path / "output"
    out.mkdir()
    img = out / "Sol_Ring-C21-263-ultrasharp_v2-800dpi.png"
    img.write_bytes(b"png")

    entry = DeckEntry(quantity=1, name="Sol Ring", set_code="c21", collector_number="263")
    assert (
        db_module.adopt_gallery_items("tag-b", [entry], db_path=db_path, output_dir=out) == 1
    )
    [item] = list_gallery_items("tag-b", db_path=db_path)
    assert item["scryfall_id"] == ""
    assert item["model"] == "ultrasharp_v2"
    assert item["dpi"] == 800
    assert item["created_at"] is not None

    # Idempotent.
    assert (
        db_module.adopt_gallery_items("tag-b", [entry], db_path=db_path, output_dir=out) == 0
    )

    # A real generation for the same variant supersedes the placeholder.
    db_module.upsert_gallery_item(
        "tag-b", _result(out_path=img, original_path=img, dpi=800), db_path=db_path
    )
    [item] = list_gallery_items("tag-b", db_path=db_path)
    assert item["scryfall_id"] == "sol-id"


def test_adopt_gallery_items_skips_rows_whose_file_is_gone(db_path: Path, tmp_path: Path) -> None:
    from proxy_scaler.decklist import DeckEntry

    missing = tmp_path / "deleted.png"  # never written
    db_module.upsert_gallery_item(
        "tag-a", _result(out_path=missing, original_path=missing), db_path=db_path
    )

    entry = DeckEntry(quantity=1, name="Sol Ring", set_code="c21", collector_number="263")
    assert db_module.adopt_gallery_items("tag-b", [entry], db_path=db_path) == 0
    assert list_gallery_items("tag-b", db_path=db_path) == []


# --- Task queue -------------------------------------------------------


def _enqueue_sol_ring(db_path: Path, **overrides) -> int:
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
        output_dir="/tmp/out",
        cache_dir="/tmp/cache",
        weights_dir="/tmp/weights",
        db_path=db_path,
    )
    kwargs.update(overrides)
    return enqueue_task(kwargs.pop("project_tag"), **kwargs)


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


def test_enqueue_task_round_trips_total_faces(db_path: Path) -> None:
    """total_faces (captured once at enqueue time from Scryfall's card
    data — see db migration 003) survives the INSERT and comes back intact
    via both list_tasks() and claim_next_task(), so process_task() has it
    without ever calling Scryfall itself."""
    tid = _enqueue_sol_ring(db_path, total_faces=2)
    [pending] = list_tasks(db_path=db_path)
    assert pending.id == tid
    assert pending.total_faces == 2

    claimed = claim_next_task(db_path=db_path)
    assert claimed is not None
    assert claimed.total_faces == 2

    # Unset stays None rather than defaulting to "single-faced".
    _enqueue_sol_ring(db_path, collector_number="9")
    assert {t.total_faces for t in list_tasks(db_path=db_path) if t.collector_number == "9"} == {
        None
    }


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


def test_cancel_pending_tasks_for_tag_only_touches_that_tags_pending(db_path: Path) -> None:
    """Discarding one Project's session must leave another Project's queue
    alone, and can't reach work that already started — a running upscale
    finishes and writes its row anyway (the accepted residue in spec §8)."""
    running = _enqueue_sol_ring(db_path, project_tag="tag-a", collector_number="1")
    claim_next_task(db_path=db_path)  # the only pending task so far -> running
    mine = _enqueue_sol_ring(db_path, project_tag="tag-a", collector_number="2")
    theirs = _enqueue_sol_ring(db_path, project_tag="tag-b", collector_number="3")

    assert cancel_pending_tasks_for_tag("tag-a", db_path=db_path) == 1

    assert get_task(mine, db_path=db_path).status == "canceled"
    assert get_task(running, db_path=db_path).status == "running"
    assert get_task(theirs, db_path=db_path).status == "pending"


def test_cancel_pending_tasks_for_tag_is_a_no_op_for_a_falsy_tag(db_path: Path) -> None:
    """The same guard every other project_tag-scoped write in the module
    carries: an empty tag must never widen into "cancel everything"."""
    tid = _enqueue_sol_ring(db_path, project_tag="tag-a")
    untagged = _enqueue_sol_ring(db_path, collector_number="2")

    assert cancel_pending_tasks_for_tag("", db_path=db_path) == 0

    assert get_task(tid, db_path=db_path).status == "pending"
    assert get_task(untagged, db_path=db_path).status == "pending"


def test_list_tasks_filters_by_project_tag_and_status(db_path: Path) -> None:
    _enqueue_sol_ring(db_path, project_tag="tag-a", collector_number="1")
    t2 = _enqueue_sol_ring(db_path, project_tag="tag-a", collector_number="2")
    _enqueue_sol_ring(db_path, project_tag="tag-b", collector_number="3")
    mark_task_done(t2, db_path=db_path)

    a_tasks = list_tasks(project_tag="tag-a", db_path=db_path)
    assert len(a_tasks) == 2
    assert all(t.project_tag == "tag-a" for t in a_tasks)

    a_pending = list_tasks(project_tag="tag-a", statuses=["pending"], db_path=db_path)
    assert len(a_pending) == 1

    all_tasks = list_tasks(db_path=db_path)
    assert len(all_tasks) == 3


def test_upsert_gallery_item_for_task_writes_and_updates(db_path: Path) -> None:
    tag = "tag-p"
    tid = _enqueue_sol_ring(db_path, project_tag=tag)
    task = claim_next_task(db_path=db_path)
    assert task.id == tid

    result = _result(device="gpu")
    upsert_gallery_item_for_task(task, result, db_path=db_path)
    items = list_gallery_items(tag, db_path=db_path)
    assert len(items) == 1
    assert items[0]["device"] == "gpu"

    # Re-upserting the same (project_tag, scryfall_id, face_index, model,
    # dpi) updates in place rather than duplicating — e.g. a later regen
    # of the same variant.
    updated_result = FaceResult(**{**result.__dict__, "device": "cpu"})
    upsert_gallery_item_for_task(task, updated_result, db_path=db_path)
    items2 = list_gallery_items(tag, db_path=db_path)
    assert len(items2) == 1
    assert items2[0]["device"] == "cpu"


def test_upsert_gallery_item_for_task_noop_without_project_tag(db_path: Path) -> None:
    tid = _enqueue_sol_ring(db_path, project_tag=None)
    task = get_task(tid, db_path=db_path)
    result = _result(out_path=Path("/o/x.png"), original_path=Path("/c/x.png"), png_url="")
    # Should not raise even though there's no project_tag to attach to.
    upsert_gallery_item_for_task(task, result, db_path=db_path)
    assert list_gallery_items("", db_path=db_path) == []


def test_get_gallery_item_by_id_not_scoped_by_project_tag(db_path: Path) -> None:
    tag = "tag-p"
    tid = _enqueue_sol_ring(db_path, project_tag=tag)
    task = claim_next_task(db_path=db_path)
    assert task.id == tid
    upsert_gallery_item_for_task(task, _result(), db_path=db_path)

    [item] = list_gallery_items(tag, db_path=db_path)
    fetched = get_gallery_item(item["id"], db_path=db_path)
    assert fetched is not None
    assert fetched["scryfall_id"] == "sol-id"
    assert fetched["project_tag"] == tag

    assert get_gallery_item(999999, db_path=db_path) is None


def test_worker_lock_prevents_double_acquire(tmp_path: Path) -> None:
    lock_path = tmp_path / "worker.lock"
    assert is_worker_running(lock_path) is False

    fd = acquire_worker_lock(lock_path)
    assert fd is not None
    assert is_worker_running(lock_path) is True
    assert acquire_worker_lock(lock_path) is None  # already held

    release_worker_lock(fd)
    assert is_worker_running(lock_path) is False


# --- Schema versioning / migrations ----------------------------------


def test_init_db_fresh_db_stamps_latest_version(tmp_path: Path) -> None:
    path = tmp_path / "fresh.db"
    init_db(path)

    conn = sqlite3.connect(str(path))
    try:
        assert conn.execute("PRAGMA user_version").fetchone()[0] == db_module.SCHEMA_VERSION
    finally:
        conn.close()


def test_init_db_bridges_already_current_unversioned_db(tmp_path: Path) -> None:
    """A database already reshaped by the pre-versioning _migrate() (real
    users, including this project's own dev DB) has project_tag columns
    and no `projects` table, but PRAGMA user_version was never set
    (implicit 0). init_db() must recognize it's already at the current
    shape and just stamp the version -- not rerun migration 1's drops
    against data that's already there."""
    path = tmp_path / "bridge.db"
    conn = sqlite3.connect(str(path))
    conn.executescript(db_module._SCHEMA)
    conn.execute(
        """
        INSERT INTO generation_tasks (
            project_tag, scryfall_id, face_name, card_name, set_code,
            collector_number, png_url, dpi, model, output_dir, cache_dir,
            weights_dir, created_at
        ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
        """,
        (
            "tag-a", "sol-id", "Sol Ring", "Sol Ring", "c21", "263",
            "https://example.com/sol.png", 800, "ultrasharp_v2", "/tmp/out",
            "/tmp/cache", "/tmp/weights", "2024-01-01T00:00:00+00:00",
        ),
    )
    conn.execute(
        """
        INSERT INTO project_gallery_items (
            project_tag, scryfall_id, model, dpi, image_filename,
            out_path, original_path, png_url
        ) VALUES (?, ?, ?, ?, ?, ?, ?, ?)
        """,
        (
            "tag-a", "sol-id", "ultrasharp_v2", 800, "sol.png",
            "/tmp/out/sol.png", "/tmp/cache/sol.png",
            "https://example.com/sol.png",
        ),
    )
    conn.commit()
    conn.close()

    init_db(path)

    conn = sqlite3.connect(str(path))
    try:
        assert conn.execute("PRAGMA user_version").fetchone()[0] == db_module.SCHEMA_VERSION
        assert conn.execute("SELECT COUNT(*) FROM generation_tasks").fetchone()[0] == 1
        assert conn.execute("SELECT COUNT(*) FROM project_gallery_items").fetchone()[0] == 1
    finally:
        conn.close()


def test_init_db_migrates_legacy_pre_reshape_db(tmp_path: Path) -> None:
    """A genuinely old-shape database (pre client/generation split) gets
    its legacy tables dropped and generation_tasks rebuilt with the
    current project_tag shape, same as the old ad hoc _migrate() did."""
    path = tmp_path / "legacy.db"
    conn = sqlite3.connect(str(path))
    conn.executescript(
        """
        CREATE TABLE projects (id INTEGER PRIMARY KEY, name TEXT);
        CREATE TABLE project_cards (id INTEGER PRIMARY KEY, project_id INTEGER);
        CREATE TABLE app_settings (key TEXT PRIMARY KEY, value TEXT);
        CREATE TABLE generation_tasks (
            id INTEGER PRIMARY KEY,
            project_id INTEGER,
            status TEXT
        );
        """
    )
    conn.execute("INSERT INTO projects (id, name) VALUES (1, 'Old Project')")
    conn.execute("INSERT INTO project_cards (id, project_id) VALUES (1, 1)")
    conn.execute("INSERT INTO app_settings (key, value) VALUES ('k', 'v')")
    conn.execute(
        "INSERT INTO generation_tasks (id, project_id, status) VALUES (1, 1, 'done')"
    )
    conn.commit()
    conn.close()

    init_db(path)

    conn = sqlite3.connect(str(path))
    try:
        tables = {
            row[0]
            for row in conn.execute(
                "SELECT name FROM sqlite_master WHERE type='table'"
            ).fetchall()
        }
        assert "projects" not in tables
        assert "project_cards" not in tables
        assert "app_settings" not in tables
        assert "generation_tasks" in tables

        cols = {
            row[1] for row in conn.execute("PRAGMA table_info(generation_tasks)").fetchall()
        }
        assert "project_tag" in cols

        assert conn.execute("SELECT COUNT(*) FROM generation_tasks").fetchone()[0] == 0
        assert conn.execute("PRAGMA user_version").fetchone()[0] == db_module.SCHEMA_VERSION
    finally:
        conn.close()


def test_migration_003_adds_total_faces_to_existing_rows_as_null(tmp_path: Path) -> None:
    """A database at the version-2 shape (created_at exists, total_faces
    doesn't) gets total_faces added to both tables on upgrade, nullable
    with no backfill — existing rows predate the column and have no honest
    value to give it (same reasoning as migration 002's created_at)."""
    path = tmp_path / "v2.db"
    conn = sqlite3.connect(str(path))
    conn.executescript(
        """
        CREATE TABLE generation_tasks (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            project_tag TEXT,
            status TEXT NOT NULL DEFAULT 'pending',
            scryfall_id TEXT NOT NULL,
            face_index INTEGER,
            face_label TEXT,
            face_name TEXT NOT NULL,
            card_name TEXT NOT NULL,
            set_code TEXT NOT NULL,
            collector_number TEXT NOT NULL,
            png_url TEXT NOT NULL,
            dpi INTEGER NOT NULL,
            model TEXT NOT NULL,
            tile_size INTEGER NOT NULL DEFAULT 0,
            output_dir TEXT NOT NULL,
            cache_dir TEXT NOT NULL,
            weights_dir TEXT NOT NULL,
            error TEXT,
            created_at TEXT NOT NULL,
            started_at TEXT,
            completed_at TEXT
        );
        CREATE TABLE project_gallery_items (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            project_tag TEXT NOT NULL,
            scryfall_id TEXT NOT NULL,
            face_index INTEGER,
            face_name TEXT,
            card_name TEXT,
            set_code TEXT,
            collector_number TEXT,
            face_label TEXT,
            model TEXT NOT NULL,
            dpi INTEGER NOT NULL,
            native_scale INTEGER NOT NULL DEFAULT 4,
            device TEXT NOT NULL DEFAULT 'unknown',
            image_filename TEXT NOT NULL,
            out_path TEXT NOT NULL,
            original_path TEXT NOT NULL,
            png_url TEXT NOT NULL,
            created_at TEXT,
            UNIQUE (project_tag, scryfall_id, face_index, model, dpi)
        );
        """
    )
    conn.execute(
        """
        INSERT INTO generation_tasks (
            project_tag, scryfall_id, face_name, card_name, set_code,
            collector_number, png_url, dpi, model, output_dir, cache_dir,
            weights_dir, created_at
        ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
        """,
        (
            "tag-a", "sol-id", "Sol Ring", "Sol Ring", "c21", "263",
            "https://example.com/sol.png", 800, "ultrasharp_v2", "/tmp/out",
            "/tmp/cache", "/tmp/weights", "2024-01-01T00:00:00+00:00",
        ),
    )
    conn.execute(
        """
        INSERT INTO project_gallery_items (
            project_tag, scryfall_id, model, dpi, image_filename,
            out_path, original_path, png_url
        ) VALUES (?, ?, ?, ?, ?, ?, ?, ?)
        """,
        (
            "tag-a", "sol-id", "ultrasharp_v2", 800, "sol.png",
            "/tmp/out/sol.png", "/tmp/cache/sol.png",
            "https://example.com/sol.png",
        ),
    )
    conn.execute("PRAGMA user_version = 2")
    conn.commit()
    conn.close()

    init_db(path)

    conn = sqlite3.connect(str(path))
    conn.row_factory = sqlite3.Row
    try:
        assert conn.execute("PRAGMA user_version").fetchone()[0] == db_module.SCHEMA_VERSION
        task_cols = {row[1] for row in conn.execute("PRAGMA table_info(generation_tasks)")}
        gallery_cols = {
            row[1] for row in conn.execute("PRAGMA table_info(project_gallery_items)")
        }
        assert "total_faces" in task_cols
        assert "total_faces" in gallery_cols

        task_row = conn.execute("SELECT * FROM generation_tasks").fetchone()
        gallery_row = conn.execute("SELECT * FROM project_gallery_items").fetchone()
        assert task_row["total_faces"] is None
        assert gallery_row["total_faces"] is None
    finally:
        conn.close()


def test_init_db_is_idempotent(db_path: Path) -> None:
    tid = _enqueue_sol_ring(db_path)

    init_db(db_path)
    init_db(db_path)

    task = get_task(tid, db_path=db_path)
    assert task is not None
    assert task.id == tid


def test_connect_raises_on_stale_schema(tmp_path: Path) -> None:
    path = tmp_path / "stale.db"
    init_db(path)

    conn = sqlite3.connect(str(path))
    conn.execute("PRAGMA user_version = 0")
    conn.commit()
    conn.close()

    with pytest.raises(db_module.SchemaVersionMismatch):
        db_module.connect(path)


def test_migration_runner_applies_steps_in_order_and_is_idempotent(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    calls: list[int] = []
    fake_migrations = [
        db_module.Migration(1, "fake v1", lambda conn: calls.append(1)),
        db_module.Migration(2, "fake v2", lambda conn: calls.append(2)),
    ]
    monkeypatch.setattr(db_module, "_MIGRATIONS", fake_migrations)
    monkeypatch.setattr(db_module, "SCHEMA_VERSION", 2)

    path = tmp_path / "runner.db"
    conn = db_module._raw_connect(path)
    conn.executescript(db_module._SCHEMA)  # seed a known table so it's
    conn.commit()                          # treated as existing, not fresh
    db_module._migrate(conn)
    conn.close()
    assert calls == [1, 2]

    calls.clear()
    conn = db_module._raw_connect(path)
    db_module._migrate(conn)
    conn.close()
    assert calls == []
