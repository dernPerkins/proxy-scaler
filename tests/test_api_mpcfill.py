"""The /api/mpcfill routes and the /fetch sync routes on /api/customs and
/api/backs. The art source and Google Drive are faked; the stores, the
corpus lookup and the HTTP contract are real."""

from __future__ import annotations

import hashlib
import io
from pathlib import Path

import pytest
from fastapi.testclient import TestClient
from PIL import Image

from proxy_scaler import art_sources, backs, carddb, customs, db, drive_images
from proxy_scaler.art_sources import ArtCard, ArtFilters, ArtQuery, ArtSourceError

DRIVE_ID = "1taR38keSBJgplwmZkX3NETXQSblhPYSh"
OTHER_ID = "1MsVtWxw2bJO9xrduU0fUBraSVuvBW0zq"


def _png(size=(816, 1110), color=(20, 40, 90)) -> bytes:
    buf = io.BytesIO()
    Image.new("RGB", size, color).save(buf, format="PNG")
    return buf.getvalue()


def _art(drive_id: str, name: str = "Lightning Bolt (Full Art)") -> ArtCard:
    return ArtCard(
        drive_id=drive_id,
        name=name,
        card_type="CARD",
        extension="png",
        dpi=1200,
        size_bytes=1234,
        language="EN",
        source_name="Chilli_Axe",
        date_created="4th June, 2020",
        tags=("Full Art",),
    )


class _FakeSource:
    def __init__(self) -> None:
        self.searches: list[tuple[list[ArtQuery], ArtFilters, bool]] = []
        self.ids = [f"{DRIVE_ID[:-3]}{i:03d}" for i in range(70)]
        self.fail = False

    def search(self, queries, filters, *, refresh=False):
        if self.fail:
            raise ArtSourceError("MPC Fill is down.")
        self.searches.append((queries, filters, refresh))
        return {q: list(self.ids) for q in queries}

    def cards(self, drive_ids):
        return {i: _art(i) for i in drive_ids if i in self.ids or i == DRIVE_ID}

    def cardbacks(self, filters, *, refresh=False):
        return self.ids[:5]


@pytest.fixture
def source(monkeypatch) -> _FakeSource:
    fake = _FakeSource()
    monkeypatch.setattr(art_sources, "get_art_source", lambda cache_dir: fake)
    return fake


@pytest.fixture
def downloads(monkeypatch) -> list[str]:
    """Drive downloads answer with `_png()` and are recorded."""
    calls: list[str] = []

    def fake_download(drive_id):
        calls.append(drive_images.validate_drive_id(drive_id))
        return _png(), "png"

    monkeypatch.setattr(drive_images, "download_original", fake_download)
    return calls


@pytest.fixture
def client(tmp_path: Path, monkeypatch) -> TestClient:
    db_path = tmp_path / "test.db"
    db.init_db(db_path)
    monkeypatch.setenv("PROXY_SCALER_DB_PATH", str(db_path))
    monkeypatch.setenv("PROXY_SCALER_WORKER_LOCK_PATH", str(tmp_path / "worker.lock"))
    monkeypatch.setenv("PROXY_SCALER_CARD_DB_PATH", str(tmp_path / "cards.db"))
    monkeypatch.chdir(tmp_path)
    from proxy_scaler.api.app import app

    return TestClient(app)


def _seed(cards: list[dict]) -> None:
    import os

    path = os.environ["PROXY_SCALER_CARD_DB_PATH"]
    carddb.init_card_db(path)
    conn = carddb.connect(path)
    try:
        carddb.upsert_cards(conn, cards)
        carddb.write_import_meta(
            conn, dataset_type="default_cards", dataset_updated_at="2026-10-01T00:00:00Z"
        )
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


# --- search -----------------------------------------------------------------


def test_search_returns_every_id_and_hydrates_the_first_page(client, source) -> None:
    resp = client.get("/api/mpcfill/search", params={"q": "lightning bolt"})
    assert resp.status_code == 200, resp.text
    body = resp.json()
    assert body["query"] == "lightning bolt" and body["card_type"] == "CARD"
    assert body["ids"] == source.ids
    assert [c["drive_id"] for c in body["cards"]] == source.ids[:60]
    assert body["cards"][0]["source_name"] == "Chilli_Axe"


def test_search_maps_filters(client, source) -> None:
    client.get(
        "/api/mpcfill/search",
        params={"q": "bolt", "lang": "ja", "fuzzy": True, "nsfw": True, "min_dpi": 800},
    )
    _queries, filters, _refresh = source.searches[-1]
    assert filters == ArtFilters(languages=("JA",), fuzzy=True, include_nsfw=True, min_dpi=800)


