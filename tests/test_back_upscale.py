"""Upscaling Back Images from the Backs tab (db migration 010 and the
back_hash identity, services.generation.enqueue_back_upscale,
pipeline.process_back_source_task, backs.resolve_print_source).

A back is never a card: nothing here goes through a decklist. The PDF and
ZIP paths find the back by hash and rank its versions the way a Custom
Image's are ranked, degrading to the best available rather than blanking
the Reverse.
"""

from __future__ import annotations

import hashlib
import io
import sqlite3
import zipfile
from pathlib import Path

import pytest
from fastapi.testclient import TestClient
from PIL import Image

from proxy_scaler import backs, db as db_module
from proxy_scaler.api.schemas import ExportZipIn
from proxy_scaler.db import init_db, parse_output_filename
from proxy_scaler.decklist import DeckEntry
from proxy_scaler.dpi import BACK_SOURCE_MODEL, LIBRARY_TAG, dpi_at_card_size, target_pixels
from proxy_scaler.pipeline import FaceResult, face_group_key, output_filename
from proxy_scaler.upscale import UpscaleModel, UpscaleResult, cache_path, original_cache_path

HASH_A = "a" * 64


@pytest.fixture
def db_path(tmp_path: Path) -> Path:
    return tmp_path / "test.db"


@pytest.fixture
def client(tmp_path: Path, db_path: Path, monkeypatch) -> TestClient:
    init_db(db_path)
    monkeypatch.setenv("PROXY_SCALER_DB_PATH", str(db_path))
    monkeypatch.setenv("PROXY_SCALER_WORKER_LOCK_PATH", str(tmp_path / "worker.lock"))
    monkeypatch.chdir(tmp_path)
    from proxy_scaler.api.app import app

    return TestClient(app)


def _png(size: tuple[int, int], color=(200, 100, 50)) -> tuple[bytes, str]:
    buf = io.BytesIO()
    Image.new("RGB", size, color).save(buf, format="PNG")
    data = buf.getvalue()
    return data, hashlib.sha256(data).hexdigest()


def _upload(client: TestClient, size=(600, 840)) -> str:
    data, content_hash = _png(size)
    assert client.post(f"/api/backs/{content_hash}", content=data).status_code == 200
    return content_hash


def _body(tmp_path: Path, content_hash: str, **over) -> dict:
    body = {
        "kind": "back",
        "content_hash": content_hash,
        "label": "Dragon back",
        "model": "ultrasharp_v2",
        "dpi_targets": [1200],
        "tile_size": 0,
        "mode": "target",
        "output_dir": str(tmp_path / "out"),
        "cache_dir": str(tmp_path / "cache"),
        "weights_dir": str(tmp_path / "weights"),
    }
    body.update(over)
    return body


def _tasks(db_path: Path, ids: list[int]) -> list[tuple[str | None, str, int]]:
    return sorted(
        (t.project_tag, t.model, t.dpi) for t in (db_module.get_task(i, db_path=db_path) for i in ids)
    )


# --------------------------------------------------------------------
# Identity plumbing
# --------------------------------------------------------------------


def test_identity_filename_and_cache_stem_round_trip() -> None:
    assert backs.identity_key(HASH_A) == f"back:{HASH_A}"
    name = output_filename("Dragon back", "", "", None, "ultrasharp_v2", 1200, back_hash=HASH_A)
    assert name == f"Dragon_back-back-{HASH_A}-ultrasharp_v2-1200dpi.png"
    meta = parse_output_filename(name)
    assert meta["back_hash"] == HASH_A and meta["custom_hash"] is None
    assert (meta["model"], meta["dpi"], meta["scryfall_id"]) == ("ultrasharp_v2", 1200, "")
    assert parse_output_filename(f"Dragon_back-back-{HASH_A}.png")["model"] == BACK_SOURCE_MODEL
    cache = Path("c")
    assert original_cache_path(cache, None, None, back_hash=HASH_A).name == f"back_{HASH_A}_single.png"
    assert cache_path(cache, None, None, 4, "ultrasharp_v2", back_hash=HASH_A).name.startswith(f"back_{HASH_A}")
    item = FaceResult(
        out_path=Path("/x.png"), original_path=Path("/o.png"), scryfall_id="", back_hash=HASH_A,
        face_index=None, face_name="b", card_name="b", set_code="", collector_number="", png_url="", dpi=1200,
    )
    assert item.is_back and item.is_upload and not item.is_custom
    assert item.identity_key == f"back:{HASH_A}" and face_group_key(item).startswith("back:")


