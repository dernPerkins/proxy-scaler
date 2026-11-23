// The art picker's two sides (PrintingGridModal, PrintingPicker.tsx).
//
// The picker answers "what is on this side of the card?" the same way for
// the Front and the Back (docs/adr/0011, .scratch/back-card/spec.md
// decisions 20-28):
//   - a Front | Back toggle over the preview, naming what is on each side;
//   - art sources: Scryfall, MPC Fill, Custom, and on the Back, Backs
//     (Universal, Blank, the Back Library);
//   - Card Search's field in the head: a pick puts that card on the side
//     being viewed. On the Front that replaces the card; on the Back it is
//     a Back Card.
// Everything here stages through the deck walk like a printing pick and is
// applied on Confirm. Chosen from a prototype
// (.scratch/back-card-picker-prototype/).
import { useEffect, useRef, useState, type ReactNode } from "react";
import { useQuery, useQueryClient } from "@tanstack/react-query";
import { generationApi } from "../api/generation";
import { projectApi, type CardRow, type ScryfallFaceSource } from "../api/project";
import type { BackMode, CardSearchHit, CardVariant, CustomImage, ImageOrigin } from "../api/types";
import { useBackLibrary } from "../cardBacks";
import { useProject } from "../context/ProjectContext";
import { ACCEPTED_IMAGE_TYPES, readPickedImage } from "../imageUpload";
import { hasShortcutModifier } from "../shortcuts";
import { SearchStatus, TokensOnlySwitch, useCardSearch } from "./AddCards";
import { useBackCardThumb, useBackThumb } from "./CardBackPicker";
import type { BackChange, Side, StagedChanges } from "./PrintingPicker";

/** What the source switch offers. `backs` is the Back side's only. */
export type SideSource = "scryfall" | "mpc" | "custom" | "backs";

const SOURCE_LABELS: Record<SideSource, string> = {
  scryfall: "Scryfall",
  mpc: "MPC Fill",
  custom: "Custom",
  backs: "Backs",
};

/** What a card's Reverse is set to: the three stored fields. */
export type BackFields = Pick<CardRow, "back_mode" | "back_card" | "back_image_id" | "back_art">;

/** The card's Reverse once `staged` applies. */
export function backAfter(card: CardRow, staged: StagedChanges | null): BackFields {
  const change = staged?.back;
  if (!change) {
    return {
      back_mode: card.back_mode,
      back_card: card.back_card,
      back_image_id: card.back_image_id,
      back_art: card.back_mode === "card" ? card.back_art : null,
    };
  }
  if (change.kind !== "card") {
    return { back_mode: change.mode, back_card: null, back_image_id: change.backImageId, back_art: null };
  }
  const art = change.art;
  return {
    back_mode: "card",
    back_card: change.source,
    back_image_id: null,
    back_art: !art
      ? null
      : art.kind === "custom"
        ? {
            face: 0,
            total_faces: 1,
            custom_image_id: art.image.id,
            custom_hash: art.image.content_hash,
            label: art.image.label,
            mpc_drive_id: art.image.mpc?.drive_id ?? null,
            mpc_source: art.image.mpc?.source ?? null,
          }
        : {
            // Chosen, downloaded on Confirm.
            face: 0,
            total_faces: 1,
            custom_image_id: null,
            custom_hash: null,
            label: art.art.name,
            mpc_drive_id: art.art.drive_id,
            mpc_source: art.art.source_name,
          },
  };
}

/** Whether `change` is what the card already has. */
export function backUnchanged(card: CardRow, change: BackChange): boolean {
  if (change.kind === "card") {
    const b = card.back_mode === "card" ? card.back_card : null;
    if (
      b == null ||
      b.scryfall_id !== change.source.scryfall_id ||
      b.source_face !== change.source.source_face
    ) {
      return false;
    }
    const has = card.back_art;
    const art = change.art;
    if (!art) return has == null;
    if (art.kind === "custom") return has?.custom_image_id === art.image.id;
    // Already-fetched art is no change; art still to download downloads.
    return has?.mpc_drive_id === art.art.drive_id && has.custom_image_id != null;
  }
  if (card.back_mode !== change.mode) return false;
  return change.mode !== "image" || card.back_image_id === change.backImageId;
}

