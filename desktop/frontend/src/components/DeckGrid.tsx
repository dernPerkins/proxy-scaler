// The Decklist tab's Grid view: every card as its own thumbnail, three to a
// row — the sheet the PDF tab prints to by default (3 columns across an A4
// page), so a deck reads as the pages it becomes instead of as a table — and
// every one of those thumbnails carries the Row view's functionality for that
// card.
//
// It used to be thumbnails only ("viewing only, on purpose"). ADR 0008
// reverses that: a card you can see here is a card you should be able to
// change here, instead of one you have to switch back to Row to touch. What a
// tile now carries:
//
//   · the image the deck would actually print — the FRONT face's variant at
//     the selected model and the highest selected DPI, when one exists —
//     painted with the coverage that got it there: one badge per DPI, lowest
//     first (the original's own DPI, then each goal DPI at the selected
//     model). A grey badge IS its own button: the original's downloads, a
//     goal DPI's upscales just that DPI and nothing else on the card;
//   · the eye bottom-left of the art and the trash top-right, on hover, drawn
//     in pills so they read as the same language as the badges. The eye opens
//     every version of the card in a modal (CardImagesModal.tsx), so no tile
//     ever changes size while you read one card's versions;
//   · under the tile, this card's own controls, the same components the rows
//     use: the printing picker (its button IS the card's set · number ·
//     language, so the tile's own meta row is gone), the card-back swatch and
//     the quantity stepper;
//   · right-click (or Control-click) anywhere on it for the menu: change
//     printing, MPC Fill art, card back, show images, download, upscale,
//     remove. Quantity is not in the menu — the stepper under the tile is
//     already there for it.
//
// Tile art comes from the same places the rest of the app reads it:
//
//   · a card printing its own Scryfall printing (or another printing's
//     face, picked in the change-printing modal) — the generation
//     server's thumbnail endpoint, GET /api/cards/{id}/thumb, never
//     Scryfall directly (same rule as the printing picker's tiles);
//   · a card printing MPC Fill or uploaded art — that Custom Image's own
//     library thumbnail, over IPC, like the Customs tab's tiles;
//   · a generated variant — GET /api/gallery/{id}/thumb, the 640px WebP
//     thumb every other upscale in the app shows;
//   · nothing to show — a printing that was never resolved, MPC art
//     chosen but not downloaded yet, or a server too old to serve
//     thumbnails — a card-shaped placeholder naming the card.
//
// Tiles load as they approach the viewport, five at a time (imageGrid.ts),
// so a deck of hundreds scrolls rather than one-shots its thumbnails — and a
// tile whose image changes (a Generate landed, or the DPI selection moved)
// queues again for a fresh turn instead of fetching outside the queue.
//
// A tile shows its FRONT; a double-faced card's back stays on the Row
// view's second face (and on its Reverse) until this view grows one.
import { useCallback, useEffect, useMemo, useRef, useState, useSyncExternalStore } from "react";
import { useQuery } from "@tanstack/react-query";
import { generationApi } from "../api/generation";
import { projectApi, type CardRow } from "../api/project";
import type { CustomImage, ImageOrigin } from "../api/types";
import { modelDisplayName } from "../constants";
import {
  getProbedDevice,
  serverSupportsPrintingImages,
  subscribeProbedDevice,
  useServerVersion,
} from "../config";
import { coverageFor, hasImages, type CardCoverage, type Cover, type DisplayFace } from "../cardCoverage";
import { fillFrameWith, isAlphaPrinting, trimGeometry } from "../cardTrim";
import { useImageCacheSetting } from "../imageOrigin";
import CardBackPicker from "./CardBackPicker";
import CardImagesModal from "./CardImagesModal";
import { EyeIcon, TrashIcon } from "./RowIcons";
import PrintingPicker, { type PickerOpenRequest } from "./PrintingPicker";
import SetIcon from "./SetIcon";
import { useQueuedThumb, useThumbnailQueue, type ThumbnailQueue } from "./imageGrid";

