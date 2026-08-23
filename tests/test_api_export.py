"""TestClient tests for /api/export — the ZIP export endpoints. Same
tmp_path-isolated fixture idiom as test_api.py (see that module's
docstring); seeds gallery rows the way the worker would, then inspects
the returned archive with zipfile directly."""

from __future__ import annotations

import io
import zipfile
from datetime import date
from pathlib import Path

import pytest
from fastapi.testclient import TestClient
from PIL import Image

from proxy_scaler import backs, db
from proxy_scaler.pipeline import FaceResult


@pytest.fixture
def client(tmp_path: Path, monkeypatch) -> TestClient:
    db_path = tmp_path / "test.db"
    db.init_db(db_path)
    # `backs/` resolves against the process cwd (same as test_api.py's
    # fixture) — chdir keeps the Selected Back seeding out of the repo.
    monkeypatch.chdir(tmp_path)
    monkeypatch.setenv("PROXY_SCALER_DB_PATH", str(db_path))
    monkeypatch.setenv("PROXY_SCALER_WORKER_LOCK_PATH", str(tmp_path / "worker.lock"))
    monkeypatch.setenv("PROXY_SCALER_CARD_DB_PATH", str(tmp_path / "cards.db"))
    from proxy_scaler.api.app import app

    return TestClient(app)


_next_task_id = iter(range(1, 10_000))


def _seed_face(
    tmp_path: Path,
    db_path: Path,
    project_tag: str,
    *,
    scryfall_id: str = "sol-id",
    face_index: int | None = None,
    name: str = "Sol Ring",
    set_code: str = "c21",
    collector_number: str = "263",
    dpi: int = 800,
    color: tuple[int, int, int, int] = (10, 20, 30, 255),
    total_faces: int | None = None,
) -> Path:
    """Fakes one completed face the way the worker would, with a
    distinctly-colored PNG so a ZIP entry's bytes identify their source."""
    img_path = tmp_path / f"{scryfall_id}-{face_index}-{dpi}.png"
    Image.new("RGBA", (200, 280), color).save(img_path, format="PNG")
    result = FaceResult(
        out_path=img_path,
        original_path=img_path,
        scryfall_id=scryfall_id,
        face_index=face_index,
        face_name=name,
        card_name=name,
        set_code=set_code,
        collector_number=collector_number,
        png_url=f"https://example.com/{scryfall_id}.png",
        dpi=dpi,
        model="ultrasharp_v2",
        total_faces=total_faces,
    )
    task = db.TaskRow(
        id=next(_next_task_id),
        project_tag=project_tag,
        status="done",
        scryfall_id=scryfall_id,
        face_index=face_index,
        face_label=None,
        face_name=name,
        card_name=name,
        set_code=set_code,
        collector_number=collector_number,
        png_url=f"https://example.com/{scryfall_id}.png",
        dpi=dpi,
        model="ultrasharp_v2",
        tile_size=0,
        output_dir=str(tmp_path),
        cache_dir=str(tmp_path),
        weights_dir=str(tmp_path),
        error=None,
        created_at="2026-01-01T00:00:00Z",
        started_at=None,
        completed_at=None,
        total_faces=total_faces,
    )
    db.upsert_gallery_item_for_task(task, result, db_path=db_path)
    return img_path


def _seed_back() -> tuple[str, bytes]:
    """Store a Selected Back on the server (cwd-relative backs/, so run
    after the fixture's chdir). Returns (content_hash, stored PNG bytes)."""
    raw = io.BytesIO()
    Image.new("RGB", (200, 280), (200, 100, 50)).save(raw, format="PNG")
    content_hash, path = backs.store_original(raw.getvalue())
    return content_hash, path.read_bytes()


def _entry(**overrides) -> dict:
    entry = {
        "quantity": 1,
        "name": "Sol Ring",
        "set_code": "c21",
        "collector_number": "263",
        "raw_line": "1 Sol Ring (c21) 263",
    }
    entry.update(overrides)
    return entry


def _body(**overrides) -> dict:
    body = {
        "project_tag": "tag-a",
        "entries": [_entry()],
        "project_name": "Deck",
    }
    body.update(overrides)
    return body


def _open_zip(resp) -> zipfile.ZipFile:
    assert resp.status_code == 200
    assert resp.headers["content-type"] == "application/zip"
    return zipfile.ZipFile(io.BytesIO(resp.content))


def test_export_no_entries_is_400(client: TestClient) -> None:
    resp = client.post("/api/export/zip", json=_body(entries=[]))
    assert resp.status_code == 400


def test_export_nothing_generated_is_400(client: TestClient) -> None:
    resp = client.post("/api/export/zip", json=_body())
    assert resp.status_code == 400
    assert "Nothing to export" in resp.json()["detail"]


def test_default_format_dedupes_quantities_and_omits_back(
    client: TestClient, tmp_path: Path
) -> None:
    """Quantity 4 still yields ONE default-format front — it's an image
    dump, not a print run — and with no back selected there is no BACK/."""
    db_path = tmp_path / "test.db"
    _seed_face(tmp_path, db_path, "tag-a")

    resp = client.post("/api/export/zip", json=_body(entries=[_entry(quantity=4)]))
    assert _open_zip(resp).namelist() == ["Deck/FRONT/001.png"]
    assert "Deck.zip" in resp.headers["content-disposition"]


def test_default_format_includes_selected_back_once(
    client: TestClient, tmp_path: Path
) -> None:
    db_path = tmp_path / "test.db"
    front_path = _seed_face(tmp_path, db_path, "tag-a")
    content_hash, back_bytes = _seed_back()

    resp = client.post("/api/export/zip", json=_body(back_image_hash=content_hash))
    archive = _open_zip(resp)
    assert archive.namelist() == ["Deck/FRONT/001.png", "Deck/BACK/001.png"]
    assert archive.read("Deck/FRONT/001.png") == front_path.read_bytes()
    assert archive.read("Deck/BACK/001.png") == back_bytes


