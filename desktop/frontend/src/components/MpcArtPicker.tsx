// Browsing MPC Fill community art: a search box, a few filters, a grid of
// previews and a preview pane with the button that picks one.
//
// Everything comes from the generation server (api/routers/mpcfill.py):
// the search (its downloaded MPC Fill catalog, never mpcfill.com), the
// previews (Google Drive, cached on the server's disk) and, when a pick is
// made, the download. The client itself never contacts MPC Fill or Google.
// With no catalog downloaded the browser offers the download instead of
// searching (CatalogNeeded).
//
// MpcArtBrowser fills a `.modal.printing-grid-modal` — the same layout as
// the change-printing grid, preview column on the left — so it works both
// in its own modal (MpcArtModal, used by the Customs and Backs tabs) and
// inside the change-printing modal's MPC Fill mode.
import {
  useEffect,
  useLayoutEffect,
  useMemo,
  useRef,
  useState,
  type KeyboardEvent,
  type ReactNode,
} from "react";
import { useQuery, useQueryClient } from "@tanstack/react-query";
import { ApiError, generationApi } from "../api/generation";
import { projectApi } from "../api/project";
import type { MpcCard, MpcCardType } from "../api/types";
import ModalOverlay from "./ModalOverlay";
import { LoadingGrid, LoadingStage } from "./PickerLoading";
import { fillFrameWith, trimGeometry } from "../cardTrim";
import { MPC_IMPORT_BLEED_MM } from "../mpcFill";
import { setMpcCatalogJobId, useMpcCatalogReady } from "../mpcCatalog";
import { errorText as toastErrorText, toast } from "../toasts";
import SelectMenu from "./SelectMenu";
import TutorialButton from "../tutorial/TutorialButton";
import { useTourWhenPresent } from "../tutorial/tutorialStore";
import type { TourId } from "../tutorial/tours";
import {
  KEYBOARD_PREVIEW_SETTLE_MS,
  gridColumns,
  useQueuedThumb,
  useThumbnailQueue,
  useTileVisibility,
  type ThumbnailQueue,
} from "./imageGrid";

// How many previews are hydrated (name, uploader, DPI) at a time as the
// grid scrolls; the search response carries the first page.
const PAGE = 60;
// How many the search response itself hydrates (api/routers/mpcfill.py
// FIRST_PAGE).
const FIRST_HYDRATED = 60;

// MPC Fill's languages (GET /2/languages/), and the Scryfall codes that
// map onto them for a card's own default.
export const MPC_LANGUAGES: { code: string; name: string }[] = [
  { code: "EN", name: "English" },
  { code: "ES", name: "Spanish" },
  { code: "FR", name: "French" },
  { code: "DE", name: "German" },
  { code: "IT", name: "Italian" },
  { code: "PT", name: "Portuguese" },
  { code: "JA", name: "Japanese" },
  { code: "RU", name: "Russian" },
  { code: "ZH", name: "Chinese" },
  { code: "AR", name: "Arabic" },
  { code: "SA", name: "Sanskrit" },
];
const SCRYFALL_TO_MPC: Record<string, string> = { zhs: "ZH", zht: "ZH" };

function mpcLanguage(code: string | null | undefined): string {
  if (!code) return "";
  const upper = SCRYFALL_TO_MPC[code.toLowerCase()] ?? code.toUpperCase();
  return MPC_LANGUAGES.some((l) => l.code === upper) ? upper : "";
}

const TYPE_LABELS: Record<MpcCardType, string> = {
  CARD: "Cards",
  TOKEN: "Tokens",
  CARDBACK: "Card backs",
};

const MIN_DPI_OPTIONS = [
  { value: "0", label: "Any DPI" },
  { value: "300", label: "300+ DPI" },
  { value: "600", label: "600+ DPI" },
  { value: "800", label: "800+ DPI" },
  { value: "1200", label: "1200+ DPI" },
];

