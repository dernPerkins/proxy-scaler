// The Decklist tab's Add Cards area: a Search | List switch, the import
// language and Art From settings over one big field. Search lists matching
// cards as you type (Enter adds the highlighted card's default printing,
// ⌘Enter opens Card Search on it); List takes a pasted decklist. ⌘K opens
// a command palette that searches the same way and holds the other ways in
// (a pasted list, your own art, an MPC Fill order). Adding from files sits
// in the page's sidebar. Chosen from a prototype
// (.scratch/deck-add-prototype/).
import { useEffect, useRef, useState, type ReactNode } from "react";
import type { KeyboardEvent as ReactKeyboardEvent } from "react";
import { keepPreviousData, useQuery, useQueryClient } from "@tanstack/react-query";
import { ApiError, generationApi } from "../api/generation";
import type { CardRow, ImportArtSource, ResolvedImportCard } from "../api/project";
import type { CardSearchHit, CardVariant, MpcCard } from "../api/types";
import { useServerVersion } from "../config";
import { useProject } from "../context/ProjectContext";
import { useImageCacheSetting } from "../imageOrigin";
import { importMpcArt } from "../mpcFill";
import { registerCustomCards } from "../syncCustoms";
import { errorText, toast } from "../toasts";
import ModalOverlay from "./ModalOverlay";
import SelectMenu from "./SelectMenu";
import SplitPill from "./SplitPill";
import { PrintingGridModal, type PickerSource } from "./PrintingPicker";

/** The project's import settings, which searches and adds follow too. */
export interface AddCardsSettings {
  languages: string[];
  lang: string;
  setLang: (value: string) => void;
  /** "All Languages": match any language, best effort. */
  langAny: boolean;
  setLangAny: (value: boolean) => void;
  artSource: ImportArtSource;
  setArtSource: (value: ImportArtSource) => void;
  /** Why MPC Fill art can't be chosen, or null when it can. */
  mpcArtUnavailable: string | null;
}

interface AddCardsProps {
  /** The pasted decklist (the page owns it, so it outlives the tab). */
  draft: string;
  setDraft: (text: string) => void;
  onImport: () => void;
  canImport: boolean;
  importing: boolean;
  /** Why importing can't run, as the import button's tooltip. */
  importTitle?: string;
  /** Under the area: the import's progress, failed lines, and so on. */
  notes: ReactNode;
  onChooseFiles: () => void;
  onChooseOrder: () => void;
  orderDisabled: boolean;
  settings: AddCardsSettings;
  /** Rows a search or Card Search just added, for Art From: MPC Fill's
   *  automatic pick. */
  afterAdd: (added: CardRow[]) => void;
}

const LIST_PLACEHOLDER =
  "One card per line — 4 Card Name (set) 123\n1 Sol Ring (c21) 263\n4 Lightning Bolt";

