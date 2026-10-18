"""Tests for the MPC Fill art catalog (proxy_scaler/mpc_catalog.py): the
manifest contract (.scratch/mpcfill-art-picker/phase-c/format.md),
first and incremental imports from in-test shards, the integrity check,
cancellation, CatalogSource search, get_art_source switching, and the
/api/mpccatalog routes. The network is always a stub session."""

from __future__ import annotations

import gzip
import hashlib
import io
import json
import time
from pathlib import Path

import pytest
from fastapi.testclient import TestClient

from proxy_scaler import art_sources, mpc_catalog, mpc_catalog_jobs, mpcfill
from proxy_scaler.art_sources import ArtFilters, ArtQuery, ArtSourceError

BASE = "https://catalog.test/"


@pytest.fixture(autouse=True)
def _isolated(tmp_path, monkeypatch):
    monkeypatch.setenv("PROXY_SCALER_MPC_CATALOG_DB_PATH", str(tmp_path / "mpc_catalog.db"))
    monkeypatch.setenv("PROXY_SCALER_MPC_CATALOG_URL", BASE)
    mpc_catalog._reset_for_tests()
    mpc_catalog_jobs._reset_for_tests()
    yield
    mpc_catalog._reset_for_tests()
    mpc_catalog_jobs._reset_for_tests()


def _record(drive_id: str, name: str, **overrides) -> dict:
    record = {
        "id": drive_id,
        "name": name,
        "ext": "png",
        "type": "CARD",
        "lang": "EN",
        "tags": [],
        "dpi": 800,
        "w": 2192,
        "h": 2992,
        "size": 5_000_000,
        "created": "2024-01-01T00:00:00.000Z",
        "modified": "2024-01-01T00:00:00.000Z",
        "folder": "Cards",
    }
    record.update(overrides)
    return record


def _shard(records: list[dict]) -> bytes:
    buf = io.BytesIO()
    with gzip.GzipFile(fileobj=buf, mode="wb", mtime=0) as gz:
        for record in records:
            gz.write((json.dumps(record) + "\n").encode("utf-8"))
    return buf.getvalue()


class _Resp:
    def __init__(self, body: bytes, status: int = 200) -> None:
        self.body = body
        self.status_code = status

    def raise_for_status(self) -> None:
        if self.status_code >= 400:
            import requests

            raise requests.HTTPError(f"HTTP {self.status_code}")

    def json(self):
        return json.loads(self.body)

    def iter_content(self, chunk_size: int = 1024):
        for start in range(0, len(self.body), chunk_size):
            yield self.body[start : start + chunk_size]


class _Catalog:
    """A published catalog: sources -> records, served like R2 would."""

    def __init__(self) -> None:
        self.files: dict[str, bytes] = {}
        self.requests: list[str] = []
        self.version = "2026-10-18T06:00:00Z"
        self.sources: list[dict] = []
        self.corrupt: set[str] = set()

    def publish(self, version: str, sources: list[tuple[str, list[dict]]]) -> None:
        self.version = version
        self.sources = []
        for order, (key, records) in enumerate(sources):
            blob = _shard(records)
            sha = hashlib.sha256(blob).hexdigest()
            path = f"sources/{key}.{sha}.ndjson.gz"
            self.files[path] = blob
            self.sources.append(
                {
                    "key": key,
                    "name": key.replace("_", " "),
                    "order": order,
                    "path": path,
                    "sha256": sha,
                    "bytes": len(blob),
                    "count": len(records),
                    "crawled_at": version,
                }
            )
        self.files["manifest.json"] = json.dumps(
            {"format": 1, "version": version, "generated_at": version, "sources": self.sources}
        ).encode()

    # requests.Session's surface, as far as mpc_catalog uses it.
    def get(self, url: str, headers=None, timeout=None, stream=False):
        assert url.startswith(BASE)
        path = url[len(BASE) :]
        self.requests.append(path)
        if path not in self.files:
            return _Resp(b"", 404)
        body = self.files[path]
        if path in self.corrupt:
            body = body[:-1] + bytes([body[-1] ^ 0xFF])
        return _Resp(body)

    def shard_requests(self) -> list[str]:
        return [p for p in self.requests if p.startswith("sources/")]


