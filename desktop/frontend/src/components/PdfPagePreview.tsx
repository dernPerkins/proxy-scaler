import type { ReactNode } from "react";
import type { PdfPagePreview as PdfPagePreviewData, RectMm } from "../api/types";

const PANEL_WIDTH_PX = 360;
// A landscape page at the portrait width is a short, squat strip — the
// cards come out ~60% the size they get on a portrait sheet. Widening
// the panel 1.5× brings them back to roughly the same on-screen size.
// Same rule as pdf_layout.page_orientation: a square page is portrait.
const LANDSCAPE_PANEL_WIDTH_PX = 540;

function panelWidthPx(preview: PdfPagePreviewData): number {
  return preview.page_w_mm > preview.page_h_mm ? LANDSCAPE_PANEL_WIDTH_PX : PANEL_WIDTH_PX;
}
const MM_PER_IN = 25.4;

// The guides' look before it was configurable — pdf_layout.py's
// _OUTER_LINE_COLOR / _MARK_COLOR, and what a server older than guide
// styling still prints, so the preview falls back to them when the
// response doesn't carry the styling fields.
const DEFAULT_PAGE_GUIDE_COLOR = "#000000";
const DEFAULT_CARD_GUIDE_COLOR = "#00AA50";
// pdf_layout.py's CARD_CORNER_RADIUS_MM — the curve a rounded Card Guide
// follows.
const CARD_CORNER_RADIUS_MM = 3.175;

// SVG stroke-dasharray for a dashed guide, in px. Mirrors
// pdf_layout.py::_guide_dash_mm, but floored in screen pixels too: at
// preview scale the PDF's sub-millimetre dashes would blur into a solid
// line, and showing that a guide IS dashed is the point here.
function dashArrayPx(widthMm: number, strokePx: number, scale: number): string {
  const dashMm = Math.max(0.75, 3 * widthMm);
  const dash = Math.max(dashMm * scale, 3 * strokePx, 3);
  return `${dash} ${dash * 0.8}`;
}

// Each card's own pair of trim-edge coordinates along one axis — mirrors
// pdf_layout.py::_card_trim_edges exactly (see its own comment for why
// this is 2*count coordinates, not count+1: adjacent cards' bleed strips
// end at two distinct nearby points, not one shared line). The trailing
// edge comes from the bled card's own extent, not the cell stride, which
// also includes the spacing gap and put every right/bottom guide one gap
// outside its card once spacing was set.
function cardTrimEdges(
  count: number,
  cellMm: number,
  bleedMm: number,
  originMm: number,
  cardExtentMm: number,
): number[] {
  const edges: number[] = [];
  for (let i = 0; i < count; i++) {
    edges.push(originMm + i * cellMm + bleedMm);
    edges.push(originMm + i * cellMm + cardExtentMm - bleedMm);
  }
  return edges;
}

// Mirrors pdf_layout.py::_clip_edge_segment: a page guide runs between a
// page edge and the grid; any registration-mark keep-out zone its stroke
// crosses cuts it back to the zone's far side, so no stub of black line
// survives between the page edge and the mark. Returns null when nothing
// is left. `fromEdge` says the segment starts at the top/left edge rather
// than ending at the bottom/right one.
function clipEdgeSegment(
  start: number,
  end: number,
  crossLo: number,
  crossHi: number,
  keepOut: RectMm[],
  vertical: boolean,
  fromEdge: boolean,
): [number, number] | null {
  for (const zone of keepOut) {
    const cross0 = vertical ? zone.x_mm : zone.y_mm;
    const cross1 = vertical ? zone.x_mm + zone.w_mm : zone.y_mm + zone.h_mm;
    const along0 = vertical ? zone.y_mm : zone.x_mm;
    const along1 = vertical ? zone.y_mm + zone.h_mm : zone.x_mm + zone.w_mm;
    if (cross1 <= crossLo || cross0 >= crossHi) continue;
    if (along1 <= start || along0 >= end) continue;
    if (fromEdge) start = Math.max(start, along1);
    else end = Math.min(end, along0);
  }
  return start < end ? [start, end] : null;
}

