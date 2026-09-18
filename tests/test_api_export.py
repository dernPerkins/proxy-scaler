"""TestClient tests for /api/export — the ZIP export endpoints. Same
tmp_path-isolated fixture idiom as test_api.py (see that module's
docstring); seeds gallery rows the way the worker would, then inspects
the returned archive with zipfile directly."""

from __future__ import annotations

import io
import tempfile
import threading
import time
import zipfile
from datetime import date
from pathlib import Path

import pytest
from fastapi.testclient import TestClient
from PIL import Image

from proxy_scaler import backs, db, pdf_jobs
from proxy_scaler.api.routers import export as export_router
from proxy_scaler.dpi import dpi_at_card_size, target_pixels
from proxy_scaler.pdf_layout import _bled_pixels
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
    # Archives (and a rendering export's scratch images) are temp files;
    # pointing tempfile here makes "nothing leaked" a glob over tmp_path.
    monkeypatch.setattr(tempfile, "tempdir", str(tmp_path))
    pdf_jobs._reset_for_tests()
    from proxy_scaler.api.app import app

    yield TestClient(app)
    pdf_jobs._reset_for_tests()


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
    image: Image.Image | None = None,
    suffix: str = ".png",
) -> Path:
    """Fakes one completed face the way the worker would, with a
    distinctly-colored PNG so a ZIP entry's bytes identify their source.
    `image` replaces the flat fill; `suffix` picks the stored file type
    (a custom upload can be a JPEG)."""
    img_path = tmp_path / f"{scryfall_id}-{face_index}-{dpi}{suffix}"
    if image is None:
        image = Image.new("RGBA", (200, 280), color)
    if suffix == ".jpg":
        image = image.convert("RGB")
    image.save(img_path)
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


def _seed_back(image: Image.Image | None = None) -> tuple[str, bytes]:
    """Store a Selected Back on the server (cwd-relative backs/, so run
    after the fixture's chdir). Returns (content_hash, stored PNG bytes)."""
    raw = io.BytesIO()
    (image or Image.new("RGB", (200, 280), (200, 100, 50))).save(raw, format="PNG")
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


def test_export_filenames_follow_entries_order(
    client: TestClient, tmp_path: Path
) -> None:
    """FRONT/NNN numbering follows the entries array, not the order the
    gallery generated in — the client sends entries pre-sorted by the
    shared sort control, and that order is the export order."""
    db_path = tmp_path / "test.db"
    counter_path = _seed_face(
        tmp_path, db_path, "tag-a",
        scryfall_id="counter-id", name="Counterspell",
        set_code="lea", collector_number="55", color=(1, 2, 3, 255),
    )
    sol_path = _seed_face(tmp_path, db_path, "tag-a", color=(4, 5, 6, 255))

    counter_entry = _entry(
        name="Counterspell", set_code="lea", collector_number="55",
        raw_line="1 Counterspell (lea) 55",
    )
    resp = client.post(
        "/api/export/zip", json=_body(entries=[_entry(), counter_entry])
    )
    archive = _open_zip(resp)
    assert archive.namelist() == ["Deck/FRONT/001.png", "Deck/FRONT/002.png"]
    assert archive.read("Deck/FRONT/001.png") == sol_path.read_bytes()
    assert archive.read("Deck/FRONT/002.png") == counter_path.read_bytes()


