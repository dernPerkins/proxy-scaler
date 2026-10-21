"""MPC Fill endpoints: search community card art, preview it, and import
a pick into this server's Custom or Back Image store.

Everything MPC-related the client does goes through here, so the client
itself still makes no external network calls. Search answers come from
the locally downloaded MPC Fill catalog (art_sources.py, mpc_catalog.py)
and nothing else: no user's machine ever calls mpcfill.com, by agreement
with its admin. Without a catalog the search routes answer 409, which the
client turns into an offer to download one. Images come from Google Drive
(drive_images.py), each user's own server fetching them.
"""

from __future__ import annotations

import hashlib
import io
from pathlib import Path

from fastapi import APIRouter, HTTPException, Query, Response
from PIL import Image

from proxy_scaler import art_sources, backs, carddb, customs, drive_images
from proxy_scaler.api.deps import get_card_db_path
from proxy_scaler.api.routers.customs import store_custom_bytes
from proxy_scaler.api.routers.misc import DEFAULT_CACHE_DIR
from proxy_scaler.api.schemas import (
    MpcBestCardOut,
    MpcBestIn,
    MpcBestOut,
    MpcCardOut,
    MpcCardsIn,
    MpcCardsOut,
    MpcCardType,
    MpcImportIn,
    MpcSearchOut,
)
from proxy_scaler.art_sources import ArtCard, ArtFilters, ArtQuery, ArtSourceError, CatalogMissing

router = APIRouter(prefix="/api/mpcfill", tags=["mpcfill"])

# Ids hydrated with the search response; the client asks for the rest a
# page at a time as the grid scrolls.
FIRST_PAGE = 60

# Corpus layouts MPC Fill files under TOKEN rather than CARD.
TOKEN_LAYOUTS = frozenset({"token", "double_faced_token", "emblem"})

# Scryfall language codes -> MPC Fill's. Anything missing (ko, he, ...)
# has no MPC Fill equivalent, so it searches every language.
_LANGUAGES = {
    "en": "EN",
    "es": "ES",
    "fr": "FR",
    "de": "DE",
    "it": "IT",
    "pt": "PT",
    "ja": "JA",
    "ru": "RU",
    "zhs": "ZH",
    "zht": "ZH",
    "ar": "AR",
    "sa": "SA",
}
MPC_LANGUAGES = frozenset(_LANGUAGES.values())

_THUMB_CACHE = "public, max-age=604800"

def mpc_language(code: str | None) -> str | None:
    """An MPC Fill language code for a Scryfall or MPC Fill code, or None
    (search every language)."""
    if not code:
        return None
    if code.upper() in MPC_LANGUAGES:
        return code.upper()
    return _LANGUAGES.get(code.lower())


def _source():
    return art_sources.get_art_source(Path(DEFAULT_CACHE_DIR))


def _out(card: ArtCard) -> MpcCardOut:
    return MpcCardOut(
        drive_id=card.drive_id,
        name=card.name,
        card_type=card.card_type,
        extension=card.extension,
        dpi=card.dpi,
        size_bytes=card.size_bytes,
        language=card.language,
        source_name=card.source_name,
        date_created=card.date_created,
        tags=list(card.tags),
    )


_NO_CORPUS = "No local card database — import it from the Card database panel first."


def card_faces(card: dict) -> tuple[list[str], MpcCardType]:
    """The name to search for each face of a corpus card, in face order,
    and whether MPC Fill files it as a CARD or a TOKEN. A DFC has one
    image, so one MPC Fill file, per face; a card with one image for both
    halves (split, adventure, flip ...) is named after its front."""
    card_type: MpcCardType = "TOKEN" if card.get("layout") in TOKEN_LAYOUTS else "CARD"
    faces = [f for f in card.get("card_faces") or [] if isinstance(f, dict)]
    with_images = [f for f in faces if (f.get("image_uris") or {}).get("png")]
    if with_images:
        return [str(f.get("name") or card.get("name") or "") for f in with_images], card_type
    if faces and faces[0].get("name"):
        return [str(faces[0]["name"])], card_type
    return [str(card.get("name") or "")], card_type


