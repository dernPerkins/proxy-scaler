"""Face overrides: a Custom Image (MPC Fill art) printed in place of one
face of a Scryfall card. Generation skips that face's Scryfall work and
registers the custom; print matching swaps the custom in under the
card's identity, so DFC pairing and per-line quantities keep working."""

from __future__ import annotations

import hashlib
import io
from pathlib import Path

import pytest
from PIL import Image

from proxy_scaler import carddb, customs, db
from proxy_scaler.decklist import DeckEntry, FaceOverride
from proxy_scaler.dpi import CUSTOM_SOURCE_MODEL
from proxy_scaler.pdf_layout import build_print_slots, match_quantities
from proxy_scaler.pipeline import FaceResult
from proxy_scaler.scryfall import ScryfallClient
from proxy_scaler.services import generation

DELVER = {
    "id": "delver-id",
    "name": "Delver of Secrets // Insectile Aberration",
    "set": "isd",
    "collector_number": "51",
    "layout": "transform",
    "image_status": "highres_scan",
    "card_faces": [
        {"name": "Delver of Secrets", "image_uris": {"png": "https://example.com/d0.png"}},
        {"name": "Insectile Aberration", "image_uris": {"png": "https://example.com/d1.png"}},
    ],
}

HASH = "a" * 64
OTHER_HASH = "b" * 64


@pytest.fixture(autouse=True)
def _no_default_card_corpus(tmp_path: Path, monkeypatch) -> None:
    monkeypatch.setattr(carddb, "DEFAULT_CARD_DB_PATH", tmp_path / "no-corpus.db")


@pytest.fixture
def db_path(tmp_path: Path, monkeypatch) -> Path:
    # customs/ resolves against the cwd, as on a real server.
    monkeypatch.chdir(tmp_path)
    path = tmp_path / "test.db"
    db.init_db(path)
    return path


def _store_custom() -> str:
    buf = io.BytesIO()
    Image.new("RGB", (816, 1110), (10, 20, 30)).save(buf, format="PNG")
    data = buf.getvalue()
    content_hash = hashlib.sha256(data).hexdigest()
    customs.store_original(data, expected_hash=content_hash, bleed_mm=3.175)
    return content_hash


def _delver(*overrides: FaceOverride, quantity: int = 1) -> DeckEntry:
    return DeckEntry(
        quantity=quantity,
        name="Delver of Secrets",
        set_code="isd",
        collector_number="51",
        scryfall_id="delver-id",
        face_overrides=tuple(overrides),
    )


def _enqueue(entries, tmp_path, db_path, **kwargs):
    return generation.enqueue_decklist_entries(
        entries,
        model="ultrasharp_v2",
        dpi_targets=[1200],
        skip_existing=False,
        tile_size=0,
        output_dir=tmp_path / "out",
        cache_dir=tmp_path / "cache",
        weights_dir=tmp_path / "weights",
        project_tag="tag",
        db_path=db_path,
        **kwargs,
    )


@pytest.fixture
def resolves_delver(monkeypatch) -> None:
    monkeypatch.setattr(
        ScryfallClient, "resolve_many", lambda self, entries: [(DELVER, []) for _ in entries]
    )


# --- generation --------------------------------------------------------------


def test_overridden_back_skips_scryfall_and_registers_the_custom(
    db_path, tmp_path, resolves_delver
) -> None:
    content_hash = _store_custom()
    queued, failed, _ids = _enqueue(
        [_delver(FaceOverride(face=1, total_faces=2, custom_hash=content_hash))], tmp_path, db_path
    )
    assert failed == 0
    tasks = db.list_tasks(db_path=db_path)
    scryfall = [(t.face_index, t.model) for t in tasks if t.scryfall_id]
    assert scryfall == [(0, "ultrasharp_v2")]
    custom = [t for t in tasks if t.custom_hash]
    assert [(t.custom_hash, t.model) for t in custom] == [(content_hash, CUSTOM_SOURCE_MODEL)]


def test_another_line_of_the_same_printing_still_gets_the_face(
    db_path, tmp_path, resolves_delver
) -> None:
    content_hash = _store_custom()
    _enqueue(
        [_delver(FaceOverride(1, 2, content_hash)), _delver()],
        tmp_path,
        db_path,
    )
    faces = sorted(t.face_index for t in db.list_tasks(db_path=db_path) if t.scryfall_id)
    assert faces == [0, 1]


def test_custom_upscale_upscales_the_override(db_path, tmp_path, resolves_delver) -> None:
    content_hash = _store_custom()
    _enqueue(
        [_delver(FaceOverride(1, 2, content_hash))],
        tmp_path,
        db_path,
        custom_upscale="native",
    )
    models = {t.model for t in db.list_tasks(db_path=db_path) if t.custom_hash}
    assert CUSTOM_SOURCE_MODEL in models
    assert "ultrasharp_v2" in models


