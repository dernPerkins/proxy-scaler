import { useState } from "react";
import { useQuery } from "@tanstack/react-query";
import { api, ApiError } from "../api/client";
import { useProject } from "../context/ProjectContext";
import { downloadBlob } from "../download";
import type { PdfLayoutRequest } from "../api/types";

const PAGE_PRESETS: Record<string, { width: number; height: number }> = {
  A4: { width: 210, height: 297 },
  Letter: { width: 215.9, height: 279.4 },
};

const DEFAULT_LAYOUT: PdfLayoutRequest = {
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

export default function PdfPage() {
  const { projectId, projectName } = useProject();
  const [layout, setLayout] = useState<PdfLayoutRequest>(DEFAULT_LAYOUT);
  const [downloading, setDownloading] = useState(false);
  const [downloadError, setDownloadError] = useState<string | null>(null);

  const previewQuery = useQuery({
    queryKey: ["pdf-preview", projectId, layout],
    queryFn: () => api.pdfPreview(projectId as number, layout),
    enabled: projectId != null,
  });

  function updateLayout<K extends keyof PdfLayoutRequest>(key: K, value: PdfLayoutRequest[K]) {
    setLayout((l) => ({ ...l, [key]: value }));
  }

  function applyPreset(name: string) {
    const preset = PAGE_PRESETS[name];
    if (preset) {
      setLayout((l) => ({ ...l, page_width_mm: preset.width, page_height_mm: preset.height }));
    }
  }

  async function handleDownload() {
    if (projectId == null) return;
    setDownloadError(null);
    setDownloading(true);
    try {
      const blob = await api.downloadPdf(projectId, layout);
      await downloadBlob(blob, `${projectName || "proxy-scaler"}.pdf`);
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
        </div>
      </aside>

      <main className="content">
        <h2>PDF</h2>

        {previewQuery.isLoading ? (
          <p className="hint" style={{ marginTop: 10 }}>
            Calculating layout…
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

        <div className="summary-row">
          <button className="btn-primary" onClick={handleDownload} disabled={downloading}>
            {downloading ? "Generating…" : "Generate & Download PDF"}
          </button>
          {downloadError && <span className="error-text">{downloadError}</span>}
        </div>
      </main>
    </div>
  );
}
