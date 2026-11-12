import { useEffect, useLayoutEffect, useRef, useState, type ReactNode } from "react";

// How long the slide takes; matches .collapse-y's transition in styles.css.
const SLIDE_MS = 240;

/** A block that slides shut upward and open downward, staying mounted
 *  while shut — the Decklist's Add Cards box (Cmd/Ctrl+I) keeps its draft
 *  and its Cmd+K palette that way.
 *
 *  The slide is a grid row going 1fr ↔ 0fr, which needs the inside
 *  clipped. But Add Cards' search results hang below the box, so once it's
 *  fully open the clip comes off again. Settled on a timer rather than
 *  transitionend, which never fires under reduced motion or on a webview
 *  that can't animate grid rows — the clip would stay on for good.
 *
 *  `animate` is off until the user toggles: the saved state loading in at
 *  launch should land, not slide. */
export default function CollapseY({
  hidden,
  animate,
  children,
}: {
  hidden: boolean;
  animate: boolean;
  children: ReactNode;
}) {
  const ref = useRef<HTMLDivElement>(null);
  const [open, setOpen] = useState(!hidden);
  useLayoutEffect(() => {
    if (hidden) {
      setOpen(false);
      return;
    }
    if (!animate) {
      setOpen(true);
      return;
    }
    const timer = setTimeout(() => setOpen(true), SLIDE_MS + 20);
    return () => clearTimeout(timer);
  }, [hidden, animate]);
  useEffect(() => {
    const el = ref.current;
    if (!el) return;
    // React 18 doesn't know the inert attribute; set the property instead.
    el.inert = hidden;
    if (hidden && el.contains(document.activeElement)) (document.activeElement as HTMLElement).blur();
  }, [hidden]);
  const classes = ["collapse-y"];
  if (hidden) classes.push("collapse-y-hidden");
  if (open && !hidden) classes.push("collapse-y-open");
  if (animate) classes.push("collapse-y-animate");
  return (
    <div ref={ref} className={classes.join(" ")} aria-hidden={hidden || undefined}>
      <div className="collapse-y-inner">{children}</div>
    </div>
  );
}