def test_registry_and_tasks_accept_exactly_one_identity(db_path: Path) -> None:
    init_db(db_path)
    common = dict(
        face_index=None, face_label=None, face_name="b", card_name="b", dpi=1200, model="ultrasharp_v2",
        output_dir="o", cache_dir="c", weights_dir="w", db_path=db_path,
    )
    task_id = db_module.enqueue_task(LIBRARY_TAG, back_hash=HASH_A, **common)
    task = db_module.get_task(task_id, db_path=db_path)
    assert task.is_back and task.identity_key == f"back:{HASH_A}"
    # Two identities on one row is stopped by the database CHECK itself.
    with pytest.raises(sqlite3.IntegrityError):
        db_module.enqueue_task(LIBRARY_TAG, back_hash=HASH_A, custom_hash="b" * 64, **common)
    with sqlite3.connect(db_path) as conn:
        with pytest.raises(sqlite3.IntegrityError):
            conn.execute(
                "INSERT INTO generated_images (scryfall_id, back_hash, model, dpi, image_filename, "
                "out_path, original_path) VALUES ('sid', ?, 'm', 1200, 'f', 'o', 'p')",
                (HASH_A,),
            )


# --------------------------------------------------------------------
# Migration 010
# --------------------------------------------------------------------

_V9_SCHEMA = """
CREATE TABLE generation_tasks (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    project_tag TEXT,
    status TEXT NOT NULL DEFAULT 'pending',
    scryfall_id TEXT,
    custom_hash TEXT,
    face_index INTEGER,
    face_label TEXT,
    face_name TEXT NOT NULL,
    card_name TEXT NOT NULL,
    set_code TEXT,
    collector_number TEXT,
    png_url TEXT,
    dpi INTEGER NOT NULL,
    model TEXT NOT NULL,
    tile_size INTEGER NOT NULL DEFAULT 0,
    output_dir TEXT NOT NULL,
    cache_dir TEXT NOT NULL,
    weights_dir TEXT NOT NULL,
    error TEXT,
    created_at TEXT NOT NULL,
    started_at TEXT,
    completed_at TEXT,
    total_faces INTEGER,
    lang TEXT NOT NULL DEFAULT 'en',
    force INTEGER NOT NULL DEFAULT 0,
    attempts INTEGER NOT NULL DEFAULT 0,
    CHECK ((scryfall_id IS NULL) <> (custom_hash IS NULL))
);
CREATE TABLE generated_images (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    scryfall_id TEXT,
    custom_hash TEXT,
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
    png_url TEXT,
    created_at TEXT,
    total_faces INTEGER,
    lang TEXT NOT NULL DEFAULT 'en',
    CHECK ((scryfall_id IS NULL) <> (custom_hash IS NULL))
);
CREATE UNIQUE INDEX idx_generated_images_variant
    ON generated_images(COALESCE(scryfall_id, 'custom:' || custom_hash), COALESCE(face_index, -1), model, dpi);
CREATE TABLE project_gallery_memberships (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    project_tag TEXT NOT NULL,
    image_id INTEGER NOT NULL REFERENCES generated_images(id) ON DELETE CASCADE,
    UNIQUE (project_tag, image_id)
);
CREATE TABLE worker_control (key TEXT PRIMARY KEY, value TEXT NOT NULL);
"""


