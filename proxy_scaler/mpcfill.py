"""MPC Fill (mpcfill.com, the hosted MPC Autofill) as an art source.

mpcfill.com has no documented public API; its JSON routes are what its
own web app calls. We use four of them and nothing else:

- GET  /2/sources/      the uploaders, needed by every search
- POST /2/editorSearch/ drive ids per (query, card type), up to 300 queries
- POST /2/cards/        details per drive id, up to 1000 ids
- POST /2/cardbacks/    every card back, for browsing without a query

Images never come from MPC Fill's hosts (img./cdn.mpcautofill.com): the
thumbnail and download URLs in its responses are dropped here, and
drive_images.py fetches from Google instead. The aim is to cost the MPC
Fill maintainer as little as possible:

- sources, searches and card-back listings are cached on disk for a day,
  and a failed sources refresh falls back to the stale copy;
- card details are cached in memory for a day;
- calls are serialised with a minimum gap, and identify Proxy Scaler.

See docs/adr/0006-mpc-fill-art-via-generation-server.md.
"""

from __future__ import annotations

import hashlib
import json
import threading
import time
from pathlib import Path
from typing import Any

import requests

from . import __version__
from .art_sources import ArtCard, ArtFilters, ArtQuery, ArtSourceError
from .upscale import atomic_write_bytes

__all__ = [
    "ArtFilters",
    "ArtQuery",
    "MpcFillSource",
    "MPCFILL_BASE",
    "USER_AGENT",
    "normalise_query",
    "search_settings",
]

MPCFILL_BASE = "https://mpcfill.com"
USER_AGENT = f"proxy-scaler/{__version__} (+https://proxy-scaler.com)"
CACHE_DIRNAME = "mpcfill"

SOURCES_TTL_S = 24 * 60 * 60
SEARCH_TTL_S = 24 * 60 * 60
CARDS_TTL_S = 24 * 60 * 60
MAX_QUERIES_PER_CALL = 300
MAX_IDS_PER_CALL = 1000
# Room for a long Drive-backed catalogue session without growing forever.
MAX_CACHED_CARDS = 50_000
MIN_CALL_GAP_S = 0.25
TIMEOUT_S = (5, 30)

# MPC Fill's own filter defaults; maximumSize is in MB.
MAXIMUM_DPI = 1500
MAXIMUM_SIZE_MB = 30
NSFW_TAG = "NSFW"

_session: requests.Session | None = None
_call_lock = threading.Lock()
_last_call_at = 0.0
_cards_lock = threading.Lock()
_cards: dict[str, tuple[float, ArtCard]] = {}


def _reset_for_tests() -> None:
    global _session, _last_call_at
    with _call_lock:
        _session = None
        _last_call_at = 0.0
    with _cards_lock:
        _cards.clear()


def _shared_session() -> requests.Session:
    global _session
    if _session is None:
        sess = requests.Session()
        sess.headers.update({"User-Agent": USER_AGENT, "Accept": "application/json"})
        _session = sess
    return _session


def _call(method: str, path: str, body: dict[str, Any] | None = None) -> dict[str, Any]:
    """One request to mpcfill.com. Serialised under a lock with a minimum
    gap since the previous call, so a burst of clients can't fan out into
    a burst against MPC Fill."""
    global _last_call_at
    with _call_lock:
        wait = _last_call_at + MIN_CALL_GAP_S - time.monotonic()
        if wait > 0:
            time.sleep(wait)
        try:
            resp = _shared_session().request(
                method, f"{MPCFILL_BASE}{path}", json=body, timeout=TIMEOUT_S
            )
        except requests.RequestException as exc:
            raise ArtSourceError(f"Couldn't reach MPC Fill ({exc.__class__.__name__}).") from exc
        finally:
            _last_call_at = time.monotonic()
    if not resp.ok:
        raise ArtSourceError(f"MPC Fill answered HTTP {resp.status_code} for {path}.")
    try:
        data = resp.json()
    except ValueError as exc:
        raise ArtSourceError(f"MPC Fill sent an unreadable response for {path}.") from exc
    if not isinstance(data, dict):
        raise ArtSourceError(f"MPC Fill sent an unexpected response for {path}.")
    return data


