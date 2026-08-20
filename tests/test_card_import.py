"""Tests for the bulk-dump import pipeline (proxy_scaler/card_import.py):
pruning, the full run_import flow over an in-test gzipped JSONL fixture,
cancellation, dataset switching, and the disk guardrail. Network is always
faked — a stub session stands in for both the bulk catalog and the dump
download."""

from __future__ import annotations

import gzip
import io
import json
from pathlib import Path

import pytest

from proxy_scaler import card_import, card_jobs, carddb


@pytest.fixture(autouse=True)
def _clean_jobs():
    card_jobs._reset_for_tests()
    yield
    card_jobs._reset_for_tests()


def _bulk_card(**overrides) -> dict:
    """A raw bulk-dump card object with the extra baggage prune_card is
    supposed to drop."""
    card = {
        "object": "card",
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
        "image_uris": {
            "png": "https://img.example/bolt.png",
            "large": "https://img.example/bolt-large.jpg",
            "small": "https://img.example/bolt-small.jpg",
        },
        "oracle_text": "Lightning Bolt deals 3 damage to any target.",
        "legalities": {"modern": "legal"},
        "prices": {"usd": "1.00"},
    }
    card.update(overrides)
    return card


def _gzipped_jsonl(cards: list[dict]) -> bytes:
    buf = io.BytesIO()
    with gzip.GzipFile(fileobj=buf, mode="wb") as gz:
        for card in cards:
            gz.write((json.dumps(card) + "\n").encode("utf-8"))
    return buf.getvalue()


class _StubResponse:
    def __init__(self, *, json_data=None, content: bytes = b""):
        self._json = json_data
        self._content = content
        self.status_code = 200

    def raise_for_status(self) -> None:
        pass

    def json(self):
        return self._json

    def iter_content(self, chunk_size: int):
        for i in range(0, len(self._content), chunk_size):
            yield self._content[i : i + chunk_size]


class _StubSession:
    """Answers the bulk catalog with one dataset entry and the download URI
    with the given gzipped payload."""

    def __init__(self, dataset: str, payload: bytes, updated_at="2026-08-19T00:00:00Z"):
        self.dataset = dataset
        self.payload = payload
        self.updated_at = updated_at

    def get(self, url, **kwargs):
        if url == card_import.BULK_DATA_URL:
            return _StubResponse(
                json_data={
                    "data": [
                        {
                            "type": self.dataset,
                            "updated_at": self.updated_at,
                            "jsonl_download_uri": "https://data.example/dump.jsonl.gz",
                            "compressed_size": len(self.payload),
                        }
                    ]
                }
            )
        return _StubResponse(content=self.payload)


def _run(tmp_path: Path, cards: list[dict], dataset="default_cards", **kwargs) -> str:
    job = card_jobs.create_job(dataset=dataset)
    session = kwargs.pop("session", _StubSession(dataset, _gzipped_jsonl(cards)))
    card_import.run_import(
        job.id,
        dataset,
        card_db_path=tmp_path / "cards.db",
        tmp_dir=tmp_path / "tmp",
        session=session,
        **kwargs,
    )
    return job.id


# ---------------------------------------------------------------------------
# prune_card


def test_prune_card_keeps_expand_faces_fields_drops_baggage() -> None:
    pruned = card_import.prune_card(_bulk_card())
    assert pruned["id"] == "id-1"
    assert pruned["set"] == "lea"
    assert pruned["collector_number"] == "161"
    assert pruned["image_uris"] == {"png": "https://img.example/bolt.png"}
    for dropped in ("object", "oracle_text", "legalities", "prices"):
        assert dropped not in pruned