def test_migration_010_keeps_rows_and_memberships(db_path: Path) -> None:
    with sqlite3.connect(db_path) as conn:
        conn.executescript(_V9_SCHEMA)
        conn.execute(
            "INSERT INTO generated_images (id, scryfall_id, model, dpi, image_filename, out_path, original_path) "
            "VALUES (7, 'sid', 'ultrasharp_v2', 1200, 'f.png', '/o/f.png', '/c/o.png')"
        )
        conn.execute(
            "INSERT INTO generated_images (id, custom_hash, model, dpi, image_filename, out_path, original_path) "
            "VALUES (8, ?, 'custom_source', 400, 'g.png', '/o/g.png', '/c/g.png')",
            ("b" * 64,),
        )
        conn.execute("INSERT INTO project_gallery_memberships (project_tag, image_id) VALUES ('t', 7)")
        conn.execute("INSERT INTO project_gallery_memberships (project_tag, image_id) VALUES ('t', 8)")
        conn.execute(
            "INSERT INTO generation_tasks (project_tag, status, scryfall_id, face_name, card_name, dpi, model, "
            "output_dir, cache_dir, weights_dir, created_at, attempts) "
            "VALUES ('t', 'done', 'sid', 'n', 'n', 1200, 'ultrasharp_v2', 'o', 'c', 'w', 'now', 1)"
        )
        conn.execute("PRAGMA user_version = 9")
        conn.commit()

    init_db(db_path)

    with sqlite3.connect(db_path) as conn:
        assert conn.execute("PRAGMA user_version").fetchone()[0] == db_module.SCHEMA_VERSION
        cols = {r[1] for r in conn.execute("PRAGMA table_info(generated_images)")}
        assert "back_hash" in cols
        assert {r[1] for r in conn.execute("PRAGMA table_info(generation_tasks)")} >= {"back_hash", "attempts"}
        rows = conn.execute("SELECT id, scryfall_id, custom_hash, back_hash FROM generated_images ORDER BY id").fetchall()
        assert rows == [(7, "sid", None, None), (8, None, "b" * 64, None)]
        assert conn.execute("SELECT COUNT(*) FROM project_gallery_memberships").fetchone()[0] == 2
        assert conn.execute("SELECT attempts FROM generation_tasks").fetchone()[0] == 1
        # The three-way CHECK and the identity index are live on the rebuilt table.
        conn.execute(
            "INSERT INTO generated_images (back_hash, model, dpi, image_filename, out_path, original_path) "
            "VALUES (?, 'ultrasharp_v2', 1200, 'h.png', '/o/h.png', '/c/h.png')",
            (HASH_A,),
        )
        with pytest.raises(sqlite3.IntegrityError):
            conn.execute(
                "INSERT INTO generated_images (back_hash, model, dpi, image_filename, out_path, original_path) "
                "VALUES (?, 'ultrasharp_v2', 1200, 'h2.png', '/o/h2.png', '/c/h2.png')",
                (HASH_A,),
            )


# --------------------------------------------------------------------
# Queueing from the Backs tab
# --------------------------------------------------------------------


def test_upscale_queues_source_and_targets_under_the_library_tag(
    client: TestClient, tmp_path: Path, db_path: Path
) -> None:
    content_hash = _upload(client, (744, 1039))  # ~300 DPI
    resp = client.post("/api/library/upscale", json=_body(tmp_path, content_hash))
    assert resp.status_code == 200, resp.text
    out = resp.json()
    assert (out["queued"], out["failed"]) == (2, 0)
    assert _tasks(db_path, out["task_ids"]) == [
        (LIBRARY_TAG, BACK_SOURCE_MODEL, 300),
        (LIBRARY_TAG, "ultrasharp_v2", 1200),
    ]
    task = db_module.get_task(out["task_ids"][-1], db_path=db_path)
    assert task.back_hash == content_hash and task.card_name == "Dragon back"
    # Idempotent while in flight.
    again = client.post("/api/library/upscale", json=_body(tmp_path, content_hash)).json()
    assert again["queued"] == 0


def test_a_back_that_reaches_every_target_only_registers_its_source(
    client: TestClient, tmp_path: Path, db_path: Path
) -> None:
    content_hash = _upload(client, target_pixels(1400))
    out = client.post("/api/library/upscale", json=_body(tmp_path, content_hash)).json()
    assert _tasks(db_path, out["task_ids"]) == [(LIBRARY_TAG, BACK_SOURCE_MODEL, 1400)]