export default function AddCards(props: AddCardsProps) {
  const { draft, settings } = props;
  const lang = settings.langAny ? null : settings.lang;
  const adds = useDeckAdds(props.afterAdd);
  const [cardSearchFor, setCardSearchFor] = useState<CardSearchHit | null | undefined>(undefined);
  const quick = useQuickAdd(lang, adds, (hit) => setCardSearchFor(hit));
  const [mode, setMode] = useState<"search" | "paste">(draft.trim() ? "paste" : "search");
  const [paletteOpen, setPaletteOpen] = useState(false);
  const fieldRef = useRef<HTMLInputElement>(null);
  useCmdK(() => setPaletteOpen(true));
  const lineCount = countLines(draft);

  // A list that imports in full flips back to Search, ready for the next
  // card. Lines that failed stay in the box (listed under it) to fix.
  const wasImporting = useRef(false);
  useEffect(() => {
    if (wasImporting.current && !props.importing && !draft.trim()) {
      setMode("search");
      setTimeout(() => fieldRef.current?.focus(), 0);
    }
    wasImporting.current = props.importing;
  }, [props.importing, draft]);

  return (
    <div className="add-cards" data-tour="add-cards">
      {/* Three columns over the field: how to add, the language centred,
          and where the art comes from. */}
      <div className="add-cards-head">
        <SplitPill
          ariaLabel="Add cards by"
          value={mode}
          onChange={setMode}
          options={[
            { value: "search", label: "Search", title: "Find cards by name" },
            { value: "paste", label: "List", title: "Paste a list, one card per line" },
          ]}
        />
        {/* The language adds demand ("strictly literal": a card without a
            version in this language errors and is not added), unless All
            Languages hands matching back to best effort. */}
        <span className="add-cards-lang">
          <SelectMenu
            ariaLabel="Card language to add"
            title="Card language to add"
            options={settings.languages.map((l) => ({ value: l, label: l.toUpperCase() }))}
            value={settings.lang}
            disabled={settings.langAny}
            onChange={settings.setLang}
          />
          <label className="check" title="Match any language, best effort">
            <input
              type="checkbox"
              checked={settings.langAny}
              onChange={(e) => settings.setLangAny(e.target.checked)}
            />
            All Languages
          </label>
        </span>
        {/* MPC Fill still matches each card against Scryfall (that's the
            card), then prints MPC Fill's top art for every face in its
            place. */}
        <span className="import-art-source add-cards-art">
          <span className="hint">Art From</span>
          <SplitPill
            ariaLabel="Where added cards' art comes from"
            value={settings.artSource}
            onChange={settings.setArtSource}
            options={[
              {
                value: "scryfall",
                label: "Scryfall",
                title: "Each card's own printing (upscaled when you Generate)",
              },
              {
                value: "mpc",
                label: "MPC Fill",
                title:
                  settings.mpcArtUnavailable ??
                  "MPC Fill's top community art for every face, picked automatically. Change any of them from the card's printing button.",
                disabled: settings.mpcArtUnavailable != null,
              },
            ]}
          />
        </span>
      </div>

      {mode === "search" ? (
        <div className="add-cards-field">
          <SearchGlyph />
          <input
            ref={fieldRef}
            value={quick.query}
            placeholder={quick.placeholder ?? "Add Cards — type a card name"}
            onChange={(e) => quick.setQuery(e.target.value)}
            onKeyDown={quick.onKeyDown}
            onFocus={() => quick.setOpen(true)}
            onBlur={() => setTimeout(() => quick.setOpen(false), 120)}
            aria-label="Add Cards"
          />
          <button
            type="button"
            className="add-cards-kbd"
            title="Open the command palette"
            onClick={() => setPaletteOpen(true)}
          >
            <kbd>⌘K</kbd>
          </button>
          {quick.open && <ResultList quick={quick} className="add-cards-sheet" />}
        </div>
      ) : (
        <div className="add-cards-paste">
          <textarea
            className="add-cards-list"
            value={draft}
            onChange={(e) => props.setDraft(e.target.value)}
            rows={6}
            placeholder={LIST_PLACEHOLDER}
            autoFocus
          />
          <div className="add-cards-paste-bar">
            <button
              type="button"
              className="btn-primary"
              onClick={props.onImport}
              disabled={!props.canImport}
              title={props.importTitle}
            >
              {importLabel(props.importing, lineCount)}
            </button>
          </div>
        </div>
      )}
      {props.notes}

      {paletteOpen && (
        <Palette
          {...props}
          lang={lang}
          adds={adds}
          onClose={() => setPaletteOpen(false)}
          onCardSearch={(hit) => {
            setPaletteOpen(false);
            setCardSearchFor(hit);
          }}
        />
      )}
      {cardSearchFor !== undefined && (
        <CardSearchModal
          lang={lang}
          adds={adds}
          initial={cardSearchFor}
          onClose={() => setCardSearchFor(undefined)}
        />
      )}
    </div>
  );
}

const countLines = (text: string) => text.split("\n").filter((l) => l.trim()).length;

function importLabel(importing: boolean, lines: number) {
  if (importing) return "Importing…";
  if (lines === 0) return "Import Cards";
  return `Import ${lines} ${lines === 1 ? "Card" : "Cards"}`;
}

