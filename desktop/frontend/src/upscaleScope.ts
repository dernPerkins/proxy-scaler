// What each of the Decklist's whole-deck upscale buttons covers.
//
// "Upscale Originals" upscales the Scryfall art; the caret menu adds Upscale
// Customs (the user's own uploads), Upscale MPC Fill (art downloaded from
// MPC Fill) and Upscale Everything. Custom and MPC art share one library
// (custom_images); MPC rows are the ones with MPC provenance. A card can
// mix kinds — Scryfall faces with MPC art on one — so each scope sends
// exactly the cards and face overrides it covers, and the server is told
// whether to upscale Scryfall faces at all (GenerateRequest.scryfall_upscale).
import { isImageFaceArt, type CardRow } from "./api/project";
import type { DeckEntryIn } from "./api/types";
import { cardToEntry } from "./deckEntries";

export type UpscaleScope = "originals" | "customs" | "mpc" | "all";

export const SCOPE_LABELS: Record<UpscaleScope, string> = {
  originals: "Upscale Originals",
  customs: "Upscale Customs",
  mpc: "Upscale MPC Fill",
  all: "Upscale Everything",
};

/** Said when a scope has nothing to upscale in this deck. */
export const SCOPE_EMPTY: Record<UpscaleScope, string> = {
  originals: "No Scryfall cards in this deck.",
  customs: "No custom images in this deck.",
  mpc: "No MPC Fill art in this deck.",
  all: "No cards in this deck.",
};

export interface ScopeRequest {
  entries: DeckEntryIn[];
  /** The cards those entries came from (for fetching pending art and
   *  syncing customs first). */
  cards: CardRow[];
  scryfallUpscale: boolean;
  /** Whether the request's custom faces are upscaled (else registered). */
  upscaleCustoms: boolean;
}

/** `mpcImageIds`: library images that came from MPC Fill. */
export function scopeRequest(
  cards: CardRow[],
  scope: UpscaleScope,
  mpcImageIds: ReadonlySet<number>,
): ScopeRequest {
  if (scope === "all") {
    return { entries: cards.map(cardToEntry), cards, scryfallUpscale: true, upscaleCustoms: true };
  }
  if (scope === "originals") {
    // Face art rides along as overrides so those faces are left out (and
    // their art registered), never upscaled from the Scryfall image.
    const picked = cards.filter((c) => c.custom_image_id == null);
    return {
      entries: picked.map(cardToEntry),
      cards: picked,
      scryfallUpscale: true,
      upscaleCustoms: false,
    };
  }
  const wantMpc = scope === "mpc";
  const entries: DeckEntryIn[] = [];
  const picked: CardRow[] = [];
  for (const card of cards) {
    if (card.custom_image_id != null) {
      if (mpcImageIds.has(card.custom_image_id) === wantMpc) {
        entries.push(cardToEntry(card));
        picked.push(card);
      }
      continue;
    }
    // Art still to be downloaded counts by its provenance too: an MPC
    // pick is MPC art. A face from another printing is Scryfall art,
    // which neither scope covers.
    const faces = (card.face_art ?? []).filter(
      (fa) => isImageFaceArt(fa) && (fa.mpc_drive_id != null) === wantMpc,
    );
    if (faces.length === 0) continue;
    const entry = cardToEntry({ ...card, face_art: faces });
    entries.push(entry);
    picked.push(card);
  }
  return { entries, cards: picked, scryfallUpscale: false, upscaleCustoms: true };
}
