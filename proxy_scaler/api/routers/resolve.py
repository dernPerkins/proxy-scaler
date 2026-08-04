from __future__ import annotations

from fastapi import APIRouter

from proxy_scaler.api.schemas import (
    DeckEntryIn,
    ResolvedCardOut,
    ResolvedFaceOut,
    ResolveFailureOut,
    ResolveIn,
    ResolveOut,
)
from proxy_scaler.decklist import DeckEntry
from proxy_scaler.scryfall import ScryfallClient, ScryfallError, expand_faces

router = APIRouter(prefix="/api", tags=["resolve"])


def _to_deck_entry(e: DeckEntryIn) -> DeckEntry:
    return DeckEntry(
        quantity=e.quantity,
        name=e.name,
        set_code=e.set_code,
        collector_number=e.collector_number,
        raw_line=e.raw_line or e.name,
    )


@router.post("/resolve", response_model=ResolveOut)
def resolve(body: ResolveIn) -> ResolveOut:
    """Resolve raw decklist entries against Scryfall for display purposes
    only — no DB writes, no enqueue. /generate resolves independently
    (see ARCHITECTURE.md); this endpoint exists purely so the client can
    show canonical names/identity before committing to generation."""
    if not body.entries:
        return ResolveOut(resolved=[], failed=[])

    entries = [_to_deck_entry(e) for e in body.entries]
    client = ScryfallClient()
    results = client.resolve_many(entries)

    resolved: list[ResolvedCardOut] = []
    failed: list[ResolveFailureOut] = []
    for entry, pre in zip(entries, results):
        if isinstance(pre, ScryfallError):
            failed.append(ResolveFailureOut(raw_line=entry.raw_line, error=str(pre)))
            continue
        card, warnings = pre
        try:
            faces = expand_faces(card)
        except ScryfallError as exc:
            failed.append(ResolveFailureOut(raw_line=entry.raw_line, error=str(exc)))
            continue
        resolved.append(
            ResolvedCardOut(
                raw_line=entry.raw_line,
                quantity=entry.quantity,
                faces=[
                    ResolvedFaceOut(
                        scryfall_id=f.scryfall_id,
                        face_index=f.face_index,
                        face_label=f.face_label,
                        face_name=f.face_name,
                        card_name=f.card_name,
                        set_code=f.set_code,
                        collector_number=f.collector_number,
                        png_url=f.png_url,
                        image_status=f.image_status,
                    )
                    for f in faces
                ],
                warnings=warnings,
            )
        )
    return ResolveOut(resolved=resolved, failed=failed)
