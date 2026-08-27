"""Unit tests for proxy_scaler/services/generation.py — the enqueue
orchestration extracted from proxy_scaler/ui/decklist.py during the
Streamlit -> FastAPI migration. Uses tmp_path-isolated SQLite (same
db_path-override pattern worker.py/supervisor.py already use) and mocks
ScryfallClient.resolve_many the same way test_pipeline.py does, rather
than making real network calls."""

from __future__ import annotations

from pathlib import Path

import pytest

from proxy_scaler import carddb, db
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


@pytest.fixture(autouse=True)
def _no_default_card_corpus(tmp_path: Path, monkeypatch) -> None:
    """Point the default card-corpus location at a nonexistent tmp file.

    CardResolver answers from the local corpus first and only falls
    through to the (mocked) ScryfallClient on a miss — so a dev machine
    with a real bulk-imported data/scryfall_cards.db would resolve
    "Sol Ring" locally to a real printing, bypassing every
    resolve_many mock and the SOL_RING_CARD fixture ids these tests
    key on. Tests that want a corpus seed their own and pass
    card_db_path explicitly, which wins over this default."""
    monkeypatch.setattr(carddb, "DEFAULT_CARD_DB_PATH", tmp_path / "no-corpus.db")


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
        model="ultrasharp_v2",
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


def test_enqueue_face_passes_total_faces_through_to_task(
    db_path: Path, tmp_path: Path
) -> None:
    """total_faces isn't recomputed by enqueue_task — it's whatever the
    caller already knows from expand_faces() at resolve time (see
    enqueue_decklist_entries below), just carried onto the row."""
    generation.enqueue_face(
        scryfall_id="dfc-id",
        face_index=0,
        face_label="front",
        face_name="Delver of Secrets",
        card_name="Delver of Secrets // Insectile Aberration",
        set_code="isd",
        collector_number="51",
        png_url="https://example.com/delver.png",
        dpi_targets=[800],
        model="ultrasharp_v2",
        tile_size=0,
        output_dir=tmp_path / "out",
        cache_dir=tmp_path / "cache",
        weights_dir=tmp_path / "weights",
        project_tag=None,
        total_faces=2,
        db_path=db_path,
    )
    [task] = db.list_tasks(db_path=db_path)
    assert task.total_faces == 2


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
        model="ultrasharp_v2",
        tile_size=0,
        output_dir=tmp_path / "out",
        cache_dir=tmp_path / "cache",
        weights_dir=tmp_path / "weights",
        project_tag=tag,
        db_path=db_path,
    )
    active = generation.active_task_keys(tag, db_path=db_path)
    assert active == {("sol-id", None, 800, "ultrasharp_v2")}


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
        model="ultrasharp_v2",
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
    # SOL_RING_CARD has no card_faces, so expand_faces() reports exactly
    # one printable face — captured onto the task at enqueue time.
    [task] = db.list_tasks(db_path=db_path)
    assert task.total_faces == 1