/** The card the picker shows for the Front: a different card staged by
 *  the search stands in for the stored one, so the grid lists ITS
 *  printings. */
export function frontAfter(card: CardRow, staged: StagedChanges | null): CardRow {
  const front = staged?.faces.get(0);
  if (front?.kind !== "printing" || front.pick.name === card.name) return card;
  return {
    ...card,
    scryfall_id: front.pick.scryfallId,
    name: front.pick.name,
    set_code: front.pick.setCode,
    collector_number: front.pick.collectorNumber,
    lang: front.pick.lang,
    face_art: [],
  };
}

/** The Back Card as the card the picker is pointed at: the grid lists its
 *  printings, with the Front's filters. */
export function backRow(card: CardRow, back: ScryfallFaceSource): CardRow {
  return {
    ...card,
    scryfall_id: back.scryfall_id,
    name: back.name,
    set_code: back.set_code,
    collector_number: back.collector_number,
    lang: back.lang,
    face_art: [],
  };
}

/** Face `face` of a printing, as a Back Card's source. */
export function backSourceOf(v: CardVariant, face: number): ScryfallFaceSource {
  const two = (v.face_count ?? 1) > 1;
  return {
    scryfall_id: v.scryfall_id,
    source_face: two ? face : 0,
    name: (two ? v.face_names?.[face] : undefined) ?? v.name,
    set_code: v.set_code,
    collector_number: v.collector_number,
    lang: v.lang,
    printed_name: two ? null : v.printed_name,
    flavor_name: two ? null : (v.flavor_name ?? null),
    rarity: v.rarity ?? null,
  };
}

/** What is on the Reverse, in a word or two. `realBack` names a
 *  double-faced card's own back face while that is what prints there. */
function useBackName(back: BackFields, realBack: string | undefined): string {
  const library = useBackLibrary();
  if (back.back_mode === "card" && back.back_card)
    return back.back_card.printed_name ?? back.back_card.name;
  if (realBack) return realBack;
  if (back.back_mode === "blank") return "Blank";
  if (back.back_mode === "image")
    return library.data?.find((b) => b.id === back.back_image_id)?.label ?? "Card Back";
  return "Universal";
}

// ---- The head: side toggle, sources, search -------------------------------

/** Front | Back over the preview column. Text only: the preview below
 *  keeps its size. */
export function SideToggle(props: {
  frontName: string;
  back: BackFields;
  realBack: string | undefined;
  side: Side;
  setSide: (side: Side) => void;
}) {
  const backName = useBackName(props.back, props.realBack);
  const sides: [Side, string, string][] = [
    ["front", "Front", props.frontName],
    ["back", "Back", backName],
  ];
  return (
    <div className="picker-sides" role="group" aria-label="Side" data-tour="picker-face">
      {sides.map(([side, label, name]) => (
        <button
          key={side}
          type="button"
          className={`picker-side${props.side === side ? " active" : ""}`}
          aria-pressed={props.side === side}
          title={`${label}: ${name}`}
          onClick={() => props.setSide(side)}
        >
          <span className="picker-side-label">{label}</span>
          <span className="picker-side-name">{name}</span>
        </button>
      ))}
    </div>
  );
}

export function SourceSwitch(props: {
  options: readonly SideSource[];
  value: SideSource;
  onChange: (source: SideSource) => void;
}) {
  if (props.options.length < 2) return null;
  return (
    <span className="segmented" role="group" aria-label="Art source">
      {props.options.map((s) => (
        <button
          key={s}
          type="button"
          className={`btn-sm${props.value === s ? " active" : ""}`}
          aria-pressed={props.value === s}
          title={
            s === "mpc"
              ? "Community art from MPC Fill, printed in place of this printing's art"
              : undefined
          }
          onClick={() => props.onChange(s)}
        >
          {SOURCE_LABELS[s]}
        </button>
      ))}
    </span>
  );
}

