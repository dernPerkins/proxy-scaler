// The change-printing control: a card row's set/collector rendered as a
// button that opens a modal grid of card images, one tile per printing of
// the card (variants share an oracle_id — served by GET /api/cards/variants
// from the server's locally-imported Scryfall corpus; no live Scryfall call,
// and a deliberate "import the card database first" hint when there is no
// corpus). Tile images come from the generation server's thumbnail endpoint
// (GET /api/cards/{id}/thumb), never from Scryfall directly, so "the client
// never calls Scryfall" still holds. Clicking a tile selects it into the
// left-hand preview pane, which shows the full-size PNG Generate would use
// (GET /api/cards/{id}/original — loading it caches that original on the
// server, so Generate later skips the download). The modal is the deck
// walk's (DeckArtWalk.tsx): selecting a printing or MPC Fill art chooses it
// for the card, Prev / Next move through the deck, and the choices apply
// when the walk closes; the pane's button, a double-click or Enter choose
// and close.
import {
  useEffect,
  useLayoutEffect,
  useMemo,
  useRef,
  useState,
  type KeyboardEvent,
  type ReactNode,
} from "react";
import { useMutation, useQuery, useQueryClient } from "@tanstack/react-query";
import { generationApi, ApiError } from "../api/generation";
import { isImageFaceArt, projectApi } from "../api/project";
import {
  PRINTING_IMAGES_MIN_SERVER_VERSION,
  serverSupportsMpcFill,
  serverSupportsPrintingImages,
  useServerVersion,
} from "../config";
import { MpcArtBrowser } from "./MpcArtPicker";
import { LoadingGrid, LoadingStage } from "./PickerLoading";
import { useProject } from "../context/ProjectContext";
import { modelDisplayName } from "../constants";
import { useImageCacheSetting } from "../imageOrigin";
import type { CardRow } from "../api/project";
import type { CardVariant, GeneratedPair, ImageOrigin, MpcCard } from "../api/types";
import ModalOverlay from "./ModalOverlay";
import StatusBadge from "./StatusBadge";
import TutorialButton from "../tutorial/TutorialButton";
import { useTourWhenPresent } from "../tutorial/tutorialStore";
import MultiSelectFilter, { type FilterOption } from "./MultiSelectFilter";
import { alternateName } from "../cardNames";
import SetIcon from "./SetIcon";
import SelectMenu from "./SelectMenu";
import {
  KEYBOARD_PREVIEW_SETTLE_MS,
  gridColumns,
  useQueuedThumb,
  useThumbnailQueue,
  type ThumbnailQueue,
} from "./imageGrid";

export type Pick = {
  scryfallId: string;
  name: string;
  setCode: string;
  collectorNumber: string;
  lang: string;
  printedName: string | null;
  flavorName?: string | null;
  rarity: string | null;
};

function variantYear(v: CardVariant): string {
  return v.released_at ? ` (${v.released_at.slice(0, 4)})` : "";
}

// How much of a variant is already generated for the current sidebar
// model + DPI selection: "full" = every selected DPI has all of the
// printing's faces, "partial" = something exists but not everything,
// "none" = no mark at all.
type Coverage = "none" | "partial" | "full";

function variantCoverage(
  v: CardVariant,
  found: GeneratedPair[] | undefined,
  dpis: number[],
): Coverage {
  if (!found || found.length === 0 || dpis.length === 0) return "none";
  // Count distinct generated faces per DPI rather than comparing face
  // indices: Scryfall face indices aren't guaranteed 0..n-1 (faces
  // without their own image are skipped), so "how many" is the honest
  // comparison against face_count.
  const facesPerDpi = new Map<number, Set<number | null>>();
  for (const pair of found) {
    const faces = facesPerDpi.get(pair.dpi) ?? new Set<number | null>();
    faces.add(pair.face_index);
    facesPerDpi.set(pair.dpi, faces);
  }
  const needed = v.face_count ?? 1;
  const full = dpis.every((dpi) => (facesPerDpi.get(dpi)?.size ?? 0) >= needed);
  return full ? "full" : "partial";
}

type SortMode = "newest" | "oldest" | "set";

// One printing across languages: its set and collector number.
const slotOf = (v: CardVariant) => `${v.set_code}|${v.collector_number}`;

// Scryfall's printed-language codes, named for the language filter's
// tooltips. Includes the fictional ones (Quenya, Phyrexian) a few promos
// are printed in.
const LANGUAGE_NAMES: Record<string, string> = {
  en: "English",
  es: "Spanish",
  fr: "French",
  de: "German",
  it: "Italian",
  pt: "Portuguese",
  ja: "Japanese",
  ko: "Korean",
  ru: "Russian",
  zhs: "Simplified Chinese",
  zht: "Traditional Chinese",
  he: "Hebrew",
  la: "Latin",
  grc: "Ancient Greek",
  ar: "Arabic",
  sa: "Sanskrit",
  ph: "Phyrexian",
  qya: "Quenya",
};

const naturalCompare = (a: string, b: string) =>
  a.localeCompare(b, undefined, { numeric: true, sensitivity: "base" });

// The server already sends newest first (then set, then collector number
// in natural order, English first), so "newest" is its order untouched.
function sortVariants(variants: CardVariant[], mode: SortMode): CardVariant[] {
  if (mode === "newest") return variants;
  const tail = (a: CardVariant, b: CardVariant) =>
    naturalCompare(a.collector_number, b.collector_number) ||
    Number(a.lang !== "en") - Number(b.lang !== "en") ||
    a.lang.localeCompare(b.lang);
  if (mode === "oldest") {
    // Undated printings last, as in the server's newest-first order.
    const date = (v: CardVariant) => v.released_at ?? "￿";
    return [...variants].sort(
      (a, b) =>
        date(a).localeCompare(date(b)) || a.set_code.localeCompare(b.set_code) || tail(a, b),
    );
  }
  const setName = (v: CardVariant) => v.set_name ?? v.set_code;
  return [...variants].sort(
    (a, b) => naturalCompare(setName(a), setName(b)) || tail(a, b),
  );
}

