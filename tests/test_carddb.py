"""Tests for the local Scryfall card corpus (proxy_scaler/carddb.py):
schema init, idempotent upserts, the lookup preference orders the resolver
depends on, and the variant listing that feeds the change-printing picker."""

from __future__ import annotations

from pathlib import Path

import pytest

from proxy_scaler import carddb


@pytest.fixture()
def conn(tmp_path: Path):
    path = tmp_path / "cards.db"
    carddb.init_card_db(path)
    connection = carddb.connect(path)
    yield connection
    connection.close()


def _card(**overrides) -> dict:
    """A minimal pruned card object, defaults chosen so each test overrides
    only what it is actually about."""
    card = {
        "id": "id-1",
        "oracle_id": "oracle-1",
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
    card.update(overrides)
    return card


# ---------------------------------------------------------------------------
# init / connect / open_if_ready


def test_init_is_idempotent(tmp_path: Path) -> None:
    path = tmp_path / "cards.db"
    carddb.init_card_db(path)
    carddb.init_card_db(path)  # re-run must be a no-op, not an error
    conn = carddb.connect(path)
    assert carddb.count_cards(conn) == 0
    conn.close()


def test_connect_rejects_uninitialized_file(tmp_path: Path) -> None:
    path = tmp_path / "cards.db"
    path.write_bytes(b"")  # exists but never initialized (user_version 0)
    with pytest.raises(carddb.CardDbVersionMismatch):
        carddb.connect(path)


def test_open_if_ready_missing_file_returns_none_without_creating(
    tmp_path: Path,
) -> None:
    path = tmp_path / "cards.db"
    assert carddb.open_if_ready(path) is None
    assert not path.exists()


def test_open_if_ready_uninitialized_returns_none(tmp_path: Path) -> None:
    path = tmp_path / "cards.db"
    path.write_bytes(b"")
    assert carddb.open_if_ready(path) is None


def test_open_if_ready_initialized_returns_connection(tmp_path: Path) -> None:
    path = tmp_path / "cards.db"
    carddb.init_card_db(path)
    conn = carddb.open_if_ready(path)
    assert conn is not None
    conn.close()


# ---------------------------------------------------------------------------
# upserts


def test_upsert_twice_replaces_not_duplicates(conn) -> None:
    carddb.upsert_cards(conn, [_card()])
    carddb.upsert_cards(conn, [_card(name="Lightning Bolt (fixed)")])
    assert carddb.count_cards(conn) == 1
    card = carddb.get_card_by_id(conn, "id-1")
    assert card is not None
    assert card["name"] == "Lightning Bolt (fixed)"


def test_upsert_empty_batch_is_noop(conn) -> None:
    assert carddb.upsert_cards(conn, []) == 0


def test_reversible_card_oracle_id_falls_back_to_first_face(conn) -> None:
    card = _card(
        id="id-rev",
        oracle_id=None,
        layout="reversible_card",
        card_faces=[
            {"name": "Front", "oracle_id": "oracle-face", "image_uris": {"png": "x"}},
            {"name": "Back", "oracle_id": "oracle-face", "image_uris": {"png": "y"}},
        ],
    )
    del card["oracle_id"]
    carddb.upsert_cards(conn, [card])
    row = conn.execute("SELECT oracle_id FROM cards WHERE id = 'id-rev'").fetchone()
    assert row["oracle_id"] == "oracle-face"


def test_delete_all_cards_clears_rows_and_meta(conn) -> None:
    carddb.upsert_cards(conn, [_card()])
    carddb.write_import_meta(
        conn, dataset_type="all_cards", dataset_updated_at="2026-08-19T00:00:00Z"
    )
    carddb.delete_all_cards(conn)
    assert carddb.count_cards(conn) == 0
    assert carddb.get_meta(conn) == {}


def test_write_import_meta_records_count_and_dataset(conn) -> None:
    carddb.upsert_cards(conn, [_card(), _card(id="id-2")])
    carddb.write_import_meta(
        conn, dataset_type="default_cards", dataset_updated_at="2026-08-19T00:00:00Z"
    )
    meta = carddb.get_meta(conn)
    assert meta[carddb.META_DATASET_TYPE] == "default_cards"
    assert meta[carddb.META_DATASET_UPDATED_AT] == "2026-08-19T00:00:00Z"
    assert meta[carddb.META_CARD_COUNT] == "2"
    assert meta[carddb.META_IMPORTED_AT]


# ---------------------------------------------------------------------------
# lookups


def test_set_collector_prefers_requested_lang_then_english(conn) -> None:
    carddb.upsert_cards(
        conn,
        [
            _card(id="id-en", lang="en", set="neo", collector_number="99"),
            _card(id="id-ja", lang="ja", set="neo", collector_number="99"),
            _card(id="id-de", lang="de", set="neo", collector_number="99"),
        ],
    )
    assert carddb.get_card_by_set_collector(conn, "NEO", "99", "ja")["id"] == "id-ja"
    assert carddb.get_card_by_set_collector(conn, "neo", "99")["id"] == "id-en"
    # Requested language absent → English fallback.
    assert carddb.get_card_by_set_collector(conn, "neo", "99", "ko")["id"] == "id-en"


def test_set_collector_without_english_returns_whatever_exists(conn) -> None:
    carddb.upsert_cards(
        conn, [_card(id="id-ja", lang="ja", set="sta", collector_number="1")]
    )
    assert carddb.get_card_by_set_collector(conn, "sta", "1")["id"] == "id-ja"


def test_name_lookup_is_case_insensitive(conn) -> None:
    carddb.upsert_cards(conn, [_card()])
    assert carddb.get_card_by_name(conn, "lightning bolt")["id"] == "id-1"


def test_name_lookup_matches_dfc_front_face(conn) -> None:
    carddb.upsert_cards(
        conn,
        [
            _card(
                id="id-dfc",
                name="Delver of Secrets // Insectile Aberration",
                layout="transform",
            )
        ],
    )
    assert carddb.get_card_by_name(conn, "Delver of Secrets")["id"] == "id-dfc"


def test_name_lookup_prefers_paper_highres_newest(conn) -> None:
    carddb.upsert_cards(
        conn,
        [
            _card(id="id-digital", set="mb2", digital=True, released_at="2024-01-01"),
            _card(id="id-lowres", set="old", highres_image=False, released_at="2024-01-01"),
            _card(id="id-old", set="lea", released_at="1993-08-05"),
            _card(id="id-new", set="clu", released_at="2024-02-23"),
        ],
    )
    assert carddb.get_card_by_name(conn, "Lightning Bolt")["id"] == "id-new"


def test_name_lookup_prefers_requested_lang(conn) -> None:
    carddb.upsert_cards(
        conn,
        [
            _card(id="id-en", lang="en"),
            _card(id="id-ja", lang="ja", set="sta"),
        ],
    )
    assert carddb.get_card_by_name(conn, "Lightning Bolt", "ja")["id"] == "id-ja"


def test_name_and_collector_matches_across_sets(conn) -> None:
    carddb.upsert_cards(
        conn,
        [
            _card(id="id-lea", set="lea", collector_number="161"),
            _card(id="id-sta", set="sta", collector_number="116", released_at="2021-04-23"),
        ],
    )
    row = carddb.find_row_by_name_and_collector(conn, "lightning bolt", "116")
    assert row is not None and row["id"] == "id-sta"
    assert carddb.find_row_by_name_and_collector(conn, "Lightning Bolt", "999") is None


def test_name_and_collector_prefers_lang_then_english(conn) -> None:
    carddb.upsert_cards(
        conn,
        [
            _card(id="id-en", set="sta", collector_number="116", lang="en"),
            _card(id="id-ja", set="sta", collector_number="116", lang="ja"),
        ],
    )
    ja = carddb.find_row_by_name_and_collector(conn, "Lightning Bolt", "116", "ja")
    assert ja["id"] == "id-ja"
    fallback = carddb.find_row_by_name_and_collector(conn, "Lightning Bolt", "116", "ko")
    assert fallback["id"] == "id-en"


def test_name_and_collector_matches_dfc_front_face(conn) -> None:
    carddb.upsert_cards(
        conn,
        [
            _card(
                id="id-dfc",
                name="Delver of Secrets // Insectile Aberration",
                collector_number="51",
                layout="transform",
            )
        ],
    )
    row = carddb.find_row_by_name_and_collector(conn, "Delver of Secrets", "51")
    assert row is not None and row["id"] == "id-dfc"


def test_distinct_languages_english_first(conn) -> None:
    carddb.upsert_cards(
        conn,
        [
            _card(id="a", lang="ja"),
            _card(id="b", lang="de"),
            _card(id="c", lang="en"),
        ],
    )
    assert carddb.distinct_languages(conn) == ["en", "de", "ja"]


# ---------------------------------------------------------------------------
# variants


def test_variants_sorted_newest_release_then_natural_collector(conn) -> None:
    carddb.upsert_cards(
        conn,
        [
            _card(id="v-old", set="lea", collector_number="161", released_at="1993-08-05"),
            _card(id="v-2", set="sta", collector_number="2", released_at="2021-04-23"),
            _card(id="v-10", set="sta", collector_number="10", released_at="2021-04-23"),
            _card(id="v-new", set="clu", collector_number="141", released_at="2024-02-23"),
        ],
    )
    variants = carddb.variants_for_oracle_id(conn, "oracle-1")
    assert [v["id"] for v in variants] == ["v-new", "v-2", "v-10", "v-old"]


def test_variants_english_before_other_langs_of_same_printing(conn) -> None:
    carddb.upsert_cards(
        conn,
        [
            _card(id="v-ja", lang="ja", set="sta", collector_number="1"),
            _card(id="v-en", lang="en", set="sta", collector_number="1"),
        ],
    )
    variants = carddb.variants_for_oracle_id(conn, "oracle-1")
    assert [v["id"] for v in variants] == ["v-en", "v-ja"]


def test_variants_exclude_digital_by_default(conn) -> None:
    carddb.upsert_cards(
        conn,
        [
            _card(id="v-paper"),
            _card(id="v-digital", set="mb2", digital=True),
        ],
    )
    assert [v["id"] for v in carddb.variants_for_oracle_id(conn, "oracle-1")] == [
        "v-paper"
    ]
    both = carddb.variants_for_oracle_id(conn, "oracle-1", include_digital=True)
    assert {v["id"] for v in both} == {"v-paper", "v-digital"}


def test_variants_missing_release_date_sorts_last(conn) -> None:
    carddb.upsert_cards(
        conn,
        [
            _card(id="v-dated"),
            _card(id="v-undated", set="xxx", released_at=None),
        ],
    )
    variants = carddb.variants_for_oracle_id(conn, "oracle-1")
    assert [v["id"] for v in variants] == ["v-dated", "v-undated"]


def test_collector_sort_key_natural_order() -> None:
    numbers = ["100b", "2", "10", "100a", "A-1"]
    ordered = sorted(numbers, key=carddb.collector_sort_key)
    assert ordered == ["2", "10", "100a", "100b", "A-1"]


def test_upsert_extracts_printed_name_column(conn) -> None:
    carddb.upsert_cards(
        conn,
        [_card(id="aang-de", printed_name="Aang der Luftnomade", lang="de")],
    )
    row = conn.execute("SELECT printed_name FROM cards WHERE id = 'aang-de'").fetchone()
    assert row["printed_name"] == "Aang der Luftnomade"


def test_dfc_printed_name_composes_from_faces(conn) -> None:
    card = _card(
        id="delver-de",
        name="Delver of Secrets // Insectile Aberration",
        lang="de",
        layout="transform",
        card_faces=[
            {"name": "Delver of Secrets", "printed_name": "Delver der Geheimnisse",
             "image_uris": {"png": "x"}},
            {"name": "Insectile Aberration", "printed_name": "Insektoide Abartigkeit",
             "image_uris": {"png": "y"}},
        ],
    )
    del card["image_uris"]
    carddb.upsert_cards(conn, [card])
    # Printed front-face name alone finds the card, like the English form.
    row = carddb.find_row_by_name(conn, "Delver der Geheimnisse")
    assert row is not None and row["id"] == "delver-de"
    assert row["printed_name"] == "Delver der Geheimnisse // Insektoide Abartigkeit"


def test_init_card_db_rebuilds_outdated_schema(tmp_path: Path) -> None:
    """A v1 corpus (pre-printed_name) is rebuilt from scratch by the next
    import's init — reimport is the upgrade path, not a migration."""
    import sqlite3

    path = tmp_path / "cards.db"
    conn = sqlite3.connect(str(path))
    conn.execute("CREATE TABLE cards (id TEXT PRIMARY KEY)")  # old, shapeless
    conn.execute("INSERT INTO cards VALUES ('stale')")
    conn.execute("PRAGMA user_version = 1")
    conn.commit()
    conn.close()

    assert carddb.open_if_ready(path) is None  # v1 reads as "no corpus"
    carddb.init_card_db(path)
    conn = carddb.connect(path)
    try:
        assert carddb.count_cards(conn) == 0  # stale rows gone, new shape
    finally:
        conn.close()