def normalise_query(query: str) -> str:
    """MPC Fill matches on a sanitised lower-case name, so differently
    spaced or cased queries are one search (and one cache entry)."""
    return " ".join(query.lower().split())


def search_settings(filters: ArtFilters, source_pks: list[int]) -> dict[str, Any]:
    """The `searchSettings` object every MPC Fill search takes. Sources
    must be listed explicitly; null is rejected."""
    return {
        "searchTypeSettings": {"fuzzySearch": filters.fuzzy, "filterCardbacks": False},
        "sourceSettings": {"sources": [[pk, True] for pk in source_pks]},
        "filterSettings": {
            "minimumDPI": max(0, filters.min_dpi),
            "maximumDPI": MAXIMUM_DPI,
            "maximumSize": MAXIMUM_SIZE_MB,
            "languages": list(filters.languages),
            "includesTags": [],
            "excludesTags": [] if filters.include_nsfw else [NSFW_TAG],
        },
    }


def _parse_card(raw: Any) -> ArtCard | None:
    """An ArtCard from one /2/cards/ entry, or None if it isn't usable.
    Thumbnail and download URLs are deliberately not kept."""
    if not isinstance(raw, dict):
        return None
    drive_id = raw.get("identifier")
    name = raw.get("name")
    if not isinstance(drive_id, str) or not drive_id or not isinstance(name, str):
        return None
    tags = raw.get("tags")
    try:
        return ArtCard(
            drive_id=drive_id,
            name=name,
            card_type=str(raw.get("cardType") or "CARD"),
            extension=str(raw.get("extension") or ""),
            dpi=int(raw.get("dpi") or 0),
            size_bytes=int(raw.get("size") or 0),
            language=str(raw.get("language") or ""),
            source_name=str(raw.get("sourceName") or raw.get("source") or ""),
            date_created=str(raw.get("dateCreated") or ""),
            tags=tuple(str(t) for t in tags) if isinstance(tags, list) else (),
        )
    except (TypeError, ValueError):
        return None


