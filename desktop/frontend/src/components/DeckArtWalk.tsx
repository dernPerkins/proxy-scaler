// The deck walk: the change-printing modal (PrintingGridModal) for one card
// at a time, with ‹ Prev / Next › (or Alt+← / Alt+→) through the Decklist
// in its current sort. Selecting a printing or MPC Fill art chooses it for
// the face on screen: a double-faced card's faces are chosen apart. The
// front's printing is the card's printing; the back follows it until the
// back is given a printing or art of its own, and keeps that when the
// front changes (choosing the front's printing for the back re-links it).
// The filters, on both tabs, carry from card to card: set once, they hold
// for the rest of the walk. Nothing is saved while the walk is open: Confirm, the preview's button
// or a double-click close it, apply every card's choice and download every
// card the walk visited, changed or not, each as it will print (both faces
// of a double-faced card) — looking at a card and confirming is confirming
// it. Close, Esc or a click outside close it without downloading; with
// choices made they ask first — Keep (the same as Confirm), Discard or
// Cancel — so unconfirmed work isn't lost to a stray click.
// Chosen from a prototype (.scratch/mpcfill-art-picker/prototype-deck-walk/).
import { useEffect, useState } from "react";
import { isImageFaceArt, type CardFaceArt, type CardRow, type ScryfallFaceSource } from "../api/project";
import type { MpcCard } from "../api/types";
import ModalOverlay from "./ModalOverlay";
import {
  DEFAULT_PICKER_FILTERS,
  PrintingGridModal,
  facePrinting,
  type PickerFilters,
  type Pick,
  type PickerOpenRequest,
  type StagedChanges,
  type WalkChoice,
} from "./PrintingPicker";

/** How the Decklist applies the walk's choices, card by card. Each
 *  resolves once stored, so the next can build on it. */
export interface WalkApply {
  /** The card's printing (its front's). Keeps the back's own choice. */
  printing: (card: CardRow, printing: Pick) => Promise<void>;
  /** One face printed from another printing of the card. */
  facePrinting: (card: CardRow, face: number, totalFaces: number, source: ScryfallFaceSource) => Promise<void>;
  /** MPC Fill art on one face; downloads in the background. */
  faceArt: (card: CardRow, face: number, totalFaces: number, art: MpcCard) => void;
  /** A face back on the card's own printing. */
  clearFaceArt: (card: CardRow, face: number) => Promise<void>;
  /** Download a card the walk visited, as it prints now (`card` already
   *  carries the walk's choices): every face that isn't MPC Fill art, and
   *  any MPC Fill art chosen but not downloaded yet. */
  download: (card: CardRow) => void;
}

/** Face `face` of a picked printing, as the store keeps it. */
export function sourceOf(pick: Pick, face: number): ScryfallFaceSource {
  return {
    scryfall_id: pick.scryfallId,
    source_face: face,
    name: pick.name,
    set_code: pick.setCode,
    collector_number: pick.collectorNumber,
    lang: pick.lang,
    printed_name: pick.printedName,
    flavor_name: pick.flavorName ?? null,
    rarity: pick.rarity,
  };
}

/** The card as it prints once `change` applies — what Confirm downloads.
 *  Mirrors the store (face_art.rs, set_card_printing): a new front printing
 *  drops the front's art and keeps the back's; a back given the card's
 *  printing follows it, with no override. */
function cardAfter(card: CardRow, change: StagedChanges): CardRow {
  let next: CardRow = { ...card, face_art: [...(card.face_art ?? [])] };
  const front = change.faces.get(0);
  if (front?.kind === "printing" && front.pick.scryfallId !== card.scryfall_id) {
    const p = front.pick;
    next = {
      ...next,
      scryfall_id: p.scryfallId,
      name: p.name,
      set_code: p.setCode,
      collector_number: p.collectorNumber,
      lang: p.lang,
      printed_name: p.printedName,
      flavor_name: p.flavorName ?? null,
      rarity: p.rarity,
      face_art: next.face_art.filter(
        (fa) =>
          fa.face !== 0 &&
          !(fa.scryfall?.scryfall_id === p.scryfallId && fa.scryfall.source_face === fa.face),
      ),
    };
  }
  for (const [face, choice] of change.faces) {
    const kept = next.face_art.filter((fa) => fa.face !== face);
    let added: CardFaceArt | null = null;
    if (choice.kind === "art") {
      added = {
        face,
        total_faces: choice.totalFaces,
        custom_image_id: null,
        custom_hash: null,
        label: choice.art.name,
        mpc_drive_id: choice.art.drive_id,
        mpc_source: choice.art.source_name,
      };
    } else if (choice.pick.scryfallId !== next.scryfall_id) {
      added = {
        face,
        total_faces: choice.totalFaces,
        custom_image_id: null,
        custom_hash: null,
        label: choice.pick.name,
        mpc_drive_id: null,
        mpc_source: null,
        scryfall: sourceOf(choice.pick, face),
      };
    }
    next = { ...next, face_art: (added ? [...kept, added] : kept).sort((a, b) => a.face - b.face) };
  }
  return next;
}

