"""Scryfall API client for resolving printings and expanding card faces."""

from __future__ import annotations

import time
from dataclasses import dataclass
from typing import Any
from urllib.parse import quote

import requests

from .decklist import DeckEntry

SCRYFALL_API = "https://api.scryfall.com"
USER_AGENT = "proxy-scaler/0.1.0 (home proxy printing; local tool)"
REQUEST_DELAY_S = 0.1


@dataclass(frozen=True)
class CardFaceImage:
    """One printable face with a PNG URL."""

    scryfall_id: str
    card_name: str
    face_name: str
    set_code: str
    collector_number: str
    png_url: str
    face_index: int | None  # None = single-faced; 0/1 = DFC front/back
    image_status: str | None = None
    # How many faces this printing actually has an image for (usually 2 for
    # DFC/transform, 1 otherwise — but Scryfall occasionally lacks a PNG for
    # one face, and expand_faces() already excludes that face below, so this
    # reflects "printable faces", not a layout assumption). None only for a
    # CardFaceImage built somewhere other than expand_faces().
    total_faces: int | None = None

    @property
    def is_dfc_face(self) -> bool:
        return self.face_index is not None

    @property
    def face_label(self) -> str | None:
        if self.face_index is None:
            return None
        return "front" if self.face_index == 0 else "back"


class ScryfallError(Exception):
    """Raised when Scryfall cannot resolve a card."""


class ScryfallClient:
    def __init__(
        self,
        session: requests.Session | None = None,
        delay_s: float = REQUEST_DELAY_S,
    ) -> None:
        self._session = session or requests.Session()
        self._session.headers.update(
            {
                "User-Agent": USER_AGENT,
                "Accept": "application/json",
            }
        )
        self._delay_s = delay_s
        self._last_request = 0.0

    def _throttle(self) -> None:
        elapsed = time.monotonic() - self._last_request
        if elapsed < self._delay_s:
            time.sleep(self._delay_s - elapsed)

    def _get(self, path: str, params: dict[str, str] | None = None) -> dict[str, Any]:
        self._throttle()
        url = f"{SCRYFALL_API}{path}"
        resp = self._session.get(url, params=params, timeout=30)
        self._last_request = time.monotonic()
        if resp.status_code == 404:
            raise ScryfallError(f"Card not found: {url} params={params}")
        if not resp.ok:
            raise ScryfallError(
                f"Scryfall HTTP {resp.status_code} for {url}: {resp.text[:200]}"
            )
        return resp.json()

    def _post(self, path: str, json: dict[str, Any]) -> dict[str, Any]:
        self._throttle()
        url = f"{SCRYFALL_API}{path}"
        resp = self._session.post(url, json=json, timeout=30)
        self._last_request = time.monotonic()
        if not resp.ok:
            raise ScryfallError(
                f"Scryfall HTTP {resp.status_code} for {url}: {resp.text[:200]}"
            )
        return resp.json()

    def fetch_by_set_collector(self, set_code: str, collector: str) -> dict[str, Any]:
        # Collector numbers may contain letters/hyphens; URL-encode the path segment
        code = quote(set_code.lower(), safe="")
        number = quote(collector, safe="")
        return self._get(f"/cards/{code}/{number}")

    def fetch_by_name(self, name: str) -> dict[str, Any]:
        # Prefer fuzzy named lookup (mpc-scryfall style)
        return self._get("/cards/named", params={"fuzzy": name})

    def resolve_collection(
        self, identifiers: list[dict[str, str]], *, chunk_size: int = 75
    ) -> tuple[list[dict[str, Any]], list[dict[str, Any]]]:
        """Resolve many cards in one or more batched requests via Scryfall's
        /cards/collection endpoint — their recommended way to look up many
        cards at once (up to `chunk_size` identifiers per request), instead
        of one request per card. Returns (data, not_found)."""
        data: list[dict[str, Any]] = []
        not_found: list[dict[str, Any]] = []
        for i in range(0, len(identifiers), chunk_size):
            chunk = identifiers[i : i + chunk_size]
            resp = self._post("/cards/collection", {"identifiers": chunk})
            data.extend(resp.get("data", []))
            not_found.extend(resp.get("not_found", []))
        return data, not_found

    def resolve(self, entry: DeckEntry) -> tuple[dict[str, Any], list[str]]:
        """Resolve a deck entry to a Scryfall card object.

        Returns (card_json, warnings).
        """
        warnings: list[str] = []
        if entry.has_exact_printing:
            assert entry.set_code is not None and entry.collector_number is not None
            card = self.fetch_by_set_collector(entry.set_code, entry.collector_number)
            warning = _exact_printing_warning(entry, card)
            if warning:
                warnings.append(warning)
        else:
            card = self.fetch_by_name(entry.name)
            warnings.append(_name_only_note(card))
        return card, warnings

    def resolve_many(
        self, entries: list[DeckEntry]
    ) -> list[tuple[dict[str, Any], list[str]] | ScryfallError]:
        """Resolve many deck entries efficiently.

        Exact-printing entries are batch-resolved via resolve_collection()
        (a handful of requests instead of one per card). Name-only entries
        still resolve individually via fuzzy name search — the collection
        endpoint only does exact name matching, which would silently break
        fuzzy resolution for typo'd or partial names.

        Positionally aligned with `entries`: each slot is either a
        successful (card_json, warnings) or a ScryfallError representing
        that entry's own failure, mirroring how process_entries()
        independently try/excepts each entry today so one bad card doesn't
        abort the rest.
        """
        results: list[tuple[dict[str, Any], list[str]] | ScryfallError] = [
            None  # type: ignore[list-item]
        ] * len(entries)

        exact_indices = [i for i, e in enumerate(entries) if e.has_exact_printing]
        if exact_indices:
            identifiers = [
                {
                    "set": entries[i].set_code,
                    "collector_number": entries[i].collector_number,
                }
                for i in exact_indices
            ]
            try:
                data, _not_found = self.resolve_collection(identifiers)
            except ScryfallError:
                # Batch call itself failed — fall back to resolving every
                # exact-printing entry individually rather than failing
                # the whole run; batching is a speed optimization, not a
                # new failure mode.
                data = []

            by_key: dict[tuple[str, str], dict[str, Any]] = {
                (str(card.get("set", "")).lower(), str(card.get("collector_number", ""))): card
                for card in data
            }

            for i in exact_indices:
                entry = entries[i]
                key = (entry.set_code.lower(), str(entry.collector_number))
                card = by_key.get(key)
                if card is None:
                    # Missing from the batch response (genuinely not_found,
                    # or the whole batch call failed) — fall back to an
                    # individual request for a precise per-card error.
                    try:
                        card = self.fetch_by_set_collector(
                            entry.set_code, entry.collector_number
                        )
                    except ScryfallError as exc:
                        results[i] = exc
                        continue
                warning = _exact_printing_warning(entry, card)
                results[i] = (card, [warning] if warning else [])

        for i, entry in enumerate(entries):
            if entry.has_exact_printing:
                continue
            try:
                card = self.fetch_by_name(entry.name)
            except ScryfallError as exc:
                results[i] = exc
                continue
            results[i] = (card, [_name_only_note(card)])

        return results


