// Pieces shared by the image-grid pickers (PrintingPicker's printings,
// MpcArtPicker's MPC Fill art): lazy thumbnail loading rooted on the
// grid's scroll container, the grid's column count for arrow keys, and
// how long a keyboard selection settles before its preview loads.
import { useCallback, useEffect, useRef } from "react";

// How long a keyboard selection must rest before the preview asks for the
// full-size image: arrowing across a row shouldn't download every card on
// the way.
export const KEYBOARD_PREVIEW_SETTLE_MS = 250;

// How many tiles one row of the grid holds, read off the laid-out CSS grid
// (auto-fill columns change with the window width).
export function gridColumns(grid: HTMLElement | null): number {
  if (!grid) return 1;
  const columns = getComputedStyle(grid).gridTemplateColumns.split(" ").filter(Boolean);
  return Math.max(1, columns.length);
}

// Thumbnails load as their tiles near the grid's visible area. Not
// `loading="lazy"`: WebKit (the macOS app's webview) loaded every tile of
// the grid on open, ignoring the scroll container. One IntersectionObserver
// rooted on that container serves every tile; a tile reports visible once
// and is then forgotten.
export type ObserveTile = (el: Element, onVisible: () => void) => () => void;

export function useTileVisibility(scrollRef: React.RefObject<HTMLElement | null>): ObserveTile {
  const observerRef = useRef<IntersectionObserver | null>(null);
  const callbacks = useRef(new Map<Element, () => void>());
  useEffect(
    () => () => {
      // Dropped, not just disconnected, so a remount (StrictMode does one
      // in development) builds a fresh observer on the current container.
      observerRef.current?.disconnect();
      observerRef.current = null;
      callbacks.current.clear();
    },
    [],
  );
  return useCallback(
    (el, onVisible) => {
      if (!observerRef.current) {
        observerRef.current = new IntersectionObserver(
          (entries, observer) => {
            for (const entry of entries) {
              if (!entry.isIntersecting) continue;
              callbacks.current.get(entry.target)?.();
              callbacks.current.delete(entry.target);
              observer.unobserve(entry.target);
            }
          },
          // About two rows ahead, so scrolling rarely meets an empty frame.
          { root: scrollRef.current, rootMargin: "600px 0px" },
        );
      }
      const observer = observerRef.current;
      callbacks.current.set(el, onVisible);
      observer.observe(el);
      return () => {
        callbacks.current.delete(el);
        observer.unobserve(el);
      };
    },
    [scrollRef],
  );
}
