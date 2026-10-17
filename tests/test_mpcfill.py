"""The mpcfill.com art source (proxy_scaler/mpcfill.py): the request
shapes it sends, its day-long caches, chunking, and that nothing from
MPC Fill's own image hosts survives parsing. Replays recorded responses
from tests/fixtures/mpcfill/ (tools/record_mpcfill_fixtures.py); never
touches the network."""

from __future__ import annotations

import json
import os
import time
from pathlib import Path

import pytest

from proxy_scaler import mpcfill
from proxy_scaler.art_sources import ArtFilters, ArtQuery, ArtSourceError

FIXTURES = Path(__file__).parent / "fixtures" / "mpcfill"


def _fixture(name: str) -> dict:
    return json.loads((FIXTURES / name).read_text(encoding="utf-8"))


class _Resp:
    def __init__(self, data=None, status_code: int = 200):
        self._data = data
        self.status_code = status_code
        self.ok = status_code < 400

    def json(self):
        if isinstance(self._data, Exception):
            raise self._data
        return self._data


class _FakeSession:
    """Answers each MPC Fill route from the fixtures (or `overrides`),
    recording every (method, path, body)."""

    def __init__(self, overrides: dict | None = None):
        self.calls: list[tuple[str, str, dict | None]] = []
        self.overrides = overrides or {}
        self.headers: dict[str, str] = {}

    def request(self, method, url, json=None, timeout=None):
        path = url.removeprefix(mpcfill.MPCFILL_BASE)
        self.calls.append((method, path, json))
        if path in self.overrides:
            hit = self.overrides[path]
            return hit(json) if callable(hit) else hit
        if path == "/2/sources/":
            return _Resp(_fixture("sources.json"))
        if path == "/2/editorSearch/":
            recorded = _fixture("editor_search.json")["results"]
            return _Resp(
                {
                    "results": {
                        q["query"]: {q["cardType"]: recorded.get(q["query"], {}).get(q["cardType"], [])}
                        for q in json["queries"]
                    }
                }
            )
        if path == "/2/cards/":
            recorded = _fixture("cards.json")["results"]
            return _Resp(
                {"results": {i: recorded[i] for i in json["cardIdentifiers"] if i in recorded}}
            )
        if path == "/2/cardbacks/":
            return _Resp(_fixture("cardbacks.json"))
        return _Resp({"detail": "nope"}, 404)

    def paths(self) -> list[str]:
        return [p for _m, p, _b in self.calls]


@pytest.fixture
def session(monkeypatch) -> _FakeSession:
    mpcfill._reset_for_tests()
    sess = _FakeSession()
    monkeypatch.setattr(mpcfill, "_session", sess)
    monkeypatch.setattr(mpcfill, "MIN_CALL_GAP_S", 0.0)
    yield sess
    mpcfill._reset_for_tests()


def _age(path: Path, seconds: float) -> None:
    t = time.time() - seconds
    os.utime(path, (t, t))


def test_user_agent_names_proxy_scaler_and_its_version() -> None:
    from proxy_scaler import __version__

    assert mpcfill.USER_AGENT.startswith(f"proxy-scaler/{__version__} ")


def test_search_sends_every_drive_source_and_default_filters(tmp_path, session) -> None:
    source = mpcfill.MpcFillSource(tmp_path)
    q = ArtQuery("Lightning  Bolt", "CARD")
    ids = source.search([q], ArtFilters())[q]
    assert ids == _fixture("editor_search.json")["results"]["lightning bolt"]["CARD"]

    method, path, body = session.calls[-1]
    assert (method, path) == ("POST", "/2/editorSearch/")
    assert body["queries"] == [{"query": "lightning bolt", "cardType": "CARD"}]
    settings = body["searchSettings"]
    pks = [int(k) for k in _fixture("sources.json")["results"]]
    assert settings["sourceSettings"]["sources"] == [[pk, True] for pk in pks]
    assert settings["filterSettings"]["excludesTags"] == ["NSFW"]
    assert settings["searchTypeSettings"]["fuzzySearch"] is False


def test_search_filters_map_onto_search_settings(tmp_path, session) -> None:
    source = mpcfill.MpcFillSource(tmp_path)
    filters = ArtFilters(languages=("JA",), fuzzy=True, include_nsfw=True, min_dpi=800)
    source.search([ArtQuery("treasure", "TOKEN")], filters)
    settings = session.calls[-1][2]["searchSettings"]
    assert settings["filterSettings"]["languages"] == ["JA"]
    assert settings["filterSettings"]["excludesTags"] == []
    assert settings["filterSettings"]["minimumDPI"] == 800
    assert settings["searchTypeSettings"]["fuzzySearch"] is True


