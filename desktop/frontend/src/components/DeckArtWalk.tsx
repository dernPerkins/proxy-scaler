// The deck walk: the change-printing modal (PrintingGridModal) for one card
// at a time, with ‹ Prev / Next › (or Alt+← / Alt+→) through the Decklist
// in its current sort. Selecting a printing or MPC Fill art chooses it for
// that card. Nothing is saved or downloaded while the walk is open, so
// browsing past a card costs no downloads: Done, Esc, a click outside, the
// preview's button or a double-click close it and apply every card's
// choice; Discard closes it applying nothing.
// Chosen from a prototype (.scratch/mpcfill-art-picker/prototype-deck-walk/).
import { useEffect, useState } from "react";
import type { CardRow } from "../api/project";
import type { MpcCard } from "../api/types";
import {
  PrintingGridModal,
  type Pick,
  type PickerOpenRequest,
  type StagedChanges,
  type WalkChoice,
} from "./PrintingPicker";

/** How the Decklist applies the walk's choices, card by card. */
export interface WalkApply {
  printing: (card: CardRow, printing: Pick) => void;
  faceArt: (card: CardRow, face: number, totalFaces: number, art: MpcCard) => void;
  clearFaceArt: (card: CardRow, face: number) => void;
}

/** A card's staged changes with one more choice folded in; undefined when
 *  the card ends up as it is. */
function withChoice(
  card: CardRow,
  prev: StagedChanges | undefined,
  choice: WalkChoice,
): StagedChanges | undefined {
  let printing = prev?.printing ?? null;
  const faces = new Map(prev?.faces);
  if (choice.kind === "printing") {
    printing = choice.pick;
    // A new printing prints its own art (setCardPrinting drops face art).
    if (choice.pick) faces.clear();
  } else {
    // MPC Fill art goes on the card's own printing.
    printing = null;
    const existing = card.face_art?.find((fa) => fa.face === choice.face);
    // Already-fetched art is no change; art an import chose and left
    // undownloaded still downloads.
    const unchanged = choice.art
      ? existing?.mpc_drive_id === choice.art.drive_id && existing.custom_image_id != null
      : existing == null;
    if (unchanged) faces.delete(choice.face);
    else faces.set(choice.face, { totalFaces: choice.totalFaces, art: choice.art });
  }
  return printing || faces.size > 0 ? { printing, faces } : undefined;
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
  // The next card opens on the tab this one was left on.
  const [request, setRequest] = useState<PickerOpenRequest>(props.startRequest);
  const [source, setSource] = useState(props.startRequest.source);

  const go = (next: number) => {
    const target = cards[Math.max(0, Math.min(cards.length - 1, next))];
    if (!target || target.id === card?.id) return;
    setCardId(target.id);
    setRequest({ source, face: target.face_art?.[0]?.face ?? 0 });
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
    for (const [id, change] of changes) {
      const target = cards.find((c) => c.id === id);
      if (!target) continue;
      if (change.printing) props.apply.printing(target, change.printing);
      for (const [face, { totalFaces, art }] of change.faces) {
        if (art) props.apply.faceArt(target, face, totalFaces, art);
        else props.apply.clearFaceArt(target, face);
      }
    }
    props.onClose();
  }

  // Alt+← / Alt+→ step through the deck from anywhere in the walk but a
  // text field, where they move by word.
  useEffect(() => {
    function onKeyDown(e: KeyboardEvent) {
      if (!e.altKey || (e.key !== "ArrowLeft" && e.key !== "ArrowRight")) return;
      const target = e.target as HTMLElement | null;
      if (target?.closest("input, textarea, [contenteditable]")) return;
      e.preventDefault();
      go(index + (e.key === "ArrowLeft" ? -1 : 1));
    }
    document.addEventListener("keydown", onKeyDown);
    return () => document.removeEventListener("keydown", onKeyDown);
  });

  if (!card) return null;
  const changes = staged.size;

  const nav = (
    <>
      <span className="walk-head" data-tour="picker-walk">
        <button
          type="button"
          className="btn-sm"
          disabled={index === 0}
          title="Previous card (Alt+←)"
          onClick={() => go(index - 1)}
        >
          ‹ Prev
        </button>
        <span className="walk-pos">
          Card {index + 1} of {cards.length}
        </span>
        <span className="walk-name">{card.name}</span>
        <button
          type="button"
          className="btn-sm"
          disabled={index === cards.length - 1}
          title="Next card (Alt+→)"
          onClick={() => go(index + 1)}
        >
          Next ›
        </button>
      </span>
      {changes > 0 && (
        <span className="walk-changes">
          {changes} {changes === 1 ? "Change" : "Changes"}
        </span>
      )}
      <button
        type="button"
        className="btn-sm"
        disabled={changes === 0}
        title="Close without applying these changes"
        onClick={props.onClose}
      >
        Discard
      </button>
      <button type="button" className="btn-sm btn-primary" onClick={() => finish(staged)}>
        Done
      </button>
    </>
  );

  return (
    <PrintingGridModal
      // A new card is a fresh grid: its own filters, selection and scroll.
      key={card.id}
      card={card}
      preferredLang={preferredLang}
      initial={request}
      onClose={() => finish(staged)}
      onSourceChange={setSource}
      walk={{
        nav,
        staged: staged.get(card.id) ?? null,
        onSelect: (choice) => void choose(choice),
        onChoose: (choice) => finish(choose(choice)),
      }}
    />
  );
}
