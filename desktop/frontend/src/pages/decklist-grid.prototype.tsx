// PROTOTYPE — throwaway. Three ways to give the Decklist tab's Grid view the
// Row view's functionality — the printing picker, per-card backs, the quantity
// stepper, Show/Hide images, Download, Upscale and Remove — plus visible state
// for "this card's original is downloaded" and "it's upscaled at the selected
// model and goal DPI". Switched with ?variant=A|B|C and the floating bar
// ([ and ] cycle; "Current UI" is the view-only grid, kept in the cycle for
// comparison). Dev builds only. Actions are REAL: they call the page's own
// handlers, so try it in a scratch project. Delete once a variant is picked.
//
//   A · Right-Click Menu — tiles stay exactly as they are (art, name, count)
//                         with a state pip under them; right-click any card for
//                         everything, the way the other tools do.
//   B · Button Strip     — as many controls as fit, always visible under each
//                         tile: printing / back / qty, the row's four icon
//                         buttons, and its status badges.
//   C · Painted Status   — state ON the art (the original, plus every goal DPI
//                         at the selected model), qty and back on the tile, the
//                         rest revealed on hover or keyboard focus.
//   D · Merged           — Dean's cut of A+B+C: the row's printing button IS
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
//
// The Pages-view prototype (decklist-pages.prototype.tsx) borrows this file's
// pieces rather than repeating them: the coverage math (coverageFor), the
// painted DPI badges, the right-click menu and its modal of images.
import { useCallback, useEffect, useMemo, useRef, useState, useSyncExternalStore } from "react";
import { generationApi } from "../api/generation";
import { faceArtKind } from "../api/project";
import type { CardFaceArt, CardRow } from "../api/project";
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
import StatusBadge from "../components/StatusBadge";
import { DownloadIcon, EyeIcon, EyeOffIcon, SparklesIcon, TrashIcon } from "../components/RowIcons";
import type { PickerOpenRequest } from "../components/PrintingPicker";
import { useThumbnailQueue, type ThumbnailQueue } from "../components/imageGrid";
import "./decklist-grid.prototype.css";

