"""Back Image store + endpoints (proxy_scaler/backs.py, api/routers/backs.py).

The generation server's half of the Back Library. The client owns the
canonical copy (docs/adr/0003), so everything here is a cache keyed by
content hash — which is exactly why the hash checks are worth testing:
a cache that stores the wrong bytes under a hash is wrong forever after.
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


def test_synthetic_id_has_no_characters_illegal_in_a_filename() -> None:
    """The id is interpolated straight into a filename by
    upscale.original_cache_path. A colon — the obvious separator, and the
    first one used — is an alternate-data-stream separator on Windows, so
    the file becomes unopenable on one of the three shipped platforms."""
    sid = backs.synthetic_id("a" * 64)
    assert ":" not in sid
    assert not set(sid) & set('<>:"/\\|?*')
    assert backs.hash_from_id(sid) == "a" * 64


def test_a_back_image_id_is_recognisable_as_one() -> None:
    assert backs.is_back_image_id(backs.synthetic_id("b" * 64))
    assert not backs.is_back_image_id("5d59c8f2-f6af-40a6-8dfe-8cc45bf231ce")
    assert not backs.is_back_image_id(None)


def test_upload_is_idempotent_and_reports_status(client: TestClient) -> None:
    data, digest = _png()
    assert client.get(f"/api/backs/{digest}").json()["present"] is False
    assert client.post(f"/api/backs/{digest}", content=data).status_code == 200
    # Re-syncing identical bytes is a no-op, which is what lets the client
    # call this unconditionally before every render.
    assert client.post(f"/api/backs/{digest}", content=data).status_code == 200
    body = client.get(f"/api/backs/{digest}").json()
    assert body["present"] is True
    assert body["variants"] == []


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


def test_upscale_queues_an_untagged_task_in_the_backs_directory(
    client: TestClient, db_path: Path
) -> None:
    """Two properties in one: no project_tag (a Back Image is app-global,
    and tagging it would expose it to that tag's discard), and both
    directories pointing at `backs/` (which is what keeps its output
    outside clear_generated_data and prune_registry_under_dir)."""
    data, digest = _png()
    client.post(f"/api/backs/{digest}", content=data)
    resp = client.post(
        f"/api/backs/{digest}/upscale",
        json={"model": "ultrasharp_v2", "dpi_targets": [800], "weights_dir": "weights"},
    )
    assert resp.status_code == 202
    assert resp.json()["queued"] == 1

    [task] = client.get("/api/tasks").json()
    assert task["project_tag"] is None
    assert task["scryfall_id"] == backs.synthetic_id(digest)
    row = db.get_task(task["id"], db_path=db_path)
    assert Path(row.output_dir).name == backs.BACKS_DIR_NAME
    assert Path(row.cache_dir).name == backs.BACKS_DIR_NAME


def test_upscaling_seeds_the_cache_so_no_download_is_needed(client: TestClient) -> None:
    """The upload is stored at exactly the path _regenerate_faces probes
    before it reaches for png_url, so an upscale of a Back Image makes no
    network call — with no change to pipeline.py at all (docs/adr/0004)."""
    data, digest = _png()
    client.post(f"/api/backs/{digest}", content=data)
    from proxy_scaler.upscale import original_cache_path

    probed = original_cache_path(
        Path(backs.BACKS_DIR_NAME), backs.synthetic_id(digest), None
    )
    assert probed.is_file()
    assert probed == backs.original_path(digest)


def test_upscaling_an_unsynced_back_is_a_conflict_not_a_crash(client: TestClient) -> None:
    _data, digest = _png()
    resp = client.post(
        f"/api/backs/{digest}/upscale", json={"model": "ultrasharp_v2", "dpi_targets": [800]}
    )
    assert resp.status_code == 409


def test_upscale_rejects_unknown_models_and_dpis(client: TestClient) -> None:
    data, digest = _png()
    client.post(f"/api/backs/{digest}", content=data)
    bad_dpi = client.post(
        f"/api/backs/{digest}/upscale", json={"model": "ultrasharp_v2", "dpi_targets": [999]}
    )
    bad_model = client.post(
        f"/api/backs/{digest}/upscale", json={"model": "nope", "dpi_targets": [800]}
    )
    no_dpi = client.post(
        f"/api/backs/{digest}/upscale", json={"model": "ultrasharp_v2", "dpi_targets": []}
    )
    assert bad_dpi.status_code == bad_model.status_code == no_dpi.status_code == 400


def test_delete_removes_the_original_from_this_server_only(client: TestClient) -> None:
    data, digest = _png()
    client.post(f"/api/backs/{digest}", content=data)
    assert client.delete(f"/api/backs/{digest}").json()["removed"] == 1
    assert client.get(f"/api/backs/{digest}").json()["present"] is False


def test_clearing_upscales_keeps_the_synced_original(client: TestClient) -> None:
    """"Clear upscales" is the action that reclaims disk on a GPU box
    without losing anything that can't be rebuilt — so the original, which
    a re-upscale needs, has to survive it."""
    data, digest = _png()
    client.post(f"/api/backs/{digest}", content=data)
    client.delete(f"/api/backs/{digest}/variants")
    assert client.get(f"/api/backs/{digest}").json()["present"] is True


def test_a_missing_variant_falls_back_to_the_plain_original(
    client: TestClient, db_path: Path
) -> None:
    """Switching to a server that never upscaled this back must still
    print. Quality varies; correctness does not."""
    data, digest = _png()
    client.post(f"/api/backs/{digest}", content=data)
    path, not_upscaled = backs.resolve_print_source(digest, db_path=db_path)
    assert path == backs.original_path(digest)
    assert not_upscaled is True


def test_resolve_print_source_is_empty_when_nothing_is_synced(
    client: TestClient, db_path: Path
) -> None:
    _data, digest = _png()
    assert backs.resolve_print_source(digest, db_path=db_path) == (None, False)
    assert backs.resolve_print_source(None, db_path=db_path) == (None, False)


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