interface Submitted {
  q: string;
  scryfallId: string | null;
  face: number;
  /** null until known: a search by card lets the server say CARD or TOKEN. */
  type: MpcCardType | null;
  /** The language filter as chosen: "any", "card" or an MPC Fill code.
   *  null follows the remembered default (getMpcFillLanguage). */
  langChoice: string | null;
  minDpi: number;
  nsfw: boolean;
  fuzzy: boolean;
  /** One uploader's art only; "" for everyone. */
  source: string;
}

function sizeLabel(bytes: number): string {
  if (bytes <= 0) return "";
  return bytes >= 1024 * 1024
    ? `${(bytes / (1024 * 1024)).toFixed(1)} MB`
    : `${Math.round(bytes / 1024)} KB`;
}

// MakePlayingCards' template, the shape almost all MPC Fill art has:
// assumed until a preview has loaded and said otherwise.
const MPC_TEMPLATE_SIZE = { w: 816, h: 1110 };

/** Where to draw a tile's image so only the card as cut shows. Every MPC
 *  Fill file is trimmed, even one shaped like a bare card: MakePlayingCards
 *  stretches any upload onto its bled template and trims it, and uploaders
 *  design for that (see the server's /api/mpcfill/import). */
function mpcTrimStyle(natural: { w: number; h: number } | null) {
  const shape = natural ?? MPC_TEMPLATE_SIZE;
  return fillFrameWith(trimGeometry(shape, MPC_IMPORT_BLEED_MM).trim);
}

function MpcTile(props: {
  card: MpcCard;
  selected: boolean;
  current: boolean;
  /** The selected art, whose thumbnail loads before the rest. */
  first: boolean;
  tabIndex: number;
  queue: ThumbnailQueue;
  buttonRef: (el: HTMLButtonElement | null) => void;
  onFocus: () => void;
  onSelect: () => void;
  onPick: () => void;
}) {
  const { card, selected, current } = props;
  const frameRef = useRef<HTMLSpanElement>(null);
  // The thumbnail waits its turn: the selection first, then what's in
  // view, then the margin (imageGrid.ts).
  const { load, settle } = useQueuedThumb(props.queue, frameRef, true, props.first);
  const [failed, setFailed] = useState(false);
  // MPC Fill art carries its bleed; the tile shows the card as cut.
  const [natural, setNatural] = useState<{ w: number; h: number } | null>(null);
  const trimmed = mpcTrimStyle(natural);
  return (
    <div className={`printing-tile${current ? " active" : ""}${selected ? " selected" : ""}`}>
      <button
        type="button"
        ref={props.buttonRef}
        className="printing-tile-pick"
        tabIndex={props.tabIndex}
        onFocus={props.onFocus}
        onClick={props.onSelect}
        onDoubleClick={props.onPick}
        onKeyDown={(e) => {
          if (e.key !== "Enter") return;
          e.preventDefault();
          props.onPick();
        }}
        aria-pressed={selected}
        title={`${card.name} — ${card.source_name}`}
      >
        <span className="printing-tile-frame mpc-tile-frame" ref={frameRef}>
          {failed ? (
            <span className="printing-tile-placeholder">
              <span className="printing-tile-placeholder-name">{card.name}</span>
              <span className="printing-set-name">{card.source_name}</span>
            </span>
          ) : load ? (
            <img
              src={generationApi.mpcThumbUrl(card.drive_id, "small")}
              alt=""
              decoding="async"
              draggable={false}
              className={trimmed ? "mpc-trimmed" : undefined}
              style={trimmed ?? undefined}
              onLoad={(e) => {
                settle();
                setNatural({ w: e.currentTarget.naturalWidth, h: e.currentTarget.naturalHeight });
              }}
              onError={() => {
                settle();
                setFailed(true);
              }}
            />
          ) : null}
        </span>
        <span className="mpc-tile-name">{card.name}</span>
        <span className="printing-tile-label">
          <span className="mpc-tile-source">{card.source_name}</span>
          <span className="mpc-tile-dpi mono">{card.dpi} DPI</span>
        </span>
      </button>
    </div>
  );
}