def test_tcgplaytest_fronts_and_backs_follow_entries_order(
    client: TestClient, tmp_path: Path
) -> None:
    """Reordering by entries keeps FRONT/NNN and BACK/NNN in lockstep — a
    DFC sorted after a normal card still gets its own Back Face on its
    slot, with the Selected Back on the normal card's."""
    db_path = tmp_path / "test.db"
    dfc_front_path = _seed_face(
        tmp_path, db_path, "tag-a",
        scryfall_id="dfc-id", face_index=0, name="Delver of Secrets",
        set_code="isd", collector_number="51",
        color=(1, 2, 3, 255), total_faces=2,
    )
    dfc_back_path = _seed_face(
        tmp_path, db_path, "tag-a",
        scryfall_id="dfc-id", face_index=1, name="Insectile Aberration",
        set_code="isd", collector_number="51",
        color=(4, 5, 6, 255), total_faces=2,
    )
    sol_path = _seed_face(tmp_path, db_path, "tag-a", color=(7, 8, 9, 255))
    content_hash, back_bytes = _seed_back()

    dfc_entry = _entry(
        name="Delver of Secrets", set_code="isd",
        collector_number="51", raw_line="1 Delver of Secrets (isd) 51",
    )
    resp = client.post(
        "/api/export/zip",
        json=_body(
            entries=[_entry(), dfc_entry],
            format="tcgplaytest",
            back_image_hash=content_hash,
        ),
    )
    archive = _open_zip(resp)
    assert archive.namelist() == [
        "Deck/FRONT/001.png", "Deck/FRONT/002.png",
        "Deck/BACK/001.png", "Deck/BACK/002.png",
    ]
    assert archive.read("Deck/FRONT/001.png") == sol_path.read_bytes()
    assert archive.read("Deck/BACK/001.png") == back_bytes
    assert archive.read("Deck/FRONT/002.png") == dfc_front_path.read_bytes()
    assert archive.read("Deck/BACK/002.png") == dfc_back_path.read_bytes()


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


def test_use_originals_exports_download_variant_bytes(
    client: TestClient, tmp_path: Path
) -> None:
    """With use_originals the ZIP ships the (300, "original") download
    variant's bytes verbatim, even when a higher-DPI upscale exists for
    the same face — and without the flag, the upscale ships instead."""
    from proxy_scaler.dpi import ORIGINAL_DPI, ORIGINAL_MODEL

    db_path = tmp_path / "test.db"
    upscale_path = _seed_face(tmp_path, db_path, "tag-a", dpi=1200)

    original_path = tmp_path / "sol-id-original.png"
    Image.new("RGBA", (200, 280), (250, 240, 10, 255)).save(original_path, format="PNG")
    db.upsert_gallery_item(
        "tag-a",
        FaceResult(
            out_path=original_path,
            original_path=original_path,
            scryfall_id="sol-id",
            face_index=None,
            face_name="Sol Ring",
            card_name="Sol Ring",
            set_code="c21",
            collector_number="263",
            png_url="https://example.com/sol-id.png",
            dpi=ORIGINAL_DPI,
            model=ORIGINAL_MODEL,
        ),
        db_path=db_path,
    )

    resp = client.post("/api/export/zip", json=_body(use_originals=True))
    archive = _open_zip(resp)
    assert archive.read("Deck/FRONT/001.png") == original_path.read_bytes()

    resp = client.post("/api/export/zip", json=_body())
    archive = _open_zip(resp)
    assert archive.read("Deck/FRONT/001.png") == upscale_path.read_bytes()


def test_use_originals_preview_reports_missing_download(
    client: TestClient, tmp_path: Path
) -> None:
    """A face with only upscaled variants counts as missing under
    use_originals — never silently substituted with an upscale."""
    db_path = tmp_path / "test.db"
    _seed_face(tmp_path, db_path, "tag-a", dpi=1200)

    resp = client.post("/api/export/zip/preview", json=_body(use_originals=True))
    assert resp.status_code == 200
    body = resp.json()
    assert body["fronts"] == 0
    assert len(body["missing"]) == 1


# --- Output options: bleed and image format --------------------------------
#
# The seed face is 200x280 at 800 DPI, so 3 mm of bleed is
# round(800 / 25.4 * 3) = 94 px per side -> 388x468.

SEED_SIZE = (200, 280)
SEED_DPI = 800


def _bled_size(size: tuple[int, int], dpi: int, bleed_mm: float) -> tuple[int, int]:
    px = round(dpi / 25.4 * bleed_mm)
    return size[0] + 2 * px, size[1] + 2 * px


def _decode(archive: zipfile.ZipFile, name: str) -> Image.Image:
    img = Image.open(io.BytesIO(archive.read(name)))
    img.load()
    return img


def _close(actual: tuple[int, ...], expected: tuple[int, ...], tol: int = 12) -> bool:
    return all(abs(a - e) <= tol for a, e in zip(actual, expected))