def test_enqueue_decklist_entries_skips_existing_output_file(
    db_path: Path, tmp_path: Path, monkeypatch
) -> None:
    from proxy_scaler.pipeline import output_filename

    monkeypatch.setattr(
        ScryfallClient, "resolve_many", lambda self, entries: [(SOL_RING_CARD, [])]
    )
    output_dir = tmp_path / "out"
    output_dir.mkdir()
    existing_name = output_filename("Sol Ring", "c21", "263", None, "ultrasharp_v2", 800)
    (output_dir / existing_name).write_bytes(b"fake png")

    notes = []
    queued, failed, task_ids = generation.enqueue_decklist_entries(
        [_entry()],
        model="ultrasharp_v2",
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


def test_enqueue_decklist_entries_registry_hit_skips_without_filename_probe(
    db_path: Path, tmp_path: Path, monkeypatch
) -> None:
    """Registry-first skip: a variant already in generated_images (with a
    live file at its recorded out_path — anywhere, not necessarily under
    this request's output_dir or either filename shape) is skipped, and
    this project just gains a membership to the existing row."""
    from proxy_scaler.pipeline import FaceResult

    monkeypatch.setattr(
        ScryfallClient, "resolve_many", lambda self, entries: [(SOL_RING_CARD, [])]
    )
    elsewhere = tmp_path / "elsewhere"
    elsewhere.mkdir()
    img = elsewhere / "arbitrary-name.png"
    img.write_bytes(b"fake png")
    db.upsert_gallery_item(
        "tag-original",
        FaceResult(
            out_path=img,
            original_path=img,
            scryfall_id="sol-id",
            face_index=None,
            face_name="Sol Ring",
            card_name="Sol Ring",
            set_code="c21",
            collector_number="263",
            png_url="https://example.com/sol.png",
            dpi=800,
            model="ultrasharp_v2",
        ),
        db_path=db_path,
    )

    output_dir = tmp_path / "out"
    output_dir.mkdir()  # empty — no file of either filename shape here
    tag = "proj-tag-registry"
    queued, failed, task_ids = generation.enqueue_decklist_entries(
        [_entry()],
        model="ultrasharp_v2",
        dpi_targets=[800],
        skip_existing=True,
        tile_size=0,
        output_dir=output_dir,
        cache_dir=tmp_path / "cache",
        weights_dir=tmp_path / "weights",
        project_tag=tag,
        db_path=db_path,
    )
    assert queued == 0
    assert task_ids == []
    [item] = db.list_gallery_items(tag, db_path=db_path)
    assert item["scryfall_id"] == "sol-id"
    assert item["out_path"] == str(img)

    # A registry row whose file is gone does NOT satisfy the skip — the
    # face queues for real generation instead.
    img.unlink()
    queued, failed, task_ids = generation.enqueue_decklist_entries(
        [_entry()],
        model="ultrasharp_v2",
        dpi_targets=[800],
        skip_existing=True,
        tile_size=0,
        output_dir=output_dir,
        cache_dir=tmp_path / "cache",
        weights_dir=tmp_path / "weights",
        project_tag="proj-tag-after-delete",
        db_path=db_path,
    )
    assert queued == 1


def test_enqueue_decklist_entries_backfills_gallery_for_preexisting_file(
    db_path: Path, tmp_path: Path, monkeypatch
) -> None:
    """A file that already exists on disk under a fresh project_tag (e.g.
    generated in an earlier session, or copied in) has no task/gallery row
    for *this* project_tag yet — skip_existing must register it into the
    gallery anyway, or the UI shows "not generated yet" for an image that
    genuinely already exists. See db.py::upsert_gallery_item."""
    from proxy_scaler.pipeline import output_filename
    from proxy_scaler.upscale import original_cache_path

    monkeypatch.setattr(
        ScryfallClient, "resolve_many", lambda self, entries: [(SOL_RING_CARD, [])]
    )
    output_dir = tmp_path / "out"
    output_dir.mkdir()
    existing_name = output_filename("Sol Ring", "c21", "263", None, "ultrasharp_v2", 800)
    (output_dir / existing_name).write_bytes(b"fake png")
    cache_dir = tmp_path / "cache"

    tag = "proj-tag-backfill"
    queued, failed, task_ids = generation.enqueue_decklist_entries(
        [_entry()],
        model="ultrasharp_v2",
        dpi_targets=[800],
        skip_existing=True,
        tile_size=0,
        output_dir=output_dir,
        cache_dir=cache_dir,
        weights_dir=tmp_path / "weights",
        project_tag=tag,
        db_path=db_path,
    )
    assert queued == 0
    assert task_ids == []

    items = db.list_gallery_items(tag, db_path=db_path)
    assert len(items) == 1
    assert items[0]["scryfall_id"] == "sol-id"
    assert items[0]["dpi"] == 800
    assert items[0]["model"] == "ultrasharp_v2"
    assert items[0]["out_path"] == str(output_dir / existing_name)
    # The skip_existing branch registers the gallery row directly (no
    # task involved), still carrying total_faces from expand_faces().
    assert items[0]["total_faces"] == 1
    # No cached original actually on disk for this scryfall_id — the
    # deterministic path is still stored (see the cache-hit variant below
    # for why), so "Compare" 404s on request rather than the gallery row
    # having no path recorded for it at all.
    assert items[0]["original_path"] == str(original_cache_path(cache_dir, "sol-id", None))
    assert not Path(items[0]["original_path"]).exists()


def test_enqueue_decklist_entries_backfill_finds_cached_original(
    db_path: Path, tmp_path: Path, monkeypatch
) -> None:
    """Same as the no-cache-hit case above, but the pre-upscale original is
    still sitting in cache_dir (its path is deterministic, keyed only by
    scryfall_id/face_index — see upscale.py::original_cache_path) — the
    backfilled gallery row must point "Compare" at it instead of leaving
    original_path empty when it doesn't have to."""
    from proxy_scaler.pipeline import output_filename
    from proxy_scaler.upscale import original_cache_path

    monkeypatch.setattr(
        ScryfallClient, "resolve_many", lambda self, entries: [(SOL_RING_CARD, [])]
    )
    output_dir = tmp_path / "out"
    output_dir.mkdir()
    existing_name = output_filename("Sol Ring", "c21", "263", None, "ultrasharp_v2", 800)
    (output_dir / existing_name).write_bytes(b"fake png")

    cache_dir = tmp_path / "cache"
    cached_original = original_cache_path(cache_dir, "sol-id", None)
    cached_original.parent.mkdir(parents=True)
    cached_original.write_bytes(b"fake original")

    tag = "proj-tag-backfill-cached"
    generation.enqueue_decklist_entries(
        [_entry()],
        model="ultrasharp_v2",
        dpi_targets=[800],
        skip_existing=True,
        tile_size=0,
        output_dir=output_dir,
        cache_dir=cache_dir,
        weights_dir=tmp_path / "weights",
        project_tag=tag,
        db_path=db_path,
    )

    [item] = db.list_gallery_items(tag, db_path=db_path)
    assert item["original_path"] == str(cached_original)


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
        model="ultrasharp_v2",
        tile_size=0,
        output_dir=tmp_path / "out",
        cache_dir=tmp_path / "cache",
        weights_dir=tmp_path / "weights",
        project_tag=None,
        db_path=db_path,
    )

    queued, failed, task_ids = generation.enqueue_decklist_entries(
        [_entry()],
        model="ultrasharp_v2",
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
        model="ultrasharp_v2",
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
        model="ultrasharp_v2",
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
        model="ultrasharp_v2",
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


def test_enqueue_decklist_entries_resolves_from_local_corpus_without_network(
    db_path: Path, tmp_path: Path, monkeypatch
) -> None:
    """With a seeded card corpus, enqueueing never touches Scryfall at all —
    the whole resolve happens against carddb (see card_lookup.CardResolver)."""
    from proxy_scaler import carddb

    card_db_path = tmp_path / "cards.db"
    carddb.init_card_db(card_db_path)
    conn = carddb.connect(card_db_path)
    try:
        carddb.upsert_cards(
            conn,
            [dict(SOL_RING_CARD, lang="en", oracle_id="oracle-sol")],
        )
    finally:
        conn.close()

    def no_network(self, entries):
        raise AssertionError("live ScryfallClient used despite a local corpus hit")

    monkeypatch.setattr(ScryfallClient, "resolve_many", no_network)

    queued, failed, task_ids = generation.enqueue_decklist_entries(
        [_entry(set_code="c21", collector_number="263")],
        model="ultrasharp_v2",
        dpi_targets=[600],
        skip_existing=False,
        tile_size=0,
        output_dir=tmp_path / "out",
        cache_dir=tmp_path / "cache",
        weights_dir=tmp_path / "weights",
        project_tag="tag-local",
        db_path=db_path,
        card_db_path=card_db_path,
    )
    assert (queued, failed) == (1, 0)
    [task] = db.list_tasks(project_tag="tag-local", db_path=db_path)
    assert task.scryfall_id == "sol-id"
    assert task.png_url == "https://example.com/sol.png"


# --- enqueue_download_entries -----------------------------------------------


def test_enqueue_download_entries_queues_one_sentinel_task_per_face(
    db_path: Path, tmp_path: Path, monkeypatch
) -> None:
    from proxy_scaler.dpi import ORIGINAL_DPI, ORIGINAL_MODEL

    monkeypatch.setattr(
        ScryfallClient, "resolve_many", lambda self, entries: [(SOL_RING_CARD, [])]
    )

    queued, failed, task_ids = generation.enqueue_download_entries(
        [_entry()],
        output_dir=tmp_path / "out",
        cache_dir=tmp_path / "cache",
        weights_dir=tmp_path / "weights",
        project_tag="tag-dl",
        db_path=db_path,
    )
    assert (queued, failed) == (1, 0)
    [task] = db.list_tasks(db_path=db_path)
    assert task.model == ORIGINAL_MODEL
    assert task.dpi == ORIGINAL_DPI
    assert task.status == "pending"


def test_enqueue_download_entries_skips_active_download(
    db_path: Path, tmp_path: Path, monkeypatch
) -> None:
    """A pending/running download for the same face must not be duplicated
    by a second batch — same in-flight rule as upscale enqueueing."""
    from proxy_scaler.dpi import ORIGINAL_DPI, ORIGINAL_MODEL

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
        dpi_targets=[ORIGINAL_DPI],
        model=ORIGINAL_MODEL,
        tile_size=0,
        output_dir=tmp_path / "out",
        cache_dir=tmp_path / "cache",
        weights_dir=tmp_path / "weights",
        project_tag="tag-dl",
        db_path=db_path,
    )

    notes: list[str] = []
    queued, failed, task_ids = generation.enqueue_download_entries(
        [_entry()],
        output_dir=tmp_path / "out",
        cache_dir=tmp_path / "cache",
        weights_dir=tmp_path / "weights",
        project_tag="tag-dl",
        db_path=db_path,
        on_note=notes.append,
    )
    assert queued == 0
    assert task_ids == []
    assert len(db.list_tasks(db_path=db_path)) == 1
    assert any("already queued" in n for n in notes)


