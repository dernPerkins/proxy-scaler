// PROTOTYPE — throwaway. The page geometry the two page-layout prototypes draw:
// the Pages view (decklist-pages.prototype.tsx) and variant E of the Grid
// prototypes (decklist-grid.prototype.tsx). Kept in one file so a preview in
// either view says the same thing about the same settings.
//
// It mirrors proxy_scaler/pdf_layout.py::resolve_page_layout: a printed card is
// the trim box plus bleed on every side, a cell is a printed card plus the gap
// after it, the grid is cols × cell one gap narrower than that, and the grid is
// centered on the page before the X/Y offsets nudge it. Same arithmetic so a
// preview and the PDF cannot drift. Delete with the prototypes.
import type { CSSProperties } from "react";
import type { CardRow, ProjectSettings } from "../api/project";
import { CARD_H_MM, CARD_W_MM } from "../cardTrim";

export interface PagesLayout {
  pageW: number;
  pageH: number;
  cols: number;
  rows: number;
  bleed: number;
  /** One printed card, bleed included — what actually leaves the printer. */
  bledW: number;
  bledH: number;
  /** A bled card plus the gap after it. */
  cellW: number;
  cellH: number;
  marginX: number;
  marginY: number;
  capacity: number;
}

export function resolvePageLayout(s: ProjectSettings): PagesLayout {
  const bleed = s.bleed_mm;
  const bledW = CARD_W_MM + 2 * bleed;
  const bledH = CARD_H_MM + 2 * bleed;
  const cellW = bledW + s.spacing_x_mm;
  const cellH = bledH + s.spacing_y_mm;
  // The last column has no gap after it, so the grid is one gap narrower
  // than cols × cellW.
  const gridW = s.cols * cellW - s.spacing_x_mm;
  const gridH = s.rows * cellH - s.spacing_y_mm;
  return {
    pageW: s.page_width_mm,
    pageH: s.page_height_mm,
    cols: s.cols,
    rows: s.rows,
    bleed,
    bledW,
    bledH,
    cellW,
    cellH,
    marginX: (s.page_width_mm - gridW) / 2 + s.offset_x_mm,
    marginY: (s.page_height_mm - gridH) / 2 + s.offset_y_mm,
    capacity: s.cols * s.rows,
  };
}

/** One entry per printed card: a 4-of takes four slots, and those four sit
 *  side by side like any other cards, which means they can straddle a page
 *  break. The print pipeline flattens quantities the same way. */
export function paginateDeck(
  cards: readonly CardRow[],
  capacity: number,
): { card: CardRow; copy: number }[][] {
  const slots: { card: CardRow; copy: number }[] = [];
  for (const card of cards) {
    const n = Math.max(1, Math.round(card.quantity ?? 1));
    for (let copy = 0; copy < n; copy++) slots.push({ card, copy });
  }
  const pages: { card: CardRow; copy: number }[][] = [];
  for (let i = 0; i < slots.length; i += capacity) pages.push(slots.slice(i, i + capacity));
  return pages;
}

/** Where slot i of a page lands, as percentages of a page box drawn at the
 *  page's own aspect ratio — so the preview says the mm it means. */
export function slotStyleFor(layout: PagesLayout, i: number): CSSProperties {
  const pct = (v: number, total: number) => `${((v / total) * 100).toFixed(4)}%`;
  return {
    left: pct(layout.marginX + (i % layout.cols) * layout.cellW, layout.pageW),
    top: pct(layout.marginY + Math.floor(i / layout.cols) * layout.cellH, layout.pageH),
    width: pct(layout.bledW, layout.pageW),
    height: pct(layout.bledH, layout.pageH),
  };
}

/** The trim edge as an inset of its own slot: the bleed, on each side. Null when
 *  there is no bleed, because then the printed card IS the trim box. */
export function trimInsetFor(layout: PagesLayout): string | null {
  return layout.bleed > 0
    ? `${((layout.bleed / layout.bledH) * 100).toFixed(3)}% ${((layout.bleed / layout.bledW) * 100).toFixed(3)}%`
    : null;
}

/** The page's aspect ratio, handed to CSS as the custom property the sheet's
 *  width cap and its box are both built from. */
export function pageArStyle(layout: PagesLayout): CSSProperties {
  return { "--page-ar": `${layout.pageW} / ${layout.pageH}` } as CSSProperties;
}