def _corpus_face(scryfall_id: str, face: int) -> tuple[str, MpcCardType]:
    """The name to search for one face of a corpus card, and its type."""
    conn = carddb.open_if_ready(get_card_db_path())
    if conn is None:
        raise HTTPException(status_code=404, detail=_NO_CORPUS)
    try:
        card = carddb.get_card_by_id(conn, scryfall_id)
    finally:
        conn.close()
    if card is None:
        raise HTTPException(status_code=404, detail="Card not found in the local card database.")
    names, card_type = card_faces(card)
    if face >= len(names):
        raise HTTPException(status_code=404, detail=f"{card.get('name')} has no face {face}.")
    return names[face], card_type


def _art_error(exc: ArtSourceError) -> HTTPException:
    """409 when there is no catalog to search (the client offers the
    download), 502 for anything else the catalog can't answer."""
    return HTTPException(
        status_code=409 if isinstance(exc, CatalogMissing) else 502, detail=str(exc)
    )


def _hydrate(ids: list[str]) -> list[MpcCardOut]:
    try:
        found = _source().cards(ids)
    except ArtSourceError as exc:
        raise _art_error(exc) from exc
    return [_out(found[i]) for i in ids if i in found]


@router.get("/search", response_model=MpcSearchOut)
def search(
    q: str = "",
    type: MpcCardType | None = None,
    scryfall_id: str | None = None,
    face: int = Query(0, ge=0, le=1),
    lang: str | None = None,
    fuzzy: bool = False,
    nsfw: bool = False,
    min_dpi: int = Query(0, ge=0, le=1500),
) -> MpcSearchOut:
    """Art matching `q` (or, with `scryfall_id`, the name of that card's
    `face`, searched as a TOKEN when the corpus says it is one). A
    CARDBACK search with no query lists every card back. `lang` is a
    Scryfall or MPC Fill language code; omitted, every language."""
    query = q.strip()
    card_type: MpcCardType = type or "CARD"
    if scryfall_id and not query:
        query, corpus_type = _corpus_face(scryfall_id, face)
        card_type = type or corpus_type
    language = mpc_language(lang)
    filters = ArtFilters(
        languages=(language,) if language else (),
        fuzzy=fuzzy,
        include_nsfw=nsfw,
        min_dpi=min_dpi,
    )
    source = _source()
    try:
        if not query:
            if card_type != "CARDBACK":
                raise HTTPException(status_code=400, detail="Type a card name to search for.")
            ids = source.cardbacks(filters)
        else:
            art_query = ArtQuery(query, card_type)
            ids = source.search([art_query], filters)[art_query]
    except ArtSourceError as exc:
        raise _art_error(exc) from exc
    return MpcSearchOut(
        query=query, card_type=card_type, ids=ids, cards=_hydrate(ids[:FIRST_PAGE])
    )


@router.post("/cards", response_model=MpcCardsOut)
def cards(body: MpcCardsIn) -> MpcCardsOut:
    """Details for more of a search's ids, in request order."""
    ids = [i for i in body.ids if isinstance(i, str)]
    return MpcCardsOut(cards=_hydrate(ids))


