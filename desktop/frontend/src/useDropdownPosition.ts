// Where an open dropdown's list goes: SelectMenu's options and
// MultiSelectFilter's panel. Both render their list into document.body
// with position: fixed, so no modal body, sticky sidebar or other
// overflow container can clip it — which means placing it is ours to do.
//
// Below the toggle by default; above it when the list doesn't fit below
// and there's more room above (a dropdown near the bottom of the window).
// Its height is capped at the room on that side, so a long list scrolls
// rather than running off-screen, and it's nudged back inside the window
// horizontally. Recomputed on resize and on any scroll (captured, so a
// scrolling modal body counts) while open.
import { useLayoutEffect, useState, type CSSProperties, type RefObject } from "react";

// Gap between the toggle and the list, and the closest the list may get
// to the window's edge.
const GAP = 4;
const MARGIN = 8;

const HIDDEN: CSSProperties = { position: "fixed", top: 0, left: 0, visibility: "hidden" };

export function useDropdownPosition(
  open: boolean,
  anchorRef: RefObject<HTMLElement | null>,
  floatingRef: RefObject<HTMLElement | null>,
  { align = "start", maxHeight = 280 }: { align?: "start" | "end"; maxHeight?: number } = {},
): CSSProperties {
  const [style, setStyle] = useState<CSSProperties>(HIDDEN);

  useLayoutEffect(() => {
    if (!open) {
      // Measured fresh on the next open, rather than starting from where
      // it sat last time (the toggle may have moved since).
      setStyle(HIDDEN);
      return;
    }
    // The list's natural height, measured once per open while it is still
    // unconstrained. Not re-read on later passes: a panel that scrolls an
    // inner list (MultiSelectFilter) reports its capped height once capped,
    // and deciding the side from that would flip it back and forth.
    let natural: number | null = null;
    function update() {
      const anchor = anchorRef.current;
      const floating = floatingRef.current;
      if (!anchor || !floating) return;
      const rect = anchor.getBoundingClientRect();
      const viewportW = window.innerWidth;
      const viewportH = window.innerHeight;
      natural ??= Math.min(floating.scrollHeight, maxHeight);
      const below = viewportH - rect.bottom - GAP - MARGIN;
      const above = rect.top - GAP - MARGIN;
      const flip = natural > below && above > below;
      const room = Math.max(flip ? above : below, 0);
      const width = Math.max(floating.offsetWidth, rect.width);
      let left = align === "end" ? rect.right - width : rect.left;
      left = Math.max(MARGIN, Math.min(left, viewportW - MARGIN - width));
      setStyle({
        position: "fixed",
        left,
        minWidth: rect.width,
        maxHeight: Math.min(maxHeight, room),
        ...(flip ? { bottom: viewportH - rect.top + GAP } : { top: rect.bottom + GAP }),
      });
    }
    update();
    window.addEventListener("resize", update);
    window.addEventListener("scroll", update, true);
    return () => {
      window.removeEventListener("resize", update);
      window.removeEventListener("scroll", update, true);
    };
  }, [open, anchorRef, floatingRef, align, maxHeight]);

  return style;
}
