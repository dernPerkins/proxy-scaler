// PROTOTYPE — throwaway. A third Decklist view: the deck as it sits on the
// printed pages, laid out with the PDF tab's settings as they are right now
// (page size, cols × rows, spacing, bleed, offsets). Change the PDF tab and
// this moves with it — that is what is being evaluated: does seeing the real
// page answer questions the Grid view's fixed three-up cannot.
//
// Deliberately simplified, because it previews a layout rather than prints:
//   · fronts only — no back pages, no duplex mirroring, no face pairing;
//   · no Card/Page Guides and no registration marks; the page is not drawn as
//     paper, only as the grid the PDF settings describe (cols × rows at the
//     real page ratio), one page per row so its slots are tile-sized;
//   · a slot paints the bled box (what actually prints) with a dashed hairline
//     where the trim falls; its image is the one this deck would print — the
//     front face at the selected model and the highest goal DPI, falling back
//     to the source art, through the same thumbnail queue the Grid view uses.
// The controls it borrows from variant D are the ones under test: the
// right-click menu, and the painted per-DPI badges. A page has no stepper of
// its own, so quantity stays in the menu. Delete with the other prototypes.
// Dev builds only.
import { useCallback, useMemo, useRef, useState, useSyncExternalStore } from "react";
import type { CSSProperties } from "react";
import { generationApi } from "../api/generation";
import type { CardRow, ProjectSettings } from "../api/project";
import type { ImageOrigin } from "../api/types";
import { modelDisplayName } from "../constants";
import {
  getProbedDevice,
  serverSupportsPrintingImages,
  subscribeProbedDevice,
  useServerVersion,
} from "../config";
import { useImageCacheSetting } from "../imageOrigin";
import CardBackPicker from "../components/CardBackPicker";
import { DeckTile } from "../components/DeckGrid";
import { useThumbnailQueue, type ThumbnailQueue } from "../components/imageGrid";
import {
  CardImagesModal,
  ContextMenu,
  PaintedBadges,
  coverageFor,
  makeActions,
  menuFor,
  type Coverage,
  type DeckGridKit,
} from "./decklist-grid.prototype";
import "./decklist-pages.prototype.css";

import {
  paginateDeck,
  pageArStyle,
  resolvePageLayout,
  slotStyleFor,
  trimInsetFor,
} from "./deck-page-layout.prototype";

