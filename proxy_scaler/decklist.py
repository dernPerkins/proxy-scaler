"""Parse MTG decklist lines in quantity+name or quantity+name+set+collector form."""

from __future__ import annotations

import re
from dataclasses import dataclass
from pathlib import Path

# Trailing "(set) collector" — set is alphanumeric; collector is opaque (21p, DDG-14, etc.)
_SET_COLLECTOR_RE = re.compile(
    r"^(?P<name>.+?)\s+\((?P<set>[A-Za-z0-9]+)\)\s+(?P<collector>\S+)\s*$"
)
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
