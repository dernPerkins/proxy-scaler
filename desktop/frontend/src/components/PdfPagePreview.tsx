import type { PdfPagePreview as PdfPagePreviewData } from "../api/types";

const PANEL_WIDTH_PX = 360;

// Real DOM/CSS, deliberately no iframe/dangerouslySetInnerHTML — matches
// CompareDialog.tsx's own documented precedent against iframe indirection
// in this WKWebView app. Placement math mirrors pdf_layout.py::build_pdf
// exactly (col, row = idx % cols, idx // cols) so the preview lines up
// with the real PDF output at the same layout settings.
export default function PdfPagePreview({ preview }: { preview: PdfPagePreviewData }) {
  const scale = PANEL_WIDTH_PX / preview.page_w_mm;
  const pageHeightPx = preview.page_h_mm * scale;

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
      {preview.slots.map((slot, idx) => {
        const col = idx % preview.cols;
        const row = Math.floor(idx / preview.cols);
        const x = preview.margin_x_mm + col * preview.cell_w_mm;
        const y = preview.margin_y_mm + row * preview.cell_h_mm;
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
                style={{ width: "100%", height: "100%", objectFit: "cover", display: "block" }}
              />
            )}
          </div>
        );
      })}
    </div>
  );
}