// The card-shaped stand-in for a missing image: shown when the thumbnail
// fails (offline, nothing cached) and for every tile against a server too
// old to serve thumbnails. Carries enough to tell printings apart.
function TilePlaceholder({
  v,
  faceName,
  altName,
}: {
  v: CardVariant;
  faceName: string;
  altName: string | null;
}) {
  return (
    <span className="printing-tile-placeholder">
      <span className="mono">
        <SetIcon setCode={v.set_code} rarity={v.rarity} />
        {v.set_code.toUpperCase()} #{v.collector_number}
      </span>
      <span className="printing-tile-placeholder-name">{faceName}</span>
      {altName && <span className="printing-set-name">{altName}</span>}
      {v.set_name && <span className="printing-set-name">{v.set_name}</span>}
    </span>
  );
}

// The face's own (English) name: what was imported and what matches.
// What's printed on this printing, when different, is faceAltName.
function faceNameOf(v: CardVariant, face: number): string {
  return (v.face_count ?? 1) > 1 ? (v.face_names?.[face] ?? v.name) : v.name;
}

function faceAltName(v: CardVariant, face: number): string | null {
  const double = (v.face_count ?? 1) > 1;
  return alternateName(
    double ? faceNameOf(v, face) : v.name,
    v.printed_name && double ? v.printed_name.split(" // ")[face] : v.printed_name,
    v.flavor_name && double ? v.flavor_name.split(" // ")[face] : v.flavor_name,
  );
}

function flipTitle(v: CardVariant, face: number): string {
  const other = face === 0 ? 1 : 0;
  return `Show ${v.face_names?.[other] ?? (other === 1 ? "back" : "front")}`;
}

function GenMark({ coverage }: { coverage: Coverage }) {
  if (coverage === "none") return null;
  return (
    <span
      className={`printing-gen-mark ${coverage === "full" ? "gen-full" : "gen-partial"}`}
      title={
        coverage === "full"
          ? "Already generated: every selected DPI, all faces (current model)"
          : "Partially generated for the current model/DPI selection"
      }
    >
      ✓
    </span>
  );
}

function PrintingTile(props: {
  v: CardVariant;
  current: boolean;
  selected: boolean;
  coverage: Coverage;
  face: number;
  /** null: the setting hasn't loaded yet, so no image is requested. */
  origin: ImageOrigin | null;
  showImages: boolean;
  /** The selected printing, whose thumbnail loads before the rest. */
  first: boolean;
  tabIndex: number;
  queue: ThumbnailQueue;
  buttonRef: (el: HTMLButtonElement | null) => void;
  onFocus: () => void;
  onSelect: () => void;
  onPick: () => void;
  onFlip: () => void;
}) {
  const { v, current, selected, coverage, face, origin, showImages } = props;
  const faceCount = v.face_count ?? 1;
  const [failedFaces, setFailedFaces] = useState<ReadonlySet<number>>(new Set());
  const faceName = faceNameOf(v, face);
  const frameRef = useRef<HTMLSpanElement>(null);
  // The thumbnail waits its turn: the selection first, then what's in
  // view, then the margin (imageGrid.ts).
  const { load, settle } = useQueuedThumb(
    props.queue,
    frameRef,
    showImages && origin != null,
    props.first,
  );

  let image;
  if (!showImages || failedFaces.has(face)) {
    image = <TilePlaceholder v={v} faceName={faceName} altName={faceAltName(v, face)} />;
  } else if (origin == null || !load) {
    // The empty frame: waiting its turn, or the origin setting hasn't
    // loaded.
    image = null;
  } else {
    image = (
      <img
        key={face}
        src={generationApi.cardThumbUrl(v.scryfall_id, face, origin, v.image_version)}
        alt=""
        decoding="async"
        draggable={false}
        onLoad={settle}
        onError={() => {
          settle();
          setFailedFaces((prev) => new Set(prev).add(face));
        }}
      />
    );
  }

  return (
    <div
      className={`printing-tile${current ? " active" : ""}${selected ? " selected" : ""}${
        faceCount > 1 ? " has-flip" : ""
      }`}
    >
      <button
        type="button"
        ref={props.buttonRef}
        className="printing-tile-pick"
        tabIndex={props.tabIndex}
        onFocus={props.onFocus}
        // A click selects (the preview pane shows it); picking is the
        // pane's button, a double-click, or Enter.
        onClick={props.onSelect}
        onDoubleClick={props.onPick}
        onKeyDown={(e) => {
          if (e.key !== "Enter") return;
          // Without this, Enter's native click would only select.
          e.preventDefault();
          props.onPick();
        }}
        aria-pressed={selected}
        aria-current={current ? "true" : undefined}
        title={`${faceName}${faceAltName(v, face) ? ` (${faceAltName(v, face)})` : ""} — ${
          v.set_name ?? v.set_code.toUpperCase()
        }${variantYear(v)}`}
      >
        <span className="printing-tile-frame" ref={frameRef}>
          {image}
        </span>
        <span className="printing-tile-label">
          <span className="mono">
            <SetIcon setCode={v.set_code} rarity={v.rarity} />
            {v.set_code.toUpperCase()} #{v.collector_number} · {v.lang.toUpperCase()}
          </span>
          {v.digital && <span className="printing-tile-tag">digital</span>}
          <GenMark coverage={coverage} />
        </span>
      </button>
      {faceCount > 1 && (
        <button
          type="button"
          className="printing-tile-flip btn-sm"
          // Out of the tab order: arrow keys walk the tiles, and the flip
          // stays one click away for a pointer.
          tabIndex={-1}
          title={flipTitle(v, face)}
          aria-label="Flip card"
          onClick={props.onFlip}
        >
          ⟲ Flip
        </button>
      )}
    </div>
  );
}

