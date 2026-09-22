"""The /api/customs routes: the server's content-addressed cache of Custom
Image bytes, and the declared-bleed contract layered on it (the query
parameter, the sidecar it records, and the invalidation a changed
declaration triggers)."""

from __future__ import annotations

import hashlib
import io
from pathlib import Path

import pytest
from fastapi.testclient import TestClient
from PIL import Image

from proxy_scaler import customs, db
from proxy_scaler.dpi import CARD_HEIGHT_MM, CARD_WIDTH_MM
from proxy_scaler.pipeline import FaceResult
from proxy_scaler.upscale import cache_path, original_cache_path, original_thumb_path


@pytest.fixture
def db_path(tmp_path: Path) -> Path:
    return tmp_path / "test.db"


@pytest.fixture
def client(tmp_path: Path, db_path: Path, monkeypatch) -> TestClient:
    db.init_db(db_path)
    monkeypatch.setenv("PROXY_SCALER_DB_PATH", str(db_path))
    monkeypatch.setenv("PROXY_SCALER_WORKER_LOCK_PATH", str(tmp_path / "worker.lock"))
    # customs/ and imgcache/ resolve against the process cwd, exactly as
    # the server's other relative directory names do.
    monkeypatch.chdir(tmp_path)
    from proxy_scaler.api.app import app

    return TestClient(app)


def _png(size: tuple[int, int] = (1000, 1000), color=(20, 40, 90)) -> tuple[bytes, str]:
    buf = io.BytesIO()
    Image.new("RGB", size, color).save(buf, format="PNG")
    data = buf.getvalue()
    return data, hashlib.sha256(data).hexdigest()


def test_upload_records_the_declared_bleed_and_reports_it(client: TestClient) -> None:
    data, content_hash = _png()
    resp = client.post(f"/api/customs/{content_hash}?bleed_mm=3.175", content=data)
    assert resp.status_code == 200, resp.text
    assert resp.json()["present"] is True
    assert resp.json()["bleed_mm"] == pytest.approx(3.175)
    assert client.get(f"/api/customs/{content_hash}").json()["bleed_mm"] == pytest.approx(3.175)
    assert customs.sidecar_path(content_hash).is_file()
    with Image.open(customs.original_path(content_hash)) as img:
        w, h = img.size
    assert w / h == pytest.approx((63 + 6.35) / (88 + 6.35), rel=1e-3)

    # No declaration is plain card aspect and reports 0.
    data2, hash2 = _png((900, 900), (90, 40, 20))
    resp = client.post(f"/api/customs/{hash2}", content=data2)
    assert resp.status_code == 200
    assert resp.json()["bleed_mm"] == 0.0
    with Image.open(customs.original_path(hash2)) as img:
        w, h = img.size
    assert w / h == pytest.approx(CARD_WIDTH_MM / CARD_HEIGHT_MM, rel=1e-3)


def test_upload_rejects_an_out_of_range_bleed(client: TestClient) -> None:
    data, content_hash = _png()
    assert client.post(f"/api/customs/{content_hash}?bleed_mm=11", content=data).status_code == 422
    assert client.post(f"/api/customs/{content_hash}?bleed_mm=-1", content=data).status_code == 422
    assert client.get(f"/api/customs/{content_hash}").json()["present"] is False


def _seed_derivatives(tmp_path: Path, db_path: Path, content_hash: str) -> dict[str, Path]:
    """Everything the server derives from a stored custom: a cached
    original + thumbnail + x4 cache under imgcache/, an upscaled output
    under output/, its registry row (shown in a project), and a done task."""
    cache_dir = tmp_path / "imgcache"
    original = original_cache_path(cache_dir, None, None, custom_hash=content_hash)
    original.parent.mkdir(parents=True, exist_ok=True)
    Image.new("RGB", (63, 88)).save(original)
    thumb = original_thumb_path(original)
    thumb.write_bytes(b"jpg")
    cached = cache_path(cache_dir, None, None, 4, "ultrasharp_v2", custom_hash=content_hash)
    cached.parent.mkdir(parents=True, exist_ok=True)
    cached.write_bytes(b"png")
    out = tmp_path / "output" / f"My-Alter-custom-{content_hash}-ultrasharp_v2-600.png"
    out.parent.mkdir(parents=True, exist_ok=True)
    Image.new("RGB", (63, 88)).save(out)
    result = FaceResult(
        out_path=out,
        original_path=original,
        scryfall_id=None,
        custom_hash=content_hash,
        face_index=None,
        face_name="My Alter",
        card_name="My Alter",
        set_code="",
        collector_number="",
        png_url="",
        dpi=600,
        model="ultrasharp_v2",
    )
    task = db.TaskRow(
        id=1,
        project_tag="tag-a",
        status="done",
        scryfall_id=None,
        custom_hash=content_hash,
        face_index=None,
        face_label=None,
        face_name="My Alter",
        card_name="My Alter",
        set_code="",
        collector_number="",
        png_url="",
        dpi=600,
        model="ultrasharp_v2",
        tile_size=0,
        output_dir=str(out.parent),
        cache_dir=str(cache_dir),
        weights_dir=str(tmp_path),
        error=None,
        created_at="2026-01-01T00:00:00Z",
        started_at=None,
        completed_at=None,
        total_faces=1,
    )
    db.upsert_gallery_item_for_task(task, result, db_path=db_path)
    assert len(db.list_gallery_items("tag-a", db_path=db_path)) == 1
    return {"original": original, "thumb": thumb, "cached": cached, "out": out}


def test_reupload_under_a_different_bleed_discards_everything_derived(
    client: TestClient, tmp_path: Path, db_path: Path
) -> None:
    """The stored PNG's geometry changed, so every upscale, cache entry,
    registry row and done task made from the old one is wrong and goes;
    the stored PNG itself is re-cropped in place."""
    data, content_hash = _png()
    assert client.post(f"/api/customs/{content_hash}", content=data).status_code == 200
    files = _seed_derivatives(tmp_path, db_path, content_hash)

    # Same declaration again: a no-op, nothing discarded.
    assert client.post(f"/api/customs/{content_hash}", content=data).status_code == 200
    assert all(p.exists() for p in files.values())
    assert len(db.list_gallery_items("tag-a", db_path=db_path)) == 1

    resp = client.post(f"/api/customs/{content_hash}?bleed_mm=3.175", content=data)
    assert resp.status_code == 200
    assert resp.json()["bleed_mm"] == pytest.approx(3.175)
    assert not any(p.exists() for p in files.values()), files
    assert db.list_gallery_items("tag-a", db_path=db_path) == []
    with db.connect(db_path) as conn:
        assert conn.execute("SELECT COUNT(*) FROM generation_tasks").fetchone()[0] == 0
    assert customs.has_original(content_hash)
    with Image.open(customs.original_path(content_hash)) as img:
        w, h = img.size
    assert w / h == pytest.approx((63 + 6.35) / (88 + 6.35), rel=1e-3)


def test_delete_removes_the_sidecar_too(client: TestClient) -> None:
    data, content_hash = _png()
    client.post(f"/api/customs/{content_hash}?bleed_mm=2", content=data)
    assert client.delete(f"/api/customs/{content_hash}").json() == {"removed": 1}
    assert not customs.sidecar_path(content_hash).exists()
    assert client.get(f"/api/customs/{content_hash}").json()["bleed_mm"] == 0.0