def _exact_printing_warning(entry: DeckEntry, card: dict[str, Any]) -> str | None:
    returned = card.get("name", "")
    if _names_compatible(entry.name, returned):
        return None
    return (
        f"Name mismatch: list has {entry.name!r}, "
        f"Scryfall returned {returned!r} "
        f"({card.get('set')}/{card.get('collector_number')})"
    )


def _name_only_note(card: dict[str, Any]) -> str:
    return (
        f"Name-only lookup resolved to "
        f"{card.get('name')} ({card.get('set')}/{card.get('collector_number')}) "
        f"[image_status={card.get('image_status')}]"
    )


def _names_compatible(listed: str, returned: str) -> bool:
    """True if listed name reasonably matches Scryfall card name."""
    a = _normalize_name(listed)
    b = _normalize_name(returned)
    if a == b:
        return True
    # Allow front-face-only list names for DFCs
    if "//" in b:
        front = _normalize_name(b.split("//", 1)[0])
        if a == front:
            return True
    if "//" in a:
        front = _normalize_name(a.split("//", 1)[0])
        if front == b or front == _normalize_name(b.split("//", 1)[0]):
            return True
    return False


def _normalize_name(name: str) -> str:
    return " ".join(name.casefold().split())


def expand_faces(card: dict[str, Any]) -> list[CardFaceImage]:
    """Expand a Scryfall card into one or more printable face images.

    DFC / transform / MDFC: one image per face that has image_uris.
    Split / flip / adventure: single parent image_uris (one file).
    """
    scryfall_id = card["id"]
    card_name = card["name"]
    set_code = card["set"]
    collector = str(card["collector_number"])
    image_status = card.get("image_status")

    faces = card.get("card_faces") or []
    per_face_images = [
        (i, face)
        for i, face in enumerate(faces)
        if isinstance(face, dict) and face.get("image_uris", {}).get("png")
    ]

    if per_face_images:
        total_faces = len(per_face_images)
        results: list[CardFaceImage] = []
        for i, face in per_face_images:
            results.append(
                CardFaceImage(
                    scryfall_id=scryfall_id,
                    card_name=card_name,
                    face_name=face.get("name", card_name),
                    set_code=set_code,
                    collector_number=collector,
                    png_url=face["image_uris"]["png"],
                    face_index=i,
                    image_status=image_status,
                    total_faces=total_faces,
                )
            )
        return results

    image_uris = card.get("image_uris") or {}
    png = image_uris.get("png")
    if not png:
        raise ScryfallError(
            f"No PNG image for {card_name} ({set_code}/{collector})"
        )

    return [
        CardFaceImage(
            scryfall_id=scryfall_id,
            card_name=card_name,
            face_name=card_name,
            set_code=set_code,
            collector_number=collector,
            png_url=png,
            face_index=None,
            image_status=image_status,
            total_faces=1,
        )
    ]


def download_png(url: str, session: requests.Session | None = None) -> bytes:
    sess = session or requests.Session()
    # .update(), not .setdefault(): requests.Session() already pre-populates
    # headers with its own default User-Agent (python-requests/X.Y.Z), which
    # setdefault() would never override — and Scryfall's CDN rejects that
    # default UA with a 400.
    sess.headers.update({"User-Agent": USER_AGENT})
    resp = sess.get(url, timeout=60)
    if not resp.ok:
        raise ScryfallError(
            f"Scryfall HTTP {resp.status_code} downloading {url}: {resp.text[:200]}"
        )
    return resp.content