def test_unsynced_or_malformed_backs_are_400(client: TestClient, tmp_path: Path) -> None:
    assert client.post("/api/library/upscale", json=_body(tmp_path, "e" * 64)).status_code == 400
    assert client.post("/api/library/upscale", json=_body(tmp_path, "nope")).status_code == 400
    assert client.get("/api/library/back/nope/status").status_code == 400
    assert client.get("/api/library/wat/" + HASH_A + "/status").status_code == 404


def test_status_lists_the_backs_tasks_and_rows(client: TestClient, tmp_path: Path, db_path: Path) -> None:
    content_hash = _upload(client)
    client.post("/api/library/upscale", json=_body(tmp_path, content_hash))
    status = client.get(f"/api/library/back/{content_hash}/status").json()
    assert status["gallery"] == []
    assert sorted((t["model"], t["dpi"], t["back_hash"]) for t in status["tasks"]) == [
        (BACK_SOURCE_MODEL, 242, content_hash),
        ("ultrasharp_v2", 1200, content_hash),
    ]


# --------------------------------------------------------------------
# What the worker writes
# --------------------------------------------------------------------


def _fake_x4(monkeypatch) -> list[tuple[int, int]]:
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


def _run(db_path: Path, task_ids: list[int]) -> dict[tuple[str, int], FaceResult]:
    """Run the queued tasks as the worker would; results keyed by (model, dpi)."""
    from proxy_scaler import pipeline

    results = {}
    for task_id in task_ids:
        task = db_module.get_task(task_id, db_path=db_path)
        result = pipeline.process_task(task)
        db_module.upsert_gallery_item_for_task(task, result, db_path=db_path)
        db_module.mark_task_done(task_id, db_path=db_path)
        results[(task.model, task.dpi)] = result
    return results


def test_worker_keeps_the_backs_aspect_and_registers_both_rows(
    client: TestClient, tmp_path: Path, db_path: Path, monkeypatch
) -> None:
    """A back is stored uncropped (a 3:4 file here, not 63:88), so its
    variant scales the whole file until its long edge spans 88 mm at the
    target DPI — never forced into the card box."""
    seen = _fake_x4(monkeypatch)
    content_hash = _upload(client, (600, 800))
    out = client.post("/api/library/upscale", json=_body(tmp_path, content_hash)).json()
    results = _run(db_path, out["task_ids"])

    source = results[(BACK_SOURCE_MODEL, 231)]
    assert source.is_back and source.out_path == source.original_path
    assert source.out_path.name == f"back_{content_hash}_single.png" and source.out_path.is_file()

    up = results[("ultrasharp_v2", 1200)]
    assert seen == [(600, 800)]
    with Image.open(up.out_path) as im:
        assert abs(im.width * 4 - im.height * 3) <= 4  # 3:4 kept, to rounding
        assert round(dpi_at_card_size(*im.size)) == 1200
    assert up.out_path.name == f"Dragon_back-back-{content_hash}-ultrasharp_v2-1200dpi.png"

    rows = db_module.list_registry_items_for_back(content_hash, db_path=db_path)
    assert sorted((r["model"], r["dpi"]) for r in rows) == [(BACK_SOURCE_MODEL, 231), ("ultrasharp_v2", 1200)]
    status = client.get(f"/api/library/back/{content_hash}/status").json()
    assert sorted(g["dpi"] for g in status["gallery"]) == [231, 1200]


def test_native_mode_keeps_the_4x_result(client: TestClient, tmp_path: Path, db_path: Path, monkeypatch) -> None:
    _fake_x4(monkeypatch)
    content_hash = _upload(client, (1200, 1600))  # ~462 DPI
    out = client.post("/api/library/upscale", json=_body(tmp_path, content_hash, mode="native")).json()
    results = _run(db_path, out["task_ids"])
    up = results[("ultrasharp_v2", 462 * 4)]
    assert up.dpi == 462 * 4
    with Image.open(up.out_path) as im:
        assert im.size == (4800, 6400)


