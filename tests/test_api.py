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

import base64
import io
import os
import time
from contextlib import ExitStack
from datetime import date
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
    # Not initialized on purpose — card-corpus endpoints must behave as
    # "no corpus imported yet" by default; tests that need one seed it.
    monkeypatch.setenv("PROXY_SCALER_CARD_DB_PATH", str(tmp_path / "cards.db"))
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
        model="ultrasharp_v2",
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
        model="ultrasharp_v2",
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


def _write_gallery_item_with_transparent_corner(
    tmp_path: Path, db_path: Path, project_tag: str
) -> dict:
    """Same as _write_gallery_item, but the source PNG has a real
    transparent rounded-corner arc (like an actual Scryfall card image)
    instead of a flat opaque rectangle — needed to exercise
    flatten_corner_alpha's behavior end-to-end through the preview API."""
    img_path = tmp_path / "sol_ring.png"
    img = Image.new("RGBA", (200, 280), (10, 20, 30, 255))
    px = img.load()
    for y in range(12):
        for x in range(12 - y):
            px[x, y] = (0, 0, 0, 0)  # transparent top-left corner arc
    img.save(img_path, format="PNG")
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
        model="ultrasharp_v2",
    )
    task = db.TaskRow(
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
        model="ultrasharp_v2",
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
    db.upsert_gallery_item_for_task(task, result, db_path=db_path)
    [item] = db.list_gallery_items(project_tag, db_path=db_path)
    return item


def _decode_data_url(data_url: str) -> Image.Image:
    assert data_url.startswith("data:image/jpeg;base64,")
    raw = base64.b64decode(data_url.split(",", 1)[1])
    return Image.open(io.BytesIO(raw)).convert("RGB")


def test_pdf_preview_page_thumbnail_has_no_white_corner_hole(
    client: TestClient, tmp_path: Path
) -> None:
    """Regression guard: the preview grid composites thumbnails
    edge-to-edge, so a rounded-corner arc left transparent (and flattened
    to plain white) shows up as a visible white notch at every card
    corner. The corner pixel must be flattened to the card's own edge
    color, not white — see pipeline.py::_generate_original_thumbnail."""
    db_path = os.environ["PROXY_SCALER_DB_PATH"]
    _write_gallery_item_with_transparent_corner(tmp_path, db_path, "tag-a")

    resp = client.post("/api/pdf/preview/page", json=_pdf_layout_body(cols=1, rows=1))
    assert resp.status_code == 200
    [slot] = resp.json()["slots"]
    img = _decode_data_url(slot["thumbnail_data_url"])
    r, g, b = img.getpixel((0, 0))
    # Nowhere near white (255,255,255) — should read close to the card's
    # own body color (10, 20, 30), not the old white-background flatten.
    assert (r, g, b) != (255, 255, 255)
    assert r < 100 and g < 100 and b < 100


def test_pdf_preview_page_bleed_grows_the_actual_image(
    client: TestClient, tmp_path: Path
) -> None:
    """Regression guard: raising bleed_mm must genuinely edge-extend the
    preview art (matching build_pdf's real add_bleed step), not just grow
    the CSS box the same static image gets stretched into — the reported
    bug was that increasing bleed only made cards look bigger with no
    real bleed content."""
    db_path = os.environ["PROXY_SCALER_DB_PATH"]
    _write_gallery_item(tmp_path, db_path, "tag-a")

    small = client.post(
        "/api/pdf/preview/page", json=_pdf_layout_body(cols=1, rows=1, bleed_mm=0.5)
    )
    large = client.post(
        "/api/pdf/preview/page", json=_pdf_layout_body(cols=1, rows=1, bleed_mm=6.0)
    )
    assert small.status_code == 200 and large.status_code == 200
    small_img = _decode_data_url(small.json()["slots"][0]["thumbnail_data_url"])
    large_img = _decode_data_url(large.json()["slots"][0]["thumbnail_data_url"])
    assert large_img.width > small_img.width
    assert large_img.height > small_img.height


def test_health(client: TestClient) -> None:
    resp = client.get("/api/health")
    assert resp.status_code == 200
    assert resp.json() == {"status": "ok"}


def test_version_reports_the_package_version(client: TestClient) -> None:
    """The client's drift warning compares its own version against this —
    it must track proxy_scaler.__version__ (the copy set-version.py keeps
    in lockstep with pyproject), not a second hand-maintained string."""
    import proxy_scaler

    resp = client.get("/api/version")
    assert resp.status_code == 200
    assert resp.json() == {"version": proxy_scaler.__version__}


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
    # `kind` stays exactly "gpu" for every GPU backend — its values are
    # persisted into on-disk cache sidecars, so they can't be widened.
    # `backend` is the additive field the client uses to tell them apart.
    assert resp.json() == {"kind": "gpu", "backend": "cuda"}


def test_device_reports_mps_backend_on_apple_silicon(client: TestClient) -> None:
    """Apple Silicon must be distinguishable from CUDA in the response:
    both are `kind: "gpu"`, but the client picks a much lighter default
    model for MPS (see ProjectContext.tsx::recommendedDefaultModel)."""
    with (
        patch("torch.cuda.is_available", return_value=False),
        patch("torch.backends.mps.is_available", return_value=True),
    ):
        resp = client.get("/api/device")
    assert resp.status_code == 200
    assert resp.json() == {"kind": "gpu", "backend": "mps"}


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
    assert resp.json() == {"kind": "cpu", "backend": "cpu"}


def test_get_paths_resolves_against_server_cwd(
    client: TestClient, tmp_path: Path, monkeypatch
) -> None:
    # The relative defaults ("output" etc.) are meaningful only relative
    # to the serving process's cwd — that resolution contract is the whole
    # point of the endpoint, so pin it by moving the cwd.
    monkeypatch.chdir(tmp_path)
    resp = client.get("/api/paths")
    assert resp.status_code == 200
    assert resp.json() == {
        "output_dir": str(tmp_path / "output"),
        "cache_dir": str(tmp_path / "imgcache"),
        "weights_dir": str(tmp_path / "weights"),
    }


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
            "model": "ultrasharp_v2",
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
            "model": "ultrasharp_v2",
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
            "model": "ultrasharp_v2",
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
            "model": "ultrasharp_v2",
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


def test_cancel_all_tasks_cancels_only_pending(client: TestClient, tmp_path: Path) -> None:
    db_path = os.environ["PROXY_SCALER_DB_PATH"]
    resp = client.post(
        "/api/generate",
        json={
            "project_tag": "tag-a",
            "entries": [_sol_ring_entry()],
            "model": "ultrasharp_v2",
            "dpi_targets": [800],
            "skip_existing": False,
            "output_dir": str(tmp_path / "out"),
            "cache_dir": str(tmp_path / "cache"),
            "weights_dir": str(tmp_path / "weights"),
        },
    )
    pending_id = resp.json()["task_ids"][0]
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
        model="ultrasharp_v2",
        output_dir=str(tmp_path),
        cache_dir=str(tmp_path),
        weights_dir=str(tmp_path),
        db_path=db_path,
    )
    db.mark_task_done(done_id, db_path=db_path)

    resp = client.post("/api/tasks/cancel-all")
    assert resp.status_code == 200
    assert resp.json() == {"canceled": 1}
    assert client.get(f"/api/tasks/{pending_id}").json()["status"] == "canceled"
    assert client.get(f"/api/tasks/{done_id}").json()["status"] == "done"

    # No-op on a second call — nothing left pending.
    resp = client.post("/api/tasks/cancel-all")
    assert resp.json() == {"canceled": 0}


