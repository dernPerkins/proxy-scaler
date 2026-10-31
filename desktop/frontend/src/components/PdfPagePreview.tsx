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
// follows, for a server too old to send each cell's own (Radius).
const CARD_CORNER_RADIUS_MM = 2.5;

// SVG stroke-dasharray for one dashed guide piece `lengthMm` long, in px.
// Mirrors pdf_layout.py::_guide_dash_mm: a target dash, then stretched or
// shrunk so the piece holds a whole number of dashes and starts and ends
// on one. The target is also floored in screen pixels — at preview scale
// the PDF's sub-millimetre dashes would blur into a solid line, and
// showing that a guide IS dashed is the point here — so on a small panel
// a piece may get fewer, longer dashes than the PDF, but it still starts
// and ends where the PDF's does. SVG restarts the pattern at every
// subpath, as the PDF does at every stroke.
const MIN_DASH_PX = 3;
function dashArrayPx(widthMm: number, lengthMm: number, scale: number): string {
  const target = Math.max(Math.max(0.75, 3 * widthMm) * scale, MIN_DASH_PX);
  const length = lengthMm * scale;
  const count = Math.max(1, Math.round((length + 0.8 * target) / (1.8 * target)));
  const dash = length / (count + 0.8 * (count - 1));
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
    // Each piece runs from the grid out toward the page edge (`from` is
    // its grid end), like the PDF's, so its dashes start at the grid.
    const pieces: { d: string; lengthMm: number }[] = [];
    const v = (x: number, seg: [number, number] | null, from: number) => {
      if (!seg) return;
      const to = from === seg[0] ? seg[1] : seg[0];
      pieces.push({ d: `M${x * scale} ${from * scale}V${to * scale}`, lengthMm: seg[1] - seg[0] });
    };
    const h = (y: number, seg: [number, number] | null, from: number) => {
      if (!seg) return;
      const to = from === seg[0] ? seg[1] : seg[0];
      pieces.push({ d: `M${from * scale} ${y * scale}H${to * scale}`, lengthMm: seg[1] - seg[0] });
    };
    for (const x of xs) {
      const top = clipEdgeSegment(0, gridY0, x - halfMm, x + halfMm, keepOut, true, true);
      v(x, top, top?.[1] ?? 0);
      const bottom = clipEdgeSegment(gridY1, preview.page_h_mm, x - halfMm, x + halfMm, keepOut, true, false);
      v(x, bottom, bottom?.[0] ?? 0);
    }
    for (const y of ys) {
      const left = clipEdgeSegment(0, gridX0, y - halfMm, y + halfMm, keepOut, false, true);
      h(y, left, left?.[1] ?? 0);
      const right = clipEdgeSegment(gridX1, preview.page_w_mm, y - halfMm, y + halfMm, keepOut, false, false);
      h(y, right, right?.[0] ?? 0);
    }
    const stroke = preview.page_guide_color ?? DEFAULT_PAGE_GUIDE_COLOR;
    const px = strokePx(widthMm);
    if (preview.page_guide_line === "dashed") {
      // Pieces differ in length, so each gets its own fitted pattern.
      pieces.forEach((piece, i) =>
        paths.push(
          <path
            key={`page-${i}`}
            d={piece.d}
            stroke={stroke}
            strokeWidth={px}
            strokeDasharray={dashArrayPx(widthMm, piece.lengthMm, scale)}
          />,
        ),
      );
    } else {
      paths.push(<path key="page" d={pieces.map((p) => p.d).join("")} stroke={stroke} strokeWidth={px} />);
    }
  }

  if (!preview.hide_card_guides) {
    const widthMm = ptToMm(preview.guide_width_pt);
    const halfMm = widthMm / 2;
    const xs = nudged(trimXs, halfMm);
    const ys = nudged(trimYs, halfMm);
    const len = preview.guide_length_mm;
    // One path per dash fit: the length one dashed piece is fitted over
    // is a "+" arm, or a whole rounded mark from tip to tip — which
    // depends on the mark's radius, so each radius is its own path.
    const groups = new Map<number, { d: string; pieceMm: number }>();
    if (preview.card_guide_corners === "rounded") {
      // Each cell's corner radius: the server's per-slot answer (Alpha
      // Corners), its base for a cell with no slot, or — from a server
      // that predates both — the modern corner.
      const baseR = preview.corner_radius_mm ?? CARD_CORNER_RADIUS_MM;
      const cellR = (cell: number) => preview.slots[cell]?.corner_radius_mm ?? baseR;
      xs.forEach((x, i) => {
        // +1 when the card lies to the right of / below this edge.
        const intoX = i % 2 === 0 ? 1 : -1;
        ys.forEach((y, j) => {
          const intoY = j % 2 === 0 ? 1 : -1;
          // Trim edge i is column i / 2's, edge j row j / 2's.
          const cardR = cellR(Math.floor(j / 2) * preview.cols + Math.floor(i / 2));
          const r = cardR + halfMm;
          let group = groups.get(cardR);
          if (!group) {
            group = { d: "", pieceMm: 2 * len + (Math.PI / 2) * r };
            groups.set(cardR, group);
          }
          // The corner's own centre, R in from both trim edges; radius
          // R + half so the stroke's inner edge is the card's curve.
          const cx = x + intoX * r;
          const cy = y + intoY * r;
          // One subpath, so one dash pattern, tip to tip: the tail along
          // the top/bottom edge in to the arc, round the corner, then the
          // tail down/up the side edge. Sweep is clockwise for the
          // top-right and bottom-left corners.
          const sweep = intoX * intoY > 0 ? 0 : 1;
          group.d +=
            `M${(cx + intoX * len) * scale} ${y * scale}H${cx * scale}` +
            `A${r * scale} ${r * scale} 0 0 ${sweep} ${x * scale} ${cy * scale}` +
            `V${(cy + intoY * len) * scale}`;
        });
      });
    } else {
      // Four arms out from each corner, so the dashes mirror about it.
      let d = "";
      for (const x of xs) {
        for (const y of ys) {
          const at = `M${x * scale} ${y * scale}`;
          d +=
            `${at}H${(x - len) * scale}${at}H${(x + len) * scale}` +
            `${at}V${(y - len) * scale}${at}V${(y + len) * scale}`;
        }
      }
      groups.set(0, { d, pieceMm: len });
    }
    const px = strokePx(widthMm);
    for (const [key, { d, pieceMm }] of groups) {
      paths.push(
        <path
          key={`card-${key}`}
          d={d}
          stroke={preview.card_guide_color ?? DEFAULT_CARD_GUIDE_COLOR}
          strokeWidth={px}
          strokeDasharray={preview.card_guide_line === "dashed" ? dashArrayPx(widthMm, pieceMm, scale) : undefined}
        />,
      );
    }
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
