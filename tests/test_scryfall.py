"""Tests for ScryfallClient's batch resolution (resolve_collection/resolve_many)."""

from __future__ import annotations

import pytest

from proxy_scaler.decklist import DeckEntry
from proxy_scaler.scryfall import USER_AGENT, ScryfallClient, ScryfallError, download_png


def _entry(set_code: str, collector: str, name: str = "X") -> DeckEntry:
    return DeckEntry(quantity=1, name=name, set_code=set_code, collector_number=collector)


def _card(name: str, set_code: str, collector: str, scryfall_id: str) -> dict:
    return {
        "id": scryfall_id,
        "name": name,
        "set": set_code,
        "collector_number": collector,
        "image_status": "highres_scan",
        "image_uris": {"png": f"https://example.com/{scryfall_id}.png"},
    }


SOL_RING = _card("Sol Ring", "c21", "263", "sol-id")
LIGHTNING_BOLT = _card("Lightning Bolt", "lea", "161", "bolt-id")


def test_resolve_many_batches_exact_printings_into_one_post_call(monkeypatch) -> None:
    client = ScryfallClient(delay_s=0)
    post_calls: list[dict] = []

    def fake_post(path, json):
        post_calls.append(json)
        assert path == "/cards/collection"
        return {"data": [SOL_RING, LIGHTNING_BOLT], "not_found": []}

    monkeypatch.setattr(client, "_post", fake_post)

    entries = [_entry("c21", "263"), _entry("lea", "161")]
    results = client.resolve_many(entries)

    assert len(post_calls) == 1
    assert len(post_calls[0]["identifiers"]) == 2
    assert results[0][0]["name"] == "Sol Ring"
    assert results[1][0]["name"] == "Lightning Bolt"


def test_resolve_many_chunks_over_75_identifiers(monkeypatch) -> None:
    client = ScryfallClient(delay_s=0)
    post_calls: list[dict] = []

    def fake_post(path, json):
        post_calls.append(json)
        cards = [
            _card(f"Card {ident['collector_number']}", "abc", ident["collector_number"], f"id-{ident['collector_number']}")
            for ident in json["identifiers"]
        ]
        return {"data": cards, "not_found": []}

    monkeypatch.setattr(client, "_post", fake_post)

    entries = [_entry("abc", str(i)) for i in range(80)]
    results = client.resolve_many(entries)

    assert len(post_calls) == 2  # 75 + 5
    assert len(results) == 80
    assert all(not isinstance(r, ScryfallError) for r in results)


def test_resolve_many_not_found_falls_back_to_individual_request(monkeypatch) -> None:
    client = ScryfallClient(delay_s=0)
    get_calls: list[tuple] = []

    def fake_post(path, json):
        return {"data": [], "not_found": json["identifiers"]}

    def fake_get(path, params=None):
        get_calls.append(path)
        raise ScryfallError(f"Card not found: {path}")

    monkeypatch.setattr(client, "_post", fake_post)
    monkeypatch.setattr(client, "_get", fake_get)

    entries = [_entry("zzz", "999")]
    results = client.resolve_many(entries)

    assert len(get_calls) == 1
    assert isinstance(results[0], ScryfallError)


def test_resolve_many_name_only_entries_never_batched(monkeypatch) -> None:
    client = ScryfallClient(delay_s=0)
    post_calls: list[dict] = []
    name_calls: list[str] = []

    def fake_post(path, json):
        post_calls.append(json)
        return {"data": [SOL_RING], "not_found": []}

    def fake_fetch_by_name(name):
        name_calls.append(name)
        return LIGHTNING_BOLT

    monkeypatch.setattr(client, "_post", fake_post)
    monkeypatch.setattr(client, "fetch_by_name", fake_fetch_by_name)

    entries = [_entry("c21", "263"), DeckEntry(quantity=1, name="Lightning Bolt")]
    results = client.resolve_many(entries)

    assert post_calls == [{"identifiers": [{"set": "c21", "collector_number": "263"}]}]
    assert name_calls == ["Lightning Bolt"]
    assert results[1][0]["name"] == "Lightning Bolt"


