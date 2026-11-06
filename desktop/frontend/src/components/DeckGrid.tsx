// The Decklist tab's Grid view: every card as its own thumbnail, three to
// a row — the sheet the PDF tab prints to by default (3 columns across an
// A4 page), so a deck reads as the pages it becomes instead of as a table.
//
// Viewing only, on purpose: a tile carries the card's art, its name and
// its count and nothing else. Everything the rows carry (the printing
// picker, per-card backs, the quantity stepper, Download, Upscale) stays
// in the Row view; the page's card header switches between the two.
//
// Tile art comes from the same places the rest of the app reads it:
//
//   · a card printing its own Scryfall printing (or another printing's
//     face, picked in the change-printing modal) — the generation
//     server's thumbnail endpoint, GET /api/cards/{id}/thumb, never
//     Scryfall directly (same rule as the printing picker's tiles);
//   · a card printing MPC Fill or uploaded art — that Custom Image's own
//     library thumbnail, over IPC, like the Customs tab's tiles;
//   · nothing to show — a printing that was never resolved, MPC art
//     chosen but not downloaded yet, or a server too old to serve
//     thumbnails — a card-shaped placeholder naming the card.
//
// A tile shows its FRONT; a double-faced card's back stays on the Row
// view's second face (and on its Reverse) until this view grows one.
import { useEffect, useRef, useState } from "react";
import { useQuery } from "@tanstack/react-query";
import { generationApi } from "../api/generation";
import { projectApi, type CardRow } from "../api/project";
import type { CustomImage, ImageOrigin } from "../api/types";
import { serverSupportsPrintingImages, useServerVersion } from "../config";
import { useImageCacheSetting } from "../imageOrigin";
import { alternateName } from "../cardNames";
import { fillFrameWith, isAlphaPrinting, trimGeometry } from "../cardTrim";
import SetIcon from "./SetIcon";
import { useQueuedThumb, useThumbnailQueue, type ThumbnailQueue } from "./imageGrid";

export interface DeckGridProps {
  /** The deck in the project's sort order — the same list the rows walk,
   *  so switching views doesn't reshuffle the deck. */
  cards: readonly CardRow[];
  /** The Custom Image library by id, for the bleed a tile's art carries. */
  library: ReadonlyMap<number, CustomImage>;
}

/** What one tile draws. A face's MPC Fill or uploaded art wins over the
 *  printing's own; `pending` is such an art chosen but not downloaded
 *  yet, which has no bytes to show. */
type TileArt =
  | { kind: "server"; scryfallId: string; face: number }
  | { kind: "local"; customImageId: number }
  | { kind: "pending" }
  | { kind: "none" };

function tileArt(card: CardRow): TileArt {
  const faceArt = (card.face_art ?? []).find((fa) => fa.face === 0);
  if (faceArt) {
    if (faceArt.scryfall) {
      return {
        kind: "server",
        scryfallId: faceArt.scryfall.scryfall_id,
        face: faceArt.scryfall.source_face,
      };
    }
    // An import with "Art from: MPC Fill" records the drive id before any
    // bytes exist; the row says "Not Downloaded Yet" for it, and so here.
    return faceArt.custom_image_id != null
      ? { kind: "local", customImageId: faceArt.custom_image_id }
      : { kind: "pending" };
  }
  if (card.custom_image_id != null) return { kind: "local", customImageId: card.custom_image_id };
  // A card imported before the resolve flow, or one whose resolve never
  // landed, has a name and maybe a set but no printing id to ask for.
  if (card.scryfall_id) return { kind: "server", scryfallId: card.scryfall_id, face: 0 };
  return { kind: "none" };
}

export default function DeckGrid({ cards, library }: DeckGridProps) {
  const serverVersion = useServerVersion();
  // GET /api/cards/{id}/thumb arrived in 0.5.0; an older server has
  // nothing to serve, and every tile becomes its placeholder.
  const serverThumbs = serverSupportsPrintingImages(serverVersion);
  const { origin } = useImageCacheSetting();
  // The decklist scrolls with the whole window rather than inside a
  // container of its own, so the queue keeps its root null (the viewport)
  // and loads the rows as they approach.
  const scrollRef = useRef<HTMLElement | null>(null);
  const queue = useThumbnailQueue(scrollRef);
  return (
    <div className="deck-grid" data-tour="deck-grid">
      {cards.map((card) => (
        <DeckTile
          key={card.id}
          card={card}
          library={library}
          origin={origin}
          serverThumbs={serverThumbs}
          queue={queue}
        />
      ))}
    </div>
  );
}