@router.post("/best", response_model=MpcBestOut)
def best(body: MpcBestIn) -> MpcBestOut:
    """MPC Fill's top match for every face of each card, for importing a
    whole decklist with MPC Fill art: every face searched in the local
    catalog, then the winners hydrated. A face with no match comes back null and keeps its Scryfall
    art; a card the corpus doesn't hold is left out.

    `lang` is the import's language: only art in it counts. Unlike
    /search, a language MPC Fill doesn't have at all (Korean, Phyrexian
    ...) matches nothing rather than everything, so those cards keep their
    printing instead of getting English art."""
    conn = carddb.open_if_ready(get_card_db_path())
    if conn is None:
        raise HTTPException(status_code=404, detail=_NO_CORPUS)
    plans: list[tuple[str, list[ArtQuery]]] = []
    try:
        for scryfall_id in dict.fromkeys(body.scryfall_ids):
            card = carddb.get_card_by_id(conn, scryfall_id)
            if card is None:
                continue
            names, card_type = card_faces(card)
            plans.append((scryfall_id, [ArtQuery(name, card_type) for name in names]))
    finally:
        conn.close()
    language = mpc_language(body.lang)
    filters = ArtFilters(
        languages=(language,) if language else (),
        include_nsfw=body.nsfw,
        min_dpi=body.min_dpi,
    )
    unsupported = bool(body.lang) and language is None
    queries = (
        []
        if unsupported
        else list(dict.fromkeys(q for _sid, qs in plans for q in qs if q.query))
    )
    try:
        found = _source().search(queries, filters) if queries else {}
    except ArtSourceError as exc:
        raise _art_error(exc) from exc
    top_ids = list(dict.fromkeys(ids[0] for ids in found.values() if ids))
    hydrated = {c.drive_id: c for c in _hydrate(top_ids)}

    def winner(q: ArtQuery) -> MpcCardOut | None:
        ids = found.get(q) or []
        return hydrated.get(ids[0]) if ids else None

    return MpcBestOut(
        results=[
            MpcBestCardOut(
                scryfall_id=sid, total_faces=len(qs), faces=[winner(q) for q in qs]
            )
            for sid, qs in plans
        ]
    )


@router.get("/thumb/{drive_id}")
def thumb(drive_id: str, size: str = Query("small", pattern="^(small|large)$")) -> Response:
    """A JPEG preview (400 or 800 px tall), from Google via this server's
    disk cache. 502 when Google won't serve it; the grid shows its
    placeholder tile, which can still be picked."""
    try:
        data = drive_images.thumbnail(drive_id, size, cache_dir=Path(DEFAULT_CACHE_DIR))
    except ValueError as exc:
        raise HTTPException(status_code=400, detail=str(exc)) from exc
    except drive_images.DriveImageError as exc:
        raise HTTPException(status_code=502, detail=str(exc)) from exc
    return Response(content=data, media_type="image/jpeg", headers={"Cache-Control": _THUMB_CACHE})


@router.post("/import")
def import_art(body: MpcImportIn) -> Response:
    """Download one piece of art from Google Drive, store it as a Custom
    Image (with `bleed_mm`) or a Back Image on this server, and return the
    original file so the client can add the same bytes to its library.
    The client's next sync then finds the hash already here and uploads
    nothing. 502 when Google won't serve it; 422 when it isn't an image
    this server accepts. A sync def: it blocks on the download."""
    try:
        data, fmt = drive_images.download_original(body.drive_id)
    except ValueError as exc:
        raise HTTPException(status_code=400, detail=str(exc)) from exc
    except drive_images.DriveImageError as exc:
        raise HTTPException(status_code=502, detail=str(exc)) from exc
    content_hash = hashlib.sha256(data).hexdigest()
    try:
        with Image.open(io.BytesIO(data)) as img:
            width, height = img.size
    except (OSError, ValueError):
        width = height = 0  # the store says why below
    # Every MPC Fill file is declared with the requested bleed, whatever
    # its shape: MakePlayingCards stretches any upload onto its bled
    # template and trims 1/8" off each side, and uploaders design for that
    # (a file shaped like a bare card often draws its border extra wide so
    # the trim leaves a normal one). pdf_layout.fit_bled_image crops each
    # axis by its own scale, so the result matches an MPC print.
    declared = body.bleed_mm
    try:
        if body.kind == "custom":
            store_custom_bytes(content_hash, data, declared)
        else:
            backs.store_original(data, expected_hash=content_hash)
    except (customs.CustomImageError, backs.BackImageError) as exc:
        raise HTTPException(status_code=422, detail=str(exc)) from exc
    # The client's Rust side reads these instead of decoding the file (it
    # has no image decoder): its library row needs the dimensions, and the
    # bleed it was stored under so the two agree.
    return Response(
        content=data,
        media_type=f"image/{fmt}",
        headers={
            "Cache-Control": "no-store",
            "X-Image-Width": str(width),
            "X-Image-Height": str(height),
            "X-Declared-Bleed-Mm": f"{declared:g}",
        },
    )
