"""Tests for the set-symbol cache (proxy_scaler/set_icons.py): resolving a
code through the /sets index, caching both layers on disk, throttling
index refreshes for unknown codes, and refusing anything that isn't a
small SVG from Scryfall's icon host."""

from __future__ import annotations

import os
import time
from pathlib import Path

from proxy_scaler import set_icons

_SVG = b'<svg xmlns="http://www.w3.org/2000/svg"><path d="M0 0h1v1z"/></svg>'
_DMU = "https://svgs.scryfall.io/sets/dmu.svg?1791777600"


class _StubResponse:
    def __init__(self, *, json_data=None, content: bytes = b"", content_type="", status_code=200):
        self._json = json_data
        self.content = content
        self.status_code = status_code
        self.ok = status_code < 400
        self.headers = {"Content-Type": content_type}

    def raise_for_status(self) -> None:
        if not self.ok:
            raise set_icons.requests.HTTPError(str(self.status_code))

    def json(self):
        return self._json


class _StubSession:
    """Answers /sets with `sets` and any icon URL with `icon`; records every
    URL asked for."""

    def __init__(self, sets=None, icon=None):
        self.sets = sets if sets is not None else [
            {"code": "dmu", "icon_svg_uri": _DMU},
            {"code": "pdmu", "icon_svg_uri": _DMU},
        ]
        self.icon = icon if icon is not None else _StubResponse(
            content=_SVG, content_type="image/svg+xml"
        )
        self.urls: list[str] = []

    def get(self, url, **kwargs):
        self.urls.append(url)
        if url == set_icons.SETS_URL:
            return _StubResponse(json_data={"data": self.sets})
        return self.icon


def test_promo_code_shares_parent_icon_file(tmp_path: Path) -> None:
    sess = _StubSession()
    assert set_icons.get_set_icon("PDMU", tmp_path, session=sess) == _SVG
    assert (tmp_path / "set_icons" / "dmu.svg").read_bytes() == _SVG
    # dmu resolves to the same cached file: no further requests at all.
    assert set_icons.get_set_icon("dmu", tmp_path, session=sess) == _SVG
    assert sess.urls == [set_icons.SETS_URL, _DMU]


def test_unknown_code_refreshes_index_once_per_window(tmp_path: Path) -> None:
    sess = _StubSession()
    assert set_icons.get_set_icon("zzz", tmp_path, session=sess) is None
    assert set_icons.get_set_icon("zzz", tmp_path, session=sess) is None
    assert sess.urls == [set_icons.SETS_URL]


def test_unknown_code_refetches_a_stale_index(tmp_path: Path) -> None:
    sess = _StubSession()
    set_icons.get_set_icon("dmu", tmp_path, session=sess)
    index = tmp_path / "set_icons" / set_icons.INDEX_FILENAME
    old = time.time() - set_icons.INDEX_REFRESH_S - 1
    os.utime(index, (old, old))
    sess.sets = sess.sets + [{"code": "new", "icon_svg_uri": "https://svgs.scryfall.io/sets/new.svg"}]
    assert set_icons.get_set_icon("new", tmp_path, session=sess) == _SVG
    assert sess.urls.count(set_icons.SETS_URL) == 2


def test_invalid_code_never_touches_network(tmp_path: Path) -> None:
    sess = _StubSession()
    for code in ("../etc", "", "a", "dmu.svg"):
        assert set_icons.get_set_icon(code, tmp_path, session=sess) is None
    assert sess.urls == []


def test_icon_url_on_another_host_is_ignored(tmp_path: Path) -> None:
    sess = _StubSession(sets=[{"code": "dmu", "icon_svg_uri": "https://evil.example/sets/dmu.svg"}])
    assert set_icons.get_set_icon("dmu", tmp_path, session=sess) is None
    assert sess.urls == [set_icons.SETS_URL]


def test_rejects_non_svg_and_oversized_icons(tmp_path: Path) -> None:
    html = _StubResponse(content=b"<html>", content_type="text/html")
    assert set_icons.get_set_icon("dmu", tmp_path, session=_StubSession(icon=html)) is None
    huge = _StubResponse(
        content=b"<svg>" + b" " * set_icons.MAX_ICON_BYTES, content_type="image/svg+xml"
    )
    assert set_icons.get_set_icon("dmu", tmp_path, session=_StubSession(icon=huge)) is None
    assert not (tmp_path / "set_icons" / "dmu.svg").exists()


def test_failed_icon_fetch_is_not_cached(tmp_path: Path) -> None:
    down = _StubResponse(status_code=503, content_type="text/plain")
    assert set_icons.get_set_icon("dmu", tmp_path, session=_StubSession(icon=down)) is None
    assert set_icons.get_set_icon("dmu", tmp_path, session=_StubSession()) == _SVG