def test_download_skips_the_overridden_face(db_path, tmp_path, resolves_delver) -> None:
    content_hash = _store_custom()
    generation.enqueue_download_entries(
        [_delver(FaceOverride(0, 2, content_hash))],
        output_dir=tmp_path / "out",
        cache_dir=tmp_path / "cache",
        weights_dir=tmp_path / "weights",
        project_tag="tag",
        db_path=db_path,
    )
    tasks = db.list_tasks(db_path=db_path)
    assert [t.face_index for t in tasks if t.scryfall_id] == [1]
    assert [t.custom_hash for t in tasks if t.custom_hash] == [content_hash]


# --- print matching ------------------------------------------------------------


def _scryfall_face(face_index, dpi=1200, *, set_code="isd", collector="51") -> FaceResult:
    return FaceResult(
        out_path=Path(f"/o/delver-{face_index}-{dpi}.png"),
        original_path=Path(f"/c/delver-{face_index}.png"),
        scryfall_id="delver-id",
        face_index=face_index,
        face_name=["Delver of Secrets", "Insectile Aberration"][face_index or 0],
        card_name="Delver of Secrets // Insectile Aberration",
        set_code=set_code,
        collector_number=collector,
        png_url="",
        dpi=dpi,
        face_label=None if face_index is None else ("front" if face_index == 0 else "back"),
        total_faces=2,
    )


def _custom_face(content_hash=HASH, dpi=1204) -> FaceResult:
    return FaceResult(
        out_path=Path(f"/customs/{content_hash}.png"),
        original_path=Path(f"/customs/{content_hash}.png"),
        scryfall_id=None,
        face_index=None,
        face_name="Insectile (MPC)",
        card_name="Insectile (MPC)",
        set_code="",
        collector_number="",
        png_url="",
        dpi=dpi,
        model=CUSTOM_SOURCE_MODEL,
        total_faces=1,
        custom_hash=content_hash,
        custom_bleed_mm=3.175,
    )


def test_override_replaces_the_face_and_pairs_as_the_card() -> None:
    gallery = [_scryfall_face(0), _scryfall_face(1), _custom_face()]
    entry = _delver(FaceOverride(1, 2, HASH), quantity=3)
    units, missing, missing_at_dpi = match_quantities([entry], gallery, preferred_dpi=1200)
    assert missing == [] and missing_at_dpi == []
    assert len(units) == 2
    front, back = units
    assert front.best.custom_hash is None and front.best.face_index == 0
    # The custom, under the card's identity, at its own resolution.
    assert back.best.custom_hash == HASH
    assert back.best.scryfall_id == "delver-id"
    assert back.best.face_index == 1 and back.best.face_label == "back"
    assert back.best.custom_bleed_mm == pytest.approx(3.175)
    assert back.dpi_fallback is True
    assert back.quantity == 3

    slots = build_print_slots(units, pair_back_faces=True)
    assert len(slots) == 3
    assert slots[0].front.face_index == 0
    assert slots[0].reverse is not None and slots[0].reverse.custom_hash == HASH


def test_single_faced_override_replaces_the_scryfall_image() -> None:
    sol = FaceResult(
        out_path=Path("/o/sol.png"),
        original_path=Path("/c/sol.png"),
        scryfall_id="sol-id",
        face_index=None,
        face_name="Sol Ring",
        card_name="Sol Ring",
        set_code="c21",
        collector_number="263",
        png_url="",
        dpi=1200,
        total_faces=1,
    )
    entry = DeckEntry(
        quantity=2,
        name="Sol Ring",
        set_code="c21",
        collector_number="263",
        scryfall_id="sol-id",
        face_overrides=(FaceOverride(0, 1, HASH),),
    )
    units, missing, _ = match_quantities([entry], [sol, _custom_face()])
    assert missing == []
    [unit] = units
    assert unit.best.custom_hash == HASH and unit.best.face_index is None
    assert unit.quantity == 2


def test_two_lines_of_one_printing_split_on_the_override() -> None:
    gallery = [_scryfall_face(0), _scryfall_face(1), _custom_face()]
    entries = [_delver(FaceOverride(1, 2, HASH), quantity=2), _delver(quantity=1)]
    units, missing, _ = match_quantities(entries, gallery)
    assert missing == []
    backs = sorted(
        ((u.best.custom_hash is not None), u.quantity) for u in units if u.best.face_index == 1
    )
    assert backs == [(False, 1), (True, 2)]
    fronts = [u.quantity for u in units if u.best.face_index == 0]
    assert fronts == [3]


