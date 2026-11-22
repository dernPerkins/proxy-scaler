"""Back Card (docs/adr/0011): one face of a different card printed on an
entry's Reverse. Unlike a face override it keeps its own identity: it is
fetched and reported under its own name, never counts as a card, and
rides on the front's print slot."""

from __future__ import annotations

from pathlib import Path

import pytest

from proxy_scaler import carddb, db
from proxy_scaler.decklist import BackCard, DeckEntry
from proxy_scaler.pdf_layout import (
    ReverseFill,
    build_print_slots,
    match_quantities,
    resolve_reverse,
    reverse_is_blank,
)
from proxy_scaler.pipeline import FaceResult
from proxy_scaler.scryfall import ScryfallClient
from proxy_scaler.services import generation

ELVES_ID = "11111111-1111-1111-1111-111111111111"
TREASURE_ID = "22222222-2222-2222-2222-222222222222"
CLUE_ID = "33333333-3333-3333-3333-333333333333"

ELVES = {
    "id": ELVES_ID,
    "name": "Llanowar Elves",
    "set": "m19",
    "collector_number": "314",
    "layout": "normal",
    "image_status": "highres_scan",
    "image_uris": {"png": "https://example.com/elves.png"},
}
TREASURE = {
    "id": TREASURE_ID,
    "name": "Treasure",
    "set": "tm19",
    "collector_number": "1",
    "layout": "token",
    "image_status": "highres_scan",
    "image_uris": {"png": "https://example.com/treasure.png"},
}


@pytest.fixture(autouse=True)
def _no_default_card_corpus(tmp_path: Path, monkeypatch) -> None:
    monkeypatch.setattr(carddb, "DEFAULT_CARD_DB_PATH", tmp_path / "no-corpus.db")


@pytest.fixture
def db_path(tmp_path: Path, monkeypatch) -> Path:
    monkeypatch.chdir(tmp_path)
    path = tmp_path / "test.db"
    db.init_db(path)
    return path


@pytest.fixture
def resolves(monkeypatch) -> None:
    by_id = {ELVES_ID: ELVES, TREASURE_ID: TREASURE}
    monkeypatch.setattr(
        ScryfallClient,
        "resolve_many",
        lambda self, entries: [(by_id[e.scryfall_id], []) for e in entries],
    )


def _treasure_back(name: str = "Treasure") -> BackCard:
    return BackCard(TREASURE_ID, 0, name)


def _elves(back: BackCard | None = None, *, quantity: int = 1) -> DeckEntry:
    return DeckEntry(
        quantity=quantity,
        name="Llanowar Elves",
        set_code="m19",
        collector_number="314",
        scryfall_id=ELVES_ID,
        back_mode="card" if back else "inherit",
        back_card=back,
    )


def _face(
    scryfall_id: str, name: str, set_code: str, collector: str, dpi: int = 1200, model=None
) -> FaceResult:
    kwargs = {"model": model} if model else {}
    return FaceResult(
        out_path=Path(f"/o/{name.lower().replace(' ', '-')}-{dpi}.png"),
        original_path=Path(f"/c/{name}.png"),
        scryfall_id=scryfall_id,
        face_index=None,
        face_name=name,
        card_name=name,
        set_code=set_code,
        collector_number=collector,
        png_url="",
        dpi=dpi,
        total_faces=1,
        **kwargs,
    )


def _elves_face(dpi: int = 1200) -> FaceResult:
    return _face(ELVES_ID, "Llanowar Elves", "m19", "314", dpi)


def _treasure_face(dpi: int = 1200) -> FaceResult:
    return _face(TREASURE_ID, "Treasure", "tm19", "1", dpi)


# --- wire --------------------------------------------------------------------


