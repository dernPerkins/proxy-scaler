// PROTOTYPE — throwaway. Two ways to give the Decklist tab's Grid view the
// Row view's functionality — the printing picker, per-card backs, the quantity
// stepper, Show/Hide images, Download, Upscale and Remove — plus visible state
// for "this card's original is downloaded" and "it's upscaled at the selected
// model and goal DPI". Switched with ?variant=D|E and the floating bar
// ([ and ] cycle; "Current UI" is the view-only grid, kept in the cycle for
// comparison). Dev builds only. Actions are REAL: they call the page's own
// handlers, so try it in a scratch project. Delete once a variant is picked.
//
//   A, B and C (the right-click menu, the button strip, the painted status)
//   were tried and dropped: D is what their feedback produced, and it keeps
//   one piece of each — C's painted badges, A's right-click menu, B's control
//   row under the tile.
//
//   D · Merged           — Dean's cut of those three: the row's printing button IS
//                         the art selector, the back swatch and qty stepper
//                         under the tile, status painted bottom-right of the
//                         art (one badge per DPI, lowest first — the original's
//                         own DPI, then each goal DPI) where a missing badge IS
//                         the Download / Upscale button, an eye bottom-left and
//                         a trash icon top-right (both on hover), a modal for
//                         the images with the row's per-image controls (the grid
//                         never reflows), and the right-click menu without
//                         quantity. Its tile paints the variant it would
//                         print — the front face at the selected model and
//                         the highest goal DPI — when one exists, else the
//                         source art it shows today.
//   E · Merged on the page — D's cards and D's controls, arranged on the pages
//                         the PDF tab's settings describe (deck-page-layout
//                         .prototype.ts): one page per row, each drawn at its
//                         real aspect ratio under a "Page n" head, one printed
//                         card per slot (bleed included, dashed trim edge) and a
//                         4-of taking four adjacent slots. Nothing about a card
//                         changes — name, painted badges, printing picker, back
//                         swatch, stepper, eye, trash — except WHERE they can
//                         sit: a printed card has no room under itself for D's
//                         control row, so the row and the name float as chips
//                         above and below the slot on hover.
//
// The Pages-view prototype (decklist-pages.prototype.tsx) borrows this file's
// pieces rather than repeating them: the coverage math (coverageFor), the
// painted DPI badges, the right-click menu and its modal of images. Variant E
// and that view share their page geometry (deck-page-layout.prototype.ts), so
// the same settings lay out the same way in either.
import { useCallback, useEffect, useMemo, useRef, useState, useSyncExternalStore } from "react";
import type { CSSProperties } from "react";
import { generationApi } from "../api/generation";
import { faceArtKind } from "../api/project";
import type { CardFaceArt, CardRow, ProjectSettings } from "../api/project";
import type { CustomImage, ImageOrigin } from "../api/types";
import CompareDialog from "../components/CompareDialog";
import CompareMenu, { type CompareSide } from "../components/CompareMenu";
import FullImageDialog from "../components/FullImageDialog";
import { CUSTOM_SOURCE_MODEL, ORIGINAL_DPI, ORIGINAL_MODEL, modelDisplayName } from "../constants";
import {
  getProbedDevice,
  serverSupportsPrintingImages,
  subscribeProbedDevice,
  useServerVersion,
} from "../config";
import { runDownload, useDownloadStatus } from "../download";
import { fillFrameWith, trimGeometry } from "../cardTrim";
import { useImageCacheSetting } from "../imageOrigin";
import { errorText, toast } from "../toasts";
import type { VariantStatus } from "../mergeCardStatus";
import type { PendingFaceArt } from "../pickDownloads";
import CardBackPicker from "../components/CardBackPicker";
import { DeckTile } from "../components/DeckGrid";
import ModalOverlay from "../components/ModalOverlay";
import PrintingPicker from "../components/PrintingPicker";
import { EyeIcon, TrashIcon } from "../components/RowIcons";
import type { PickerOpenRequest } from "../components/PrintingPicker";
import { useThumbnailQueue, type ThumbnailQueue } from "../components/imageGrid";
import {
  paginateDeck,
  pageArStyle,
  resolvePageLayout,
  slotStyleFor,
  trimInsetFor,
} from "./deck-page-layout.prototype";
import "./decklist-grid.prototype.css";

export const DECK_GRID_VARIANTS = [
  { key: "D", name: "Merged" },
  { key: "E", name: "Merged on the page" },
] as const;

export const DECK_GRID_VARIANT_KEYS: readonly string[] = DECK_GRID_VARIANTS.map((v) => v.key);