def test_enqueue_download_entries_registry_hit_adds_membership_only(
    db_path: Path, tmp_path: Path, monkeypatch
) -> None:
    from proxy_scaler.dpi import ORIGINAL_DPI, ORIGINAL_MODEL
    from proxy_scaler.pipeline import FaceResult
    from proxy_scaler.upscale import original_cache_path

    monkeypatch.setattr(
        ScryfallClient, "resolve_many", lambda self, entries: [(SOL_RING_CARD, [])]
    )
    cache_dir = tmp_path / "cache"
    original = original_cache_path(cache_dir, "sol-id", None)
    original.parent.mkdir(parents=True, exist_ok=True)
    original.write_bytes(b"fake png")
    db.upsert_gallery_item(
        "tag-other",
        FaceResult(
            out_path=original,
            original_path=original,
            scryfall_id="sol-id",
            face_index=None,
            face_name="Sol Ring",
            card_name="Sol Ring",
            set_code="c21",
            collector_number="263",
            png_url="https://example.com/sol.png",
            dpi=ORIGINAL_DPI,
            model=ORIGINAL_MODEL,
        ),
        db_path=db_path,
    )

    queued, failed, task_ids = generation.enqueue_download_entries(
        [_entry()],
        output_dir=tmp_path / "out",
        cache_dir=cache_dir,
        weights_dir=tmp_path / "weights",
        project_tag="tag-dl",
        db_path=db_path,
    )
    assert queued == 0
    assert db.list_tasks(db_path=db_path) == []
    # The requesting project joined the existing registry row.
    [item] = db.list_gallery_items("tag-dl", db_path=db_path)
    assert item["model"] == ORIGINAL_MODEL


