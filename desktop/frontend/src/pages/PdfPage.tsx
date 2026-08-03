import { useState } from "react";
import { useQuery } from "@tanstack/react-query";
import { api, ApiError } from "../api/client";
import { useProject } from "../context/ProjectContext";
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
      const url = URL.createObjectURL(blob);
      const a = document.createElement("a");
      a.href = url;
      a.download = `${projectName || "proxy-scaler"}.pdf`;
      document.body.appendChild(a);
      a.click();
      a.remove();
      URL.revokeObjectURL(url);
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
        <p>Enter a project name in the project bar above and click Save to get started.</p>
      </div>
    );
  }

  return (
    <div style={{ display: "flex", gap: 24 }}>
      <aside style={{ width: 280, flexShrink: 0 }}>
        <h3>Layout</h3>
        <label>
          Page size
          <select onChange={(e) => applyPreset(e.target.value)} defaultValue="A4">
            {Object.keys(PAGE_PRESETS).map((name) => (
              <option key={name} value={name}>
                {name}
              </option>
            ))}
          </select>
        </label>
        <label>
          Page width (mm)
          <input
            type="number"
            value={layout.page_width_mm}
            onChange={(e) => updateLayout("page_width_mm", Number(e.target.value))}
          />
        </label>
        <label>
          Page height (mm)
          <input
            type="number"
            value={layout.page_height_mm}
            onChange={(e) => updateLayout("page_height_mm", Number(e.target.value))}
          />
        </label>
        <label>
          Columns
          <input
            type="number"
            min={1}
            value={layout.cols}
            onChange={(e) => updateLayout("cols", Number(e.target.value))}
          />
        </label>
        <label>
          Rows
          <input
            type="number"
            min={1}
            value={layout.rows}
            onChange={(e) => updateLayout("rows", Number(e.target.value))}
          />
        </label>
        <label>
          Bleed (mm)
          <input
            type="number"
            step={0.1}
            value={layout.bleed_mm}
            onChange={(e) => updateLayout("bleed_mm", Number(e.target.value))}
          />
        </label>
        <label>
          Export DPI
          <input
            type="number"
            value={layout.export_dpi}
            onChange={(e) => updateLayout("export_dpi", Number(e.target.value))}
          />
        </label>
        <label>
          <input
            type="checkbox"
            checked={layout.show_cut_lines}
            onChange={(e) => updateLayout("show_cut_lines", e.target.checked)}
          />
          Show cut lines
        </label>
      </aside>

      <main style={{ flex: 1 }}>
        <h2>PDF</h2>
        {previewQuery.isLoading ? (
          <p>Calculating layout…</p>
        ) : previewQuery.data ? (
          <div>
            <p>
              {previewQuery.data.units} card(s) matched across {previewQuery.data.page_count} page
              (s).
            </p>
            {previewQuery.data.unmatched.length > 0 && (
              <ul>
                {previewQuery.data.unmatched.map((note, i) => (
                  <li key={i} style={{ color: "#c66" }}>
                    {note}
                  </li>
                ))}
              </ul>
            )}
          </div>
        ) : null}

        <button onClick={handleDownload} disabled={downloading}>
          {downloading ? "Generating…" : "Generate & Download PDF"}
        </button>
        {downloadError && <p style={{ color: "#c66" }}>{downloadError}</p>}
      </main>
    </div>
  );
}