def test_converter_takes_a_back_card() -> None:
    from fastapi import HTTPException

    from proxy_scaler.api.entries import to_deck_entry
    from proxy_scaler.api.schemas import DeckEntryIn

    entry = to_deck_entry(
        DeckEntryIn(
            name="Llanowar Elves",
            scryfall_id=ELVES_ID,
            back_mode="card",
            back_card={"scryfall_id": TREASURE_ID.upper(), "name": "Treasure"},
        )
    )
    assert entry.back_mode == "card"
    assert entry.back_card == _treasure_back()

    # "card" with nothing to name follows the project.
    bare = to_deck_entry(DeckEntryIn(name="Llanowar Elves", back_mode="card"))
    assert (bare.back_mode, bare.back_card) == ("inherit", None)
    # A back card sent in another mode is ignored.
    stale = to_deck_entry(
        DeckEntryIn(name="x", back_mode="blank", back_card={"scryfall_id": TREASURE_ID})
    )
    assert (stale.back_mode, stale.back_card) == ("blank", None)

    with pytest.raises(HTTPException) as info:
        to_deck_entry(
            DeckEntryIn(name="x", back_mode="card", back_card={"scryfall_id": "../../etc"})
        )
    assert info.value.status_code == 400


# --- generation --------------------------------------------------------------


def test_download_fetches_the_back_card_once_under_its_own_name(
    db_path, tmp_path, resolves
) -> None:
    queued, failed, _ids = generation.enqueue_download_entries(
        [_elves(_treasure_back()), _elves(_treasure_back(), quantity=3), _elves()],
        output_dir=tmp_path / "out",
        cache_dir=tmp_path / "cache",
        weights_dir=tmp_path / "weights",
        project_tag="tag",
        db_path=db_path,
    )
    assert failed == 0
    tasks = db.list_tasks(db_path=db_path)
    assert sorted(t.scryfall_id for t in tasks) == sorted([ELVES_ID, TREASURE_ID])


def test_a_back_card_in_another_mode_is_not_fetched(db_path, tmp_path, resolves) -> None:
    entry = DeckEntry(
        quantity=1, name="Llanowar Elves", scryfall_id=ELVES_ID,
        back_mode="blank", back_card=_treasure_back(),
    )
    generation.enqueue_download_entries(
        [entry],
        output_dir=tmp_path / "out",
        cache_dir=tmp_path / "cache",
        weights_dir=tmp_path / "weights",
        project_tag="tag",
        db_path=db_path,
    )
    assert [t.scryfall_id for t in db.list_tasks(db_path=db_path)] == [ELVES_ID]


# --- print matching ----------------------------------------------------------


def test_the_back_card_rides_on_the_front_and_adds_no_card() -> None:
    gallery = [_elves_face(), _treasure_face()]
    units, missing, missing_at_dpi = match_quantities(
        [_elves(_treasure_back(), quantity=3)], gallery, preferred_dpi=1200
    )
    assert missing == [] and missing_at_dpi == []
    (unit,) = units
    assert unit.quantity == 3
    assert unit.best.card_name == "Llanowar Elves"
    # Its own identity, not dressed as the Elves.
    assert unit.back_face is not None
    assert unit.back_face.scryfall_id == TREASURE_ID
    assert unit.back_face.card_name == "Treasure"

    for pair in (True, False):
        slots = build_print_slots(units, pair_back_faces=pair)
        assert len(slots) == 3
        assert all(s.reverse is unit.back_face for s in slots)
        path, face, back = resolve_reverse(slots[0], {}, None, ReverseFill.BLANK)
        assert path == Path("/o/treasure-1200.png") and face is unit.back_face and back is None
        assert not reverse_is_blank(slots[0], ReverseFill.BLANK)


def test_lines_with_different_backs_stay_apart() -> None:
    clue = BackCard(CLUE_ID, 0, "Clue")
    gallery = [_elves_face(), _treasure_face(), _face(CLUE_ID, "Clue", "tm19", "2")]
    entries = [_elves(_treasure_back(), quantity=2), _elves(clue), _elves(quantity=4)]
    units, missing, _ = match_quantities(entries, gallery)
    assert missing == []
    assert [(u.quantity, u.back_face.card_name if u.back_face else None) for u in units] == [
        (2, "Treasure"),
        (1, "Clue"),
        (4, None),
    ]