// Mirrors pdf_layout.py::_draw_cut_marks: Page Guides from the page edge
// to the card grid block, plus Card Guides at every card's own trim corner
// (the full xs x ys cross product) — a "+", or a quarter arc hugging the
// card's rounded corner with a tail along each edge. Like the PDF, every
// guide is nudged outward from its card by half its own (mm) stroke width,
// so the ink's inner edge sits on the trim line instead of straddling it —
// even indices in cardTrimEdges are leading (left/top) edges, odd are
// trailing (right/bottom).
//
// Drawn as one SVG over the page: dashes and arcs come for free, and the
// square line caps match the PDF's.
function CutMarks({ preview, scale }: { preview: PdfPagePreviewData; scale: number }) {
  const trimXs = cardTrimEdges(
    preview.cols, preview.cell_w_mm, preview.bleed_mm, preview.margin_x_mm, preview.bled_card_w_mm,
  );
  const trimYs = cardTrimEdges(
    preview.rows, preview.cell_h_mm, preview.bleed_mm, preview.margin_y_mm, preview.bled_card_h_mm,
  );
  const nudged = (edges: number[], halfMm: number) =>
    edges.map((e, i) => (i % 2 === 0 ? e - halfMm : e + halfMm));
  // Real print widths (a fraction of a point) are effectively hairlines
  // even before scaling down to a ~360px on-screen panel — sub-pixel
  // widths get anti-aliased into near-invisibility by the browser, unlike
  // a physical print device. Clamped to a visible minimum here (the
  // outward nudge still uses the true mm width); the actual PDF output is
  // unaffected, this only touches the on-screen approximation.
  const strokePx = (widthMm: number) => Math.max(1, widthMm * scale);
  const ptToMm = (pt: number) => (pt / 72) * MM_PER_IN;

  const paths: ReactNode[] = [];

  // The two guide kinds are gated independently, mirroring
  // pdf_layout.py::_draw_cut_marks. Which page kind's flags arrive here is
  // already resolved server-side, so a Back Page preview draws exactly
  // what that page will carry.
  if (!preview.hide_page_guides) {
    const widthMm = ptToMm(preview.page_guide_width_pt ?? preview.guide_width_pt);
    const halfMm = widthMm / 2;
    const xs = nudged(trimXs, halfMm);
    const ys = nudged(trimYs, halfMm);
    const gridX0 = xs[0];
    const gridX1 = xs[xs.length - 1];
    const gridY0 = ys[0];
    const gridY1 = ys[ys.length - 1];
    // Registration-mark keep-out zones clip the page guides exactly as the
    // PDF does; empty (no clipping) without a cutter or on an older server.
    const keepOut = preview.registration_keep_out ?? [];
    let d = "";
    const v = (x: number, seg: [number, number] | null) => {
      if (seg) d += `M${x * scale} ${seg[0] * scale}V${seg[1] * scale}`;
    };
    const h = (y: number, seg: [number, number] | null) => {
      if (seg) d += `M${seg[0] * scale} ${y * scale}H${seg[1] * scale}`;
    };
    for (const x of xs) {
      v(x, clipEdgeSegment(0, gridY0, x - halfMm, x + halfMm, keepOut, true, true));
      v(x, clipEdgeSegment(gridY1, preview.page_h_mm, x - halfMm, x + halfMm, keepOut, true, false));
    }
    for (const y of ys) {
      h(y, clipEdgeSegment(0, gridX0, y - halfMm, y + halfMm, keepOut, false, true));
      h(y, clipEdgeSegment(gridX1, preview.page_w_mm, y - halfMm, y + halfMm, keepOut, false, false));
    }
    const px = strokePx(widthMm);
    paths.push(
      <path
        key="page"
        d={d}
        stroke={preview.page_guide_color ?? DEFAULT_PAGE_GUIDE_COLOR}
        strokeWidth={px}
        strokeDasharray={preview.page_guide_line === "dashed" ? dashArrayPx(widthMm, px, scale) : undefined}
      />,
    );
  }

  if (!preview.hide_card_guides) {
    const widthMm = ptToMm(preview.guide_width_pt);
    const halfMm = widthMm / 2;
    const xs = nudged(trimXs, halfMm);
    const ys = nudged(trimYs, halfMm);
    const len = preview.guide_length_mm;
    let d = "";
    if (preview.card_guide_corners === "rounded") {
      const r = CARD_CORNER_RADIUS_MM + halfMm;
      xs.forEach((x, i) => {
        // +1 when the card lies to the right of / below this edge.
        const intoX = i % 2 === 0 ? 1 : -1;
        ys.forEach((y, j) => {
          const intoY = j % 2 === 0 ? 1 : -1;
          // The corner's own centre, R in from both trim edges; radius
          // R + half so the stroke's inner edge is the card's curve.
          const cx = x + intoX * r;
          const cy = y + intoY * r;
          // Tail along the top/bottom edge in to the arc, round the
          // corner, then the tail down/up the side edge. Sweep is
          // clockwise for the top-right and bottom-left corners.
          const sweep = intoX * intoY > 0 ? 0 : 1;
          d +=
            `M${(cx + intoX * len) * scale} ${y * scale}H${cx * scale}` +
            `A${r * scale} ${r * scale} 0 0 ${sweep} ${x * scale} ${cy * scale}` +
            `V${(cy + intoY * len) * scale}`;
        });
      });
    } else {
      for (const x of xs) {
        for (const y of ys) {
          d +=
            `M${(x - len) * scale} ${y * scale}H${(x + len) * scale}` +
            `M${x * scale} ${(y - len) * scale}V${(y + len) * scale}`;
        }
      }
    }
    const px = strokePx(widthMm);
    paths.push(
      <path
        key="card"
        d={d}
        stroke={preview.card_guide_color ?? DEFAULT_CARD_GUIDE_COLOR}
        strokeWidth={px}
        strokeDasharray={preview.card_guide_line === "dashed" ? dashArrayPx(widthMm, px, scale) : undefined}
      />,
    );
  }

  return (
    <svg
      width={preview.page_w_mm * scale}
      height={preview.page_h_mm * scale}
      fill="none"
      strokeLinecap="square"
      style={{ position: "absolute", left: 0, top: 0, overflow: "visible", pointerEvents: "none" }}
      aria-hidden="true"
    >
      {paths}
    </svg>
  );
}

