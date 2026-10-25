// The deck walk: the change-printing modal (PrintingGridModal) for one card
// at a time, with ‹ Prev / Next › (or Alt+← / Alt+→) through the Decklist
// in its current sort. Selecting a printing or MPC Fill art chooses it for
// that card. Nothing is saved while the walk is open: Confirm, the
// preview's button or a double-click close it, apply every card's choice
// and download every card the walk visited, changed or not, each as it
// will print (both faces of a double-faced card) — looking at a card and
// confirming is confirming it. Close, Esc or a click outside close it
// without downloading; with choices made they ask first — Keep (the same
// as Confirm), Discard or Cancel — so unconfirmed work isn't lost to a
// stray click. Picking the card's own printing on the Scryfall tab puts it
// back on Scryfall art.
// Chosen from a prototype (.scratch/mpcfill-art-picker/prototype-deck-walk/).
import { useEffect, useState } from "react";
import type { CardFaceArt, CardRow } from "../api/project";
import type { MpcCard } from "../api/types";
import ModalOverlay from "./ModalOverlay";
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
  /** Download a card the walk visited, as it prints once its choices are
   *  applied. `faces` is null for a card the walk left as it was. A card
   *  given a new printing isn't passed: `printing` downloads it. */
  download: (card: CardRow, faces: FacesAfter | null) => void;
}

/** A card's faces once the walk's face changes to it apply. */
export interface FacesAfter {
  /** How many faces its printing has. */
  totalFaces: number;
  /** The faces printing MPC Fill art (hash null while still downloading). */
  mpcFaces: { face: number; customHash: string | null }[];
  /** Its MPC Fill art on faces the walk didn't change, which may still be
   *  undownloaded (an import's choice). */
  kept: CardFaceArt[];
}

function facesAfter(card: CardRow, change: StagedChanges): FacesAfter {
  const mpc = new Map<number, string | null>();
  let totalFaces = 1;
  for (const fa of card.face_art ?? []) {
    mpc.set(fa.face, fa.custom_hash ?? null);
    totalFaces = Math.max(totalFaces, fa.total_faces);
  }
  for (const [face, { totalFaces: total, art }] of change.faces) {
    if (art) mpc.set(face, null);
    else mpc.delete(face);
    totalFaces = Math.max(totalFaces, total);
  }
  return {
    totalFaces,
    mpcFaces: [...mpc].map(([face, customHash]) => ({ face, customHash })),
    kept: (card.face_art ?? []).filter((fa) => !change.faces.has(fa.face)),
  };
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
    faces.clear();
    // A new printing prints its own art (setCardPrinting drops face art).
    // The card's own printing, picked on the Scryfall tab, means its own
    // Scryfall art too: any MPC Fill art on its faces comes off.
    if (!choice.pick) {
      for (const fa of card.face_art ?? []) {
        faces.set(fa.face, { totalFaces: fa.total_faces, art: null });
      }
    }
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
  // Every card shown so far: Confirm downloads them all.
  const [visited, setVisited] = useState<ReadonlySet<number>>(() => new Set([props.startCardId]));
  // The next card opens on the tab this one was left on.
  const [request, setRequest] = useState<PickerOpenRequest>(props.startRequest);
  const [source, setSource] = useState(props.startRequest.source);
  // Closing with choices made: Keep, Discard or Cancel.
  const [asking, setAsking] = useState(false);

  const go = (next: number) => {
    const target = cards[Math.max(0, Math.min(cards.length - 1, next))];
    if (!target || target.id === card?.id) return;
    setCardId(target.id);
    setVisited((prev) => new Set(prev).add(target.id));
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
    for (const id of new Set([...visited, ...changes.keys()])) {
      const target = cards.find((c) => c.id === id);
      if (!target) continue;
      const change = changes.get(id);
      if (!change) {
        props.apply.download(target, null);
        continue;
      }
      // A new printing downloads all of its faces itself.
      if (change.printing) props.apply.printing(target, change.printing);
      for (const [face, { totalFaces, art }] of change.faces) {
        if (art) props.apply.faceArt(target, face, totalFaces, art);
        else props.apply.clearFaceArt(target, face);
      }
      // Every face that prints downloads, not just the one chosen: a
      // double-faced card's other face arrives with its new front.
      if (!change.printing && change.faces.size > 0) {
        props.apply.download(target, facesAfter(target, change));
      }
    }
    props.onClose();
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
      <button
        type="button"
        className="btn-sm btn-primary"
        title="Apply your choices for every card and close"
        onClick={() => finish(staged)}
      >
        Confirm
      </button>
      <button type="button" className="btn-sm" title="Close (Esc)" onClick={requestClose}>
        Close
      </button>
    </>
  );

  return (
    <>
    <PrintingGridModal
      // A new card is a fresh grid: its own filters, selection and scroll.
      key={card.id}
      card={card}
      preferredLang={preferredLang}
      initial={request}
      // Esc and a click outside are Close.
      onClose={requestClose}
      onSourceChange={setSource}
      walk={{
        nav,
        staged: staged.get(card.id) ?? null,
        onSelect: (choice) => void choose(choice),
        onChoose: (choice) => finish(choose(choice)),
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