/** A card's staged changes with one more choice folded in, touching only
 *  the chosen face; undefined when the card ends up as it is. */
function withChoice(
  card: CardRow,
  prev: StagedChanges | undefined,
  choice: WalkChoice,
): StagedChanges | undefined {
  const faces = new Map(prev?.faces);
  faces.delete(choice.face);
  let unchanged: boolean;
  if (choice.kind === "printing") {
    // What the face prints with nothing chosen for it (a back with no
    // printing of its own follows the front, as chosen so far).
    unchanged = facePrinting(card, { faces }, choice.face) === choice.pick.scryfallId;
    if (!unchanged) {
      faces.set(choice.face, { kind: "printing", totalFaces: choice.totalFaces, pick: choice.pick });
    }
  } else {
    const existing = card.face_art?.find((fa) => fa.face === choice.face);
    // Already-fetched art is no change; art an import chose and left
    // undownloaded still downloads.
    unchanged =
      existing?.mpc_drive_id === choice.art.drive_id && existing.custom_image_id != null;
    if (!unchanged) {
      faces.set(choice.face, { kind: "art", totalFaces: choice.totalFaces, art: choice.art });
    }
  }
  return faces.size > 0 ? { faces } : undefined;
}

export default function DeckArtWalk(props: {
  /** The deck in the Decklist's current sort: the cards with printings. */
  cards: CardRow[];
  startCardId: number;
  startRequest: PickerOpenRequest;
  preferredLang: string;
  apply: WalkApply;
  /** The walk closed, applied or not. */
  onClose: () => void;
}) {
  const { cards, preferredLang } = props;
  const [cardId, setCardId] = useState(props.startCardId);
  const index = Math.max(0, cards.findIndex((c) => c.id === cardId));
  const card = cards[index];
  const [staged, setStaged] = useState<ReadonlyMap<number, StagedChanges>>(new Map());
  // Every card shown so far: Confirm downloads them all.
  const [visited, setVisited] = useState<ReadonlySet<number>>(() => new Set([props.startCardId]));
  // The next card opens on the tab this one was left on.
  const [request, setRequest] = useState<PickerOpenRequest>(props.startRequest);
  const [source, setSource] = useState(props.startRequest.source);
  // ... and with the filters it was left with.
  const [filters, setFilters] = useState<PickerFilters>(DEFAULT_PICKER_FILTERS);
  // Closing with choices made: Keep, Discard or Cancel.
  const [asking, setAsking] = useState(false);

  const go = (next: number) => {
    const target = cards[Math.max(0, Math.min(cards.length - 1, next))];
    if (!target || target.id === card?.id) return;
    setCardId(target.id);
    setVisited((prev) => new Set(prev).add(target.id));
    // On MPC Fill, at the first face printing some; else the front.
    const art = source === "mpc" ? target.face_art?.find(isImageFaceArt) : undefined;
    setRequest({ source, face: art?.face ?? 0 });
  };

  const choose = (choice: WalkChoice) => {
    const next = new Map(staged);
    const changes = withChoice(card, staged.get(card.id), choice);
    if (changes) next.set(card.id, changes);
    else next.delete(card.id);
    setStaged(next);
    return next;
  };

  function finish(changes: ReadonlyMap<number, StagedChanges>) {
    for (const id of new Set([...visited, ...changes.keys()])) {
      const target = cards.find((c) => c.id === id);
      if (!target) continue;
      const change = changes.get(id);
      if (change) void applyCard(target, change);
      else props.apply.download(target);
    }
    props.onClose();
  }

  /** One card's choices, in order (the front's printing decides what the
   *  back follows), then its download as it now prints. */
  async function applyCard(target: CardRow, change: StagedChanges) {
    const { apply } = props;
    const hasArt = (face: number) => (target.face_art ?? []).some((fa) => fa.face === face);
    try {
      const front = change.faces.get(0);
      if (front?.kind === "printing") {
        if (front.pick.scryfallId !== target.scryfall_id) await apply.printing(target, front.pick);
        else if (hasArt(0)) await apply.clearFaceArt(target, 0);
      } else if (front?.kind === "art") {
        apply.faceArt(target, 0, front.totalFaces, front.art);
      }
      const cardPrinting =
        front?.kind === "printing" ? front.pick.scryfallId : target.scryfall_id;
      const back = change.faces.get(1);
      if (back?.kind === "printing") {
        // The front's printing for the back re-links it: no override.
        if (back.pick.scryfallId === cardPrinting) {
          if (hasArt(1)) await apply.clearFaceArt(target, 1);
        } else {
          await apply.facePrinting(target, 1, back.totalFaces, sourceOf(back.pick, 1));
        }
      } else if (back?.kind === "art") {
        apply.faceArt(target, 1, back.totalFaces, back.art);
      }
    } catch {
      // Each apply surfaces its own error; download what did land.
    }
    apply.download(cardAfter(target, change));
  }

  function requestClose() {
    if (staged.size === 0) props.onClose();
    else setAsking(true);
  }

  // Alt+← / Alt+→ step through the deck from anywhere in the walk but a
  // text field, where they move by word.
  useEffect(() => {
    function onKeyDown(e: KeyboardEvent) {
      if (asking || !e.altKey || (e.key !== "ArrowLeft" && e.key !== "ArrowRight")) return;
      const target = e.target as HTMLElement | null;
      if (target?.closest("input, textarea, [contenteditable]")) return;
      e.preventDefault();
      go(index + (e.key === "ArrowLeft" ? -1 : 1));
    }
    document.addEventListener("keydown", onKeyDown);
    return () => document.removeEventListener("keydown", onKeyDown);
  });

  if (!card) return null;

  const prevCard = cards[index - 1];
  const nextCard = cards[index + 1];
  // Close in the head; the deck stepper and Confirm in a bar under the
  // grid, so the head keeps its width for the Front / Back switch.
  // Hovering Prev / Next names the card it goes to.
  const nav = (
    <button type="button" className="btn-sm" title="Close (Esc)" onClick={requestClose}>
      Close
    </button>
  );
  const footer = (
    <>
      <span className="walk-head" data-tour="picker-walk">
        <button
          type="button"
          className="btn-sm"
          disabled={index === 0}
          title={prevCard ? `Previous: ${prevCard.name} (Alt+←)` : "Previous card (Alt+←)"}
          onClick={() => go(index - 1)}
        >
          ‹ Prev
        </button>
        <span className="walk-pos">
          Card {index + 1} of {cards.length}
        </span>
        <button
          type="button"
          className="btn-sm"
          disabled={index === cards.length - 1}
          title={nextCard ? `Next: ${nextCard.name} (Alt+→)` : "Next card (Alt+→)"}
          onClick={() => go(index + 1)}
        >
          Next ›
        </button>
      </span>
      <button
        type="button"
        className="btn-sm btn-primary"
        title="Apply your choices for every card and close"
        onClick={() => finish(staged)}
      >
        Confirm
      </button>
    </>
  );

  return (
    <>
    <PrintingGridModal
      // A new card is a fresh grid: its own selection and scroll, but the
      // filters carry over.
      key={card.id}
      card={card}
      preferredLang={preferredLang}
      initial={request}
      // Esc and a click outside are Close.
      onClose={requestClose}
      onSourceChange={setSource}
      filters={filters}
      onFiltersChange={(patch) =>
        setFilters((prev) => ({
          printing: patch.printing ?? prev.printing,
          mpc: { ...prev.mpc, ...patch.mpc },
        }))
      }
      walk={{
        nav,
        staged: staged.get(card.id) ?? null,
        onSelect: (choice) => void choose(choice),
        onChoose: (choice) => finish(choose(choice)),
        footer,
      }}
    />
    {asking && (
      <KeepChangesDialog
        count={staged.size}
        onKeep={() => finish(staged)}
        onDiscard={props.onClose}
        onCancel={() => setAsking(false)}
      />
    )}
    </>
  );
}