function MpcPreview(props: {
  card: MpcCard;
  current: boolean;
  pickLabel: string;
  currentLabel: string;
  busy: boolean;
  error: string | null;
  /** 0 after a click; KEYBOARD_PREVIEW_SETTLE_MS after an arrow key. */
  settleMs: number;
  onPick: () => void;
}) {
  const { card, settleMs } = props;
  const smallUrl = generationApi.mpcThumbUrl(card.drive_id, "small");
  const largeUrl = generationApi.mpcThumbUrl(card.drive_id, "large");
  const [requested, setRequested] = useState(false);
  const [largeLoaded, setLargeLoaded] = useState(false);
  const [largeFailed, setLargeFailed] = useState(false);
  // Shown as cut, like the tiles: the bleed is cropped off.
  const [natural, setNatural] = useState<{ w: number; h: number } | null>(null);
  const trimmed = mpcTrimStyle(natural);
  useEffect(() => {
    const timer = setTimeout(() => setRequested(true), settleMs);
    return () => clearTimeout(timer);
  }, [settleMs]);

  // The size is the full file's, which downloads only once picked: the
  // pane shows Google's 800 px preview, so browsing downloads nothing.
  const details = [
    card.source_name,
    `${card.dpi} DPI`,
    sizeLabel(card.size_bytes),
    card.language,
    card.extension.toUpperCase(),
  ].filter(Boolean);

  return (
    <aside className="printing-preview" aria-label="Selected art">
      <div className="printing-preview-wash" style={{ backgroundImage: `url("${smallUrl}")` }} />
      <div className="printing-preview-stage">
        <div className="printing-preview-frame">
          <img
            src={smallUrl}
            alt=""
            draggable={false}
            className={trimmed ? "mpc-trimmed" : undefined}
            style={trimmed ?? undefined}
            onLoad={(e) =>
              setNatural({ w: e.currentTarget.naturalWidth, h: e.currentTarget.naturalHeight })
            }
          />
          {requested && !largeFailed && (
            <img
              className={`printing-preview-full${largeLoaded ? " loaded" : ""}${
                trimmed ? " mpc-trimmed" : ""
              }`}
              style={trimmed ?? undefined}
              src={largeUrl}
              alt={card.name}
              draggable={false}
              onLoad={() => setLargeLoaded(true)}
              onError={() => setLargeFailed(true)}
            />
          )}
        </div>
      </div>
      <div className="printing-preview-scrim" data-tour="mpc-picker-preview">
        <h3 className="printing-preview-name">{card.name}</h3>
        <p className="printing-preview-meta">
          {details.join(" · ")}
          {props.current ? " · Current" : ""}
        </p>
        <div className="printing-preview-versions">
          {card.tags.map((tag) => (
            <span key={tag} className="mpc-tag">
              {tag}
            </span>
          ))}
        </div>
        {props.error && <p className="printing-preview-meta mpc-pick-error">{props.error}</p>}
        <button
          type="button"
          className="printing-preview-cta"
          disabled={props.busy}
          onClick={props.onPick}
        >
          {props.busy ? "Downloading…" : props.current ? props.currentLabel : props.pickLabel}
        </button>
      </div>
    </aside>
  );
}

/** What the picker searches for first: a name, or a corpus card (and face)
 *  whose name — and CARD vs TOKEN — the server works out, and optionally
 *  the filters to start from (the MPC Fill tab hands over its own). */
/** In place of results while the server has no MPC Fill catalog: MPC
 *  Fill art search works from it alone, so offer the download. The import
 *  itself runs under MpcCatalogPrompt's blocking modal, and an import
 *  refreshes the catalog status, so search starts by itself afterwards. */
