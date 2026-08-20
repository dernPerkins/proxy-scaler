"""Tests for local-first resolution (proxy_scaler/card_lookup.py): local
hits must produce zero HTTP traffic, misses must fall through to the live
client with today's exact semantics, and the two must partition cleanly
within one batch."""

from __future__ import annotations

from pathlib import Path

import pytest

from proxy_scaler import carddb
from proxy_scaler.card_lookup import CardResolver
from proxy_scaler.decklist import DeckEntry
from proxy_scaler.scryfall import ScryfallError


def _seed(tmp_path: Path, cards: list[dict]) -> None:
    path = tmp_path / "cards.db"
    carddb.init_card_db(path)
    conn = carddb.connect(path)
    try:
        carddb.upsert_cards(conn, cards)
    finally:
        conn.close()


def _card(**overrides) -> dict:
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
    card.update(overrides)
    return card


class _NoNetworkAllowed:
    """client_factory that fails the test if the resolver ever goes live."""

    def __call__(self):
        raise AssertionError("CardResolver constructed a live client for a local hit")


class _FakeClient:
    """Stands in for ScryfallClient on the fallback path, recording calls."""

    def __init__(self, by_key: dict | None = None, by_lang_key: dict | None = None):
        self.by_key = by_key or {}
        self.by_lang_key = by_lang_key or {}
        self.resolve_many_calls: list[list[DeckEntry]] = []
        self.fetch_calls: list[tuple] = []

    def resolve_many(self, entries):
        self.resolve_many_calls.append(entries)
        out = []
        for e in entries:
            key = (e.set_code, e.collector_number) if e.has_exact_printing else e.name
            card = self.by_key.get(key)
            if card is None:
                out.append(ScryfallError(f"not found: {key}"))
            else:
                out.append((card, []))
        return out

    def fetch_by_set_collector(self, set_code, collector, lang=None):
        self.fetch_calls.append((set_code, collector, lang))
        card = self.by_lang_key.get((set_code, collector, lang))
        if card is None:
            raise ScryfallError(f"not found: {set_code}/{collector}/{lang}")
        return card


def _resolver(tmp_path: Path, client=None) -> CardResolver:
    factory = (lambda: client) if client is not None else _NoNetworkAllowed()
    return CardResolver(card_db_path=tmp_path / "cards.db", client_factory=factory)


def _entry(**overrides) -> DeckEntry:
    kwargs = dict(quantity=1, name="Lightning Bolt", raw_line="1 Lightning Bolt")
    kwargs.update(overrides)
    return DeckEntry(**kwargs)


# ---------------------------------------------------------------------------
# local hits — zero network


def test_local_hit_by_scryfall_id_no_network(tmp_path: Path) -> None:
    _seed(tmp_path, [_card()])
    resolver = _resolver(tmp_path)
    card, warnings = resolver.resolve(_entry(scryfall_id="bolt-lea"))
    assert card["id"] == "bolt-lea"
    assert warnings == []


def test_local_hit_by_set_collector_no_network(tmp_path: Path) -> None:
    _seed(tmp_path, [_card()])
    resolver = _resolver(tmp_path)
    card, warnings = resolver.resolve(_entry(set_code="lea", collector_number="161"))
    assert card["id"] == "bolt-lea"
    assert warnings == []


def test_local_hit_by_name_no_network_carries_note(tmp_path: Path) -> None:
    _seed(tmp_path, [_card()])
    resolver = _resolver(tmp_path)
    card, warnings = resolver.resolve(_entry())
    assert card["id"] == "bolt-lea"
    # Same note the live name-only path produces, so the client renders
    # both identically.
    assert len(warnings) == 1 and "Name-only lookup resolved" in warnings[0]


def test_local_id_beats_set_collector(tmp_path: Path) -> None:
    _seed(
        tmp_path,
        [_card(), _card(id="bolt-sta", set="sta", collector_number="116")],
    )
    resolver = _resolver(tmp_path)
    card, _ = resolver.resolve(
        _entry(scryfall_id="bolt-sta", set_code="lea", collector_number="161")
    )
    assert card["id"] == "bolt-sta"


