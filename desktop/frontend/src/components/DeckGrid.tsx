// The Decklist tab's Grid view: every PRINTED COPY as its own thumbnail, laid
// out on the pages the PDF tab's settings describe — its column count across,
// a new page every cols × rows copies, each page headed "Page n · x of y
// Slots" — so a deck reads as the sheets it becomes (a rough PDF preview: no
// mm-accurate margins or bleed, those are the PDF tab's). Every thumbnail
// carries the Row view's functionality for its card.
//
// Copies, not cards (docs/adr/0009): a 4-of is four tiles, and each can be
// picked up — press and move the pointer a few pixels — and dropped anywhere;
// the copies after the drop point shift along one slot, so the order never
// has holes. Let go before it moves and it was a click: on the art, that opens
// the card's printing picker, the same one its set · number button opens. The first drag switches the shared Sort to Custom, starting from
// the order on screen, and the PDF and ZIP then print in that order. A copy is
// independent in placement only: its printing, art and back are its card's,
// so changing them on one tile changes every copy (hovering a tile outlines
// its siblings). Quantity has no stepper here — the trash removes that copy,
// the menu adds another — while the Row view keeps its stepper.
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
//     language, so the tile's own meta row is gone) and the card-back swatch;
//   · right-click (or Control-click) anywhere on it for the menu: change
//     printing, MPC Fill art, card back, show images, download, upscale, add
//     another copy, remove this copy, remove all copies.
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
import {
  useCallback,
  useEffect,
  useMemo,
  useRef,
  useState,
  useSyncExternalStore,
  type CSSProperties,
} from "react";
import {
  DndContext,
  DragOverlay,
  KeyboardSensor,
  PointerSensor,
  closestCenter,
  useSensor,
  useSensors,
  type DragEndEvent,
  type DragStartEvent,
} from "@dnd-kit/core";
import {
  SortableContext,
  arrayMove,
  rectSortingStrategy,
  sortableKeyboardCoordinates,
  useSortable,
} from "@dnd-kit/sortable";
import { CSS } from "@dnd-kit/utilities";
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
import type { DeckCopy } from "../deckEntries";
import { fillFrameWith, isAlphaPrinting, trimGeometry } from "../cardTrim";
import { useImageCacheSetting } from "../imageOrigin";
import CardBackPicker from "./CardBackPicker";
import CardImagesModal from "./CardImagesModal";
import { EyeIcon, TrashIcon } from "./RowIcons";
import PrintingPicker, { pickerRequestFor, type PickerOpenRequest } from "./PrintingPicker";
import SetIcon from "./SetIcon";
import PrototypeSwitcher, { usePrototypeVariant } from "./PrototypeSwitcher";
import { useQueuedThumb, useThumbnailQueue, type ThumbnailQueue } from "./imageGrid";

// PROTOTYPE — throwaway. What sits under a tile's art, switchable with
// `?tileFooter=` (the floating bar, or ← / →): A is the footer as it ships,
// B drops the name and the printing / back controls for just the View button
// and the DPI badges (which leave the art). Delete once one wins.
const TILE_FOOTERS = [
  { key: "A", name: "Current: Name + Printing + Back" },
  { key: "B", name: "View + DPI Badges" },
] as const;
type TileFooter = (typeof TILE_FOOTERS)[number]["key"];

export interface DeckGridProps {
  /** Every printed copy in print order (deckEntries.deckCopies): the
   *  placements under a Custom sort, otherwise the rows' order with each
   *  card's copies side by side. */
  copies: readonly DeckCopy[];
  /** The PDF tab's grid: columns across a page, rows down it. */
  cols: number;
  rows: number;
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
  /** A drop: every copy's placement id, in the new order. */
  reorder: (placementIds: number[]) => void;
  /** One more copy of the card; it lands after its siblings. */
  addCopy: (card: CardRow) => void;
  /** Just this copy — the card too when it was the last. */
  removeCopy: (copy: DeckCopy) => void;
  /** Queue this card's full-size originals, no upscaling. */
  download: (card: CardRow) => void;
  upscale: (card: CardRow) => void;
  /** One painted badge upscales the one DPI it stands for. */
  upscaleAt: (card: CardRow, dpi: number) => void;
  /** Every copy of the card. */
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
  const [menu, setMenu] = useState<{ copy: DeckCopy; x: number; y: number } | null>(null);
  // The card under the pointer: its other copies get an outline, so it is
  // plain what a printing or back change on this tile will also touch.
  const [hover, setHover] = useState<{ card: number; key: string } | null>(null);