def test_enqueue_download_entries_backfills_registry_for_on_disk_original(
    db_path: Path, tmp_path: Path, monkeypatch
) -> None:
    """An original already cached by an earlier upscale run (no download-
    variant row anywhere) is registered rather than re-downloaded — same
    rationale as enqueue_decklist_entries' skip-existing backfill."""
    from proxy_scaler.dpi import ORIGINAL_DPI, ORIGINAL_MODEL
    from proxy_scaler.upscale import original_cache_path

    monkeypatch.setattr(
        ScryfallClient, "resolve_many", lambda self, entries: [(SOL_RING_CARD, [])]
    )
    cache_dir = tmp_path / "cache"
    original = original_cache_path(cache_dir, "sol-id", None)
    original.parent.mkdir(parents=True, exist_ok=True)
    original.write_bytes(b"fake png")

    queued, failed, task_ids = generation.enqueue_download_entries(
        [_entry()],
        output_dir=tmp_path / "out",
        cache_dir=cache_dir,
        weights_dir=tmp_path / "weights",
        project_tag="tag-dl",
        db_path=db_path,
    )
    assert queued == 0
    assert db.list_tasks(db_path=db_path) == []
    [item] = db.list_gallery_items("tag-dl", db_path=db_path)
    assert item["model"] == ORIGINAL_MODEL
    assert item["dpi"] == ORIGINAL_DPI
    assert item["out_path"] == str(original)


def test_enqueue_face_force_lands_on_rows(db_path: Path, tmp_path: Path) -> None:
    generation.enqueue_face(
        scryfall_id="sol-id", face_index=None, face_label=None,
        face_name="Sol Ring", card_name="Sol Ring", set_code="c21",
        collector_number="263", png_url="https://example.com/sol.png",
        dpi_targets=[600, 800], model="ultrasharp_v2", tile_size=0,
        output_dir=tmp_path / "out", cache_dir=tmp_path / "cache",
        weights_dir=tmp_path / "weights", project_tag=None,
        force=True, db_path=db_path,
    )
    assert all(t.force for t in db.list_tasks(db_path=db_path))


def test_enqueue_decklist_entries_tasks_are_not_forced(
    db_path: Path, tmp_path: Path, monkeypatch
) -> None:
    """First generation must reuse the x4 cache (sibling DPI sharing);
    only the Regenerate button enqueues force=True."""
    monkeypatch.setattr(
        ScryfallClient, "resolve_many", lambda self, entries: [(SOL_RING_CARD, [])]
    )
    generation.enqueue_decklist_entries(
        [_entry()], model="ultrasharp_v2", dpi_targets=[600, 800, 1200],
        skip_existing=False, tile_size=0,
        output_dir=tmp_path / "out", cache_dir=tmp_path / "cache",
        weights_dir=tmp_path / "weights", project_tag=None, db_path=db_path,
    )
    tasks = db.list_tasks(db_path=db_path)
    assert len(tasks) == 3
    assert not any(t.force for t in tasks)
