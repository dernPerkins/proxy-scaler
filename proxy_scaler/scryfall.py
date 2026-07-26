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

    def fetch_by_set_collector(self, set_code: str, collector: str) -> dict[str, Any]:
        # Collector numbers may contain letters/hyphens; URL-encode the path segment
        code = quote(set_code.lower(), safe="")
        number = quote(collector, safe="")
        return self._get(f"/cards/{code}/{number}")

    def fetch_by_name(self, name: str) -> dict[str, Any]:
        # Prefer fuzzy named lookup (mpc-scryfall style)
        return self._get("/cards/named", params={"fuzzy": name})

    def resolve(self, entry: DeckEntry) -> tuple[dict[str, Any], list[str]]:
        """Resolve a deck entry to a Scryfall card object.

        Returns (card_json, warnings).
        """
        warnings: list[str] = []
        if entry.has_exact_printing:
            assert entry.set_code is not None and entry.collector_number is not None
            card = self.fetch_by_set_collector(entry.set_code, entry.collector_number)
            returned = card.get("name", "")
            if not _names_compatible(entry.name, returned):
                warnings.append(
                    f"Name mismatch: list has {entry.name!r}, "
                    f"Scryfall returned {returned!r} "
                    f"({card.get('set')}/{card.get('collector_number')})"
                )
        else:
            card = self.fetch_by_name(entry.name)
            warnings.append(
                f"Name-only lookup resolved to "
                f"{card.get('name')} ({card.get('set')}/{card.get('collector_number')}) "
                f"[image_status={card.get('image_status')}]"
            )
        return card, warnings


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
        )
    ]


def download_png(url: str, session: requests.Session | None = None) -> bytes:
    sess = session or requests.Session()
    sess.headers.setdefault("User-Agent", USER_AGENT)
    resp = sess.get(url, timeout=60)
    resp.raise_for_status()
    return resp.content