def test_local_set_collector_prefers_entry_lang(tmp_path: Path) -> None:
    _seed(
        tmp_path,
        [
            _card(id="bolt-en", set="sta", collector_number="116", lang="en"),
            _card(id="bolt-ja", set="sta", collector_number="116", lang="ja"),
        ],
    )
    resolver = _resolver(tmp_path)
    card, _ = resolver.resolve(
        _entry(set_code="sta", collector_number="116", lang="ja")
    )
    assert card["id"] == "bolt-ja"
    # Absent language falls back to English rather than missing.
    card, _ = resolver.resolve(
        _entry(set_code="sta", collector_number="116", lang="ko")
    )
    assert card["id"] == "bolt-en"


def test_local_mismatched_name_warns_like_live(tmp_path: Path) -> None:
    _seed(tmp_path, [_card()])
    resolver = _resolver(tmp_path)
    _, warnings = resolver.resolve(
        _entry(name="Storm Crow", set_code="lea", collector_number="161")
    )
    assert len(warnings) == 1 and "Name mismatch" in warnings[0]


def test_collector_hint_matches_printing_across_sets(tmp_path: Path) -> None:
    """"1 Sol Ring 263"-style lines (no set code — the best some deck
    managers export, notably for non-English cards): the trailing number is
    matched against the name's printings, any set."""
    _seed(
        tmp_path,
        [
            _card(),  # lea/161
            _card(id="bolt-sta", set="sta", collector_number="116", released_at="2021-04-23"),
        ],
    )
    resolver = _resolver(tmp_path)
    card, warnings = resolver.resolve(_entry(collector_number="116"))
    assert card["id"] == "bolt-sta"
    assert len(warnings) == 1 and "Collector-number lookup resolved" in warnings[0]


def test_collector_hint_prefers_entry_lang(tmp_path: Path) -> None:
    _seed(
        tmp_path,
        [
            _card(id="bolt-en", set="sta", collector_number="116", lang="en"),
            _card(id="bolt-ja", set="sta", collector_number="116", lang="ja"),
        ],
    )
    resolver = _resolver(tmp_path)
    card, _ = resolver.resolve(_entry(collector_number="116", lang="ja"))
    assert card["id"] == "bolt-ja"


def test_collector_hint_miss_falls_back_to_latest_by_name_with_warning(
    tmp_path: Path,
) -> None:
    """A bogus/stale hint degrades to the plain name-only behavior —
    "latest printing of the name", language-preferred — with an explicit
    warning instead of a failure."""
    _seed(
        tmp_path,
        [
            _card(),  # lea/161, 1993
            _card(id="bolt-clu", set="clu", collector_number="141", released_at="2024-02-23"),
        ],
    )
    resolver = _resolver(tmp_path)
    card, warnings = resolver.resolve(_entry(collector_number="999"))
    assert card["id"] == "bolt-clu"  # newest printing wins, hint ignored
    assert len(warnings) == 1
    assert "Collector number 999 not found" in warnings[0]
    assert "clu/141" in warnings[0]


def test_collector_hint_number_that_is_part_of_the_name(tmp_path: Path) -> None:
    """If the stripped name + hint match nothing but the *unsplit* line is
    a real card name, the recombined name wins over the name-only
    fallback."""
    _seed(
        tmp_path,
        [
            _card(id="weird-7", name="Assembly Hall 7", collector_number="42"),
            _card(id="hall", name="Assembly Hall", collector_number="1"),
        ],
    )
    resolver = _resolver(tmp_path)
    # Parses as name="Assembly Hall", hint="7"; no printing of Assembly
    # Hall has collector 7, but "Assembly Hall 7" exists as a card.
    card, _ = resolver.resolve(_entry(name="Assembly Hall", collector_number="7"))
    assert card["id"] == "weird-7"


