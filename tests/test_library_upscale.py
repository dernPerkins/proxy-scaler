"""Library upscaling: upscaling a Custom Image straight from the Customs
tab, with no project involved (api/routers/library.py).

Pins the three things that make it work without a project: work is queued
under dpi.LIBRARY_TAG, status is read by identity across every tag, and a
project prints (and adopts) a variant it never made itself.
"""

from __future__ import annotations

import hashlib
import io
from pathlib import Path

import pytest
from fastapi.testclient import TestClient
from PIL import Image

from proxy_scaler import db
from proxy_scaler.decklist import DeckEntry
from proxy_scaler.dpi import CUSTOM_SOURCE_MODEL, LIBRARY_TAG, target_pixels
from proxy_scaler.pipeline import FaceResult


@pytest.fixture
def db_path(tmp_path: Path) -> Path:
    return tmp_path / "test.db"


@pytest.fixture
def client(tmp_path: Path, db_path: Path, monkeypatch) -> TestClient:
    db.init_db(db_path)
    monkeypatch.setenv("PROXY_SCALER_DB_PATH", str(db_path))
    monkeypatch.setenv("PROXY_SCALER_WORKER_LOCK_PATH", str(tmp_path / "worker.lock"))
    monkeypatch.chdir(tmp_path)
    from proxy_scaler.api.app import app

    return TestClient(app)


def _png(size: tuple[int, int], color=(20, 40, 90)) -> tuple[bytes, str]:
    buf = io.BytesIO()
    Image.new("RGB", size, color).save(buf, format="PNG")
    data = buf.getvalue()
    return data, hashlib.sha256(data).hexdigest()


def _upload(client: TestClient, size=(744, 1039)) -> str:
    data, content_hash = _png(size)
    assert client.post(f"/api/customs/{content_hash}", content=data).status_code == 200
    return content_hash


