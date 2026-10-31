import { useLayoutEffect, useRef, type ReactNode } from "react";

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
}: {
  children: ReactNode;
  /** Extra classes on the column, e.g. a wider layout for one tab. */
  className?: string;
}) {
  const ref = useRef<HTMLDivElement>(null);
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
    <div ref={ref} className={className ? `sidebar-column ${className}` : "sidebar-column"}>
      {children}
    </div>
  );
}