def test_a_standalone_token_line_is_untouched() -> None:
    token = DeckEntry(
        quantity=2, name="Treasure", set_code="tm19", collector_number="1",
        scryfall_id=TREASURE_ID,
    )
    units, missing, _ = match_quantities(
        [_elves(_treasure_back()), token], [_elves_face(), _treasure_face()]
    )
    assert missing == []
    assert [(u.best.card_name, u.quantity) for u in units] == [
        ("Llanowar Elves", 1),
        ("Treasure", 2),
    ]
    assert len(build_print_slots(units, pair_back_faces=True)) == 3


def test_a_back_card_never_generated_is_missing_by_its_own_name() -> None:
    missing_entries: list[int] = []
    units, missing, _ = match_quantities(
        [_elves(), _elves(_treasure_back())],
        [_elves_face()],
        missing_entries=missing_entries,
    )
    assert missing == ["Treasure"]
    assert missing_entries == [1]
    back_unit = next(u for u in units if u.back_mode == "card")
    assert back_unit.back_face is None
    (slot,) = build_print_slots([back_unit], pair_back_faces=True)
    # Nothing prints there: it never falls back to the Selected Back.
    assert resolve_reverse(slot, {}, "default", ReverseFill.BACK_IMAGE) == (None, None, None)
    assert not reverse_is_blank(slot, ReverseFill.BLANK)


def test_a_back_card_without_the_requested_dpi_and_no_original_is_reported() -> None:
    from proxy_scaler.pdf_layout import ORIGINAL_MODEL

    assert ORIGINAL_MODEL  # the fallback this test leaves out
    missing_at_dpi_entries: list[int] = []
    units, missing, missing_at_dpi = match_quantities(
        [_elves(_treasure_back())],
        [_elves_face(1200), _treasure_face(600)],
        preferred_dpi=1200,
        missing_at_dpi_entries=missing_at_dpi_entries,
    )
    assert missing == []
    assert len(missing_at_dpi) == 1 and "Treasure" in missing_at_dpi[0]
    assert missing_at_dpi_entries == [0]
    assert units[0].back_face is None


def test_copy_order_keeps_each_copys_back() -> None:
    gallery = [_elves_face(), _treasure_face()]
    entries = [_elves(_treasure_back(), quantity=2), _elves(quantity=1)]
    units, _, _ = match_quantities(entries, gallery)
    slots = build_print_slots(units, pair_back_faces=True, copy_order=[0, 1, 0])
    assert [s.reverse.card_name if s.reverse else None for s in slots] == [
        "Treasure",
        None,
        "Treasure",
    ]


# --- routes' helpers (no HTTP client needed) ----------------------------------


def test_print_gallery_pulls_in_the_back_cards_face(db_path, tmp_path) -> None:
    from proxy_scaler.api.routers.pdf import gallery_for_print

    face = _treasure_face()
    out = tmp_path / "treasure.png"
    out.write_bytes(b"png")
    face.out_path = out
    # Made by another project: this one has never generated it.
    db.upsert_gallery_item("elsewhere", face, db_path=db_path)

    assert gallery_for_print("tag", [_elves()], db_path) == []
    items = gallery_for_print("tag", [_elves(_treasure_back())], db_path)
    assert [i.scryfall_id for i in items] == [TREASURE_ID]


def test_the_default_zip_ships_each_back_card_once() -> None:
    from proxy_scaler.api.routers.export import _default_backs

    gallery = [_elves_face(), _treasure_face()]
    entries = [_elves(_treasure_back(), quantity=2), _elves()]
    units, _, _ = match_quantities(entries, gallery)
    slots = build_print_slots(units, pair_back_faces=False)
    backs = _default_backs(slots, {}, None, ReverseFill.BLANK)
    assert [(path, face.card_name if face else None, back) for path, face, back in backs] == [
        (Path("/o/treasure-1200.png"), "Treasure", None)
    ]