def test_cancel_all_include_running_requires_held_worker(
    client: TestClient, tmp_path: Path
) -> None:
    """include_running is only safe while the worker is held (a held
    worker has claimed nothing, so 'running' rows are provable orphans);
    against a possibly-live worker it must be refused, or the worker would
    overwrite 'canceled' with 'done'/'failed' when the task finishes."""
    db_path = os.environ["PROXY_SCALER_DB_PATH"]
    orphan_id = _enqueue_for_tag(tmp_path, db_path, "tag-a")
    assert db.claim_next_task(db_path=db_path).id == orphan_id  # -> 'running'

    resp = client.post("/api/tasks/cancel-all", params={"include_running": True})
    assert resp.status_code == 409
    assert client.get(f"/api/tasks/{orphan_id}").json()["status"] == "running"

    db.set_worker_hold(True, db_path=db_path)
    resp = client.post("/api/tasks/cancel-all", params={"include_running": True})
    assert resp.status_code == 200
    assert resp.json() == {"canceled": 1}
    task = client.get(f"/api/tasks/{orphan_id}").json()
    assert task["status"] == "canceled"
    assert task["started_at"] is None


def _enqueue_for_tag(tmp_path: Path, db_path: str, project_tag: str, **overrides) -> int:
    """Queue one pending task straight into the DB, skipping /api/generate
    (and its Scryfall round trip) — for tests that only care about the
    task rows themselves."""
    kwargs = dict(
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
        output_dir=str(tmp_path),
        cache_dir=str(tmp_path),
        weights_dir=str(tmp_path),
        db_path=db_path,
    )
    kwargs.update(overrides)
    return db.enqueue_task(project_tag, **kwargs)


