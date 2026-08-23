import type { CardRow } from "./api/project";
import type { DeckEntryIn } from "./api/types";

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
  };
}