// ---------------------------------------------------------------------------
// Searching and adding

function useDebounced<T>(value: T, ms: number): T {
  const [settled, setSettled] = useState(value);
  useEffect(() => {
    const timer = setTimeout(() => setSettled(value), ms);
    return () => clearTimeout(timer);
  }, [value, ms]);
  return settled;
}

/** Cards matching `query` (fuzzy, by name, local to the server). `lang`
 *  null is All Languages. */
function useCardSearch(query: string, lang: string | null, limit = 8) {
  const q = useDebounced(query.trim(), 120);
  const search = useQuery({
    queryKey: ["card-search", q, lang, limit],
    queryFn: () =>
      generationApi.searchCards(q, { lang: lang ?? "en", strictLang: lang != null, limit }),
    enabled: q.length >= 2,
    placeholderData: keepPreviousData,
    staleTime: 60_000,
  });
  const hits = q.length >= 2 ? (search.data?.results ?? []) : [];
  const noCorpus = search.error instanceof ApiError && search.error.status === 404;
  // Settled: what's shown answers what's typed (not a debounce or a
  // request behind it), so "no matches" never flashes mid-typing.
  const settled = q === query.trim() && !search.isFetching;
  return { hits, noCorpus, error: search.error, settled };
}

type CardSearch = ReturnType<typeof useCardSearch>;
type DeckAdds = ReturnType<typeof useDeckAdds>;

/** Adding one printing, or one printing with MPC Fill art on a face, to
 *  the deck, counting this session's adds for the palette and Card
 *  Search. */
function useDeckAdds(afterAdd: (added: CardRow[]) => void) {
  const { cards, decklistText, importResolvedCards, addCardWithFaceArt, projectTag } =
    useProject();
  const queryClient = useQueryClient();
  const serverVersion = useServerVersion();
  const [added, setAdded] = useState(0);
  const cardsRef = useRef(cards);
  cardsRef.current = cards;

  /** The printing as an import line the store takes, matched first so its
   *  names (printed, flavor) are the ones an import would store. */
  async function resolved(v: CardVariant): Promise<ResolvedImportCard> {
    const line = `1 ${v.name} (${v.set_code}) ${v.collector_number}`;
    const result = await generationApi.resolve([
      {
        quantity: 1,
        name: v.name,
        set_code: v.set_code,
        collector_number: v.collector_number,
        scryfall_id: v.scryfall_id,
        raw_line: line,
        lang: v.lang,
      },
    ]);
    const face = result.resolved[0]?.faces[0];
    if (!face) throw new Error(result.failed[0]?.error ?? "No match");
    return {
      raw_line: line,
      quantity: 1,
      name: face.card_name,
      printed_name: face.printed_name ?? null,
      flavor_name: face.flavor_name ?? null,
      set_code: face.set_code,
      collector_number: face.collector_number,
      scryfall_id: face.scryfall_id,
      lang: face.lang ?? "en",
      rarity: face.rarity ?? null,
    };
  }

  /** One copy of this printing (a card already in the deck gains one, as
   *  an import would). */
  async function addPrinting(v: CardVariant) {
    setAdded((n) => n + 1);
    try {
      const before = new Set(cardsRef.current.map((c) => c.id));
      const after = await importResolvedCards(decklistText, [await resolved(v)]);
      afterAdd(after.filter((c) => !before.has(c.id)));
    } catch (err) {
      setAdded((n) => n - 1);
      toast(`Couldn't add ${v.name}: ${errorText(err)}`, "err");
    }
  }

  /** The card as this printing, with MPC Fill art on one face. The art
   *  downloads first; the row appears when it has. */
  async function addArt(v: CardVariant, face: number, totalFaces: number, art: MpcCard) {
    setAdded((n) => n + 1);
    try {
      const image = await importMpcArt("custom", art);
      void queryClient.invalidateQueries({ queryKey: ["custom-images"] });
      const out = await addCardWithFaceArt(await resolved(v), face, totalFaces, image.id);
      void registerCustomCards(out.cards, out.projectTag ?? projectTag, serverVersion).catch(() => {});
    } catch (err) {
      setAdded((n) => n - 1);
      toast(`Couldn't add ${art.name}: ${errorText(err)}`, "err");
    }
  }

  return { added, addPrinting, addArt };
}

