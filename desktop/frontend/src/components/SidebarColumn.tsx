import { useLayoutEffect, useRef, type ReactNode } from "react";

/** The column a tab's sidebar sits in: sticky under the pinned tab bar,
 *  and scrolled on its own so the whole sidebar slides past the window's
 *  edge rather than scrolling inside itself. See styles.css.
 *
 *  Its height is what's visible below its top, kept up as the page
 *  scrolls. The stylesheet's max-height only fits once it's stuck: at the
 *  top of the page the column starts lower, under the project bar, and its
 *  bottom would hang past the window with no way to scroll to it. */
export default function SidebarColumn({ children }: { children: ReactNode }) {
  const ref = useRef<HTMLDivElement>(null);
  useLayoutEffect(() => {
    const el = ref.current;
    if (!el) return;
    let frame = 0;
    const fit = () => {
      frame = 0;
      el.style.maxHeight = `${Math.max(0, window.innerHeight - el.getBoundingClientRect().top)}px`;
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
    <div ref={ref} className="sidebar-column">
      {children}
    </div>
  );
}
