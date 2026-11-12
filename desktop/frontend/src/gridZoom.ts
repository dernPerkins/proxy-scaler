// The Decklist Grid view's card size (ADR 0010). 100% is the card's real
// 63 × 88 mm on the screen the window is on, so a card held against the
// screen matches its tile. Chosen from a prototype
// (.scratch/deck-grid-zoom-prototype/, variant D).
//
// Two things make up a tile's width in CSS px:
//
//   · the zoom: one of CARD_SIZES once settled (the slider snaps), saved as
//     an app preference like the Row/Grid choice;
//   · px per real mm on the current display: from the display's own size
//     (display_size.rs), or a hand calibration saved for that display, or —
//     where neither exists (Windows/Linux, a browser tab) — the 96 dpi CSS
//     assumes, which a calibration can then correct.
import { useCallback, useEffect, useState } from "react";
import { useMutation, useQuery, useQueryClient } from "@tanstack/react-query";
import { projectApi, type DisplaySize } from "./api/project";
import { isTauri } from "./tauri";

export const CARD_W_MM = 63;
export const CARD_H_MM = 88;
/** What CSS's own `mm` assumes (96 dpi). */
const CSS_PX_PER_MM = 96 / 25.4;
/** The calibration key used where the platform reports no displays. Its
 *  value is px per mm itself; a real display's is its width in mm. */
const UNKNOWN_DISPLAY = "unknown";

/** Which parts of a tile show at a size. "art" and "compact" are both just
 *  the card (the foot's controls stay in the right-click menu), on tight and
 *  medium padding; "full" adds the foot under it. */
export type TileLayout = "art" | "compact" | "full";

/** The sizes the zoom settles on, smallest first. Each fixes the tile's
 *  layout; between two (mid-drag) the smaller one's layout holds. */
export const CARD_SIZES: readonly { zoom: number; label: string; layout: TileLayout }[] = [
  { zoom: 0.35, label: "XS", layout: "art" },
  { zoom: 0.5, label: "S", layout: "compact" },
  { zoom: 0.65, label: "M", layout: "compact" },
  { zoom: 0.8, label: "L", layout: "full" },
  { zoom: 1, label: "Actual", layout: "full" },
  { zoom: 1.25, label: "XL", layout: "full" },
  { zoom: 1.5, label: "XXL", layout: "full" },
];
export const MIN_ZOOM = CARD_SIZES[0].zoom;
export const MAX_ZOOM = CARD_SIZES[CARD_SIZES.length - 1].zoom;
const EPS = 0.004;

export const clampZoom = (z: number) => Math.min(MAX_ZOOM, Math.max(MIN_ZOOM, z));

/** The size z is sitting on, if any. */
export function sizeAt(z: number) {
  return CARD_SIZES.find((s) => Math.abs(s.zoom - z) < EPS);
}

/** The size at or below z: whose layout is in force. */
export function sizeFloor(z: number) {
  return [...CARD_SIZES].reverse().find((s) => s.zoom <= z + EPS) ?? CARD_SIZES[0];
}

/** Where a released drag or an ended pinch settles: the size it is already
 *  on, otherwise the next one in the direction it was moving (the nearest
 *  when it has no direction). */
export function snapToward(z: number, dir: number): number {
  const on = sizeAt(z);
  if (on) return on.zoom;
  if (dir > 0) return (CARD_SIZES.find((s) => s.zoom > z) ?? CARD_SIZES[CARD_SIZES.length - 1]).zoom;
  if (dir < 0) return ([...CARD_SIZES].reverse().find((s) => s.zoom < z) ?? CARD_SIZES[0]).zoom;
  return CARD_SIZES.reduce((a, b) => (Math.abs(b.zoom - z) < Math.abs(a.zoom - z) ? b : a)).zoom;
}

/** One size on from z (the − / + buttons, keys): moves unless at an end. */
export function stepFrom(z: number, dir: 1 | -1): number {
  if (dir > 0) return (CARD_SIZES.find((s) => s.zoom > z + EPS) ?? CARD_SIZES[CARD_SIZES.length - 1]).zoom;
  return ([...CARD_SIZES].reverse().find((s) => s.zoom < z - EPS) ?? CARD_SIZES[0]).zoom;
}

export const zoomLabel = (z: number) => `${Math.round(z * 100)}%`;

// --- the saved zoom ------------------------------------------------------

const zoomQuery = {
  queryKey: ["grid-card-zoom"],
  queryFn: () => (isTauri() ? projectApi.getGridCardZoom().catch(() => 1) : 1),
  staleTime: Infinity,
};

/** The zoom on screen: the saved one, or a live value while a drag, pinch
 *  or glide is in progress. Only `save` writes the store, so a drag costs
 *  one write, not one per frame. */
