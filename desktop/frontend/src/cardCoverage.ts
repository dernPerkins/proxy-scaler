// "Is this card ready to print at what I have selected right now?" — the Grid
// view's painted DPI badges are this file's answer drawn onto a tile
// (components/DeckGrid.tsx), and the images modal takes its per-face DPI
// labels from it too.
//
// It reads the same merged faces the Row view shows (DecklistPage's
// facesForCard, built from mergeCardStatus.ts), so the two views can never
// disagree about a card: the badges are the row's own variant list, read at
// the model and goal DPIs the sidebar has selected.
import { faceArtKind } from "./api/project";
import type { CardFaceArt, CardRow } from "./api/project";
import type { CustomImage } from "./api/types";
import { CUSTOM_SOURCE_MODEL, ORIGINAL_DPI, ORIGINAL_MODEL } from "./constants";
import type { CardFaceStatus, VariantStatus } from "./mergeCardStatus";
import type { PendingFaceArt } from "./pickDownloads";

/** One face of a card as the Decklist shows it: every variant it has, `art`
 *  when the face prints something in place of the printing's own (MPC Fill or
 *  uploaded art, or another printing's face), and `pick` for art chosen for
 *  that face and still on its way. */
export type DisplayFace = CardFaceStatus<CardFaceArt> & {
  pick?: PendingFaceArt;
};

// "Original downloaded" is a source-tier variant: the download-only original,
// or a Custom/MPC image's own source (its "full" image IS the uploaded file).
export const SOURCE_MODELS = [ORIGINAL_MODEL, CUSTOM_SOURCE_MODEL];

export type Cover = "done" | "active" | "failed" | "none";

function coverOf(v: VariantStatus | undefined): Cover {
  if (!v) return "none";
  if (v.status === "done") return "done";
  if (v.status === "pending" || v.status === "running") return "active";
  if (v.status === "failed") return "failed";
  return "none";
}

export interface CardCoverage {
  faces: DisplayFace[];
  original: Cover;
  /** What each face's original image IS, in DPI — the number the original's
   *  badge says. A source-tier variant carries it: the server registers a
   *  printing's own Scryfall art at ORIGINAL_DPI (300) and MPC Fill / Custom
   *  Image art at that file's measured DPI (proxy_scaler/db.py). Null when
   *  there is no file to measure yet — an MPC pick that has never downloaded
   *  stores only its drive id. */
  originalDpis: (number | null)[];
  // One entry per goal DPI, for the SELECTED model only — the question is
  // "is this card ready to print at what I have selected", not every variant.
  targets: { dpi: number; cover: Cover }[];
  downloading: boolean;
}

export function faceOriginalDpi(
  f: DisplayFace,
  card: CardRow,
  library: ReadonlyMap<number, CustomImage>,
): number | null {
  const src = f.variants.find((v) => SOURCE_MODELS.includes(v.model));
  if (src) return src.dpi;
  const customId = f.art ? f.art.custom_image_id : card.custom_image_id;
  if (customId != null) {
    const measured = library.get(customId)?.source_dpi;
    return measured != null ? Math.round(measured) : null;
  }
  // No art entry at all, or another printing's face: that art comes from
  // Scryfall, whose images are ~300 DPI.
  if (f.art == null || faceArtKind(f.art) === "scryfall") return ORIGINAL_DPI;
  return null;
}

export function coverageFor(
  card: CardRow,
  faces: DisplayFace[],
  model: string,
  dpis: number[],
  library: ReadonlyMap<number, CustomImage>,
): CardCoverage {
  const all = faces.flatMap((f) => f.variants);
  // A face's original is a source-tier variant. A download that has not
  // produced one yet lives in face.pick — the row shows it as "Downloading ·
  // Original". Fold that in here too, or an original's badge would stay grey
  // (and clickable, offering a second download) while one is already in flight.
  // An upscale downloads the face's original itself, as its first step, and
  // the server registers it the moment it is on disk — so until then a face
  // with an upscale queued or running has its original on the way too, just
  // as if Download had been clicked.
  const faceOriginal = (f: DisplayFace): Cover => {
    const v = f.variants.find((v) => SOURCE_MODELS.includes(v.model));
    if (v) return coverOf(v);
    if (f.pick) return f.pick.status === "failed" ? "failed" : "active";
    if (f.variants.some((v) => coverOf(v) === "active")) return "active";
    return "none";
  };
  const originals = faces.map(faceOriginal);
  const original: Cover =
    originals.length === 0
      ? "none"
      : originals.some((c) => c === "active")
        ? "active"
        : originals.every((c) => c === "done")
          ? "done"
          : originals.some((c) => c === "failed")
            ? "failed"
            : // Some faces downloaded, others not, nothing in flight: there
              // is still a download to run, so the badge stays a button.
              "none";
  return {
    faces,
    original,
    originalDpis: faces.map((f) => faceOriginalDpi(f, card, library)),
    targets: dpis.map((dpi) => ({
      dpi,
      cover: coverOf(all.find((v) => v.dpi === dpi && v.model === model)),
    })),
    downloading: faces.some((f) => f.pick),
  };
}

/** Anything to show this card's images for, yet. */
export function hasImages(c: CardCoverage): boolean {
  return c.faces.some((f) => f.variants.some((v) => v.galleryItemId != null));
}

/** "front" / "back" as the label above a face's images. */
export function faceLabelTitle(label: string): string {
  return label.charAt(0).toUpperCase() + label.slice(1);
}
