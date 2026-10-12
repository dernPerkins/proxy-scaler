// The change-printing control: a card row's set/collector rendered as a
// button that opens a modal grid of card images, one tile per printing of
// the card (variants share an oracle_id — served by GET /api/cards/variants
// from the server's locally-imported Scryfall corpus; no live Scryfall call,
// and a deliberate "import the card database first" hint when there is no
// corpus). Tile images come from the generation server's thumbnail endpoint
// (GET /api/cards/{id}/thumb), never from Scryfall directly, so "the client
// never calls Scryfall" still holds. Picking a tile pins the card to that
// exact printing via ProjectContext.setCardPrinting; nothing downloads until
// Generate.
import { useEffect, useMemo, useRef, useState, type KeyboardEvent } from "react";
import { useMutation, useQuery, useQueryClient } from "@tanstack/react-query";
import { generationApi, ApiError } from "../api/generation";
import { projectApi } from "../api/project";
import { serverSupportsThumbnails, THUMBNAILS_MIN_SERVER_VERSION, useServerVersion } from "../config";
import { useProject } from "../context/ProjectContext";
import { useImageCacheSetting } from "../imageOrigin";
import type { CardRow } from "../api/project";
import type { CardVariant, GeneratedPair, ImageOrigin } from "../api/types";
import ModalOverlay from "./ModalOverlay";

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

function matchesText(v: CardVariant, query: string): boolean {
  const q = query.trim().toLowerCase();
  if (!q) return true;
  return [v.set_code, v.set_name, v.printed_name].some((field) =>
    field?.toLowerCase().includes(q),
  );
}