export const DECK_GRID_VARIANTS = [
  { key: "A", name: "Right-click menu" },
  { key: "B", name: "Button strip" },
  { key: "C", name: "Painted status" },
  { key: "D", name: "Merged" },
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

const COVER_GLYPH: Record<Cover, string> = { done: "✓", active: "…", failed: "✗", none: "—" };

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

function modelLabel(model: string, art?: CardFaceArt): string {
  if (art && model === CUSTOM_SOURCE_MODEL) return art.mpc_drive_id ? "MPC" : "Custom";
  return modelDisplayName(model);
}

function faceLabel(label: string) {
  return label.charAt(0).toUpperCase() + label.slice(1);
}

/** The state, as three dots: the original, then one per goal DPI at the
 *  selected model. Variant A's only always-on state. */
function StatusPips({ c, model }: { c: Coverage; model: string }) {
  const name = modelDisplayName(model);
  return (
    <span className="proto-pips mono" aria-label="Card state">
      <span className={`proto-pip is-${c.original}`} title={`Original: ${c.original}`}>
        {COVER_GLYPH[c.original]}
      </span>
      {c.targets.map((t) => (
        <span
          key={t.dpi}
          className={`proto-pip is-${t.cover}`}
          title={`${t.dpi} DPI · ${name}: ${t.cover}`}
        >
          {t.dpi}
        </span>
      ))}
      {c.downloading && <span className="proto-pip is-active" title="Art downloading">↓</span>}
    </span>
  );
}

/** The Row view's per-variant badges, unchanged. */
function FaceBadges({ faces }: { faces: ProtoFace[] }) {
  if (faces.length === 0) return <span className="variant-note">Not Generated Yet</span>;
  return (
    <>
      {faces.map((face, i) => (
        <span key={i} className="proto-face-badges">
          {face.faceLabel && <span className="variant-face">{faceLabel(face.faceLabel)}</span>}
          {face.pick && (
            <StatusBadge
              status={
                face.pick.status === "failed"
                  ? "failed"
                  : face.pick.status === "downloading"
                    ? "running"
                    : "pending"
              }
              title={face.pick.error ?? face.pick.name}
            >
              {face.pick.status === "failed"
                ? "Download failed"
                : face.pick.status === "downloading"
                  ? "Downloading"
                  : "Queued"}{" "}
              · {face.pick.source === "scryfall" ? "Original" : "MPC"}
            </StatusBadge>
          )}
          {!face.pick && face.variants.length === 0 && (
            <span className="variant-note">
              {face.art && !face.art.scryfall ? "Not Downloaded Yet" : "Not Generated Yet"}
            </span>
          )}
          {face.variants.map((v) => (
            <StatusBadge key={`${v.dpi}-${v.model}`} status={v.status}>
              {v.dpi} · {modelLabel(v.model, face.art)}
            </StatusBadge>
          ))}
        </span>
      ))}
    </>
  );
}

/** Show/Hide in the grid: the face's versions, in a strip under the tile. */
function VersionsStrip({ faces }: { faces: ProtoFace[] }) {
  return (
    <div className="proto-versions">
      {faces.map((face, i) => {
        const done = face.variants.filter((v) => v.galleryItemId != null);
        const source = done.find((v) => SOURCE_MODELS.includes(v.model)) ?? done[0];
        const ups = done.filter((v) => !SOURCE_MODELS.includes(v.model));
        return (
          <div key={i} className="proto-versions-face">
            {face.faceLabel && (
              <span className="proto-versions-label">{faceLabel(face.faceLabel)}</span>
            )}
            {source && (
              <figure className="proto-version" title="Original">
                <img src={generationApi.imageUrl(source.galleryItemId!, "original")} alt="" />
                <figcaption className="mono">Original</figcaption>
              </figure>
            )}
            {ups.map((v) => (
              <figure
                key={`${v.dpi}-${v.model}`}
                className="proto-version"
                title={`${v.dpi} DPI · ${modelDisplayName(v.model)}`}
              >
                <img src={generationApi.imageUrl(v.galleryItemId!, "thumb")} alt="" />
                <figcaption className="mono">
                  {v.dpi} · {modelDisplayName(v.model)}
                </figcaption>
              </figure>
            ))}
            {!source && <span className="variant-note">Nothing generated yet</span>}
          </div>
        );
      })}
    </div>
  );
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
  expanded: boolean;
  hasImages: boolean;
  custom: boolean;
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
  /** B and C only: the inline strip that D replaces with a modal. */
  toggleImages: () => void;
}

/** The Row view's four icon buttons. */
function ActionButtons({ a, disabled }: { a: TileActions; disabled: boolean }) {
  return (
    <span className="proto-actions">
      <button
        className="btn-sm icon-button"
        onClick={a.toggleImages}
        disabled={!a.hasImages}
        title={
          a.hasImages
            ? a.expanded
              ? "Hide images"
              : "Show images"
            : "Nothing to show yet — download or generate this card first"
        }
        aria-label={a.expanded && a.hasImages ? "Hide images" : "Show images"}
      >
        {a.expanded && a.hasImages ? <EyeOffIcon /> : <EyeIcon />}
      </button>
      <button
        className="btn-sm icon-button"
        onClick={a.download}
        disabled={disabled}
        title="Download — the full-size image, without upscaling"
        aria-label="Download"
      >
        <DownloadIcon />
      </button>
      <button
        className="btn-sm icon-button"
        onClick={a.upscale}
        disabled={disabled}
        title="Upscale — this card's art to the selected DPIs"
        aria-label="Upscale"
      >
        <SparklesIcon />
      </button>
      <button
        className="btn-sm btn-danger icon-button"
        onClick={a.remove}
        title="Remove from the deck"
        aria-label="Remove"
      >
        <TrashIcon />
      </button>
    </span>
  );
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

/** Variant A's answer to "where do the controls live": a menu on the card,
 *  like the other tools use. Opened by right-click, closed by Escape, an
 *  outside click or picking something. */
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
    // Variant A has no stepper of its own, so quantity stays in its menu; D
    // drops it (its stepper sits on the tile) — Dean's note on the merged cut.
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
    expanded: false,
    hasImages: false,
    custom: card.custom_image_id != null,
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
    toggleImages: () => log(`Show images · ${card.name}`),
  };
}

// The row's four icon buttons never sit disabled in the prototype: the grid
// has no row-level "has anything generated" check, so the strip always shows
// them and the state panel says what actually happened.
const ALWAYS_ON = { hasImages: true };

