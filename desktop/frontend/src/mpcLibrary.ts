// The MPC Fill tab's "Add to Project": MPC art enters a deck the way a
// change-printing pick would — as its Scryfall card with the art on one
// face — so it keeps the card's printing, other face and back, shows MPC
// badges, and can go back to Scryfall art in the picker. Each add is a new
// card, beside any of the same name already in the deck. Art whose name
// no card has (custom designs, typos) is added as a plain custom card.
import { useSyncExternalStore } from "react";
import type { QueryClient } from "@tanstack/react-query";
import { generationApi } from "./api/generation";
import type { CardRow, ResolvedImportCard } from "./api/project";
import type { CustomImage, MpcCard } from "./api/types";
import { importMpcArt } from "./mpcFill";
import { errorText, toast } from "./toasts";

type Added = { cards: CardRow[]; projectTag: string | null };

export async function addMpcArtToDeck(
  image: CustomImage,
  opts: {
    /** The project's import language, or null for any. */
    lang: string | null;
    addCardWithFaceArt: (
      card: ResolvedImportCard,
      face: number,
      totalFaces: number,
      customImageId: number,
    ) => Promise<Added>;
    addCustomCards: (ids: number[]) => Promise<Added>;
  },
): Promise<Added & { asCustom: boolean }> {
  const name = (image.mpc?.name || image.label).trim();
  let resolved = null;
  try {
    const result = await generationApi.resolve([
      {
        quantity: 1,
        name,
        set_code: null,
        collector_number: null,
        raw_line: `1 ${name}`,
        lang: opts.lang,
      },
    ]);
    resolved = result.resolved[0] ?? null;
  } catch {
    // No server, or nothing matched: a plain custom card still prints.
  }
  const faces = resolved?.faces ?? [];
  if (faces.length === 0) {
    return { ...(await opts.addCustomCards([image.id])), asCustom: true };
  }
  const front = faces[0];
  const totalFaces = faces.length > 1 ? 2 : 1;
  // Art named after a double-faced card's back face goes on that face.
  const lower = name.toLowerCase();
  const matched = faces.find((f) => f.face_name.toLowerCase() === lower);
  const face = matched && matched !== front && totalFaces === 2 ? 1 : 0;
  const card: ResolvedImportCard = {
    raw_line: `1 ${front.card_name}`,
    quantity: 1,
    name: front.card_name,
    printed_name: front.printed_name ?? null,
    flavor_name: front.flavor_name ?? null,
    set_code: front.set_code,
    collector_number: front.collector_number,
    scryfall_id: front.scryfall_id,
    lang: front.lang ?? "en",
    rarity: front.rarity ?? null,
  };
  return { ...(await opts.addCardWithFaceArt(card, face, totalFaces, image.id)), asCustom: false };
}

// --- Picks from the MPC Fill tab's search, downloading in the background ---
//
// A pick closes the search at once, like a pick in the Decklist's picker,
// and its art downloads behind a placeholder tile. Module-level so a tab
// switch mid-download keeps the placeholder (and the download carries on
// regardless: it is Rust's, not the page's).

export interface PendingMpcImport {
  driveId: string;
  name: string;
  source: string;
}

let pending: PendingMpcImport[] = [];
const listeners = new Set<() => void>();

function setPending(next: PendingMpcImport[]): void {
  pending = next;
  for (const listener of listeners) listener();
}

export function usePendingMpcImports(): PendingMpcImport[] {
  return useSyncExternalStore(
    (listener) => {
      listeners.add(listener);
      return () => listeners.delete(listener);
    },
    () => pending,
  );
}

/** Download one picked piece of art into the library in the background.
 *  `onDone` gets the library entry (to select it). Art already in the
 *  library comes back without another download. */
export function importMpcArtInBackground(
  card: MpcCard,
  queryClient: QueryClient,
  onDone: (image: CustomImage) => void,
): void {
  if (pending.some((p) => p.driveId === card.drive_id)) return;
  setPending([...pending, { driveId: card.drive_id, name: card.name, source: card.source_name }]);
  void importMpcArt("custom", card)
    .then(async (image) => {
      await queryClient.invalidateQueries({ queryKey: ["custom-images"] });
      onDone(image);
    })
    .catch((err) => toast(`Couldn't download ${card.name}: ${errorText(err)}`, "err"))
    .finally(() => setPending(pending.filter((p) => p.driveId !== card.drive_id)));
}
