// Picking MPC Fill art into the Custom Image or Back Image library.
//
// The generation server does all the talking to MPC Fill and Google Drive
// (api/routers/mpcfill.py); the client never does. A pick asks Rust to
// import the art (custom_images.rs / back_images.rs import_mpc_*), which
// has the server download it — keeping a copy, so the next sync uploads
// nothing — and takes the bytes straight from the server. Nothing large
// crosses into the webview.
import { generationApi } from "./api/generation";
import { projectApi } from "./api/project";
import type { CustomCardItem } from "./api/project";
import type { BackImage, CustomImage, MpcCard } from "./api/types";
import { getApiBaseUrl } from "./config";
import { orderArtName, type OrderArt, type ParsedOrder } from "./mpcOrder";

/** The bleed MPC Fill art is declared with: MakePlayingCards' 1/8 in per
 *  side, the same default every library image starts from. The server
 *  declares a file whose aspect says it has none at 0 instead. */
export const MPC_IMPORT_BLEED_MM = 3.175;

export type MpcLibraryKind = "custom" | "back";

/**
 * Put one piece of MPC Fill art in the library and return its entry. Art
 * already picked before comes straight back from the library, without
 * another download. Throws with a user-facing message.
 */
export async function importMpcArt(kind: "custom", card: MpcCard): Promise<CustomImage>;
export async function importMpcArt(kind: "back", card: MpcCard): Promise<BackImage>;
export async function importMpcArt(
  kind: MpcLibraryKind,
  card: MpcCard,
): Promise<CustomImage | BackImage> {
  const args = {
    serverBaseUrl: getApiBaseUrl(),
    driveId: card.drive_id,
    mpcSource: card.source_name,
    mpcName: card.name,
    bleedMm: MPC_IMPORT_BLEED_MM,
  };
  return kind === "custom"
    ? projectApi.importMpcCustomImage(args)
    : projectApi.importMpcBackImage(args);
}

export interface OrderImportResult {
  items: CustomCardItem[];
  /** The order's default back, in the library, when it named one. */
  cardback: BackImage | null;
  errors: string[];
  canceled: boolean;
}

/**
 * Fetch every front and back an MPC order uses into the library, one at a
 * time (each is a download and a decode; doing them together is how the
 * webview runs out of memory), and work out the project rows. Art already
 * in the library costs nothing. A failed piece of art is reported and its
 * cards left out; the rest still import.
 */
export async function importMpcOrder(
  order: ParsedOrder,
  onProgress: (done: number, total: number, name: string) => void,
  isCanceled: () => boolean,
): Promise<OrderImportResult> {
  // One request for names, uploaders and formats, answered by the
  // downloaded catalog. Art it doesn't hold, or no catalog at all (a 409),
  // still imports under the order's own file name.
  const allIds = [...order.fronts, ...order.backs].map((a) => a.driveId);
  const known = new Map<string, MpcCard>();
  try {
    for (const card of (await generationApi.mpcCards(allIds)).cards) known.set(card.drive_id, card);
  } catch {
    // Names fall back to the order's.
  }
  const asCard = (art: OrderArt): MpcCard =>
    known.get(art.driveId) ?? {
      drive_id: art.driveId,
      name: orderArtName(art) || "MPC art",
      card_type: "CARD",
      extension: "",
      dpi: 0,
      size_bytes: 0,
      language: "",
      source_name: "",
      date_created: "",
      tags: [],
    };

  const errors: string[] = [];
  const fronts = new Map<string, CustomImage>();
  const backs = new Map<string, BackImage>();
  const total = order.fronts.length + order.backs.length;
  let done = 0;
  for (const [kind, arts] of [
    ["custom", order.fronts],
    ["back", order.backs],
  ] as const) {
    for (const art of arts) {
      if (isCanceled()) return { items: [], cardback: null, errors, canceled: true };
      const card = asCard(art);
      onProgress(done, total, card.name);
      try {
        if (kind === "custom") fronts.set(art.driveId, await importMpcArt("custom", card));
        else backs.set(art.driveId, await importMpcArt("back", card));
      } catch (err) {
        errors.push(`${card.name}: ${err instanceof Error ? err.message : String(err)}`);
      }
      done += 1;
    }
  }
  onProgress(total, total, "");

  const items: CustomCardItem[] = [];
  for (const line of order.lines) {
    const front = fronts.get(line.front.driveId);
    if (!front) continue;
    const back = line.back ? backs.get(line.back.driveId) : undefined;
    items.push({
      custom_image_id: front.id,
      quantity: line.quantity,
      // A slot whose own back failed to import prints the project's back
      // rather than not at all.
      back_mode: back ? "image" : "inherit",
      back_image_id: back ? back.id : null,
    });
  }
  return {
    items,
    cardback: order.cardback ? (backs.get(order.cardback.driveId) ?? null) : null,
    errors,
    canceled: false,
  };
}