export default function DeckPagesPrototype({
  cards,
  settings,
  kit,
}: {
  cards: readonly CardRow[];
  settings: ProjectSettings;
  kit: DeckGridKit;
}) {
  const serverVersion = useServerVersion();
  const serverThumbs = serverSupportsPrintingImages(serverVersion);
  const { origin } = useImageCacheSetting();
  // Same rule the row uses for its "· CPU" note, for the images modal.
  const warnCpu = useSyncExternalStore(subscribeProbedDevice, getProbedDevice)?.kind === "gpu";
  // Pages scroll with the window, so the queue's root stays null (the viewport)
  // — the same arrangement DeckGrid and the Grid prototypes use.
  const scrollRef = useRef<HTMLElement | null>(null);
  const queue = useThumbnailQueue(scrollRef);
  const [imagesCard, setImagesCard] = useState<CardRow | null>(null);
  // The menu belongs to a slot, not only to a card: a 4-of has four of them,
  // and "Card back…" opens the picker mounted in the one you clicked.
  const [menu, setMenu] = useState<{ card: CardRow; pickerKey: string; x: number; y: number } | null>(
    null,
  );
  const [log, setLog] = useState<string[]>([]);
  const backBtns = useRef(new Map<string, HTMLElement>());

  const addLog = useCallback((s: string) => {
    setLog((prev) => [`${new Date().toLocaleTimeString()} · ${s}`, ...prev].slice(0, 6));
  }, []);

  const layout = useMemo(() => resolvePageLayout(settings), [settings]);
  const pages = useMemo(() => paginateDeck(cards, layout.capacity), [cards, layout]);
  const coverage = useMemo(
    () =>
      new Map<number, Coverage>(
        cards.map((card) => [
          card.id,
          coverageFor(card, kit.facesFor(card), kit.model, kit.dpis, kit.library),
        ]),
      ),
    [cards, kit],
  );
  // What each slot draws: the front face at the selected model and the highest
  // goal DPI, when that variant exists (variant D's rule). Null is DeckTile's
  // business then — it shows the source art it would print from today.
  const goalDpi = kit.dpis.length > 0 ? Math.max(...kit.dpis) : null;
  const paintedUrls = useMemo(() => {
    const out = new Map<number, string | null>();
    for (const card of cards) {
      const c = coverage.get(card.id);
      const done = c?.faces[0]?.variants.find(
        (v) => v.dpi === goalDpi && v.model === kit.model && v.status === "done" && v.galleryItemId != null,
      );
      const url = done?.galleryItemId != null ? generationApi.imageUrl(done.galleryItemId, "thumb") : null;
      out.set(card.id, url);
    }
    return out;
  }, [cards, coverage, goalDpi, kit.model]);

  // One style per grid position: the same mm box, expressed as a percentage of
  // the page so the page can be any size on screen.
  const slotStyles = useMemo(
    () => Array.from({ length: layout.capacity }, (_, i) => slotStyleFor(layout, i)),
    [layout],
  );
  // The trim edge, as an inset of its own slot: the bleed, on each side.
  const trimInset = trimInsetFor(layout);

  const registerBack = useCallback((key: string, button: HTMLElement | null) => {
    if (button) backBtns.current.set(key, button);
    else backBtns.current.delete(key);
  }, []);
  const openBackPicker = (key: string, card: CardRow) => {
    addLog(`Open card back picker · ${card.name}`);
    backBtns.current.get(key)?.click();
  };
  const showImages = useCallback(
    (card: CardRow) => {
      setImagesCard(card);
      addLog(`Open images modal · ${card.name}`);
    },
    [addLog],
  );
  const menuActions = menu ? makeActions(kit, menu.card, addLog) : null;

  const copies = pages.reduce((n, page) => n + page.length, 0);
  const lastPage = pages[pages.length - 1];
  // A grid larger than its page shows up as negative margins, which is also
  // what the server clips. Say so instead of drawing it quietly.
  const overflows = layout.marginX < 0 || layout.marginY < 0;
  const mm = (v: number) => `${v.toFixed(1)}`;

  return (
    <div className="pages-wrap">
      <div className="proto-state mono">
        <div className="proto-state-head">
          Pages view · {cards.length} cards · {copies} printed cards · {pages.length}{" "}
          {pages.length === 1 ? "page" : "pages"} · {layout.cols}×{layout.rows} ·{" "}
          {modelDisplayName(kit.model)} · {kit.dpis.join(" / ")} DPI
          {kit.serverUnavailable && " · generation server unreachable"}
        </div>
        <div className="proto-state-row">
          <span title="The page this view draws, from the PDF tab">
            page {mm(layout.pageW)}×{mm(layout.pageH)} mm
          </span>
          <span title="Card plus the bleed that prints around it">
            slot {mm(layout.bledW)}×{mm(layout.bledH)} mm
          </span>
          <span title="Gap between cards">
            gap {mm(settings.spacing_x_mm)} / {mm(settings.spacing_y_mm)} mm
          </span>
          <span title="Where the first card lands, from the page's left and top edge">
            margin {mm(layout.marginX)} / {mm(layout.marginY)} mm
          </span>
          <span title="Printed cards per page, and how full the last one is">
            {lastPage ? `${lastPage.length}/${layout.capacity} on the last page` : "no cards"}
          </span>
          <span title="This view draws fronts only">fronts only</span>
        </div>
        {overflows && (
          <div className="pages-warn">
            This grid is larger than its page (the margins are negative) — reduce cols, rows or
            spacing, or pick a larger page.
          </div>
        )}
        {log.map((line, i) => (
          <div key={`${i}-${line}`}>{line}</div>
        ))}
      </div>

      <div className="pages-flow">
        {pages.map((page, pageIndex) => (
          <div
            key={pageIndex}
            className="pages-sheet"
            style={pageArStyle(layout)}
          >
            <div className="pages-page-head">
              Page {pageIndex + 1} · {page.length} of {layout.capacity} slots
            </div>
            <div className="pages-page">
              {page.map((slot, i) => (
                <PagesSlot
                  key={`${slot.card.id}:${slot.copy}`}
                  card={slot.card}
                  pickerKey={`${slot.card.id}:${slot.copy}`}
                  index={i}
                  c={coverage.get(slot.card.id)!}
                  a={makeActions(kit, slot.card, addLog)}
                  painted={paintedUrls.get(slot.card.id) ?? null}
                  trimInset={trimInset}
                  style={slotStyles[i]}
                  kit={kit}
                  origin={origin}
                  serverThumbs={serverThumbs}
                  queue={queue}
                  onMenu={(x, y) =>
                    setMenu({ card: slot.card, pickerKey: `${slot.card.id}:${slot.copy}`, x, y })
                  }
                  onBackTrigger={registerBack}
                />
              ))}
            </div>
          </div>
        ))}
      </div>

      {menu && menuActions && (
        <ContextMenu
          x={menu.x}
          y={menu.y}
          items={menuFor(
            coverage.get(menu.card.id)!,
            menuActions,
            menu.card.custom_image_id != null,
            kit.serverUnavailable,
            () => openBackPicker(menu.pickerKey, menu.card),
            () => showImages(menu.card),
            true,
          )}
          onClose={() => setMenu(null)}
        />
      )}
      {imagesCard && (
        <CardImagesModal
          card={imagesCard}
          faces={kit.facesFor(imagesCard)}
          kit={kit}
          warnCpu={warnCpu}
          onClose={() => setImagesCard(null)}
        />
      )}
    </div>
  );
}

