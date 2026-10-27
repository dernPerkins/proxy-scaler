// Pieces shared by the image-grid pickers (PrintingPicker's printings,
// MpcArtPicker's MPC Fill art): the thumbnail load queue (what's in view
// loads first), lazy loading rooted on the grid's scroll container, the
// grid's column count for arrow keys, and how long a keyboard selection
// settles before its preview loads.
import { useCallback, useEffect, useRef, useState } from "react";

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
      // Rebuilt when the scroll container itself is a new element: the
      // change-printing modal swaps its whole grid out for the MPC Fill
      // browser and back, and an observer still rooted on the old,
      // detached container never reports anything — every tile stayed
      // blank until the modal was reopened.
      const root = scrollRef.current;
      if (observerRef.current && observerRef.current.root !== root) {
        observerRef.current.disconnect();
        observerRef.current = null;
        for (const watched of callbacks.current.keys()) {
          if (!watched.isConnected) callbacks.current.delete(watched);
        }
      }
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
          { root, rootMargin: "600px 0px" },
        );
        // Tiles still on screen from before the rebuild keep watching.
        for (const watched of callbacks.current.keys()) observerRef.current.observe(watched);
      }
      callbacks.current.set(el, onVisible);
      observerRef.current.observe(el);
      return () => {
        callbacks.current.delete(el);
        // The current observer, which may not be the one that started.
        observerRef.current?.unobserve(el);
      };
    },
    [scrollRef],
  );
}

// The pickers' thumbnails take turns. Loading every tile near the view at
// once put them in the webview's own queue in the order the tiles were
// built: WebKit opens 6 connections to the sidecar, and a new card's
// thumbnails are each a remote fetch there, so the tiles in view waited
// behind the rows above them (and the preview behind them all). Here the
// grid hands out turns instead, a few at a time, in this order:
//
//   1. the selected tile, wherever it is;
//   2. the tiles in view, in reading order;
//   3. the tiles in the margin (about two rows either side), nearest first.
//
// Positions are read when a turn is handed out, so a scroll since (the
// picker's own, to the card's printing, or yours) is already counted.

// One under WebKit's 6 per host: the webview never queues thumbnails in
// an order of its own, and the preview and API calls always have a
// connection. (4 cost an extra round of uncached fetches for a screenful.)
const MAX_LOADING = 5;
const MARGIN_PX = 600;
// A turn whose image never reports back (it was swapped out mid-load) is
// handed back after this, so a stuck slot can't slow the grid for good.
const TURN_TIMEOUT_MS = 15_000;

export interface QueuedLoad {
  /** The image loaded or failed: hand the turn back. */
  settle: () => void;
  /** Gone (unmounted, or images turned off): leave the queue, handing back
   *  a turn already given. */
  cancel: () => void;
  /** This tile became, or stopped being, the selected one. */
  setFirst: (first: boolean) => void;
}

export interface ThumbnailQueue {
  /** Wait for a turn to load `el`'s image; `start` is called when it comes. */
  enqueue: (el: Element, start: () => void, first: boolean) => QueuedLoad;
}

interface Waiting {
  start: () => void;
  first: boolean;
  near: boolean;
  /** Called when the turn is given. */
  granted: () => void;
}

class LoadQueue implements ThumbnailQueue {
  private waiting = new Map<Element, Waiting>();
  private loading = 0;
  private observer: IntersectionObserver | null = null;
  private root: Element | null | undefined = undefined;
  private frame = 0;
  private attached = true;
  // The first turns wait a frame longer, so the picker's scroll to the
  // card's printing on open lands first.
  private settledIn = false;

  constructor(private readonly rootRef: React.RefObject<HTMLElement | null>) {}

  enqueue(el: Element, start: () => void, first: boolean): QueuedLoad {
    this.ensureObserver();
    let state: "waiting" | "loading" | "done" = "waiting";
    let timer = 0;
    const handBack = () => {
      if (state !== "loading") return;
      state = "done";
      window.clearTimeout(timer);
      this.loading -= 1;
      this.schedule();
    };
    const entry: Waiting = {
      start,
      first,
      near: false,
      granted: () => {
        state = "loading";
        timer = window.setTimeout(handBack, TURN_TIMEOUT_MS);
      },
    };
    this.waiting.set(el, entry);
    this.observer?.observe(el);
    this.schedule();
    return {
      settle: handBack,
      cancel: () => {
        if (state === "waiting") {
          state = "done";
          this.waiting.delete(el);
          this.observer?.unobserve(el);
        } else handBack();
      },
      setFirst: (value) => {
        if (state !== "waiting" || entry.first === value) return;
        entry.first = value;
        this.schedule();
      },
    };
  }