def _enqueue_and_fail(tmp_path: Path, db_path: str, *, model: str, dpi: int) -> int:
    task_id = _enqueue_for_tag(tmp_path, db_path, "tag-a", model=model, dpi=dpi)
    db.mark_task_failed(task_id, "disk full", db_path=db_path)
    return task_id


def test_task_retry_requeues_failed_task(client: TestClient, tmp_path: Path) -> None:
    db_path = os.environ["PROXY_SCALER_DB_PATH"]
    task_id = _enqueue_and_fail(tmp_path, db_path, model="ultrasharp_v2", dpi=800)

    resp = client.post(f"/api/tasks/{task_id}/retry")
    assert resp.status_code == 200
    assert resp.json() == {"retried": True}
    task = client.get(f"/api/tasks/{task_id}").json()
    assert task["status"] == "pending"
    assert task["error"] is None

    # No-op — task is pending now, not failed.
    resp = client.post(f"/api/tasks/{task_id}/retry")
    assert resp.json() == {"retried": False}


def test_retry_all_only_matches_project_model_and_dpi(client: TestClient, tmp_path: Path) -> None:
    db_path = os.environ["PROXY_SCALER_DB_PATH"]
    matching_id = _enqueue_and_fail(tmp_path, db_path, model="ultrasharp_v2", dpi=800)
    wrong_dpi_id = _enqueue_and_fail(tmp_path, db_path, model="ultrasharp_v2", dpi=1200)
    wrong_model_id = _enqueue_and_fail(tmp_path, db_path, model="illustrationjanai", dpi=800)

    resp = client.post(
        "/api/tasks/retry-all",
        params={"project_tag": "tag-a", "model": "ultrasharp_v2", "dpi": [800]},
    )
    assert resp.status_code == 200
    assert resp.json() == {"retried": 1}
    assert client.get(f"/api/tasks/{matching_id}").json()["status"] == "pending"
    assert client.get(f"/api/tasks/{wrong_dpi_id}").json()["status"] == "failed"
    assert client.get(f"/api/tasks/{wrong_model_id}").json()["status"] == "failed"


def test_worker_status_not_running(client: TestClient) -> None:
    resp = client.get("/api/worker/status")
    assert resp.status_code == 200
    assert resp.json() == {"running": False, "held": False}


def test_worker_status_reports_hold_and_release_clears_it(client: TestClient) -> None:
    db_path = os.environ["PROXY_SCALER_DB_PATH"]
    db.set_worker_hold(True, db_path=db_path)

    resp = client.get("/api/worker/status")
    assert resp.json()["held"] is True

    resp = client.post("/api/worker/release")
    assert resp.status_code == 200
    assert resp.json() == {"released": True}
    assert client.get("/api/worker/status").json()["held"] is False

    # Idempotent — a repeat release just reports there was nothing held.
    resp = client.post("/api/worker/release")
    assert resp.json() == {"released": False}


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
            # Registry rows are global (shared via memberships), so the
            # client says which project the regeneration belongs to.
            "project_tag": "tag-a",
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
    assert new_task["model"] == "ultrasharp_v2"
    assert new_task["project_tag"] == "tag-a"