@pytest.fixture
def published() -> _Catalog:
    cat = _Catalog()
    cat.publish(
        "2026-10-18T06:00:00Z",
        [
            (
                "Alpha",
                [
                    _record("a1", "Sol Ring", dpi=600),
                    _record("a2", "Sol Ring", dpi=1200, tags=["Retro"]),
                    _record("a3", "Sol Ring Fan Art", dpi=1200),
                    _record("a4", "Island", lang="JA"),
                    _record("a5", "Sol Ring", tags=["NSFW"]),
                    _record("a6", "Classic Back", type="CARDBACK", dpi=900),
                    _record("a7", "Treasure", type="TOKEN"),
                ],
            ),
            ("Beta", [_record("b1", "Sol Ring", dpi=1100), _record("b2", "Island")]),
        ],
    )
    return cat


def _import(cat: _Catalog) -> mpc_catalog_jobs.CatalogImportJob:
    job = mpc_catalog_jobs.create_job()
    mpc_catalog.run_import(job.id, session=cat)
    return mpc_catalog_jobs.get(job.id)


def _source() -> mpc_catalog.CatalogSource:
    return mpc_catalog.CatalogSource(mpc_catalog.db_path_from_env())


# --- Manifest -------------------------------------------------------------


def test_manifest_rejects_unsafe_paths_and_hashes(published) -> None:
    data = json.loads(published.files["manifest.json"])
    good = mpc_catalog.parse_manifest(data)
    assert [s.key for s in good.sources] == ["Alpha", "Beta"]

    for bad in ("../secrets", "sources/x.ndjson.gz?x=1", "https://evil/x.ndjson.gz"):
        data["sources"][0]["path"] = bad
        with pytest.raises(mpc_catalog.CatalogError):
            mpc_catalog.parse_manifest(data)
    data = json.loads(published.files["manifest.json"])
    data["sources"][0]["sha256"] = "nothex"
    with pytest.raises(mpc_catalog.CatalogError):
        mpc_catalog.parse_manifest(data)


def test_manifest_from_a_newer_format_asks_for_an_update(published) -> None:
    data = json.loads(published.files["manifest.json"])
    data["format"] = 2
    with pytest.raises(mpc_catalog.CatalogError, match="newer Proxy Scaler"):
        mpc_catalog.parse_manifest(data)


# --- Import ---------------------------------------------------------------


def test_first_import_writes_everything_and_meta(published) -> None:
    assert mpc_catalog.local_status() is None
    assert not mpc_catalog.is_ready()

    job = _import(published)

    assert job.status == mpc_catalog_jobs.DONE, job.error
    status = mpc_catalog.local_status()
    assert status["version"] == "2026-10-18T06:00:00Z"
    assert status["card_count"] == 9
    assert status["source_count"] == 2
    assert mpc_catalog.is_ready()


def test_update_downloads_only_changed_shards_and_drops_removed(published) -> None:
    _import(published)
    alpha_records = [
        _record("a1", "Sol Ring", dpi=600),
        _record("a8", "Lightning Bolt"),
    ]
    published.requests.clear()
    # Alpha changes, Beta goes away, Gamma is new (and listed first).
    published.publish(
        "2026-10-19T06:00:00Z",
        [("Gamma", [_record("g1", "Island")]), ("Alpha", alpha_records)],
    )

    check = mpc_catalog.check_update(session=published, refresh=True)
    assert check.update_available
    assert (check.changed_sources, check.removed_sources) == (2, 1)

    job = _import(published)

    assert job.status == mpc_catalog_jobs.DONE, job.error
    assert sorted(published.shard_requests()) == sorted(
        s["path"] for s in published.sources
    )
    source = _source()
    filters = ArtFilters()
    found = source.search(
        [ArtQuery("Sol Ring", "CARD"), ArtQuery("Island", "CARD"), ArtQuery("Lightning Bolt", "CARD")],
        filters,
    )
    assert found[ArtQuery("Sol Ring", "CARD")] == ["a1"]
    # Beta's Island is gone; Gamma's is there.
    assert found[ArtQuery("Island", "CARD")] == ["g1"]
    assert found[ArtQuery("Lightning Bolt", "CARD")] == ["a8"]
    assert mpc_catalog.local_status()["version"] == "2026-10-19T06:00:00Z"

    # Nothing changed since: no download offered.
    published.requests.clear()
    assert not mpc_catalog.check_update(session=published, refresh=True).update_available


