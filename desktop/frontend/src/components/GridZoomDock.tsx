// The Grid view's card size control (ADR 0010), docked in the bottom-right
// corner while the Grid shows (the Back to Top button and the toasts move up
// above it — body.has-grid-zoom in styles.css).
//
// At rest it is a round accent button, Back to Top's twin, showing the zoom.
// It opens leftward over itself into: Calibrate, −, the size slider, + and
// the zoom. It stays open while the pointer is over it, while a drag is held
// (even if the pointer wanders off), while keyboard focus is inside it, and
// for a moment after a pinch or ⌘-key zoom, so you can see which size it
// landed on.
//
// The slider moves freely while dragged, then snaps to the next size in the
// direction it was moving when let go (gridZoom.ts's snapToward). A pinch
// over the grid moves the same value and snaps the same way when it stops.
// (⌘-scroll deliberately doesn't zoom: it stays a plain scroll.) − / + and ⌘− / ⌘+ step one size; ⌘0 goes to Actual.
import { useCallback, useEffect, useRef, useState, type PointerEvent as ReactPointerEvent } from "react";
import {
  CARD_SIZES,
  MAX_ZOOM,
  MIN_ZOOM,
  clampZoom,
  sizeAt,
  snapToward,
  stepFrom,
  zoomLabel,
  type GridCardSize,
} from "../gridZoom";
import { hasShortcutModifier, isDialogOpen, shortcutLabel } from "../shortcuts";
import CalibrateCardSize from "./CalibrateCardSize";

/** How far (in zoom) a drag must come back before its direction flips:
 *  1.5% is about 4px of the slider, so a hand's wobble while holding still
 *  doesn't turn a drag up into a snap down. */
const TURN_BACK = 0.015;
/** How long the snap glides. */
const GLIDE_MS = 140;
/** A wheel pinch has no end event: this much quiet ends it. */
const WHEEL_END_MS = 180;