// Cutter registration marks, from the server's page-coordinate rects
// (pdf_layout.py::registration_mark_rects) — the preview never re-derives
// the geometry. Keep-out zones draw hatched so a card sitting under one is
// visibly the problem the conflict warning describes; they go UNDER the
// guides and cards, the black marks go on top of everything.
const KEEP_OUT_FILL =
  "repeating-linear-gradient(45deg, rgba(220, 0, 0, 0.12) 0 3px, transparent 3px 7px)";

function RegistrationMarks({
  rects,
  scale,
}: {
  rects: RectMm[];
  scale: number;
}) {
  return (
    <>
      {rects.map((r, i) => (
        <div
          key={i}
          style={{
            position: "absolute",
            background: "#000",
            left: r.x_mm * scale,
            top: r.y_mm * scale,
            // A 1mm bar is ~1.7px at A4 width — visible, but clamped for
            // the same reason strokeWidthPx is: a wider page could round
            // it away entirely.
            width: Math.max(1, r.w_mm * scale),
            height: Math.max(1, r.h_mm * scale),
          }}
        />
      ))}
    </>
  );
}

function KeepOutZones({ rects, scale }: { rects: RectMm[]; scale: number }) {
  return (
    <>
      {rects.map((r, i) => (
        <div
          key={i}
          style={{
            position: "absolute",
            left: r.x_mm * scale,
            top: r.y_mm * scale,
            width: r.w_mm * scale,
            height: r.h_mm * scale,
            background: KEEP_OUT_FILL,
            border: "1px dashed rgba(220, 0, 0, 0.45)",
            boxSizing: "border-box",
          }}
        />
      ))}
    </>
  );
}

// Real DOM/CSS, deliberately no iframe/dangerouslySetInnerHTML — matches
// CompareDialog.tsx's own documented precedent against iframe indirection
// in this WKWebView app. Placement math mirrors pdf_layout.py::build_pdf
// exactly (col, row = idx % cols, idx // cols) so the preview lines up
// with the real PDF output at the same layout settings.
export default function PdfPagePreview({ preview }: { preview: PdfPagePreviewData }) {
  const panelWidth = panelWidthPx(preview);
  const scale = panelWidth / preview.page_w_mm;
  const pageHeightPx = preview.page_h_mm * scale;
  const marks = preview.registration_marks ?? [];
  const keepOut = preview.registration_keep_out ?? [];

  return (
    <div
      className="pdf-page-preview"
      style={{
        position: "relative",
        width: panelWidth,
        height: pageHeightPx,
        background: "#fff",
        border: "1px solid var(--border)",
        borderRadius: "var(--radius)",
        overflow: "hidden",
        flexShrink: 0,
      }}
    >
      {keepOut.length > 0 && <KeepOutZones rects={keepOut} scale={scale} />}
      {preview.slots.map((slot, idx) => {
        const col = idx % preview.cols;
        const row = Math.floor(idx / preview.cols);
        const x = preview.margin_x_mm + col * preview.cell_w_mm;
        const y = preview.margin_y_mm + row * preview.cell_h_mm;
        // A Back Page preview returns every grid position, empty ones
        // included, because its cells are placed by mirrored index — an
        // empty position has to stay empty rather than collapsing and
        // shifting every card after it.
        const empty = !slot.thumbnail_data_url && !slot.card_name;
        if (empty) return null;
        return (
          <div
            key={idx}
            title={slot.card_name}
            style={{
              position: "absolute",
              left: x * scale,
              top: y * scale,
              width: preview.bled_card_w_mm * scale,
              height: preview.bled_card_h_mm * scale,
              background: "var(--surface-2)",
              overflow: "hidden",
            }}
          >
            {slot.thumbnail_data_url && (
              <img
                src={slot.thumbnail_data_url}
                alt={slot.card_name}
                style={{
                  width: "100%",
                  height: "100%",
                  objectFit: "cover",
                  display: "block",
                  // Matches what build_pdf does to this page's images. A
                  // preview that skipped it would show the one thing the
                  // preview exists to let you check — the flip edge —
                  // looking correct when it isn't.
                  transform: preview.rotated ? "rotate(180deg)" : undefined,
                }}
              />
            )}
          </div>
        );
      })}
      {(!preview.hide_card_guides || !preview.hide_page_guides) && (
        <CutMarks preview={preview} scale={scale} />
      )}
      {marks.length > 0 && <RegistrationMarks rects={marks} scale={scale} />}
    </div>
  );
}