  // One coverage per card, shared by all its copies.
  const cards = useMemo(() => {
    const seen = new Map<number, CardRow>();
    for (const c of props.copies) if (!seen.has(c.card.id)) seen.set(c.card.id, c.card);
    return [...seen.values()];
  }, [props.copies]);
  const coverage = useMemo(
    () =>
      new Map(
        cards.map((card) => [
          card.id,
          coverageFor(card, props.facesFor(card), props.model, props.dpis, props.library),
        ]),
      ),
    // eslint-disable-next-line react-hooks/exhaustive-deps
    [cards, props.facesFor, props.model, props.dpis, props.library],
  );

  // Press and move to pick a copy up: nothing lifts until the pointer has
  // travelled 8px, so a press released in place stays a click — on the art
  // that opens the printing picker, and the printing button, badges, eye and
  // trash keep theirs. Once a drag has started, dnd-kit swallows the click
  // its release would fire. The keyboard sensor picks up the focused tile with
  // Space/Enter (only the tile itself — a key on a button inside it stays
  // that button's).
  const sensors = useSensors(
    useSensor(PointerSensor, { activationConstraint: { distance: 8 } }),
    useSensor(KeyboardSensor, { coordinateGetter: sortableKeyboardCoordinates }),
  );
  const [dragging, setDragging] = useState<{ key: string; src: string | null; name: string } | null>(
    null,
  );
  const onDragStart = (e: DragStartEvent) => {
    const key = String(e.active.id);
    const copy = props.copies.find((c) => c.key === key);
    // The floating copy shows the art the tile already has on screen —
    // read off the tile rather than queued and fetched a second time.
    const el = document.querySelector<HTMLImageElement>(`[data-copy-key="${CSS_ESCAPE(key)}"] .deck-tile-frame img`);
    setDragging({ key, src: el?.currentSrc || el?.src || null, name: copy?.card.name ?? "" });
    setMenu(null);
  };
  const onDragEnd = (e: DragEndEvent) => {
    setDragging(null);
    const { active, over } = e;
    if (!over || active.id === over.id) return;
    const from = props.copies.findIndex((c) => c.key === active.id);
    const to = props.copies.findIndex((c) => c.key === over.id);
    if (from < 0 || to < 0) return;
    const next = arrayMove([...props.copies], from, to);
    // A copy whose placement hasn't been read back yet (added a moment ago)
    // has no id to send; the drop is dropped rather than half-applied.
    if (next.some((c) => c.placementId == null)) return;
    props.reorder(next.map((c) => c.placementId!));
  };

  const cols = Math.max(1, Math.round(props.cols));
  const perPage = cols * Math.max(1, Math.round(props.rows));
  const pages: DeckCopy[][] = [];
  for (let i = 0; i < props.copies.length; i += perPage) pages.push(props.copies.slice(i, i + perPage));

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
  const imagesCard = images ? (cards.find((c) => c.id === images.id) ?? null) : null;
  const menuActions = menu ? makeActions(props, menu.copy) : null;
  // The card-back picker is mounted once per card (its first copy), so the
  // menu's "Card Back…" has exactly one control to open.
  const tileFooter = usePrototypeVariant(TILE_FOOTERS, "tileFooter") as TileFooter;
  const firstCopyKey = new Map<number, string>();
  for (const c of props.copies) if (!firstCopyKey.has(c.card.id)) firstCopyKey.set(c.card.id, c.key);

