import type { ReactNode } from "react";
import type { PdfPagePreview as PdfPagePreviewData, RectMm } from "../api/types";

const PANEL_WIDTH_PX = 360;
const MM_PER_IN = 25.4;

// Matches pdf_layout.py's own hardcoded _OUTER_LINE_COLOR / _MARK_COLOR —
// not user-configurable there either, so not exposed via the API; just
// mirrored as constants here. Both line kinds share the user-configurable
// guide_width_pt stroke width.
const OUTER_LINE_COLOR = "#000";
const MARK_COLOR = "rgb(0, 170, 80)";

// Line segment as a thin absolutely-positioned div — always drawn as a
// px-sized rect (never relying on CSS border, since a sub-pixel border
// width can get rounded away entirely by the browser at this scale).
function Line({
  x1,
  y1,
  x2,
  y2,
  widthPx,
  color,
}: {
  x1: number;
  y1: number;
  x2: number;
  y2: number;
  widthPx: number;
  color: string;
}) {
  const isVertical = x1 === x2;
  return (
    <div
      style={{
        position: "absolute",
        background: color,
        left: isVertical ? x1 - widthPx / 2 : Math.min(x1, x2),
        top: isVertical ? Math.min(y1, y2) : y1 - widthPx / 2,
        width: isVertical ? widthPx : Math.abs(x2 - x1),
        height: isVertical ? Math.abs(y2 - y1) : widthPx,
      }}
    />
  );
}

// Each card's own pair of trim-edge coordinates along one axis — mirrors
// pdf_layout.py::_card_trim_edges exactly (see its own comment for why
// this is 2*count coordinates, not count+1: adjacent cards' bleed strips
// end at two distinct nearby points, not one shared line).
function cardTrimEdges(count: number, cellMm: number, bleedMm: number, originMm: number): number[] {
  const edges: number[] = [];
  for (let i = 0; i < count; i++) {
    edges.push(originMm + i * cellMm + bleedMm);
    edges.push(originMm + (i + 1) * cellMm - bleedMm);
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

// Mirrors pdf_layout.py::_draw_cut_marks: outer lines from the page edge
// to the card grid block, plus a small "+" crop mark at every card's own
// trim corner (the full xs x ys cross product). Like the PDF, every guide
// is nudged outward from its card by half the true (mm) stroke width, so
// the ink's inner edge sits on the trim line instead of straddling it —
// even indices in cardTrimEdges are leading (left/top) edges, odd are
// trailing (right/bottom).
function CutMarks({ preview, scale }: { preview: PdfPagePreviewData; scale: number }) {
  const guideWidthMm = (preview.guide_width_pt / 72) * MM_PER_IN;
  const halfMm = guideWidthMm / 2;
  const xs = cardTrimEdges(preview.cols, preview.cell_w_mm, preview.bleed_mm, preview.margin_x_mm)
    .map((x, i) => (i % 2 === 0 ? x - halfMm : x + halfMm));
  const ys = cardTrimEdges(preview.rows, preview.cell_h_mm, preview.bleed_mm, preview.margin_y_mm)
    .map((y, i) => (i % 2 === 0 ? y - halfMm : y + halfMm));
  const gridX0 = xs[0];
  const gridX1 = xs[xs.length - 1];
  const gridY0 = ys[0];
  const gridY1 = ys[ys.length - 1];
  // Real print widths (a fraction of a point) are effectively hairlines
  // even before scaling down to a ~360px on-screen panel — sub-pixel CSS
  // widths get anti-aliased into near-invisibility (or rounded to 0) by
  // the browser, unlike a physical print device. Clamped to a visible
  // minimum here (the outward nudge above still uses the true mm width);
  // the actual PDF output is unaffected, this only touches the on-screen
  // approximation.
  const strokeWidthPx = Math.max(1, guideWidthMm * scale);
  const markLenMm = preview.guide_length_mm;

  const lines: ReactNode[] = [];
  let key = 0;
  // Registration-mark keep-out zones clip the page guides exactly as the
  // PDF does; empty (no clipping) without a cutter or on an older server.
  const keepOut = preview.registration_keep_out ?? [];

  // The two guide kinds share all the geometry above and are gated
  // independently below, mirroring pdf_layout.py::_draw_cut_marks. Which
  // page kind's flags arrive here is already resolved server-side, so a
  // Back Page preview draws exactly what that page will carry.
  if (!preview.hide_page_guides) {
    for (const x of xs) {
      const top = clipEdgeSegment(0, gridY0, x - halfMm, x + halfMm, keepOut, true, true);
      if (top) {
        lines.push(
          <Line key={key++} x1={x * scale} y1={top[0] * scale} x2={x * scale} y2={top[1] * scale} widthPx={strokeWidthPx} color={OUTER_LINE_COLOR} />,
        );
      }
      const bottom = clipEdgeSegment(gridY1, preview.page_h_mm, x - halfMm, x + halfMm, keepOut, true, false);
      if (bottom) {
        lines.push(
          <Line key={key++} x1={x * scale} y1={bottom[0] * scale} x2={x * scale} y2={bottom[1] * scale} widthPx={strokeWidthPx} color={OUTER_LINE_COLOR} />,
        );
      }
    }
    for (const y of ys) {
      const left = clipEdgeSegment(0, gridX0, y - halfMm, y + halfMm, keepOut, false, true);
      if (left) {
        lines.push(
          <Line key={key++} x1={left[0] * scale} y1={y * scale} x2={left[1] * scale} y2={y * scale} widthPx={strokeWidthPx} color={OUTER_LINE_COLOR} />,
        );
      }
      const right = clipEdgeSegment(gridX1, preview.page_w_mm, y - halfMm, y + halfMm, keepOut, false, false);
      if (right) {
        lines.push(
          <Line key={key++} x1={right[0] * scale} y1={y * scale} x2={right[1] * scale} y2={y * scale} widthPx={strokeWidthPx} color={OUTER_LINE_COLOR} />,
        );
      }
    }
  }

  if (!preview.hide_card_guides) {
    for (const x of xs) {
      for (const y of ys) {
        lines.push(
          <Line
            key={key++}
            x1={(x - markLenMm) * scale}
            y1={y * scale}
            x2={(x + markLenMm) * scale}
            y2={y * scale}
            widthPx={strokeWidthPx}
            color={MARK_COLOR}
          />,
        );
        lines.push(
          <Line
            key={key++}
            x1={x * scale}
            y1={(y - markLenMm) * scale}
            x2={x * scale}
            y2={(y + markLenMm) * scale}
            widthPx={strokeWidthPx}
            color={MARK_COLOR}
          />,
        );
      }
    }
  }
  return <>{lines}</>;
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
  const scale = PANEL_WIDTH_PX / preview.page_w_mm;
  const pageHeightPx = preview.page_h_mm * scale;
  const marks = preview.registration_marks ?? [];
  const keepOut = preview.registration_keep_out ?? [];

  return (
    <div
      className="pdf-page-preview"
      style={{
        position: "relative",
        width: PANEL_WIDTH_PX,
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