/** A search field's results, keyboard-driven: ↑/↓ move, Enter adds the
 *  highlighted card's default printing, ⌘Enter opens Card Search on it,
 *  Esc closes the list. After an add the field clears and keeps focus for
 *  the next card; its placeholder says what was added, which costs no
 *  space. */
function useQuickAdd(
  lang: string | null,
  adds: DeckAdds,
  openCardSearch: (hit: CardSearchHit) => void,
) {
  const [query, setQuery] = useState("");
  const [open, setOpen] = useState(false);
  const [active, setActive] = useState(0);
  const [lastAdded, setLastAdded] = useState<string | null>(null);
  const search = useCardSearch(query, lang);
  const hits = search.hits;
  useEffect(() => setActive(0), [hits]);

  function add(hit: CardSearchHit) {
    if (!hit.default) return;
    void adds.addPrinting(hit.default);
    setLastAdded(hit.name);
    setQuery("");
    setOpen(false);
  }
  function onKeyDown(e: ReactKeyboardEvent) {
    if (e.key === "ArrowDown" || e.key === "ArrowUp") {
      e.preventDefault();
      setOpen(true);
      const step = e.key === "ArrowDown" ? 1 : -1;
      setActive((i) => (hits.length ? (i + step + hits.length) % hits.length : 0));
    } else if (e.key === "Enter") {
      const hit = hits[active];
      if (!hit) return;
      e.preventDefault();
      if (e.metaKey || e.ctrlKey) {
        // Not the tab's own ⌘Enter (import the pasted list).
        e.stopPropagation();
        openCardSearch(hit);
        setOpen(false);
      } else add(hit);
    } else if (e.key === "Escape" && open) {
      e.preventDefault();
      setOpen(false);
    }
  }
  return {
    query,
    setQuery: (q: string) => {
      setQuery(q);
      setOpen(true);
      setLastAdded(null);
    },
    open: open && query.trim().length >= 2,
    setOpen,
    active,
    setActive,
    hits,
    search,
    add,
    onKeyDown,
    placeholder: lastAdded ? `Added ${lastAdded} — type the next card` : null,
  };
}

type QuickAdd = ReturnType<typeof useQuickAdd>;

/** ⌘K from anywhere on the tab. */
function useCmdK(onCmdK: () => void) {
  const ref = useRef(onCmdK);
  ref.current = onCmdK;
  useEffect(() => {
    function onKey(e: KeyboardEvent) {
      if ((e.metaKey || e.ctrlKey) && e.key.toLowerCase() === "k") {
        e.preventDefault();
        ref.current();
      }
    }
    window.addEventListener("keydown", onKey);
    return () => window.removeEventListener("keydown", onKey);
  }, []);
}

function useThumb() {
  const { origin } = useImageCacheSetting();
  return (v: CardVariant) =>
    origin ? generationApi.cardThumbUrl(v.scryfall_id, 0, origin, v.image_version) : null;
}

const hitDetail = (hit: CardSearchHit) =>
  hit.default
    ? `(${hit.default.set_code.toUpperCase()}) (${hit.default.collector_number})`
    : "No printing in this language";

function SearchStatus({ search, query }: { search: CardSearch; query: string }) {
  if (search.noCorpus)
    return <p className="hint add-cards-status">Import the card database (sidebar) to search cards.</p>;
  if (search.error)
    return <p className="hint add-cards-status">Search failed: {errorText(search.error)}</p>;
  if (search.settled && search.hits.length === 0 && query.trim().length >= 2)
    return <p className="hint add-cards-status">No cards match “{query.trim()}”.</p>;
  return null;
}

/** Matches with their art, name and set; the highlighted one shows its
 *  keys. The pointer moves the highlight, so it and the keyboard always
 *  agree on which card Enter adds. */
