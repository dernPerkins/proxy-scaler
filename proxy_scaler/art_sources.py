"""Where community card art is searched: the seam between the MPC Fill
routes and whatever answers them.

The one implementation is mpc_catalog.CatalogSource, a local index of the
MPC Fill uploaders' Google Drive folders, downloaded from our own daily
crawl (workers/mpc-catalog). Nothing on a user's machine ever calls
mpcfill.com: by agreement with MPC Fill's admin, the crawler's one request
a day is the only traffic it sees from Proxy Scaler. Without a catalog,
MPC Fill art search is simply unavailable (CatalogMissing) until the user
downloads one. See docs/adr/0007-mpc-art-catalog-on-cloudflare-workflows.md.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from pathlib import Path
from typing import Literal, Protocol

from . import __version__

# How Proxy Scaler's own requests identify themselves: to our catalog
# (catalog.proxy-scaler.com) and to Google for MPC Fill art images.
USER_AGENT = f"proxy-scaler/{__version__} (+https://proxy-scaler.com)"
# Under the server's image cache: MPC Fill art thumbnails (drive_images.py).
CACHE_DIRNAME = "mpcfill"
# MPC Fill's own filter defaults, kept so results match what its search
# returns; maximumSize is in MB.
MAXIMUM_DPI = 1500
MAXIMUM_SIZE_MB = 30
NSFW_TAG = "NSFW"

CardType = Literal["CARD", "TOKEN", "CARDBACK"]
CARD_TYPES: tuple[CardType, ...] = ("CARD", "TOKEN", "CARDBACK")


class ArtSourceError(RuntimeError):
    """The art source can't answer right now. The message is shown to the
    user as-is."""


class CatalogMissing(ArtSourceError):
    """No MPC Fill catalog is downloaded on this server, so there is
    nothing to search. The routes answer 409 so the client can offer the
    download."""

    def __init__(self) -> None:
        super().__init__(
            "The MPC Fill catalog isn't downloaded yet. Download it from the "
            "Decklist sidebar to search MPC Fill art."
        )


@dataclass(frozen=True)
class ArtFilters:
    """Search filters, in the art source's own terms. `languages` are MPC
    Fill language codes ("EN", "JA" ...) and `sources` uploader names;
    empty means any."""

    languages: tuple[str, ...] = ()
    fuzzy: bool = False
    include_nsfw: bool = False
    min_dpi: int = 0
    sources: tuple[str, ...] = ()


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
    def search(self, queries: list[ArtQuery], filters: ArtFilters) -> dict[ArtQuery, list[str]]:
        """Ordered drive ids per query, best first."""
        ...

    def cards(self, drive_ids: list[str]) -> dict[str, ArtCard]:
        """Details for each known id; unknown ids are left out."""
        ...

    def uploaders(self, drive_ids: list[str]) -> list[tuple[str, int]]:
        """How many of `drive_ids` each uploader holds, most first."""
        ...

    def cardbacks(self, filters: ArtFilters) -> list[str]:
        """Every card back, best first, for browsing without a query."""
        ...


def get_art_source(cache_dir: Path) -> ArtSource:
    """The art source the routes use: the local catalog, which raises
    CatalogMissing until one is downloaded. `cache_dir` is unused now and
    kept for the routes' call shape. Tests monkeypatch this."""
    from . import mpc_catalog

    return mpc_catalog.CatalogSource(mpc_catalog.db_path_from_env())