/** Variant D's painted status, bottom-right of the art (that corner is the
 *  least interesting of a card — often just trademark text). A badge that is
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
  expanded: boolean;
  onToggle: () => void;
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
    expanded,
    onToggle,
    onShowImages,
    onMenu,
    onBackTrigger,
  } = props;
  const custom = card.custom_image_id != null;
  const backPicker = <CardBackPicker card={card} transformOnBack={false} />;
  const hasImages = c.faces.some((f) => f.variants.some((v) => v.galleryItemId != null));
  // Variant D paints the image it would actually print: the FRONT face's
  // variant at the selected model and the highest selected DPI, through the
  // gallery thumb endpoint (640px WebP) like every other upscale thumb. When
  // that badge is grey — nothing generated at that pair yet — the tile keeps
  // the source art it shows today. DeckTile queues it like any other tile
  // image, so a deck still loads thumbnails as it scrolls.
  const goalDpi = kit.dpis.length > 0 ? Math.max(...kit.dpis) : null;
  const painted =
    variant === "D" && goalDpi != null
      ? c.faces[0]?.variants.find(
          (v) => v.dpi === goalDpi && v.model === kit.model && v.status === "done" && v.galleryItemId != null,
        )
      : undefined;
  const toggleImages = () => {
    onToggle();
    a.toggleImages();
  };
  const paintedUrl =
    painted?.galleryItemId != null ? generationApi.imageUrl(painted.galleryItemId, "thumb") : null;
  const tile = (
    <DeckTile
      card={card}
      library={kit.library}
      origin={origin}
      serverThumbs={serverThumbs}
      queue={queue}
      // Every variant paints the card's identity onto the tile itself (name,
      // count, and in D the printing and the state), so the hover tooltip that
      // repeats it is noise.
      hideTileTitle
      variantSrc={paintedUrl}
    />
  );
  return (
    <div
      className="proto-cell"
      onContextMenu={
        variant === "A" || variant === "D"
          ? (e) => {
              e.preventDefault();
              onMenu(e.clientX + 2, e.clientY + 2);
            }
          : undefined
      }
    >
      {variant === "D" ? (
        <span className="proto-tile-wrap">
          {tile}
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
      ) : (
        tile
      )}

      {variant === "A" && (
        <>
          <StatusPips c={c} model={kit.model} />
          {/* The back control is the row's own component; variant A keeps it
              mounted but trigger-less and opens it from the menu. */}
          <span
            className="proto-hidden-back"
            ref={(el) => {
              const button = el?.querySelector("button") ?? null;
              if (button) onBackTrigger(card.id, button);
              else onBackTrigger(card.id, null);
            }}
          >
            {backPicker}
          </span>
        </>
      )}

      {variant === "B" && (
        <>
          <span className="proto-controls">
            <button
              className="btn-sm mono"
              disabled={custom || kit.serverUnavailable}
              onClick={a.openPrinting}
              title={custom ? "A Custom Image has no printing to change" : "Change printing"}
            >
              {card.set_code ? card.set_code.toUpperCase() : "Printing"}
            </button>
            <button className="btn-sm" onClick={a.openArt} title="Print from MPC Fill art">
              Art
            </button>
            {backPicker}
            <QtyStepper quantity={a.quantity} onSet={a.setQuantity} />
            <ActionButtons
              a={{ ...a, ...ALWAYS_ON, toggleImages }}
              disabled={kit.serverUnavailable}
            />
          </span>
          <FaceBadges faces={c.faces} />
          {expanded && <VersionsStrip faces={c.faces} />}
        </>
      )}

      {variant === "C" && (
        <>
          <span className="proto-painted mono">
            <span className={`proto-badge is-${c.original}`} title="Original — the full-size download">
              orig
            </span>
            {c.targets.map((t) => (
              <span
                key={t.dpi}
                className={`proto-badge is-${t.cover}`}
                title={`${t.dpi} DPI · ${modelDisplayName(kit.model)}`}
              >
                {t.dpi}
              </span>
            ))}
          </span>
          <span className="proto-controls proto-reveal">
            <QtyStepper quantity={a.quantity} onSet={a.setQuantity} />
            <ActionButtons a={{ ...a, ...ALWAYS_ON, toggleImages }} disabled={kit.serverUnavailable} />
            <button className="btn-sm" onClick={a.openPrinting} disabled={custom}>
              Printing
            </button>
            <button className="btn-sm" onClick={a.openArt}>
              Art
            </button>
          </span>
          {expanded && <VersionsStrip faces={c.faces} />}
        </>
      )}

      {variant === "D" && (
        <>
          <span className="proto-controls">
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
        </>
      )}
    </div>
  );
}

export default function DeckGridPrototype({
  variant,
  kit,
}: {
  variant: string;
  kit: DeckGridKit;
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
  const [expanded, setExpanded] = useState<Set<number>>(new Set());
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

  const toggleExpand = useCallback((id: number) => {
    setExpanded((prev) => {
      const next = new Set(prev);
      if (next.has(id)) next.delete(id);
      else next.add(id);
      return next;
    });
  }, []);

  // Variant A's "Card back…" opens the row's own picker through the trigger
  // each tile registers from its (hidden) mount of that control.
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
  // Variant D's images: a modal over the grid, never an inline strip, so no
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

  return (
    <div className="proto-grid-wrap">
      <div className={`deck-grid proto-${variant}`}>
        {kit.cards.map((card) => (
          <GridCell
            key={card.id}
            card={card}
            c={coverage.get(card.id)!}
            a={makeActions(kit, card, addLog)}
            variant={variant}
            kit={kit}
            origin={origin}
            serverThumbs={serverThumbs}
            queue={queue}
            expanded={expanded.has(card.id)}
            onToggle={() => toggleExpand(card.id)}
            onShowImages={() => showImages(card)}
            onMenu={(x, y) => setMenu({ card, x, y })}
            onBackTrigger={registerBack}
          />
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
            () => openBackPicker(menu.card),
            () => showImages(menu.card),
            variant === "A",
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
          <span>inline images {expanded.size}</span>
          {(variant === "A" || variant === "D") && (
            <span>images modal {imagesCard ? "open" : "closed"}</span>
          )}
        </div>
        {log.map((line, i) => (
          <div key={`${i}-${line}`}>{line}</div>
        ))}
      </div>
    </div>
  );
}