/** One printed card, sitting in the grid position the print pipeline gives it.
 *  The box is the bled card; the dashed hairline inside it is where the trim
 *  falls when bleed is on — what you see is what leaves the printer. Its name
 *  and status are not painted in place: a slot's size is the page's business,
 *  and a 9-column sheet's slot cannot carry them. Hovering floats the name above
 *  the slot and the badges below it, at a size you can read and click. Nothing
 *  grows or moves: the page's geometry is the thing being read. */
function PagesSlot(props: {
  card: CardRow;
  index: number;
  /** A 4-of is four slots; "Card back…" must open the picker of the one you
   *  clicked, so each slot registers its own trigger. */
  pickerKey: string;
  c: Coverage;
  a: ReturnType<typeof makeActions>;
  painted: string | null;
  trimInset: string | null;
  style: CSSProperties;
  kit: DeckGridKit;
  origin: ImageOrigin | null;
  serverThumbs: boolean;
  queue: ThumbnailQueue;
  onMenu: (x: number, y: number) => void;
  onBackTrigger: (key: string, button: HTMLElement | null) => void;
}) {
  const {
    card,
    index,
    pickerKey,
    c,
    a,
    painted,
    trimInset,
    style,
    kit,
    origin,
    serverThumbs,
    queue,
    onMenu,
    onBackTrigger,
  } = props;
  const quantity = card.quantity ?? 1;
  return (
    <div
      className="pages-slot"
      style={style}
      title={`${card.name}${quantity > 1 ? ` ×${quantity}` : ""} · card ${index + 1} on this page`}
      onContextMenu={(e) => {
        e.preventDefault();
        onMenu(e.clientX + 2, e.clientY + 2);
      }}
    >
      <DeckTile
        card={card}
        library={kit.library}
        origin={origin}
        serverThumbs={serverThumbs}
        queue={queue}
        hideTileTitle
        variantSrc={painted}
      />
      {trimInset && <span className="pages-trim" style={{ inset: trimInset }} />}
      <PaintedBadges c={c} kit={kit} onDownload={a.download} onUpscaleAt={a.upscaleAt} />
      {/* The menu's "Card back…" runs this control's own button, mounted here
          and hidden (CardBackPicker renders its trigger and its modal together,
          so only the button is display:none). */}
      <span
        className="proto-hidden-back"
        ref={(el) => {
          const button = el?.querySelector("button") ?? null;
          onBackTrigger(pickerKey, button);
        }}
      >
        <CardBackPicker card={card} transformOnBack={false} />
      </span>
      <span className="pages-slot-name mono">
        {card.name}
        {quantity > 1 && ` ×${quantity}`}
      </span>
    </div>
  );
}



