"""Google Drive image fetching (proxy_scaler/drive_images.py): the
original download's fallback order, size cap and content sniffing, and
the thumbnail disk cache. Fake session only; never touches the network."""

from __future__ import annotations

import io
import os
import time
from pathlib import Path

import pytest
from PIL import Image

from proxy_scaler import drive_images

DRIVE_ID = "1taR38keSBJgplwmZkX3NETXQSblhPYSh"


def _png() -> bytes:
    buf = io.BytesIO()
    Image.new("RGB", (8, 11), (10, 20, 30)).save(buf, format="PNG")
    return buf.getvalue()


def _jpeg() -> bytes:
    buf = io.BytesIO()
    Image.new("RGB", (8, 11), (10, 20, 30)).save(buf, format="JPEG")
    return buf.getvalue()


class _Resp:
    def __init__(self, content: bytes = b"", status_code: int = 200):
        self.content = content
        self.status_code = status_code
        self.ok = status_code < 400

    def iter_content(self, size):
        for i in range(0, len(self.content), max(1, size)):
            yield self.content[i : i + size]

    def __enter__(self):
        return self

    def __exit__(self, *exc):
        return False


class _FakeSession:
    def __init__(self, answers: dict[str, _Resp]):
        self.answers = answers
        self.urls: list[str] = []

    def get(self, url, **kwargs):
        self.urls.append(url)
        for prefix, resp in self.answers.items():
            if url.startswith(prefix):
                return resp
        return _Resp(b"", 404)


@pytest.fixture
def fake(monkeypatch):
    def install(answers: dict[str, _Resp]) -> _FakeSession:
        sess = _FakeSession(answers)
        monkeypatch.setattr(drive_images, "_session", sess)
        return sess

    yield install
    drive_images._reset_for_tests()


def test_original_comes_from_the_image_server_first(fake) -> None:
    sess = fake({drive_images.LH3_BASE: _Resp(_png())})
    data, fmt = drive_images.download_original(DRIVE_ID)
    assert fmt == "png" and data == _png()
    assert sess.urls == [f"{drive_images.LH3_BASE}/{DRIVE_ID}=s0"]


def test_original_falls_back_to_drive_download(fake) -> None:
    sess = fake(
        {
            drive_images.LH3_BASE: _Resp(b"", 500),
            drive_images.DRIVE_DOWNLOAD_URL: _Resp(_jpeg()),
        }
    )
    data, fmt = drive_images.download_original(DRIVE_ID)
    assert fmt == "jpeg"
    assert len(sess.urls) == 2 and sess.urls[1].startswith(drive_images.DRIVE_DOWNLOAD_URL)


def test_an_html_page_is_never_mistaken_for_the_image(fake) -> None:
    page = b"<!DOCTYPE html><html>Download quota exceeded</html>"
    fake({drive_images.LH3_BASE: _Resp(page), drive_images.DRIVE_DOWNLOAD_URL: _Resp(page)})
    with pytest.raises(drive_images.DriveImageError, match="daily download limit"):
        drive_images.download_original(DRIVE_ID)


def test_quota_status_gives_the_daily_limit_message(fake) -> None:
    fake(
        {
            drive_images.LH3_BASE: _Resp(b"", 500),
            drive_images.DRIVE_DOWNLOAD_URL: _Resp(b"", 403),
        }
    )
    with pytest.raises(drive_images.DriveImageError, match="daily limit"):
        drive_images.download_original(DRIVE_ID)


def test_oversized_download_is_abandoned(fake, monkeypatch) -> None:
    monkeypatch.setattr(drive_images, "MAX_ORIGINAL_BYTES", 16)
    monkeypatch.setattr(drive_images, "_CHUNK", 4)
    fake({drive_images.LH3_BASE: _Resp(_png()), drive_images.DRIVE_DOWNLOAD_URL: _Resp(_png())})
    with pytest.raises(drive_images.DriveImageError, match="larger than"):
        drive_images.download_original(DRIVE_ID)


@pytest.mark.parametrize("bad", ["", "short", "../../etc/passwd" + "a" * 20, "a b" * 10])
def test_bad_ids_are_rejected_before_any_request(fake, bad) -> None:
    sess = fake({})
    with pytest.raises(ValueError):
        drive_images.download_original(bad)
    with pytest.raises(ValueError):
        drive_images.thumbnail(bad, "small", cache_dir=Path("."))
    assert sess.urls == []


def test_thumbnail_is_fetched_once_then_served_from_disk(fake, tmp_path) -> None:
    sess = fake({drive_images.LH3_BASE: _Resp(_jpeg())})
    first = drive_images.thumbnail(DRIVE_ID, "small", cache_dir=tmp_path)
    second = drive_images.thumbnail(DRIVE_ID, "small", cache_dir=tmp_path)
    assert first == second == _jpeg()
    assert sess.urls == [f"{drive_images.LH3_BASE}/{DRIVE_ID}=h400-rj"]
    drive_images.thumbnail(DRIVE_ID, "large", cache_dir=tmp_path)
    assert sess.urls[-1].endswith("=h800-rj")


def test_stale_thumbnail_is_refreshed(fake, tmp_path) -> None:
    sess = fake({drive_images.LH3_BASE: _Resp(_jpeg())})
    drive_images.thumbnail(DRIVE_ID, "small", cache_dir=tmp_path)
    path = drive_images.thumb_cache_path(tmp_path, DRIVE_ID, "small")
    t = time.time() - drive_images.THUMB_REFRESH_S - 60
    os.utime(path, (t, t))
    drive_images.thumbnail(DRIVE_ID, "small", cache_dir=tmp_path)
    assert len(sess.urls) == 2


def test_thumbnail_failure_raises_and_caches_nothing(fake, tmp_path) -> None:
    fake({drive_images.LH3_BASE: _Resp(b"", 500)})
    with pytest.raises(drive_images.DriveImageError):
        drive_images.thumbnail(DRIVE_ID, "small", cache_dir=tmp_path)
    assert not drive_images.thumb_cache_path(tmp_path, DRIVE_ID, "small").exists()