function CatalogNeeded({ importing, known }: { importing: boolean; known: boolean }) {
  const [starting, setStarting] = useState(false);
  if (!known) return <p className="hint">Checking for the MPC Fill Catalog…</p>;
  if (importing) return <p className="hint">Downloading the MPC Fill Catalog…</p>;
  return (
    <div className="mpc-catalog-needed">
      <p>MPC Fill art search uses the MPC Fill Catalog, downloaded once to this server.</p>
      <button
        type="button"
        className="btn-primary"
        disabled={starting}
        onClick={() => {
          setStarting(true);
          generationApi
            .startMpcCatalogImport()
            .then(({ job_id }) => setMpcCatalogJobId(job_id))
            .catch((err) =>
              toast(`Couldn't start the download: ${toastErrorText(err)}`, "err", { key: "mpc-catalog" }),
            )
            .finally(() => setStarting(false));
        }}
      >
        {starting ? "Starting…" : "Download MPC Fill Catalog"}
      </button>
    </div>
  );
}

export interface MpcPickerInitial {
  query?: string;
  scryfallId?: string | null;
  face?: number;
  type?: MpcCardType;
  /** An MPC Fill language code; for this search only, not remembered. */
  langChoice?: string;
  minDpi?: number;
  nsfw?: boolean;
  /** Start with Fuzzy on: a search typed by hand (the MPC Fill tab)
   *  rather than one made from a card's exact name. */
  fuzzy?: boolean;
  /** Start filtered to one uploader. */
  source?: string;
}

export interface MpcArtBrowserProps {
  initial: MpcPickerInitial;
  /** The card types offered; the first is the default. CARDBACK alone
   *  browses every back until a name is typed. */
  cardTypes: MpcCardType[];
  /** Scryfall or MPC Fill language code to filter by at first; nothing
   *  (or a language MPC Fill doesn't have) means any. */
  defaultLang?: string | null;
  /** The modal head's contents (Close, a mode switch ...). */
  head: ReactNode;
  pickLabel: string;
  /** Shown on the button when the selection is already `currentDriveId`. */
  currentLabel?: string;
  currentDriveId?: string | null;
  /** Called with the chosen art; throws with a user-facing message. */
  onPick: (card: MpcCard) => Promise<void>;
  /** Makes selecting choose (the Decklist's deck walk): called for every
   *  selection, by click or arrow key, and `currentDriveId` should follow
   *  it, so the chosen tile shows only as current. */
  onSelect?: (card: MpcCard) => void;
  /** The base tour that must be done before this picker's own
   *  ("mpc-picker") auto-shows: the screen it was opened from. */
  tourAfter: TourId;
}