def test_resolve_many_warning_matches_individual_resolve(monkeypatch) -> None:
    """The batch path's name-mismatch warning must be identical to what
    the individual resolve() path would produce for the same card."""
    mismatched = _card("Not Sol Ring At All", "c21", "263", "sol-id")

    client = ScryfallClient(delay_s=0)
    monkeypatch.setattr(client, "_post", lambda path, json: {"data": [mismatched], "not_found": []})
    monkeypatch.setattr(client, "fetch_by_set_collector", lambda s, c: mismatched)

    entry = _entry("c21", "263", name="Sol Ring")

    _card_json, individual_warnings = client.resolve(entry)
    batch_results = client.resolve_many([entry])
    _batch_card, batch_warnings = batch_results[0]

    assert batch_warnings == individual_warnings
    assert len(batch_warnings) == 1
    assert "Name mismatch" in batch_warnings[0]


def test_resolve_many_batch_call_failure_falls_back_to_individual(monkeypatch) -> None:
    client = ScryfallClient(delay_s=0)
    get_calls: list[str] = []

    def fake_post(path, json):
        raise ScryfallError("Scryfall HTTP 500")

    def fake_fetch_by_set_collector(set_code, collector):
        get_calls.append(f"{set_code}/{collector}")
        return SOL_RING

    monkeypatch.setattr(client, "_post", fake_post)
    monkeypatch.setattr(client, "fetch_by_set_collector", fake_fetch_by_set_collector)

    entries = [_entry("c21", "263")]
    results = client.resolve_many(entries)

    assert get_calls == ["c21/263"]
    assert results[0][0]["name"] == "Sol Ring"


class _FakeResponse:
    def __init__(self, *, ok=True, status_code=200, text="", content=b"png-bytes"):
        self.ok = ok
        self.status_code = status_code
        self.text = text
        self.content = content


class _FakeSession:
    """Mimics requests.Session()'s own pre-populated default headers, the
    exact thing that made download_png()'s old `setdefault` a no-op."""

    def __init__(self, response: _FakeResponse) -> None:
        self.headers = {"User-Agent": "python-requests/2.34.2"}
        self._response = response
        self.get_calls: list[str] = []

    def get(self, url, timeout=None):
        self.get_calls.append(url)
        return self._response


def test_download_png_overrides_default_user_agent() -> None:
    """Regression test: requests.Session() pre-populates headers with its
    own default User-Agent, so download_png() must override it (not just
    setdefault it) or Scryfall's CDN rejects the request with a 400."""
    sess = _FakeSession(_FakeResponse())
    content = download_png("https://cards.scryfall.io/x.png", session=sess)
    assert content == b"png-bytes"
    assert sess.headers["User-Agent"] == USER_AGENT
    assert sess.headers["User-Agent"] != "python-requests/2.34.2"


def test_download_png_raises_with_status_and_body_on_failure() -> None:
    sess = _FakeSession(
        _FakeResponse(ok=False, status_code=400, text="bot detection triggered")
    )
    with pytest.raises(ScryfallError) as exc_info:
        download_png("https://cards.scryfall.io/x.png", session=sess)
    assert "400" in str(exc_info.value)
    assert "bot detection triggered" in str(exc_info.value)


def test_expected_face_count_agrees_with_expand_faces() -> None:
    """expected_face_count is expand_faces' rule as a bare count: one image
    per face carrying its own image_uris.png, else one parent image."""
    from proxy_scaler.scryfall import expand_faces, expected_face_count

    single = _card("Sol Ring", "c21", "263", "sol-id")
    assert expected_face_count(single) == 1
    assert expected_face_count(single) == len(expand_faces(single))

    dfc = {
        "id": "dfc-id",
        "name": "Dion, Bahamut's Dominant // Bahamut, Warden of Light",
        "set": "fin",
        "collector_number": "376",
        "image_status": "highres_scan",
        "card_faces": [
            {"name": "Dion", "image_uris": {"png": "https://example.com/f.png"}},
            {"name": "Bahamut", "image_uris": {"png": "https://example.com/b.png"}},
        ],
    }
    assert expected_face_count(dfc) == 2
    assert expected_face_count(dfc) == len(expand_faces(dfc))

    # Split/adventure shape: faces exist but carry no per-face image_uris —
    # one parent image.
    adventure = {
        "id": "adv-id",
        "name": "Bonecrusher Giant // Stomp",
        "set": "eld",
        "collector_number": "115",
        "image_status": "highres_scan",
        "image_uris": {"png": "https://example.com/adv.png"},
        "card_faces": [{"name": "Bonecrusher Giant"}, {"name": "Stomp"}],
    }
    assert expected_face_count(adventure) == 1
    assert expected_face_count(adventure) == len(expand_faces(adventure))

    # Unlike expand_faces, a card with no usable image still answers 1 —
    # this is coverage math, not a fetch.
    assert expected_face_count({"id": "x", "name": "X"}) == 1
