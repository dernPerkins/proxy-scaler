import { useState } from "react";
import { useQuery } from "@tanstack/react-query";
import { generationApi, ApiError } from "../api/generation";
import type { CardRow } from "../api/project";
import PdfPagePreview from "../components/PdfPagePreview";
import { useConnection } from "../connection";
import { useServerReadiness } from "../config";
import { useProject } from "../context/ProjectContext";
import { downloadBlob } from "../download";
import type { DeckEntryIn, PdfLayoutRequest } from "../api/types";

const PAGE_PRESETS: Record<string, { width: number; height: number }> = {
  A4: { width: 210, height: 297 },
  Letter: { width: 215.9, height: 279.4 },
};

type LayoutSettings = Omit<PdfLayoutRequest, "project_tag" | "entries" | "project_name">;

const DEFAULT_LAYOUT: LayoutSettings = {
  page_width_mm: PAGE_PRESETS.A4.width,
  page_height_mm: PAGE_PRESETS.A4.height,
  cols: 3,
  rows: 3,
  bleed_mm: 1.0,
  spacing_x_mm: 0,
  spacing_y_mm: 0,
  offset_x_mm: 0,
  offset_y_mm: 0,
  guide_width_pt: 0.75,
  guide_length_mm: 2.75,
  export_dpi: 1200,
  show_cut_lines: true,
  preferred_dpi: null,
  preferred_model: null,
};

function cardToEntry(card: CardRow): DeckEntryIn {
  return {
    quantity: card.quantity ?? 1,
    name: card.name,
    set_code: card.set_code,
    collector_number: card.collector_number,
    raw_line: card.original_import_line,
  };
}