  return (
    <>
      <DndContext
        sensors={sensors}
        collisionDetection={closestCenter}
        onDragStart={onDragStart}
        onDragEnd={onDragEnd}
        onDragCancel={() => setDragging(null)}
      >
        <SortableContext items={props.copies.map((c) => c.key)} strategy={rectSortingStrategy}>
          <div className="deck-pages" data-tour="deck-grid">
            {pages.map((page, pi) => (
              <section key={pi} className="deck-page">
                <header className="deck-page-head mono">
                  Page {pi + 1} · {page.length} of {perPage} Slots
                </header>
                <div className="deck-grid" style={{ "--deck-cols": cols } as CSSProperties}>
                  {page.map((copy) => (
                    <SortableCell
                      key={copy.key}
                      copy={copy}
                      sibling={hover?.card === copy.card.id && hover.key !== copy.key}
                      lifted={dragging?.key === copy.key}
                      c={coverage.get(copy.card.id)!}
                      a={makeActions(props, copy)}
                      p={props}
                      origin={origin}
                      serverThumbs={serverThumbs}
                      queue={queue}
                      ownsBackPicker={firstCopyKey.get(copy.card.id) === copy.key}
                      onHover={(on) =>
                        setHover((h) =>
                          on ? { card: copy.card.id, key: copy.key } : h?.key === copy.key ? null : h,
                        )
                      }
                      onShowImages={() => setImages(copy.card)}
                      onMenu={(x, y) => setMenu({ copy, x, y })}
                      onBackTrigger={registerBack}
                      footer={tileFooter}
                    />
                  ))}
                  {pi === pages.length - 1 &&
                    Array.from({ length: perPage - page.length }, (_, i) => (
                      <span key={`empty-${i}`} className="deck-slot-empty" aria-hidden="true" />
                    ))}
                </div>
              </section>
            ))}
          </div>
        </SortableContext>
        <DragOverlay>
          {dragging ? (
            <span className="deck-drag-ghost">
              {dragging.src ? (
                <img src={dragging.src} alt={dragging.name} draggable={false} />
              ) : (
                <span className="deck-drag-ghost-name">{dragging.name}</span>
              )}
            </span>
          ) : null}
        </DragOverlay>
      </DndContext>
      <PrototypeSwitcher variants={TILE_FOOTERS} param="tileFooter" label="Tile Footer" />

      {menu && menuActions && (
        <TileMenu
          x={menu.x}
          y={menu.y}
          items={menuFor(
            coverage.get(menu.copy.card.id)!,
            menuActions,
            menu.copy.card.custom_image_id != null,
            props.serverUnavailable,
            () => openBackPicker(menu.copy.card),
            () => setImages(menu.copy.card),
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

/** `CSS.escape` where the webview has it; keys are `p123` / `c12-0`, so the
 *  fallback never has anything to escape anyway. */
const CSS_ESCAPE = (s: string) => (typeof window !== "undefined" && window.CSS?.escape ? window.CSS.escape(s) : s);

/** One printed copy in the grid: its tile, and the control row under it. The
 *  tile and everything painted on it are DeckTile's; this is the part that
 *  gives the tile the Row view's controls, and makes it something you can
 *  pick up. */
function SortableCell(props: {
  copy: DeckCopy;
  /** Another copy of the card under the pointer. */
  sibling: boolean;
  /** This copy is the one being dragged: its slot stays, faded, as the
   *  drop target the others shift around. */
  lifted: boolean;
  c: CardCoverage;
  a: TileActions;
  p: DeckGridProps;
  origin: ImageOrigin | null;
  serverThumbs: boolean;
  queue: ThumbnailQueue;
  ownsBackPicker: boolean;
  onHover: (on: boolean) => void;
  onShowImages: () => void;
  onMenu: (x: number, y: number) => void;
  onBackTrigger: (cardId: number, button: HTMLElement | null) => void;
  /** PROTOTYPE: which footer to draw under the art. */
  footer: TileFooter;
}) {
  const { copy, sibling, lifted, c, a, p, origin, serverThumbs, queue, ownsBackPicker, footer } = props;
  const { onHover, onShowImages, onMenu, onBackTrigger } = props;
  const card = copy.card;
  const { attributes, listeners, setNodeRef, setActivatorNodeRef, transform, transition } = useSortable({
    id: copy.key,
  });
  const style: CSSProperties = {
    transform: CSS.Translate.toString(transform),
    transition,
  };
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
      ref={(el) => {
        setNodeRef(el);
        setActivatorNodeRef(el);
      }}
      style={style}
      className={`deck-cell${sibling ? " is-sibling" : ""}${lifted ? " is-lifted" : ""}`}
      data-copy-key={copy.key}
      {...attributes}
      {...listeners}
      aria-roledescription="card copy — hold to move"
      onMouseEnter={() => onHover(true)}
      onMouseLeave={() => onHover(false)}
      onContextMenu={(e) => {
        e.preventDefault();
        onMenu(e.clientX + 2, e.clientY + 2);
      }}
    >
      {/* A click on the art (released before the pointer moved far enough to
          drag) opens the same picker as the printing button under it; a
          Custom Image has none, so nothing. A click on a badge or pill stays
          that button's. */}
      <span
        className={`deck-tile-wrap${custom || p.serverUnavailable ? "" : " is-pickable"}`}
        onClick={(e) => {
          if ((e.target as HTMLElement).closest("button")) return;
          if (custom || p.serverUnavailable) return;
          a.openPrintingWith(pickerRequestFor(card));
        }}
      >
        <DeckTile
          card={card}
          library={p.library}
          origin={origin}
          serverThumbs={serverThumbs}
          queue={queue}
          variantSrc={paintedUrl}
          showName={footer === "A"}
        />
        {/* Everything painted ON the art lives in a box that mirrors the art
            frame's own box — same width, same 488/680 aspect — so the offsets
            in styles.css are margins off the card, not off the name row under
            it. */}
        <span className="deck-tile-ov">
          {/* The status IS the button here: a grey badge runs its job when
              clicked. Bottom-right: the least interesting corner of a card. */}
          {footer === "A" && (
            <PaintedBadges c={c} model={p.model} serverUnavailable={p.serverUnavailable} onDownload={a.download} onUpscaleAt={a.upscaleAt} />
          )}
          {/* The images live in a modal, so the grid never reflows; the
              right-click menu opens it too, this icon is the shortcut. */}
          {footer === "A" && <button
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
          </button>}
          <button
            type="button"
            className={
              footer === "B"
                ? "btn-sm btn-danger icon-button proto-tile-trash deck-reveal"
                : "deck-pill deck-pill-remove deck-reveal"
            }
            onClick={a.removeCopy}
            title={
              a.quantity > 1
                ? `Remove this copy (${a.quantity - 1} left)`
                : "Remove this card from the deck"
            }
            aria-label="Remove this copy"
          >
            <TrashIcon />
          </button>
        </span>
      </span>

      {footer === "B" ? (
        <span className="proto-tile-footer">
          <PaintedBadges c={c} model={p.model} serverUnavailable={p.serverUnavailable} onDownload={a.download} onUpscaleAt={a.upscaleAt} look="row" />
          <button
            type="button"
            className="btn-sm icon-button"
            aria-label="Show images"
            onClick={onShowImages}
            disabled={!hasSomeImages}
            title={
              hasSomeImages
                ? "Every version of this card, in a modal"
                : "Nothing to show yet — download or generate this card first"
            }
          >
            <EyeIcon />
          </button>
          {/* Not shown, but still mounted for the first copy so the
              right-click menu's "Card Back…" has a control to open. */}
          {ownsBackPicker && (
            <span
              className="proto-tile-back"
              ref={(el) => onBackTrigger(card.id, el?.querySelector("button") ?? null)}
            >
              <CardBackPicker card={card} transformOnBack={false} />
            </span>
          )}
        </span>
      ) : (
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
        {/* Mounted visibly on every copy (they share the card's back), and
            the first copy's is registered so the right-click menu's
            "Card Back…" opens this same control. */}
        <span
          ref={
            ownsBackPicker
              ? (el) => onBackTrigger(card.id, el?.querySelector("button") ?? null)
              : undefined
          }
        >
          <CardBackPicker card={card} transformOnBack={false} />
        </span>
      </span>
      )}
    </div>
  );
}

/** One copy's art, and nothing else: the tile the cell above paints its
 *  badges, pills and control row around. Its identity — the name — is the
 *  label under the art; its printing is the control row's button. */
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
  /** PROTOTYPE: footer B has no name row. */
  showName?: boolean;
}) {
  const { card, library, origin, serverThumbs, queue, variantSrc, showName = true } = props;
  const art = tileArt(card);
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
  // No hover tooltip: the name is the label under the art, and
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
          <TilePlaceholder card={card} setLabel={setLabel} reason={placeholderReason} />
        ) : null}
      </span>
      {showName && (
        <span className="deck-tile-label">
          <span className="deck-tile-name">{card.name}</span>
        </span>
      )}
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
  setLabel,
  reason,
}: {
  card: CardRow;
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
      <span className="deck-tile-placeholder-name">{card.name}</span>
    </span>
  );
}