// Every variant already generated for this printing's face — the deck
// list's badges, minus task states (tasks are per project; the registry is
// what the picker can see). Registry-wide: an image from any project counts.
function PreviewVersions({ v, face, enabled }: { v: CardVariant; face: number; enabled: boolean }) {
  const query = useQuery({
    queryKey: ["printing-generated", v.scryfall_id],
    queryFn: () => generationApi.galleryStatus([v.scryfall_id]),
    enabled,
  });
  if (!enabled || !query.isSuccess) return null;
  // Single-faced rows carry face_index null; a DFC's rows, its face.
  const faceIndex = (v.face_count ?? 1) > 1 ? face : null;
  const versions = (query.data.statuses[v.scryfall_id] ?? []).filter(
    (pair) => pair.face_index === faceIndex && pair.model,
  );
  if (versions.length === 0) {
    return <span className="printing-preview-none">Not Generated Yet</span>;
  }
  // The deck list's own badges, so a version reads the same everywhere.
  return (
    <>
      {versions.map((pair) => (
        <StatusBadge key={`${pair.dpi}-${pair.model}`} status="done">
          {pair.dpi} · {modelDisplayName(pair.model as string)}
        </StatusBadge>
      ))}
    </>
  );
}

// The left-hand column, the modal's full height: the selected printing
// at full size — the exact PNG Generate downloads and upscales — on a
// blurred wash of its own art, with its details, versions and the button
// that picks it on a gradient scrim along the bottom. The thumbnail
// (already loaded by its tile, and cached) shows at once; the full image
// fades in over it once loaded. Loading it is the printing's download: the
// server keeps it in its originals cache, so a later Generate or Download
// of this printing skips the fetch.
//
// The column is a subgrid of the modal's rows, so the card sits in the
// grid's row: its top lines up with the first row of tiles whatever the
// header and filter rows measure.
function PrintingPreview(props: {
  v: CardVariant;
  face: number;
  current: boolean;
  origin: ImageOrigin | null;
  showImages: boolean;
  /** 0 after a click; KEYBOARD_PREVIEW_SETTLE_MS after an arrow key. */
  settleMs: number;
  onFlip: () => void;
  onPick: () => void;
  /** The pick button's label; "Use This Printing" unless given. */
  pickLabel?: string;
}) {
  const { v, face, current, origin, showImages, settleMs } = props;
  const queryClient = useQueryClient();
  const faceCount = v.face_count ?? 1;
  const faceName = faceNameOf(v, face);
  const canLoad = showImages && origin != null;
  const fullUrl = canLoad ? generationApi.cardOriginalUrl(v.scryfall_id, face, origin) : null;
  const thumbUrl = canLoad
    ? generationApi.cardThumbUrl(v.scryfall_id, face, origin, v.image_version)
    : null;

  // The full image's src, set only once the selection has settled.
  const [requestedUrl, setRequestedUrl] = useState<string | null>(null);
  const [loadedUrl, setLoadedUrl] = useState<string | null>(null);
  const [failedUrls, setFailedUrls] = useState<ReadonlySet<string>>(new Set());
  const [thumbFailed, setThumbFailed] = useState<string | null>(null);
  useEffect(() => {
    if (!fullUrl) return;
    const timer = setTimeout(() => setRequestedUrl(fullUrl), settleMs);
    return () => clearTimeout(timer);
  }, [fullUrl, settleMs]);

  const fullFailed = fullUrl != null && failedUrls.has(fullUrl);
  const fullLoaded = fullUrl != null && loadedUrl === fullUrl;
  const thumbShown = thumbUrl != null && thumbFailed !== thumbUrl;

  // Loading a face full size is its download (see above), so a
  // double-faced printing's other face loads behind it: looking at a card
  // leaves the whole card downloaded, not just its front. In the
  // background, once the face on show has loaded, so it never slows that.
  useEffect(() => {
    if (!fullLoaded || faceCount < 2 || origin == null) return;
    const other = new Image();
    other.onload = () =>
      void queryClient.invalidateQueries({ queryKey: ["printing-generated", v.scryfall_id] });
    other.src = generationApi.cardOriginalUrl(v.scryfall_id, face === 0 ? 1 : 0, origin);
    return () => {
      other.onload = null;
    };
  }, [fullLoaded, faceCount, origin, v.scryfall_id, face, queryClient]);

  return (
    <aside className="printing-preview" aria-label="Selected printing">
      {thumbShown && (
        <div className="printing-preview-wash" style={{ backgroundImage: `url("${thumbUrl}")` }} />
      )}
      {faceCount > 1 && (
        <button
          type="button"
          className="printing-preview-glass printing-preview-flip"
          title={flipTitle(v, face)}
          onClick={props.onFlip}
        >
          ⟲ Flip
        </button>
      )}

      <div className="printing-preview-stage">
        <div className="printing-preview-frame">
          {/* Bottom layer: the tile's thumbnail, or the placeholder when
              there is no image to show. The full image layers over it. */}
          {thumbShown ? (
            <img
              src={thumbUrl}
              alt=""
              draggable={false}
              onError={() => setThumbFailed(thumbUrl)}
            />
          ) : (
            <TilePlaceholder v={v} faceName={faceName} altName={faceAltName(v, face)} />
          )}
          {fullUrl && requestedUrl === fullUrl && !fullFailed && (
            <img
              className={`printing-preview-full${fullLoaded ? " loaded" : ""}`}
              src={fullUrl}
              alt={`${faceName}, full size`}
              draggable={false}
              onLoad={() => {
                setLoadedUrl(fullUrl);
                // Serving it registered the original on the server: show its
                // "300 · Original" version now.
                void queryClient.invalidateQueries({
                  queryKey: ["printing-generated", v.scryfall_id],
                });
              }}
              onError={() => setFailedUrls((prev) => new Set(prev).add(fullUrl))}
            />
          )}
          {canLoad && !fullLoaded && !fullFailed && (
            <span className="printing-preview-status">Loading full size…</span>
          )}
        </div>
      </div>

      <div className="printing-preview-scrim" data-tour="picker-preview-details">
        <h3 className="printing-preview-name">{faceName}</h3>
        {faceAltName(v, face) && (
          <p className="printing-preview-alt-name" title="As printed on this card">
            {faceAltName(v, face)}
          </p>
        )}
        <p className="printing-preview-meta">
          <SetIcon setCode={v.set_code} rarity={v.rarity} />
          {v.set_name ?? v.set_code.toUpperCase()}
          {variantYear(v)} · {v.set_code.toUpperCase()} #{v.collector_number} ·{" "}
          {v.lang.toUpperCase()}
          {current ? " · Current" : ""}
          {v.digital ? " · Digital" : ""}
        </p>
        {canLoad && fullFailed && (
          <p className="printing-preview-meta">Full-size image unavailable.</p>
        )}
        {/* A fixed-height row, scrolling when a printing has many versions,
            so the scrim doesn't change shape from one printing to the next. */}
        <div className="printing-preview-versions" aria-label="Generated versions">
          <PreviewVersions v={v} face={face} enabled={showImages} />
        </div>
        <button type="button" className="printing-preview-cta" onClick={props.onPick}>
          {props.pickLabel ?? "Use This Printing"}
        </button>
      </div>
    </aside>
  );
}

