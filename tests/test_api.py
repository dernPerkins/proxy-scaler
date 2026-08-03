"""TestClient-based tests for proxy_scaler/api/ — the FastAPI layer
replacing Streamlit. Uses tmp_path-isolated SQLite via the same
PROXY_SCALER_DB_PATH env var worker.py/supervisor.py already read (see
proxy_scaler/api/deps.py::get_db_path), and mocks ScryfallClient the same
way test_pipeline.py/test_services_generation.py do — no real network
calls."""

from __future__ import annotations

from pathlib import Path

import pytest
from fastapi.testclient import TestClient
from PIL import Image

from proxy_scaler import db
from proxy_scaler.scryfall import ScryfallClient

SOL_RING_CARD = {
    "id": "sol-id",
    "name": "Sol Ring",
    "set": "c21",
    "collector_number": "263",
    "image_status": "highres_scan",
    "image_uris": {"png": "https://example.com/sol.png"},
}


@pytest.fixture
def client(tmp_path: Path, monkeypatch) -> TestClient:
    db_path = tmp_path / "test.db"
    db.init_db(db_path)
    monkeypatch.setenv("PROXY_SCALER_DB_PATH", str(db_path))
    monkeypatch.setenv("PROXY_SCALER_WORKER_LOCK_PATH", str(tmp_path / "worker.lock"))
    monkeypatch.setattr(
        ScryfallClient, "resolve_many", lambda self, entries: [(SOL_RING_CARD, [])] * len(entries)
    )
    from proxy_scaler.api.app import app

    return TestClient(app)


def test_health(client: TestClient) -> None:
    resp = client.get("/api/health")
    assert resp.status_code == 200
    assert resp.json() == {"status": "ok"}


def test_list_models_matches_upscale_model_enum(client: TestClient) -> None:
    """Regression guard: the frontend's model dropdown must read this
    list, not hardcode it — a hardcoded copy in the React rewrite
    silently dropped two real models. Assert this endpoint stays in
    lockstep with the enum itself, not a second hand-maintained list."""
    from proxy_scaler.upscale import UpscaleModel

    resp = client.get("/api/models")
    assert resp.status_code == 200
    models = resp.json()
    assert {m["value"] for m in models} == {m.value for m in UpscaleModel}
    assert len(models) == len(list(UpscaleModel))
    for m in models:
        assert m["label"]


def test_project_crud_round_trip(client: TestClient) -> None:
    resp = client.post("/api/projects", json={"name": "My Deck"})
    assert resp.status_code == 200
    created = resp.json()
    pid = created["id"]
    assert created["name"] == "My Deck"

    resp = client.get("/api/projects")
    assert resp.status_code == 200
    assert [p["id"] for p in resp.json()] == [pid]

    resp = client.get(f"/api/projects/{pid}")
    assert resp.status_code == 200
    loaded = resp.json()
    assert loaded["name"] == "My Deck"
    assert loaded["settings"]["model"]  # has a default

    resp = client.put(
        f"/api/projects/{pid}",
        json={"name": "Renamed Deck", "settings": loaded["settings"]},
    )
    assert resp.status_code == 200
    assert resp.json()["name"] == "Renamed Deck"

    resp = client.delete(f"/api/projects/{pid}")
    assert resp.status_code == 204
    resp = client.get(f"/api/projects/{pid}")
    assert resp.status_code == 404


def test_project_not_found(client: TestClient) -> None:
    resp = client.get("/api/projects/999")
    assert resp.status_code == 404


def test_clear_all_projects_requires_confirm(client: TestClient) -> None:
    client.post("/api/projects", json={"name": "Deck"})
    resp = client.delete("/api/projects")
    assert resp.status_code == 400
    resp = client.delete("/api/projects?confirm=true")
    assert resp.status_code == 204
    assert client.get("/api/projects").json() == []


