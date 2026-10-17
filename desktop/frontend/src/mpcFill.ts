// Picking MPC Fill art into the Custom Image or Back Image library.
//
// The generation server does all the talking to MPC Fill and Google Drive
// (api/routers/mpcfill.py); the client never does. A pick asks the server
// to download the art (/api/mpcfill/import), which stores it on the server
// and hands the same bytes back, and those go through the exact path a
// dropped file does — readPickedImage for the dimensions and preview, then
// Rust for hashing and storage — plus where they came from. The next sync
// then finds the server already holding them and uploads nothing.
import { generationApi } from "./api/generation";
import { projectApi } from "./api/project";
import type { CardRow, CustomCardItem } from "./api/project";
import type { BackImage, CustomImage, MpcBestResult, MpcCard } from "./api/types";
import { readPickedImage } from "./imageUpload";
import { orderArtName, type OrderArt, type ParsedOrder } from "./mpcOrder";

/** The bleed MPC Fill art is declared with: MakePlayingCards' 1/8 in per
 *  side, the same default every library image starts from. */
export const MPC_IMPORT_BLEED_MM = 3.175;

// Mirrors proxy_scaler/api/routers/mpcfill.py::declared_bleed_mm: a file
// this close to the bare card's 63:88 carries no bleed. The server stores
// the import under the same rule, so the two agree without asking.
const BLEED_FREE_ASPECT = 63 / 88;
const BLEED_FREE_TOLERANCE = 0.006;

export function declaredBleedMm(width: number, height: number, bleedMm: number): number {
  if (width <= 0 || height <= 0) return bleedMm;
  const aspect = Math.min(width, height) / Math.max(width, height);
  return Math.abs(aspect - BLEED_FREE_ASPECT) <= BLEED_FREE_TOLERANCE ? 0 : bleedMm;
}

const EXTENSIONS: Record<string, string> = {
  "image/png": "png",
  "image/jpeg": "jpg",
  "image/webp": "webp",
};

/** A filename for the library: MPC Fill's name made safe for a path, with
 *  the extension the bytes actually are (Rust names the stored file, and
 *  the viewer picks its MIME type, by that extension). */
export function mpcFilename(card: MpcCard, mime: string): string {
  const stem = card.name.replace(/[\\/:*?"<>|]+/g, " ").replace(/\s+/g, " ").trim() || "MPC art";
  return `${stem}.${EXTENSIONS[mime] ?? card.extension ?? "png"}`;
}

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
  const existing =
    kind === "custom"
      ? await projectApi.findCustomImageByDriveId(card.drive_id)
      : await projectApi.findBackImageByDriveId(card.drive_id);
  if (existing) return existing;

  const blob = await generationApi.mpcImport({
    drive_id: card.drive_id,
    kind,
    bleed_mm: MPC_IMPORT_BLEED_MM,
  });
  const file = new File([blob], mpcFilename(card, blob.type), { type: blob.type });
  const picked = await readPickedImage(file);
  const bleed = declaredBleedMm(picked.width, picked.height, MPC_IMPORT_BLEED_MM);
  const args = {
    ...picked,
    originalFilename: file.name,
    label: card.name,
    includesBleed: bleed > 0,
    // Kept while unticked, so ticking the box later restores the amount.
    bleedMm: MPC_IMPORT_BLEED_MM,
    driveId: card.drive_id,
    mpcSource: card.source_name,
    mpcName: card.name,
  };
  return kind === "custom"
    ? projectApi.addMpcCustomImage(args)
    : projectApi.addMpcBackImage(args);
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
  // One request for names, uploaders and formats; art MPC Fill no longer
  // lists still imports under the order's own file name.
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

export interface AutoPickResult {
  /** Faces that now print MPC Fill art. */
  picked: number;
  /** Cards for which MPC Fill had nothing, on any face. */
  unmatched: string[];
  errors: string[];
  canceled: boolean;
}

// One /api/mpcfill/best call covers this many cards (the server's cap).
const BEST_BATCH = 300;

/**
 * Give freshly imported Scryfall cards MPC Fill art: the top match for
 * every face (one search per 300 cards on the server), each downloaded
 * into the library and set as that face's art, one at a time. Faces MPC
 * Fill has nothing for keep their Scryfall art. Cards that already have
 * face art are left alone. Throws only if the matching itself fails.
 */
export async function autoPickMpcArt(
  cards: CardRow[],
  setFaceArt: (cardId: number, face: number, totalFaces: number, image: CustomImage) => Promise<void>,
  onProgress: (done: number, total: number, name: string) => void,
  isCanceled: () => boolean,
): Promise<AutoPickResult> {
  const eligible = cards.filter(
    (c) => c.scryfall_id != null && c.custom_image_id == null && !(c.face_art?.length),
  );
  const ids = [...new Set(eligible.map((c) => c.scryfall_id as string))];
  const best = new Map<string, MpcBestResult["results"][number]>();
  for (let i = 0; i < ids.length; i += BEST_BATCH) {
    const resp = await generationApi.mpcBest({ scryfall_ids: ids.slice(i, i + BEST_BATCH) });
    for (const r of resp.results) best.set(r.scryfall_id, r);
  }

  const jobs: { card: CardRow; face: number; total: number; art: MpcCard }[] = [];
  const unmatched: string[] = [];
  for (const card of eligible) {
    const match = best.get(card.scryfall_id as string);
    const faces = match?.faces ?? [];
    if (!faces.some((f) => f != null)) unmatched.push(card.name);
    faces.forEach((art, face) => {
      if (art && match) jobs.push({ card, face, total: match.total_faces, art });
    });
  }

  const errors: string[] = [];
  let picked = 0;
  for (const [i, job] of jobs.entries()) {
    if (isCanceled()) return { picked, unmatched, errors, canceled: true };
    onProgress(i, jobs.length, job.art.name);
    try {
      // The library check inside importMpcArt makes a repeat (four copies
      // of a basic land on four lines) cost nothing.
      const image = await importMpcArt("custom", job.art);
      await setFaceArt(job.card.id, job.face, job.total, image);
      picked += 1;
    } catch (err) {
      errors.push(`${job.card.name}: ${err instanceof Error ? err.message : String(err)}`);
    }
  }
  onProgress(jobs.length, jobs.length, "");
  return { picked, unmatched, errors, canceled: false };
}
