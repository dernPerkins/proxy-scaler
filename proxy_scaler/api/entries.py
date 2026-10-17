"""DeckEntryIn (the wire shape) -> DeckEntry (what the services take),
shared by every route that generates or prints from a decklist."""

from __future__ import annotations

from fastapi import HTTPException

from proxy_scaler import customs
from proxy_scaler.api.schemas import DeckEntryIn
from proxy_scaler.decklist import DeckEntry, FaceOverride


def to_deck_entry(e: DeckEntryIn) -> DeckEntry:
    """Raises a 400 for a face override naming a malformed hash: it ends up
    in filesystem paths, so it is never trusted unchecked."""
    overrides: list[FaceOverride] = []
    for o in e.face_overrides:
        try:
            custom_hash = customs.validate_hash(o.custom_hash)
        except customs.CustomImageError as exc:
            raise HTTPException(status_code=400, detail=str(exc)) from exc
        if o.face >= o.total_faces:
            raise HTTPException(
                status_code=400, detail=f"Face {o.face} of a {o.total_faces}-faced card."
            )
        overrides.append(FaceOverride(o.face, o.total_faces, custom_hash))
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