/** Where the change-printing modal opens: the printing grid, or MPC Fill
 *  art for one face. */
export interface PickerOpenRequest {
  source: PickerSource;
  face: number;
}

export type PickerSource = "scryfall" | "mpc";

/** One choice made in the deck walk for one face of the card on screen:
 *  a Scryfall printing (that face of it) or MPC Fill art. Faces are
 *  chosen independently. The front's printing is the card's printing;
 *  the back follows it unless the back is given its own. */
export type WalkChoice =
  | { kind: "printing"; face: number; totalFaces: number; pick: Pick }
  | { kind: "art"; face: number; totalFaces: number; art: MpcCard };

/** What the deck walk will apply to one face when it closes. */
export type FaceChoice =
  | { kind: "printing"; totalFaces: number; pick: Pick }
  | { kind: "art"; totalFaces: number; art: MpcCard };

/** What the deck walk will apply to one card when it closes, by face. */
export interface StagedChanges {
  faces: ReadonlyMap<number, FaceChoice>;
}

/** The printing a face prints from, once `staged` applies: a Scryfall id,
 *  or null when it prints MPC Fill or custom art. Face 0 is the card's
 *  printing; face 1 has its own only when given one, else it follows the
 *  front's printing (even while the front prints MPC Fill art). */
export function facePrinting(
  card: CardRow,
  staged: StagedChanges | null,
  face: number,
): string | null {
  const chosen = staged?.faces.get(face);
  if (chosen) return chosen.kind === "printing" ? chosen.pick.scryfallId : null;
  const own = card.face_art?.find((fa) => fa.face === face);
  if (own) return own.scryfall ? own.scryfall.scryfall_id : null;
  return frontPrinting(card, staged);
}

/** The card's printing once `staged` applies: the front's chosen printing,
 *  else the card's own. */
export function frontPrinting(card: CardRow, staged: StagedChanges | null): string | null {
  const front = staged?.faces.get(0);
  return front?.kind === "printing" ? front.pick.scryfallId : card.scryfall_id;
}

/** The deck walk's side of the modal (DeckArtWalk.tsx). */
export interface WalkHooks {
  /** The walk's buttons in the modal head (Close). */
  nav: ReactNode;
  staged: StagedChanges | null;
  /** Something was selected: it is the card's choice now. */
  onSelect: (choice: WalkChoice) => void;
  /** Chosen with the pane's button, a double-click or Enter: choose it
   *  and close the walk. */
  onChoose: (choice: WalkChoice) => void;
  /** The deck navigation and Confirm, in a bar under the grid. */
  footer: ReactNode;
}