export interface DeckGridProps {
  /** The deck in the project's sort order — the same list the rows walk,
   *  so switching views doesn't reshuffle the deck. */
  cards: readonly CardRow[];
  /** The Custom Image library by id, for the bleed a tile's art carries. */
  library: ReadonlyMap<number, CustomImage>;
  /** One card's faces, merged from the 3s status poll: the same merge the
   *  rows show, so both views read one state (cardCoverage.ts). */
  facesFor: (card: CardRow) => DisplayFace[];
  /** The sidebar's selection. Badges are drawn for this model only: the
   *  question is what this card is ready to print at now. */
  model: string;
  dpis: number[];
  serverUnavailable: boolean;
  openPicker: (card: CardRow, request: PickerOpenRequest) => void;
  setQuantity: (card: CardRow, quantity: number) => void;
  /** Queue this card's full-size originals, no upscaling. */
  download: (card: CardRow) => void;
  upscale: (card: CardRow) => void;
  /** One painted badge upscales the one DPI it stands for. */
  upscaleAt: (card: CardRow, dpi: number) => void;
  remove: (card: CardRow) => void;
  /** The Row view's own per-image mutations, for the images modal. */
  refetch: (galleryItemId: number) => void;
  regen: (galleryItemId: number) => void;
}

export default function DeckGrid(props: DeckGridProps) {
  const serverVersion = useServerVersion();
  // GET /api/cards/{id}/thumb arrived in 0.5.0; an older server has
  // nothing to serve, and every tile becomes its placeholder.
  const serverThumbs = serverSupportsPrintingImages(serverVersion);
  const { origin } = useImageCacheSetting();
  // Same rule the row uses for its "· CPU" note: only worth saying on a
  // machine that has a GPU and fell back off it.
  const warnCpu = useSyncExternalStore(subscribeProbedDevice, getProbedDevice)?.kind === "gpu";
  // The decklist scrolls with the whole window rather than inside a
  // container of its own, so the queue keeps its root null (the viewport)
  // and loads the rows as they approach.
  const scrollRef = useRef<HTMLElement | null>(null);
  const queue = useThumbnailQueue(scrollRef);
  const [images, setImages] = useState<CardRow | null>(null);
  const [menu, setMenu] = useState<{ card: CardRow; x: number; y: number } | null>(null);

  const coverage = useMemo(
    () =>
      new Map(
        props.cards.map((card) => [
          card.id,
          coverageFor(card, props.facesFor(card), props.model, props.dpis, props.library),
        ]),
      ),
    [props],
  );

  // The menu's "Card back…" opens the same CardBackPicker the control row
  // shows, through the button each cell registers — one picker per card, not
  // a second hidden one.
  const backBtns = useRef(new Map<number, HTMLElement>());
  const registerBack = useCallback((cardId: number, button: HTMLElement | null) => {
    if (button) backBtns.current.set(cardId, button);
    else backBtns.current.delete(cardId);
  }, []);
  const openBackPicker = (card: CardRow) => backBtns.current.get(card.id)?.click();

  // A card's images are always read from the current list: a card removed
  // while its modal is open must not keep showing stale rows.
  const imagesCard = images ? (props.cards.find((c) => c.id === images.id) ?? null) : null;
  const menuActions = menu ? makeActions(props, menu.card) : null;

  return (
    <>
      <div className="deck-grid" data-tour="deck-grid">
        {props.cards.map((card) => (
          <GridCell
            key={card.id}
            card={card}
            c={coverage.get(card.id)!}
            a={makeActions(props, card)}
            p={props}
            origin={origin}
            serverThumbs={serverThumbs}
            queue={queue}
            onShowImages={() => setImages(card)}
            onMenu={(x, y) => setMenu({ card, x, y })}
            onBackTrigger={registerBack}
          />
        ))}
      </div>

      {menu && menuActions && (
        <TileMenu
          x={menu.x}
          y={menu.y}
          items={menuFor(
            coverage.get(menu.card.id)!,
            menuActions,
            menu.card.custom_image_id != null,
            props.serverUnavailable,
            () => openBackPicker(menu.card),
            () => setImages(menu.card),
          )}
          onClose={() => setMenu(null)}
        />
      )}

      {imagesCard && (
        <CardImagesModal
          card={imagesCard}
          faces={props.facesFor(imagesCard)}
          library={props.library}
          serverUnavailable={props.serverUnavailable}
          warnCpu={warnCpu}
          onRefetch={props.refetch}
          onRegen={props.regen}
          onClose={() => setImages(null)}
        />
      )}
    </>
  );
}

/** One card in the grid: its tile, and the control row under it. The tile and
 *  everything painted on it are DeckTile's; this is the part that gives the
 *  tile the Row view's controls. */