def test_an_override_that_was_never_registered_is_missing() -> None:
    entry = _delver(FaceOverride(1, 2, HASH))
    units, missing, _ = match_quantities([entry], [_scryfall_face(0), _scryfall_face(1)])
    # The Scryfall back is never a silent stand-in.
    assert all(u.best.face_index == 0 for u in units)
    assert missing and "1 of 2" in missing[0]


def test_art_chosen_but_not_downloaded_prints_nothing_for_that_face() -> None:
    gallery = [_scryfall_face(0), _scryfall_face(1), _custom_face()]
    entry = _delver(FaceOverride(1, 2, None))
    units, missing, _ = match_quantities([entry], gallery)
    # Never the Scryfall back in its place; reported instead.
    assert [u.best.face_index for u in units] == [0]
    assert missing and "1 of 2" in missing[0]


def test_generation_leaves_an_undownloaded_face_alone(db_path, tmp_path, resolves_delver) -> None:
    _enqueue([_delver(FaceOverride(1, 2, None))], tmp_path, db_path)
    tasks = db.list_tasks(db_path=db_path)
    assert [t.face_index for t in tasks if t.scryfall_id] == [0]
    assert not [t for t in tasks if t.custom_hash]


def test_the_same_image_as_a_card_and_as_an_override_prints_twice() -> None:
    gallery = [_scryfall_face(0), _scryfall_face(1), _custom_face()]
    entries = [
        _delver(FaceOverride(1, 2, HASH)),
        DeckEntry(quantity=4, name="Insectile (MPC)", custom_hash=HASH),
    ]
    units, missing, _ = match_quantities(entries, gallery)
    assert missing == []
    custom_units = sorted(
        (u.best.scryfall_id or "", u.quantity) for u in units if u.best.custom_hash
    )
    assert custom_units == [("", 4), ("delver-id", 1)]


def test_use_originals_still_prints_the_custom_source() -> None:
    gallery = [_scryfall_face(0, dpi=300), _custom_face()]
    gallery[0].model = "original"
    entry = _delver(FaceOverride(1, 2, HASH))
    units, missing, _ = match_quantities([entry], gallery, use_originals=True)
    assert missing == []
    assert {u.best.face_index for u in units} == {0, 1}


def test_converter_validates_override_hashes() -> None:
    from fastapi import HTTPException

    from proxy_scaler.api.entries import to_deck_entry
    from proxy_scaler.api.schemas import DeckEntryIn

    good = to_deck_entry(
        DeckEntryIn(
            name="Delver",
            scryfall_id="delver-id",
            face_overrides=[{"face": 1, "total_faces": 2, "custom_hash": OTHER_HASH.upper()}],
        )
    )
    assert good.face_overrides == (FaceOverride(1, 2, OTHER_HASH),)
    with pytest.raises(HTTPException):
        to_deck_entry(
            DeckEntryIn(
                name="x", face_overrides=[{"face": 0, "total_faces": 1, "custom_hash": "../etc"}]
            )
        )
    # Meaningless on a custom card: dropped.
    custom = to_deck_entry(
        DeckEntryIn(
            name="x",
            custom_hash=HASH,
            face_overrides=[{"face": 0, "total_faces": 1, "custom_hash": OTHER_HASH}],
        )
    )
    assert custom.face_overrides == ()


def test_the_api_accepts_art_chosen_but_not_downloaded(tmp_path, monkeypatch, resolves_delver) -> None:
    """A face override with no image yet (custom_hash null) is valid on the
    wire: Download images sends exactly that for an import's chosen art."""
    from fastapi.testclient import TestClient

    monkeypatch.chdir(tmp_path)
    db_path = tmp_path / "api.db"
    db.init_db(db_path)
    monkeypatch.setenv("PROXY_SCALER_DB_PATH", str(db_path))
    monkeypatch.setenv("PROXY_SCALER_WORKER_LOCK_PATH", str(tmp_path / "worker.lock"))
    monkeypatch.setenv("PROXY_SCALER_CARD_DB_PATH", str(tmp_path / "cards.db"))
    from proxy_scaler.api.app import app

    resp = TestClient(app).post(
        "/api/generate/downloads",
        json={
            "project_tag": "tag",
            "entries": [
                {
                    "quantity": 1,
                    "name": "Delver of Secrets",
                    "scryfall_id": "delver-id",
                    "face_overrides": [{"face": 1, "total_faces": 2, "custom_hash": None}],
                }
            ],
            "output_dir": str(tmp_path / "out"),
            "cache_dir": str(tmp_path / "cache"),
            "weights_dir": str(tmp_path / "weights"),
        },
    )
    assert resp.status_code == 200, resp.text
    tasks = db.list_tasks(db_path=db_path)
    assert [t.face_index for t in tasks if t.scryfall_id] == [0]