/** One face of a card, the way the Row view computed it for the page. */
export interface ProtoFace {
  faceLabel: string | null;
  variants: VariantStatus[];
  art?: CardFaceArt;
  pick?: PendingFaceArt;
}

/** Everything a variant needs from DecklistPage: the deck, the face merge the
 *  Row view already does, the selected model + DPIs, and the row's actions. */
export interface DeckGridKit {
  cards: readonly CardRow[];
  library: ReadonlyMap<number, CustomImage>;
  facesFor: (card: CardRow) => ProtoFace[];
  model: string;
  dpis: number[];
  serverUnavailable: boolean;
  openPicker: (card: CardRow, request: PickerOpenRequest) => void;
  setQuantity: (card: CardRow, quantity: number) => void;
  download: (card: CardRow) => void;
  upscale: (card: CardRow) => void;
  /** Variant D: one painted badge upscales its one DPI, not the whole
   *  selection. Optional: the row only knows how to do all of them. */
  upscaleAt?: (card: CardRow, dpi: number) => void;
  remove: (card: CardRow) => void;
  /** Variant D's image modal: the Row view's per-image buttons, same
   *  mutations the row's thumbs use. */
  refetch: (galleryItemId: number) => void;
  regen: (galleryItemId: number) => void;
}

// "Original downloaded" is a source-tier variant: the download-only original,
// or a Custom/MPC image's own source (its "full" image IS the uploaded file).
const SOURCE_MODELS = [ORIGINAL_MODEL, CUSTOM_SOURCE_MODEL];

type Cover = "done" | "active" | "failed" | "none";

function coverOf(v: VariantStatus | undefined): Cover {
  if (!v) return "none";
  if (v.status === "done") return "done";
  if (v.status === "pending" || v.status === "running") return "active";
  if (v.status === "failed") return "failed";
  return "none";
}

export interface Coverage {
  faces: ProtoFace[];
  original: Cover;
  /** What each face's original image IS, in DPI — the badge says the number
   *  instead of "OG". A source-tier variant carries it: the server registers a
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

function faceOriginalDpi(
  f: ProtoFace,
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
  faces: ProtoFace[],
  model: string,
  dpis: number[],
  library: ReadonlyMap<number, CustomImage>,
): Coverage {
  const all = faces.flatMap((f) => f.variants);
  // A face's original is a source-tier variant. A download that has not
  // produced one yet lives in face.pick — the row shows it as "Downloading ·
  // Original". Fold that in here too, or an OG badge would stay grey (and
  // clickable, offering a second download) while one is already in flight.
  const faceOriginal = (f: ProtoFace): Cover => {
    const v = f.variants.find((v) => SOURCE_MODELS.includes(v.model));
    if (v) return coverOf(v);
    if (f.pick) return f.pick.status === "failed" ? "failed" : "active";
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

function faceLabel(label: string) {
  return label.charAt(0).toUpperCase() + label.slice(1);
}

function QtyStepper({ quantity, onSet }: { quantity: number; onSet: (q: number) => void }) {
  return (
    <span className="proto-qty">
      <button className="btn-sm" aria-label="Decrease quantity" disabled={quantity <= 1} onClick={() => onSet(quantity - 1)}>
        −
      </button>
      <span className="proto-qty-n mono">×{quantity}</span>
      <button className="btn-sm" aria-label="Increase quantity" onClick={() => onSet(quantity + 1)}>
        +
      </button>
    </span>
  );
}

// One card's actions, already logged by the grid.
export interface TileActions {
  quantity: number;
  download: () => void;
  upscale: () => void;
  upscaleAt: (dpi: number) => void;
  remove: () => void;
  setQuantity: (q: number) => void;
  openPrinting: () => void;
  /** Variant D: the row's own PrintingPicker opens the walk with whatever
   *  request it builds (it knows the pinned printing and MPC state). */
  openPrintingWith: (request: PickerOpenRequest) => void;
  openArt: () => void;
}

interface MenuAction {
  run: () => void;
  label: string;
  disabled?: boolean;
  title?: string;
  danger?: boolean;
}

export interface MenuItem {
  sep?: true;
  label?: string;
  action?: MenuAction;
}

const SEP: MenuItem = { sep: true };

/** The right-click menu D paints on the tile, and the Pages view borrows: a
 *  menu on the card, like the other tools use. Opened by right-click, closed
 *  by Escape, an outside click or picking something. */