function ResultList({ quick, className }: { quick: QuickAdd; className: string }) {
  const thumb = useThumb();
  return (
    <div className={className} role="listbox">
      {quick.hits.map((hit, i) => {
        const src = hit.default && thumb(hit.default);
        return (
          <button
            key={hit.oracle_id}
            type="button"
            role="option"
            aria-selected={i === quick.active}
            className={`add-cards-option add-cards-result${i === quick.active ? " active" : ""}`}
            onMouseMove={() => quick.setActive(i)}
            onMouseDown={(e) => e.preventDefault()}
            onClick={() => quick.add(hit)}
            disabled={!hit.default}
          >
            <span className="add-cards-thumb">{src && <img src={src} alt="" />}</span>
            <span className="add-cards-result-text">
              <span className="add-cards-result-name">{hit.name}</span>
              <span className="add-cards-result-meta">
                {hit.default?.set_name} {hitDetail(hit)}
              </span>
            </span>
            {i === quick.active && (
              <span className="add-cards-result-keys">
                <kbd>↩</kbd> Add <kbd>⌘↩</kbd> Printings
              </span>
            )}
          </button>
        );
      })}
      <SearchStatus search={quick.search} query={quick.query} />
    </div>
  );
}

// ---------------------------------------------------------------------------
// ⌘K palette

function Palette({
  draft,
  setDraft,
  onImport,
  canImport,
  importing,
  onChooseFiles,
  onChooseOrder,
  orderDisabled,
  lang,
  adds,
  onClose,
  onCardSearch,
}: AddCardsProps & {
  lang: string | null;
  adds: DeckAdds;
  onClose: () => void;
  onCardSearch: (hit: CardSearchHit) => void;
}) {
  const quick = useQuickAdd(lang, adds, onCardSearch);
  const [listMode, setListMode] = useState(draft.trim().length > 0);
  const [cmd, setCmd] = useState(0);
  const commands: { label: string; hint: string; run: () => void; disabled?: boolean }[] = [
    { label: "Paste a List…", hint: "One card per line", run: () => setListMode(true) },
    {
      label: "Add Your Own Art…",
      hint: "Images become cards",
      run: () => {
        onClose();
        onChooseFiles();
      },
    },
    {
      label: "Import MPC Fill Order…",
      hint: "An order's .xml",
      run: () => {
        onClose();
        onChooseOrder();
      },
      disabled: orderDisabled,
    },
  ];
  const showCommands = !listMode && quick.query.trim().length < 2;

  useEffect(() => {
    function onKey(e: KeyboardEvent) {
      if (e.key === "Escape" && !e.defaultPrevented) {
        e.preventDefault();
        if (listMode) setListMode(false);
        else onClose();
      }
    }
    document.addEventListener("keydown", onKey);
    return () => document.removeEventListener("keydown", onKey);
  }, [listMode, onClose]);

  function onKeyDown(e: ReactKeyboardEvent<HTMLInputElement>) {
    if (showCommands && (e.key === "ArrowDown" || e.key === "ArrowUp")) {
      e.preventDefault();
      setCmd((i) => (i + (e.key === "ArrowDown" ? 1 : -1) + commands.length) % commands.length);
      return;
    }
    if (showCommands && e.key === "Enter") {
      e.preventDefault();
      const c = commands[cmd];
      if (!c.disabled) c.run();
      return;
    }
    // Results: Enter adds and the palette stays open for the next card.
    quick.onKeyDown(e);
  }

  return (
    <ModalOverlay onClick={onClose} className="add-cards-palette-overlay">
      <div
        className="add-cards-palette"
        role="dialog"
        aria-label="Add Cards"
        onClick={(e) => e.stopPropagation()}
      >
        {listMode ? (
          <>
            <div className="add-cards-palette-head">
              <span className="add-cards-palette-title">Paste a List</span>
              <button type="button" className="linklike" onClick={() => setListMode(false)}>
                Back to Search
              </button>
            </div>
            <textarea
              className="add-cards-palette-list"
              value={draft}
              onChange={(e) => setDraft(e.target.value)}
              rows={10}
              placeholder={LIST_PLACEHOLDER}
              autoFocus
            />
            <div className="add-cards-palette-list-bar">
              <button
                type="button"
                className="btn-primary"
                onClick={() => {
                  onImport();
                  onClose();
                }}
                disabled={!canImport}
              >
                {importLabel(importing, countLines(draft))}
              </button>
            </div>
          </>
        ) : (
          <>
            <div className="add-cards-palette-input">
              <SearchGlyph />
              <input
                value={quick.query}
                placeholder={quick.placeholder ?? "Search cards, or choose an action"}
                onChange={(e) => quick.setQuery(e.target.value)}
                onKeyDown={onKeyDown}
                onPaste={(e) => {
                  // Several lines are a list, not a search.
                  const text = e.clipboardData.getData("text");
                  if (text.includes("\n")) {
                    e.preventDefault();
                    setDraft(text.trim());
                    setListMode(true);
                  }
                }}
                aria-label="Add Cards"
                autoFocus
              />
            </div>
            {showCommands ? (
              <div className="add-cards-palette-body" role="listbox">
                {commands.map((c, i) => (
                  <button
                    key={c.label}
                    type="button"
                    role="option"
                    aria-selected={i === cmd}
                    className={`add-cards-option add-cards-command${i === cmd ? " active" : ""}`}
                    onMouseMove={() => setCmd(i)}
                    onClick={c.run}
                    disabled={c.disabled}
                  >
                    <span>{c.label}</span>
                    <span className="add-cards-command-hint">{c.hint}</span>
                  </button>
                ))}
              </div>
            ) : (
              <ResultList quick={quick} className="add-cards-palette-body" />
            )}
            <div className="add-cards-palette-foot">
              <span>
                <kbd>↩</kbd> Add
              </span>
              <span>
                <kbd>⌘↩</kbd> All Printings
              </span>
              <span>
                <kbd>esc</kbd> Close
              </span>
              {adds.added > 0 && <span className="add-cards-count">{adds.added} Added</span>}
            </div>
          </>
        )}
      </div>
    </ModalOverlay>
  );
}

