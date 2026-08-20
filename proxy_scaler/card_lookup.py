"""Local-first card resolution: answer from the imported corpus, fall back
to the live Scryfall API only for what the corpus can't answer.

CardResolver is a drop-in for ScryfallClient at the two places the server
resolves deck entries (routers/resolve.py, services/generation.py) — same
resolve()/resolve_many() shapes, same warning strings, same per-entry
error semantics. scryfall.py itself stays a pure HTTP client; the "check
the corpus first" policy lives here, and a machine with no imported corpus
behaves exactly as before this module existed (everything falls through
live).

Per-entry local lookup order: pinned scryfall_id → set+collector in the
entry's preferred language (English fallback — matching what the live
GET /cards/{set}/{number} would have answered) → exact name. Fuzzy name
matching deliberately stays a live-API concern; a bare name that misses
exactly here (typo, partial name) goes to /cards/named?fuzzy= as always.
"""

from __future__ import annotations

from pathlib import Path
from typing import Any, Callable

from . import carddb
from .decklist import DeckEntry
from .scryfall import (
    ScryfallClient,
    ScryfallError,
    _exact_printing_warning,
    _name_only_note,
)


class CardResolver:
    def __init__(
        self,
        card_db_path: Path | str | None = None,
        client_factory: Callable[[], ScryfallClient] = ScryfallClient,
    ) -> None:
        self._card_db_path = card_db_path
        self._client_factory = client_factory
        # Constructed lazily on the first live fallback, so a fully-local
        # resolve never even builds an HTTP session.
        self._client: ScryfallClient | None = None

    def _live(self) -> ScryfallClient:
        if self._client is None:
            self._client = self._client_factory()
        return self._client

    def resolve(
        self, entry: DeckEntry, *, strict_lang: bool = False
    ) -> tuple[dict[str, Any], list[str]]:
        result = self.resolve_many([entry], strict_lang=strict_lang)[0]
        if isinstance(result, ScryfallError):
            raise result
        return result

    def resolve_many(
        self, entries: list[DeckEntry], *, strict_lang: bool = False
    ) -> list[tuple[dict[str, Any], list[str]] | ScryfallError]:
        """Positionally aligned with `entries`, exactly like
        ScryfallClient.resolve_many: each slot is (card_json, warnings) or
        that entry's own ScryfallError.

        strict_lang: the resolve-gated import's "strictly literal" language
        mode — each entry's `lang` is a demand, not a preference. Local
        lookups restrict to exactly that language, the live exact-printing
        path uses /cards/{set}/{num}/{lang} with no bare fallback, and a
        fuzzy name match in any other language is rejected as that entry's
        error. Entries with no lang (the "All Languages" checkbox) behave
        exactly like the relaxed mode."""
        results: list[tuple[dict[str, Any], list[str]] | ScryfallError | None] = [
            None
        ] * len(entries)

        misses = list(range(len(entries)))
        conn = carddb.open_if_ready(self._card_db_path)
        if conn is not None:
            try:
                misses = []
                for i, entry in enumerate(entries):
                    card = _lookup_local(conn, entry, strict_lang=strict_lang)
                    if card is None:
                        misses.append(i)
                    else:
                        results[i] = (card, _local_warnings(entry, card))
            finally:
                conn.close()

        if misses:
            self._resolve_live(entries, misses, results, strict_lang=strict_lang)

        return results  # type: ignore[return-value]

    def _resolve_live(
        self,
        entries: list[DeckEntry],
        misses: list[int],
        results: list,
        *,
        strict_lang: bool = False,
    ) -> None:
        client = self._live()
        # /cards/collection can't express a language, so an exact-printing
        # entry that wants a non-English language gets its own
        # /cards/{set}/{number}/{lang} request (English printing as the
        # fallback when that language doesn't exist — unless strict).
        # Everything else keeps the batched path.
        individual = [
            i
            for i in misses
            if entries[i].has_exact_printing
            and entries[i].lang
            and entries[i].lang != "en"
        ]
        batched = [i for i in misses if i not in individual]

        if batched:
            sub_results = client.resolve_many([entries[i] for i in batched])
            for i, result in zip(batched, sub_results):
                entry = entries[i]
                if not isinstance(result, ScryfallError):
                    card, warnings = result
                    if (
                        strict_lang
                        and entry.lang
                        and card.get("lang") != entry.lang
                    ):
                        # Strictly literal: a match in any other language
                        # is this entry's failure, not a substitute.
                        results[i] = ScryfallError(
                            f"No {entry.lang} match for {entry.name!r} — "
                            f"closest was {card.get('name')} "
                            f"({card.get('set')}/{card.get('collector_number')}, "
                            f"{card.get('lang')})"
                        )
                        continue
                    if entry.collector_number and not entry.has_exact_printing:
                        # A set-less collector hint can't be expressed to
                        # the live API (fuzzy name only), so say what
                        # happened to it rather than letting it silently
                        # vanish.
                        if str(card.get("collector_number")) != str(entry.collector_number):
                            result = (
                                card,
                                warnings
                                + [
                                    f"Collector number {entry.collector_number} "
                                    "can't be matched without a local card "
                                    f"database — used {card.get('name')} "
                                    f"({card.get('set')}/{card.get('collector_number')})"
                                ],
                            )
                results[i] = result

        for i in individual:
            entry = entries[i]
            try:
                card = client.fetch_by_set_collector(
                    entry.set_code, entry.collector_number, lang=entry.lang
                )
            except ScryfallError as exc:
                if strict_lang:
                    # No bare retry: the chosen language is the version
                    # being imported, and it doesn't exist.
                    results[i] = ScryfallError(
                        f"No {entry.lang} version of {entry.name!r} "
                        f"({entry.set_code}/{entry.collector_number})"
                    )
                    continue
                try:
                    card = client.fetch_by_set_collector(
                        entry.set_code, entry.collector_number
                    )
                except ScryfallError:
                    results[i] = exc
                    continue
            warning = _exact_printing_warning(entry, card)
            results[i] = (card, [warning] if warning else [])