function GridCell(props: {
  card: CardRow;
  c: CardCoverage;
  a: TileActions;
  p: DeckGridProps;
  origin: ImageOrigin | null;
  serverThumbs: boolean;
  queue: ThumbnailQueue;
  onShowImages: () => void;
  onMenu: (x: number, y: number) => void;
  onBackTrigger: (cardId: number, button: HTMLElement | null) => void;
}) {
  const { card, c, a, p, origin, serverThumbs, queue, onShowImages, onMenu, onBackTrigger } = props;
  const custom = card.custom_image_id != null;
  const hasSomeImages = hasImages(c);
  // The tile paints the image it would actually print: the FRONT face's variant
  // at the selected model and the highest selected DPI, through the gallery thumb
  // endpoint like every other upscale thumb. When that badge is grey — nothing
  // generated at that pair yet — the tile keeps the source art it would show
  // anyway. DeckTile queues it like any other tile image, so a deck still loads
  // thumbnails as it scrolls, and a variant thumb that 404s costs that image and
  // not the tile.
  const goalDpi = p.dpis.length > 0 ? Math.max(...p.dpis) : null;
  const painted =
    goalDpi != null
      ? c.faces[0]?.variants.find(
          (v) => v.dpi === goalDpi && v.model === p.model && v.status === "done" && v.galleryItemId != null,
        )
      : undefined;
  const paintedUrl =
    painted?.galleryItemId != null ? generationApi.imageUrl(painted.galleryItemId, "thumb") : null;

  return (
    <div
      className="deck-cell"
      onContextMenu={(e) => {
        e.preventDefault();
        onMenu(e.clientX + 2, e.clientY + 2);
      }}
    >
      <span className="deck-tile-wrap">
        <DeckTile
          card={card}
          library={p.library}
          origin={origin}
          serverThumbs={serverThumbs}
          queue={queue}
          variantSrc={paintedUrl}
        />
        {/* Everything painted ON the art lives in a box that mirrors the art
            frame's own box — same width, same 488/680 aspect — so the offsets
            in styles.css are margins off the card, not off the name row under
            it. */}
        <span className="deck-tile-ov">
          {/* The status IS the button here: a grey badge runs its job when
              clicked. Bottom-right: the least interesting corner of a card. */}
          <PaintedBadges c={c} model={p.model} serverUnavailable={p.serverUnavailable} onDownload={a.download} onUpscaleAt={a.upscaleAt} />
          {/* The images live in a modal, so the grid never reflows; the
              right-click menu opens it too, this icon is the shortcut. */}
          <button
            type="button"
            className="deck-pill deck-pill-images deck-reveal"
            onClick={onShowImages}
            disabled={!hasSomeImages}
            title={
              hasSomeImages
                ? "Show images — every version of this card, in a modal"
                : "Nothing to show yet — download or generate this card first"
            }
            aria-label="Show images"
          >
            <EyeIcon />
          </button>
          <button
            type="button"
            className="deck-pill deck-pill-remove deck-reveal"
            onClick={a.remove}
            title="Remove this card from the deck"
            aria-label="Remove"
          >
            <TrashIcon />
          </button>
        </span>
      </span>

      <span className="deck-cell-controls">
        {/* The printing button, the same one the row uses: its label is the
            card's set · number · language, which is why the tile no longer
            draws that row itself. */}
        {custom ? (
          <span className="chip" title="Your own uploaded art — manage it on the Customs tab">
            Custom
          </span>
        ) : (
          <PrintingPicker
            card={card}
            rarity={card.rarity ?? undefined}
            disabled={p.serverUnavailable}
            onOpen={a.openPrintingWith}
          />
        )}
        {/* Mounted visibly, and registered so the right-click menu's
            "Card back…" can open this same control. */}
        <span
          ref={(el) => onBackTrigger(card.id, el?.querySelector("button") ?? null)}
        >
          <CardBackPicker card={card} transformOnBack={false} />
        </span>
        <QtyStepper quantity={a.quantity} onSet={a.setQuantity} />
      </span>
    </div>
  );
}

/** One card's art, and nothing else: the tile the cell above paints its
 *  badges, pills and control row around. Its identity — name and copies — is
 *  the label under the art; its printing is the control row's button. */