export function PrintingGridModal(props: {
  card: CardRow;
  preferredLang: string;
  initial: PickerOpenRequest;
  /** Closes the walk, applying what it chose. */
  onClose: () => void;
  /** The Scryfall | MPC Fill switch moved. */
  onSourceChange: (source: PickerSource) => void;
  walk: WalkHooks;
  /** The pick buttons' labels, for a use other than the deck walk
   *  (Add Cards' Card Search: "Add To Deck"). */
  pickLabels?: { printing: string; art: string };
}) {
  const { card, preferredLang, onClose, walk } = props;
  const [source, setSourceState] = useState<PickerSource>(props.initial.source);
  const setSource = (next: PickerSource) => {
    setSourceState(next);
    props.onSourceChange(next);
  };
  // The face being chosen for, on both tabs: a double-faced card's faces
  // are chosen independently (the head's Front | Back switch).
  const [face, setFaceState] = useState(props.initial.face);
  const queryClient = useQueryClient();
  const cardLang = card.lang ?? preferredLang ?? "en";
  // Starts on "<defaultLang> + Exclusives" (see shownByDefault); after
  // that, any number of languages can be ticked, none meaning every one.
  const [langDefault, setLangDefault] = useState(true);
  const [langFilter, setLangFilter] = useState<ReadonlySet<string>>(() => new Set());
  const [setFilter, setSetFilter] = useState<ReadonlySet<string>>(new Set());
  const [sortMode, setSortMode] = useState<SortMode>("newest");
  const serverVersion = useServerVersion();
  const showImages = serverSupportsPrintingImages(serverVersion);
  const mpcAvailable = card.scryfall_id != null && serverSupportsMpcFill(serverVersion);
  const { origin } = useImageCacheSetting();

  useEffect(() => {
    function onKeyDown(e: globalThis.KeyboardEvent) {
      // A handled Esc (the set dropdown closing) isn't the grid's.
      if (e.key === "Escape" && !e.defaultPrevented) onClose();
    }
    document.addEventListener("keydown", onKeyDown);
    return () => document.removeEventListener("keydown", onKeyDown);
  }, [onClose]);

  // "Show Digital" is an app-wide preference persisted in the client's
  // app_settings store — every picker instance shares this query entry,
  // so checking it on one card holds for the next and across restarts.
  const showDigitalQuery = useQuery({
    queryKey: ["show-digital"],
    queryFn: () => projectApi.getShowDigitalPrintings(),
    staleTime: Infinity,
  });
  const includeDigital = showDigitalQuery.data ?? false;
  const setShowDigital = useMutation({
    mutationFn: (show: boolean) => projectApi.setShowDigitalPrintings(show),
    onSuccess: (_data, show) => queryClient.setQueryData(["show-digital"], show),
  });

  // Fetched only while the grid is open (this component mounts on open),
  // so a 200-card decklist doesn't fire 200 variant queries on render.
  const variantsQuery = useQuery({
    queryKey: [
      "card-variants",
      card.scryfall_id ?? `${card.set_code}/${card.collector_number}/${card.name}`,
      includeDigital,
    ],
    queryFn: () =>
      generationApi.cardVariants({
        scryfall_id: card.scryfall_id,
        set_code: card.set_code,
        collector_number: card.collector_number,
        name: card.name,
        include_digital: includeDigital,
      }),
    // Wait for the saved Show Digital choice: fetching with the default
    // first would load the grid twice and place the initial focus on a
    // list that may not contain the current (digital) printing.
    enabled: showDigitalQuery.isFetched,
    staleTime: 60_000,
  });

  const variants = useMemo(() => variantsQuery.data?.variants ?? [], [variantsQuery.data]);
  // Loaded and got something — until then (still loading, or 404 because
  // there's no card corpus on the server) the filter controls have nothing
  // to filter and render disabled placeholders instead of pretending.
  const ready = variantsQuery.isSuccess && variants.length > 0;

  // "Already generated" marks, judged against the sidebar's current
  // generation settings. Chained on the variants query. Model/DPIs sit in
  // the query key, so changing them in the sidebar refetches only this
  // cheap lookup, never the corpus-derived variants. Answered from the
  // server's registry alone: cross-project, no filesystem stats.
  const { settings } = useProject();
  const dpis = settings.dpi_targets;
  const variantIds = useMemo(() => variants.map((v) => v.scryfall_id), [variants]);
  const statusQuery = useQuery({
    queryKey: [
      "printing-gen-status",
      card.scryfall_id ?? `${card.set_code}/${card.collector_number}/${card.name}`,
      includeDigital,
      settings.model,
      dpis.join(","),
    ],
    queryFn: () => generationApi.galleryStatus(variantIds, settings.model, dpis),
    enabled: ready && dpis.length > 0,
  });
  const statuses = statusQuery.data?.statuses;
  const coverageOf = (v: CardVariant) => variantCoverage(v, statuses?.[v.scryfall_id], dpis);
  // English first, then by code.
  const languageOptions = useMemo<FilterOption[]>(
    () =>
      Array.from(new Set(variants.map((v) => v.lang)))
        .sort((a, b) => Number(a !== "en") - Number(b !== "en") || a.localeCompare(b))
        .map((lang) => ({ value: lang, label: lang.toUpperCase(), title: LANGUAGE_NAMES[lang] })),
    [variants],
  );
  // Only languages this card actually has count: if none of the ticked
  // ones do (the card has no printing in its default language), the grid
  // shows every language rather than nothing.
  const effectiveLangs = useMemo<ReadonlySet<string>>(
    () => new Set([...langFilter].filter((l) => languageOptions.some((o) => o.value === l))),
    [langFilter, languageOptions],
  );
  const setOptions = useMemo<FilterOption[]>(() => {
    const byCode = new Map<string, string | null>();
    for (const v of variants) if (!byCode.has(v.set_code)) byCode.set(v.set_code, v.set_name);
    return [...byCode]
      .sort(([a], [b]) => a.localeCompare(b))
      .map(([code, name]) => ({
        value: code,
        label: code.toUpperCase(),
        title: name,
        icon: <SetIcon setCode={code} />,
      }));
  }, [variants]);
  // What this face prints now: the card's printing, or for a back given
  // its own, that printing (null while it prints MPC Fill art).
  const currentId = facePrinting(card, null, face);
  const isCurrent = (v: CardVariant) =>
    card.scryfall_id
      ? v.scryfall_id === currentId
      : v.set_code === (card.set_code ?? "").toLowerCase() &&
        v.collector_number === (card.collector_number ?? "") &&
        v.lang === cardLang;
  // The card's own printing (its front's), whatever this face prints.
  const isCardPrinting = (v: CardVariant) =>
    card.scryfall_id ? v.scryfall_id === card.scryfall_id : isCurrent(v);

  // The default view's language. Normally the card's own; but when the
  // card is currently an exclusive (a printing with no English version,
  // e.g. a Japanese-only promo), its language says nothing about what the
  // user prints in, so the Project's import language stands in — and
  // follows the import dropdown when that changes.
  const defaultLang = useMemo(() => {
    const current = variants.find(isCardPrinting);
    if (!current || current.lang === "en") return cardLang;
    const slot = slotOf(current);
    const hasEnglish = variants.some((v) => v.lang === "en" && slotOf(v) === slot);
    return hasEnglish ? cardLang : preferredLang || "en";
    // isCurrent reads only props that are fixed while the grid is open.
  }, [variants, cardLang, preferredLang]);

  // The default language view, the way Scryfall lists a card's prints:
  // printings in defaultLang, plus every printing — a set + collector
  // number — that has no version in that language or in English. Those
  // exclusives are real printings that only exist in another language (a
  // Japanese-only Spotlight promo, the Quenya and Dwarvish Collector
  // Booster versions) and would otherwise be hidden. A card with no
  // printing in defaultLang shows everything.
  const shownByDefault = useMemo(() => {
    if (!variants.some((v) => v.lang === defaultLang)) return () => true;
    const covered = new Set(
      variants.filter((v) => v.lang === defaultLang || v.lang === "en").map(slotOf),
    );
    return (v: CardVariant) => v.lang === defaultLang || !covered.has(slotOf(v));
  }, [variants, defaultLang]);
  // The default view also always lists this face's own printing and the
  // one chosen for it in the walk, whatever their language, so reopening
  // the card (Prev / Next) can land on it.
  const stagedId = facePrinting(card, walk.staged, face);
  const shown = useMemo(
    () =>
      sortVariants(
        variants.filter(
          (v) =>
            (langDefault
              ? shownByDefault(v) || v.scryfall_id === stagedId || isCurrent(v)
              : effectiveLangs.size === 0 || effectiveLangs.has(v.lang)) &&
            (setFilter.size === 0 || setFilter.has(v.set_code)),
        ),
        sortMode,
      ),
    // isCurrent reads only the face and props fixed while the grid is open.
    [variants, langDefault, shownByDefault, stagedId, effectiveLangs, setFilter, sortMode, face],
  );

  function pickOf(v: CardVariant): Pick {
    return {
      scryfallId: v.scryfall_id,
      name: v.name,
      setCode: v.set_code,
      collectorNumber: v.collector_number,
      lang: v.lang,
      printedName: v.printed_name,
      flavorName: v.flavor_name ?? null,
      rarity: v.rarity ?? null,
    };
  }
  function choiceOf(v: CardVariant): WalkChoice {
    return { kind: "printing", face, totalFaces: Math.min(2, v.face_count ?? 1), pick: pickOf(v) };
  }
  // The walk's choice for this face: its staged printing, else what it
  // prints now. None while it prints MPC Fill art: its own printing then
  // only opens selected, and picking it puts it back on Scryfall art.
  const isChosen = (v: CardVariant) => stagedId != null && v.scryfall_id === stagedId;

  // Which face each double-faced tile shows: the face being chosen for,
  // unless flipped to look at the other. Lifted here so the tile and the
  // preview pane flip together.
  const [faceById, setFaceById] = useState<ReadonlyMap<string, number>>(new Map());
  const faceOf = (v: CardVariant) =>
    faceById.get(v.scryfall_id) ?? Math.min(face, (v.face_count ?? 1) - 1);
  const flip = (v: CardVariant) =>
    setFaceById((prev) => new Map(prev).set(v.scryfall_id, faceOf(v) === 0 ? 1 : 0));

  // The selected printing is what the preview pane shows and picks. It
  // follows keyboard focus as well as clicks, so arrowing previews too.
  const [selectedId, setSelectedId] = useState<string | null>(null);
  const selected = variants.find((v) => v.scryfall_id === selectedId) ?? null;
  // The picker's first-open tour, once the grid and the preview are filled
  // in; decklist-cards (the card rows' tour) always comes before it.
  useTourWhenPresent("decklist-picker", ready && selected != null, "decklist-cards");
  const [settleMs, setSettleMs] = useState(0);

  // Keyboard: one tile is in the tab order at a time (a roving tabindex);
  // arrow keys move it in grid order, and selection with it.
  const gridRef = useRef<HTMLDivElement>(null);
  const scrollRef = useRef<HTMLDivElement>(null);
  const thumbQueue = useThumbnailQueue(scrollRef);
  const tileRefs = useRef<(HTMLButtonElement | null)[]>([]);
  const [focusIndex, setFocusIndex] = useState(0);
  const clampedFocus = Math.min(focusIndex, Math.max(0, shown.length - 1));

  function moveFocus(index: number) {
    const next = Math.max(0, Math.min(shown.length - 1, index));
    setFocusIndex(next);
    tileRefs.current[next]?.focus();
  }

  function onGridKeyDown(e: KeyboardEvent<HTMLDivElement>) {
    // Alt+←/→ is the deck walk's Prev / Next. Moving focus first would
    // select (and so choose) the neighbouring tile on the card being left.
    if (e.altKey) return;
    const columns = gridColumns(gridRef.current);
    const delta = { ArrowLeft: -1, ArrowRight: 1, ArrowUp: -columns, ArrowDown: columns }[e.key];
    if (delta === undefined) return;
    e.preventDefault();
    setSettleMs(KEYBOARD_PREVIEW_SETTLE_MS);
    moveFocus(clampedFocus + delta);
  }

  // On open, and on each switch of face, that face's printing is
  // selected, focused and scrolled into view, once, the first time the
  // grid has tiles to show.
  const placedFor = useRef<number | null>(null);
  useEffect(() => {
    if (placedFor.current === face || shown.length === 0) return;
    placedFor.current = face;
    // The chosen printing, else the face's own (while it prints MPC Fill
    // art, none is chosen), else the first.
    const chosen = shown.findIndex(isChosen);
    const index = Math.max(0, chosen >= 0 ? chosen : shown.findIndex(isCurrent));
    setFocusIndex(index);
    setSelectedId(shown[index].scryfall_id);
    // isChosen and isCurrent read only what is fixed until a selection
    // changes it.
  }, [shown, face]);
  // The scroll waits for that selection to render: the preview pane it
  // mounts narrows the grid, reflowing every tile, so scrolling any
  // earlier lands on the wrong row. A layout effect, so it happens before
  // the first paint with the pane.
  const scrolledFor = useRef<number | null>(null);
  useLayoutEffect(() => {
    if (scrolledFor.current === face || selected == null || source !== "scryfall") return;
    if (placedFor.current !== face) return;
    scrolledFor.current = face;
    const tile = tileRefs.current[clampedFocus];
    tile?.scrollIntoView({ block: "center" });
    tile?.focus({ preventScroll: true });
  }, [selected, clampedFocus, source, face]);

  // isPending, not isLoading: also while the query waits on Show Digital.
  const loadingPrintings = variantsQuery.isPending;

  // The card's faces: how many, and what each is called. Known once the
  // printings load; until then, what the card's face art says (or one).
  const cardVariant = variants.find(isCardPrinting);
  const totalFaces = Math.min(
    2,
    cardVariant?.face_count ?? card.face_art?.[0]?.total_faces ?? 1,
  );
  // This face's MPC Fill (or custom) art, if it prints some.
  const artFor = (f: number) =>
    card.face_art?.find((fa) => fa.face === f && isImageFaceArt(fa)) ?? null;
  const faceLabelOf = (f: number) =>
    cardVariant?.face_names?.[f] ?? (f === 0 ? "Front" : "Back");
  function setFace(next: number) {
    if (next === face) return;
    setFaceState(next);
    // Each face opens on its own printing, showing that side.
    setFaceById(new Map());
    setSelectedId(null);
  }
  const faceSwitch =
    totalFaces > 1 ? (
      <span className="segmented" role="group" aria-label="Face" data-tour="picker-face">
        {[0, 1].map((f) => (
          <button
            key={f}
            type="button"
            className={`btn-sm${face === f ? " active" : ""}`}
            aria-pressed={face === f}
            onClick={() => setFace(f)}
          >
            {faceLabelOf(f)}
          </button>
        ))}
      </span>
    ) : null;

  const sourceSwitch = mpcAvailable ? (
    <span className="segmented" role="group" aria-label="Art source">
      <button
        type="button"
        className={`btn-sm${source === "scryfall" ? " active" : ""}`}
        aria-pressed={source === "scryfall"}
        onClick={() => setSource("scryfall")}
      >
        Scryfall
      </button>
      <button
        type="button"
        className={`btn-sm${source === "mpc" ? " active" : ""}`}
        aria-pressed={source === "mpc"}
        onClick={() => setSource("mpc")}
        title="Community art from MPC Fill, printed in place of this printing's art"
      >
        MPC Fill
      </button>
    </span>
  ) : null;

  if (source === "mpc" && mpcAvailable) {
    const art = artFor(face);
    const stagedFace = walk.staged?.faces.get(face);
    const artChoice = (picked: MpcCard): WalkChoice => ({
      kind: "art",
      face,
      totalFaces,
      art: picked,
    });
    return (
      <ModalOverlay onClick={onClose}>
        <div
          className="modal printing-grid-modal"
          role="dialog"
          aria-label={`MPC Fill art: ${card.name}`}
          onClick={(e) => e.stopPropagation()}
        >
          <MpcArtBrowser
            // A new face is a new search.
            key={face}
            initial={{ scryfallId: card.scryfall_id, face }}
            cardTypes={["CARD", "TOKEN"]}
            defaultLang={card.lang}
            currentDriveId={
              stagedFace
                ? stagedFace.kind === "art"
                  ? stagedFace.art.drive_id
                  : null
                : (art?.mpc_drive_id ?? null)
            }
            pickLabel={props.pickLabels?.art ?? "Use This Art"}
            tourAfter="decklist-cards"
            onSelect={(picked) => walk.onSelect(artChoice(picked))}
            onPick={async (picked) => walk.onChoose(artChoice(picked))}
            head={
              <>
                <span className="picker-head-left">
                  {sourceSwitch}
                  {faceSwitch}
                  <TutorialButton tour="mpc-picker" />
                </span>
                {walk.nav}
              </>
            }
          />
          <div className="walk-footer">{walk.footer}</div>
        </div>
      </ModalOverlay>
    );
  }

  return (
    <ModalOverlay onClick={onClose}>
      <div
        className="modal printing-grid-modal"
        role="dialog"
        aria-label={`Change printing: ${card.name}`}
        onClick={(e) => e.stopPropagation()}
      >
        {/* The modal is a grid: the preview fills the first column top to
            bottom; the right column stacks Close, notes, the filter row and
            the scrolling grid. */}
        {loadingPrintings ? (
          <LoadingStage label="Loading printings…" />
        ) : selected && (
          <PrintingPreview
            key={selected.scryfall_id}
            v={selected}
            face={faceOf(selected)}
            current={isCurrent(selected)}
            origin={origin}
            showImages={showImages}
            settleMs={settleMs}
            onFlip={() => flip(selected)}
            onPick={() => walk.onChoose(choiceOf(selected))}
            pickLabel={props.pickLabels?.printing}
          />
        )}

        {/* No title: the card's name is on the preview. */}
        <div className="modal-head printing-grid-head">
          <span className="picker-head-left">
            {sourceSwitch}
            {faceSwitch}
            {/* The tab bar's ? is behind this modal; replay from here. */}
            <TutorialButton tour="decklist-picker" />
          </span>
          {walk.nav}
        </div>

        <div className="printing-grid-notes">
          {ready && !showImages && (
            <p className="hint">
              Previews need a newer generation server (v{PRINTING_IMAGES_MIN_SERVER_VERSION} or
              later). You can still pick a printing.
            </p>
          )}
          {variantsQuery.isError && (
            <p className="hint">
              {variantsQuery.error instanceof ApiError && variantsQuery.error.status === 404
                ? "No local card database on this server — import it from the sidebar's Card Database panel to change printings."
                : `Couldn't load printings: ${
                    variantsQuery.error instanceof Error
                      ? variantsQuery.error.message
                      : String(variantsQuery.error)
                  }`}
            </p>
          )}
        </div>

        {/* Rendered once the list loads even when it's empty: a card with
            only digital printings needs "Show Digital" reachable. */}
        {loadingPrintings ? (
          <div className="modal-body">
            <LoadingGrid label="Loading printings…" />
          </div>
        ) : variantsQuery.isSuccess && (
          <>
            {/* Above the grid, right-aligned, the count first. */}
            <div className="printing-grid-controls" data-tour="picker-filters" data-tour-fit>
              <span className="printing-grid-count">
                {shown.length} of {variants.length}
              </span>
              <MultiSelectFilter
                options={setOptions}
                selected={setFilter}
                onChange={setSetFilter}
                disabled={!ready}
                allLabel="All Sets"
                ariaLabel="Sets"
                findLabel="Find a set"
              />
              <MultiSelectFilter
                options={languageOptions}
                selected={langDefault ? new Set() : effectiveLangs}
                onChange={(next) => {
                  setLangDefault(false);
                  setLangFilter(next);
                }}
                disabled={!ready}
                allLabel="All Languages"
                ariaLabel="Languages"
                preset={{
                  label: `${defaultLang.toUpperCase()} + Exclusives`,
                  title: `${LANGUAGE_NAMES[defaultLang] ?? defaultLang.toUpperCase()} printings, plus printings that only exist in another language`,
                  active: langDefault,
                  onSelect: () => setLangDefault(true),
                }}
              />
              <SelectMenu
                ariaLabel="Sort"
                options={[
                  { value: "newest", label: "Newest first" },
                  { value: "oldest", label: "Oldest first" },
                  { value: "set", label: "By set" },
                ]}
                value={sortMode}
                disabled={!ready}
                onChange={(value) => setSortMode(value as SortMode)}
              />
              <label className="check">
                <input
                  type="checkbox"
                  checked={includeDigital}
                  onChange={(e) => setShowDigital.mutate(e.target.checked)}
                />
                Show Digital
              </label>
            </div>
            {/* Mounted for as long as the grid is, even when the filters
                match nothing: the thumbnail queue is rooted on this element.
                (Switching to MPC Fill and back does remount it; the
                queue rebuilds on the new element — imageGrid.ts.) */}
            <div className="modal-body" ref={scrollRef} data-tour="picker-grid">
              {shown.length === 0 ? (
                <p className="hint">No printings match this filter.</p>
              ) : (
                /* No virtualisation: ~870 tiles (a basic land) is well
                   within what a plain CSS grid handles once thumbnails
                   load only near the view, what's in view first
                   (useThumbnailQueue). */
                <div className="printing-grid" ref={gridRef} onKeyDown={onGridKeyDown}>
                  {shown.map((v, i) => (
                    <PrintingTile
                      key={v.scryfall_id}
                      v={v}
                      // Selecting chooses, so the chosen tile shows only
                      // as chosen; the selection border is left for a
                      // tile the walk opened on without choosing it.
                      current={isChosen(v)}
                      selected={v.scryfall_id === selectedId && !isChosen(v)}
                      coverage={coverageOf(v)}
                      face={faceOf(v)}
                      origin={origin}
                      showImages={showImages}
                      first={v.scryfall_id === selectedId}
                      tabIndex={i === clampedFocus ? 0 : -1}
                      queue={thumbQueue}
                      buttonRef={(el) => {
                        tileRefs.current[i] = el;
                      }}
                      onFocus={() => {
                        setFocusIndex(i);
                        // An arrow key's move chooses too; the initial
                        // focus, on the selection already, doesn't.
                        if (v.scryfall_id !== selectedId) walk.onSelect(choiceOf(v));
                        setSelectedId(v.scryfall_id);
                      }}
                      onSelect={() => {
                        // WKWebView doesn't focus a button on click, so
                        // selection can't rely on onFocus alone.
                        setSettleMs(0);
                        setSelectedId(v.scryfall_id);
                        setFocusIndex(i);
                        tileRefs.current[i]?.focus();
                        walk.onSelect(choiceOf(v));
                      }}
                      onPick={() => walk.onChoose(choiceOf(v))}
                      onFlip={() => flip(v)}
                    />
                  ))}
                </div>
              )}
            </div>
          </>
        )}
        <div className="walk-footer">{walk.footer}</div>
      </div>
    </ModalOverlay>
  );
}