export default function GridZoomDock({ size }: { size: GridCardSize }) {
  const { zoom, setLive, save } = size;
  const zoomRef = useRef(zoom);
  zoomRef.current = zoom;
  const [calibrating, setCalibrating] = useState(false);

  // The direction a release snaps toward: the last way the value went, kept
  // until a snap uses it. A turn back only counts after TURN_BACK of travel.
  const dir = useRef(0);
  const against = useRef(0);
  const anim = useRef<number | undefined>(undefined);

  /** A live, continuous move (drag, pinch): on screen, not yet saved. */
  const move = useCallback(
    (z: number) => {
      if (anim.current) cancelAnimationFrame(anim.current);
      anim.current = undefined;
      const next = clampZoom(z);
      const d = next - zoomRef.current;
      if (Math.abs(d) > 1e-4) {
        const s = Math.sign(d);
        if (dir.current === 0 || s === dir.current) {
          dir.current = s;
          against.current = 0;
        } else if ((against.current += Math.abs(d)) >= TURN_BACK) {
          dir.current = s;
          against.current = 0;
        }
      }
      zoomRef.current = next;
      setLive(next);
    },
    [setLive],
  );

  /** Glide to a size, then save it. */
  const glideTo = useCallback(
    (target: number) => {
      if (anim.current) cancelAnimationFrame(anim.current);
      anim.current = undefined;
      const from = zoomRef.current;
      if (Math.abs(from - target) < 1e-4) return save(target);
      const t0 = performance.now();
      const tick = (t: number) => {
        const k = Math.min(1, (t - t0) / GLIDE_MS);
        const z = from + (target - from) * (1 - (1 - k) ** 3);
        zoomRef.current = z;
        if (k < 1) {
          setLive(z);
          anim.current = requestAnimationFrame(tick);
        } else {
          anim.current = undefined;
          save(target);
        }
      };
      anim.current = requestAnimationFrame(tick);
    },
    [setLive, save],
  );
  useEffect(
    () => () => {
      if (anim.current) cancelAnimationFrame(anim.current);
    },
    [],
  );

  const settle = useCallback(() => {
    glideTo(snapToward(zoomRef.current, dir.current));
    dir.current = 0;
    against.current = 0;
  }, [glideTo]);

  // --- open / closed ---
  const [hover, setHover] = useState(false);
  const [held, setHeld] = useState(false);
  const [focused, setFocused] = useState(false);
  const [peeking, setPeeking] = useState(false);
  const leaveT = useRef<number | undefined>(undefined);
  const peekT = useRef<number | undefined>(undefined);
  const open = hover || held || focused || peeking;
  const peek = useCallback(() => {
    setPeeking(true);
    window.clearTimeout(peekT.current);
    peekT.current = window.setTimeout(() => setPeeking(false), 1200);
  }, []);
  useEffect(
    () => () => {
      window.clearTimeout(leaveT.current);
      window.clearTimeout(peekT.current);
    },
    [],
  );

  useEffect(() => {
    document.body.classList.add("has-grid-zoom");
    return () => document.body.classList.remove("has-grid-zoom");
  }, []);

  // --- the slider ---
  // Our own track rather than <input type="range">: WebKit's native range
  // sends no release the page can count on when the mouse is held still as
  // the button comes up, which left it between sizes. The track captures
  // the pointer on press, so the release always arrives here.
  const trackRef = useRef<HTMLSpanElement>(null);
  const dragging = useRef(false);
  const grabOffset = useRef(0);
  const zoomAtX = (clientX: number) => {
    const r = trackRef.current!.getBoundingClientRect();
    const f = Math.min(1, Math.max(0, (clientX - r.left) / r.width));
    return MIN_ZOOM + f * (MAX_ZOOM - MIN_ZOOM);
  };
  const onTrackDown = (e: ReactPointerEvent<HTMLSpanElement>) => {
    if (e.button !== 0) return;
    e.preventDefault();
    trackRef.current!.focus();
    trackRef.current!.setPointerCapture(e.pointerId);
    dragging.current = true;
    setHeld(true);
    // Grabbing the thumb keeps it under the pointer; pressing the track
    // jumps there first (and settles onward in the direction it jumped).
    const onThumb = (e.target as HTMLElement).closest(".zoom-thumb") != null;
    grabOffset.current = onThumb ? zoomAtX(e.clientX) - zoomRef.current : 0;
    if (!onThumb) move(zoomAtX(e.clientX));
  };
  const onTrackMove = (e: ReactPointerEvent<HTMLSpanElement>) => {
    if (dragging.current) move(zoomAtX(e.clientX) - grabOffset.current);
  };
  const onTrackUp = () => {
    if (!dragging.current) return;
    dragging.current = false;
    setHeld(false);
    settle();
  };

  // --- ⌘− / ⌘+ / ⌘0 ---
  useEffect(() => {
    const onKey = (e: KeyboardEvent) => {
      if (!hasShortcutModifier(e) || isDialogOpen()) return;
      if (e.key === "=" || e.key === "+") glideTo(stepFrom(zoomRef.current, 1));
      else if (e.key === "-") glideTo(stepFrom(zoomRef.current, -1));
      else if (e.key === "0") glideTo(1);
      else return;
      e.preventDefault();
      peek();
    };
    window.addEventListener("keydown", onKey);
    return () => window.removeEventListener("keydown", onKey);
  }, [glideTo, peek]);

  // --- pinch over the grid ---
  useEffect(() => {
    let wheelEnd: number | undefined;
    let gestureFrom = 1;
    // A trackpad pinch arrives as a ctrl+wheel in WebKit (as does a mouse's
    // Ctrl-scroll, which is the same gesture to the page). ⌘-scroll is a
    // meta+wheel and is left alone. Only over the cards, so a pinch elsewhere
    // does nothing here.
    const onWheel = (e: WheelEvent) => {
      if (!e.ctrlKey) return;
      if (!(e.target as HTMLElement | null)?.closest(".deck-pages")) return;
      e.preventDefault();
      move(zoomRef.current * Math.exp(-e.deltaY * 0.01));
      peek();
      window.clearTimeout(wheelEnd);
      wheelEnd = window.setTimeout(settle, WHEEL_END_MS);
    };
    // WebKit's own pinch events, for when the web view sends those instead.
    const onGestureStart = (e: Event) => {
      e.preventDefault();
      gestureFrom = zoomRef.current;
    };
    const onGestureChange = (e: Event) => {
      e.preventDefault();
      move(gestureFrom * (e as Event & { scale: number }).scale);
      peek();
    };
    const onGestureEnd = (e: Event) => {
      e.preventDefault();
      settle();
    };
    window.addEventListener("wheel", onWheel, { passive: false });
    window.addEventListener("gesturestart", onGestureStart);
    window.addEventListener("gesturechange", onGestureChange);
    window.addEventListener("gestureend", onGestureEnd);
    return () => {
      window.clearTimeout(wheelEnd);
      window.removeEventListener("wheel", onWheel);
      window.removeEventListener("gesturestart", onGestureStart);
      window.removeEventListener("gesturechange", onGestureChange);
      window.removeEventListener("gestureend", onGestureEnd);
    };
  }, [move, settle, peek]);

  const at = sizeAt(zoom);
  const frac = (z: number) => (z - MIN_ZOOM) / (MAX_ZOOM - MIN_ZOOM);
  const tab = open ? 0 : -1;
  return (
    <>
      <div
        className={"zoom-dock" + (open ? " is-open" : "")}
        onPointerEnter={() => {
          window.clearTimeout(leaveT.current);
          setHover(true);
        }}
        onPointerLeave={() => {
          window.clearTimeout(leaveT.current);
          leaveT.current = window.setTimeout(() => setHover(false), 250);
        }}
        onFocus={() => setFocused(true)}
        onBlur={(e) => {
          if (!e.currentTarget.contains(e.relatedTarget as Node | null)) setFocused(false);
        }}
      >
        <button
          type="button"
          className="zoom-dock-button"
          aria-label={`Card Size ${zoomLabel(zoom)}`}
          onClick={() => trackRef.current?.focus()}
        >
          {zoomLabel(zoom)}
        </button>
        <div className="zoom-dock-panel" aria-hidden={!open}>
          <button
            type="button"
            className="zoom-dock-calibrate"
            title="Match Actual size to a real card held against the screen"
            tabIndex={tab}
            onClick={() => setCalibrating(true)}
          >
            Calibrate
          </button>
          <span className="zoom-dock-sep" aria-hidden="true" />
          <button
            type="button"
            className="zoom-dock-step"
            aria-label="Smaller"
            title={`Smaller · ${shortcutLabel("−")}`}
            tabIndex={tab}
            disabled={zoom <= MIN_ZOOM + 1e-3}
            onClick={() => glideTo(stepFrom(zoomRef.current, -1))}
          >
            <ZoomIcon minus />
          </button>
          <span className="zoom-slider">
            <span
              ref={trackRef}
              className="zoom-track"
              role="slider"
              tabIndex={tab}
              aria-label="Card Size"
              aria-valuemin={Math.round(MIN_ZOOM * 100)}
              aria-valuemax={Math.round(MAX_ZOOM * 100)}
              aria-valuenow={Math.round(zoom * 100)}
              aria-valuetext={at ? `${at.label}, ${zoomLabel(at.zoom)}` : zoomLabel(zoom)}
              onPointerDown={onTrackDown}
              onPointerMove={onTrackMove}
              onPointerUp={onTrackUp}
              onPointerCancel={onTrackUp}
              onLostPointerCapture={onTrackUp}
              onKeyDown={(e) => {
                const d =
                  e.key === "ArrowRight" || e.key === "ArrowUp"
                    ? 1
                    : e.key === "ArrowLeft" || e.key === "ArrowDown"
                      ? -1
                      : 0;
                if (e.key === "Home") glideTo(MIN_ZOOM);
                else if (e.key === "End") glideTo(MAX_ZOOM);
                else if (d) glideTo(stepFrom(zoomRef.current, d));
                else return;
                e.preventDefault();
              }}
            >
              <span className="zoom-rail" />
              <span className="zoom-fill" style={{ width: `${frac(zoom) * 100}%` }} />
              {CARD_SIZES.map((s) => (
                <span
                  key={s.label}
                  className={"zoom-notch" + (s.zoom <= zoom + 1e-3 ? " is-passed" : "")}
                  style={{ left: `${frac(s.zoom) * 100}%` }}
                />
              ))}
              <span className="zoom-thumb" style={{ left: `${frac(zoom) * 100}%` }} />
            </span>
            <span className="zoom-labels" aria-hidden="true">
              {CARD_SIZES.map((s) => (
                <button
                  key={s.label}
                  type="button"
                  tabIndex={-1}
                  className={"zoom-label" + (at === s ? " is-on" : "")}
                  style={{ left: `${frac(s.zoom) * 100}%` }}
                  onClick={() => glideTo(s.zoom)}
                  title={zoomLabel(s.zoom)}
                >
                  {s.label}
                </button>
              ))}
            </span>
          </span>
          <button
            type="button"
            className="zoom-dock-step"
            aria-label="Larger"
            title={`Larger · ${shortcutLabel("+")}`}
            tabIndex={tab}
            disabled={zoom >= MAX_ZOOM - 1e-3}
            onClick={() => glideTo(stepFrom(zoomRef.current, 1))}
          >
            <ZoomIcon plus />
          </button>
          <span className="zoom-dock-value mono">{zoomLabel(zoom)}</span>
        </div>
      </div>
      {calibrating && <CalibrateCardSize scale={size.scale} onClose={() => setCalibrating(false)} />}
    </>
  );
}

/** A magnifier with a minus or plus in the lens. */
function ZoomIcon({ plus, minus }: { plus?: boolean; minus?: boolean }) {
  return (
    <svg
      width={18}
      height={18}
      viewBox="0 0 24 24"
      fill="none"
      stroke="currentColor"
      strokeWidth={2}
      strokeLinecap="round"
      aria-hidden="true"
    >
      <circle cx="10.5" cy="10.5" r="6.5" />
      <path d="M15.5 15.5 L20 20" />
      {(plus || minus) && <path d="M7.5 10.5 H13.5" />}
      {plus && <path d="M10.5 7.5 V13.5" />}
    </svg>
  );
}
