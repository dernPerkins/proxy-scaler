"""TestClient-based tests for proxy_scaler/api/ — the generation server's
FastAPI layer (Scryfall resolution, download+upscale pipeline, task
queue, gallery, PDF assembly), scoped by an opaque project_tag string
(see ARCHITECTURE.md). Project management itself lives client-side now,
so there's nothing project-CRUD-shaped to test here any more. Uses
tmp_path-isolated SQLite via the same PROXY_SCALER_DB_PATH env var
worker.py/supervisor.py already read (see proxy_scaler/api/deps.py::
get_db_path), and mocks ScryfallClient the same way
test_services_generation.py does — no real network calls."""

from __future__ import annotations

import os
import time
from contextlib import ExitStack
from pathlib import Path
from unittest.mock import patch

import pytest
from fastapi.testclient import TestClient
from PIL import Image

from proxy_scaler import db
from proxy_scaler import pdf_jobs
from proxy_scaler.pipeline import FaceResult
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


def _sol_ring_entry(**overrides) -> dict:
    entry = {
        "quantity": 1,
        "name": "Sol Ring",
        "set_code": "c21",
        "collector_number": "263",
        "raw_line": "1 Sol Ring (c21) 263",
    }
    entry.update(overrides)
    return entry


def _write_gallery_item(
    tmp_path: Path, db_path: Path, project_tag: str, **task_overrides
) -> dict:
    """Fakes a completed task's on-disk output + gallery row the way the
    worker normally would, without a real Scryfall/GPU round trip."""
    img_path = tmp_path / "sol_ring.png"
    Image.new("RGBA", (200, 280), (10, 20, 30, 255)).save(img_path, format="PNG")
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
    task_kwargs = dict(
        id=1,
        project_tag=project_tag,
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
        total_faces=None,
    )
    task_kwargs.update(task_overrides)
    task = db.TaskRow(**task_kwargs)
    db.upsert_gallery_item_for_task(task, result, db_path=db_path)
    [item] = db.list_gallery_items(project_tag, db_path=db_path)
    return item


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


def test_device_reports_gpu_when_cuda_available(client: TestClient) -> None:
    # Patched at torch's own module, not proxy_scaler.upscale.torch...:
    # resolve_device() imports torch locally inside its function body
    # (see upscale.py's module docstring on why), so the name only
    # exists at torch's real defining module by the time this runs — see
    # tests/test_oom.py for the same gotcha with torchvision's to_tensor.
    with patch("torch.cuda.is_available", return_value=True):
        resp = client.get("/api/device")
    assert resp.status_code == 200
    assert resp.json() == {"kind": "gpu"}


def test_device_reports_cpu_when_no_gpu_available(client: TestClient) -> None:
    # All three of resolve_device()'s GPU paths have to be closed off, not
    # just CUDA and MPS: torch_directml is an optional dependency present
    # only in a GPU_VARIANT=directml build, and it reports *any* DirectX12
    # adapter — including an Nvidia one. So on a Windows box with that
    # variant installed, patching only the first two left the third
    # answering truthfully and this test failed for an entirely correct
    # reason. Patched conditionally because the import genuinely doesn't
    # exist in a default or ROCm build, which is exactly why
    # resolve_device() guards it with try/except ImportError.
    directml = ExitStack()
    with directml:
        try:
            import torch_directml  # noqa: F401
        except ImportError:
            pass
        else:
            directml.enter_context(
                patch("torch_directml.is_available", return_value=False)
            )
        with (
            patch("torch.cuda.is_available", return_value=False),
            patch("torch.backends.mps.is_available", return_value=False),
        ):
            resp = client.get("/api/device")
    assert resp.status_code == 200
    assert resp.json() == {"kind": "cpu"}


def test_resolve_returns_canonical_identity(client: TestClient) -> None:
    resp = client.post("/api/resolve", json={"entries": [_sol_ring_entry()]})
    assert resp.status_code == 200
    body = resp.json()
    assert body["failed"] == []
    assert len(body["resolved"]) == 1
    card = body["resolved"][0]
    assert card["quantity"] == 1
    assert len(card["faces"]) == 1
    assert card["faces"][0]["scryfall_id"] == "sol-id"
    assert card["faces"][0]["card_name"] == "Sol Ring"