def test_regenerate_gallery_item_not_found(client: TestClient, tmp_path: Path) -> None:
    resp = client.post(
        "/api/gallery/999/regenerate",
        json={
            "project_tag": "tag-a",
            "output_dir": str(tmp_path),
            "cache_dir": str(tmp_path),
            "weights_dir": str(tmp_path),
        },
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


def test_pdf_generate_falls_back_to_a_dated_filename(client: TestClient, tmp_path: Path) -> None:
    """An Unnamed Project has no project_name, and the opaque project_tag
    is no name to hand a user — the download would arrive as a 32-char hex
    string. The dated fallback is shared with the client's save-dialog
    default (PdfPage.tsx) so the two agree."""
    db_path = os.environ["PROXY_SCALER_DB_PATH"]
    _write_gallery_item(tmp_path, db_path, "tag-a")

    resp = client.post("/api/pdf", json=_pdf_layout_body(project_name=""))
    assert resp.status_code == 200
    expected = f"proxy-scaler-{date.today().isoformat()}.pdf"
    assert f'filename="{expected}"' in resp.headers["content-disposition"]
    assert "tag-a" not in resp.headers["content-disposition"]


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
        model="ultrasharp_v2",
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
        model="ultrasharp_v2",
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


# --- Tag discard -----------------------------------------------------------


def test_discard_tag_cancels_pending_and_clears_records_for_that_tag_only(
    client: TestClient, tmp_path: Path
) -> None:
    """Discard means "this session was thrown away": the tag's pending
    work is canceled and its records go, while another Project's tasks and
    gallery rows — and this tag's already-running task, which can't be
    cancelled (spec §8) — are untouched."""
    db_path = os.environ["PROXY_SCALER_DB_PATH"]
    running_id = _enqueue_for_tag(tmp_path, db_path, "tag-a", collector_number="1")
    db.claim_next_task(db_path=db_path)  # the only pending task so far -> running
    pending_id = _enqueue_for_tag(tmp_path, db_path, "tag-a", collector_number="2")
    done_id = _enqueue_for_tag(tmp_path, db_path, "tag-a", collector_number="3")
    db.mark_task_done(done_id, db_path=db_path)
    failed_id = _enqueue_for_tag(tmp_path, db_path, "tag-a", collector_number="4")
    db.mark_task_failed(failed_id, "boom", db_path=db_path)
    other_pending_id = _enqueue_for_tag(tmp_path, db_path, "tag-b", collector_number="5")
    other_done_id = _enqueue_for_tag(tmp_path, db_path, "tag-b", collector_number="6")
    db.mark_task_done(other_done_id, db_path=db_path)
    _write_gallery_item(tmp_path, db_path, "tag-a")
    _write_gallery_item(tmp_path, db_path, "tag-b")

    resp = client.post("/api/tags/tag-a/discard")
    assert resp.status_code == 200
    assert resp.json() == {"canceled": 1}

    # Canceling before clearing (not the other way round) is what stops the
    # worker claiming a pending task in between — the clear then sweeps the
    # freshly-canceled row away with the rest of the tag's history, so the
    # running task is all that's left of this tag.
    assert db.get_task(pending_id, db_path=db_path) is None
    assert db.get_task(done_id, db_path=db_path) is None
    assert db.get_task(failed_id, db_path=db_path) is None
    assert [t.id for t in db.list_tasks(project_tag="tag-a", db_path=db_path)] == [running_id]
    assert db.get_task(running_id, db_path=db_path).status == "running"
    assert db.list_gallery_items("tag-a", db_path=db_path) == []

    assert db.get_task(other_pending_id, db_path=db_path).status == "pending"
    assert db.get_task(other_done_id, db_path=db_path).status == "done"
    assert db.list_gallery_items("tag-b", db_path=db_path) != []


def test_discard_tag_deletes_no_files(client: TestClient, tmp_path: Path) -> None:
    """The one thing discard must never do: output filenames carry no tag,
    so the images are shared across every Project and deleting them would
    strand other Projects' gallery rows."""
    db_path = os.environ["PROXY_SCALER_DB_PATH"]
    output_dir = tmp_path / "out"
    cache_dir = tmp_path / "cache"
    output_dir.mkdir()
    cache_dir.mkdir()
    (output_dir / "a.png").write_bytes(b"x")
    (cache_dir / "b.png").write_bytes(b"y")
    # The route takes no directories, so these are reachable only the way a
    # file-deleting implementation would have to find them: off the tag's
    # own task rows. Recording them here is what stops this test passing
    # vacuously against a route that did wipe the dirs it can see.
    _enqueue_for_tag(
        tmp_path, db_path, "tag-a", output_dir=str(output_dir), cache_dir=str(cache_dir)
    )
    item = _write_gallery_item(tmp_path, db_path, "tag-a")

    resp = client.post("/api/tags/tag-a/discard")
    assert resp.status_code == 200

    assert (output_dir / "a.png").exists()
    assert (cache_dir / "b.png").exists()
    assert Path(item["out_path"]).exists()


def test_discard_tag_with_no_records_is_a_no_op(client: TestClient) -> None:
    resp = client.post("/api/tags/unknown-tag/discard")
    assert resp.status_code == 200
    assert resp.json() == {"canceled": 0}


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


def test_pdf_job_falls_back_to_a_dated_filename(client: TestClient, tmp_path: Path) -> None:
    """The job route is the one the desktop client actually drives, so it
    needs the same dated fallback as the synchronous route above."""
    pdf_jobs._reset_for_tests()
    db_path = os.environ["PROXY_SCALER_DB_PATH"]
    _write_gallery_item(tmp_path, db_path, "tag-a")

    resp = client.post("/api/pdf/jobs", json=_pdf_layout_body(project_name=""))
    assert resp.status_code == 202
    job_id = resp.json()["job_id"]
    assert _await_pdf_job(client, job_id)["status"] == "done"

    resp = client.get(f"/api/pdf/jobs/{job_id}/result")
    assert resp.status_code == 200
    expected = f"proxy-scaler-{date.today().isoformat()}.pdf"
    assert f'filename="{expected}"' in resp.headers["content-disposition"]


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


def test_adopt_gallery_surfaces_another_projects_images(
    client: TestClient, tmp_path: Path
) -> None:
    """Importing a deck whose cards were already generated under another
    project should show those images without a Generate request."""
    db_path = os.environ["PROXY_SCALER_DB_PATH"]
    _write_gallery_item(tmp_path, db_path, "tag-a")

    resp = client.post(
        "/api/gallery/adopt",
        json={"project_tag": "tag-b", "entries": [_sol_ring_entry()]},
    )
    assert resp.status_code == 200
    assert resp.json() == {"adopted": 1, "pruned": 0}

    gallery = client.get("/api/gallery", params={"project_tag": "tag-b"}).json()
    assert [g["card_name"] for g in gallery] == ["Sol Ring"]

    # Idempotent — a re-import adopts nothing new.
    resp = client.post(
        "/api/gallery/adopt",
        json={"project_tag": "tag-b", "entries": [_sol_ring_entry()]},
    )
    assert resp.json() == {"adopted": 0, "pruned": 0}


def test_adopt_gallery_scans_output_dir_for_rowless_files(
    client: TestClient, tmp_path: Path
) -> None:
    """Images on disk with no registry row (pre-registry or CLI-produced)
    adopt from their filenames when output_dir is sent — legacy-named
    files resolve to a real scryfall_id through the card corpus."""
    _seed_card_db(
        id="sol-id",
        oracle_id="oracle-sol",
        name="Sol Ring",
        set="c21",
        set_name="Commander 2021",
        collector_number="263",
        released_at="2021-04-23",
    )
    out = tmp_path / "output"
    out.mkdir()
    (out / "Sol_Ring-C21-263-illustrationjanai-1200dpi.png").write_bytes(b"png")

    resp = client.post(
        "/api/gallery/adopt",
        json={
            "project_tag": "tag-x",
            "entries": [_sol_ring_entry()],
            "output_dir": str(out),
        },
    )
    assert resp.status_code == 200
    assert resp.json() == {"adopted": 1, "pruned": 0}

    gallery = client.get("/api/gallery", params={"project_tag": "tag-x"}).json()
    assert [(g["scryfall_id"], g["model"], g["dpi"]) for g in gallery] == [
        ("sol-id", "illustrationjanai", 1200)
    ]


def test_adopt_gallery_scan_skips_unresolvable_files_without_corpus(
    client: TestClient, tmp_path: Path
) -> None:
    """A legacy-named file whose printing can't be resolved (no corpus
    imported at all here) is skipped outright — the registry never holds
    sentinel scryfall_ids, and the file simply waits for a corpus import
    or a real generation to register it."""
    out = tmp_path / "output"
    out.mkdir()
    (out / "Sol_Ring-C21-263-illustrationjanai-1200dpi.png").write_bytes(b"png")

    resp = client.post(
        "/api/gallery/adopt",
        json={
            "project_tag": "tag-x",
            "entries": [_sol_ring_entry()],
            "output_dir": str(out),
        },
    )
    assert resp.status_code == 200
    assert resp.json() == {"adopted": 0, "pruned": 0}
    assert client.get("/api/gallery", params={"project_tag": "tag-x"}).json() == []


def test_gallery_status_reports_cross_project_existence(
    client: TestClient, tmp_path: Path
) -> None:
    """The picker's coverage lookup: an image generated under any project
    counts (the request carries no project_tag at all), ids with nothing
    generated are simply absent, and non-matching model/dpi filter out."""
    db_path = os.environ["PROXY_SCALER_DB_PATH"]
    _write_gallery_item(tmp_path, db_path, "tag-someone-else")

    resp = client.post(
        "/api/gallery/status",
        json={
            "scryfall_ids": ["sol-id", "unknown-id"],
            "model": "ultrasharp_v2",
            "dpis": [800, 1200],
        },
    )
    assert resp.status_code == 200
    assert resp.json() == {
        "statuses": {"sol-id": [{"dpi": 800, "face_index": None}]}
    }

    # Another model: nothing generated there.
    resp = client.post(
        "/api/gallery/status",
        json={"scryfall_ids": ["sol-id"], "model": "illustrationjanai", "dpis": [800]},
    )
    assert resp.json() == {"statuses": {}}


def test_adopt_gallery_prunes_stale_records_on_load(
    client: TestClient, tmp_path: Path
) -> None:
    """Output files are shared and tag-less, so another project (or a
    manual delete) can remove them out from under this project's gallery
    rows — the reconcile on project load must drop those rows AND the
    done-task records that would re-assert the same green badge via the
    client's task-status fallback."""
    db_path = os.environ["PROXY_SCALER_DB_PATH"]
    item = _write_gallery_item(tmp_path, db_path, "tag-stale")
    task_id = _enqueue_and_fail(tmp_path, db_path, model="ultrasharp_v2", dpi=800)
    # Promote the failed fixture task to done under the stale tag so it
    # exercises the task-record half of the prune.
    import sqlite3

    with sqlite3.connect(db_path) as conn:
        conn.execute(
            "UPDATE generation_tasks SET status = 'done', project_tag = 'tag-stale' WHERE id = ?",
            (task_id,),
        )

    # Delete the file the row points at.
    Path(item["out_path"]).unlink()

    resp = client.post(
        "/api/gallery/adopt",
        json={"project_tag": "tag-stale", "entries": [_sol_ring_entry()]},
    )
    assert resp.status_code == 200
    body = resp.json()
    assert body["pruned"] == 2  # the gallery row + the done task record
    assert body["adopted"] == 0  # nothing left anywhere to adopt from

    assert client.get("/api/gallery", params={"project_tag": "tag-stale"}).json() == []
    tasks = client.get("/api/tasks", params={"project_tag": "tag-stale"}).json()
    assert tasks == []


# ---------------------------------------------------------------------------
# Card corpus endpoints (routers/cards.py)


def _seed_card_db(**card_overrides) -> None:
    """Initialize the tmp corpus (PROXY_SCALER_CARD_DB_PATH, set by the
    client fixture) with one card plus finished import meta."""
    from proxy_scaler import carddb

    path = os.environ["PROXY_SCALER_CARD_DB_PATH"]
    card = {
        "id": "bolt-lea",
        "oracle_id": "oracle-bolt",
        "name": "Lightning Bolt",
        "lang": "en",
        "set": "lea",
        "set_name": "Limited Edition Alpha",
        "collector_number": "161",
        "released_at": "1993-08-05",
        "layout": "normal",
        "digital": False,
        "image_status": "highres_scan",
        "highres_image": True,
        "image_uris": {"png": "https://img.example/bolt.png"},
    }
    card.update(card_overrides)
    carddb.init_card_db(path)
    conn = carddb.connect(path)
    try:
        carddb.upsert_cards(conn, [card])
        carddb.write_import_meta(
            conn, dataset_type="default_cards", dataset_updated_at="2026-08-19T00:00:00Z"
        )
    finally:
        conn.close()


@pytest.fixture(autouse=True)
def _clean_card_jobs():
    from proxy_scaler import card_jobs

    card_jobs._reset_for_tests()
    yield
    card_jobs._reset_for_tests()


def test_card_status_no_corpus_offline(client: TestClient, monkeypatch) -> None:
    from proxy_scaler import card_import

    monkeypatch.setattr(
        card_import,
        "fetch_bulk_catalog",
        lambda *a, **k: (_ for _ in ()).throw(RuntimeError("offline")),
    )
    body = client.get("/api/cards/status").json()
    assert body["local"] is None
    assert body["remote"] is None  # unreachable catalog is "unknown", not a 5xx
    assert body["import_running"] is False


def test_card_status_reports_local_and_remote(client: TestClient, monkeypatch) -> None:
    from proxy_scaler import card_import
    from proxy_scaler.card_import import BulkDatasetInfo

    _seed_card_db()
    monkeypatch.setattr(
        card_import,
        "fetch_bulk_catalog",
        lambda *a, **k: {
            "default_cards": BulkDatasetInfo(
                dataset="default_cards",
                updated_at="2026-08-20T00:00:00Z",
                download_uri="https://data.example/d.jsonl.gz",
                compressed_size=77,
            )
        },
    )
    body = client.get("/api/cards/status").json()
    assert body["local"]["dataset_type"] == "default_cards"
    assert body["local"]["card_count"] == 1
    assert body["remote"]["default_cards"]["updated_at"] == "2026-08-20T00:00:00Z"


def test_card_import_validates_dataset(client: TestClient) -> None:
    resp = client.post("/api/cards/import", json={"dataset": "oracle_cards"})
    assert resp.status_code == 422


def test_card_import_refuses_concurrent_jobs(client: TestClient, monkeypatch) -> None:
    from proxy_scaler import card_import, card_jobs

    # Don't actually run anything on the spawned thread.
    monkeypatch.setattr(card_import, "run_import", lambda *a, **k: None)
    first = client.post("/api/cards/import", json={"dataset": "default_cards"})
    assert first.status_code == 202
    job_id = first.json()["job_id"]
    assert card_jobs.get(job_id) is not None
    second = client.post("/api/cards/import", json={"dataset": "default_cards"})
    assert second.status_code == 409


def test_card_import_status_and_cancel_roundtrip(client: TestClient, monkeypatch) -> None:
    from proxy_scaler import card_import, card_jobs

    monkeypatch.setattr(card_import, "run_import", lambda *a, **k: None)
    job_id = client.post("/api/cards/import", json={"dataset": "all_cards"}).json()[
        "job_id"
    ]
    body = client.get(f"/api/cards/import/{job_id}").json()
    assert body["status"] == "running"
    assert body["dataset"] == "all_cards"
    assert client.post(f"/api/cards/import/{job_id}/cancel").status_code == 204
    assert card_jobs.is_cancel_requested(job_id)
    assert client.get("/api/cards/import/nope").status_code == 404
    assert client.post("/api/cards/import/nope/cancel").status_code == 404


def test_card_languages_full_list_regardless_of_corpus(client: TestClient) -> None:
    """The dropdown expresses what the user wants, not what's downloaded —
    the endpoint serves the full Scryfall language list (English first)
    whether or not any corpus exists."""
    from proxy_scaler.scryfall import SCRYFALL_LANGUAGES

    expected = {"languages": list(SCRYFALL_LANGUAGES)}
    assert client.get("/api/cards/languages").json() == expected
    assert expected["languages"][0] == "en"
    _seed_card_db()
    assert client.get("/api/cards/languages").json() == expected


def test_card_variants_requires_corpus(client: TestClient) -> None:
    resp = client.get("/api/cards/variants", params={"name": "Lightning Bolt"})
    assert resp.status_code == 404
    assert "import" in resp.json()["detail"].lower()


def test_card_variants_by_each_anchor(client: TestClient) -> None:
    _seed_card_db()
    for params in (
        {"scryfall_id": "bolt-lea"},
        {"set_code": "LEA", "collector_number": "161"},
        {"name": "lightning bolt"},
    ):
        body = client.get("/api/cards/variants", params=params).json()
        assert body["anchor"]["scryfall_id"] == "bolt-lea"
        assert body["anchor"]["face_count"] == 1
        assert body["total"] == 1
        assert body["variants"][0]["set_code"] == "lea"
        assert body["variants"][0]["face_count"] == 1


def test_card_variants_face_count_for_dfc(client: TestClient) -> None:
    """A transform card with per-face images reports face_count 2 — the
    picker's coverage math needs it to tell a half-generated DFC apart
    from a complete one."""
    _seed_card_db(
        id="dion-fin",
        oracle_id="oracle-dion",
        name="Dion, Bahamut's Dominant // Bahamut, Warden of Light",
        set="fin",
        set_name="Final Fantasy",
        collector_number="376",
        layout="transform",
        card_faces=[
            {"name": "Dion, Bahamut's Dominant", "image_uris": {"png": "https://img.example/f.png"}},
            {"name": "Bahamut, Warden of Light", "image_uris": {"png": "https://img.example/b.png"}},
        ],
    )
    body = client.get(
        "/api/cards/variants", params={"scryfall_id": "dion-fin"}
    ).json()
    assert body["anchor"]["face_count"] == 2
    assert body["variants"][0]["face_count"] == 2


def test_card_variants_unknown_card_404(client: TestClient) -> None:
    _seed_card_db()
    resp = client.get("/api/cards/variants", params={"name": "Storm Crow"})
    assert resp.status_code == 404
    assert "newer than the last import" in resp.json()["detail"]


def test_delete_card_database(client: TestClient, monkeypatch) -> None:
    _seed_card_db()
    assert client.get("/api/cards/status").json()["local"] is not None

    resp = client.request("DELETE", "/api/cards/database")
    assert resp.status_code == 204
    body = client.get("/api/cards/status").json()
    assert body["local"] is None
    # Idempotent: deleting an absent corpus is still a 204.
    assert client.request("DELETE", "/api/cards/database").status_code == 204


def test_delete_card_database_refused_during_import(
    client: TestClient, monkeypatch
) -> None:
    from proxy_scaler import card_import, card_jobs

    monkeypatch.setattr(card_import, "run_import", lambda *a, **k: None)
    client.post("/api/cards/import", json={"dataset": "default_cards"})
    assert card_jobs.active_job() is not None
    resp = client.request("DELETE", "/api/cards/database")
    assert resp.status_code == 409
