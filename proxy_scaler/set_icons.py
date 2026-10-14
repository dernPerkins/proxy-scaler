"""Set symbols for the client's set codes, cached on disk.

Scryfall's /sets lists every set with an `icon_svg_uri` on svgs.scryfall.io.
Promo, token and other child sets point at their parent's icon (pdmu and
tdmu both use dmu.svg), so the 1000-odd sets share a few hundred files.
Both layers are cached under the generation server's cache directory, in
`set_icons/`:

- `sets.json`, the set code -> icon URL index, fetched on first use and
  refetched only when asked for a code it lacks, at most once per
  INDEX_REFRESH_S, so a bogus code can't make every request hit Scryfall.
- `<slug>.svg`, one per distinct icon, fetched on first use and then kept
  forever. Set symbols don't change, and it keeps them working offline.

Nothing goes through the image Worker: it only proxies cards.scryfall.io.
"""

from __future__ import annotations

import json
import re
import threading
import time
from pathlib import Path
from urllib.parse import urlsplit

import requests

from .scryfall import SCRYFALL_API, USER_AGENT
from .upscale import atomic_write_bytes

SET_ICONS_DIRNAME = "set_icons"
INDEX_FILENAME = "sets.json"
SETS_URL = f"{SCRYFALL_API}/sets"
ICON_HOST = "svgs.scryfall.io"
INDEX_REFRESH_S = 6 * 60 * 60
MAX_ICON_BYTES = 64 * 1024
_TIMEOUT_S = (5, 30)

_SET_CODE = re.compile(r"^[a-z0-9]{2,8}$")
_SLUG = re.compile(r"^[a-z0-9_-]+$")

_index_lock = threading.Lock()


def _session(session: requests.Session | None) -> requests.Session:
    if session is not None:
        return session
    sess = requests.Session()
    sess.headers.update({"User-Agent": USER_AGENT})
    return sess


def _icon_slug(icon_url: str) -> str | None:
    """`dmu` for https://svgs.scryfall.io/sets/dmu.svg?1791777600, or None
    for a URL on another host or with a name we won't use as a filename."""
    parts = urlsplit(icon_url)
    if parts.hostname != ICON_HOST or not parts.path.endswith(".svg"):
        return None
    slug = parts.path.rsplit("/", 1)[-1][: -len(".svg")]
    return slug if _SLUG.match(slug) else None


def _read_index(path: Path) -> dict[str, str] | None:
    try:
        data = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, ValueError):
        return None
    return data if isinstance(data, dict) else None


def _fetch_index(sess: requests.Session) -> dict[str, str]:
    resp = sess.get(SETS_URL, headers={"Accept": "application/json"}, timeout=_TIMEOUT_S)
    resp.raise_for_status()
    index: dict[str, str] = {}
    for s in resp.json().get("data", []):
        code, url = s.get("code"), s.get("icon_svg_uri")
        if isinstance(code, str) and isinstance(url, str) and _icon_slug(url):
            index[code.lower()] = url
    return index


def _icon_url(code: str, icons_dir: Path, sess: requests.Session) -> str | None:
    index_path = icons_dir / INDEX_FILENAME
    with _index_lock:
        index = _read_index(index_path)
        if index is not None and code in index:
            return index[code]
        if index is not None:
            try:
                age = time.time() - index_path.stat().st_mtime
            except OSError:
                age = INDEX_REFRESH_S
            if age < INDEX_REFRESH_S:
                return None
        try:
            index = _fetch_index(sess)
        except (requests.RequestException, ValueError):
            return None
        icons_dir.mkdir(parents=True, exist_ok=True)
        atomic_write_bytes(index_path, json.dumps(index).encode("utf-8"))
        return index.get(code)


def _fetch_icon(url: str, sess: requests.Session) -> bytes | None:
    try:
        resp = sess.get(url, timeout=_TIMEOUT_S)
    except requests.RequestException:
        return None
    content_type = resp.headers.get("Content-Type", "")
    if not resp.ok or not content_type.startswith("image/svg+xml"):
        return None
    if len(resp.content) > MAX_ICON_BYTES:
        return None
    return resp.content


def get_set_icon(
    code: str, cache_dir: Path, session: requests.Session | None = None
) -> bytes | None:
    """The SVG symbol for set `code`, or None when the code isn't a set
    Scryfall knows, or the icon isn't cached and can't be fetched."""
    code = code.lower()
    if not _SET_CODE.match(code):
        return None
    icons_dir = Path(cache_dir) / SET_ICONS_DIRNAME
    sess = _session(session)
    url = _icon_url(code, icons_dir, sess)
    slug = _icon_slug(url) if url else None
    if slug is None:
        return None
    path = icons_dir / f"{slug}.svg"
    try:
        return path.read_bytes()
    except OSError:
        pass
    data = _fetch_icon(url, sess)
    if data is None:
        return None
    icons_dir.mkdir(parents=True, exist_ok=True)
    atomic_write_bytes(path, data)
    return data