def test_import_decklist_adds_card_and_reimport_is_noop(client: TestClient) -> None:
    pid = client.post("/api/projects", json={"name": "Deck"}).json()["id"]

    resp = client.post(f"/api/projects/{pid}/import", json={"text": "1 Sol Ring"})
    assert resp.status_code == 200
    assert resp.json() == {"added": 1, "skipped": 0, "failed": 0}

    resp = client.post(f"/api/projects/{pid}/import", json={"text": "1 Sol Ring"})
    assert resp.json() == {"added": 0, "skipped": 1, "failed": 0}

    resp = client.get(f"/api/projects/{pid}")
    assert resp.json()["import_decklist_text"] == "1 Sol Ring"


def test_list_cards_empty_project(client: TestClient) -> None:
    pid = client.post("/api/projects", json={"name": "Deck"}).json()["id"]
    resp = client.get(f"/api/projects/{pid}/cards")
    assert resp.status_code == 200
    assert resp.json() == []


def test_list_cards_after_import_has_no_faces_yet(client: TestClient) -> None:
    pid = client.post("/api/projects", json={"name": "Deck"}).json()["id"]
    client.post(f"/api/projects/{pid}/import", json={"text": "1 Sol Ring"})
    resp = client.get(f"/api/projects/{pid}/cards")
    cards = resp.json()
    assert len(cards) == 1
    assert cards[0]["card_name"] == "Sol Ring"
    assert cards[0]["faces"] == []


def test_remove_card(client: TestClient) -> None:
    pid = client.post("/api/projects", json={"name": "Deck"}).json()["id"]
    client.post(f"/api/projects/{pid}/import", json={"text": "1 Sol Ring"})
    card_id = client.get(f"/api/projects/{pid}/cards").json()[0]["id"]
    resp = client.delete(f"/api/projects/{pid}/cards/{card_id}")
    assert resp.status_code == 204
    assert client.get(f"/api/projects/{pid}/cards").json() == []


def test_generate_enqueues_tasks_for_project_cards(
    client: TestClient, tmp_path: Path
) -> None:
    pid = client.post("/api/projects", json={"name": "Deck"}).json()["id"]
    client.post(f"/api/projects/{pid}/import", json={"text": "1 Sol Ring"})

    resp = client.post(
        "/api/generate",
        json={
            "project_id": pid,
            "model": "swinir",
            "dpi_targets": [800],
            "skip_existing": False,
            "output_dir": str(tmp_path / "out"),
            "cache_dir": str(tmp_path / "cache"),
            "weights_dir": str(tmp_path / "weights"),
        },
    )
    assert resp.status_code == 200
    body = resp.json()
    assert body["queued"] == 1
    assert len(body["task_ids"]) == 1

    resp = client.get("/api/tasks", params={"project_id": pid})
    assert resp.status_code == 200
    tasks = resp.json()
    assert len(tasks) == 1
    assert tasks[0]["status"] == "pending"
    assert tasks[0]["dpi"] == 800


def test_generate_requires_at_least_one_dpi(client: TestClient, tmp_path: Path) -> None:
    pid = client.post("/api/projects", json={"name": "Deck"}).json()["id"]
    client.post(f"/api/projects/{pid}/import", json={"text": "1 Sol Ring"})
    resp = client.post(
        "/api/generate",
        json={
            "project_id": pid,
            "model": "swinir",
            "dpi_targets": [],
            "output_dir": str(tmp_path / "out"),
            "cache_dir": str(tmp_path / "cache"),
            "weights_dir": str(tmp_path / "weights"),
        },
    )
    assert resp.status_code == 400