def test_search_and_sources_are_cached_on_disk_for_a_day(tmp_path, session) -> None:
    source = mpcfill.MpcFillSource(tmp_path)
    q = ArtQuery("lightning bolt", "CARD")
    first = source.search([q], ArtFilters())[q]
    calls = len(session.calls)
    # A new instance (a new request) still hits the disk cache.
    again = mpcfill.MpcFillSource(tmp_path).search([ArtQuery("LIGHTNING BOLT", "CARD")], ArtFilters())
    assert list(again.values()) == [first]
    assert len(session.calls) == calls

    # refresh bypasses the search cache (sources stay cached).
    source.search([q], ArtFilters(), refresh=True)
    assert session.paths()[calls:] == ["/2/editorSearch/"]


def test_expired_search_cache_is_refetched(tmp_path, session) -> None:
    source = mpcfill.MpcFillSource(tmp_path)
    q = ArtQuery("treasure", "TOKEN")
    source.search([q], ArtFilters())
    for path in (tmp_path / "mpcfill" / "search").iterdir():
        _age(path, mpcfill.SEARCH_TTL_S + 60)
    source.search([q], ArtFilters())
    assert session.paths().count("/2/editorSearch/") == 2


def test_queries_are_chunked_at_three_hundred(tmp_path, session, monkeypatch) -> None:
    source = mpcfill.MpcFillSource(tmp_path)
    queries = [ArtQuery(f"card {i}", "CARD") for i in range(301)]
    result = source.search(queries, ArtFilters())
    searches = [b for _m, p, b in session.calls if p == "/2/editorSearch/"]
    assert [len(b["queries"]) for b in searches] == [300, 1]
    assert set(result) == set(queries)


def test_sources_refresh_failure_falls_back_to_the_stale_list(tmp_path, session) -> None:
    source = mpcfill.MpcFillSource(tmp_path)
    pks = source.source_pks()
    _age(tmp_path / "mpcfill" / "sources.json", mpcfill.SOURCES_TTL_S + 60)
    session.overrides["/2/sources/"] = _Resp({}, 503)
    assert source.source_pks() == pks


def test_sources_failure_with_nothing_cached_raises_and_is_not_cached(tmp_path, session) -> None:
    session.overrides["/2/sources/"] = _Resp({}, 503)
    source = mpcfill.MpcFillSource(tmp_path)
    with pytest.raises(ArtSourceError):
        source.source_pks()
    del session.overrides["/2/sources/"]
    assert source.source_pks()


def test_non_drive_sources_are_left_out(tmp_path, session) -> None:
    sources = _fixture("sources.json")
    first = next(iter(sources["results"]))
    sources["results"][first]["sourceType"] = "AWS S3"
    session.overrides["/2/sources/"] = _Resp(sources)
    pks = mpcfill.MpcFillSource(tmp_path).source_pks()
    assert int(first) not in pks
    assert len(pks) == len(sources["results"]) - 1


def test_cards_drop_mpc_image_urls_and_are_cached_in_memory(tmp_path, session) -> None:
    recorded = _fixture("cards.json")["results"]
    ids = list(recorded)
    source = mpcfill.MpcFillSource(tmp_path)
    cards = source.cards(ids + ["not-a-known-id-00000000"])
    assert list(cards) == ids
    card = cards[ids[0]]
    raw = recorded[ids[0]]
    assert card.name == raw["name"]
    assert card.dpi == raw["dpi"]
    assert card.source_name == raw["sourceName"]
    assert not any("mpcautofill" in str(v) or "drive.google" in str(v) for v in vars(card).values())

    calls = len(session.calls)
    source.cards(ids)
    assert len(session.calls) == calls


def test_cards_are_chunked_at_a_thousand(tmp_path, session) -> None:
    ids = [f"id{i:030d}" for i in range(1001)]
    mpcfill.MpcFillSource(tmp_path).cards(ids)
    chunks = [b["cardIdentifiers"] for _m, p, b in session.calls if p == "/2/cards/"]
    assert [len(c) for c in chunks] == [1000, 1]


def test_cardbacks_are_listed_and_cached(tmp_path, session) -> None:
    source = mpcfill.MpcFillSource(tmp_path)
    ids = source.cardbacks(ArtFilters())
    assert ids == _fixture("cardbacks.json")["cardbacks"]
    source.cardbacks(ArtFilters())
    assert session.paths().count("/2/cardbacks/") == 1


def test_upstream_errors_become_art_source_errors(tmp_path, session) -> None:
    session.overrides["/2/editorSearch/"] = _Resp(ValueError("not json"))
    with pytest.raises(ArtSourceError):
        mpcfill.MpcFillSource(tmp_path).search([ArtQuery("x", "CARD")], ArtFilters())
