"""Back Image store + endpoints (proxy_scaler/backs.py, api/routers/backs.py).

The generation server's half of the Back Library. The client owns the
canonical copy (docs/adr/0003), so everything here is a cache keyed by
content hash — which is exactly why the hash checks are worth testing:
a cache that stores the wrong bytes under a hash is wrong forever after.

Back Images are never upscaled. The low-resolution warning is therefore
the only quality signal a user gets, which is why it is tested here
rather than treated as cosmetic.
"""

from __future__ import annotations

import hashlib
import io
from pathlib import Path

import pytest
from fastapi.testclient import TestClient
from PIL import Image

from proxy_scaler import backs, db


@pytest.fixture
def db_path(tmp_path: Path) -> Path:
    return tmp_path / "test.db"


@pytest.fixture
def client(tmp_path: Path, db_path: Path, monkeypatch) -> TestClient:
    db.init_db(db_path)
    monkeypatch.setenv("PROXY_SCALER_DB_PATH", str(db_path))
    monkeypatch.setenv("PROXY_SCALER_WORKER_LOCK_PATH", str(tmp_path / "worker.lock"))
    # The store resolves `backs/` against the process cwd, exactly as the
    # server's other relative directory names do.
    monkeypatch.chdir(tmp_path)
    from proxy_scaler.api.app import app

    return TestClient(app)


def _png(size: tuple[int, int] = (600, 840), color=(20, 40, 90)) -> tuple[bytes, str]:
    buf = io.BytesIO()
    Image.new("RGB", size, color).save(buf, format="PNG")
    data = buf.getvalue()
    return data, hashlib.sha256(data).hexdigest()


def test_upload_is_idempotent_and_reports_status(client: TestClient) -> None:
    data, digest = _png()
    assert client.get(f"/api/backs/{digest}").json()["present"] is False
    assert client.post(f"/api/backs/{digest}", content=data).status_code == 200
    # Re-syncing identical bytes is a no-op, which is what lets the client
    # call this unconditionally before every render.
    assert client.post(f"/api/backs/{digest}", content=data).status_code == 200
    assert client.get(f"/api/backs/{digest}").json()["present"] is True


def test_bytes_that_do_not_match_their_hash_are_refused(client: TestClient) -> None:
    """A content-addressed store that accepts mismatched bytes lies about
    every future lookup of that hash."""
    data, _digest = _png()
    resp = client.post(f"/api/backs/{'0' * 64}", content=data)
    assert resp.status_code == 400
    assert not backs.has_original("0" * 64)


def test_non_image_and_malformed_ids_are_refused(client: TestClient) -> None:
    junk = b"this is not an image"
    digest = hashlib.sha256(junk).hexdigest()
    assert client.post(f"/api/backs/{digest}", content=junk).status_code == 400
    assert client.get("/api/backs/not-a-hash").status_code == 400
    assert client.post("/api/backs/../../etc/passwd", content=junk).status_code in (400, 404)


def test_low_resolution_is_reported_but_never_blocks(client: TestClient) -> None:
    """Plenty of people knowingly print a flat logo at low DPI — this is a
    warning the client shows, not a rejection."""
    data, digest = _png(size=(200, 280))
    assert client.post(f"/api/backs/{digest}", content=data).status_code == 200
    body = client.get(f"/api/backs/{digest}").json()
    assert body["low_resolution"] is True
    assert body["source_dpi"] < backs.MIN_COMFORTABLE_DPI


def test_delete_removes_the_original_from_this_server_only(client: TestClient) -> None:
    data, digest = _png()
    client.post(f"/api/backs/{digest}", content=data)
    assert client.delete(f"/api/backs/{digest}").json()["removed"] == 1
    assert client.get(f"/api/backs/{digest}").json()["present"] is False


def test_resolve_print_source_is_the_synced_original_or_nothing(
    client: TestClient,
) -> None:
    """There is exactly one candidate image for a Reverse, because Back
    Images are never upscaled — build_pdf cover-fits and resizes the
    original at export time instead."""
    data, digest = _png()
    assert backs.resolve_print_source(digest) is None
    assert backs.resolve_print_source(None) is None

    client.post(f"/api/backs/{digest}", content=data)
    assert backs.resolve_print_source(digest) == backs.original_path(digest)


def test_oversized_uploads_are_refused(client: TestClient, monkeypatch) -> None:
    monkeypatch.setattr(backs, "MAX_UPLOAD_BYTES", 128)
    data, digest = _png()
    assert client.post(f"/api/backs/{digest}", content=data).status_code == 400


def test_the_backs_directory_is_a_sibling_of_the_wipe_targets(client: TestClient) -> None:
    """The exemption from clear_generated_data is structural, not a flag:
    that call empties output/ and cache/ by name, and
    prune_registry_under_dir scans under output/. Living beside both is
    what makes "Back Images survive the wipe" true without anyone
    remembering to keep it true."""
    paths = client.get("/api/paths").json()
    backs_dir = Path(paths["backs_dir"])
    assert backs_dir.name == backs.BACKS_DIR_NAME
    assert backs_dir.parent == Path(paths["output_dir"]).parent
    assert backs_dir != Path(paths["output_dir"])
    assert backs_dir != Path(paths["cache_dir"])


def test_clearing_generated_data_leaves_back_images_alone(
    client: TestClient, tmp_path: Path
) -> None:
    """The end-to-end version of the property above."""
    data, digest = _png()
    client.post(f"/api/backs/{digest}", content=data)
    paths = client.get("/api/paths").json()

    resp = client.post(
        "/api/generated-data/clear",
        json={"output_dir": paths["output_dir"], "cache_dir": paths["cache_dir"]},
    )
    assert resp.status_code == 200
    assert client.get(f"/api/backs/{digest}").json()["present"] is True


def test_discarding_a_tag_leaves_back_images_alone(client: TestClient) -> None:
    """The library is app-global, so a discarded tag has no claim on it —
    dropping a back here would be cross-project data loss."""
    data, digest = _png()
    client.post(f"/api/backs/{digest}", content=data)
    client.post("/api/tags/some-tag/discard")
    assert client.get(f"/api/backs/{digest}").json()["present"] is True