// ---------------------------------------------------------------------------
// Card Search

/** Card Search: the change-printing picker for any card, with a search
 *  field in its head. It stays open; the preview's Add To Deck, a
 *  double-click or Enter add the printing (or MPC Fill art) to the deck. */
function CardSearchModal({
  lang,
  adds,
  initial,
  onClose,
}: {
  lang: string | null;
  adds: DeckAdds;
  initial: CardSearchHit | null;
  onClose: () => void;
}) {
  const [hit, setHit] = useState<CardSearchHit | null>(initial);
  const [source, setSource] = useState<PickerSource>("scryfall");
  const nav = (
    <span className="card-search-nav">
      <CardSearchField lang={lang} onChoose={setHit} autoFocus={hit == null} />
    </span>
  );
  const footer = (
    <>
      <span className="walk-head">
        {adds.added > 0 ? (
          <span className="add-cards-count">{adds.added} Added</span>
        ) : (
          <span className="walk-pos">Double-click a printing or art to add it. This stays open.</span>
        )}
      </span>
      <button type="button" className="btn-sm btn-primary" onClick={onClose}>
        Done
      </button>
    </>
  );
  if (!hit?.default) {
    return (
      <ModalOverlay onClick={onClose}>
        <div
          className="modal card-search-empty"
          role="dialog"
          aria-label="Card Search"
          onClick={(e) => e.stopPropagation()}
        >
          <div className="modal-head">
            <h3>Card Search</h3>
            {nav}
            <button type="button" className="btn-sm" onClick={onClose}>
              Done
            </button>
          </div>
          <p className="hint add-cards-status">
            Search for a card to see every printing and its MPC Fill art. This stays open, so you
            can add as many as you like.
          </p>
        </div>
      </ModalOverlay>
    );
  }
  const v = hit.default;
  // The picker works on a deck row; this one isn't in the deck yet.
  const card = {
    id: -1,
    name: v.name,
    scryfall_id: v.scryfall_id,
    set_code: v.set_code,
    collector_number: v.collector_number,
    lang: v.lang,
    face_art: [],
  } as unknown as CardRow;
  return (
    <PrintingGridModal
      key={`${hit.oracle_id}-${source}`}
      card={card}
      preferredLang={lang ?? "en"}
      initial={{ source, face: 0 }}
      onClose={onClose}
      onSourceChange={setSource}
      pickLabels={{ printing: "Add To Deck", art: "Add To Deck" }}
      walk={{
        nav,
        footer,
        staged: null,
        onSelect: () => {},
        onChoose: (choice) => {
          if (choice.kind === "printing") void adds.addPrinting(variantOfPick(choice.pick, v));
          else void adds.addArt(v, choice.face, choice.totalFaces, choice.art);
        },
      }}
    />
  );
}

