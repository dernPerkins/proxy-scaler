"""Tests for card-name search (proxy_scaler/card_search.py): the match
tiers, popularity ranking within a tier, folding, double-faced names,
typo correction and rebuilding the index after a reimport."""

from __future__ import annotations

from pathlib import Path

import pytest

from proxy_scaler import card_search, carddb


@pytest.fixture(autouse=True)
def _fresh_cache():
    card_search.clear_cache()
    yield
    card_search.clear_cache()


@pytest.fixture()
def db(tmp_path: Path):
    path = tmp_path / "cards.db"
    carddb.init_card_db(path)
    conn = carddb.connect(path)
    yield conn, path
    conn.close()


_serial = 0


def _card(name: str, oracle_id: str | None = None, **overrides) -> dict:
    global _serial
    _serial += 1
    card = {
        "id": f"id-{_serial}",
        "oracle_id": oracle_id or f"oracle-{name}",
        "name": name,
        "lang": "en",
        "set": "set",
        "set_name": "Set",
        "collector_number": str(_serial),
        "released_at": "2020-01-01",
        "layout": "normal",
        "digital": False,
        "image_status": "highres_scan",
        "highres_image": True,
        "image_uris": {"png": "https://img.example/card.png"},
    }
    card.update(overrides)
    return card


def _printings(name: str, count: int) -> list[dict]:
    return [_card(name) for _ in range(count)]


def _seed(conn, cards: list[dict]) -> None:
    carddb.upsert_cards(conn, cards)
    carddb.write_import_meta(conn, dataset_type="default_cards", dataset_updated_at="x")


def _names(db, query: str, **kwargs) -> list[str]:
    conn, path = db
    return [hit.name for hit in card_search.search(conn, path, query, **kwargs)]


def test_partial_words_in_order_beat_a_substring(db) -> None:
    _seed(db[0], [_card("Sol Ring"), _card("Sorin Markov"), _card("Lightning Bolt")])
    assert _names(db, "so rin") == ["Sol Ring", "Sorin Markov"]
    assert _names(db, "lig bol") == ["Lightning Bolt"]


def test_more_printings_rank_first_within_a_tier(db) -> None:
    _seed(db[0], _printings("Sol Talisman", 1) + _printings("Sol Ring", 5))
    assert _names(db, "sol") == ["Sol Ring", "Sol Talisman"]


def test_exact_and_prefix_beat_popularity(db) -> None:
    _seed(db[0], _printings("Bolt Bend", 1) + _printings("Lightning Bolt", 9))
    assert _names(db, "bolt") == ["Bolt Bend", "Lightning Bolt"]


def test_no_spaces_matches_a_spaced_name(db) -> None:
    _seed(db[0], [_card("Sol Ring")])
    assert _names(db, "solring") == ["Sol Ring"]


def test_accents_apostrophes_and_punctuation_fold(db) -> None:
    _seed(db[0], [_card("Lim-Dûl's Vault"), _card("Borborygmos, Enraged")])
    assert _names(db, "lim dul") == ["Lim-Dûl's Vault"]
    assert _names(db, "LIMDULS") == ["Lim-Dûl's Vault"]
    assert _names(db, "borborygmos enr") == ["Borborygmos, Enraged"]


def test_either_face_of_a_double_faced_card_finds_it(db) -> None:
    _seed(db[0], [_card("Delver of Secrets // Insectile Aberration", layout="transform")])
    assert _names(db, "insectile") == ["Delver of Secrets // Insectile Aberration"]
    assert _names(db, "delver") == ["Delver of Secrets // Insectile Aberration"]


def test_a_typo_is_corrected(db) -> None:
    _seed(db[0], [_card("Sol Ring"), _card("Counterspell")])
    assert _names(db, "sol rnig") == ["Sol Ring"]
    assert _names(db, "conterspell") == ["Counterspell"]


def test_tokens_and_digital_only_cards_sort_last(db) -> None:
    _seed(
        db[0],
        [
            _card("Treasure", layout="token"),
            _card("Treasure Hunt", digital=True),
            _card("Treasure Trove"),
        ],
    )
    assert _names(db, "treasure") == ["Treasure Trove", "Treasure", "Treasure Hunt"]


def test_art_series_cards_are_left_out(db) -> None:
    _seed(db[0], [_card("Sol Ring"), _card("Sol Ring // Sol Ring", "art", layout="art_series")])
    assert _names(db, "sol ring") == ["Sol Ring"]


def test_printed_names_match_in_their_language(db) -> None:
    _seed(
        db[0],
        [_card("Sol Ring", "sol"), _card("Sol Ring", "sol", lang="de", printed_name="Sonnenring")],
    )
    assert _names(db, "sonnenring") == []
    assert _names(db, "sonnenring", lang="de") == ["Sol Ring"]


def test_empty_query_finds_nothing(db) -> None:
    _seed(db[0], [_card("Sol Ring")])
    assert _names(db, "  ,  ") == []


def test_limit(db) -> None:
    _seed(db[0], [_card(f"Goblin {n}") for n in ("Guide", "Bombardment", "Lackey")])
    assert len(_names(db, "goblin", limit=2)) == 2


def test_a_reimport_rebuilds_the_index(db) -> None:
    conn, _ = db
    _seed(conn, [_card("Sol Ring")])
    assert _names(db, "talisman") == []
    conn.execute("UPDATE card_dataset_meta SET value = 'later' WHERE key = 'imported_at'")
    carddb.upsert_cards(conn, [_card("Sol Talisman")])
    assert _names(db, "talisman") == ["Sol Talisman"]
