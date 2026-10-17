"""Where community card art is searched: the seam between the MPC Fill
routes and whatever answers them.

Today the only implementation is mpcfill.MpcFillSource, which asks
mpcfill.com. A self-built catalogue (a local index of the same Google
Drive folders) can replace it without the routes, the client or the
library import noticing: they only see drive ids and ArtCards.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from pathlib import Path
from typing import Literal, Protocol

CardType = Literal["CARD", "TOKEN", "CARDBACK"]
CARD_TYPES: tuple[CardType, ...] = ("CARD", "TOKEN", "CARDBACK")


class ArtSourceError(RuntimeError):
    """The art source can't answer right now (offline, upstream down, an
    unexpected response). The message is shown to the user as-is."""


@dataclass(frozen=True)
class ArtFilters:
    """Search filters, in the art source's own terms. `languages` are MPC
    Fill language codes ("EN", "JA" ...); empty means any language."""

    languages: tuple[str, ...] = ()
    fuzzy: bool = False
    include_nsfw: bool = False
    min_dpi: int = 0


@dataclass(frozen=True)
class ArtQuery:
    query: str
    card_type: CardType


@dataclass(frozen=True)
class ArtCard:
    """One piece of art. `drive_id` is the Google Drive file id, which is
    also how its bytes are fetched (drive_images.py)."""

    drive_id: str
    name: str
    card_type: str
    extension: str
    dpi: int
    size_bytes: int
    language: str
    source_name: str
    date_created: str
    tags: tuple[str, ...] = field(default=())


class ArtSource(Protocol):
    def search(
        self, queries: list[ArtQuery], filters: ArtFilters, *, refresh: bool = False
    ) -> dict[ArtQuery, list[str]]:
        """Ordered drive ids per query, best first."""
        ...

    def cards(self, drive_ids: list[str]) -> dict[str, ArtCard]:
        """Details for each known id; unknown ids are left out."""
        ...

    def cardbacks(self, filters: ArtFilters, *, refresh: bool = False) -> list[str]:
        """Every card back, best first, for browsing without a query."""
        ...


def get_art_source(cache_dir: Path) -> ArtSource:
    """The art source the routes use. Tests monkeypatch this."""
    from .mpcfill import MpcFillSource

    return MpcFillSource(cache_dir)
