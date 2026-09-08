import type { CardRow } from "./api/project";
import type { DeckEntryIn, SortPrimary } from "./api/types";

/** The one sort applied everywhere card order is visible: the Decklist
 *  display AND the entries sent for PDF/ZIP export (the server prints in
 *  the order the entries array arrives). "(none)" keeps DB sort_order,
 *  i.e. decklist insert order. */
export function sortCards(cards: CardRow[], primary: SortPrimary): CardRow[] {
  if (primary === "(none)") return cards;
  const key = (c: CardRow) =>
    (primary === "Name" ? c.name : (c.set_code ?? "")).toLowerCase();
  return [...cards].sort((a, b) => key(a).localeCompare(key(b)));
}

/** A project card as the DeckEntryIn the generation server expects.
 *
 *  One shared copy (Decklist, PDF, and Export all send entries) because
 *  the field set is load-bearing and drifted once already: language is
 *  part of a printing's identity in the gallery match
 *  (pdf_layout.match_quantities compares entry.lang against the
 *  image's), so a copy that omitted `lang` made generated non-English
 *  cards report as "no generated image yet". The pinned scryfall_id +
 *  lang also make server-side resolution exact — a non-English printing
 *  is unreachable via set/collector alone, since every language shares
 *  them. */
export function cardToEntry(card: CardRow): DeckEntryIn {
  return {
    quantity: card.quantity ?? 1,
    name: card.name,
    set_code: card.set_code,
    collector_number: card.collector_number,
    raw_line: card.original_import_line,
    scryfall_id: card.scryfall_id,
    lang: card.lang,
    // A Custom Image entry carries only its hash and a display name: the
    // server matches it on the hash alone, never by name (a custom front
    // called "Sol Ring" must not soak up a real Sol Ring line's quantity).
    custom_hash: card.custom_hash,
  };
}