def test_collector_hint_goes_live_as_name_only_with_note(tmp_path: Path) -> None:
    """No corpus: the hint can't be expressed to the live API (fuzzy name
    only), so the entry resolves like a name-only line and the warning says
    the hint went unused."""
    live = _FakeClient(by_key={"Lightning Bolt": _card()})
    resolver = _resolver(tmp_path, client=live)
    card, warnings = resolver.resolve(_entry(collector_number="116"))
    assert card["id"] == "bolt-lea"
    assert any("can't be matched without a local card database" in w for w in warnings)


# ---------------------------------------------------------------------------
# fallbacks


def test_no_corpus_everything_falls_through(tmp_path: Path) -> None:
    live = _FakeClient(by_key={("lea", "161"): _card()})
    resolver = _resolver(tmp_path, client=live)
    entry = _entry(set_code="lea", collector_number="161")
    card, _ = resolver.resolve(entry)
    assert card["id"] == "bolt-lea"
    assert live.resolve_many_calls == [[entry]]


def test_pinned_id_missing_locally_goes_live_not_set_collector(tmp_path: Path) -> None:
    """A pinned id absent from the corpus must NOT be silently answered by
    the local set/collector row (potentially a different printing) — it
    falls through to the live client."""
    _seed(tmp_path, [_card()])  # corpus has the en row for lea/161
    live = _FakeClient(by_key={("lea", "161"): _card(id="bolt-newer")})
    resolver = _resolver(tmp_path, client=live)
    card, _ = resolver.resolve(
        _entry(scryfall_id="bolt-newer", set_code="lea", collector_number="161")
    )
    assert card["id"] == "bolt-newer"
    assert len(live.resolve_many_calls) == 1


def test_mixed_batch_partitions_local_and_live(tmp_path: Path) -> None:
    _seed(tmp_path, [_card()])
    live = _FakeClient(by_key={("sta", "116"): _card(id="bolt-sta", set="sta")})
    resolver = _resolver(tmp_path, client=live)
    entries = [
        _entry(set_code="lea", collector_number="161"),  # local hit
        _entry(set_code="sta", collector_number="116"),  # local miss -> live
    ]
    results = resolver.resolve_many(entries)
    assert results[0][0]["id"] == "bolt-lea"
    assert results[1][0]["id"] == "bolt-sta"
    # Only the miss went to the live batch.
    assert [e.set_code for e in live.resolve_many_calls[0]] == ["sta"]


def test_non_english_miss_uses_lang_endpoint_with_english_fallback(
    tmp_path: Path,
) -> None:
    carddb.init_card_db(tmp_path / "cards.db")  # corpus exists but is empty
    ja_card = _card(id="bolt-ja", lang="ja")
    live = _FakeClient(by_lang_key={("lea", "161", "ja"): ja_card})
    resolver = _resolver(tmp_path, client=live)
    card, _ = resolver.resolve(
        _entry(set_code="lea", collector_number="161", lang="ja")
    )
    assert card["id"] == "bolt-ja"
    assert live.fetch_calls == [("lea", "161", "ja")]
    assert live.resolve_many_calls == []  # not batched: collection can't say lang

    # Language printing doesn't exist -> retried without lang.
    live2 = _FakeClient(by_lang_key={("lea", "161", None): _card()})
    resolver2 = _resolver(tmp_path, client=live2)
    card, _ = resolver2.resolve(
        _entry(set_code="lea", collector_number="161", lang="ja")
    )
    assert card["id"] == "bolt-lea"
    assert live2.fetch_calls == [("lea", "161", "ja"), ("lea", "161", None)]


def test_live_failure_surfaces_as_entry_error(tmp_path: Path) -> None:
    live = _FakeClient()
    resolver = _resolver(tmp_path, client=live)
    results = resolver.resolve_many([_entry(set_code="xxx", collector_number="1")])
    assert isinstance(results[0], ScryfallError)
    with pytest.raises(ScryfallError):
        resolver.resolve(_entry(set_code="xxx", collector_number="1"))


# ---------------------------------------------------------------------------
# printed names & strict language mode