class MpcFillSource:
    """ArtSource backed by mpcfill.com, with its caches under
    `<cache_dir>/mpcfill/`. Cheap to construct per request: the session
    and the in-memory card cache are module-wide."""

    def __init__(self, cache_dir: Path) -> None:
        self.root = Path(cache_dir) / CACHE_DIRNAME

    # -- disk cache -------------------------------------------------------

    def _read_fresh(self, path: Path, ttl_s: float) -> Any | None:
        try:
            if time.time() - path.stat().st_mtime > ttl_s:
                return None
            return json.loads(path.read_text(encoding="utf-8"))
        except (OSError, ValueError):
            return None

    def _read_any(self, path: Path) -> Any | None:
        try:
            return json.loads(path.read_text(encoding="utf-8"))
        except (OSError, ValueError):
            return None

    def _write(self, path: Path, data: Any) -> None:
        path.parent.mkdir(parents=True, exist_ok=True)
        atomic_write_bytes(path, json.dumps(data).encode("utf-8"))

    # -- sources ----------------------------------------------------------

    def source_pks(self) -> list[int]:
        """The Google Drive sources' pks, in MPC Fill's order. Refreshed
        once a day; if the refresh fails the stale list is used, and only
        with no list at all does this raise (a failure is never cached)."""
        path = self.root / "sources.json"
        cached = self._read_fresh(path, SOURCES_TTL_S)
        if isinstance(cached, list):
            return cached
        try:
            data = _call("GET", "/2/sources/")
        except ArtSourceError:
            stale = self._read_any(path)
            if isinstance(stale, list) and stale:
                return stale
            raise
        results = data.get("results")
        if not isinstance(results, dict):
            raise ArtSourceError("MPC Fill sent an unexpected sources list.")
        pks: list[int] = []
        for raw in results.values():
            if isinstance(raw, dict) and raw.get("sourceType") == "Google Drive":
                try:
                    pks.append(int(raw["pk"]))
                except (KeyError, TypeError, ValueError):
                    continue
        if not pks:
            raise ArtSourceError("MPC Fill listed no Google Drive sources.")
        self._write(path, pks)
        return pks

    def _settings_key(self, filters: ArtFilters, pks: list[int]) -> dict[str, Any]:
        return {
            "languages": sorted(filters.languages),
            "fuzzy": filters.fuzzy,
            "nsfw": filters.include_nsfw,
            "min_dpi": filters.min_dpi,
            "sources": hashlib.sha256(json.dumps(pks).encode()).hexdigest()[:16],
        }

    @staticmethod
    def _digest(obj: Any) -> str:
        return hashlib.sha256(json.dumps(obj, sort_keys=True).encode()).hexdigest()

    # -- ArtSource --------------------------------------------------------

    def search(
        self, queries: list[ArtQuery], filters: ArtFilters, *, refresh: bool = False
    ) -> dict[ArtQuery, list[str]]:
        pks = self.source_pks()
        settings_key = self._settings_key(filters, pks)
        out: dict[ArtQuery, list[str]] = {}
        # (normalised query, card type) -> cache path, for the misses.
        misses: dict[tuple[str, str], Path] = {}
        for q in queries:
            key = (normalise_query(q.query), q.card_type)
            path = self.root / "search" / (
                self._digest({"q": key[0], "type": key[1], **settings_key}) + ".json"
            )
            cached = None if refresh else self._read_fresh(path, SEARCH_TTL_S)
            if isinstance(cached, list):
                out[q] = cached
            else:
                misses[key] = path

        found: dict[tuple[str, str], list[str]] = {}
        keys = [k for k in misses if k[0]]
        settings = search_settings(filters, pks)
        for start in range(0, len(keys), MAX_QUERIES_PER_CALL):
            chunk = keys[start : start + MAX_QUERIES_PER_CALL]
            data = _call(
                "POST",
                "/2/editorSearch/",
                {
                    "searchSettings": settings,
                    "queries": [{"query": q, "cardType": t} for q, t in chunk],
                },
            )
            results = data.get("results")
            if not isinstance(results, dict):
                raise ArtSourceError("MPC Fill sent an unexpected search response.")
            for q, t in chunk:
                ids = (results.get(q) or {}).get(t) or []
                ids = [i for i in ids if isinstance(i, str)]
                found[(q, t)] = ids
                self._write(misses[(q, t)], ids)

        for q in queries:
            if q not in out:
                out[q] = found.get((normalise_query(q.query), q.card_type), [])
        return out

    def cards(self, drive_ids: list[str]) -> dict[str, ArtCard]:
        now = time.monotonic()
        out: dict[str, ArtCard] = {}
        missing: list[str] = []
        with _cards_lock:
            for drive_id in dict.fromkeys(drive_ids):
                hit = _cards.get(drive_id)
                if hit is not None and hit[0] > now:
                    out[drive_id] = hit[1]
                else:
                    missing.append(drive_id)
        for start in range(0, len(missing), MAX_IDS_PER_CALL):
            chunk = missing[start : start + MAX_IDS_PER_CALL]
            data = _call("POST", "/2/cards/", {"cardIdentifiers": chunk})
            results = data.get("results")
            if not isinstance(results, dict):
                raise ArtSourceError("MPC Fill sent an unexpected card response.")
            parsed = [c for c in (_parse_card(r) for r in results.values()) if c]
            expires = time.monotonic() + CARDS_TTL_S
            with _cards_lock:
                if len(_cards) + len(parsed) > MAX_CACHED_CARDS:
                    _cards.clear()
                for card in parsed:
                    _cards[card.drive_id] = (expires, card)
            for card in parsed:
                out[card.drive_id] = card
        return {i: out[i] for i in drive_ids if i in out}

    def cardbacks(self, filters: ArtFilters, *, refresh: bool = False) -> list[str]:
        pks = self.source_pks()
        path = self.root / "cardbacks" / (
            self._digest(self._settings_key(filters, pks)) + ".json"
        )
        cached = None if refresh else self._read_fresh(path, SEARCH_TTL_S)
        if isinstance(cached, list):
            return cached
        data = _call("POST", "/2/cardbacks/", {"searchSettings": search_settings(filters, pks)})
        ids = data.get("cardbacks")
        if not isinstance(ids, list):
            raise ArtSourceError("MPC Fill sent an unexpected card back list.")
        ids = [i for i in ids if isinstance(i, str)]
        self._write(path, ids)
        return ids
