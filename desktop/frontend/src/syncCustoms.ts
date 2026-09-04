// Pushing Custom Image bytes to the generation server, just in time.
//
// This is the client half of "images stay on this machine until the server
// actually needs them". Nothing uploads on add; instead every action that
// requires the server to *see* the art — generating, downloading sources,
// rendering a PDF, building a ZIP — calls this first.
//
// Cheap to call unconditionally. The Rust side GETs /api/customs/{hash}
// and only POSTs on a miss, so an image the server already holds costs one
// small request. That also makes switching servers self-healing: the new
// host misses, and the next generate or export fills it.
import { projectApi } from "./api/project";
import type { CardRow } from "./api/project";
import { getApiBaseUrl, serverSupportsCustomImages } from "./config";

/**
 * Make sure the connected server holds the art for every Custom Image
 * card in `cards`.
 *
 * Sequential, not Promise.all: each miss is a multi-MB upload, and firing
 * thirty of them at once at a server that is about to start doing GPU work
 * is how a bulk generate turns into a timeout. Deduplicated first, since
 * the same image can legitimately appear as several cards.
 */
export async function syncCustomImages(
  cards: CardRow[],
  serverVersion?: string | null,
): Promise<void> {
  const ids = [
    ...new Set(
      cards.map((c) => c.custom_image_id).filter((id): id is number => id != null),
    ),
  ];
  if (ids.length === 0) return;
  // Checked here rather than at each call site because this is the one
  // place every custom-art action funnels through, and the failure it
  // guards is silent: /api/customs 404s, and the generate or export that
  // follows sends a custom_hash an older server's Pydantic model drops —
  // leaving an entry with no printing, which matches nothing, or matches
  // whatever real card happens to share the file's name.
  if (serverVersion !== undefined && !serverSupportsCustomImages(serverVersion)) {
    throw new Error(
      "This project has custom card images, which need a newer generation " +
        `server${serverVersion ? ` (it reports v${serverVersion})` : ""}. ` +
        "Update the server, or remove the custom cards.",
    );
  }
  const baseUrl = getApiBaseUrl();
  for (const id of ids) {
    await projectApi.syncCustomImage(id, baseUrl);
  }
}

/** Whether any of these cards is a Custom Image — used to skip the sync
 *  entirely (and to decide whether the server-version floor matters) for
 *  the common all-Scryfall project. */
export function hasCustomCards(cards: CardRow[]): boolean {
  return cards.some((c) => c.custom_image_id != null);
}