export default function PdfPage() {
  const { projectId, projectTag, projectName, cards } = useProject();
  const readiness = useServerReadiness();
  const connection = useConnection();
  // See DecklistPage's identical check — generation-server unreachability
  // used to leave this page's preview/download just silently failing with
  // no feedback.
  const serverUnavailable =
    connection.mode === "remote" ? !connection.remoteHealthy : readiness.status !== "ready";
  const [layout, setLayout] = useState<LayoutSettings>(DEFAULT_LAYOUT);
  const [downloading, setDownloading] = useState(false);
  const [downloadError, setDownloadError] = useState<string | null>(null);

  const entries = cards.map(cardToEntry);

  const previewQuery = useQuery({
    queryKey: ["pdf-preview", projectTag, entries, layout],
    queryFn: () =>
      generationApi.pdfPreview({ project_tag: projectTag as string, entries, ...layout }),
    enabled: projectTag != null && entries.length > 0 && !serverUnavailable,
  });

  // Page-1-only visual layout preview — separate query/endpoint from the
  // numeric one above (see api/types.ts's PdfPagePreview comment for why
  // they're kept distinct). Same enabled-gating as previewQuery.
  const pagePreviewQuery = useQuery({
    queryKey: ["pdf-page-preview", projectTag, entries, layout],
    queryFn: () =>
      generationApi.pdfPagePreview({ project_tag: projectTag as string, entries, ...layout }),
    enabled: projectTag != null && entries.length > 0 && !serverUnavailable,
  });

  function updateLayout<K extends keyof LayoutSettings>(key: K, value: LayoutSettings[K]) {
    setLayout((l) => ({ ...l, [key]: value }));
  }

  function applyPreset(name: string) {
    const preset = PAGE_PRESETS[name];
    if (preset) {
      setLayout((l) => ({ ...l, page_width_mm: preset.width, page_height_mm: preset.height }));
    }
  }

  async function handleDownload(method: "fpdf2" | "html") {
    if (projectTag == null || serverUnavailable) return;
    setDownloadError(null);
    setDownloading(true);
    try {
      const body = { project_tag: projectTag, entries, project_name: projectName, ...layout };
      const blob =
        method === "html"
          ? await generationApi.downloadPdfHtml(body)
          : await generationApi.downloadPdf(body);
      const suffix = method === "html" ? "-html" : "";
      await downloadBlob(blob, `${projectName || "proxy-scaler"}${suffix}.pdf`);
    } catch (err) {
      setDownloadError(err instanceof ApiError ? err.message : String(err));
    } finally {
      setDownloading(false);
    }
  }

  if (projectId == null) {
    return (
      <div>
        <h2>PDF</h2>
        <p className="hint" style={{ marginTop: 8 }}>
          Enter a project name in the project bar above and click Save to get started.
        </p>
      </div>
    );
  }

  return (
    <div className="layout">
      <aside className="sidebar panel">
        <h3 style={{ marginBottom: 14 }}>Layout</h3>

        <div className="field-group">
          <label className="field">
            <span>Page size</span>
            <select onChange={(e) => applyPreset(e.target.value)} defaultValue="A4">
              {Object.keys(PAGE_PRESETS).map((name) => (
                <option key={name} value={name}>
                  {name}
                </option>
              ))}
            </select>
          </label>

          {/* Paired dimensions sit side by side — they're read together and
              each is narrow enough that a full-width row each just adds
              vertical scrolling to the sidebar. */}
          <div style={{ display: "flex", gap: 10 }}>
            <label className="field">
              <span>Width (mm)</span>
              <input
                type="number"
                value={layout.page_width_mm}
                onChange={(e) => updateLayout("page_width_mm", Number(e.target.value))}
              />
            </label>
            <label className="field">
              <span>Height (mm)</span>
              <input
                type="number"
                value={layout.page_height_mm}
                onChange={(e) => updateLayout("page_height_mm", Number(e.target.value))}
              />
            </label>
          </div>

          <div style={{ display: "flex", gap: 10 }}>
            <label className="field">
              <span>Columns</span>
              <input
                type="number"
                min={1}
                value={layout.cols}
                onChange={(e) => updateLayout("cols", Number(e.target.value))}
              />
            </label>
            <label className="field">
              <span>Rows</span>
              <input
                type="number"
                min={1}
                value={layout.rows}
                onChange={(e) => updateLayout("rows", Number(e.target.value))}
              />
            </label>
          </div>

          <label className="field">
            <span>Bleed (mm)</span>
            <input
              type="number"
              step={0.1}
              value={layout.bleed_mm}
              onChange={(e) => updateLayout("bleed_mm", Number(e.target.value))}
            />
          </label>

          <div style={{ display: "flex", gap: 10 }}>
            <label className="field">
              <span>Spacing X (mm)</span>
              <input
                type="number"
                step={0.1}
                value={layout.spacing_x_mm}
                onChange={(e) => updateLayout("spacing_x_mm", Number(e.target.value))}
              />
            </label>
            <label className="field">
              <span>Spacing Y (mm)</span>
              <input
                type="number"
                step={0.1}
                value={layout.spacing_y_mm}
                onChange={(e) => updateLayout("spacing_y_mm", Number(e.target.value))}
              />
            </label>
          </div>

          <div style={{ display: "flex", gap: 10 }}>
            <label className="field">
              <span>Offset X (mm)</span>
              <input
                type="number"
                step={0.1}
                value={layout.offset_x_mm}
                onChange={(e) => updateLayout("offset_x_mm", Number(e.target.value))}
              />
            </label>
            <label className="field">
              <span>Offset Y (mm)</span>
              <input
                type="number"
                step={0.1}
                value={layout.offset_y_mm}
                onChange={(e) => updateLayout("offset_y_mm", Number(e.target.value))}
              />
            </label>
          </div>

          <label className="field">
            <span>Export DPI</span>
            <input
              type="number"
              value={layout.export_dpi}
              onChange={(e) => updateLayout("export_dpi", Number(e.target.value))}
            />
          </label>

          <label className="check">
            <input
              type="checkbox"
              checked={layout.show_cut_lines}
              onChange={(e) => updateLayout("show_cut_lines", e.target.checked)}
            />
            Show cut lines
          </label>

          {layout.show_cut_lines && (
            <div style={{ display: "flex", gap: 10 }}>
              <label className="field">
                <span>Guide width (pt)</span>
                <input
                  type="number"
                  step={0.05}
                  value={layout.guide_width_pt}
                  onChange={(e) => updateLayout("guide_width_pt", Number(e.target.value))}
                />
              </label>
              <label className="field">
                <span>Guide length (mm)</span>
                <input
                  type="number"
                  step={0.1}
                  value={layout.guide_length_mm}
                  onChange={(e) => updateLayout("guide_length_mm", Number(e.target.value))}
                />
              </label>
            </div>
          )}
        </div>
      </aside>

      <main className="content">
        <h2>PDF</h2>

        {serverUnavailable && (
          <p className="error-text" style={{ marginTop: 10 }}>
            Generation server is unreachable — reconnect before generating.
          </p>
        )}

        {entries.length === 0 ? (
          <p className="hint" style={{ marginTop: 10 }}>
            No cards in this project yet — add some from the Decklist tab first.
          </p>
        ) : serverUnavailable ? null : previewQuery.isLoading ? (
          <p className="hint" style={{ marginTop: 10 }}>
            Calculating layout…
          </p>
        ) : previewQuery.isError ? (
          <p className="error-text" style={{ marginTop: 10 }}>
            Couldn&apos;t calculate layout:{" "}
            {previewQuery.error instanceof Error ? previewQuery.error.message : String(previewQuery.error)}
          </p>
        ) : previewQuery.data ? (
          <div className="panel" style={{ padding: 14, marginTop: 10 }}>
            <p>
              <strong>{previewQuery.data.units}</strong> card(s) across{" "}
              <strong>{previewQuery.data.page_count}</strong> page(s).
            </p>
            {previewQuery.data.unmatched.length > 0 && (
              <ul style={{ margin: "8px 0 0", paddingLeft: 18 }}>
                {previewQuery.data.unmatched.map((note, i) => (
                  <li key={i} className="error-text">
                    {note}
                  </li>
                ))}
              </ul>
            )}
          </div>
        ) : null}

        {!serverUnavailable && entries.length > 0 && (
          <div style={{ marginTop: 14 }}>
            {pagePreviewQuery.isLoading ? (
              <p className="hint">Loading page preview…</p>
            ) : pagePreviewQuery.isError ? (
              <p className="error-text">
                Couldn&apos;t load page preview:{" "}
                {pagePreviewQuery.error instanceof Error
                  ? pagePreviewQuery.error.message
                  : String(pagePreviewQuery.error)}
              </p>
            ) : pagePreviewQuery.data && pagePreviewQuery.data.slots.length > 0 ? (
              <PdfPagePreview preview={pagePreviewQuery.data} />
            ) : null}
          </div>
        )}

        <div className="summary-row">
          <button
            className="btn-primary"
            onClick={() => handleDownload("fpdf2")}
            disabled={downloading || entries.length === 0 || serverUnavailable}
            title={serverUnavailable ? "Generation server is unreachable" : undefined}
          >
            {downloading ? "Generating…" : "Generate & Download PDF"}
          </button>
          {/* Alternate HTML->PDF pipeline via WeasyPrint, for comparing
              output quality against the default fpdf2 method above — see
              pdf_html.py. 503s with a clear message when the connected
              server doesn't have the optional html-pdf extra installed. */}
          <button
            onClick={() => handleDownload("html")}
            disabled={downloading || entries.length === 0 || serverUnavailable}
            title={serverUnavailable ? "Generation server is unreachable" : undefined}
          >
            {downloading ? "Generating…" : "Download PDF (HTML/WeasyPrint)"}
          </button>
          {downloadError && <span className="error-text">{downloadError}</span>}
        </div>
      </main>
    </div>
  );
}