def test_unsupported_language_searches_every_language(client, source) -> None:
    client.get("/api/mpcfill/search", params={"q": "bolt", "lang": "ko"})
    assert source.searches[-1][1].languages == ()


def test_search_by_card_uses_the_corpus_name(client, source) -> None:
    _seed([_card()])
    body = client.get("/api/mpcfill/search", params={"scryfall_id": "bolt-lea"}).json()
    assert body["query"] == "Lightning Bolt" and body["card_type"] == "CARD"


def test_search_by_token_searches_tokens(client, source) -> None:
    _seed([_card(id="tok", name="Treasure", layout="token")])
    body = client.get("/api/mpcfill/search", params={"scryfall_id": "tok"}).json()
    assert body["card_type"] == "TOKEN"
    assert source.searches[-1][0] == [ArtQuery("Treasure", "TOKEN")]


def test_search_by_dfc_face_uses_that_face_name(client, source) -> None:
    _seed(
        [
            _card(
                id="delver",
                name="Delver of Secrets // Insectile Aberration",
                layout="transform",
                image_uris=None,
                card_faces=[
                    {"name": "Delver of Secrets", "image_uris": {"png": "https://img.example/a.png"}},
                    {"name": "Insectile Aberration", "image_uris": {"png": "https://img.example/b.png"}},
                ],
            )
        ]
    )
    body = client.get("/api/mpcfill/search", params={"scryfall_id": "delver", "face": 1}).json()
    assert body["query"] == "Insectile Aberration"


def test_card_back_search_without_a_query_lists_every_back(client, source) -> None:
    body = client.get("/api/mpcfill/search", params={"type": "CARDBACK"}).json()
    assert body["ids"] == source.ids[:5]


def test_empty_card_search_is_a_400(client, source) -> None:
    assert client.get("/api/mpcfill/search", params={"q": "  "}).status_code == 400


def test_art_source_failure_is_a_502(client, source) -> None:
    source.fail = True
    resp = client.get("/api/mpcfill/search", params={"q": "bolt"})
    assert resp.status_code == 502
    assert "MPC Fill is down" in resp.json()["detail"]


def test_best_returns_the_top_match_per_face_in_one_search(client, source) -> None:
    _seed(
        [
            _card(),
            _card(
                id="delver",
                name="Delver of Secrets // Insectile Aberration",
                layout="transform",
                image_uris=None,
                card_faces=[
                    {"name": "Delver of Secrets", "image_uris": {"png": "https://img.example/a.png"}},
                    {"name": "Insectile Aberration", "image_uris": {"png": "https://img.example/b.png"}},
                ],
            ),
            _card(id="tok", name="Treasure", layout="token"),
        ]
    )
    body = client.post(
        "/api/mpcfill/best",
        json={"scryfall_ids": ["bolt-lea", "delver", "tok", "unknown", "bolt-lea"], "lang": "en"},
    ).json()
    by_id = {r["scryfall_id"]: r for r in body["results"]}
    assert set(by_id) == {"bolt-lea", "delver", "tok"}
    assert by_id["delver"]["total_faces"] == 2
    assert [f["drive_id"] for f in by_id["delver"]["faces"]] == [source.ids[0], source.ids[0]]
    # Every face of every card in one search call, TOKEN for the token.
    assert len(source.searches) == 1
    queries, filters, _ = source.searches[0]
    assert ArtQuery("Treasure", "TOKEN") in queries
    assert ArtQuery("Insectile Aberration", "CARD") in queries
    assert filters.languages == ("EN",)


def test_best_leaves_faces_without_a_match_null(client, source) -> None:
    _seed([_card()])
    source.ids = []
    body = client.post("/api/mpcfill/best", json={"scryfall_ids": ["bolt-lea"]}).json()
    assert body["results"][0]["faces"] == [None]


def test_cards_hydrates_in_request_order(client, source) -> None:
    ids = [source.ids[65], source.ids[61], "unknown-id-0000000000000"]
    body = client.post("/api/mpcfill/cards", json={"ids": ids}).json()
    assert [c["drive_id"] for c in body["cards"]] == ids[:2]


# --- thumb ------------------------------------------------------------------


def test_thumb_serves_a_jpeg(client, monkeypatch) -> None:
    monkeypatch.setattr(drive_images, "thumbnail", lambda i, s, cache_dir: b"\xff\xd8\xffjpeg")
    resp = client.get(f"/api/mpcfill/thumb/{DRIVE_ID}", params={"size": "large"})
    assert resp.status_code == 200
    assert resp.headers["content-type"] == "image/jpeg"
    assert "max-age" in resp.headers["cache-control"]


def test_thumb_failure_is_a_502(client, monkeypatch) -> None:
    def boom(*_a, **_k):
        raise drive_images.DriveImageError("nope")

    monkeypatch.setattr(drive_images, "thumbnail", boom)
    assert client.get(f"/api/mpcfill/thumb/{DRIVE_ID}").status_code == 502