def test_unchanged_shard_is_not_downloaded_again(published) -> None:
    _import(published)
    published.requests.clear()
    records = [_record("b1", "Sol Ring", dpi=1100), _record("b2", "Island"), _record("b3", "Swamp")]
    alpha_records = [
        _record("a1", "Sol Ring", dpi=600),
        _record("a2", "Sol Ring", dpi=1200, tags=["Retro"]),
        _record("a3", "Sol Ring Fan Art", dpi=1200),
        _record("a4", "Island", lang="JA"),
        _record("a5", "Sol Ring", tags=["NSFW"]),
        _record("a6", "Classic Back", type="CARDBACK", dpi=900),
        _record("a7", "Treasure", type="TOKEN"),
    ]
    published.publish("2026-10-19T06:00:00Z", [("Alpha", alpha_records), ("Beta", records)])

    _import(published)

    assert [p.split(".")[0] for p in published.shard_requests()] == ["sources/Beta"]


def test_checksum_mismatch_fails_and_keeps_the_old_catalog(published) -> None:
    _import(published)
    published.publish("2026-10-19T06:00:00Z", [("Alpha", [_record("x1", "Sol Ring")])])
    published.corrupt.add(published.sources[0]["path"])

    job = _import(published)

    assert job.status == mpc_catalog_jobs.FAILED
    assert "checksum" in job.error
    assert mpc_catalog.local_status()["version"] == "2026-10-18T06:00:00Z"
    assert "a1" in _source().search([ArtQuery("Sol Ring", "CARD")], ArtFilters())[
        ArtQuery("Sol Ring", "CARD")
    ]


def test_cancel_leaves_no_catalog(published, monkeypatch) -> None:
    job = mpc_catalog_jobs.create_job()
    mpc_catalog_jobs.request_cancel(job.id)

    mpc_catalog.run_import(job.id, session=published)

    assert mpc_catalog_jobs.get(job.id).status == mpc_catalog_jobs.CANCELED
    assert mpc_catalog.local_status() is None


def test_unreachable_catalog_fails_with_a_plain_message(monkeypatch) -> None:
    empty = _Catalog()
    job = _import(empty)
    assert job.status == mpc_catalog_jobs.FAILED
    assert "Couldn't reach" in job.error


def test_bad_records_are_skipped() -> None:
    assert mpc_catalog.record_row({"name": "x", "type": "CARD"}, "s") is None
    assert mpc_catalog.record_row({"id": "x", "name": " ", "type": "CARD"}, "s") is None
    assert mpc_catalog.record_row({"id": "x", "name": "y", "type": "PLANE"}, "s") is None
    row = mpc_catalog.record_row({"id": "x", "name": "  Sol  Ring ", "type": "CARD", "z": 1}, "s")
    assert row[1:3] == ("Sol  Ring", "sol ring")


def test_search_key_folds_accents_apostrophes_and_underscores() -> None:
    key = mpc_catalog.search_key
    assert key("Mishra_s Factory") == key("Mishra's Factory") == key("Mishra\u2019s Factory")
    assert key("Lim-D\u00fbl the Necromancer") == "lim-dul the necromancer"
    assert key("Sol_Ring") == "sol ring"
    assert key("  Sol   RING ") == "sol ring"


# --- Search ---------------------------------------------------------------


def test_search_orders_exact_then_source_then_dpi(published) -> None:
    _import(published)
    q = ArtQuery("sol  RING", "CARD")
    ids = _source().search([q], ArtFilters())[q]
    # NSFW hidden by default; Alpha before Beta; within Alpha, higher DPI first.
    assert ids == ["a2", "a1", "b1"]


def test_fuzzy_adds_containing_names_after_exact_ones(published) -> None:
    _import(published)
    q = ArtQuery("Sol Ring", "CARD")
    ids = _source().search([q], ArtFilters(fuzzy=True))[q]
    assert ids[:3] == ["a2", "a1", "b1"]
    assert "a3" in ids


def test_filters_language_dpi_nsfw_and_type(published) -> None:
    _import(published)
    source = _source()
    sol = ArtQuery("Sol Ring", "CARD")
    island = ArtQuery("Island", "CARD")
    assert source.search([island], ArtFilters(languages=("JA",)))[island] == ["a4"]
    assert source.search([sol], ArtFilters(min_dpi=1000))[sol] == ["a2", "b1"]
    assert "a5" in source.search([sol], ArtFilters(include_nsfw=True))[sol]
    assert source.search([ArtQuery("Treasure", "TOKEN")], ArtFilters())[
        ArtQuery("Treasure", "TOKEN")
    ] == ["a7"]
    assert source.search([ArtQuery("Treasure", "CARD")], ArtFilters())[
        ArtQuery("Treasure", "CARD")
    ] == []
    assert source.cardbacks(ArtFilters()) == ["a6"]


