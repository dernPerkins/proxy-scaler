import type { CardPlacement, CardRow } from "./api/project";
import type { DeckEntryIn, SortPrimary } from "./api/types";

/** The one sort applied everywhere card order is visible: the Decklist
 *  display AND the entries sent for PDF/ZIP export (the server prints in
 *  the order the entries array arrives). "(none)" keeps DB sort_order,
 *  i.e. decklist insert order. "Custom" orders cards by where their first
 *  copy sits in the placements (docs/adr/0009) — the Row view's order;
 *  the copies themselves print where deckCopies/printOrder put them. */
export function sortCards(
  cards: CardRow[],
  primary: SortPrimary,
  placements: readonly CardPlacement[] = [],
): CardRow[] {
  if (primary === "(none)") return cards;
  if (primary === "Custom") {
    const first = new Map<number, number>();
    for (const p of placements) if ((p.face ?? 0) === 0 && !first.has(p.card_id)) first.set(p.card_id, p.position);
    const at = (c: CardRow) => first.get(c.id) ?? Number.MAX_SAFE_INTEGER;
    return [...cards].sort((a, b) => at(a) - at(b));
  }
  const key = (c: CardRow) =>
    (primary === "Name" ? c.name : (c.set_code ?? "")).toLowerCase();
  return [...cards].sort((a, b) => key(a).localeCompare(key(b)));
}

/** One printed copy of a card: what the Grid draws as a tile. */
export interface DeckCopy {
  /** Stable React/drag key: the placement id, or a stand-in for a copy
   *  whose placement hasn't been read back yet (a card added a moment ago). */
  key: string;
  placementId: number | null;
  card: CardRow;
  /** 0 for the card itself; 1 for a double-faced card's transform side,
   *  printed as a card of its own (see deckCopies' `transformCards`). */
  face: 0 | 1;
}

const copiesWanted = (card: CardRow) => Math.max(1, Math.round(card.quantity ?? 1));

/** The deck as printed copies, in the order they print.
 *
 *  Custom walks the placements, so copies of one card can sit anywhere.
 *  Every other sort is sortCards with each card's copies side by side —
 *  exactly how the server groups them — but still carries each copy's
 *  placement id, so a drag on that order can become the Custom order.
 *
 *  `transformCards` are the double-faced cards whose transform side prints
 *  as a card of its own (pairing off on the PDF tab): each copy of one also
 *  gets a face-1 copy, where its transform placement puts it under Custom,
 *  and after the card's fronts under every other sort — the server's
 *  grouped order, a printing's face 0 before its face 1. */
export function deckCopies(
  cards: CardRow[],
  primary: SortPrimary,
  placements: readonly CardPlacement[],
  transformCards: ReadonlySet<number> = new Set(),
): DeckCopy[] {
  const byCard = new Map<string, CardPlacement[]>();
  for (const p of placements) {
    const k = `${p.card_id}:${p.face ?? 0}`;
    const list = byCard.get(k);
    if (list) list.push(p);
    else byCard.set(k, [p]);
  }
  const copiesOf = (card: CardRow, face: 0 | 1): DeckCopy[] => {
    const have = byCard.get(`${card.id}:${face}`) ?? [];
    const stand = face === 1 ? "t" : "c";
    return Array.from({ length: copiesWanted(card) }, (_, k) =>
      have[k]
        ? { key: `p${have[k].id}`, placementId: have[k].id, card, face }
        : { key: `${stand}${card.id}-${k}`, placementId: null, card, face },
    );
  };
  const faces = (card: CardRow): (0 | 1)[] => (transformCards.has(card.id) ? [0, 1] : [0]);
  if (primary !== "Custom") {
    return sortCards(cards, primary).flatMap((card) => faces(card).flatMap((f) => copiesOf(card, f)));
  }
  const cardById = new Map(cards.map((c) => [c.id, c]));
  const taken = new Map<string, number>();
  const out: DeckCopy[] = [];
  for (const p of placements) {
    const card = cardById.get(p.card_id);
    const face = p.face ?? 0;
    if (!card || (face === 1 && !transformCards.has(card.id))) continue;
    const key = `${card.id}:${face}`;
    const k = taken.get(key) ?? 0;
    if (k >= copiesWanted(card)) continue;
    taken.set(key, k + 1);
    out.push({ key: `p${p.id}`, placementId: p.id, card, face });
  }
  // Copies the placements don't cover yet (a refetch in flight) go last,
  // where the reconcile will put them.
  for (const card of cards) {
    for (const face of faces(card)) {
      const stand = face === 1 ? "t" : "c";
      for (let i = taken.get(`${card.id}:${face}`) ?? 0; i < copiesWanted(card); i++) {
        out.push({ key: `${stand}${card.id}-${i}`, placementId: null, card, face });
      }
    }
  }
  return out;
}