export function ContextMenu({
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
        className="proto-ctx-backdrop"
        onContextMenu={(e) => {
          e.preventDefault();
          onClose();
        }}
        onClick={onClose}
      />
      <div ref={menuRef} className="proto-ctx mono" style={{ left: x, top: y }} role="menu">
        {items.map((item, i) =>
          item.sep ? (
            <span key={i} className="proto-ctx-sep" />
          ) : (
            <button
              key={i}
              type="button"
              role="menuitem"
              className={`proto-ctx-item${item.action?.danger ? " is-danger" : ""}`}
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

export function menuFor(
  c: Coverage,
  a: TileActions,
  custom: boolean,
  disabled: boolean,
  openBackPicker: () => void,
  openImages: () => void,
  withQuantity: boolean,
): MenuItem[] {
  const has = c.faces.some((f) => f.variants.some((v) => v.galleryItemId != null));
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
    // D and E keep quantity in the stepper under the tile, so it is not in
    // their menus; the Pages view has no stepper of its own, so it stays in
    // that menu — Dean's note on the merged cut.
    ...(withQuantity
      ? ([
          {
            label: "Quantity −1",
            action: { run: () => a.setQuantity(a.quantity - 1), label: "Quantity −1", disabled: a.quantity <= 1 },
          },
          { label: "Quantity +1", action: { run: () => a.setQuantity(a.quantity + 1), label: "Quantity +1" } },
          SEP,
        ] as MenuItem[])
      : []),
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

/** One card's actions, wrapped so the state panel records what a variant
 *  did. Every one of these calls the page's own handler — real work, not a
 *  stub. Plain function (not a hook) so the context menu can build the same
 *  actions for whichever card it is open on. */
export function makeActions(kit: DeckGridKit, card: CardRow, log: (s: string) => void): TileActions {
  const quantity = card.quantity ?? 1;
  return {
    quantity,
    download: () => {
      kit.download(card);
      log(`Download · ${card.name}`);
    },
    upscale: () => {
      kit.upscale(card);
      log(`Upscale · ${card.name} · ${kit.model} · ${kit.dpis.join("/")}`);
    },
    upscaleAt: (dpi) => {
      if (kit.upscaleAt) kit.upscaleAt(card, dpi);
      else kit.upscale(card);
      log(`Upscale · ${card.name} · ${kit.model} · ${dpi} DPI (badge)`);
    },
    remove: () => {
      kit.remove(card);
      log(`Remove · ${card.name}`);
    },
    setQuantity: (q: number) => {
      const n = Math.max(1, Math.round(q));
      kit.setQuantity(card, n);
      log(`Quantity · ${card.name} · ×${n}`);
    },
    openPrinting: () => {
      kit.openPicker(card, { source: "scryfall", face: 0 });
      log(`Open printing picker · ${card.name}`);
    },
    openArt: () => {
      kit.openPicker(card, { source: "mpc", face: 0 });
      log(`Open MPC Fill art · ${card.name}`);
    },
    openPrintingWith: (request) => {
      kit.openPicker(card, request);
      log(`Open printing picker · ${card.name} · ${request.source}`);
    },
  };
}

/** D's painted status, bottom-right of the art (that corner is the
 *  least interesting of a card — often just trademark text). E and the Pages
 *  view reuse this component unchanged. A badge that is
 *  grey — nothing exists for that DPI at the selected model — IS its button:
 *  the original's badge downloads, a DPI badge upscales just that DPI. Done
 *  badges are marks.
 *
 *  The original is labelled with the DPI its file actually is rather than
 *  "OG", because it IS one of these badges: the server records a printing's
 *  own Scryfall original at 300 DPI and an MPC Fill / Custom Image original at
 *  that file's measured DPI. Sorted low to high, the row reads as the chain it
 *  describes — the source, then the upscales. */
export function PaintedBadges({
  c,
  kit,
  onDownload,
  onUpscaleAt,
}: {
  c: Coverage;
  kit: DeckGridKit;
  onDownload: () => void;
  onUpscaleAt: (dpi: number) => void;
}) {
  const name = modelDisplayName(kit.model);
  // One card can print different sources per face (a Scryfall front, an MPC
  // back), so the badge shows the LOWEST of them — the DPI the card is
  // genuinely printable at from its originals — and the title lists them all.
  const faceDpis = [...new Set(c.originalDpis.filter((d): d is number => d != null))].sort(
    (a, b) => a - b,
  );
  const ogDpi = faceDpis[0] ?? null;
  const badge = (cover: Cover, key: string, label: string, title: string, act?: () => void) => {
    const actionable = act != null && (cover === "none" || cover === "failed") && !kit.serverUnavailable;
    const cls = `proto-badge is-${cover}${actionable ? " is-act" : ""}`;
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
  return <span className="proto-painted mono">{badges.map((b) => b.el)}</span>;
}

/** The row's bleed-aware thumbnail (DecklistPage.tsx::ThumbImage), same frame. */
function ProtoThumb({ src, alt, bleedMm }: { src: string; alt: string; bleedMm: number }) {
  if (bleedMm <= 0) return <img src={src} alt={alt} loading="lazy" />;
  return (
    <div className="thumb-frame">
      <img src={src} alt={alt} loading="lazy" style={fillFrameWith(trimGeometry(null, bleedMm).trim)} />
    </div>
  );
}

function protoSlug(name: string): string {
  return name.replace(/[^A-Za-z0-9._-]+/g, "_").replace(/^_+|_+$/g, "") || "card";
}

/** Variant D's Show/Hide replacement: every version of the card in a modal, so
 *  the grid never reflows. Each image carries the row's own per-image controls
 *  on the row's own markup — an Original gets Download / View / Re-Fetch, an
 *  upscale gets Download / View / Compare (with the pick-any-other caret) /
 *  Regen — because that is what each kind of image can actually do.
 *
 *  One deliberate addition: the row has no View on an upscale, its thumb is
 *  already on screen at arm's length. In a modal of them, opening one
 *  full-size is the reason to be here. */
export function CardImagesModal({
  card,
  faces,
  kit,
  warnCpu,
  onClose,
}: {
  card: CardRow;
  faces: ProtoFace[];
  kit: DeckGridKit;
  warnCpu: boolean;
  onClose: () => void;
}) {
  const downloadStatus = useDownloadStatus();
  const [viewTarget, setViewTarget] = useState<{
    title: string;
    src: string;
    setCode?: string | null;
    bleedMm: number;
  } | null>(null);
  const [compare, setCompare] = useState<{
    mine: CompareSide;
    against: CompareSide;
    options: CompareSide[];
  } | null>(null);

  // Escape closes the topmost thing open. FullImageDialog handles its own.
  useEffect(() => {
    if (viewTarget) return;
    const onKey = (e: KeyboardEvent) => {
      if (e.key !== "Escape") return;
      if (compare) setCompare(null);
      else onClose();
    };
    window.addEventListener("keydown", onKey);
    return () => window.removeEventListener("keydown", onKey);
  }, [compare, viewTarget, onClose]);

  const saveFile = async (url: string, filename: string) => {
    try {
      await runDownload(filename, { url });
    } catch (err) {
      toast(`Download failed: ${errorText(err)}`, "err");
    }
  };

  return (
    <ModalOverlay onClick={onClose}>
      <div className="modal proto-card-images" onClick={(e) => e.stopPropagation()}>
        <div className="modal-head">
          <span className="modal-title">
            {card.name} — every version
            <span className="proto-card-images-note mono">
              {kit.serverUnavailable ? "generation server unreachable" : "the row view's own controls"}
            </span>
          </span>
          <button className="btn-sm" onClick={onClose}>
            Close
          </button>
        </div>
        <div className="modal-body proto-card-images-body">
          {faces.map((face, i) => {
            const done = face.variants.filter(
              (v): v is VariantStatus & { galleryItemId: number } => v.galleryItemId != null,
            );
            // Same split the row makes: the source-tier variant IS the
            // original, so it gets no Compare / Regen (it would compare
            // against itself), and the upscales below are the comparable ones.
            const source = done.find((v) => SOURCE_MODELS.includes(v.model)) ?? done[0];
            const ups = done.filter((v) => !SOURCE_MODELS.includes(v.model));
            const artImage = kit.library.get(
              (face.art ? face.art.custom_image_id : card.custom_image_id) ?? -1,
            );
            const bleedMm = artImage?.includes_bleed ? artImage.bleed_mm : 0;
            const faceName = `${card.name}${face.faceLabel ? ` (${faceLabel(face.faceLabel)})` : ""}`;
            const faceDpi = faceOriginalDpi(face, card, kit.library);
            const sides: CompareSide[] = [
              ...(source
                ? [
                    {
                      key: "original",
                      label: "Original",
                      url: generationApi.imageUrl(source.galleryItemId, "original"),
                    },
                  ]
                : []),
              ...ups.map((v) => ({
                key: `${v.dpi}:${v.model}`,
                label: `${v.dpi} DPI · ${modelDisplayName(v.model)}`,
                url: generationApi.imageUrl(v.galleryItemId, "full"),
              })),
            ];
            const openCompare = (mineKey: string, againstKey?: string) => {
              const mine = sides.find((s) => s.key === mineKey);
              if (!mine) return;
              const options = sides.filter((s) => s.key !== mineKey);
              const against = options.find((s) => s.key === againstKey) ?? options[0];
              if (!against) return;
              setCompare({ mine, against, options });
            };
            return (
              <section key={i} className="proto-images-face">
                {face.faceLabel && (
                  <div className="proto-versions-label">{faceLabel(face.faceLabel)}</div>
                )}
                <div className="thumbs">
                  {source && (
                    <div className="thumb">
                      <div className="thumb-label">
                        Original{faceDpi != null && <span className="mono"> · {faceDpi} DPI</span>}
                        {source.status !== "done" && (
                          <span className="thumb-regen-note">
                            {" "}
                            · {source.status === "running" ? "downloading…" : "queued"}
                          </span>
                        )}
                      </div>
                      <ProtoThumb
                        src={generationApi.imageUrl(source.galleryItemId, "original")}
                        alt={card.name}
                        bleedMm={bleedMm}
                      />
                      <div className="thumb-buttons">
                        <button
                          className="btn-sm"
                          disabled={downloadStatus != null}
                          onClick={() =>
                            void saveFile(
                              generationApi.imageUrl(source.galleryItemId, "original"),
                              `${protoSlug(card.name)}-original.png`,
                            )
                          }
                        >
                          {downloadStatus ? "Downloading…" : "Download"}
                        </button>
                        <button
                          className="btn-sm"
                          onClick={() =>
                            setViewTarget({
                              title: `${faceName} — Original`,
                              src: generationApi.imageUrl(source.galleryItemId, "original"),
                              setCode: face.art?.scryfall?.set_code ?? card.set_code,
                              bleedMm,
                            })
                          }
                        >
                          View
                        </button>
                        {/* Nothing to re-fetch from for a Custom Image or MPC
                            Fill art: its source is the file the user owns. */}
                        {face.art?.scryfall || (!face.art && card.custom_image_id == null) ? (
                          <button
                            className="btn-sm"
                            onClick={() => kit.refetch(source.galleryItemId)}
                            disabled={kit.serverUnavailable}
                            title="Re-download the original from Scryfall (overwrites the cached copy)"
                          >
                            Re-Fetch
                          </button>
                        ) : null}
                      </div>
                    </div>
                  )}
                  {ups.map((v) => (
                    <div key={`${v.dpi}-${v.model}`} className="thumb">
                      <div className="thumb-label" title={`${v.dpi} DPI · ${v.model}`}>
                        {v.dpi} DPI · {modelDisplayName(v.model)}
                        {v.status !== "done" && (
                          <span className="thumb-regen-note">
                            {" "}
                            · {v.status === "running" ? "regenerating…" : "queued"}
                          </span>
                        )}
                        {warnCpu && v.device === "cpu" && (
                          <span
                            className="thumb-cpu-note"
                            title="Generated on the CPU — the GPU ran out of memory during this generation. Regen to retry on the GPU."
                          >
                            {" "}
                            · CPU
                          </span>
                        )}
                      </div>
                      <ProtoThumb
                        src={generationApi.imageUrl(v.galleryItemId, "thumb")}
                        alt={card.name}
                        bleedMm={bleedMm}
                      />
                      <div className="thumb-buttons">
                        <button
                          className="btn-sm"
                          disabled={downloadStatus != null}
                          onClick={() =>
                            void saveFile(
                              generationApi.imageUrl(v.galleryItemId, "full"),
                              `${protoSlug(card.name)}-${v.dpi}dpi-${v.model}.png`,
                            )
                          }
                        >
                          {downloadStatus ? "Downloading…" : "Download"}
                        </button>
                        <button
                          className="btn-sm"
                          onClick={() =>
                            setViewTarget({
                              title: `${faceName} — ${v.dpi} DPI · ${modelDisplayName(v.model)}`,
                              src: generationApi.imageUrl(v.galleryItemId, "full"),
                              setCode: face.art?.scryfall?.set_code ?? card.set_code,
                              bleedMm,
                            })
                          }
                        >
                          View
                        </button>
                        <span className="compare-split">
                          <button className="btn-sm" onClick={() => openCompare(`${v.dpi}:${v.model}`)}>
                            Compare
                          </button>
                          {sides.length > 2 && (
                            <CompareMenu
                              options={sides.filter((s) => s.key !== `${v.dpi}:${v.model}`)}
                              onPick={(side) => openCompare(`${v.dpi}:${v.model}`, side.key)}
                            />
                          )}
                        </span>
                        <button
                          className="btn-sm"
                          onClick={() => kit.regen(v.galleryItemId)}
                          disabled={kit.serverUnavailable || v.status !== "done"}
                          title={
                            v.status !== "done"
                              ? "A regeneration for this variant is already queued"
                              : undefined
                          }
                        >
                          Regen
                        </button>
                      </div>
                    </div>
                  ))}
                  {!source && <span className="variant-note">Nothing generated for this face yet.</span>}
                </div>
              </section>
            );
          })}
          {faces.length === 0 && <p className="empty-note">Nothing generated for this card yet.</p>}
        </div>
      </div>
      {compare && (
        <CompareDialog
          cardName={card.name}
          mine={compare.mine}
          against={compare.against}
          options={compare.options}
          onChangeAgainst={(side) => setCompare((t) => (t ? { ...t, against: side } : t))}
          onClose={() => setCompare(null)}
        />
      )}
      {viewTarget && (
        <FullImageDialog
          title={viewTarget.title}
          src={viewTarget.src}
          setCode={viewTarget.setCode}
          bleedMm={viewTarget.bleedMm}
          onClose={() => setViewTarget(null)}
        />
      )}
    </ModalOverlay>
  );
}

function GridCell(props: {
  card: CardRow;
  c: Coverage;
  a: TileActions;
  variant: string;
  kit: DeckGridKit;
  origin: ImageOrigin | null;
  serverThumbs: boolean;
  queue: ThumbnailQueue;
  /** Variant E: where this printed card sits on its page. Variant D
   *  is a cell in a plain flex grid and has no position of its own. */
  style?: CSSProperties;
  /** Variant E: the trim edge inside the printed box, as an inset. Null when
   *  there is no bleed, because then the printed box IS the trim box. */
  trimInset?: string | null;
  onShowImages: () => void;
  onMenu: (x: number, y: number) => void;
  onBackTrigger: (cardId: number, button: HTMLElement | null) => void;
}) {
  const {
    card,
    c,
    a,
    variant,
    kit,
    origin,
    serverThumbs,
    queue,
    style,
    trimInset,
    onShowImages,
    onMenu,
    onBackTrigger,
  } = props;
  // D and E are the same card: D arranges it in a flex grid, E arranges it on a
  // printed page. Everything in this cell is D's presentation, and E keeps it
  // unchanged; `variant` only picks the parts E draws differently.
  const custom = card.custom_image_id != null;
  const backPicker = <CardBackPicker card={card} transformOnBack={false} />;
  const hasImages = c.faces.some((f) => f.variants.some((v) => v.galleryItemId != null));
  // Variants D and E paint the image it would actually print: the FRONT face's
  // variant at the selected model and the highest selected DPI, through the
  // gallery thumb endpoint (640px WebP) like every other upscale thumb. When
  // that badge is grey — nothing generated at that pair yet — the tile keeps
  // the source art it shows today. DeckTile queues it like any other tile
  // image, so a deck still loads thumbnails as it scrolls.
  const goalDpi = kit.dpis.length > 0 ? Math.max(...kit.dpis) : null;
  const painted =
    goalDpi != null
      ? c.faces[0]?.variants.find(
          (v) => v.dpi === goalDpi && v.model === kit.model && v.status === "done" && v.galleryItemId != null,
        )
      : undefined;
  const paintedUrl =
    painted?.galleryItemId != null ? generationApi.imageUrl(painted.galleryItemId, "thumb") : null;
  const tile = (
    <DeckTile
      card={card}
      library={kit.library}
      origin={origin}
      serverThumbs={serverThumbs}
      queue={queue}
      // Both variants paint the card's identity onto the tile itself (name,
      // count, and the printing and the state), so the hover tooltip that
      // repeats it is noise.
      hideTileTitle
      variantSrc={paintedUrl}
    />
  );
  return (
    <div
      className="proto-cell"
      style={style}
      onContextMenu={(e) => {
        e.preventDefault();
        onMenu(e.clientX + 2, e.clientY + 2);
      }}
    >
      <span className="proto-tile-wrap">
        {tile}
        {/* Variant E only: the trim edge inside the printed box. Outside this
            hairline is the part that gets cut off. A hairline, not a Card
            Guide — like the Pages view, this variant draws no guides. */}
        {trimInset != null && <span className="proto-E-trim" style={{ inset: trimInset }} />}
        {/* Everything painted ON the art lives in a box that mirrors the art
            frame's own box — same width, same 488/680 aspect — so the offsets
            below are margins off the card, not off the name row under it. */}
        <span className="proto-tile-ov">
          {/* The status IS the button here: a grey badge runs its job when
              clicked (the original's downloads, a goal DPI's upscales that
              DPI). Bottom-right: the least interesting corner of a card. */}
          <PaintedBadges c={c} kit={kit} onDownload={a.download} onUpscaleAt={a.upscaleAt} />
          {/* The images live in a modal, so the grid never reflows; the
              right-click menu opens it too, this icon is the shortcut. */}
          <button
            type="button"
            className="proto-pill proto-pill-images proto-reveal"
            onClick={onShowImages}
            disabled={!hasImages}
            title={
              hasImages
                ? "Show images — every version of this card, in a modal"
                : "Nothing to show yet — download or generate this card first"
            }
            aria-label="Show images"
          >
            <EyeIcon />
          </button>
          <button
            type="button"
            className="proto-pill proto-pill-remove proto-reveal"
            onClick={a.remove}
            title="Remove this card from the deck"
            aria-label="Remove"
          >
            <TrashIcon />
          </button>
        </span>
      </span>

      <span className={variant === "E" ? "proto-controls proto-E-controls" : "proto-controls"}>
        {/* The printing button, same one the row uses: its label is the
            card's set · number · language. */}
        {custom ? (
          <span className="chip" title="Your own uploaded art — manage it on the Customs tab">
            Custom
          </span>
        ) : (
          <PrintingPicker
            card={card}
            rarity={card.rarity ?? undefined}
            disabled={kit.serverUnavailable}
            onOpen={a.openPrintingWith}
          />
        )}
        {/* Mounted visibly, and registered so the right-click menu's
            "Card back…" can open this same control. */}
        <span
          ref={(el) =>
            onBackTrigger(card.id, el?.querySelector("button") ?? null)
          }
        >
          {backPicker}
        </span>
        <QtyStepper quantity={a.quantity} onSet={a.setQuantity} />
      </span>
    </div>
  );
}

export default function DeckGridPrototype({
  variant,
  kit,
  settings,
}: {
  variant: string;
  kit: DeckGridKit;
  /** Variant E lays the deck out on the pages these PDF settings print. */
  settings: ProjectSettings;
}) {
  const serverVersion = useServerVersion();
  const serverThumbs = serverSupportsPrintingImages(serverVersion);
  const { origin } = useImageCacheSetting();
  // Same rule the row uses for its "· CPU" note: only worth saying on a
  // machine that has a GPU and fell back off it.
  const warnCpu = useSyncExternalStore(subscribeProbedDevice, getProbedDevice)?.kind === "gpu";
  // Same as DeckGrid: the decklist scrolls with the window, so the queue's
  // root stays null (the viewport).
  const scrollRef = useRef<HTMLElement | null>(null);
  const queue = useThumbnailQueue(scrollRef);
  const [images, setImages] = useState<CardRow | null>(null);
  const [menu, setMenu] = useState<{ card: CardRow; x: number; y: number } | null>(null);
  const [log, setLog] = useState<string[]>([]);

  const addLog = useCallback((s: string) => {
    setLog((prev) => [`${new Date().toLocaleTimeString()} · ${s}`, ...prev].slice(0, 8));
  }, []);

  const coverage = useMemo(
    () =>
      new Map(
        kit.cards.map((card) => [
          card.id,
          coverageFor(card, kit.facesFor(card), kit.model, kit.dpis, kit.library),
        ]),
      ),
    [kit],
  );

  // Variant E: the pages the PDF tab's settings print as they stand, and this
  // deck spread across them — one slot per printed copy, in the deck's Sort
  // order, so a 4-of takes four adjacent slots and can straddle a page break.
  const layout = useMemo(() => resolvePageLayout(settings), [settings]);
  const pages = useMemo(() => paginateDeck(kit.cards, layout.capacity), [kit.cards, layout]);
  const trimInset = trimInsetFor(layout);
  // A grid larger than its page shows up as negative margins, which is also
  // what the server clips. Say so instead of drawing it quietly.
  const overflows = layout.marginX < 0 || layout.marginY < 0;

  // The menu's "Card back…" opens the row's own picker through the trigger each
  // tile registers from the mount in its control row.
  const openBackPicker = (card: CardRow) => {
    addLog(`Open card back picker · ${card.name}`);
    backBtns.current.get(card.id)?.click();
  };

  const menuActions = menu ? makeActions(kit, menu.card, addLog) : null;
  const backBtns = useRef(new Map<number, HTMLElement>());
  const registerBack = useCallback((cardId: number, button: HTMLElement | null) => {
    if (button) backBtns.current.set(cardId, button);
    else backBtns.current.delete(cardId);
  }, []);
  // D and E show a card's images in a modal over the grid, never inline, so no
  // tile ever changes size while you read one card's versions.
  const showImages = useCallback(
    (card: CardRow) => {
      setImages(card);
      addLog(`Open images modal · ${card.name}`);
    },
    [addLog],
  );

  const countDone = (pred: (c: Coverage) => boolean) => [...coverage.values()].filter(pred).length;
  const imagesCard = images ? (kit.cards.find((c) => c.id === images.id) ?? images) : null;
  const mm = (v: number) => `${v.toFixed(1)}`;

  // One card, drawn identically by both variants: only its placement differs. D
  // hands it to the flex grid and has no position or trim edge of its own; E
  // places it in a page slot and shows where the cut lands.
  const cellFor = (slot: { card: CardRow; copy: number }, style?: CSSProperties) => (
    <GridCell
      key={`${slot.card.id}:${slot.copy}`}
      card={slot.card}
      c={coverage.get(slot.card.id)!}
      a={makeActions(kit, slot.card, addLog)}
      variant={variant}
      kit={kit}
      origin={origin}
      serverThumbs={serverThumbs}
      queue={queue}
      style={style}
      trimInset={variant === "E" ? trimInset : undefined}
      onShowImages={() => showImages(slot.card)}
      onMenu={(x, y) => setMenu({ card: slot.card, x, y })}
      onBackTrigger={registerBack}
    />
  );

  return (
    <div className="proto-grid-wrap">
      {variant === "E" ? (
        // `proto-E` is what every rule for this variant's card is scoped to: the
        // cards themselves are shared with D, the page around them is not.
        <div className="proto-E proto-E-flow">
          {pages.map((page, pageIndex) => (
            <div key={pageIndex} className="proto-E-sheet" style={pageArStyle(layout)}>
              <div className="proto-E-head">
                Page {pageIndex + 1} · {page.length} of {layout.capacity} slots ·{" "}
                {mm(layout.bledW)}×{mm(layout.bledH)} mm cards
              </div>
              <div className="proto-E-page">
                {page.map((slot, i) => cellFor(slot, slotStyleFor(layout, i)))}
              </div>
            </div>
          ))}
        </div>
      ) : (
        <div className={`deck-grid proto-${variant}`}>
          {kit.cards.map((card) => cellFor({ card, copy: 0 }))}
        </div>
      )}

      {menu && menuActions && (
        <ContextMenu
          x={menu.x}
          y={menu.y}
          items={menuFor(
            coverage.get(menu.card.id)!,
            menuActions,
            menu.card.custom_image_id != null,
            kit.serverUnavailable,
            () => openBackPicker(menu.card),
            () => showImages(menu.card),
            // Both variants carry quantity in the stepper under the tile, so
            // their menus have no Quantity items.
            false,
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
          onClose={() => setImages(null)}
        />
      )}

      {/* Rule 5 of the prototype skill: surface the state. Counts are across
          the whole deck, so a variant's effect is readable at a glance. */}
      <div className="proto-state mono">
        <div className="proto-state-head">
          Prototype state · variant {variant} · {kit.cards.length} cards · {modelDisplayName(kit.model)} ·{" "}
          {kit.dpis.join(" / ")} DPI
          {kit.serverUnavailable && " · generation server unreachable"}
        </div>
        <div className="proto-state-row">
          <span title="Cards whose original (source-tier variant) is downloaded">
            original {countDone((c) => c.original === "done")}/{kit.cards.length}
          </span>
          {kit.dpis.map((d) => (
            <span key={d} title={`Cards done at ${d} DPI with the selected model`}>
              {d} DPI {countDone((c) => c.targets.some((t) => t.dpi === d && t.cover === "done"))}/
              {kit.cards.length}
            </span>
          ))}
          <span>
            failed{" "}
            {countDone((c) => c.original === "failed" || c.targets.some((t) => t.cover === "failed"))}
          </span>
          <span>images modal {imagesCard ? "open" : "closed"}</span>
        </div>
        {variant === "E" && (
          <div className="proto-state-row">
            <span title="The page this view draws, from the PDF tab">
              page {mm(layout.pageW)}×{mm(layout.pageH)} mm
            </span>
            <span title="One printed card: the trim box plus the selected bleed">
              card {mm(layout.bledW)}×{mm(layout.bledH)} mm
            </span>
            <span title="Gap between printed cards">
              gap {mm(settings.spacing_x_mm)} / {mm(settings.spacing_y_mm)} mm
            </span>
            <span title="Where the first card lands, from the page's left and top edge">
              margin {mm(layout.marginX)} / {mm(layout.marginY)} mm
            </span>
            <span title="Printed cards per page, and how many pages this deck needs">
              {layout.cols}×{layout.rows} · {pages.length} {(pages.length === 1 ? "page" : "pages")}
            </span>
          </div>
        )}
        {variant === "E" && overflows && (
          <div className="proto-E-warn">
            This grid is larger than its page (the margins are negative) — reduce cols, rows or
            spacing, or pick a larger page.
          </div>
        )}
        {log.map((line, i) => (
          <div key={`${i}-${line}`}>{line}</div>
        ))}
      </div>
    </div>
  );
}