def _lookup_local(
    conn, entry: DeckEntry, *, strict_lang: bool = False
) -> dict[str, Any] | None:
    prefer_lang = entry.lang or "en"
    # In strict mode the entry's language is a demand: every lookup below
    # is restricted to exactly it (only_lang), never falling back — a
    # local miss then gets its verdict from the (equally strict) live path.
    only_lang = entry.lang if strict_lang and entry.lang else None
    if entry.scryfall_id:
        # The pin is authoritative: when the id isn't in the corpus (a
        # printing newer than the last import, say), report a miss and let
        # the live path find that exact printing — falling through to
        # set/collector here could silently answer with a *different*
        # printing (e.g. the English row of a pinned Japanese card).
        return carddb.get_card_by_id(conn, entry.scryfall_id)
    if entry.has_exact_printing:
        return carddb.get_card_by_set_collector(
            conn, entry.set_code, entry.collector_number, prefer_lang,
            only_lang=only_lang,
        )
    if entry.collector_number:
        # Collector-number hint with no set code ("Sol Ring 263" — see
        # decklist._NAME_COLLECTOR_RE). Try, in order:
        #   1. a printing of this name with that collector number (any
        #      set), language-preferred like every other lookup;
        #   2. the whole line as an exact card name, in case the trailing
        #      number was really part of the name and the parse split it;
        #   3. the plain name-only path — "latest printing of the name" —
        #      so a bogus/stale hint degrades to today's name behavior
        #      instead of failing (the warning in _local_warnings says so).
        row = carddb.find_row_by_name_and_collector(
            conn, entry.name, entry.collector_number, prefer_lang,
            only_lang=only_lang,
        )
        if row is not None:
            return carddb._row_json(row)
        full_name = f"{entry.name} {entry.collector_number}"
        card = carddb.get_card_by_name(conn, full_name, prefer_lang, only_lang=only_lang)
        if card is not None:
            return card
    return carddb.get_card_by_name(conn, entry.name, prefer_lang, only_lang=only_lang)


def _local_warnings(entry: DeckEntry, card: dict[str, Any]) -> list[str]:
    """The same notes the live path produces, so the client renders local
    and live resolutions identically."""
    if entry.has_exact_printing:
        warning = _exact_printing_warning(entry, card)
        return [warning] if warning else []
    if entry.scryfall_id:
        # Pinned by id with no set/collector on the entry — the id is the
        # authority, nothing to cross-check.
        return []
    if entry.collector_number:
        return [_collector_hint_note(entry, card)]
    return [_name_only_note(card)]


def _collector_hint_note(entry: DeckEntry, card: dict[str, Any]) -> str:
    """What a set-less collector-number hint actually resolved to — a
    matter-of-fact note when the hint matched a printing, an explicit
    warning when it matched nothing and the latest-by-name fallback was
    used instead."""
    resolved_to = (
        f"{card.get('name')} ({card.get('set')}/{card.get('collector_number')})"
    )
    if str(card.get("collector_number")) == str(entry.collector_number):
        return f"Collector-number lookup resolved to {resolved_to}"
    return (
        f"Collector number {entry.collector_number} not found for "
        f"{entry.name!r} — using {resolved_to}"
    )