/** Card Search's field (AddCards' CardSearchField) with Tokens Only: a
 *  dropdown of matches; choosing one puts it on the side being viewed. */
export function PickerSearch(props: { onChoose: (v: CardVariant) => void }) {
  const [query, setQuery] = useState("");
  const [open, setOpen] = useState(false);
  const [active, setActive] = useState(0);
  // The picker's own Tokens Only, off each time it opens: Add Cards' is
  // left alone, so a token search here never hides cards there.
  const [tokensOnly, setTokensOnly] = useState(false);
  const search = useCardSearch(query, null, 8, tokensOnly);
  useEffect(() => setActive(0), [search.hits]);
  const inputRef = useRef<HTMLInputElement>(null);

  // ⌘T / Ctrl+T. Captured, so Add Cards' own ⌘T behind the modal stays
  // quiet.
  useEffect(() => {
    function onKey(e: KeyboardEvent) {
      if (hasShortcutModifier(e) && !e.shiftKey && e.code === "KeyT") {
        e.preventDefault();
        e.stopImmediatePropagation();
        setTokensOnly((on) => !on);
        inputRef.current?.focus();
      }
    }
    window.addEventListener("keydown", onKey, true);
    return () => window.removeEventListener("keydown", onKey, true);
  }, []);

  const choose = (hit: CardSearchHit) => {
    if (!hit.default) return;
    props.onChoose(hit.default);
    setOpen(false);
    setQuery("");
    inputRef.current?.blur();
  };
  return (
    <span className={`picker-search${tokensOnly ? " tokens-only" : ""}`}>
      <span className="card-search-combo">
        <input
          ref={inputRef}
          value={query}
          placeholder={tokensOnly ? "Search Any Token…" : "Search Any Card…"}
          aria-label={tokensOnly ? "Search any token" : "Search any card"}
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
            } else if (e.key === "Escape" && open && query) {
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
      <TokensOnlySwitch on={tokensOnly} toggle={() => setTokensOnly((on) => !on)} />
    </span>
  );
}

// ---- Tiles and previews for what isn't a Scryfall printing ----------------

function ImgTile(props: {
  src: string | null | undefined;
  label: ReactNode;
  /** The choice in force (green). */
  active: boolean;
  blank?: boolean;
  title?: string;
  onSelect: () => void;
  onPick?: () => void;
}) {
  return (
    <div className={`printing-tile${props.active ? " active" : ""}`}>
      <button
        type="button"
        className="printing-tile-pick"
        title={props.title}
        onClick={props.onSelect}
        onDoubleClick={props.onPick}
      >
        <span className={`printing-tile-frame${props.blank ? " picker-blank-frame" : ""}`}>
          {props.src && <img src={props.src} alt="" loading="lazy" draggable={false} />}
        </span>
        <span className="printing-tile-label">{props.label}</span>
      </button>
    </div>
  );
}

/** The preview column for something that isn't a Scryfall printing, in
 *  the printing preview's shape (PrintingPreview): the card on a wash of
 *  its own art, and a scrim of the same height, so nothing moves between
 *  tabs. */
function ImagePreview(props: {
  src: string | null | undefined;
  name: string;
  meta: string;
  cta: { label: string; onClick: () => void; disabled?: boolean };
}) {
  return (
    <aside className="printing-preview" aria-label={props.name}>
      {props.src && (
        <div className="printing-preview-wash" style={{ backgroundImage: `url("${props.src}")` }} />
      )}
      <div className="printing-preview-stage">
        <div className="printing-preview-frame">{props.src && <img src={props.src} alt="" />}</div>
      </div>
      <div className="printing-preview-scrim">
        <h3 className="printing-preview-name">{props.name}</h3>
        <p className="printing-preview-meta">{props.meta}</p>
        {/* The printing preview's row of generated versions: kept, empty,
            for its height. */}
        <div className="printing-preview-versions" />
        <button
          type="button"
          className="printing-preview-cta"
          disabled={props.cta.disabled}
          onClick={props.cta.onClick}
        >
          {props.cta.label}
        </button>
      </div>
    </aside>
  );
}