def test_with_bleed_renders_bled_pngs(client: TestClient, tmp_path: Path) -> None:
    """The MakePlayingCards.com case: every entry gains a 3 mm border at
    the image's own DPI and is re-encoded — so its bytes are no longer the
    stored file's, and the archive names it .png regardless of source."""
    db_path = tmp_path / "test.db"
    stored = _seed_face(tmp_path, db_path, "tag-a")

    resp = client.post("/api/export/zip", json=_body(with_bleed=True))
    archive = _open_zip(resp)
    assert archive.namelist() == ["Deck/FRONT/001.png"]
    img = _decode(archive, "Deck/FRONT/001.png")
    assert img.format == "PNG"
    assert img.mode == "RGB"
    assert img.size == _bled_size(SEED_SIZE, SEED_DPI, 3.0) == (388, 468)
    assert archive.read("Deck/FRONT/001.png") != stored.read_bytes()
    # Edge-extended, not padded: the border is the card's own edge colour.
    assert _close(img.getpixel((2, 2)), (10, 20, 30))


def test_without_bleed_png_is_still_byte_for_byte(client: TestClient, tmp_path: Path) -> None:
    """The defaults (and an explicit PNG + no bleed) are the original
    export, untouched — the promise the docstring makes."""
    db_path = tmp_path / "test.db"
    stored = _seed_face(tmp_path, db_path, "tag-a")
    resp = client.post(
        "/api/export/zip", json=_body(with_bleed=False, image_format="png")
    )
    archive = _open_zip(resp)
    assert archive.read("Deck/FRONT/001.png") == stored.read_bytes()


def test_bleed_mm_is_honoured_and_bounded(client: TestClient, tmp_path: Path) -> None:
    db_path = tmp_path / "test.db"
    _seed_face(tmp_path, db_path, "tag-a")

    resp = client.post("/api/export/zip", json=_body(with_bleed=True, bleed_mm=1.0))
    img = _decode(_open_zip(resp), "Deck/FRONT/001.png")
    assert img.size == _bled_size(SEED_SIZE, SEED_DPI, 1.0) == (262, 342)

    assert client.post("/api/export/zip", json=_body(with_bleed=True, bleed_mm=0)).status_code == 422
    assert client.post("/api/export/zip", json=_body(with_bleed=True, bleed_mm=11)).status_code == 422


def test_jpg_format_reencodes_and_flattens_corners(client: TestClient, tmp_path: Path) -> None:
    """JPEG has no alpha, so the rounded-corner transparency must become
    the card's own edge colour (the PDF's treatment) rather than whatever
    a naive convert() would leave — black."""
    db_path = tmp_path / "test.db"
    face = Image.new("RGBA", SEED_SIZE, (10, 20, 30, 255))
    face.paste((0, 0, 0, 0), (0, 0, 6, 6))  # a transparent corner
    stored = _seed_face(tmp_path, db_path, "tag-a", image=face)

    resp = client.post("/api/export/zip", json=_body(image_format="jpg"))
    archive = _open_zip(resp)
    assert archive.namelist() == ["Deck/FRONT/001.jpg"]
    img = _decode(archive, "Deck/FRONT/001.jpg")
    assert img.format == "JPEG"
    assert img.size == SEED_SIZE  # no bleed asked for: same pixel box
    assert archive.read("Deck/FRONT/001.jpg") != stored.read_bytes()
    assert _close(img.getpixel((0, 0)), (10, 20, 30))


def test_jpg_format_with_bleed(client: TestClient, tmp_path: Path) -> None:
    db_path = tmp_path / "test.db"
    _seed_face(tmp_path, db_path, "tag-a")
    resp = client.post("/api/export/zip", json=_body(image_format="jpg", with_bleed=True))
    archive = _open_zip(resp)
    assert archive.namelist() == ["Deck/FRONT/001.jpg"]
    img = _decode(archive, "Deck/FRONT/001.jpg")
    assert img.format == "JPEG"
    assert img.size == (388, 468)


def test_verbatim_png_export_keeps_a_jpeg_sources_suffix(
    client: TestClient, tmp_path: Path
) -> None:
    """A custom upload stored as .jpg ships as the .jpg it is on the
    verbatim path — "png" means "as stored", not "convert to PNG"."""
    db_path = tmp_path / "test.db"
    stored = _seed_face(tmp_path, db_path, "tag-a", suffix=".jpg")
    resp = client.post("/api/export/zip", json=_body(image_format="png"))
    archive = _open_zip(resp)
    assert archive.namelist() == ["Deck/FRONT/001.jpg"]
    assert archive.read("Deck/FRONT/001.jpg") == stored.read_bytes()


