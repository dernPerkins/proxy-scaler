import { useEffect, useLayoutEffect, useRef, useState } from "react";
import { createPortal } from "react-dom";
import type { Placement } from "./tours";
import {
  findTourTarget,
  nextStep,
  prevStep,
  skipTour,
  useTutorialState,
} from "./tutorialStore";

// The one overlay that draws every tutorial: a dimmed screen with a
// spotlight cut around the step's target, and a card beside it. Mounted
// once in main.tsx, above ConnectGate, so the connect screen's tour and
// the tabs' tours share it. Portaled to document.body for the same reason
// ModalOverlay is — the sticky sidebar's stacking context would otherwise
// trap it — and layered above modals (1000) but below toasts (2000).
//
// Clicks outside the card are swallowed: a tour step pointing at a control
// the user just navigated away from would leave the spotlight on nothing.

interface Rect {
  top: number;
  left: number;
  width: number;
  height: number;
}

const PAD = 6; // spotlight breathing room around the target
const GAP = 12; // between the spotlight and the card
const MARGIN = 12; // minimum distance from the viewport edge

function sameRect(a: Rect | null, b: Rect | null): boolean {
  if (a == null || b == null) return a === b;
  return a.top === b.top && a.left === b.left && a.width === b.width && a.height === b.height;
}

function measure(target: string | undefined): Rect | null {
  if (!target) return null;
  const el = findTourTarget(target);
  if (!el) return null;
  const r = el.getBoundingClientRect();
  if (r.width === 0 && r.height === 0) return null;
  return {
    top: r.top - PAD,
    left: r.left - PAD,
    width: r.width + PAD * 2,
    height: r.height + PAD * 2,
  };
}

// Tries the preferred side, then its opposite, then the other two; the
// first where the card fits wins, and the result is clamped on-screen
// either way (a target taller than the window still gets a visible card).
function placeCard(
  spot: Rect,
  card: { width: number; height: number },
  preferred: Placement,
): { top: number; left: number } {
  const vw = window.innerWidth;
  const vh = window.innerHeight;
  const opposite: Record<Placement, Placement> = {
    top: "bottom",
    bottom: "top",
    left: "right",
    right: "left",
  };
  const order: Placement[] = [preferred, opposite[preferred]];
  for (const p of ["bottom", "top", "right", "left"] as Placement[]) {
    if (!order.includes(p)) order.push(p);
  }
  const fits: Record<Placement, boolean> = {
    top: spot.top - GAP - card.height >= MARGIN,
    bottom: spot.top + spot.height + GAP + card.height <= vh - MARGIN,
    left: spot.left - GAP - card.width >= MARGIN,
    right: spot.left + spot.width + GAP + card.width <= vw - MARGIN,
  };
  const side = order.find((p) => fits[p]) ?? preferred;
  let top: number;
  let left: number;
  if (side === "top" || side === "bottom") {
    top = side === "top" ? spot.top - GAP - card.height : spot.top + spot.height + GAP;
    left = spot.left + spot.width / 2 - card.width / 2;
  } else {
    left = side === "left" ? spot.left - GAP - card.width : spot.left + spot.width + GAP;
    top = spot.top + spot.height / 2 - card.height / 2;
  }
  return {
    top: Math.min(Math.max(top, MARGIN), vh - card.height - MARGIN),
    left: Math.min(Math.max(left, MARGIN), vw - card.width - MARGIN),
  };
}

export default function TutorialOverlay() {
  const { active } = useTutorialState();
  const segment = active ? active.segments[active.segment] : null;
  const step = segment && active ? segment.steps[active.step] : null;
  const target = step?.target;

  const [spot, setSpot] = useState<Rect | null>(null);
  const [cardPos, setCardPos] = useState<{ top: number; left: number } | null>(null);
  const cardRef = useRef<HTMLDivElement>(null);
  const nextRef = useRef<HTMLButtonElement>(null);

  // Bring the target into view, then follow it every frame: sidebars
  // scroll, queries fill in and shift layout, the window resizes. A rAF
  // loop that only sets state on change is simpler and sturdier than
  // wiring scroll/resize/mutation observers for every case.
  //
  // A layout effect, and the first measurement is set unconditionally:
  // the overlay stays mounted between tours, so the previous step's
  // spotlight (say, the connect screen's ? button) must be replaced
  // before paint, not left standing when this step has no target at all.
  useLayoutEffect(() => {
    if (!step) {
      setSpot(null);
      return;
    }
    if (target) {
      findTourTarget(target)?.scrollIntoView({ block: "nearest", inline: "nearest" });
    }
    let last = measure(target);
    setSpot(last);
    let frame = 0;
    const tick = () => {
      const next = measure(target);
      if (!sameRect(next, last)) {
        last = next;
        setSpot(next);
      }
      frame = requestAnimationFrame(tick);
    };
    frame = requestAnimationFrame(tick);
    return () => cancelAnimationFrame(frame);
  }, [step, target]);

  useLayoutEffect(() => {
    const card = cardRef.current;
    if (!step || !card) return;
    const size = { width: card.offsetWidth, height: card.offsetHeight };
    setCardPos(
      spot
        ? placeCard(spot, size, step.placement ?? "bottom")
        : {
            top: (window.innerHeight - size.height) / 2,
            left: (window.innerWidth - size.width) / 2,
          },
    );
  }, [spot, step]);

  useEffect(() => {
    if (step) nextRef.current?.focus();
  }, [step]);

  // Capture phase so an open page's own Escape handling (popovers) never
  // sees the key a tour step consumed.
  useEffect(() => {
    if (!active) return;
    const onKey = (e: KeyboardEvent) => {
      if (e.key === "Escape") skipTour();
      else if (e.key === "ArrowRight") nextStep();
      else if (e.key === "ArrowLeft") prevStep();
      else return;
      e.preventDefault();
      e.stopPropagation();
    };
    window.addEventListener("keydown", onKey, true);
    return () => window.removeEventListener("keydown", onKey, true);
  }, [active]);

  if (!active || !segment || !step) return null;

  const isFirst = active.segment === 0 && active.step === 0;
  const isLast =
    active.segment === active.segments.length - 1 && active.step === segment.steps.length - 1;

  return createPortal(
    <div className="tour-root" role="dialog" aria-modal="true" aria-labelledby="tour-title">
      {/* Swallows every click that misses the card. */}
      <div className={spot ? "tour-blocker" : "tour-blocker tour-dim"} />
      {spot && (
        <div
          className="tour-spotlight"
          style={{ top: spot.top, left: spot.left, width: spot.width, height: spot.height }}
        />
      )}
      <div
        ref={cardRef}
        className="tour-card"
        style={
          cardPos
            ? { top: cardPos.top, left: cardPos.left }
            : { top: 0, left: 0, visibility: "hidden" }
        }
      >
        <div className="tour-count">
          {active.step + 1} / {segment.steps.length}
        </div>
        <h3 id="tour-title" className="tour-title">
          {step.title}
        </h3>
        <p className="tour-body">{step.body}</p>
        <div className="tour-actions">
          <button className="btn-sm tour-skip" onClick={skipTour}>
            Skip tutorial
          </button>
          <span className="tour-nav">
            {!isFirst && (
              <button className="btn-sm" onClick={prevStep}>
                Back
              </button>
            )}
            <button ref={nextRef} className="btn-primary btn-sm" onClick={nextStep}>
              {isLast ? "Done" : "Next"}
            </button>
          </span>
        </div>
      </div>
    </div>,
    document.body,
  );
}