export default function PrintingPicker(props: {
  card: CardRow;
  /** True when the generation server is unreachable — the button still
   *  renders the printing, it just can't open the picker. */
  disabled: boolean;
  /** The pinned printing's rarity, which colors its set symbol. */
  rarity?: string | null;
  /** Open the deck walk at this card (DecklistPage owns it). */
  onOpen: (request: PickerOpenRequest) => void;
}) {
  const { card, disabled, rarity, onOpen } = props;

  // Language always shown, EN included — with several languages of one
  // printing allowed in a deck, an unlabeled row is ambiguous. Rows
  // predating the lang field read as English (the only language that
  // existed then).
  const label = card.set_code
    ? `${card.set_code.toUpperCase()} · ${card.collector_number ?? "—"} · ${(
        card.lang ?? "en"
      ).toUpperCase()}`
    : "— · —";

  return (
    <span className="printing-cell" data-tour="printing-picker">
      <button
        type="button"
        className="card-meta mono printing-toggle"
        title={disabled ? "Generation server is unreachable" : "Change printing"}
        disabled={disabled}
        // A card printing MPC Fill art opens on that art, at its first
        // face printing some; everything else on its printings.
        onClick={() => {
          const art = card.face_art?.find(isImageFaceArt);
          onOpen(art ? { source: "mpc", face: art.face } : { source: "scryfall", face: 0 });
        }}
      >
        <SetIcon setCode={card.set_code} rarity={rarity} />
        {label} ▾
      </button>
    </span>
  );
}