def test_prune_card_keeps_per_face_pngs() -> None:
    card = _bulk_card(
        card_faces=[
            {
                "name": "Delver of Secrets",
                "oracle_id": "oracle-1",
                "image_uris": {"png": "https://img.example/front.png", "small": "x"},
                "mana_cost": "{U}",
            },
            {
                "name": "Insectile Aberration",
                "image_uris": {"png": "https://img.example/back.png"},
            },
        ]
    )
    del card["image_uris"]
    pruned = card_import.prune_card(card)
    assert pruned["card_faces"][0]["image_uris"] == {
        "png": "https://img.example/front.png"
    }
    assert "mana_cost" not in pruned["card_faces"][0]
    assert pruned["card_faces"][1]["name"] == "Insectile Aberration"
    assert "image_uris" not in pruned


def test_pruned_card_feeds_expand_faces() -> None:
    from proxy_scaler.scryfall import expand_faces

    faces = expand_faces(card_import.prune_card(_bulk_card()))
    assert len(faces) == 1
    assert faces[0].scryfall_id == "id-1"
    assert faces[0].png_url == "https://img.example/bolt.png"


# ---------------------------------------------------------------------------
# run_import


def test_run_import_success(tmp_path: Path) -> None:
    cards = [_bulk_card(), _bulk_card(id="id-2", collector_number="162")]
    job_id = _run(tmp_path, cards)
    job = card_jobs.get(job_id)
    assert job.status == card_jobs.DONE
    assert job.rows_imported == 2

    conn = carddb.connect(tmp_path / "cards.db")
    try:
        assert carddb.count_cards(conn) == 2
        meta = carddb.get_meta(conn)
        assert meta[carddb.META_DATASET_TYPE] == "default_cards"
        assert meta[carddb.META_DATASET_UPDATED_AT] == "2026-08-19T00:00:00Z"
        assert meta[carddb.META_CARD_COUNT] == "2"
    finally:
        conn.close()
    # The downloaded temp file is cleaned up on every exit path.
    assert not any((tmp_path / "tmp").glob("*.part"))


def test_run_import_skips_blank_lines_and_idless_rows(tmp_path: Path) -> None:
    payload = _gzipped_jsonl([_bulk_card(), {"object": "not-a-card"}])
    # splice in a blank line
    raw = gzip.decompress(payload) + b"\n\n"
    session = _StubSession("default_cards", gzip.compress(raw))
    job_id = _run(tmp_path, [], session=session)
    job = card_jobs.get(job_id)
    assert job.status == card_jobs.DONE
    assert job.rows_imported == 1


def test_run_import_reimport_upserts_same_dataset(tmp_path: Path) -> None:
    _run(tmp_path, [_bulk_card()])
    job_id = _run(tmp_path, [_bulk_card(name="Bolt v2"), _bulk_card(id="id-2")])
    assert card_jobs.get(job_id).status == card_jobs.DONE
    conn = carddb.connect(tmp_path / "cards.db")
    try:
        assert carddb.count_cards(conn) == 2
        assert carddb.get_card_by_id(conn, "id-1")["name"] == "Bolt v2"
    finally:
        conn.close()


def test_run_import_dataset_switch_wipes_old_rows(tmp_path: Path) -> None:
    _run(tmp_path, [_bulk_card(), _bulk_card(id="id-ja", lang="ja")], dataset="all_cards")
    job_id = _run(tmp_path, [_bulk_card()], dataset="default_cards")
    assert card_jobs.get(job_id).status == card_jobs.DONE
    conn = carddb.connect(tmp_path / "cards.db")
    try:
        assert carddb.count_cards(conn) == 1
        assert carddb.get_card_by_id(conn, "id-ja") is None
        assert carddb.get_meta(conn)[carddb.META_DATASET_TYPE] == "default_cards"
    finally:
        conn.close()


def test_run_import_failure_leaves_meta_absent(tmp_path: Path) -> None:
    class _FailingSession(_StubSession):
        def get(self, url, **kwargs):
            if url == card_import.BULK_DATA_URL:
                return super().get(url, **kwargs)
            raise RuntimeError("network down")

    session = _FailingSession("default_cards", _gzipped_jsonl([_bulk_card()]))
    job_id = _run(tmp_path, [], session=session)
    job = card_jobs.get(job_id)
    assert job.status == card_jobs.FAILED
    assert "network down" in job.error
    # No import ever finished → open_if_ready may find a file (init happens
    # after download), but never one claiming to be a finished dataset.
    conn = carddb.open_if_ready(tmp_path / "cards.db")
    if conn is not None:
        try:
            assert carddb.META_DATASET_TYPE not in carddb.get_meta(conn)
        finally:
            conn.close()


