import { useEffect, useLayoutEffect, useRef, type ReactNode } from "react";

/** The column a tab's sidebar sits in: sticky under the pinned tab bar,
 *  and scrolled on its own so the whole sidebar slides past the window's
 *  edge rather than scrolling inside itself. See styles.css.
 *
 *  Its height is what's visible below its top, kept up as the page
 *  scrolls. The stylesheet's max-height only fits once it's stuck: at the
 *  top of the page the column starts lower, under the project bar, and its
 *  bottom would hang past the window with no way to scroll to it.
 *
 *  On a short page (a small deck) the column is what sets the page's
 *  height, so its height must only ever follow the page's real scroll
 *  position. Two things move it further up than that: the window's
 *  rubber-band past the bottom, and the end of the layout pushing it up
 *  off its sticky top. Measured as is, either one grew the column, which
 *  grew the page, which turned the overscroll into real scroll, over and
 *  over until the whole sidebar was showing. So the overscroll is taken
 *  back out of the measured top, and the top is never read as above the
 *  sticky position. */
export default function SidebarColumn({
  children,
  className,
  hidden = false,
  animate = false,
}: {
  children: ReactNode;
  /** Extra classes on the column, e.g. a wider layout for one tab. */
  className?: string;
  /** Slid away to the left, the content taking its room (the Decklist's
   *  Cmd/Ctrl+B). Stays mounted, so nothing in it loses its state. */
  hidden?: boolean;
  /** Slide rather than jump: off until the user toggles, so a hidden state
   *  loaded at launch lands in place. */
  animate?: boolean;
}) {
  const ref = useRef<HTMLDivElement>(null);
  // The hidden column's margin pulls it back by exactly its own width, so
  // the width is kept in a variable for the stylesheet. Only while it can
  // be hidden: no other tab needs the observer.
  const hideable = hidden || animate;
  useLayoutEffect(() => {
    const el = ref.current;
    if (!el || !hideable) return;
    const measure = () => el.style.setProperty("--sidebar-column-width", `${el.offsetWidth}px`);
    measure();
    const observer = new ResizeObserver(measure);
    observer.observe(el);
    return () => observer.disconnect();
  }, [hideable]);
  useEffect(() => {
    const el = ref.current;
    if (!el) return;
    // React 18 doesn't know the inert attribute; set the property instead.
    el.inert = hidden;
    if (hidden && el.contains(document.activeElement)) (document.activeElement as HTMLElement).blur();
  }, [hidden]);
  useLayoutEffect(() => {
    const el = ref.current;
    if (!el) return;
    let frame = 0;
    const fit = () => {
      frame = 0;
      const page = document.documentElement;
      const maxScroll = Math.max(0, page.scrollHeight - window.innerHeight);
      const overscroll = window.scrollY - Math.min(Math.max(window.scrollY, 0), maxScroll);
      const stickyTop = parseFloat(getComputedStyle(el).top) || 0;
      const top = Math.max(el.getBoundingClientRect().top + overscroll, stickyTop);
      el.style.maxHeight = `${Math.max(0, window.innerHeight - top)}px`;
    };
    const schedule = () => {
      if (!frame) frame = requestAnimationFrame(fit);
    };
    fit();
    window.addEventListener("scroll", schedule, { passive: true });
    window.addEventListener("resize", schedule);
    return () => {
      cancelAnimationFrame(frame);
      window.removeEventListener("scroll", schedule);
      window.removeEventListener("resize", schedule);
    };
  }, []);
  return (
    <div
      ref={ref}
      className={[
        "sidebar-column",
        className,
        hidden && "sidebar-column-hidden",
        animate && "sidebar-column-animate",
      ]
        .filter(Boolean)
        .join(" ")}
      aria-hidden={hidden || undefined}
    >
      {children}
    </div>
  );
}