/** Closing the walk with choices made. Esc and a click outside are Cancel,
 *  the answer that changes nothing; Keep is focused, the one that loses
 *  nothing. */
function KeepChangesDialog(props: {
  count: number;
  onKeep: () => void;
  onDiscard: () => void;
  onCancel: () => void;
}) {
  const { onCancel } = props;
  useEffect(() => {
    // Capturing, and marked handled, so the picker underneath (which
    // listens for Esc too, and skips a handled one) doesn't take it as
    // another Close.
    function onKeyDown(e: KeyboardEvent) {
      if (e.key !== "Escape") return;
      e.preventDefault();
      onCancel();
    }
    document.addEventListener("keydown", onKeyDown, true);
    return () => document.removeEventListener("keydown", onKeyDown, true);
  }, [onCancel]);
  return (
    <ModalOverlay onClick={onCancel}>
      <div className="modal modal-sm" role="alertdialog" onClick={(e) => e.stopPropagation()}>
        <div className="modal-head">
          <span className="modal-title">Keep Your Changes?</span>
        </div>
        <p style={{ marginBottom: 16 }}>
          You changed the art for {props.count} card{props.count === 1 ? "" : "s"} without
          confirming. Discard will undo all changes.
        </p>
        <div className="modal-actions">
          <button className="btn-primary" autoFocus onClick={props.onKeep}>
            Keep
          </button>
          <button className="btn-danger" onClick={props.onDiscard}>
            Discard
          </button>
          <button onClick={onCancel}>Cancel</button>
        </div>
      </div>
    </ModalOverlay>
  );
}
