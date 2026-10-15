// Per-card backs: the pieces the Decklist, PDF and ZIP tabs share for
// deciding which Back Library images a print run draws on, syncing them to
// the connected server, and describing the result.
//
// A project has one universal back (settings.back_image_id, the Backs tab)
// and each card may override it with its own mode (CardRow.back_mode):
// "inherit", "blank" or "image". Resolution on the server is: transform
// face (paired DFC) > the card's own choice > the project's reverse_fill
// + universal back. The client's job here is only to make sure every back
// that choice can land on is present on the server, and to tell the server
// which ones exist and how much bleed each declares.
import { useQueries, useQuery } from "@tanstack/react-query";
import type { CardRow, ProjectSettings } from "./api/project";
import { projectApi } from "./api/project";
import type { BackImage, BackImageRef } from "./api/types";
import { getApiBaseUrl } from "./config";

/** The app-global Back Library. Shared query key with the Backs tab, so
 *  React Query dedupes every page's copy. */
export function useBackLibrary() {
  return useQuery({
    queryKey: ["back-images"],
    queryFn: () => projectApi.listBackImages(),
  });
}

/** Every library back a print of these cards may use: the universal back
 *  first, then each distinct per-card back in deck order. Ids only — the
 *  caller resolves them against the library it already holds. */
export function referencedBackIds(cards: CardRow[], settings: ProjectSettings): number[] {
  const ids: number[] = [];
  if (settings.back_image_id != null) ids.push(settings.back_image_id);
  for (const card of cards) {
    if (card.back_mode === "image" && card.back_image_id != null && !ids.includes(card.back_image_id)) {
      ids.push(card.back_image_id);
    }
  }
  return ids;
}

/** The request manifest entry for one back: its hash plus the library's
 *  own declaration about the file's bleed. */
export function backImageRef(back: BackImage): BackImageRef {
  return {
    hash: back.content_hash,
    includes_bleed: back.includes_bleed,
    bleed_mm: back.includes_bleed ? back.bleed_mm : null,
  };
}

/** Make sure the CONNECTED server holds every one of these backs' bytes
 *  before anything asks it to render with them. One query per back, keyed
 *  on the base URL too so switching servers re-syncs. Rust GETs first and
 *  only uploads on a miss, so the steady-state cost is one small request
 *  each. `ready` is true once every requested back has synced (trivially,
 *  when none is requested or syncing is disabled). */
export function useBackImagesSync(backIds: number[], enabled: boolean): { ready: boolean } {
  const baseUrl = getApiBaseUrl();
  const results = useQueries({
    queries: backIds.map((id) => ({
      queryKey: ["back-image-sync", id, baseUrl],
      queryFn: () => projectApi.syncBackImage(id, baseUrl),
      enabled,
    })),
  });
  const ready = !enabled || results.every((r) => r.isSuccess);
  return { ready };
}

/** How the deck's Reverses split, for the PDF/ZIP tabs' summary line.
 *  Counts cards (rows), not copies — it answers "what did I set", and a
 *  row is the unit the user set it on. Paired double-faced cards aren't
 *  distinguished here: whether a card's transform side takes its Reverse
 *  is only known once its faces are generated, and the summary must not
 *  change under the user as images arrive. */
export interface CardBackSummary {
  inherit: number;
  image: number;
  blank: number;
  /** The distinct per-card backs in use, deck order, resolved against the
   *  library (missing rows dropped). */
  ownBacks: BackImage[];
}

export function summarizeCardBacks(cards: CardRow[], library: BackImage[] | undefined): CardBackSummary {
  const summary: CardBackSummary = { inherit: 0, image: 0, blank: 0, ownBacks: [] };
  const seen = new Set<number>();
  for (const card of cards) {
    if (card.back_mode === "blank") {
      summary.blank += 1;
    } else if (card.back_mode === "image" && card.back_image_id != null) {
      summary.image += 1;
      if (!seen.has(card.back_image_id)) {
        seen.add(card.back_image_id);
        const back = library?.find((b) => b.id === card.back_image_id);
        if (back) summary.ownBacks.push(back);
      }
    } else {
      summary.inherit += 1;
    }
  }
  return summary;
}