def test_local_name_lookup_matches_printed_name(tmp_path: Path) -> None:
    _seed(
        tmp_path,
        [
            _card(
                id="aang-de",
                name="Aang, Air Nomad",
                printed_name="Aang der Luftnomade",
                lang="de",
                set="tle",
                collector_number="210",
            )
        ],
    )
    resolver = _resolver(tmp_path)
    card, _ = resolver.resolve(_entry(name="Aang der Luftnomade"))
    assert card["id"] == "aang-de"
    # And with the collector hint too.
    card, _ = resolver.resolve(
        _entry(name="Aang der Luftnomade", collector_number="210")
    )
    assert card["id"] == "aang-de"


def test_printed_name_match_does_not_warn_name_mismatch(tmp_path: Path) -> None:
    _seed(
        tmp_path,
        [
            _card(
                id="aang-de",
                name="Aang, Air Nomad",
                printed_name="Aang der Luftnomade",
                lang="de",
                set="tle",
                collector_number="210",
            )
        ],
    )
    resolver = _resolver(tmp_path)
    _, warnings = resolver.resolve(
        _entry(name="Aang der Luftnomade", set_code="tle", collector_number="210", lang="de")
    )
    assert warnings == []


def test_strict_lang_local_requires_exact_language(tmp_path: Path) -> None:
    _seed(
        tmp_path,
        [
            _card(id="bolt-en", set="sta", collector_number="116", lang="en"),
            _card(id="bolt-ja", set="sta", collector_number="116", lang="ja"),
        ],
    )
    resolver = _resolver(tmp_path)
    card, _ = resolver.resolve(
        _entry(set_code="sta", collector_number="116", lang="ja"), strict_lang=True
    )
    assert card["id"] == "bolt-ja"

    # Demanded language absent locally -> local miss -> live path; the
    # strict live path only asks for that language and errors when it
    # doesn't exist (no English substitute).
    live = _FakeClient()  # answers nothing
    resolver = _resolver(tmp_path, client=live)
    results = resolver.resolve_many(
        [_entry(set_code="sta", collector_number="116", lang="ko")], strict_lang=True
    )
    assert isinstance(results[0], ScryfallError)
    assert "No ko version" in str(results[0])
    assert live.fetch_calls == [("sta", "116", "ko")]  # no bare retry


def test_strict_lang_rejects_fuzzy_match_in_other_language(tmp_path: Path) -> None:
    """Strictly literal: an English name resolving (via live fuzzy) to an
    English printing is a failure when the import demanded German."""
    live = _FakeClient(by_key={"Lightning Bolt": _card()})  # en object
    resolver = _resolver(tmp_path, client=live)
    results = resolver.resolve_many(
        [_entry(name="Lightning Bolt", lang="de")], strict_lang=True
    )
    assert isinstance(results[0], ScryfallError)
    assert "No de match" in str(results[0])


def test_strict_lang_accepts_fuzzy_match_in_demanded_language(tmp_path: Path) -> None:
    """A German-typed name fuzzy-matching the German object passes strict
    German — the Aang flow under strict mode."""
    de_card = _card(
        id="aang-de",
        name="Aang, Air Nomad",
        printed_name="Aang der Luftnomade",
        lang="de",
        set="tle",
        collector_number="210",
    )
    live = _FakeClient(by_key={"Aang der Luftnomade": de_card})
    resolver = _resolver(tmp_path, client=live)
    card, _ = resolver.resolve(
        _entry(name="Aang der Luftnomade", lang="de"), strict_lang=True
    )
    assert card["id"] == "aang-de"


def test_entries_without_lang_ignore_strict_flag(tmp_path: Path) -> None:
    """"All Languages" mode: entries carry lang=None, so strict_lang has
    nothing to demand and the relaxed ladder applies unchanged."""
    de_card = _card(id="aang-de", name="Aang, Air Nomad", lang="de", set="tle")
    live = _FakeClient(by_key={"Aang, Air Nomad": de_card})
    resolver = _resolver(tmp_path, client=live)
    card, _ = resolver.resolve(_entry(name="Aang, Air Nomad"), strict_lang=True)
    assert card["id"] == "aang-de"