export function MpcArtBrowser(props: MpcArtBrowserProps) {
  const { initial, cardTypes, head, pickLabel, currentDriveId } = props;
  const [draft, setDraft] = useState(initial.query ?? "");
  const [submitted, setSubmitted] = useState<Submitted>(() => ({
    q: initial.query ?? "",
    scryfallId: initial.query ? null : (initial.scryfallId ?? null),
    face: initial.face ?? 0,
    type:
      initial.type && cardTypes.includes(initial.type)
        ? initial.type
        : initial.query || !initial.scryfallId
          ? cardTypes[0]
          : null,
    langChoice: initial.langChoice ?? null,
    minDpi: initial.minDpi ?? 0,
    nsfw: initial.nsfw ?? false,
    fuzzy: initial.fuzzy ?? false,
    source: initial.source ?? "",
  }));
  const browsing = submitted.type === "CARDBACK" && !submitted.q;
  const canSearch = Boolean(submitted.q || submitted.scryfallId || submitted.type === "CARDBACK");

  // The language filter starts where it was last left, app-wide (like the
  // printing grid's Show Digital). "card" means the card's own language,
  // which only the change-printing modal has; elsewhere it reads as any.
  const queryClient = useQueryClient();
  const hasCard = props.defaultLang !== undefined;
  const savedLanguage = useQuery({
    queryKey: ["mpc-fill-language"],
    queryFn: () => projectApi.getMpcFillLanguage(),
    staleTime: Infinity,
  });
  const languageReady = savedLanguage.isFetched;
  const langChoice = submitted.langChoice ?? savedLanguage.data ?? "any";
  const shownChoice = langChoice === "card" && !hasCard ? "any" : langChoice;
  const lang =
    shownChoice === "any"
      ? ""
      : shownChoice === "card"
        ? mpcLanguage(props.defaultLang)
        : mpcLanguage(shownChoice);
  const languageName = MPC_LANGUAGES.find((l) => l.code === lang)?.name ?? lang;
  const cardLanguageName = hasCard
    ? MPC_LANGUAGES.find((l) => l.code === mpcLanguage(props.defaultLang))?.name
    : undefined;

  function chooseLanguage(choice: string) {
    submit({ langChoice: choice });
    queryClient.setQueryData(["mpc-fill-language"], choice);
    void projectApi.setMpcFillLanguage(choice).catch(() => {});
  }

  const catalog = useMpcCatalogReady();

  // Searches run when asked (open, Search, a filter change), never per
  // keystroke; each distinct search is one cached request on the server.
  const search = useQuery({
    queryKey: ["mpc-search", submitted, lang],
    queryFn: () =>
      generationApi.mpcSearch({
        q: submitted.q || undefined,
        scryfall_id: submitted.q ? undefined : (submitted.scryfallId ?? undefined),
        face: submitted.face,
        type: submitted.type ?? undefined,
        lang: lang || undefined,
        min_dpi: submitted.minDpi || undefined,
        nsfw: submitted.nsfw || undefined,
        fuzzy: submitted.fuzzy || undefined,
        source: submitted.source || undefined,
      }),
    // After the remembered language has loaded, so the first search is
    // the right one rather than one that is immediately replaced; and only
    // with a catalog to search.
    enabled: canSearch && languageReady && catalog.ready,
    staleTime: Infinity,
    retry: false,
  });

  // A search with nothing to show yet gets the loading stage and grid.
  const loading = catalog.ready && (!languageReady || (search.isFetching && !search.data));
  const loadingLabel = "Searching MPC Fill…";

  // A search by card fills the box with the name the server searched for.
  useEffect(() => {
    if (search.data && !submitted.q && submitted.scryfallId) setDraft(search.data.query);
  }, [search.data, submitted.q, submitted.scryfallId]);

  const ids = useMemo(() => search.data?.ids ?? [], [search.data]);
  const [cards, setCards] = useState<ReadonlyMap<string, MpcCard>>(new Map());
  const [pages, setPages] = useState(1);
  const [inFlight, setInFlight] = useState(0);
  const hydrating = inFlight > 0;
  const [hydrateError, setHydrateError] = useState<string | null>(null);
  // Ids already asked for, so each is requested once: one MPC Fill no
  // longer knows comes back missing and must not be asked for again.
  const requested = useRef(new Set<string>());
  const searchData = useRef(search.data);
  useEffect(() => {
    searchData.current = search.data;
    requested.current = new Set((search.data?.ids ?? []).slice(0, FIRST_HYDRATED));
    setCards(new Map((search.data?.cards ?? []).map((c) => [c.drive_id, c])));
    setPages(1);
    setHydrateError(null);
  }, [search.data]);

  // Hydrate the page the grid has scrolled to.
  useEffect(() => {
    const wanted = ids.slice(0, pages * PAGE).filter((id) => !requested.current.has(id));
    if (wanted.length === 0 || hydrateError) return;
    for (const id of wanted) requested.current.add(id);
    const forSearch = searchData.current;
    setInFlight((n) => n + 1);
    generationApi
      .mpcCards(wanted)
      .then((resp) => {
        if (searchData.current !== forSearch) return;
        setCards((prev) => {
          const next = new Map(prev);
          for (const card of resp.cards) next.set(card.drive_id, card);
          return next;
        });
      })
      .catch((err: unknown) => {
        if (searchData.current !== forSearch) return;
        for (const id of wanted) requested.current.delete(id);
        setHydrateError(err instanceof Error ? err.message : String(err));
      })
      // Always, even for a search since replaced: this count is the
      // spinner, and a request that never decrements it is a spinner
      // that never stops.
      .finally(() => setInFlight((n) => n - 1));
  }, [ids, pages, hydrateError]);

  const shown = useMemo(
    () =>
      ids
        .slice(0, pages * PAGE)
        .map((id) => cards.get(id))
        .filter((c): c is MpcCard => c != null),
    [ids, pages, cards],
  );

  const [selectedId, setSelectedId] = useState<string | null>(null);
  const selected = shown.find((c) => c.drive_id === selectedId) ?? null;
  // The picker's first-open tour, once the grid and the preview are filled
  // in. Without a catalog the grid is the download offer, so it waits.
  useTourWhenPresent("mpc-picker", catalog.ready && !loading && selected != null, props.tourAfter);
  const [settleMs, setSettleMs] = useState(0);
  const [busy, setBusy] = useState(false);
  const [pickError, setPickError] = useState<string | null>(null);

  async function pick(card: MpcCard) {
    if (busy) return;
    setSelectedId(card.drive_id);
    setBusy(true);
    setPickError(null);
    try {
      await props.onPick(card);
    } catch (err) {
      setPickError(err instanceof Error ? err.message : String(err));
    } finally {
      setBusy(false);
    }
  }

  const scrollRef = useRef<HTMLDivElement>(null);
  const gridRef = useRef<HTMLDivElement>(null);
  const thumbQueue = useThumbnailQueue(scrollRef);
  // Only for the paging sentinel below: it loads no image.
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

  // Each new result list starts on its first (or the current) art.
  useLayoutEffect(() => {
    if (shown.length === 0) return;
    if (selectedId && shown.some((c) => c.drive_id === selectedId)) return;
    const index = Math.max(0, shown.findIndex((c) => c.drive_id === currentDriveId));
    setFocusIndex(index);
    setSelectedId(shown[index].drive_id);
    // Only when the list itself changes.
  }, [shown.length === 0 ? null : shown[0].drive_id]);

  // Reopened on a card whose art was chosen further down the results (the
  // deck walk's Prev / Next), the grid opens on that art: load the pages
  // through it, select it once it arrives, then scroll it into view —
  // once per result list. Selection first and the scroll on the next
  // pass: selecting mounts the preview pane, which narrows the grid and
  // reflows every tile, so scrolling in the same pass lands on the wrong
  // row (PrintingPicker does the same).
  useEffect(() => {
    const at = currentDriveId ? ids.indexOf(currentDriveId) : -1;
    if (at >= pages * PAGE) setPages(Math.ceil((at + 1) / PAGE));
    // Only to reach the chosen art; scrolling pages in further is the
    // sentinel's job.
  }, [ids, currentDriveId]);
  const placedFor = useRef<unknown>(null);
  useLayoutEffect(() => {
    if (!currentDriveId || placedFor.current === search.data) return;
    const index = shown.findIndex((c) => c.drive_id === currentDriveId);
    if (index < 0) return;
    if (selectedId !== currentDriveId) {
      setFocusIndex(index);
      setSelectedId(currentDriveId);
      return;
    }
    placedFor.current = search.data;
    const tile = tileRefs.current[index];
    tile?.scrollIntoView({ block: "center" });
    tile?.focus({ preventScroll: true });
  }, [shown, currentDriveId, selectedId, search.data]);

  // The end of the grid: once it nears the view, hydrate the next page.
  const sentinelRef = useRef<HTMLDivElement>(null);
  const hasMore = pages * PAGE < ids.length;
  useEffect(() => {
    if (!hasMore || hydrating || !sentinelRef.current) return;
    return observeTile(sentinelRef.current, () => setPages((p) => p + 1));
  }, [hasMore, hydrating, observeTile, shown.length]);

  // Who has art for this search, with counts (the server lists them before
  // its uploader filter, so the choice can always change). The chosen one
  // stays listed even when this search has none of theirs.
  const uploaders = search.data?.uploaders ?? [];
  const everyone = uploaders.reduce((sum, u) => sum + u.count, 0);
  const uploaderOptions = [
    { value: "", label: search.data ? `Any uploader (${everyone})` : "Any uploader" },
    ...uploaders.map((u) => ({ value: u.name, label: `${u.name} (${u.count})` })),
    ...(submitted.source && !uploaders.some((u) => u.name === submitted.source)
      ? [{ value: submitted.source, label: `${submitted.source} (0)` }]
      : []),
  ];

  function submit(patch: Partial<Submitted> = {}) {
    const q = draft.trim();
    setSubmitted((prev) => {
      // Still the card's own name: keep searching by card. Edited: search
      // that text, as the type the server found the card to be.
      const byCard = prev.scryfallId != null && !prev.q && search.data != null && q === search.data.query;
      return {
        ...prev,
        q: byCard ? "" : q,
        type: prev.type ?? search.data?.card_type ?? cardTypes[0],
        ...patch,
      };
    });
  }

  const errorText =
    search.error instanceof ApiError || search.error instanceof Error
      ? search.error.message
      : search.error
        ? String(search.error)
        : null;

  return (
    <>
      {loading ? (
        <LoadingStage label={loadingLabel} />
      ) : selected && (
        <MpcPreview
          key={selected.drive_id}
          card={selected}
          current={selected.drive_id === currentDriveId}
          pickLabel={pickLabel}
          currentLabel={props.currentLabel ?? pickLabel}
          busy={busy}
          error={pickError}
          settleMs={settleMs}
          onPick={() => void pick(selected)}
        />
      )}

      <div className="modal-head printing-grid-head">{head}</div>

      <div className="printing-grid-notes">
        {!canSearch && <p className="hint">Type a card name and press Search.</p>}
        {!loading && search.isFetching && <p className="hint">Searching MPC Fill…</p>}
        {errorText && <p className="hint">Couldn't search MPC Fill: {errorText}</p>}
        {hydrateError && <p className="hint">Couldn't load more results: {hydrateError}</p>}
      </div>

      <div className="printing-grid-controls mpc-controls" data-tour="mpc-picker-filters" data-tour-fit>
        <form
          className="mpc-search-form"
          onSubmit={(e) => {
            e.preventDefault();
            submit();
          }}
        >
          <input
            type="search"
            value={draft}
            placeholder={browsing ? "All card backs — or type a name" : "Card name"}
            aria-label="Search MPC Fill"
            onChange={(e) => setDraft(e.target.value)}
          />
          <button type="submit" className="btn-sm">
            Search
          </button>
        </form>
        {search.data && (
          <span className="printing-grid-count">
            {ids.length} {ids.length === 1 ? "result" : "results"}
          </span>
        )}
        {cardTypes.length > 1 && (
          <SelectMenu
            ariaLabel="Card type"
            options={cardTypes.map((t) => ({ value: t, label: TYPE_LABELS[t] }))}
            value={submitted.type ?? search.data?.card_type ?? cardTypes[0]}
            onChange={(value) => submit({ type: value as MpcCardType })}
          />
        )}
        <SelectMenu
          ariaLabel="Language"
          title="Remembered for every MPC Fill search"
          options={[
            { value: "any", label: "Any language" },
            ...(hasCard
              ? [
                  {
                    value: "card",
                    label: cardLanguageName ? `Card's language (${cardLanguageName})` : "Card's language",
                  },
                ]
              : []),
            ...MPC_LANGUAGES.map((l) => ({ value: l.code, label: l.name })),
          ]}
          value={shownChoice}
          onChange={chooseLanguage}
        />
        <SelectMenu
          ariaLabel="Minimum DPI"
          options={MIN_DPI_OPTIONS}
          value={String(submitted.minDpi)}
          onChange={(value) => submit({ minDpi: Number(value) })}
        />
        <SelectMenu
          ariaLabel="Uploader"
          searchable
          searchPlaceholder="Search uploaders"
          options={uploaderOptions}
          value={submitted.source}
          onChange={(value) => submit({ source: value })}
        />
        <label className="check" title="Looser name matching: finds more, including near misses">
          <input
            type="checkbox"
            checked={submitted.fuzzy}
            onChange={(e) => submit({ fuzzy: e.target.checked })}
          />
          Fuzzy
        </label>
        <label className="check" title="Include art MPC Fill tags as NSFW">
          <input
            type="checkbox"
            checked={submitted.nsfw}
            onChange={(e) => submit({ nsfw: e.target.checked })}
          />
          NSFW
        </label>
      </div>

      <div className="modal-body" ref={scrollRef} data-tour="mpc-picker-grid">
        {!catalog.ready ? (
          <CatalogNeeded importing={catalog.importing} known={catalog.known} />
        ) : loading ? (
          <LoadingGrid label={loadingLabel} />
        ) : search.isSuccess && ids.length === 0 && submitted.source ? (
          <p className="hint">
            No art from {submitted.source} for {search.data.query || "this search"}.{" "}
            <button type="button" className="linklike" onClick={() => submit({ source: "" })}>
              Show all uploaders
            </button>
          </p>
        ) : search.isSuccess && ids.length === 0 && lang ? (
          // Most MPC Fill art is English: an empty result under a language
          // filter is usually the filter, not the search.
          <p className="hint">
            No {languageName} MPC Fill art for {search.data.query || "this search"}.{" "}
            <button type="button" className="linklike" onClick={() => submit({ langChoice: "any" })}>
              Show all languages
            </button>
          </p>
        ) : search.isSuccess && ids.length === 0 ? (
          <p className="hint">
            No MPC Fill art matches
            {submitted.fuzzy ? "." : ". Try Fuzzy, or a shorter name."}
          </p>
        ) : (
          <div className="printing-grid" ref={gridRef} onKeyDown={onGridKeyDown}>
            {shown.map((card, i) => (
              <MpcTile
                key={card.drive_id}
                card={card}
                selected={
                  card.drive_id === selectedId &&
                  !(props.onSelect && card.drive_id === currentDriveId)
                }
                current={card.drive_id === currentDriveId}
                first={card.drive_id === selectedId}
                tabIndex={i === clampedFocus ? 0 : -1}
                queue={thumbQueue}
                buttonRef={(el) => {
                  tileRefs.current[i] = el;
                }}
                onFocus={() => {
                  setFocusIndex(i);
                  if (card.drive_id !== selectedId) props.onSelect?.(card);
                  setSelectedId(card.drive_id);
                  setPickError(null);
                }}
                onSelect={() => {
                  setSettleMs(0);
                  setSelectedId(card.drive_id);
                  setPickError(null);
                  setFocusIndex(i);
                  tileRefs.current[i]?.focus();
                  props.onSelect?.(card);
                }}
                onPick={() => void pick(card)}
              />
            ))}
          </div>
        )}
        {hasMore && <div ref={sentinelRef} className="mpc-sentinel" aria-hidden="true" />}
        {hydrating && <p className="hint">Loading more…</p>}
      </div>
    </>
  );
}

/** MpcArtBrowser in its own modal, for the Customs and Backs tabs. */
export function MpcArtModal(
  props: Omit<MpcArtBrowserProps, "head"> & { title: string; onClose: () => void },
) {
  const { title, onClose, ...browser } = props;
  useEffect(() => {
    function onKeyDown(e: globalThis.KeyboardEvent) {
      // A handled Esc (a dropdown closing) isn't the modal's.
      if (e.key === "Escape" && !e.defaultPrevented) onClose();
    }
    document.addEventListener("keydown", onKeyDown);
    return () => document.removeEventListener("keydown", onKeyDown);
  }, [onClose]);
  return (
    <ModalOverlay onClick={onClose}>
      <div
        className="modal printing-grid-modal mpc-art-modal"
        role="dialog"
        aria-label={title}
        onClick={(e) => e.stopPropagation()}
      >
        <MpcArtBrowser
          {...browser}
          head={
            <>
              <span className="mpc-head-title">{title}</span>
              {/* The tab bar's ? is behind this modal; replay from here. */}
              <TutorialButton tour="mpc-picker" />
              <button type="button" className="btn-sm" onClick={onClose}>
                Close
              </button>
            </>
          }
        />
      </div>
    </ModalOverlay>
  );
}
