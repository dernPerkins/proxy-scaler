"""Parse MTG decklist lines in quantity+name or quantity+name+set+collector form."""

from __future__ import annotations

import re
from dataclasses import dataclass
from pathlib import Path

# Trailing "(set) collector" — set is alphanumeric; collector is opaque (21p, DDG-14, etc.)
_SET_COLLECTOR_RE = re.compile(
    r"^(?P<name>.+?)\s+\((?P<set>[A-Za-z0-9]+)\)\s+(?P<collector>\S+)\s*$"
)
# Trailing bare collector number with NO set code — "Sol Ring 263" — the
# best some deck managers can export (notably for non-English cards, whose
# printings they often don't model at all). Deliberately strict about what
# counts as a collector number (digits + one optional letter suffix, like
# 263 or 123a): a looser token would start eating the ends of card names.
# The number is a *hint*, not an exact printing — set_code stays None and
# resolution matches it against the name's printings, falling back to the
# plain name-only path when it matches nothing (see
# card_lookup.CardResolver). Card names ending in a bare number would be
# misparsed here, but no real Magic card name does ("Borrowing 100,000
# Arrows" has commas, so its last token doesn't match) — and resolution
# retries with the number folded back into the name anyway.
_NAME_COLLECTOR_RE = re.compile(r"^(?P<name>.+?)\s+(?P<collector>\d{1,4}[A-Za-z]?)\s*$")
# Optional x after the count covers the "4x Lightning Bolt" style many
# deck-site exports use.
_QTY_RE = re.compile(r"^(?P<qty>\d+)[xX]?\s+(?P<rest>.+)$")

# Common section headers / noise lines to skip
_SKIP_LINES = {
    "deck",
    "sideboard",
    "maybeboard",
    "commander",
    "companion",
    "mainboard",
    "main",
}


@dataclass(frozen=True)
class DeckEntry:
    quantity: int
    name: str
    set_code: str | None = None
    collector_number: str | None = None
    raw_line: str = ""
    # Never parsed from decklist text — carried by entries the client sends
    # for cards it has already pinned to an exact printing (scryfall_id) or
    # stamped with the project's language preference (lang). See
    # card_lookup.CardResolver for how they steer resolution.
    scryfall_id: str | None = None
    lang: str | None = None

    @property
    def has_exact_printing(self) -> bool:
        return self.set_code is not None and self.collector_number is not None


def parse_line(line: str) -> DeckEntry | None:
    """Parse a single decklist line. Returns None for blank/header lines."""
    stripped = line.strip()
    if not stripped or stripped.startswith("#") or stripped.startswith("//"):
        return None

    lower = stripped.lower()
    if lower in _SKIP_LINES:
        return None

    qty_match = _QTY_RE.match(stripped)
    if qty_match:
        quantity = int(qty_match.group("qty"))
        rest = qty_match.group("rest").strip()
    else:
        # Allow bare name / name+set lines without quantity (treat as 1)
        quantity = 1
        rest = stripped

    set_match = _SET_COLLECTOR_RE.match(rest)
    if set_match:
        return DeckEntry(
            quantity=quantity,
            name=set_match.group("name").strip(),
            set_code=set_match.group("set").lower(),
            collector_number=set_match.group("collector"),
            raw_line=stripped,
        )

    hint_match = _NAME_COLLECTOR_RE.match(rest)
    if hint_match:
        # Collector-number hint, no set (has_exact_printing stays False —
        # set_code is None). See _NAME_COLLECTOR_RE above.
        return DeckEntry(
            quantity=quantity,
            name=hint_match.group("name").strip(),
            collector_number=hint_match.group("collector"),
            raw_line=stripped,
        )

    return DeckEntry(
        quantity=quantity,
        name=rest,
        raw_line=stripped,
    )


def parse_decklist_text(text: str) -> list[DeckEntry]:
    """Parse decklist text (newline-separated) into DeckEntry records."""
    entries: list[DeckEntry] = []
    for line in text.splitlines():
        entry = parse_line(line)
        if entry is not None:
            entries.append(entry)
    return entries


def parse_decklist(path: Path | str) -> list[DeckEntry]:
    """Parse a decklist file into DeckEntry records."""
    path = Path(path)
    return parse_decklist_text(path.read_text(encoding="utf-8"))