/** The Reverse as it will print, in the preview column. */
function BackPreview(props: {
  card: CardRow;
  back: BackFields;
  realBack: string | undefined;
  origin: ImageOrigin | null;
  onConfirm: () => void;
}) {
  const { card, back, realBack, origin } = props;
  const { settings } = useProject();
  const name = useBackName(back, realBack);
  const isCard = back.back_mode === "card" && back.back_card != null;
  const showsReal = realBack != null && !isCard;
  const imageId = isCard || showsReal
    ? null
    : back.back_mode === "image"
      ? back.back_image_id
      : back.back_mode === "inherit"
        ? settings.back_image_id
        : null;
  const image = useBackThumb(imageId);
  // The full image once it has loaded: the thumbnail is small for the
  // preview's size.
  const full = useQuery({
    queryKey: ["back-full", imageId],
    queryFn: () => projectApi.backImageFull(imageId as number),
    enabled: imageId != null,
    staleTime: Infinity,
  });
  const cardThumb = useBackCardThumb(isCard ? back.back_card : null, back.back_art);
  const realThumb =
    showsReal && origin && card.scryfall_id
      ? generationApi.cardThumbUrl(card.scryfall_id, 1, origin)
      : null;
  return (
    <ImagePreview
      src={cardThumb ?? realThumb ?? full.data ?? image.data}
      name={isCard || showsReal ? name : `${name} Back`}
      meta={`On the back of ${card.name}`}
      cta={{ label: "Confirm", onClick: props.onConfirm }}
    />
  );
}

function LibraryBackTile(props: {
  id: number;
  label: string;
  active: boolean;
  onSelect: () => void;
  onPick: () => void;
}) {
  const thumb = useBackThumb(props.id);
  return (
    <ImgTile
      src={thumb.data}
      label={props.label}
      title={props.label}
      active={props.active}
      onSelect={props.onSelect}
      onPick={props.onPick}
    />
  );
}

/** Opens the file picker; sits where the other tabs have their filters. */
function UploadButton(props: { label: string; onFile: (file: File) => void; busy: boolean }) {
  const input = useRef<HTMLInputElement>(null);
  return (
    <>
      <button
        type="button"
        className="btn-sm"
        disabled={props.busy}
        onClick={() => input.current?.click()}
      >
        {props.busy ? "Uploading…" : props.label}
      </button>
      <input
        ref={input}
        type="file"
        accept={ACCEPTED_IMAGE_TYPES}
        hidden
        onChange={(e) => {
          const file = e.target.files?.[0];
          e.target.value = "";
          if (file) props.onFile(file);
        }}
      />
    </>
  );
}

/** Backs: Universal, Blank and the Back Library. Choosing one is the
 *  card's back, and takes a Back Card off it. The filter row (the upload)
 *  and the tiles. */