/** One copy's actions, each one the page's own handler. Plain function (not a
 *  hook) so the right-click menu can build the same actions for whichever copy
 *  it is open on. Everything but the copy actions is its card's. */
interface TileActions {
  quantity: number;
  download: () => void;
  upscale: () => void;
  upscaleAt: (dpi: number) => void;
  remove: () => void;
  removeCopy: () => void;
  addCopy: () => void;
  openPrinting: () => void;
  /** The row's own PrintingPicker opens the walk with whatever request it
   *  builds (it knows the pinned printing and MPC state). */
  openPrintingWith: (request: PickerOpenRequest) => void;
  openArt: () => void;
}

function makeActions(p: DeckGridProps, copy: DeckCopy): TileActions {
  const card = copy.card;
  return {
    quantity: card.quantity ?? 1,
    download: () => p.download(card),
    upscale: () => p.upscale(card),
    upscaleAt: (dpi) => p.upscaleAt(card, dpi),
    remove: () => p.remove(card),
    removeCopy: () => p.removeCopy(copy),
    addCopy: () => p.addCopy(card),
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
  look = "painted",
}: {
  c: CardCoverage;
  model: string;
  serverUnavailable: boolean;
  onDownload: () => void;
  onUpscaleAt: (dpi: number) => void;
  /** PROTOTYPE: "row" draws the Row view's StatusBadge pills (dot + label)
   *  instead of the solid pills painted on the art. */
  look?: "painted" | "row";
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
    if (look === "row") {
      // The Row view's badge colours: done green, in flight blue, failed red,
      // nothing yet the neutral "off" grey.
      const status = { done: "done", active: "running", failed: "failed", none: "canceled" }[cover];
      const rowCls = `badge badge-${status}${actionable ? " proto-badge-act" : ""}`;
      const body = (
        <>
          <span className="dot" />
          {label}
        </>
      );
      return actionable ? (
        <button key={key} type="button" className={rowCls} title={`${title} — click to run it`} onClick={act}>
          {body}
        </button>
      ) : (
        <span key={key} className={rowCls} title={title}>
          {body}
        </span>
      );
    }
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
  return (
    <span className={`deck-badges${look === "row" ? "" : " mono"}`}>{badges.map((b) => b.el)}</span>
  );
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

/** The menu a right-click (or Control-click) on a copy opens. Its copy items
 *  stand in for the Row view's quantity stepper. */
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
      label: "Change Printing…",
      action: {
        run: a.openPrinting,
        label: "Change Printing…",
        disabled: custom || disabled,
        title: custom ? "A Custom Image has no printing to change" : "Pick which printing this card uses",
      },
    },
    {
      label: "MPC Fill Art…",
      action: { run: a.openArt, label: "MPC Fill Art…", title: "Print this card from MPC Fill art" },
    },
    {
      label: "Card Back…",
      action: { run: openBackPicker, label: "Card Back…", title: "Choose what this card's Reverse prints" },
    },
    SEP,
    {
      label: "Show Images…",
      action: {
        run: openImages,
        label: "Show Images…",
        disabled: !has,
        title: has
          ? "This card's original and every upscale, in a modal (the grid never reflows)"
          : "Nothing to show yet — download or generate this card first",
      },
    },
    {
      label: "Download Original",
      action: {
        run: a.download,
        label: "Download Original",
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
    {
      label: "Add Another Copy",
      action: {
        run: a.addCopy,
        label: "Add Another Copy",
        title: "One more copy of this card, placed after its others",
      },
    },
    ...(a.quantity > 1
      ? [
          {
            label: "Remove This Copy",
            action: { run: a.removeCopy, label: "Remove This Copy", title: "Just this copy" },
          },
          {
            label: `Remove All ${a.quantity} Copies`,
            action: { run: a.remove, label: `Remove All ${a.quantity} Copies`, danger: true },
          },
        ]
      : [{ label: "Remove From Deck", action: { run: a.remove, label: "Remove From Deck", danger: true } }]),
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