  /** On the grid's mount (again, under StrictMode). */
  attach() {
    this.attached = true;
    this.root = undefined;
    this.ensureObserver();
    this.schedule();
  }

  detach() {
    this.attached = false;
    this.observer?.disconnect();
    this.observer = null;
    cancelAnimationFrame(this.frame);
    this.frame = 0;
  }

  // Rebuilt when the scroll container is a new element: the change-printing
  // modal swaps its whole grid out for the MPC Fill browser and back, and
  // an observer rooted on the old, detached container reports nothing.
  private ensureObserver() {
    if (!this.attached) return;
    const root = this.rootRef.current;
    if (this.observer && this.root === root) return;
    this.observer?.disconnect();
    this.root = root;
    this.observer = new IntersectionObserver(
      (entries) => {
        for (const e of entries) {
          const entry = this.waiting.get(e.target);
          if (entry) entry.near = e.isIntersecting;
        }
        this.schedule();
      },
      { root, rootMargin: `${MARGIN_PX}px 0px` },
    );
    for (const [el, entry] of this.waiting) {
      if (!el.isConnected) {
        this.waiting.delete(el);
        continue;
      }
      entry.near = false;
      this.observer.observe(el);
    }
  }

  private schedule() {
    if (this.frame || !this.attached) return;
    const later = () => {
      this.frame = 0;
      this.pump();
    };
    if (this.settledIn) this.frame = requestAnimationFrame(later);
    else {
      this.settledIn = true;
      this.frame = requestAnimationFrame(() => {
        this.frame = requestAnimationFrame(later);
      });
    }
  }

  private pump() {
    while (this.loading < MAX_LOADING) {
      const next = this.best();
      if (!next) return;
      const [el, entry] = next;
      this.waiting.delete(el);
      this.observer?.unobserve(el);
      this.loading += 1;
      entry.granted();
      entry.start();
    }
  }

  /** The waiting tile to load next (see the order above), or null. */
  private best(): [Element, Waiting] | null {
    let pick: [Element, Waiting] | null = null;
    let pickRank: [number, number, number] | null = null;
    const view = (this.root ?? document.documentElement).getBoundingClientRect();
    for (const item of this.waiting) {
      const [el, entry] = item;
      if (!entry.first && !entry.near) continue;
      const r = el.getBoundingClientRect();
      // Outside the visible rows: how far, in pixels; 0 inside them.
      const away = r.bottom <= view.top ? view.top - r.bottom : r.top >= view.bottom ? r.top - view.bottom : 0;
      if (!entry.first && away > MARGIN_PX) continue;
      const rank: [number, number, number] = entry.first
        ? [0, 0, 0]
        : away === 0
          ? [1, r.top, r.left]
          : [2, away, r.left];
      if (
        !pickRank ||
        rank[0] < pickRank[0] ||
        (rank[0] === pickRank[0] &&
          (rank[1] < pickRank[1] || (rank[1] === pickRank[1] && rank[2] < pickRank[2])))
      ) {
        pick = item;
        pickRank = rank;
      }
    }
    return pick;
  }
}

/** One load queue per grid, rooted on its scroll container. */
export function useThumbnailQueue(scrollRef: React.RefObject<HTMLElement | null>): ThumbnailQueue {
  const [queue] = useState(() => new LoadQueue(scrollRef));
  useEffect(() => {
    queue.attach();
    return () => queue.detach();
  }, [queue]);
  return queue;
}

/** A tile's turn in the queue: `load` turns true when its image may load,
 *  and the image calls `settle` once it has loaded or failed. `enabled`
 *  false (images off, or no origin yet) keeps the tile out of the queue:
 *  a turn given to an image that never renders would never come back. */
export function useQueuedThumb(
  queue: ThumbnailQueue,
  frameRef: React.RefObject<Element | null>,
  enabled: boolean,
  first: boolean,
): { load: boolean; settle: () => void } {
  const [load, setLoad] = useState(false);
  const handle = useRef<QueuedLoad | null>(null);
  const firstRef = useRef(first);
  firstRef.current = first;
  const loaded = useRef(false);
  // In an effect, not the ref callback: effects run after the whole tree
  // has committed, so the scroll container the queue is rooted on exists.
  useEffect(() => {
    if (!enabled || loaded.current || !frameRef.current) return;
    const h = queue.enqueue(
      frameRef.current,
      () => {
        loaded.current = true;
        setLoad(true);
      },
      firstRef.current,
    );
    handle.current = h;
    return () => {
      h.cancel();
      handle.current = null;
    };
  }, [queue, enabled, frameRef]);
  useEffect(() => handle.current?.setFirst(first), [first]);
  const settle = useCallback(() => handle.current?.settle(), []);
  return { load, settle };
}