function BacksGrid(props: {
  back: BackFields;
  /** A double-faced card's back face prints on its back: "follow the
   *  project" means that face, and another back is kept but waits. */
  transformOnBack: boolean;
  onSelect: (mode: Exclude<BackMode, "card">, backImageId: number | null) => void;
  onPick: (mode: Exclude<BackMode, "card">, backImageId: number | null) => void;
}) {
  const { back, transformOnBack } = props;
  const { settings } = useProject();
  const queryClient = useQueryClient();
  const library = useBackLibrary();
  const universal = useBackThumb(transformOnBack ? null : settings.back_image_id);
  const [busy, setBusy] = useState(false);
  const [error, setError] = useState<string | null>(null);
  // Same path as the Backs tab's add: the file joins the library and this
  // card is pointed at it. It does not become the project's universal back.
  const upload = async (file: File) => {
    setBusy(true);
    setError(null);
    try {
      const picked = await readPickedImage(file);
      const added = await projectApi.addBackImage({
        ...picked,
        originalFilename: file.name,
        label: file.name,
      });
      await queryClient.invalidateQueries({ queryKey: ["back-images"] });
      props.onSelect("image", added.id);
    } catch (err) {
      setError(err instanceof Error ? err.message : String(err));
    } finally {
      setBusy(false);
    }
  };
  return (
    <>
      <div className="printing-grid-controls">
        <span className="printing-grid-count">
          {(library.data?.length ?? 0) + 2} Backs
        </span>
        <UploadButton label="Upload New Back" busy={busy} onFile={(file) => void upload(file)} />
      </div>
      <div className="modal-body">
      {transformOnBack && (
        <p className="hint">
          This card's back face prints on its back. Another back is kept, and prints once back
          faces print as their own cards (PDF tab).
        </p>
      )}
      {error && <p className="hint">{error}</p>}
      <div className="printing-grid">
        <ImgTile
          src={universal.data}
          blank={transformOnBack}
          label={transformOnBack ? "Real Back" : "Universal"}
          title={
            transformOnBack
              ? "This card's own back face"
              : "The project's back, chosen on the Backs tab"
          }
          active={back.back_mode === "inherit"}
          onSelect={() => props.onSelect("inherit", null)}
          onPick={() => props.onPick("inherit", null)}
        />
        <ImgTile
          src={null}
          blank
          label="Blank"
          title="Nothing prints on this card's back"
          active={back.back_mode === "blank"}
          onSelect={() => props.onSelect("blank", null)}
          onPick={() => props.onPick("blank", null)}
        />
        {(library.data ?? []).map((b) => (
          <LibraryBackTile
            key={b.id}
            id={b.id}
            label={b.label}
            active={back.back_mode === "image" && back.back_image_id === b.id}
            onSelect={() => props.onSelect("image", b.id)}
            onPick={() => props.onPick("image", b.id)}
          />
        ))}
      </div>
      </div>
    </>
  );
}

function CustomTile(props: {
  image: CustomImage;
  active: boolean;
  onSelect: () => void;
  onPick: () => void;
}) {
  const thumb = useQuery({
    queryKey: ["custom-thumb", props.image.id],
    queryFn: () => projectApi.customImageThumbnail(props.image.id),
    staleTime: Infinity,
  });
  return (
    <ImgTile
      src={thumb.data}
      label={props.image.label}
      title={props.image.label}
      active={props.active}
      onSelect={props.onSelect}
      onPick={props.onPick}
    />
  );
}

/** Custom: the Custom Image library, as art for one face. The filter row
 *  (the upload) and the tiles. */
function CustomGrid(props: {
  /** The image this face prints (staged, else stored), if it is one. */
  chosenId: number | null;
  onSelect: (image: CustomImage) => void;
  onPick: (image: CustomImage) => void;
}) {
  const queryClient = useQueryClient();
  const images = useQuery({
    queryKey: ["custom-images"],
    queryFn: () => projectApi.listCustomImages(),
  });
  const [busy, setBusy] = useState(false);
  const [error, setError] = useState<string | null>(null);
  const upload = async (file: File) => {
    setBusy(true);
    setError(null);
    try {
      const picked = await readPickedImage(file);
      const added = await projectApi.addCustomImage({ ...picked, originalFilename: file.name });
      await queryClient.invalidateQueries({ queryKey: ["custom-images"] });
      props.onSelect(added);
    } catch (err) {
      setError(err instanceof Error ? err.message : String(err));
    } finally {
      setBusy(false);
    }
  };
  return (
    <>
      <div className="printing-grid-controls">
        <span className="printing-grid-count">
          {images.data?.length ?? 0} {images.data?.length === 1 ? "Image" : "Images"}
        </span>
        <UploadButton label="Upload New Image" busy={busy} onFile={(file) => void upload(file)} />
      </div>
      <div className="modal-body">
      {error && <p className="hint">{error}</p>}
      <div className="printing-grid">
        {(images.data ?? []).map((image) => (
          <CustomTile
            key={image.id}
            image={image}
            active={props.chosenId === image.id}
            onSelect={() => props.onSelect(image)}
            onPick={() => props.onPick(image)}
          />
        ))}
      </div>
      </div>
    </>
  );
}

