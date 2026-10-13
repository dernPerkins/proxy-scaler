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
// server, so Generate later skips the download). The pane's button, a
// double-click or Enter pins the card to that exact printing via
// ProjectContext.setCardPrinting.
import {
  useCallback,
  useEffect,
  useLayoutEffect,
  useMemo,
  useRef,
  useState,
  type KeyboardEvent,
} from "react";
import { useMutation, useQuery, useQueryClient } from "@tanstack/react-query";
import { generationApi, ApiError } from "../api/generation";
import { projectApi } from "../api/project";
import {
  PRINTING_IMAGES_MIN_SERVER_VERSION,
  serverSupportsPrintingImages,
  useServerVersion,
} from "../config";
import { useProject } from "../context/ProjectContext";
import { modelDisplayName } from "../constants";
import { useImageCacheSetting } from "../imageOrigin";
import StatusBadge from "./StatusBadge";
import type { CardRow } from "../api/project";
import type { CardVariant, GeneratedPair, ImageOrigin } from "../api/types";
import ModalOverlay from "./ModalOverlay";
import MultiSelectFilter, { type FilterOption } from "./MultiSelectFilter";

type Pick = {
  scryfallId: string;
  name: string;
  setCode: string;
  collectorNumber: string;
  lang: string;
  printedName: string | null;
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

// How many tiles one row of the grid holds, read off the laid-out CSS grid
// (auto-fill columns change with the window width).
function gridColumns(grid: HTMLElement | null): number {
  if (!grid) return 1;
  const columns = getComputedStyle(grid).gridTemplateColumns.split(" ").filter(Boolean);
  return Math.max(1, columns.length);
}

// Thumbnails load as their tiles near the grid's visible area. Not
// `loading="lazy"`: WebKit (the macOS app's webview) loaded every tile of
// the grid on open, ignoring the scroll container. One IntersectionObserver
// rooted on that container serves every tile; a tile reports visible once
// and is then forgotten.
type ObserveTile = (el: Element, onVisible: () => void) => () => void;

function useTileVisibility(scrollRef: React.RefObject<HTMLElement | null>): ObserveTile {
  const observerRef = useRef<IntersectionObserver | null>(null);
  const callbacks = useRef(new Map<Element, () => void>());
  useEffect(
    () => () => {
      // Dropped, not just disconnected, so a remount (StrictMode does one
      // in development) builds a fresh observer on the current container.
      observerRef.current?.disconnect();
      observerRef.current = null;
      callbacks.current.clear();
    },
    [],
  );
  return useCallback(
    (el, onVisible) => {
      if (!observerRef.current) {
        observerRef.current = new IntersectionObserver(
          (entries, observer) => {
            for (const entry of entries) {
              if (!entry.isIntersecting) continue;
              callbacks.current.get(entry.target)?.();
              callbacks.current.delete(entry.target);
              observer.unobserve(entry.target);
            }
          },
          // About two rows ahead, so scrolling rarely meets an empty frame.
          { root: scrollRef.current, rootMargin: "600px 0px" },
        );
      }
      const observer = observerRef.current;
      callbacks.current.set(el, onVisible);
      observer.observe(el);
      return () => {
        callbacks.current.delete(el);
        observer.unobserve(el);
      };
    },
    [scrollRef],
  );
}

// The card-shaped stand-in for a missing image: shown when the thumbnail
// fails (offline, nothing cached) and for every tile against a server too
// old to serve thumbnails. Carries enough to tell printings apart.
function TilePlaceholder({ v, faceName }: { v: CardVariant; faceName: string }) {
  return (
    <span className="printing-tile-placeholder">
      <span className="mono">
        {v.set_code.toUpperCase()} #{v.collector_number}
      </span>
      <span className="printing-tile-placeholder-name">{faceName}</span>
      {v.set_name && <span className="printing-set-name">{v.set_name}</span>}
    </span>
  );
}

function faceNameOf(v: CardVariant, face: number): string {
  const displayName = v.printed_name ?? v.name;
  return (v.face_count ?? 1) > 1 ? (v.face_names?.[face] ?? displayName) : displayName;
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
  tabIndex: number;
  observe: ObserveTile;
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
  const [nearView, setNearView] = useState(false);
  const { observe } = props;
  // In an effect, not the ref callback: effects run after the whole tree
  // has committed, so the scroll container the observer is rooted on
  // exists by now.
  useEffect(() => {
    if (!showImages || !frameRef.current) return;
    return observe(frameRef.current, () => setNearView(true));
  }, [observe, showImages]);

  let image;
  if (!showImages || failedFaces.has(face)) {
    image = <TilePlaceholder v={v} faceName={faceName} />;
  } else if (origin == null || !nearView) {
    // The empty frame: off-screen, or the origin setting hasn't loaded.
    image = null;
  } else {
    image = (
      <img
        key={face}
        src={generationApi.cardThumbUrl(v.scryfall_id, face, origin, v.image_version)}
        alt=""
        decoding="async"
        draggable={false}
        onError={() => setFailedFaces((prev) => new Set(prev).add(face))}
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
        title={`${v.printed_name ?? v.name} — ${v.set_name ?? v.set_code.toUpperCase()}${variantYear(v)}`}
      >
        <span className="printing-tile-frame" ref={frameRef}>
          {image}
        </span>
        <span className="printing-tile-label">
          <span className="mono">
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
function PreviewBadges({ v, face, enabled }: { v: CardVariant; face: number; enabled: boolean }) {
  const query = useQuery({
    queryKey: ["printing-generated", v.scryfall_id],
    queryFn: () => generationApi.galleryStatus([v.scryfall_id]),
    enabled,
  });
  if (!enabled || !query.isSuccess) return null;
  // Single-faced rows carry face_index null; a DFC's rows, its face.
  const faceIndex = (v.face_count ?? 1) > 1 ? face : null;
  const variants = (query.data.statuses[v.scryfall_id] ?? []).filter(
    (pair) => pair.face_index === faceIndex && pair.model,
  );
  if (variants.length === 0) {
    return <p className="hint printing-preview-badges">Not generated yet.</p>;
  }
  return (
    <div className="variants printing-preview-badges">
      {variants.map((pair) => (
        <StatusBadge key={`${pair.dpi}-${pair.model}`} status="done">
          {pair.dpi} · {modelDisplayName(pair.model as string)}
        </StatusBadge>
      ))}
    </div>
  );
}

// How long a keyboard selection must rest before the preview asks for the
// full-size PNG (~1.3 MB, and it lands in the server's originals cache):
// arrowing across a row shouldn't download every card on the way.
const KEYBOARD_PREVIEW_SETTLE_MS = 250;

// The left-hand pane: the selected printing at full size — the exact PNG
// Generate downloads and upscales — with its details and the button that
// picks it. The thumbnail (already loaded by its tile, and cached) shows
// at once; the full image fades in over it once loaded. Loading it is the
// printing's download: the server keeps it in its originals cache, so a
// later Generate or Download of this printing skips the fetch.
function PrintingPreview(props: {
  v: CardVariant;
  face: number;
  current: boolean;
  coverage: Coverage;
  origin: ImageOrigin | null;
  showImages: boolean;
  /** 0 after a click; KEYBOARD_PREVIEW_SETTLE_MS after an arrow key. */
  settleMs: number;
  onFlip: () => void;
  onPick: () => void;
}) {
  const { v, face, current, coverage, origin, showImages, settleMs } = props;
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

  return (
    <aside className="printing-preview" aria-label="Selected printing">
      <div className="printing-preview-frame">
        {/* Bottom layer: the tile's thumbnail, or the placeholder when
            there is no image to show. The full image layers over it. */}
        {thumbUrl && thumbFailed !== thumbUrl ? (
          <img
            className="printing-preview-thumb"
            src={thumbUrl}
            alt=""
            draggable={false}
            onError={() => setThumbFailed(thumbUrl)}
          />
        ) : (
          <TilePlaceholder v={v} faceName={faceName} />
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
              // "300 · Original" badge now.
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

      {canLoad && fullFailed && (
        <p className="hint">Full-size image unavailable. You can still use this printing.</p>
      )}

      <div className="printing-preview-details">
        <span className="printing-preview-name">{faceName}</span>
        <span className="printing-set-name">
          {v.set_name ?? v.set_code.toUpperCase()}
          {variantYear(v)}
        </span>
        <span className="printing-tile-label">
          <span className="mono">
            {v.set_code.toUpperCase()} #{v.collector_number} · {v.lang.toUpperCase()}
          </span>
          {current && <span className="printing-tile-tag">current</span>}
          {v.digital && <span className="printing-tile-tag">digital</span>}
          <GenMark coverage={coverage} />
        </span>
      </div>

      <PreviewBadges v={v} face={face} enabled={showImages} />

      <div className="printing-preview-actions">
        {faceCount > 1 && (
          <button type="button" className="btn-sm" title={flipTitle(v, face)} onClick={props.onFlip}>
            ⟲ Flip
          </button>
        )}
        <button type="button" className="btn-primary" onClick={props.onPick}>
          {current ? "Keep Current Printing" : "Use This Printing"}
        </button>
      </div>
    </aside>
  );
}

function PrintingGridModal(props: {
  card: CardRow;
  preferredLang: string;
  onClose: () => void;
  onPick: (printing: Pick) => void;
}) {
  const { card, preferredLang, onClose, onPick } = props;
  const queryClient = useQueryClient();
  const cardLang = card.lang ?? preferredLang ?? "en";
  // Starts on "<card's language> + Exclusives" (see shownByDefault); after
  // that, any number of languages can be ticked, none meaning every one.
  const [langDefault, setLangDefault] = useState(true);
  const [langFilter, setLangFilter] = useState<ReadonlySet<string>>(() => new Set());
  const [setFilter, setSetFilter] = useState<ReadonlySet<string>>(new Set());
  const [sortMode, setSortMode] = useState<SortMode>("newest");
  const serverVersion = useServerVersion();
  const showImages = serverSupportsPrintingImages(serverVersion);
  const { origin } = useImageCacheSetting();

  useEffect(() => {
    function onKeyDown(e: globalThis.KeyboardEvent) {
      // A handled Esc (the set dropdown closing) isn't the grid's.
      if (e.key === "Escape" && !e.defaultPrevented) onClose();
    }
    document.addEventListener("keydown", onKeyDown);
    return () => document.removeEventListener("keydown", onKeyDown);
  }, [onClose]);

  // "Show digital" is an app-wide preference persisted in the client's
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
    // Wait for the saved Show digital choice: fetching with the default
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
      .map(([code, name]) => ({ value: code, label: code.toUpperCase(), title: name }));
  }, [variants]);
  // The default language view, the way Scryfall lists a card's prints:
  // printings in the card's language (or the Project's), plus every
  // printing — a set + collector number — that has no version in that
  // language or in English. Those exclusives are real printings that only
  // exist in another language (a Japanese-only Spotlight promo, the Quenya
  // and Dwarvish Collector Booster versions) and would otherwise be hidden.
  // A card with no printing in its own language shows everything.
  const shownByDefault = useMemo(() => {
    const slot = (v: CardVariant) => `${v.set_code}|${v.collector_number}`;
    if (!variants.some((v) => v.lang === cardLang)) return () => true;
    const covered = new Set(
      variants.filter((v) => v.lang === cardLang || v.lang === "en").map(slot),
    );
    return (v: CardVariant) => v.lang === cardLang || !covered.has(slot(v));
  }, [variants, cardLang]);
  const shown = useMemo(
    () =>
      sortVariants(
        variants.filter(
          (v) =>
            (langDefault
              ? shownByDefault(v)
              : effectiveLangs.size === 0 || effectiveLangs.has(v.lang)) &&
            (setFilter.size === 0 || setFilter.has(v.set_code)),
        ),
        sortMode,
      ),
    [variants, langDefault, shownByDefault, effectiveLangs, setFilter, sortMode],
  );

  const currentId = card.scryfall_id;
  const isCurrent = (v: CardVariant) =>
    currentId
      ? v.scryfall_id === currentId
      : v.set_code === (card.set_code ?? "").toLowerCase() &&
        v.collector_number === (card.collector_number ?? "") &&
        v.lang === cardLang;

  function pick(v: CardVariant) {
    onClose();
    if (!isCurrent(v)) {
      onPick({
        scryfallId: v.scryfall_id,
        name: v.name,
        setCode: v.set_code,
        collectorNumber: v.collector_number,
        lang: v.lang,
        printedName: v.printed_name,
      });
    }
  }

  // Which face each double-faced tile shows. Lifted here so the tile and
  // the preview pane flip together.
  const [faceById, setFaceById] = useState<ReadonlyMap<string, number>>(new Map());
  const faceOf = (v: CardVariant) => faceById.get(v.scryfall_id) ?? 0;
  const flip = (v: CardVariant) =>
    setFaceById((prev) => new Map(prev).set(v.scryfall_id, faceOf(v) === 0 ? 1 : 0));

  // The selected printing is what the preview pane shows and picks. It
  // follows keyboard focus as well as clicks, so arrowing previews too.
  const [selectedId, setSelectedId] = useState<string | null>(null);
  const selected = variants.find((v) => v.scryfall_id === selectedId) ?? null;
  const [settleMs, setSettleMs] = useState(0);

  // Keyboard: one tile is in the tab order at a time (a roving tabindex);
  // arrow keys move it in grid order, and selection with it.
  const gridRef = useRef<HTMLDivElement>(null);
  const scrollRef = useRef<HTMLDivElement>(null);
  const observeTile = useTileVisibility(scrollRef);
  const tileRefs = useRef<(HTMLButtonElement | null)[]>([]);
  const [focusIndex, setFocusIndex] = useState(0);
  const clampedFocus = Math.min(focusIndex, Math.max(0, shown.length - 1));

  function moveFocus(index: number) {
    const next = Math.max(0, Math.min(shown.length - 1, index));
    setFocusIndex(next);
    tileRefs.current[next]?.focus();
  }

  function onGridKeyDown(e: KeyboardEvent<HTMLDivElement>) {
    const columns = gridColumns(gridRef.current);
    const delta = { ArrowLeft: -1, ArrowRight: 1, ArrowUp: -columns, ArrowDown: columns }[e.key];
    if (delta === undefined) return;
    e.preventDefault();
    setSettleMs(KEYBOARD_PREVIEW_SETTLE_MS);
    moveFocus(clampedFocus + delta);
  }

  // On open, the current printing is selected, focused and scrolled into
  // view, once, the first time the grid has tiles to show.
  const placedInitialFocus = useRef(false);
  useEffect(() => {
    if (placedInitialFocus.current || shown.length === 0) return;
    placedInitialFocus.current = true;
    const index = Math.max(0, shown.findIndex(isCurrent));
    setFocusIndex(index);
    setSelectedId(shown[index].scryfall_id);
    // isCurrent reads only props that are fixed while the grid is open.
  }, [shown]);
  // The scroll waits for that selection to render: the preview pane it
  // mounts narrows the grid, reflowing every tile, so scrolling any
  // earlier lands on the wrong row. A layout effect, so it happens before
  // the first paint with the pane.
  const scrolledToSelection = useRef(false);
  useLayoutEffect(() => {
    if (scrolledToSelection.current || selected == null) return;
    scrolledToSelection.current = true;
    const tile = tileRefs.current[clampedFocus];
    tile?.scrollIntoView({ block: "center" });
    tile?.focus({ preventScroll: true });
  }, [selected, clampedFocus]);

  return (
    <ModalOverlay onClick={onClose}>
      <div
        className="modal printing-grid-modal"
        role="dialog"
        aria-label={`Change printing: ${card.name}`}
        onClick={(e) => e.stopPropagation()}
      >
        <div className="modal-head">
          <span className="modal-title">{card.printed_name ?? card.name}</span>
          <button type="button" className="btn-sm" onClick={onClose}>
            Close
          </button>
        </div>

        {ready && !showImages && (
          <p className="hint">
            Previews need a newer generation server (v{PRINTING_IMAGES_MIN_SERVER_VERSION} or
            later). You can still pick a printing.
          </p>
        )}
        {variantsQuery.isLoading && <p className="hint">Loading printings…</p>}
        {variantsQuery.isError && (
          <p className="hint">
            {variantsQuery.error instanceof ApiError && variantsQuery.error.status === 404
              ? "No local card database on this server — import it from the sidebar's Card database panel to change printings."
              : `Couldn't load printings: ${
                  variantsQuery.error instanceof Error
                    ? variantsQuery.error.message
                    : String(variantsQuery.error)
                }`}
          </p>
        )}

        {/* Rendered once the list loads even when it's empty: a card with
            only digital printings needs "Show digital" reachable. */}
        {variantsQuery.isSuccess && (
          <div className="printing-grid-layout">
            {selected && (
              <PrintingPreview
                key={selected.scryfall_id}
                v={selected}
                face={faceOf(selected)}
                current={isCurrent(selected)}
                coverage={coverageOf(selected)}
                origin={origin}
                showImages={showImages}
                settleMs={settleMs}
                onFlip={() => flip(selected)}
                onPick={() => pick(selected)}
              />
            )}
            <div className="printing-grid-main">
              {/* Above the grid, right-aligned, the count first. */}
              <div className="printing-grid-controls">
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
                    label: `${cardLang.toUpperCase()} + Exclusives`,
                    title: `${LANGUAGE_NAMES[cardLang] ?? cardLang.toUpperCase()} printings, plus printings that only exist in another language`,
                    active: langDefault,
                    onSelect: () => setLangDefault(true),
                  }}
                />
                <select
                  aria-label="Sort"
                  value={sortMode}
                  disabled={!ready}
                  onChange={(e) => setSortMode(e.target.value as SortMode)}
                >
                  <option value="newest">Newest first</option>
                  <option value="oldest">Oldest first</option>
                  <option value="set">By set</option>
                </select>
                <label className="check">
                  <input
                    type="checkbox"
                    checked={includeDigital}
                    onChange={(e) => setShowDigital.mutate(e.target.checked)}
                  />
                  Show digital
                </label>
              </div>
              {/* Mounted for as long as the grid is, even when the filters
                  match nothing: the tile observer is rooted on this element,
                  and a remounted one would leave it watching a detached node
                  (every tile after that would stay blank). */}
              <div className="modal-body" ref={scrollRef}>
                {shown.length === 0 ? (
                  <p className="hint">No printings match this filter.</p>
                ) : (
                  /* No virtualisation: ~870 tiles (a basic land) is well
                     within what a plain CSS grid handles once thumbnails
                     load only near the view (useTileVisibility). */
                  <div className="printing-grid" ref={gridRef} onKeyDown={onGridKeyDown}>
                    {shown.map((v, i) => (
                      <PrintingTile
                        key={v.scryfall_id}
                        v={v}
                        current={isCurrent(v)}
                        selected={v.scryfall_id === selectedId}
                        coverage={coverageOf(v)}
                        face={faceOf(v)}
                        origin={origin}
                        showImages={showImages}
                        tabIndex={i === clampedFocus ? 0 : -1}
                        observe={observeTile}
                        buttonRef={(el) => {
                          tileRefs.current[i] = el;
                        }}
                        onFocus={() => {
                          setFocusIndex(i);
                          setSelectedId(v.scryfall_id);
                        }}
                        onSelect={() => {
                          // WKWebView doesn't focus a button on click, so
                          // selection can't rely on onFocus alone.
                          setSettleMs(0);
                          setSelectedId(v.scryfall_id);
                          setFocusIndex(i);
                          tileRefs.current[i]?.focus();
                        }}
                        onPick={() => pick(v)}
                        onFlip={() => flip(v)}
                      />
                    ))}
                  </div>
                )}
              </div>
            </div>
          </div>
        )}
      </div>
    </ModalOverlay>
  );
}

export default function PrintingPicker(props: {
  card: CardRow;
  /** The project's import-language preference — the language filter's
   *  default, and the language of the current printing, for a card row
   *  that doesn't carry its own. */
  preferredLang: string;
  /** True when the generation server is unreachable — the button still
   *  renders the printing, it just can't open the picker. */
  disabled: boolean;
  onPick: (printing: Pick) => void;
  /** The grid closed, picked or not. Its previews may have downloaded
   *  originals the deck should now adopt. */
  onClosed?: () => void;
}) {
  const { card, preferredLang, disabled, onPick, onClosed } = props;
  const [open, setOpen] = useState(false);

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
        onClick={() => setOpen(true)}
      >
        {label} ▾
      </button>
      {open && (
        <PrintingGridModal
          card={card}
          preferredLang={preferredLang}
          onClose={() => {
            setOpen(false);
            onClosed?.();
          }}
          onPick={onPick}
        />
      )}
    </span>
  );
}