def test_resolve_empty_entries_is_a_noop(client: TestClient) -> None:
    resp = client.post("/api/resolve", json={"entries": []})
    assert resp.status_code == 200
    assert resp.json() == {"resolved": [], "failed": []}


def test_resolve_reports_scryfall_failures(client: TestClient, monkeypatch) -> None:
    from proxy_scaler.scryfall import ScryfallError

    monkeypatch.setattr(
        ScryfallClient, "resolve_many", lambda self, entries: [ScryfallError("not found")]
    )
    resp = client.post("/api/resolve", json={"entries": [_sol_ring_entry()]})
    assert resp.status_code == 200
    body = resp.json()
    assert body["resolved"] == []
    assert len(body["failed"]) == 1
    assert "not found" in body["failed"][0]["error"]


def test_generate_enqueues_tasks_for_pushed_entries(
    client: TestClient, tmp_path: Path
) -> None:
    resp = client.post(
        "/api/generate",
        json={
            "project_tag": "tag-a",
            "entries": [_sol_ring_entry()],
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

    resp = client.get("/api/tasks", params={"project_tag": "tag-a"})
    assert resp.status_code == 200
    tasks = resp.json()
    assert len(tasks) == 1
    assert tasks[0]["status"] == "pending"
    assert tasks[0]["dpi"] == 800
    assert tasks[0]["project_tag"] == "tag-a"

    # A different project_tag sees none of this project's tasks.
    assert client.get("/api/tasks", params={"project_tag": "tag-b"}).json() == []


def test_generate_requires_at_least_one_dpi(client: TestClient, tmp_path: Path) -> None:
    resp = client.post(
        "/api/generate",
        json={
            "project_tag": "tag-a",
            "entries": [_sol_ring_entry()],
            "model": "swinir",
            "dpi_targets": [],
            "output_dir": str(tmp_path / "out"),
            "cache_dir": str(tmp_path / "cache"),
            "weights_dir": str(tmp_path / "weights"),
        },
    )
    assert resp.status_code == 400


def test_generate_requires_at_least_one_entry(client: TestClient, tmp_path: Path) -> None:
    resp = client.post(
        "/api/generate",
        json={
            "project_tag": "tag-a",
            "entries": [],
            "model": "swinir",
            "dpi_targets": [800],
            "output_dir": str(tmp_path / "out"),
            "cache_dir": str(tmp_path / "cache"),
            "weights_dir": str(tmp_path / "weights"),
        },
    )
    assert resp.status_code == 400


def test_task_cancel(client: TestClient, tmp_path: Path) -> None:
    resp = client.post(
        "/api/generate",
        json={
            "project_tag": "tag-a",
            "entries": [_sol_ring_entry()],
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


def test_gallery_list_empty_project(client: TestClient) -> None:
    resp = client.get("/api/gallery", params={"project_tag": "tag-a"})
    assert resp.status_code == 200
    assert resp.json() == []


def test_gallery_list_and_fetch_images(client: TestClient, tmp_path: Path) -> None:
    db_path = os.environ["PROXY_SCALER_DB_PATH"]
    item = _write_gallery_item(tmp_path, db_path, "tag-a")

    resp = client.get("/api/gallery", params={"project_tag": "tag-a"})
    assert resp.status_code == 200
    listed = resp.json()
    assert len(listed) == 1
    assert listed[0]["id"] == item["id"]
    assert listed[0]["scryfall_id"] == "sol-id"

    resp = client.get(f"/api/gallery/{item['id']}/full")
    assert resp.status_code == 200
    assert resp.headers["content-type"] == "image/png"
    assert len(resp.content) > 0

    resp = client.get(f"/api/gallery/{item['id']}/original")
    assert resp.status_code == 200


def test_gallery_image_not_found(client: TestClient) -> None:
    resp = client.get("/api/gallery/999/full")
    assert resp.status_code == 404


def test_regenerate_gallery_item_redoes_exact_variant(
    client: TestClient, tmp_path: Path
) -> None:
    """Redo one exact existing variant unchanged. The endpoint looks up
    scryfall_id/png_url/model/dpi from the stored gallery item
    server-side — the client only ever supplies a gallery_item_id (+
    tile_size/output paths), never those low-level fields."""
    db_path = os.environ["PROXY_SCALER_DB_PATH"]
    item = _write_gallery_item(tmp_path, db_path, "tag-a")

    resp = client.post(
        f"/api/gallery/{item['id']}/regenerate",
        json={
            "output_dir": str(tmp_path),
            "cache_dir": str(tmp_path),
            "weights_dir": str(tmp_path),
        },
    )
    assert resp.status_code == 200
    body = resp.json()
    assert body["queued"] == 1
    assert len(body["task_ids"]) == 1

    new_task = client.get(f"/api/tasks/{body['task_ids'][0]}").json()
    assert new_task["scryfall_id"] == "sol-id"
    assert new_task["dpi"] == 800
    assert new_task["model"] == "swinir"
    assert new_task["project_tag"] == "tag-a"


def test_regenerate_gallery_item_not_found(client: TestClient, tmp_path: Path) -> None:
    resp = client.post(
        "/api/gallery/999/regenerate",
        json={"output_dir": str(tmp_path), "cache_dir": str(tmp_path), "weights_dir": str(tmp_path)},
    )
    assert resp.status_code == 404


def _pdf_layout_body(**overrides) -> dict:
    body = dict(
        project_tag="tag-a",
        entries=[_sol_ring_entry()],
        page_width_mm=210.0,
        page_height_mm=297.0,
        cols=3,
        rows=3,
        export_dpi=800,
    )
    body.update(overrides)
    return body


def test_pdf_preview_no_entries_is_400(client: TestClient) -> None:
    resp = client.post("/api/pdf/preview", json=_pdf_layout_body(entries=[]))
    assert resp.status_code == 400


def test_pdf_preview_zero_units_when_nothing_generated(client: TestClient) -> None:
    """An entry with no generated image at all contributes zero print units
    and is reported in `missing` — the decklist is authoritative, so a card
    that was never generated is a real problem to surface, not something to
    silently print anyway."""
    resp = client.post("/api/pdf/preview", json=_pdf_layout_body())
    assert resp.status_code == 200
    body = resp.json()
    assert body["units"] == 0
    assert body["page_count"] == 0
    assert body["missing"] == ["Sol Ring [C21 263]"]


def test_pdf_generate_returns_real_pdf_file(client: TestClient, tmp_path: Path) -> None:
    """The concrete fix for the reported download bug: a real file
    response with correct headers and non-trivial byte length, instead
    of Streamlit's download-attribute-based st.download_button."""
    db_path = os.environ["PROXY_SCALER_DB_PATH"]
    _write_gallery_item(tmp_path, db_path, "tag-a")

    resp = client.post("/api/pdf", json=_pdf_layout_body(project_name="Deck"))
    assert resp.status_code == 200
    assert resp.headers["content-type"] == "application/pdf"
    assert "attachment" in resp.headers["content-disposition"]
    assert "Deck.pdf" in resp.headers["content-disposition"]
    assert len(resp.content) > 100  # a real, non-trivial PDF, not an empty stub


def test_pdf_preview_page_no_entries_is_400(client: TestClient) -> None:
    resp = client.post("/api/pdf/preview/page", json=_pdf_layout_body(entries=[]))
    assert resp.status_code == 400


def test_pdf_preview_page_returns_page_one_with_thumbnail(
    client: TestClient, tmp_path: Path
) -> None:
    db_path = os.environ["PROXY_SCALER_DB_PATH"]
    _write_gallery_item(tmp_path, db_path, "tag-a")

    resp = client.post("/api/pdf/preview/page", json=_pdf_layout_body(cols=3, rows=3))
    assert resp.status_code == 200
    body = resp.json()

    assert body["cols"] == 3
    assert body["rows"] == 3
    assert body["page_count"] == 1
    [slot] = body["slots"]
    assert slot["card_name"] == "Sol Ring"
    assert slot["thumbnail_data_url"] is not None
    assert slot["thumbnail_data_url"].startswith("data:image/jpeg;base64,")


def test_pdf_preview_page_empty_when_nothing_generated(client: TestClient) -> None:
    resp = client.post("/api/pdf/preview/page", json=_pdf_layout_body())
    assert resp.status_code == 200
    body = resp.json()
    assert body["page_count"] == 0
    assert body["slots"] == []


def test_pdf_html_returns_real_pdf_file(client: TestClient, tmp_path: Path) -> None:
    pytest.importorskip("weasyprint")
    db_path = os.environ["PROXY_SCALER_DB_PATH"]
    _write_gallery_item(tmp_path, db_path, "tag-a")

    resp = client.post("/api/pdf/html", json=_pdf_layout_body(project_name="Deck"))
    assert resp.status_code == 200
    assert resp.headers["content-type"] == "application/pdf"
    assert "Deck-html.pdf" in resp.headers["content-disposition"]
    assert len(resp.content) > 100


def test_pdf_html_503_when_weasyprint_unavailable(client: TestClient, tmp_path: Path) -> None:
    from proxy_scaler.pdf_html import WeasyPrintUnavailable

    db_path = os.environ["PROXY_SCALER_DB_PATH"]
    _write_gallery_item(tmp_path, db_path, "tag-a")

    # Patched where the route resolves it (`from ... import build_pdf_html`
    # binds a new name in routers.pdf's own namespace) — patching
    # proxy_scaler.pdf_html.build_pdf_html instead wouldn't affect the
    # already-imported reference the route actually calls.
    with patch(
        "proxy_scaler.api.routers.pdf.build_pdf_html",
        side_effect=WeasyPrintUnavailable("nope"),
    ):
        resp = client.post("/api/pdf/html", json=_pdf_layout_body())
    assert resp.status_code == 503


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


def test_clear_generated_data_with_project_tag_clears_records_too(
    client: TestClient, tmp_path: Path
) -> None:
    """The bug this guards against: deleting the files but leaving the
    gallery/task rows behind, so the UI kept reporting every card as
    already generated after "Delete all generated images & cache". A
    still-pending task for the same project must survive, though — it
    hasn't written a file yet, so there's nothing for the delete to have
    invalidated."""
    db_path = os.environ["PROXY_SCALER_DB_PATH"]
    _write_gallery_item(tmp_path, db_path, "tag-a")

    done_id = db.enqueue_task(
        "tag-a",
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
        output_dir=str(tmp_path),
        cache_dir=str(tmp_path),
        weights_dir=str(tmp_path),
        db_path=db_path,
    )
    db.mark_task_done(done_id, db_path=db_path)
    pending_id = db.enqueue_task(
        "tag-a",
        scryfall_id="sol-id",
        face_index=None,
        face_label=None,
        face_name="Sol Ring",
        card_name="Sol Ring",
        set_code="c21",
        collector_number="263",
        png_url="https://example.com/sol.png",
        dpi=1200,
        model="swinir",
        output_dir=str(tmp_path),
        cache_dir=str(tmp_path),
        weights_dir=str(tmp_path),
        db_path=db_path,
    )

    assert db.list_gallery_items("tag-a", db_path=db_path) != []
    assert {t.id for t in db.list_tasks(project_tag="tag-a", db_path=db_path)} == {
        done_id,
        pending_id,
    }

    output_dir = tmp_path.parent / "out2"
    cache_dir = tmp_path.parent / "cache2"
    output_dir.mkdir()
    cache_dir.mkdir()

    resp = client.post(
        "/api/generated-data/clear",
        json={"output_dir": str(output_dir), "cache_dir": str(cache_dir), "project_tag": "tag-a"},
    )
    assert resp.status_code == 200

    assert db.list_gallery_items("tag-a", db_path=db_path) == []
    remaining = {t.id for t in db.list_tasks(project_tag="tag-a", db_path=db_path)}
    assert remaining == {pending_id}

    resp = client.get("/api/gallery", params={"project_tag": "tag-a"})
    assert resp.json() == []


# --- PDF render jobs -------------------------------------------------------


def _await_pdf_job(client: TestClient, job_id: str, *, timeout_s: float = 20.0) -> dict:
    """Poll a render job to a terminal state. The render runs on its own
    thread, so tests have to wait the way the real client does."""
    deadline = time.monotonic() + timeout_s
    while time.monotonic() < deadline:
        resp = client.get(f"/api/pdf/jobs/{job_id}")
        assert resp.status_code == 200
        body = resp.json()
        if body["status"] != "rendering":
            return body
        time.sleep(0.02)
    raise AssertionError(f"PDF job {job_id} never finished")


def test_pdf_job_lifecycle_start_poll_fetch(client: TestClient, tmp_path: Path) -> None:
    """The full path the desktop client drives: start a job, poll it to
    done, then fetch the bytes from a plain GET (which is what lets Rust
    download it without the PDF ever entering the webview)."""
    pdf_jobs._reset_for_tests()
    db_path = os.environ["PROXY_SCALER_DB_PATH"]
    _write_gallery_item(tmp_path, db_path, "tag-a")

    resp = client.post("/api/pdf/jobs", json=_pdf_layout_body(project_name="Deck"))
    assert resp.status_code == 202
    started = resp.json()
    job_id = started["job_id"]
    assert started["total"] == 1  # one unique source image

    final = _await_pdf_job(client, job_id)
    assert final["status"] == "done"
    assert final["completed"] == final["total"] == 1
    assert final["error"] is None

    resp = client.get(f"/api/pdf/jobs/{job_id}/result")
    assert resp.status_code == 200
    assert resp.headers["content-type"] == "application/pdf"
    assert "Deck.pdf" in resp.headers["content-disposition"]
    assert resp.content.startswith(b"%PDF")

    # Evicted on fetch — its bytes must not stay pinned in memory.
    assert client.get(f"/api/pdf/jobs/{job_id}").status_code == 404
    assert client.get(f"/api/pdf/jobs/{job_id}/result").status_code == 404


def test_pdf_job_validation_errors_surface_on_start(client: TestClient) -> None:
    """_prepare runs synchronously in the start handler, so an unprintable
    request fails immediately rather than becoming a job the user waits on
    only to watch it fail."""
    pdf_jobs._reset_for_tests()
    assert client.post("/api/pdf/jobs", json=_pdf_layout_body(entries=[])).status_code == 400
    # Nothing generated for this tag -> no pages -> still a start-time 400.
    assert client.post("/api/pdf/jobs", json=_pdf_layout_body()).status_code == 400
    assert pdf_jobs.active_count() == 0


def test_pdf_job_result_409s_before_the_render_finishes(client: TestClient) -> None:
    """A client that races ahead of `done` gets a clear 409, and crucially
    the job is left intact rather than evicted."""
    pdf_jobs._reset_for_tests()
    job = pdf_jobs.create_job(filename="deck.pdf", total=3)
    resp = client.get(f"/api/pdf/jobs/{job.id}/result")
    assert resp.status_code == 409
    assert pdf_jobs.get(job.id) is not None


def test_pdf_job_refuses_a_second_concurrent_render(
    client: TestClient, tmp_path: Path
) -> None:
    """Each finished job pins a whole PDF in memory until fetched, so only
    one render is allowed in flight. Needs a genuinely printable request,
    since _prepare's own validation runs first and would 400 instead."""
    pdf_jobs._reset_for_tests()
    db_path = os.environ["PROXY_SCALER_DB_PATH"]
    _write_gallery_item(tmp_path, db_path, "tag-a")

    pdf_jobs.create_job(filename="busy.pdf", total=1)  # stands in for a live render
    resp = client.post("/api/pdf/jobs", json=_pdf_layout_body())
    assert resp.status_code == 409


def test_pdf_job_cancel_marks_it_canceled(client: TestClient) -> None:
    pdf_jobs._reset_for_tests()
    job = pdf_jobs.create_job(filename="deck.pdf", total=5)

    assert client.post(f"/api/pdf/jobs/{job.id}/cancel").status_code == 204
    assert pdf_jobs.is_cancel_requested(job.id) is True

    assert client.post("/api/pdf/jobs/nope/cancel").status_code == 404


def test_pdf_job_status_404s_for_unknown_id(client: TestClient) -> None:
    pdf_jobs._reset_for_tests()
    assert client.get("/api/pdf/jobs/nope").status_code == 404