def test_run_import_cancel_before_download(tmp_path: Path) -> None:
    job = card_jobs.create_job(dataset="default_cards")
    card_jobs.request_cancel(job.id)
    session = _StubSession("default_cards", _gzipped_jsonl([_bulk_card()]))
    card_import.run_import(
        job.id,
        "default_cards",
        card_db_path=tmp_path / "cards.db",
        tmp_dir=tmp_path / "tmp",
        session=session,
    )
    assert card_jobs.get(job.id).status == card_jobs.CANCELED


def test_run_import_cancel_mid_import(tmp_path: Path, monkeypatch) -> None:
    """Cancel requested between upsert batches: shrink the batch size so a
    two-card dump spans two batches, and flip the cancel flag from the
    progress hook the first batch triggers."""
    monkeypatch.setattr(card_import, "_UPSERT_BATCH_ROWS", 1)
    job = card_jobs.create_job(dataset="default_cards")

    original = card_jobs.set_import_progress

    def cancel_after_first_batch(job_id: str, rows: int) -> None:
        original(job_id, rows)
        card_jobs.request_cancel(job_id)

    monkeypatch.setattr(card_jobs, "set_import_progress", cancel_after_first_batch)
    session = _StubSession(
        "default_cards", _gzipped_jsonl([_bulk_card(), _bulk_card(id="id-2")])
    )
    card_import.run_import(
        job.id,
        "default_cards",
        card_db_path=tmp_path / "cards.db",
        tmp_dir=tmp_path / "tmp",
        session=session,
    )
    job_after = card_jobs.get(job.id)
    assert job_after.status == card_jobs.CANCELED
    assert job_after.rows_imported == 1


def test_run_import_disk_guardrail(tmp_path: Path, monkeypatch) -> None:
    import shutil as shutil_module

    class _Usage:
        free = 10  # bytes — nowhere near enough

    monkeypatch.setattr(
        card_import.shutil, "disk_usage", lambda _path: _Usage, raising=True
    )
    job_id = _run(tmp_path, [_bulk_card()])
    job = card_jobs.get(job_id)
    assert job.status == card_jobs.FAILED
    assert "disk space" in job.error


# ---------------------------------------------------------------------------
# catalog parsing


def test_fetch_bulk_info_picks_requested_dataset() -> None:
    session = _StubSession("all_cards", b"x" * 42)
    info = card_import.fetch_bulk_info("all_cards", session)
    assert info.download_uri == "https://data.example/dump.jsonl.gz"
    assert info.compressed_size == 42


def test_fetch_bulk_info_missing_dataset_raises() -> None:
    session = _StubSession("all_cards", b"")
    with pytest.raises(RuntimeError, match="default_cards"):
        card_import.fetch_bulk_info("default_cards", session)


def test_prune_card_keeps_printed_name() -> None:
    pruned = card_import.prune_card(
        _bulk_card(printed_name="Aang der Luftnomade", lang="de")
    )
    assert pruned["printed_name"] == "Aang der Luftnomade"


def test_prune_card_keeps_face_printed_names() -> None:
    card = _bulk_card(
        lang="de",
        card_faces=[
            {
                "name": "Delver of Secrets",
                "printed_name": "Delver der Geheimnisse",
                "image_uris": {"png": "https://img.example/front.png"},
            },
            {
                "name": "Insectile Aberration",
                "printed_name": "Insektoide Abartigkeit",
                "image_uris": {"png": "https://img.example/back.png"},
            },
        ],
    )
    del card["image_uris"]
    pruned = card_import.prune_card(card)
    assert pruned["card_faces"][0]["printed_name"] == "Delver der Geheimnisse"