def test_unsynced_back_hash_is_400_not_a_backless_zip(
    client: TestClient, tmp_path: Path
) -> None:
    db_path = tmp_path / "test.db"
    _seed_face(tmp_path, db_path, "tag-a")

    resp = client.post("/api/export/zip", json=_body(back_image_hash="ab" * 32))
    assert resp.status_code == 400
    assert "not synced" in resp.json()["detail"]


def test_tcgplaytest_expands_quantities_into_matched_pairs(
    client: TestClient, tmp_path: Path
) -> None:
    db_path = tmp_path / "test.db"
    _seed_face(tmp_path, db_path, "tag-a")
    content_hash, back_bytes = _seed_back()

    resp = client.post(
        "/api/export/zip",
        json=_body(
            entries=[_entry(quantity=4)],
            format="tcgplaytest",
            back_image_hash=content_hash,
        ),
    )
    archive = _open_zip(resp)
    assert archive.namelist() == [
        *(f"Deck/FRONT/{i:03d}.png" for i in range(1, 5)),
        *(f"Deck/BACK/{i:03d}.png" for i in range(1, 5)),
    ]
    assert archive.read("Deck/BACK/003.png") == back_bytes


def test_tcgplaytest_without_back_image_is_400(client: TestClient, tmp_path: Path) -> None:
    db_path = tmp_path / "test.db"
    _seed_face(tmp_path, db_path, "tag-a")

    resp = client.post("/api/export/zip", json=_body(format="tcgplaytest"))
    assert resp.status_code == 400
    assert "back image" in resp.json()["detail"]


def test_tcgplaytest_dfc_back_comes_from_its_own_back_face(
    client: TestClient, tmp_path: Path
) -> None:
    """An all-DFC deck pairs each front with the card's own Back Face and
    needs no Selected Back at all."""
    db_path = tmp_path / "test.db"
    front_path = _seed_face(
        tmp_path, db_path, "tag-a",
        scryfall_id="dfc-id", face_index=0, name="Delver of Secrets",
        set_code="isd", collector_number="51",
        color=(1, 2, 3, 255), total_faces=2,
    )
    back_face_path = _seed_face(
        tmp_path, db_path, "tag-a",
        scryfall_id="dfc-id", face_index=1, name="Insectile Aberration",
        set_code="isd", collector_number="51",
        color=(4, 5, 6, 255), total_faces=2,
    )

    entry = _entry(
        quantity=2, name="Delver of Secrets", set_code="isd",
        collector_number="51", raw_line="2 Delver of Secrets (isd) 51",
    )
    resp = client.post(
        "/api/export/zip", json=_body(entries=[entry], format="tcgplaytest")
    )
    archive = _open_zip(resp)
    assert archive.namelist() == [
        "Deck/FRONT/001.png", "Deck/FRONT/002.png",
        "Deck/BACK/001.png", "Deck/BACK/002.png",
    ]
    assert archive.read("Deck/FRONT/001.png") == front_path.read_bytes()
    assert archive.read("Deck/BACK/001.png") == back_face_path.read_bytes()


def test_padding_widens_past_999_slots(client: TestClient, tmp_path: Path) -> None:
    db_path = tmp_path / "test.db"
    _seed_face(tmp_path, db_path, "tag-a")
    content_hash, _ = _seed_back()

    resp = client.post(
        "/api/export/zip",
        json=_body(
            entries=[_entry(quantity=1000)],
            format="tcgplaytest",
            back_image_hash=content_hash,
        ),
    )
    names = _open_zip(resp).namelist()
    assert names[0] == "Deck/FRONT/0001.png"
    assert names[-1] == "Deck/BACK/1000.png"


def test_filename_falls_back_to_dated_slug(client: TestClient, tmp_path: Path) -> None:
    db_path = tmp_path / "test.db"
    _seed_face(tmp_path, db_path, "tag-a")

    resp = client.post("/api/export/zip", json=_body(project_name=""))
    expected = f"proxy-scaler-{date.today().isoformat()}"
    assert f"{expected}.zip" in resp.headers["content-disposition"]
    assert _open_zip(resp).namelist() == [f"{expected}/FRONT/001.png"]


def test_preview_reports_counts_and_reverses_needing_back(
    client: TestClient, tmp_path: Path
) -> None:
    db_path = tmp_path / "test.db"
    _seed_face(tmp_path, db_path, "tag-a")

    resp = client.post(
        "/api/export/zip/preview", json=_body(entries=[_entry(quantity=4)])
    )
    assert resp.status_code == 200
    assert resp.json() == {
        "fronts": 1,
        "paired_fronts": 4,
        "missing": [],
        "missing_at_dpi": [],
        "reverses_needing_back_image": 4,
    }


def test_preview_reports_missing_at_dpi(client: TestClient, tmp_path: Path) -> None:
    """preferred_dpi is the same hard filter as the PDF's — a face with no
    image at that DPI is excluded and reported, never substituted."""
    db_path = tmp_path / "test.db"
    _seed_face(tmp_path, db_path, "tag-a", dpi=800)

    resp = client.post("/api/export/zip/preview", json=_body(preferred_dpi=1200))
    body = resp.json()
    assert body["fronts"] == 0
    assert body["missing_at_dpi"] == ["Sol Ring [C21 263]"]

    zip_resp = client.post("/api/export/zip", json=_body(preferred_dpi=1200))
    assert zip_resp.status_code == 400