function DeckTile(props: {
  card: CardRow;
  library: ReadonlyMap<number, CustomImage>;
  /** null until App Settings' image origin has loaded: nothing is asked
   *  for until the tile knows which origin to ask it through. */
  origin: ImageOrigin | null;
  serverThumbs: boolean;
  queue: ThumbnailQueue;
}) {
  const { card, library, origin, serverThumbs, queue } = props;
  const art = tileArt(card);
  const quantity = card.quantity ?? 1;
  const [failed, setFailed] = useState(false);
  const frameRef = useRef<HTMLSpanElement>(null);
  // Tiles take turns loading (imageGrid.ts). A deck is hundreds of
  // thumbnails, each one a fetch to the sidecar on a machine where the
  // webview opens six connections per host and queues them in the order
  // the tiles were built: unthrottled, the tiles in view wait behind
  // every tile above them.
  const canAsk =
    art.kind === "local" || (art.kind === "server" && serverThumbs && origin != null);
  const { load, settle } = useQueuedThumb(queue, frameRef, canAsk, false);
  const localId = art.kind === "local" ? art.customImageId : null;
  const localQuery = useQuery({
    // The same key the Customs tab's tiles use, so a card and the library
    // copy of its art cost one round trip, not two.
    queryKey: ["custom-thumb", localId],
    queryFn: () => projectApi.customImageThumbnail(localId!),
    enabled: localId != null && load,
    staleTime: Infinity,
  });
  const localImage = localId != null ? library.get(localId) : undefined;
  // Fetched-but-nothing is a real answer: the library no longer holds the
  // art this card points at (it was deleted out from under the project).
  // Without treating it as a placeholder the tile would stay an empty
  // frame, and hold its turn in the load queue until that times out.
  const localEmpty = localId != null && localQuery.isFetched && localQuery.data == null;
  // Art declared to carry bleed shows as cut, the bleed cropped off as the
  // Customs tiles and the row thumbnails crop it: the server keeps the
  // bled file, and every view trims it for display.
  const trimmed =
    localImage?.includes_bleed && localImage.bleed_mm > 0
      ? fillFrameWith(
          trimGeometry({ w: localImage.width, h: localImage.height }, localImage.bleed_mm).trim,
        )
      : null;
  const serverUrl =
    art.kind === "server" && serverThumbs && origin != null && load && !failed
      ? generationApi.cardThumbUrl(art.scryfallId, art.face, origin)
      : null;
  const src = serverUrl ?? (localId != null && load && !failed ? localQuery.data ?? null : null);
  // No src is either "nothing to draw" (placeholder) or "its turn hasn't
  // come / the bytes are on the way" (an empty card-shaped frame).
  const placeholderReason =
    failed || localQuery.isError || localEmpty
      ? "Couldn't load this art"
      : art.kind === "pending"
        ? "MPC Fill art not downloaded yet"
        : art.kind === "none"
          ? card.set_code
            ? "No art found for this card"
            : "No printing pinned"
          : art.kind === "server" && !serverThumbs
            ? "Art needs a newer generation server"
            : null;
  const alt = alternateName(card.name, card.printed_name, card.flavor_name);
  // A turn handed to a tile that will never draw (its art is gone) goes
  // straight back to the queue instead of being held to its 15 s timeout.
  const settledOut = localEmpty || failed;
  useEffect(() => {
    if (settledOut) settle();
  }, [settledOut, settle]);
  const setLabel =
    card.set_code && card.collector_number
      ? `${card.set_code.toUpperCase()} #${card.collector_number}`
      : null;
  const title = [
    card.name,
    alt ? `printed as ${alt}` : null,
    setLabel,
    card.custom_image_id != null ? "Custom Image" : null,
    `×${quantity}`,
  ]
    .filter(Boolean)
    .join(" · ");

  return (
    <div className="deck-tile" title={title}>
      <span
        ref={frameRef}
        className={`deck-tile-frame${isAlphaPrinting(card.set_code) ? " alpha-corners" : ""}`}
      >
        {src ? (
          <img
            src={src}
            alt={card.name}
            decoding="async"
            draggable={false}
            style={
              trimmed
                ? {
                    display: "block",
                    position: "absolute",
                    maxWidth: "none",
                    objectFit: "fill",
                    ...trimmed,
                  }
                : undefined
            }
            onLoad={settle}
            onError={() => {
              settle();
              setFailed(true);
            }}
          />
        ) : placeholderReason ? (
          <TilePlaceholder
            card={card}
            quantity={quantity}
            setLabel={setLabel}
            reason={placeholderReason}
          />
        ) : null}
      </span>
      <span className="deck-tile-label">
        <span className="deck-tile-name">{card.name}</span>
        {quantity > 1 && <span className="deck-tile-qty">×{quantity}</span>}
      </span>
      <span className="deck-tile-meta mono">
        {card.custom_image_id != null ? (
          "Custom"
        ) : setLabel ? (
          <>
            <SetIcon setCode={card.set_code!} rarity={card.rarity ?? undefined} />
            {setLabel}
          </>
        ) : (
          "Not pinned"
        )}
      </span>
    </div>
  );
}

/** The stand-in for a tile with no art to draw: card-shaped, so the grid
 *  keeps its shape, and carrying enough to tell the cards apart. */
function TilePlaceholder({
  card,
  quantity,
  setLabel,
  reason,
}: {
  card: CardRow;
  quantity: number;
  setLabel: string | null;
  reason: string;
}) {
  return (
    <span className="deck-tile-placeholder">
      <span className="deck-tile-placeholder-note">{reason}</span>
      {setLabel && (
        <span className="mono">
          <SetIcon setCode={card.set_code!} rarity={card.rarity ?? undefined} />
          {setLabel}
        </span>
      )}
      <span className="deck-tile-placeholder-name">
        {card.name}
        {quantity > 1 ? ` ×${quantity}` : ""}
      </span>
    </span>
  );
}