def test_cards_hydrates_known_ids_and_asks_the_fallback_for_the_rest(published) -> None:
    _import(published)

    class Fallback:
        asked: list[str] = []

        def cards(self, ids):
            self.asked.extend(ids)
            return {i: _fake_card(i) for i in ids if i == "hidden"}

    fallback = Fallback()
    source = mpc_catalog.CatalogSource(mpc_catalog.db_path_from_env(), fallback=fallback)
    cards = source.cards(["a2", "hidden", "gone"])

    assert list(cards) == ["a2", "hidden"]
    assert cards["a2"].source_name == "Alpha"
    assert cards["a2"].tags == ("Retro",)
    assert cards["a2"].dpi == 1200
    assert fallback.asked == ["hidden", "gone"]


def _fake_card(drive_id: str):
    return art_sources.ArtCard(
        drive_id=drive_id,
        name="Hidden",
        card_type="CARD",
        extension="png",
        dpi=300,
        size_bytes=1,
        language="EN",
        source_name="Hidden",
        date_created="",
    )


def test_missing_catalog_is_an_art_source_error(tmp_path) -> None:
    source = mpc_catalog.CatalogSource(tmp_path / "nope.db")
    with pytest.raises(ArtSourceError):
        source.search([ArtQuery("Sol Ring", "CARD")], ArtFilters())


def test_get_art_source_uses_the_catalog_once_imported(published, tmp_path) -> None:
    assert isinstance(art_sources.get_art_source(tmp_path), mpcfill.MpcFillSource)
    _import(published)
    source = art_sources.get_art_source(tmp_path)
    assert isinstance(source, mpc_catalog.CatalogSource)
    assert isinstance(source.fallback, mpcfill.MpcFillSource)


# --- Update checks --------------------------------------------------------


def test_check_is_cached_until_refresh(published) -> None:
    first = mpc_catalog.check_update(session=published)
    assert first.update_available and first.local_version is None
    assert first.download_bytes == sum(s["bytes"] for s in published.sources)
    mpc_catalog.check_update(session=published)
    assert published.requests.count("manifest.json") == 1
    mpc_catalog.check_update(session=published, refresh=True)
    assert published.requests.count("manifest.json") == 2


# --- Routes ---------------------------------------------------------------


@pytest.fixture
def client(published, monkeypatch, tmp_path) -> TestClient:
    monkeypatch.setenv("PROXY_SCALER_DB_PATH", str(tmp_path / "proxy_scaler.db"))
    monkeypatch.setattr(mpc_catalog, "_session", lambda session: published)
    from proxy_scaler.api.app import app

    return TestClient(app)


def test_routes_check_import_and_delete(client, published) -> None:
    status = client.get("/api/mpccatalog/status").json()
    assert status == {"local": None, "import_running": False, "active_job_id": None}
    # The status route never touches the network.
    assert published.requests == []

    check = client.post("/api/mpccatalog/check", json={}).json()
    assert check["update_available"] is True
    assert check["card_count"] == 9

    job_id = client.post("/api/mpccatalog/import").json()["job_id"]
    deadline = time.monotonic() + 5
    while True:
        job = client.get(f"/api/mpccatalog/import/{job_id}").json()
        if job["status"] != "running" or time.monotonic() > deadline:
            break
        time.sleep(0.02)
    assert job["status"] == "done", job
    local = client.get("/api/mpccatalog/status").json()["local"]
    assert local["card_count"] == 9 and local["source_count"] == 2

    assert client.delete("/api/mpccatalog").status_code == 204
    assert client.get("/api/mpccatalog/status").json()["local"] is None


def test_check_route_is_503_when_unreachable(client, published) -> None:
    published.files.pop("manifest.json")
    resp = client.post("/api/mpccatalog/check", json={"refresh": True})
    assert resp.status_code == 503
    assert "Couldn't reach" in resp.json()["detail"]


def test_mpcfill_search_answers_from_the_catalog_without_mpcfill(client, published, monkeypatch) -> None:
    def no_mpcfill(*args, **kwargs):
        raise AssertionError("mpcfill.com was called")

    monkeypatch.setattr(mpcfill, "_call", no_mpcfill)
    _import(published)

    body = client.get("/api/mpcfill/search", params={"q": "Sol Ring"}).json()

    assert body["ids"] == ["a2", "a1", "b1"]
    assert [c["drive_id"] for c in body["cards"]] == ["a2", "a1", "b1"]
    backs = client.get("/api/mpcfill/search", params={"type": "CARDBACK"}).json()
    assert backs["ids"] == ["a6"]