function useZoom() {
  const queryClient = useQueryClient();
  const saved = useQuery(zoomQuery).data ?? 1;
  const [live, setLive] = useState<number | null>(null);
  const { mutate } = useMutation({
    mutationFn: (zoom: number) => (isTauri() ? projectApi.setGridCardZoom(zoom) : Promise.resolve()),
    onError: () => queryClient.invalidateQueries({ queryKey: zoomQuery.queryKey }),
  });
  const save = useCallback(
    (zoom: number) => {
      queryClient.setQueryData(zoomQuery.queryKey, zoom);
      setLive(null);
      mutate(zoom);
    },
    [queryClient, mutate],
  );
  return { zoom: live ?? saved, setLive, save };
}

// --- px per real mm on this display --------------------------------------

const calibrationsQuery = {
  queryKey: ["display-calibrations"],
  queryFn: () =>
    isTauri()
      ? projectApi.getDisplayCalibrations().catch(() => ({}) as Record<string, number>)
      : ({} as Record<string, number>),
  staleTime: Infinity,
};

/** The display holding the window's centre, or the main one (origin 0,0). */
function displayUnderWindow(displays: DisplaySize[]): DisplaySize | null {
  if (!displays.length) return null;
  const cx = window.screenX + window.outerWidth / 2;
  const cy = window.screenY + window.outerHeight / 2;
  return (
    displays.find((d) => cx >= d.x && cx < d.x + d.widthPt && cy >= d.y && cy < d.y + d.heightPt) ??
    displays.find((d) => d.x === 0 && d.y === 0) ??
    displays[0]
  );
}

/** Px per real mm on the display the window is on, kept current as the
 *  window moves between displays (Tauri's move/scale events) and re-read
 *  on focus (a display plugged in or its resolution changed meanwhile). */
function useCardScale(enabled: boolean) {
  const queryClient = useQueryClient();
  const calibrations = useQuery(calibrationsQuery).data ?? {};
  const [displays, setDisplays] = useState<DisplaySize[]>([]);
  const [display, setDisplay] = useState<DisplaySize | null>(null);

  useEffect(() => {
    if (!enabled || !isTauri()) return;
    let alive = true;
    const load = () =>
      projectApi
        .getDisplaySizes()
        .then((d) => alive && setDisplays(d))
        .catch(() => alive && setDisplays([]));
    load();
    window.addEventListener("focus", load);
    return () => {
      alive = false;
      window.removeEventListener("focus", load);
    };
  }, [enabled]);

  useEffect(() => {
    if (!enabled) return;
    const pick = () =>
      setDisplay((cur) => {
        const next = displayUnderWindow(displays);
        return cur && next && cur.key === next.key && cur.widthPt === next.widthPt ? cur : next;
      });
    pick();
    const tauri = window.__TAURI__;
    if (!tauri) return;
    const unlisten = ["tauri://move", "tauri://scale-change", "tauri://resize"].map((ev) =>
      tauri.event.listen(ev, pick),
    );
    return () => unlisten.forEach((p) => p.then((off) => off()));
  }, [enabled, displays]);

  const detected = display && display.widthMm > 0 ? display.widthPt / display.widthMm : null;
  const key = display?.key ?? UNKNOWN_DISPLAY;
  const saved = calibrations[key];
  const calibrated = saved == null ? null : display ? display.widthPt / saved : saved;
  const pxPerMm = calibrated ?? detected ?? CSS_PX_PER_MM;

  const write = useMutation({
    mutationFn: ({ value }: { value: number | null }) =>
      isTauri() ? projectApi.setDisplayCalibration(key, value) : Promise.resolve(),
    onMutate: ({ value }) =>
      queryClient.setQueryData<Record<string, number>>(calibrationsQuery.queryKey, (m) => {
        const next = { ...(m ?? {}) };
        if (value == null) delete next[key];
        else next[key] = value;
        return next;
      }),
    onError: () => queryClient.invalidateQueries({ queryKey: calibrationsQuery.queryKey }),
  });

  return {
    pxPerMm,
    /** From the display itself; null where it reports no size. */
    detected,
    isCalibrated: calibrated != null,
    /** Where pxPerMm comes from right now. */
    source: (calibrated != null ? "calibrated" : detected != null ? "detected" : "estimated") as
      "calibrated" | "detected" | "estimated",
    /** The display's own reported size in mm, when it reports one. */
    displayMm: display && display.widthMm > 0 ? { w: display.widthMm, h: display.heightMm } : null,
    /** Save a hand-matched px per mm for this display. */
    calibrate: (px: number) => write.mutate({ value: display ? display.widthPt / px : px }),
    /** Back to the detected size (or the CSS guess). */
    resetCalibration: () => write.mutate({ value: null }),
  };
}

// --- together ------------------------------------------------------------

/** Everything the Grid view and its zoom dock need. Pass `enabled` false
 *  outside the Grid view so the display tracking sits idle. */
export function useGridCardSize(enabled: boolean) {
  const { zoom, setLive, save } = useZoom();
  const scale = useCardScale(enabled);
  return {
    zoom,
    setLive,
    save,
    scale,
    /** The card art's width in CSS px. */
    tileWidth: Math.round(CARD_W_MM * scale.pxPerMm * zoom),
    layout: sizeFloor(zoom).layout,
  };
}

export type GridCardSize = ReturnType<typeof useGridCardSize>;