def test_regenerate_gallery_item_redoes_exact_variant(
    client: TestClient, tmp_path: Path
) -> None:
    """Mirrors the old Streamlit "Regen" button: redo one exact existing
    variant unchanged. The endpoint looks up scryfall_id/png_url/model/dpi
    from the stored gallery item server-side — the client only ever
    supplies a gallery_item_id (+ optional tile_size), never those
    low-level fields."""
    pid = client.post("/api/projects", json={"name": "Deck"}).json()["id"]
    client.post(f"/api/projects/{pid}/import", json={"text": "1 Sol Ring"})

    img_path = tmp_path / "sol_ring.png"
    Image.new("RGBA", (50, 70), (1, 2, 3, 255)).save(img_path, format="PNG")
    from proxy_scaler.pipeline import FaceResult

    result = FaceResult(
        out_path=img_path,
        original_path=img_path,
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
    task = db.TaskRow(
        id=1,
        project_id=pid,
        status="done",
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
        output_dir=str(tmp_path),
        cache_dir=str(tmp_path),
        weights_dir=str(tmp_path),
        error=None,
        created_at="2026-01-01T00:00:00Z",
        started_at=None,
        completed_at=None,
    )
    import os

    db.upsert_gallery_item_for_task(task, result, db_path=os.environ["PROXY_SCALER_DB_PATH"])

    cards = client.get(f"/api/projects/{pid}/cards").json()
    gallery_item_id = cards[0]["faces"][0]["variants"][0]["gallery_item_id"]

    resp = client.post(f"/api/projects/{pid}/regenerate/{gallery_item_id}", json={})
    assert resp.status_code == 200
    body = resp.json()
    assert body["queued"] == 1
    assert len(body["task_ids"]) == 1

    new_task = client.get(f"/api/tasks/{body['task_ids'][0]}").json()
    assert new_task["scryfall_id"] == "sol-id"
    assert new_task["dpi"] == 800
    assert new_task["model"] == "swinir"


def test_regenerate_gallery_item_not_found(client: TestClient) -> None:
    pid = client.post("/api/projects", json={"name": "Deck"}).json()["id"]
    resp = client.post(f"/api/projects/{pid}/regenerate/999", json={})
    assert resp.status_code == 404


def test_task_cancel(client: TestClient, tmp_path: Path) -> None:
    pid = client.post("/api/projects", json={"name": "Deck"}).json()["id"]
    client.post(f"/api/projects/{pid}/import", json={"text": "1 Sol Ring"})
    resp = client.post(
        "/api/generate",
        json={
            "project_id": pid,
            "model": "swinir",
            "dpi_targets": [800],
            "skip_existing": False,
            "output_dir": str(tmp_path / "out"),
            "cache_dir": str(tmp_path / "cache"),
            "weights_dir": str(tmp_path / "weights"),
        },
    )
    task_id = resp.json()["task_ids"][0]
    resp = client.post(f"/api/tasks/{task_id}/cancel")
    assert resp.status_code == 200
    assert resp.json() == {"canceled": True}
    assert client.get(f"/api/tasks/{task_id}").json()["status"] == "canceled"


def test_worker_status_not_running(client: TestClient) -> None:
    resp = client.get("/api/worker/status")
    assert resp.status_code == 200
    assert resp.json() == {"running": False}


def _pdf_layout_body(**overrides) -> dict:
    body = dict(
        page_width_mm=210.0,
        page_height_mm=297.0,
        cols=3,
        rows=3,
        export_dpi=800,
    )
    body.update(overrides)
    return body


def test_pdf_preview_no_cards_is_400(client: TestClient) -> None:
    pid = client.post("/api/projects", json={"name": "Deck"}).json()["id"]
    resp = client.post(f"/api/projects/{pid}/pdf/preview", json=_pdf_layout_body())
    assert resp.status_code == 400


def test_pdf_preview_zero_units_when_nothing_generated(client: TestClient) -> None:
    """A card with no generated images yet contributes zero print units —
    match_quantities's `unmatched` list is for entries that fail to
    resolve at all, a different case from "resolved but nothing rendered
    yet", so it's correctly empty here."""
    pid = client.post("/api/projects", json={"name": "Deck"}).json()["id"]
    client.post(f"/api/projects/{pid}/import", json={"text": "1 Sol Ring"})
    resp = client.post(f"/api/projects/{pid}/pdf/preview", json=_pdf_layout_body())
    assert resp.status_code == 200
    body = resp.json()
    assert body["units"] == 0
    assert body["page_count"] == 0


def test_pdf_generate_returns_real_pdf_file(client: TestClient, tmp_path: Path) -> None:
    """The concrete fix for the reported download bug: a real file
    response with correct headers and non-trivial byte length, instead of
    Streamlit's download-attribute-based st.download_button."""
    pid = client.post("/api/projects", json={"name": "Deck"}).json()["id"]
    client.post(f"/api/projects/{pid}/import", json={"text": "1 Sol Ring"})

    # Enqueue + fake a completed task's on-disk output the way the worker
    # normally would: write a real PNG and upsert the gallery row directly.
    img_path = tmp_path / "sol_ring.png"
    Image.new("RGBA", (200, 280), (10, 20, 30, 255)).save(img_path, format="PNG")
    from proxy_scaler.pipeline import FaceResult

    result = FaceResult(
        out_path=img_path,
        original_path=img_path,
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
    task = db.TaskRow(
        id=1,
        project_id=pid,
        status="done",
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
        output_dir=str(tmp_path),
        cache_dir=str(tmp_path),
        weights_dir=str(tmp_path),
        error=None,
        created_at="2026-01-01T00:00:00Z",
        started_at=None,
        completed_at=None,
    )
    import os

    db.upsert_gallery_item_for_task(task, result, db_path=os.environ["PROXY_SCALER_DB_PATH"])

    resp = client.post(f"/api/projects/{pid}/pdf", json=_pdf_layout_body())
    assert resp.status_code == 200
    assert resp.headers["content-type"] == "application/pdf"
    assert "attachment" in resp.headers["content-disposition"]
    assert "Deck.pdf" in resp.headers["content-disposition"]
    assert len(resp.content) > 100  # a real, non-trivial PDF, not an empty stub


def test_images_original_and_full(client: TestClient, tmp_path: Path) -> None:
    pid = client.post("/api/projects", json={"name": "Deck"}).json()["id"]
    client.post(f"/api/projects/{pid}/import", json={"text": "1 Sol Ring"})

    img_path = tmp_path / "sol_ring.png"
    Image.new("RGBA", (50, 70), (1, 2, 3, 255)).save(img_path, format="PNG")
    from proxy_scaler.pipeline import FaceResult

    result = FaceResult(
        out_path=img_path,
        original_path=img_path,
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
    task = db.TaskRow(
        id=1,
        project_id=pid,
        status="done",
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
        output_dir=str(tmp_path),
        cache_dir=str(tmp_path),
        weights_dir=str(tmp_path),
        error=None,
        created_at="2026-01-01T00:00:00Z",
        started_at=None,
        completed_at=None,
    )
    import os

    db.upsert_gallery_item_for_task(task, result, db_path=os.environ["PROXY_SCALER_DB_PATH"])

    cards = client.get(f"/api/projects/{pid}/cards").json()
    variant = cards[0]["faces"][0]["variants"][0]
    assert variant["status"] == "done"
    gallery_item_id = variant["gallery_item_id"]
    assert gallery_item_id is not None

    resp = client.get(f"/api/projects/{pid}/images/{gallery_item_id}/full")
    assert resp.status_code == 200
    assert resp.headers["content-type"] == "image/png"
    assert len(resp.content) > 0

    resp = client.get(f"/api/projects/{pid}/images/{gallery_item_id}/original")
    assert resp.status_code == 200


def test_images_not_found(client: TestClient) -> None:
    pid = client.post("/api/projects", json={"name": "Deck"}).json()["id"]
    resp = client.get(f"/api/projects/{pid}/images/999/full")
    assert resp.status_code == 404


def test_clear_generated_data(client: TestClient, tmp_path: Path) -> None:
    output_dir = tmp_path / "out"
    cache_dir = tmp_path / "cache"
    output_dir.mkdir()
    cache_dir.mkdir()
    (output_dir / "a.png").write_bytes(b"x")
    resp = client.post(
        "/api/generated-data/clear",
        json={"output_dir": str(output_dir), "cache_dir": str(cache_dir)},
    )
    assert resp.status_code == 200
    assert not (output_dir / "a.png").exists()