# --------------------------------------------------------------------
# Printing picks the right version
# --------------------------------------------------------------------


def _print(client: TestClient, tmp_path: Path, db_path: Path, monkeypatch, size=(600, 800)):
    _fake_x4(monkeypatch)
    content_hash = _upload(client, size)
    out = client.post("/api/library/upscale", json=_body(tmp_path, content_hash, dpi_targets=[600, 1200])).json()
    results = _run(db_path, out["task_ids"])
    return content_hash, results


def test_resolve_print_source_ranks_variants_like_a_custom(
    client: TestClient, tmp_path: Path, db_path: Path, monkeypatch
) -> None:
    content_hash, results = _print(client, tmp_path, db_path, monkeypatch)
    original = backs.original_path(content_hash)
    by_dpi = {r.dpi: r.out_path for r in results.values()}

    # Preferred DPI wins when it exists; otherwise the best available
    # (never a blank Reverse); "Any" is the highest.
    assert backs.resolve_print_source(content_hash, preferred_dpi=1200, db_path=db_path) == by_dpi[1200]
    assert backs.resolve_print_source(content_hash, preferred_dpi=600, db_path=db_path) == by_dpi[600]
    assert backs.resolve_print_source(content_hash, preferred_dpi=800, db_path=db_path) == by_dpi[1200]
    assert backs.resolve_print_source(content_hash, db_path=db_path) == by_dpi[1200]
    # "Use originals" prints the synced upload; so does a back with no rows.
    assert backs.resolve_print_source(content_hash, use_originals=True, db_path=db_path) == original
    # A variant whose file vanished is skipped, not printed.
    by_dpi[1200].unlink()
    assert backs.resolve_print_source(content_hash, preferred_dpi=1200, db_path=db_path) == by_dpi[600]


def test_zip_export_ships_the_preferred_back_variant(
    client: TestClient, tmp_path: Path, db_path: Path, monkeypatch
) -> None:
    from proxy_scaler.api.routers.export import _resolve_back

    content_hash, results = _print(client, tmp_path, db_path, monkeypatch)
    body = ExportZipIn(project_tag="t", entries=[], back_image_hash=content_hash, preferred_dpi=1200)
    assert _resolve_back(body) == results[("ultrasharp_v2", 1200)].out_path
    body = ExportZipIn(project_tag="t", entries=[], back_image_hash=content_hash, preferred_dpi=600)
    assert _resolve_back(body) == results[("ultrasharp_v2", 600)].out_path
    body = ExportZipIn(project_tag="t", entries=[], back_image_hash=content_hash, use_originals=True)
    assert _resolve_back(body) == backs.original_path(content_hash)


def test_deleting_the_back_removes_its_derivatives(
    client: TestClient, tmp_path: Path, db_path: Path, monkeypatch
) -> None:
    content_hash, results = _print(client, tmp_path, db_path, monkeypatch)
    upscaled = [r.out_path for r in results.values() if r.model != BACK_SOURCE_MODEL]
    assert all(p.is_file() for p in upscaled)
    assert client.delete(f"/api/backs/{content_hash}").json()["removed"] == 1
    assert not any(p.exists() for p in upscaled)
    assert db_module.list_registry_items_for_back(content_hash, db_path=db_path) == []
    assert backs.resolve_print_source(content_hash, db_path=db_path) is None


def test_a_back_never_matches_a_decklist_entry(tmp_path: Path) -> None:
    from proxy_scaler.pdf_layout import match_quantities

    back = FaceResult(
        out_path=tmp_path / "b.png", original_path=tmp_path / "b.png", scryfall_id=None, back_hash=HASH_A,
        face_index=None, face_name="Sol Ring", card_name="Sol Ring", set_code="", collector_number="",
        png_url="", dpi=1200, model="ultrasharp_v2",
    )
    units, missing, _ = match_quantities([DeckEntry(quantity=1, name="Sol Ring")], [back])
    assert units == [] and len(missing) == 1