# --- import -----------------------------------------------------------------


def test_import_custom_returns_the_original_and_stores_it(client, downloads) -> None:
    resp = client.post(
        "/api/mpcfill/import", json={"drive_id": DRIVE_ID, "kind": "custom", "bleed_mm": 3.175}
    )
    assert resp.status_code == 200, resp.text
    assert resp.headers["content-type"] == "image/png"
    assert resp.content == _png()
    content_hash = hashlib.sha256(resp.content).hexdigest()
    status = client.get(f"/api/customs/{content_hash}").json()
    assert status["present"] is True
    assert status["bleed_mm"] == pytest.approx(3.175)


def test_import_of_a_bleed_free_file_declares_no_bleed(client, monkeypatch) -> None:
    flat = _png(size=(745, 1040))
    monkeypatch.setattr(drive_images, "download_original", lambda _id: (flat, "png"))
    resp = client.post(
        "/api/mpcfill/import", json={"drive_id": DRIVE_ID, "kind": "custom", "bleed_mm": 3.175}
    )
    content_hash = hashlib.sha256(resp.content).hexdigest()
    assert client.get(f"/api/customs/{content_hash}").json()["bleed_mm"] == 0.0


def test_import_back_stores_a_back(client, downloads) -> None:
    resp = client.post("/api/mpcfill/import", json={"drive_id": DRIVE_ID, "kind": "back"})
    assert resp.status_code == 200
    content_hash = hashlib.sha256(resp.content).hexdigest()
    assert backs.has_original(content_hash)
    assert not customs.has_original(content_hash)


def test_import_drive_failure_is_a_502_and_stores_nothing(client, monkeypatch, tmp_path) -> None:
    def boom(_id):
        raise drive_images.DriveImageError("Google Drive is refusing downloads of this file")

    monkeypatch.setattr(drive_images, "download_original", boom)
    resp = client.post("/api/mpcfill/import", json={"drive_id": DRIVE_ID, "kind": "custom"})
    assert resp.status_code == 502
    assert "refusing" in resp.json()["detail"]
    assert not any((tmp_path / "customs").glob("*.png"))


def test_import_rejects_a_bad_drive_id(client, downloads) -> None:
    resp = client.post("/api/mpcfill/import", json={"drive_id": "../x", "kind": "custom"})
    assert resp.status_code == 400
    assert downloads == []


# --- /fetch on customs and backs ----------------------------------------------


def test_custom_fetch_downloads_and_stores_under_the_hash(client, downloads) -> None:
    content_hash = hashlib.sha256(_png()).hexdigest()
    resp = client.post(
        f"/api/customs/{content_hash}/fetch", json={"drive_id": DRIVE_ID, "bleed_mm": 3.175}
    )
    assert resp.status_code == 200, resp.text
    assert resp.json()["present"] is True
    assert resp.json()["bleed_mm"] == pytest.approx(3.175)
    assert downloads == [DRIVE_ID]


def test_custom_fetch_skips_the_download_when_already_present(client, downloads) -> None:
    content_hash = hashlib.sha256(_png()).hexdigest()
    client.post(f"/api/customs/{content_hash}?bleed_mm=3.175", content=_png())
    resp = client.post(
        f"/api/customs/{content_hash}/fetch", json={"drive_id": DRIVE_ID, "bleed_mm": 3.175}
    )
    assert resp.status_code == 200
    assert downloads == []


def test_custom_fetch_of_a_replaced_drive_file_is_a_409(client, downloads) -> None:
    other_hash = hashlib.sha256(_png(color=(1, 2, 3))).hexdigest()
    resp = client.post(f"/api/customs/{other_hash}/fetch", json={"drive_id": DRIVE_ID})
    assert resp.status_code == 409
    assert not customs.has_original(other_hash)


def test_back_fetch_downloads_and_stores(client, downloads) -> None:
    content_hash = hashlib.sha256(_png()).hexdigest()
    resp = client.post(f"/api/backs/{content_hash}/fetch", json={"drive_id": OTHER_ID})
    assert resp.status_code == 200, resp.text
    assert resp.json()["present"] is True
    # Present now: a second fetch costs no download.
    client.post(f"/api/backs/{content_hash}/fetch", json={"drive_id": OTHER_ID})
    assert downloads == [OTHER_ID]


def test_back_fetch_of_a_replaced_drive_file_is_a_409(client, downloads) -> None:
    other_hash = hashlib.sha256(b"something else").hexdigest()
    assert client.post(f"/api/backs/{other_hash}/fetch", json={"drive_id": OTHER_ID}).status_code == 409