/** What the PDF and ZIP tabs send: the entries in the shared sort's order
 *  and, for Custom, `copy_order` — one entries index per printed copy, in
 *  print order. The server ignores a copy_order that doesn't name every
 *  entry exactly `quantity` times (pdf_layout.checked_copy_order) and
 *  prints grouped, so a list racing a quantity change can't drop cards.
 *
 *  Custom also sends `face_order` once any card has transform placements:
 *  the same order with every transform placement in it, whether or not that
 *  card is known here to be double-faced or its transform prints on its own
 *  — the server pairs, and ignores transform items for a paired or
 *  single-faced card. A card whose transform placements don't number its
 *  copies (a refetch in flight) leaves them out, and the server puts its
 *  transforms back beside its fronts. */
export function printOrder(
  cards: CardRow[],
  primary: SortPrimary,
  placements: readonly CardPlacement[],
): {
  entries: DeckEntryIn[];
  copyOrder: number[] | undefined;
  faceOrder: [number, 0 | 1][] | undefined;
} {
  const sorted = sortCards(cards, primary, placements);
  const entries = sorted.map(cardToEntry);
  if (primary !== "Custom") return { entries, copyOrder: undefined, faceOrder: undefined };
  const index = new Map(sorted.map((c, i) => [c.id, i]));
  const everyTransform = new Set(placements.filter((p) => p.face === 1).map((p) => p.card_id));
  const copies = deckCopies(cards, primary, placements, everyTransform);
  const copyOrder = copies.filter((c) => c.face === 0).map((c) => index.get(c.card.id)!);
  if (everyTransform.size === 0) return { entries, copyOrder, faceOrder: undefined };
  const placed = new Map<number, number>();
  for (const c of copies) {
    if (c.face === 1 && c.placementId != null) placed.set(c.card.id, (placed.get(c.card.id) ?? 0) + 1);
  }
  const faceOrder = copies.flatMap((c): [number, 0 | 1][] =>
    c.face === 1 && placed.get(c.card.id) !== copiesWanted(c.card) ? [] : [[index.get(c.card.id)!, c.face]],
  );
  return { entries, copyOrder, faceOrder };
}

/** A project card as the DeckEntryIn the generation server expects.
 *
 *  One shared copy (Decklist, PDF, and ZIP all send entries) because
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
    // The card's own back. An "image" whose library row is gone is sent as
    // "inherit": Rust already normalizes that, this just keeps the wire
    // honest if a stale CardRow is still in memory.
    back_mode: card.back_mode === "image" && card.back_hash == null ? "inherit" : card.back_mode,
    back_hash: card.back_mode === "image" ? card.back_hash : null,
    // Only sent when there are some, so an all-Scryfall deck's request is
    // exactly what an older server has always accepted.
    ...(card.face_art?.length
      ? {
          // A face from another printing names that printing's face;
          // anything else is its Custom Image (MPC Fill or uploaded).
          face_overrides: card.face_art.map((fa) =>
            fa.scryfall
              ? {
                  face: fa.face,
                  total_faces: fa.total_faces,
                  custom_hash: null,
                  scryfall_id: fa.scryfall.scryfall_id,
                  source_face: fa.scryfall.source_face,
                }
              : { face: fa.face, total_faces: fa.total_faces, custom_hash: fa.custom_hash },
          ),
        }
      : {}),
  };
}