function variantOfPick(
  pick: { scryfallId: string; name: string; setCode: string; collectorNumber: string; lang: string },
  base: CardVariant,
): CardVariant {
  return {
    ...base,
    scryfall_id: pick.scryfallId,
    name: pick.name,
    set_code: pick.setCode,
    collector_number: pick.collectorNumber,
    lang: pick.lang,
  };
}

/** Card Search's field: a dropdown of matches; choosing one shows its
 *  printings. */
function CardSearchField({
  lang,
  onChoose,
  autoFocus,
}: {
  lang: string | null;
  onChoose: (hit: CardSearchHit) => void;
  autoFocus?: boolean;
}) {
  const [query, setQuery] = useState("");
  const [open, setOpen] = useState(false);
  const [active, setActive] = useState(0);
  const search = useCardSearch(query, lang);
  useEffect(() => setActive(0), [search.hits]);
  const choose = (hit: CardSearchHit) => {
    onChoose(hit);
    setOpen(false);
    setQuery(hit.name);
  };
  return (
    <span className="card-search-combo">
      <input
        value={query}
        placeholder="Search Cards"
        autoFocus={autoFocus}
        onChange={(e) => {
          setQuery(e.target.value);
          setOpen(true);
        }}
        onFocus={(e) => {
          e.target.select();
          setOpen(true);
        }}
        onBlur={() => setTimeout(() => setOpen(false), 120)}
        onKeyDown={(e) => {
          const hits = search.hits;
          if (e.key === "ArrowDown" || e.key === "ArrowUp") {
            // Not the picker's grid, which moves on arrows too.
            e.preventDefault();
            e.stopPropagation();
            const step = e.key === "ArrowDown" ? 1 : -1;
            setActive((i) => (hits.length ? (i + step + hits.length) % hits.length : 0));
          } else if (e.key === "Enter" && hits[active]) {
            e.preventDefault();
            choose(hits[active]);
          } else if (e.key === "Escape" && open) {
            // The modal's Esc waits for the list.
            e.preventDefault();
            setOpen(false);
          }
        }}
      />
      {open && query.trim().length >= 2 && (
        <span className="card-search-menu" role="listbox">
          {search.hits.map((hit, i) => (
            <button
              key={hit.oracle_id}
              type="button"
              role="option"
              aria-selected={i === active}
              className={`add-cards-option card-search-option${i === active ? " active" : ""}`}
              onMouseMove={() => setActive(i)}
              onMouseDown={(e) => e.preventDefault()}
              onClick={() => choose(hit)}
            >
              <span className="card-search-option-name">{hit.name}</span>
              <span className="card-search-option-meta">{hit.printings} Printings</span>
            </button>
          ))}
          <SearchStatus search={search} query={query} />
        </span>
      )}
    </span>
  );
}

function SearchGlyph() {
  return (
    <svg className="add-cards-glyph" viewBox="0 0 16 16" aria-hidden="true">
      <circle cx="7" cy="7" r="4.5" fill="none" stroke="currentColor" strokeWidth="1.8" />
      <path d="M10.4 10.4 14 14" stroke="currentColor" strokeWidth="1.8" strokeLinecap="round" />
    </svg>
  );
}