def _upscale_body(tmp_path: Path, content_hash: str, **over) -> dict:
    body = {
        "kind": "custom",
        "content_hash": content_hash,
        "label": "My Alter",
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


def _tasks(db_path: Path, task_ids: list[int]) -> list[tuple[str | None, str, int]]:
    rows = [db.get_task(t, db_path=db_path) for t in task_ids]
    return sorted((r.project_tag, r.model, r.dpi) for r in rows)


# --------------------------------------------------------------------
# Queueing under the library tag
# --------------------------------------------------------------------


def test_upscale_queues_source_and_targets_under_the_library_tag(
    client: TestClient, tmp_path: Path, db_path: Path
) -> None:
    content_hash = _upload(client)
    resp = client.post("/api/library/upscale", json=_upscale_body(tmp_path, content_hash))
    assert resp.status_code == 200, resp.text
    out = resp.json()
    assert (out["queued"], out["failed"]) == (2, 0)
    assert _tasks(db_path, out["task_ids"]) == [
        (LIBRARY_TAG, CUSTOM_SOURCE_MODEL, 300),
        (LIBRARY_TAG, "ultrasharp_v2", 1200),
    ]
    task = db.get_task(out["task_ids"][-1], db_path=db_path)
    assert task.card_name == "My Alter" and task.custom_hash == content_hash


def test_upscale_is_idempotent_while_tasks_are_in_flight(
    client: TestClient, tmp_path: Path
) -> None:
    content_hash = _upload(client)
    first = client.post("/api/library/upscale", json=_upscale_body(tmp_path, content_hash)).json()
    second = client.post("/api/library/upscale", json=_upscale_body(tmp_path, content_hash)).json()
    assert first["queued"] == 2
    assert second["queued"] == 0 and second["task_ids"] == []


def test_native_mode_queues_the_capped_4x_variant(client: TestClient, tmp_path: Path, db_path: Path) -> None:
    content_hash = _upload(client, target_pixels(600))
    out = client.post(
        "/api/library/upscale", json=_upscale_body(tmp_path, content_hash, mode="native")
    ).json()
    assert (LIBRARY_TAG, "ultrasharp_v2", 2400) in _tasks(db_path, out["task_ids"])


def test_upscale_queues_nothing_for_an_upload_that_reaches_every_target(
    client: TestClient, tmp_path: Path, db_path: Path
) -> None:
    content_hash = _upload(client, target_pixels(1400))
    out = client.post("/api/library/upscale", json=_upscale_body(tmp_path, content_hash)).json()
    assert _tasks(db_path, out["task_ids"]) == [(LIBRARY_TAG, CUSTOM_SOURCE_MODEL, 1400)]


def test_upscale_rejects_an_unsynced_image_and_a_bad_hash(client: TestClient, tmp_path: Path) -> None:
    resp = client.post("/api/library/upscale", json=_upscale_body(tmp_path, "e" * 64))
    assert resp.status_code == 400 and "uploaded" in resp.json()["detail"]
    assert client.post("/api/library/upscale", json=_upscale_body(tmp_path, "nope")).status_code == 400


# --------------------------------------------------------------------
# Status by identity, across tags
# --------------------------------------------------------------------


def _register_done(db_path: Path, tag: str, content_hash: str, dpi: int, model: str, out_dir: Path) -> None:
    """A finished variant as the worker would have left it, under `tag`."""
    out = out_dir / f"{content_hash[:8]}-{model}-{dpi}.png"
    out.parent.mkdir(parents=True, exist_ok=True)
    Image.new("RGB", (8, 8)).save(out)
    db.upsert_gallery_item(
        tag,
        FaceResult(
            out_path=out,
            original_path=out_dir / "originals" / f"custom_{content_hash}_single.png",
            scryfall_id=None,
            custom_hash=content_hash,
            face_index=None,
            face_name="My Alter",
            card_name="My Alter",
            set_code="",
            collector_number="",
            png_url="",
            dpi=dpi,
            model=model,
            total_faces=1,
        ),
        db_path=db_path,
    )


def test_status_unions_every_tag(client: TestClient, tmp_path: Path, db_path: Path) -> None:
    content_hash = _upload(client)
    # A project made the 600 variant earlier; the library tab queues 1200.
    _register_done(db_path, "a" * 32, content_hash, 600, "ultrasharp_v2", tmp_path / "out")
    client.post("/api/library/upscale", json=_upscale_body(tmp_path, content_hash))

    status = client.get(f"/api/library/custom/{content_hash}/status").json()
    assert sorted((g["dpi"], g["model"]) for g in status["gallery"]) == [(600, "ultrasharp_v2")]
    assert sorted((t["project_tag"], t["dpi"]) for t in status["tasks"]) == [
        (LIBRARY_TAG, 300),
        (LIBRARY_TAG, 1200),
    ]
    # Another image's rows never leak in.
    other = _upload(client, (500, 700))
    assert client.get(f"/api/library/custom/{other}/status").json() == {"tasks": [], "gallery": []}


def test_registry_first_adopts_a_variant_another_project_made(
    client: TestClient, tmp_path: Path, db_path: Path
) -> None:
    content_hash = _upload(client)
    _register_done(db_path, "b" * 32, content_hash, 1200, "ultrasharp_v2", tmp_path / "out")
    out = client.post("/api/library/upscale", json=_upscale_body(tmp_path, content_hash)).json()
    # Only the source registration is new work; the 1200 was adopted.
    assert [t[1:] for t in _tasks(db_path, out["task_ids"])] == [(CUSTOM_SOURCE_MODEL, 300)]
    library_rows = db.list_gallery_items(LIBRARY_TAG, db_path=db_path)
    assert [(r["dpi"], r["model"]) for r in library_rows] == [(1200, "ultrasharp_v2")]


# --------------------------------------------------------------------
# A project prints and adopts what the library made
# --------------------------------------------------------------------


def test_print_sees_a_library_made_variant_the_project_never_generated(
    client: TestClient, tmp_path: Path, db_path: Path
) -> None:
    from proxy_scaler.api.routers.pdf import gallery_for_print

    content_hash = _upload(client)
    _register_done(db_path, LIBRARY_TAG, content_hash, 1200, "ultrasharp_v2", tmp_path / "out")
    entries = [DeckEntry(quantity=1, name="My Alter", custom_hash=content_hash)]
    items = gallery_for_print("c" * 32, entries, db_path)
    assert [(i.dpi, i.model, i.custom_hash) for i in items] == [(1200, "ultrasharp_v2", content_hash)]
    # No entry for the image → nothing merged.
    assert gallery_for_print("c" * 32, [DeckEntry(quantity=1, name="Sol Ring")], db_path) == []


def test_adopt_takes_custom_variants_by_hash_only(client: TestClient, tmp_path: Path, db_path: Path) -> None:
    content_hash = _upload(client)
    _register_done(db_path, LIBRARY_TAG, content_hash, 1200, "ultrasharp_v2", tmp_path / "out")
    tag = "d" * 32
    # A Scryfall entry that happens to share the label adopts nothing.
    assert db.adopt_gallery_items(tag, [DeckEntry(quantity=1, name="My Alter")], db_path=db_path) == 0
    assert db.adopt_gallery_items(
        tag, [DeckEntry(quantity=1, name="Whatever", custom_hash=content_hash)], db_path=db_path
    ) == 1
    assert [(r["dpi"], r["custom_hash"]) for r in db.list_gallery_items(tag, db_path=db_path)] == [
        (1200, content_hash)
    ]