function CustomPreview(props: {
  image: CustomImage | null;
  fallbackName: string;
  onPick: (image: CustomImage) => void;
}) {
  const { image } = props;
  const thumb = useQuery({
    queryKey: ["custom-thumb", image?.id],
    queryFn: () => projectApi.customImageThumbnail(image!.id),
    enabled: image != null,
    staleTime: Infinity,
  });
  // The full image once it has loaded: the thumbnail is small for the
  // preview's size.
  const full = useQuery({
    queryKey: ["custom-full", image?.id],
    queryFn: () => projectApi.customImageFull(image!.id),
    enabled: image != null,
    staleTime: Infinity,
  });
  return (
    <ImagePreview
      src={image ? (full.data ?? thumb.data) : null}
      name={image?.label ?? props.fallbackName}
      meta={image ? `${image.width} × ${image.height} px` : "Choose a Custom Image"}
      cta={{
        label: "Use This Image",
        disabled: image == null,
        onClick: () => image && props.onPick(image),
      }}
    />
  );
}

/** The picker's views that aren't the printing grid or MPC Fill: Custom
 *  on either side, and Backs. Renders the modal's children but the side
 *  toggle and the walk's bar. */
export function SideBody(props: {
  view: "custom" | "backs";
  card: CardRow;
  /** The head's contents: source switch, search, Close. */
  head: ReactNode;
  origin: ImageOrigin | null;
  back: BackFields;
  realBack: string | undefined;
  transformOnBack: boolean;
  /** Custom: the face being chosen for, and the image it prints. */
  faceName: string;
  customChosenId: number | null;
  onCustom: (image: CustomImage, choose: boolean) => void;
  onBack: (change: BackChange, choose: boolean) => void;
  /** Close the walk, applying what it chose. */
  onConfirm: () => void;
}) {
  const { view, card, back } = props;
  const [selected, setSelected] = useState<CustomImage | null>(null);
  const images = useQuery({
    queryKey: ["custom-images"],
    queryFn: () => projectApi.listCustomImages(),
    enabled: view === "custom",
  });
  const head = <div className="modal-head printing-grid-head">{props.head}</div>;
  if (view === "custom") {
    // What is selected, else the image this face already prints.
    const shown =
      selected ?? images.data?.find((image) => image.id === props.customChosenId) ?? null;
    return (
      <>
        <CustomPreview
          image={shown}
          fallbackName={props.faceName}
          onPick={(image) => props.onCustom(image, true)}
        />
        {head}
        <div className="printing-grid-notes" />
        <CustomGrid
          chosenId={props.customChosenId}
          onSelect={(image) => {
            setSelected(image);
            props.onCustom(image, false);
          }}
          onPick={(image) => props.onCustom(image, true)}
        />
      </>
    );
  }
  const change = (mode: Exclude<BackMode, "card">, backImageId: number | null): BackChange => ({
    kind: "back",
    mode,
    backImageId,
  });
  return (
    <>
      <BackPreview
        card={card}
        back={back}
        realBack={props.realBack}
        origin={props.origin}
        onConfirm={props.onConfirm}
      />
      {head}
      <div className="printing-grid-notes" />
      <BacksGrid
        back={back}
        transformOnBack={props.transformOnBack}
        onSelect={(mode, id) => props.onBack(change(mode, id), false)}
        onPick={(mode, id) => props.onBack(change(mode, id), true)}
      />
    </>
  );
}