// How many tiles one row of the grid holds, read off the laid-out CSS grid
// (auto-fill columns change with the window width).
function gridColumns(grid: HTMLElement | null): number {
  if (!grid) return 1;
  const columns = getComputedStyle(grid).gridTemplateColumns.split(" ").filter(Boolean);
  return Math.max(1, columns.length);
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

function PrintingTile(props: {
  v: CardVariant;
  current: boolean;
  coverage: Coverage;
  /** null: the setting hasn't loaded yet, so no image is requested. */
  origin: ImageOrigin | null;
  showImages: boolean;
  tabIndex: number;
  buttonRef: (el: HTMLButtonElement | null) => void;
  onFocus: () => void;
  onPick: () => void;
}) {
  const { v, current, coverage, origin, showImages } = props;
  const faceCount = v.face_count ?? 1;
  const [face, setFace] = useState(0);
  const [failedFaces, setFailedFaces] = useState<ReadonlySet<number>>(new Set());
  const faceNames = v.face_names ?? [];
  const displayName = v.printed_name ?? v.name;
  const faceName =
    faceCount > 1 ? (faceNames[face] ?? displayName) : displayName;
  const otherFace = face === 0 ? 1 : 0;

  let image;
  if (!showImages || failedFaces.has(face)) {
    image = <TilePlaceholder v={v} faceName={faceName} />;
  } else if (origin == null) {
    image = null; // the empty frame, for the moment before the setting loads
  } else {
    image = (
      <img
        key={face}
        src={generationApi.cardThumbUrl(v.scryfall_id, face, origin)}
        alt=""
        loading="lazy"
        decoding="async"
        draggable={false}
        onError={() => setFailedFaces((prev) => new Set(prev).add(face))}
      />
    );
  }

  return (
    <div
      className={`printing-tile${current ? " active" : ""}${faceCount > 1 ? " has-flip" : ""}`}
    >
      <button
        type="button"
        ref={props.buttonRef}
        className="printing-tile-pick"
        tabIndex={props.tabIndex}
        onFocus={props.onFocus}
        onClick={props.onPick}
        aria-current={current ? "true" : undefined}
        title={`${displayName} — ${v.set_name ?? v.set_code.toUpperCase()}${variantYear(v)}`}
      >
        <span className="printing-tile-frame">{image}</span>
        <span className="printing-tile-label">
          <span className="mono">
            {v.set_code.toUpperCase()} #{v.collector_number} · {v.lang.toUpperCase()}
          </span>
          {v.digital && <span className="printing-tile-tag">digital</span>}
          {coverage !== "none" && (
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
          )}
        </span>
      </button>
      {faceCount > 1 && (
        <button
          type="button"
          className="printing-tile-flip btn-sm"
          // Out of the tab order: arrow keys walk the tiles, and the flip
          // stays one click away for a pointer.
          tabIndex={-1}
          title={`Show ${faceNames[otherFace] ?? (otherFace === 1 ? "back" : "front")}`}
          aria-label="Flip card"
          onClick={() => setFace(otherFace)}
        >
          ⟲ Flip
        </button>
      )}
    </div>
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
  const [langFilter, setLangFilter] = useState(cardLang);
  const [textFilter, setTextFilter] = useState("");
  const [sortMode, setSortMode] = useState<SortMode>("newest");
  const serverVersion = useServerVersion();
  const showImages = serverSupportsThumbnails(serverVersion);
  const { origin } = useImageCacheSetting();

  useEffect(() => {
    function onKeyDown(e: globalThis.KeyboardEvent) {
      if (e.key === "Escape") onClose();
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
  const languages = useMemo(
    () => Array.from(new Set(variants.map((v) => v.lang))),
    [variants],
  );
  // The preferred filter language may simply not exist among this card's
  // printings — fall back to showing everything rather than an empty grid.
  const effectiveLang = languages.includes(langFilter) ? langFilter : null;
  const shown = useMemo(
    () =>
      sortVariants(
        variants.filter(
          (v) => (effectiveLang == null || v.lang === effectiveLang) && matchesText(v, textFilter),
        ),
        sortMode,
      ),
    [variants, effectiveLang, textFilter, sortMode],
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

  // Keyboard: one tile is in the tab order at a time (a roving tabindex);
  // arrow keys move it in grid order and Enter clicks the focused tile.
  const gridRef = useRef<HTMLDivElement>(null);
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
    moveFocus(clampedFocus + delta);
  }

  // On open, the current printing is focused and scrolled into view,
  // once, the first time the grid has tiles to show.
  const placedInitialFocus = useRef(false);
  useEffect(() => {
    if (placedInitialFocus.current || shown.length === 0) return;
    placedInitialFocus.current = true;
    const index = Math.max(0, shown.findIndex(isCurrent));
    setFocusIndex(index);
    const tile = tileRefs.current[index];
    tile?.scrollIntoView({ block: "center" });
    tile?.focus({ preventScroll: true });
    // isCurrent reads only props that are fixed while the grid is open.
  }, [shown]);

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

        <div className="printing-grid-controls">
          <input
            type="search"
            placeholder="Filter by set or name"
            aria-label="Filter by set code, set name or printed name"
            value={textFilter}
            disabled={!ready}
            onChange={(e) => setTextFilter(e.target.value)}
          />
          <select
            aria-label="Language"
            value={ready ? (effectiveLang ?? "all") : "none"}
            onChange={(e) => setLangFilter(e.target.value)}
            disabled={!ready || languages.length <= 1}
          >
            {!ready && <option value="none">—</option>}
            {ready && effectiveLang == null && <option value="all">All languages</option>}
            {ready &&
              languages.map((lang) => (
                <option key={lang} value={lang}>
                  {lang.toUpperCase()}
                </option>
              ))}
          </select>
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
              disabled={!ready}
              onChange={(e) => setShowDigital.mutate(e.target.checked)}
            />
            Show digital
          </label>
          {ready && (
            <span className="printing-grid-count">
              {shown.length} of {variants.length}
            </span>
          )}
        </div>

        {ready && !showImages && (
          <p className="hint">
            Previews need a newer generation server (v{THUMBNAILS_MIN_SERVER_VERSION} or later).
            You can still pick a printing.
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
        {variantsQuery.isSuccess && shown.length === 0 && (
          <p className="hint">No printings match this filter.</p>
        )}

        {shown.length > 0 && (
          <div className="modal-body">
            {/* No virtualisation: ~870 lazily loaded tiles (a basic land)
                is well within what a plain CSS grid handles. */}
            <div className="printing-grid" ref={gridRef} onKeyDown={onGridKeyDown}>
              {shown.map((v, i) => (
                <PrintingTile
                  key={v.scryfall_id}
                  v={v}
                  current={isCurrent(v)}
                  coverage={variantCoverage(v, statuses?.[v.scryfall_id], dpis)}
                  origin={origin}
                  showImages={showImages}
                  tabIndex={i === clampedFocus ? 0 : -1}
                  buttonRef={(el) => {
                    tileRefs.current[i] = el;
                  }}
                  onFocus={() => setFocusIndex(i)}
                  onPick={() => pick(v)}
                />
              ))}
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
   *  default when the card itself doesn't carry one. */
  preferredLang: string;
  /** True when the generation server is unreachable — the button still
   *  renders the printing, it just can't open the picker. */
  disabled: boolean;
  onPick: (printing: Pick) => void;
}) {
  const { card, preferredLang, disabled, onPick } = props;
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
          onClose={() => setOpen(false)}
          onPick={onPick}
        />
      )}
    </span>
  );
}
