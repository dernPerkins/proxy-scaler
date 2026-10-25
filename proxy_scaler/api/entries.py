"""DeckEntryIn (the wire shape) -> DeckEntry (what the services take),
shared by every route that generates or prints from a decklist."""

from __future__ import annotations

import uuid

from fastapi import HTTPException

from proxy_scaler import customs
from proxy_scaler.api.schemas import DeckEntryIn
from proxy_scaler.decklist import DeckEntry, FaceOverride


def _printing_id(value: str) -> str:
    """A face override's scryfall_id, canonical (lowercase, hyphenated) so
    it compares equal to registry rows. It reaches cache filenames, so
    anything that isn't a UUID is refused."""
    try:
        return str(uuid.UUID(value))
    except ValueError as exc:
        raise HTTPException(
            status_code=400, detail=f"Not a Scryfall id: {value!r}."
        ) from exc


def to_deck_entry(e: DeckEntryIn) -> DeckEntry:
    """Raises a 400 for a face override naming a malformed hash or
    printing id, or both at once: either ends up in filesystem paths, so
    it is never trusted unchecked."""
    overrides: list[FaceOverride] = []
    for o in e.face_overrides:
        if o.custom_hash and o.scryfall_id:
            raise HTTPException(
                status_code=400,
                detail="A face override names a custom image or a printing, not both.",
            )
        try:
            custom_hash = customs.validate_hash(o.custom_hash) if o.custom_hash else None
        except customs.CustomImageError as exc:
            raise HTTPException(status_code=400, detail=str(exc)) from exc
        scryfall_id = _printing_id(o.scryfall_id) if o.scryfall_id else None
        if o.face >= o.total_faces:
            raise HTTPException(
                status_code=400, detail=f"Face {o.face} of a {o.total_faces}-faced card."
            )
        overrides.append(
            FaceOverride(
                o.face,
                o.total_faces,
                custom_hash,
                scryfall_id=scryfall_id,
                source_face=(
                    (o.face if o.source_face is None else o.source_face)
                    if scryfall_id
                    else None
                ),
            )
        )
    return DeckEntry(
        quantity=e.quantity,
        name=e.name,
        set_code=e.set_code,
        collector_number=e.collector_number,
        raw_line=e.raw_line or e.name,
        scryfall_id=e.scryfall_id,
        lang=e.lang,
        custom_hash=e.custom_hash,
        back_mode=e.back_mode,
        back_hash=e.back_hash,
        # Overrides only mean anything on a Scryfall card.
        face_overrides=() if e.custom_hash else tuple(overrides),
    )