def _bordered_back() -> Image.Image:
    """8 px green border around a magenta interior — enough to tell the two
    back-bleed paths apart by sampling, since their sizes differ by 1 px."""
    img = Image.new("RGB", SEED_SIZE, (0, 200, 0))
    img.paste((255, 0, 255), (8, 8, SEED_SIZE[0] - 8, SEED_SIZE[1] - 8))
    return img


def test_with_bleed_back_is_bled_at_its_own_dpi(client: TestClient, tmp_path: Path) -> None:
    """The Selected Back has no generated-image record, so its DPI is what
    its pixel size implies at card size; without the includes-bleed flag
    it is cover-fitted to trim size and edge-extended like a card."""
    db_path = tmp_path / "test.db"
    _seed_face(tmp_path, db_path, "tag-a")
    content_hash, _stored = _seed_back(_bordered_back())
    back_dpi = round(dpi_at_card_size(*SEED_SIZE))

    resp = client.post(
        "/api/export/zip", json=_body(with_bleed=True, back_image_hash=content_hash)
    )
    archive = _open_zip(resp)
    assert archive.namelist() == ["Deck/FRONT/001.png", "Deck/BACK/001.png"]
    img = _decode(archive, "Deck/BACK/001.png")
    assert img.size == _bled_size(target_pixels(back_dpi), back_dpi, 3.0)
    # 12 px in is still inside the extended green border.
    assert _close(img.getpixel((12, img.height // 2)), (0, 200, 0))


def test_with_bleed_back_that_includes_bleed_fits_to_the_bled_box(
    client: TestClient, tmp_path: Path
) -> None:
    """Declared as already carrying bleed: no second border is added, the
    art is scaled straight to the bled size and its own border becomes
    the bleed — so 12 px in is already the magenta interior."""
    db_path = tmp_path / "test.db"
    _seed_face(tmp_path, db_path, "tag-a")
    content_hash, _stored = _seed_back(_bordered_back())
    back_dpi = round(dpi_at_card_size(*SEED_SIZE))

    resp = client.post(
        "/api/export/zip",
        json=_body(with_bleed=True, back_image_hash=content_hash, back_image_includes_bleed=True),
    )
    img = _decode(_open_zip(resp), "Deck/BACK/001.png")
    assert img.size == _bled_pixels(back_dpi, 3.0)
    assert _close(img.getpixel((12, img.height // 2)), (255, 0, 255))


def test_tcgplaytest_with_bleed_renders_each_source_once(
    client: TestClient, tmp_path: Path, monkeypatch
) -> None:
    """Four copies means four FRONT/BACK pairs but only two renders — the
    repeated entries are copies of one rendered file."""
    db_path = tmp_path / "test.db"
    _seed_face(tmp_path, db_path, "tag-a")
    content_hash, _stored = _seed_back()

    renders: list[Path] = []
    real_render = export_router._render_entry

    def counting_render(source, face, **kwargs):
        renders.append(source)
        return real_render(source, face, **kwargs)

    monkeypatch.setattr(export_router, "_render_entry", counting_render)

    resp = client.post(
        "/api/export/zip",
        json=_body(
            entries=[_entry(quantity=4)], format="tcgplaytest",
            with_bleed=True, back_image_hash=content_hash,
        ),
    )
    archive = _open_zip(resp)
    assert archive.namelist() == [
        "Deck/FRONT/001.png", "Deck/FRONT/002.png", "Deck/FRONT/003.png", "Deck/FRONT/004.png",
        "Deck/BACK/001.png", "Deck/BACK/002.png", "Deck/BACK/003.png", "Deck/BACK/004.png",
    ]
    assert len(renders) == 2
    fronts = {archive.read(f"Deck/FRONT/00{i}.png") for i in range(1, 5)}
    backs_ = {archive.read(f"Deck/BACK/00{i}.png") for i in range(1, 5)}
    assert len(fronts) == 1 and len(backs_) == 1
    assert _decode(archive, "Deck/FRONT/001.png").size == (388, 468)


def test_tcgplaytest_with_bleed_bleeds_a_dfc_back_face(
    client: TestClient, tmp_path: Path
) -> None:
    db_path = tmp_path / "test.db"
    for face_index, name, color in (
        (0, "Delver of Secrets", (1, 2, 3, 255)),
        (1, "Insectile Aberration", (4, 5, 6, 255)),
    ):
        _seed_face(
            tmp_path, db_path, "tag-a",
            scryfall_id="dfc-id", face_index=face_index, name=name,
            set_code="isd", collector_number="51", color=color, total_faces=2,
        )
    entry = _entry(
        name="Delver of Secrets", set_code="isd", collector_number="51",
        raw_line="1 Delver of Secrets (isd) 51",
    )
    resp = client.post(
        "/api/export/zip", json=_body(entries=[entry], format="tcgplaytest", with_bleed=True)
    )
    archive = _open_zip(resp)
    assert archive.namelist() == ["Deck/FRONT/001.png", "Deck/BACK/001.png"]
    back = _decode(archive, "Deck/BACK/001.png")
    assert back.size == (388, 468)
    assert _close(back.getpixel((2, 2)), (4, 5, 6))


def test_sync_export_leaves_no_temp_files_behind(client: TestClient, tmp_path: Path) -> None:
    db_path = tmp_path / "test.db"
    _seed_face(tmp_path, db_path, "tag-a")
    _open_zip(client.post("/api/export/zip", json=_body(with_bleed=True)))
    assert list(tmp_path.glob("*.zip")) == []
    assert list(tmp_path.glob("proxy-scaler-export-*")) == []


# --- Export jobs -----------------------------------------------------------


def _await_export_job(client: TestClient, job_id: str, *, timeout_s: float = 20.0) -> dict:
    """Poll to a terminal state, the way the desktop client does."""
    deadline = time.monotonic() + timeout_s
    while time.monotonic() < deadline:
        resp = client.get(f"/api/export/zip/jobs/{job_id}")
        assert resp.status_code == 200
        body = resp.json()
        if body["status"] != "rendering":
            return body
        time.sleep(0.02)
    raise AssertionError(f"export job {job_id} never finished")


def test_export_job_lifecycle_start_poll_fetch(client: TestClient, tmp_path: Path) -> None:
    """Start, poll to done, fetch from a plain GET — and afterwards the
    job is gone, its temp file with it (FileResponse's background unlink
    runs inside TestClient before the response returns)."""
    db_path = tmp_path / "test.db"
    _seed_face(tmp_path, db_path, "tag-a")
    content_hash, _stored = _seed_back()

    resp = client.post(
        "/api/export/zip/jobs", json=_body(with_bleed=True, back_image_hash=content_hash)
    )
    assert resp.status_code == 202
    started = resp.json()
    job_id = started["job_id"]
    assert started["total"] == 2  # one front, one back

    final = _await_export_job(client, job_id)
    assert final["status"] == "done"
    assert final["completed"] == 2
    archive_path = pdf_jobs.get(job_id).path
    assert archive_path is not None and archive_path.exists()

    resp = client.get(f"/api/export/zip/jobs/{job_id}/result")
    assert resp.status_code == 200
    assert resp.headers["content-type"] == "application/zip"
    assert 'filename="Deck.zip"' in resp.headers["content-disposition"]
    archive = zipfile.ZipFile(io.BytesIO(resp.content))
    assert archive.namelist() == ["Deck/FRONT/001.png", "Deck/BACK/001.png"]
    assert _decode(archive, "Deck/FRONT/001.png").size == (388, 468)

    assert client.get(f"/api/export/zip/jobs/{job_id}").status_code == 404
    assert client.get(f"/api/export/zip/jobs/{job_id}/result").status_code == 404
    assert not archive_path.exists()
    assert list(tmp_path.glob("*.zip")) == []


def test_export_job_verbatim_finishes_immediately(client: TestClient, tmp_path: Path) -> None:
    """No options set is still a job — the one client code path — and the
    archive is the byte-for-byte one."""
    db_path = tmp_path / "test.db"
    stored = _seed_face(tmp_path, db_path, "tag-a")
    resp = client.post("/api/export/zip/jobs", json=_body())
    assert resp.status_code == 202
    job_id = resp.json()["job_id"]
    assert _await_export_job(client, job_id)["status"] == "done"
    archive = zipfile.ZipFile(io.BytesIO(client.get(f"/api/export/zip/jobs/{job_id}/result").content))
    assert archive.read("Deck/FRONT/001.png") == stored.read_bytes()


def test_export_job_validation_errors_surface_on_start(
    client: TestClient, tmp_path: Path
) -> None:
    db_path = tmp_path / "test.db"
    assert client.post("/api/export/zip/jobs", json=_body(entries=[])).status_code == 400
    assert client.post("/api/export/zip/jobs", json=_body()).status_code == 400  # nothing generated
    _seed_face(tmp_path, db_path, "tag-a")
    resp = client.post("/api/export/zip/jobs", json=_body(format="tcgplaytest"))
    assert resp.status_code == 400  # needs a Selected Back
    assert pdf_jobs.active_count() == 0


def test_export_job_refuses_a_second_concurrent_render(
    client: TestClient, tmp_path: Path
) -> None:
    """One registry for PDF and ZIP renders: a PDF in flight blocks an
    export too."""
    db_path = tmp_path / "test.db"
    _seed_face(tmp_path, db_path, "tag-a")
    pdf_jobs.create_job(filename="busy.pdf", total=1)  # stands in for a live render
    assert client.post("/api/export/zip/jobs", json=_body()).status_code == 409


def test_export_job_result_409s_before_the_render_finishes(
    client: TestClient, tmp_path: Path, monkeypatch
) -> None:
    db_path = tmp_path / "test.db"
    _seed_face(tmp_path, db_path, "tag-a")
    release = threading.Event()
    real_render = export_router._render_entry

    def gated_render(source, face, **kwargs):
        release.wait(5)
        return real_render(source, face, **kwargs)

    monkeypatch.setattr(export_router, "_render_entry", gated_render)
    job_id = client.post("/api/export/zip/jobs", json=_body(with_bleed=True)).json()["job_id"]
    resp = client.get(f"/api/export/zip/jobs/{job_id}/result")
    assert resp.status_code == 409
    assert pdf_jobs.get(job_id) is not None
    release.set()
    assert _await_export_job(client, job_id)["status"] == "done"
    assert client.get(f"/api/export/zip/jobs/{job_id}/result").status_code == 200


def test_export_job_cancel_unlinks_the_half_built_archive(
    client: TestClient, tmp_path: Path, monkeypatch
) -> None:
    """Cancel is noticed at the next progress callback; the partial ZIP
    and the scratch renders must not survive it."""
    db_path = tmp_path / "test.db"
    for i in range(3):
        _seed_face(
            tmp_path, db_path, "tag-a", scryfall_id=f"card-{i}", name=f"Card {i}",
            collector_number=str(i),
        )
    entries = [
        _entry(name=f"Card {i}", collector_number=str(i), raw_line=f"1 Card {i} (c21) {i}")
        for i in range(3)
    ]
    real_render = export_router._render_entry

    def slow_render(source, face, **kwargs):
        time.sleep(0.15)
        return real_render(source, face, **kwargs)

    monkeypatch.setattr(export_router, "_render_entry", slow_render)
    resp = client.post("/api/export/zip/jobs", json=_body(entries=entries, with_bleed=True))
    assert resp.status_code == 202
    job_id = resp.json()["job_id"]
    assert resp.json()["total"] == 3

    assert client.post(f"/api/export/zip/jobs/{job_id}/cancel").status_code == 204
    final = _await_export_job(client, job_id)
    assert final["status"] == "canceled"
    assert pdf_jobs.get(job_id).path is None
    assert client.get(f"/api/export/zip/jobs/{job_id}/result").status_code == 409
    assert list(tmp_path.glob("*.zip")) == []
    assert list(tmp_path.glob("proxy-scaler-export-*")) == []

    assert client.post("/api/export/zip/jobs/nope/cancel").status_code == 404
    assert client.get("/api/export/zip/jobs/nope").status_code == 404