function DeckTile(props: {
  card: CardRow;
  library: ReadonlyMap<number, CustomImage>;
  /** null until App Settings' image origin has loaded: nothing is asked
   *  for until the tile knows which origin to ask it through. */
  origin: ImageOrigin | null;
  serverThumbs: boolean;
  queue: ThumbnailQueue;
  /** Paint a generated variant's own thumbnail (the gallery thumb endpoint)
   *  instead of the art this card prints from, so the tile shows the image
   *  that would actually print. It is a tile image like any other here — it
   *  waits for its turn in the load queue and re-queues when it changes — and
   *  it falls back to the source art when it is missing or fails. */
  variantSrc?: string | null;
}) {
  const { card, library, origin, serverThumbs, queue, variantSrc } = props;
  const art = tileArt(card);
  const quantity = card.quantity ?? 1;
  // The image identity that failed, not just "something failed": a variant
  // thumbnail that 404s must not black out a tile that has its source art to
  // fall back on.
  const [failedKey, setFailedKey] = useState<string | null>(null);
  const frameRef = useRef<HTMLSpanElement>(null);
  // Which image this tile WOULD draw, worked out before its turn in the queue
  // is asked for: a tile spends one turn per image, and a generated variant's
  // thumbnail is a different image from the printing's (imageGrid.ts).
  const artUrl =
    art.kind === "server" && serverThumbs && origin != null
      ? generationApi.cardThumbUrl(art.scryfallId, art.face, origin)
      : null;
  const variantUrl = variantSrc != null && serverThumbs ? variantSrc : null;
  const customId = art.kind === "local" ? art.customImageId : null;
  const localKey = customId != null ? `custom:${customId}` : null;
  const imageKey = variantUrl ?? artUrl ?? localKey;
  // Tiles take turns loading. A deck is hundreds of thumbnails, each one a
  // fetch to the sidecar on a machine where the webview opens six connections
  // per host and queues them in the order the tiles were built: unthrottled,
  // the tiles in view wait behind every tile above them. A variant thumbnail
  // needs the queue too, and needs no image origin to ask through: the file is
  // the server's own.
  const canAsk =
    variantUrl != null ||
    art.kind === "local" ||
    (art.kind === "server" && serverThumbs && origin != null);
  const { load, settle } = useQueuedThumb(queue, frameRef, canAsk, false, imageKey);
  const localQuery = useQuery({
    // The same key the Customs tab's tiles use, so a card and the library
    // copy of its art cost one round trip, not two.
    queryKey: ["custom-thumb", customId],
    queryFn: () => projectApi.customImageThumbnail(customId!),
    enabled: customId != null && load,
    staleTime: Infinity,
  });
  const localImage = customId != null ? library.get(customId) : undefined;
  // Fetched-but-nothing is a real answer: the library no longer holds the
  // art this card points at (it was deleted out from under the project).
  // Without treating it as a placeholder the tile would stay an empty
  // frame, and hold its turn in the load queue until that times out.
  const localEmpty = customId != null && localQuery.isFetched && localQuery.data == null;
  // Art declared to carry bleed shows as cut, the bleed cropped off as the
  // Customs tiles and the row thumbnails crop it: the server keeps the
  // bled file, and every view trims it for display.
  const trimmed =
    localImage?.includes_bleed && localImage.bleed_mm > 0
      ? fillFrameWith(
          trimGeometry({ w: localImage.width, h: localImage.height }, localImage.bleed_mm).trim,
        )
      : null;
  // Each candidate carries the name it is marked failed under — a Custom
  // Image's bytes arrive as a data URL, so it gets a short key rather than its
  // own url. Tried in order, once each: a variant thumbnail that 404s (its file
  // is not on the server yet) costs that image, not the tile.
  const candidates: { key: string; url: string | null }[] = [
    ...(variantUrl != null ? [{ key: variantUrl, url: variantUrl }] : []),
    ...(artUrl != null ? [{ key: artUrl, url: artUrl }] : []),
    ...(localKey != null ? [{ key: localKey, url: localQuery.data ?? null }] : []),
  ];
  const shown = candidates.find((c) => load && c.url != null && c.key !== failedKey) ?? null;
  const src = shown?.url ?? null;
  // No src is either "nothing to draw" (placeholder) or "its turn hasn't
  // come / the bytes are on the way" (an empty card-shaped frame).
  const placeholderReason =
    (failedKey != null || localQuery.isError || localEmpty) && candidates.length > 0
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
  // A turn handed to a tile that will never draw (its art is gone, or every
  // candidate has already failed) goes straight back to the queue instead of
  // being held to its 15 s timeout.
  const settledOut = localEmpty || (src == null && failedKey != null);
  useEffect(() => {
    if (settledOut) settle();
  }, [settledOut, settle]);
  const setLabel =
    card.set_code && card.collector_number
      ? `${card.set_code.toUpperCase()} #${card.collector_number}`
      : null;
  // No hover tooltip: the name and copies are the label under the art, and
  // the printing is the control row's button, so one would only repeat them.
  return (
    <div className="deck-tile">
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
              setFailedKey(shown?.key ?? null);
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
    </div>
  );
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

/** The quantity stepper under the tile — the row's stepper in tile size. */
function QtyStepper({ quantity, onSet }: { quantity: number; onSet: (q: number) => void }) {
  return (
    <span className="deck-qty">
      <button className="btn-sm" aria-label="Decrease quantity" disabled={quantity <= 1} onClick={() => onSet(quantity - 1)}>
        −
      </button>
      <span className="deck-qty-n mono">×{quantity}</span>
      <button className="btn-sm" aria-label="Increase quantity" onClick={() => onSet(quantity + 1)}>
        +
      </button>
    </span>
  );
}

/** One card's actions, each one the page's own handler. Plain function (not a
 *  hook) so the right-click menu can build the same actions for whichever card
 *  it is open on. */
interface TileActions {
  quantity: number;
  download: () => void;
  upscale: () => void;
  upscaleAt: (dpi: number) => void;
  remove: () => void;
  setQuantity: (q: number) => void;
  openPrinting: () => void;
  /** The row's own PrintingPicker opens the walk with whatever request it
   *  builds (it knows the pinned printing and MPC state). */
  openPrintingWith: (request: PickerOpenRequest) => void;
  openArt: () => void;
}

function makeActions(p: DeckGridProps, card: CardRow): TileActions {
  return {
    quantity: card.quantity ?? 1,
    download: () => p.download(card),
    upscale: () => p.upscale(card),
    upscaleAt: (dpi) => p.upscaleAt(card, dpi),
    remove: () => p.remove(card),
    setQuantity: (q: number) => p.setQuantity(card, Math.max(1, Math.round(q))),
    openPrinting: () => p.openPicker(card, { source: "scryfall", face: 0 }),
    openArt: () => p.openPicker(card, { source: "mpc", face: 0 }),
    openPrintingWith: (request) => p.openPicker(card, request),
  };
}

/** The status painted ON the art, bottom-right of it (that corner is the least
 *  interesting of a card — often just trademark text). A badge that is grey —
 *  nothing exists for that DPI at the selected model — IS its button: the
 *  original's downloads, a DPI's upscales just that DPI. Done badges are marks.
 *
 *  The original is labelled with the DPI its file actually is rather than "OG",
 *  because it IS one of these badges: the server records a printing's own
 *  Scryfall original at 300 DPI and an MPC Fill / Custom Image original at that
 *  file's measured DPI. Sorted low to high, the row reads as the chain it
 *  describes — the source, then the upscales. */
function PaintedBadges({
  c,
  model,
  serverUnavailable,
  onDownload,
  onUpscaleAt,
}: {
  c: CardCoverage;
  model: string;
  serverUnavailable: boolean;
  onDownload: () => void;
  onUpscaleAt: (dpi: number) => void;
}) {
  const name = modelDisplayName(model);
  // One card can print different sources per face (a Scryfall front, an MPC
  // back), so the badge shows the LOWEST of them — the DPI the card is
  // genuinely printable at from its originals — and the title lists them all.
  const faceDpis = [...new Set(c.originalDpis.filter((d): d is number => d != null))].sort(
    (a, b) => a - b,
  );
  const ogDpi = faceDpis[0] ?? null;
  const badge = (cover: Cover, key: string, label: string, title: string, act?: () => void) => {
    const actionable = act != null && (cover === "none" || cover === "failed") && !serverUnavailable;
    const cls = `deck-badge is-${cover}${actionable ? " is-act" : ""}`;
    return actionable ? (
      <button key={key} type="button" className={cls} title={`${title} — click to run it`} onClick={act}>
        {label}
      </button>
    ) : (
      <span key={key} className={cls} title={title}>
        {label}
      </span>
    );
  };
  const badges = [
    ...c.targets.map((t) => ({
      dpi: t.dpi,
      el: badge(
        t.cover,
        `${t.dpi}`,
        `${t.dpi}`,
        `${t.dpi} DPI · ${name}: ${t.cover}`,
        () => onUpscaleAt(t.dpi),
      ),
    })),
    {
      dpi: ogDpi ?? 0,
      el: badge(
        c.original,
        "og",
        ogDpi != null ? `${ogDpi}` : "OG",
        ogDpi != null
          ? `Original ${faceDpis.join(" · ")} DPI: ${c.original}`
          : `Original (its DPI is known once the file is downloaded): ${c.original}`,
        onDownload,
      ),
    },
  ].sort((a, b) => a.dpi - b.dpi);
  return <span className="deck-badges mono">{badges.map((b) => b.el)}</span>;
}

interface MenuAction {
  run: () => void;
  label: string;
  disabled?: boolean;
  title?: string;
  danger?: boolean;
}

interface MenuItem {
  sep?: true;
  label?: string;
  action?: MenuAction;
}

const SEP: MenuItem = { sep: true };

/** The menu a right-click (or Control-click) on a card opens. No Quantity
 *  items: the stepper under the tile already is one. */
function menuFor(
  c: CardCoverage,
  a: TileActions,
  custom: boolean,
  disabled: boolean,
  openBackPicker: () => void,
  openImages: () => void,
): MenuItem[] {
  const has = hasImages(c);
  return [
    {
      label: "Change printing…",
      action: {
        run: a.openPrinting,
        label: "Change printing…",
        disabled: custom || disabled,
        title: custom ? "A Custom Image has no printing to change" : "Pick which printing this card uses",
      },
    },
    {
      label: "MPC Fill art…",
      action: { run: a.openArt, label: "MPC Fill art…", title: "Print this card from MPC Fill art" },
    },
    {
      label: "Card back…",
      action: { run: openBackPicker, label: "Card back…", title: "Choose what this card's Reverse prints" },
    },
    SEP,
    {
      label: "Show images…",
      action: {
        run: openImages,
        label: "Show images…",
        disabled: !has,
        title: has
          ? "This card's original and every upscale, in a modal (the grid never reflows)"
          : "Nothing to show yet — download or generate this card first",
      },
    },
    {
      label: "Download original",
      action: {
        run: a.download,
        label: "Download original",
        disabled,
        title: "The full-size image, without upscaling",
      },
    },
    {
      label: "Upscale",
      action: {
        run: a.upscale,
        label: "Upscale",
        disabled,
        title: "This card's art to the selected DPIs",
      },
    },
    SEP,
    { label: "Remove from deck", action: { run: a.remove, label: "Remove from deck", danger: true } },
  ];
}

/** The menu itself: opened at the cursor, closed by Escape, an outside click
 *  or picking something. */
function TileMenu({
  x,
  y,
  items,
  onClose,
}: {
  x: number;
  y: number;
  items: MenuItem[];
  onClose: () => void;
}) {
  const menuRef = useRef<HTMLDivElement>(null);
  const firstRef = useRef<HTMLButtonElement>(null);

  useEffect(() => {
    firstRef.current?.focus();
    const el = menuRef.current;
    if (el) {
      const r = el.getBoundingClientRect();
      const nx = Math.max(8, Math.min(x, window.innerWidth - r.width - 8));
      const ny = Math.max(8, Math.min(y, window.innerHeight - r.height - 8));
      el.style.left = `${nx}px`;
      el.style.top = `${ny}px`;
    }
    function onKey(e: KeyboardEvent) {
      if (e.key === "Escape") {
        e.stopPropagation();
        onClose();
      }
    }
    window.addEventListener("keydown", onKey, true);
    return () => window.removeEventListener("keydown", onKey, true);
  }, [x, y, onClose]);

  return (
    <>
      <div
        className="deck-menu-backdrop"
        onContextMenu={(e) => {
          e.preventDefault();
          onClose();
        }}
        onClick={onClose}
      />
      <div ref={menuRef} className="deck-menu mono" style={{ left: x, top: y }} role="menu">
        {items.map((item, i) =>
          item.sep ? (
            <span key={i} className="deck-menu-sep" />
          ) : (
            <button
              key={i}
              type="button"
              role="menuitem"
              className={`deck-menu-item${item.action?.danger ? " is-danger" : ""}`}
              disabled={item.action?.disabled}
              title={item.action?.title}
              ref={i === 0 ? firstRef : undefined}
              onClick={() => {
                item.action?.run();
                onClose();
              }}
            >
              {item.label}
            </button>
          ),
        )}
      </div>
    </>
  );
}
