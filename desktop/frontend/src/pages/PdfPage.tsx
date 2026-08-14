import { useState } from "react";
import { useQuery } from "@tanstack/react-query";
import { generationApi, ApiError } from "../api/generation";
import type { CardRow } from "../api/project";
import NumberInput from "../components/NumberInput";
import PdfPagePreview from "../components/PdfPagePreview";
import { DPI_OPTIONS } from "../constants";
import { useConnection } from "../connection";
import { useServerReadiness } from "../config";
import { useProject } from "../context/ProjectContext";
import {
  DownloadCanceled,
  runDownload,
  setDownloadCancel,
  setDownloadPhase,
} from "../download";
import type { DeckEntryIn, PdfLayoutRequest } from "../api/types";

// Render progress ticks about once a second per card image, so a
// sub-second poll keeps the bar responsive without hammering the server.
const POLL_INTERVAL_MS = 400;

const PAGE_PRESETS: Record<string, { width: number; height: number }> = {
  A4: { width: 210, height: 297 },
  Letter: { width: 215.9, height: 279.4 },
};

type LayoutSettings = Omit<PdfLayoutRequest, "project_tag" | "entries" | "project_name">;

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
  const { projectId, projectTag, projectName, cards, settings, setSettings } = useProject();
  const readiness = useServerReadiness();
  const connection = useConnection();
  // See DecklistPage's identical check — generation-server unreachability
  // used to leave this page's preview/download just silently failing with
  // no feedback.
  const serverUnavailable =
    connection.mode === "remote" ? !connection.remoteHealthy : readiness.status !== "ready";
  // Persisted on the project itself (ProjectContext's `settings`, saved via
  // the same project-bar Save button as the Decklist tab's generation
  // settings) rather than local component state — these used to live in a
  // useState here and silently reset to defaults on every reload.
  const layout: LayoutSettings = {
    page_width_mm: settings.page_width_mm,
    page_height_mm: settings.page_height_mm,
    cols: settings.cols,
    rows: settings.rows,
    bleed_mm: settings.bleed_mm,
    spacing_x_mm: settings.spacing_x_mm,
    spacing_y_mm: settings.spacing_y_mm,
    offset_x_mm: settings.offset_x_mm,
    offset_y_mm: settings.offset_y_mm,
    guide_width_pt: settings.guide_width_pt,
    guide_length_mm: settings.guide_length_mm,
    export_dpi: settings.export_dpi,
    show_cut_lines: settings.show_cut_lines,
    preferred_dpi: settings.preferred_dpi,
    preferred_model: settings.preferred_model,
  };
  const [downloading, setDownloading] = useState(false);
  const [downloadError, setDownloadError] = useState<string | null>(null);

  const entries = cards.map(cardToEntry);

  // Populates the "Preferred model" picker. Same source as the Decklist
  // tab's generation-model dropdown, but used here to choose which
  // already-generated variant to print, not what to generate.
  const modelsQuery = useQuery({
    queryKey: ["models"],
    queryFn: () => generationApi.listModels(),
  });

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

  // cards_per_page is always cols*rows server-side (pdf_layout.resolve_page_
  // layout) and every page but the last is filled to exactly that (pdf_
  // layout.paginate) — so total capacity minus units is precisely how many
  // print slots are open on the last page. units already counts a
  // double-faced card's front/back as two slots, so no separate DFC
  // handling is needed here.
  const cardsPerPage = layout.cols * layout.rows;
  const leftoverSlots = previewQuery.data
    ? cardsPerPage * previewQuery.data.page_count - previewQuery.data.units
    : 0;

  function updateLayout<K extends keyof LayoutSettings>(key: K, value: LayoutSettings[K]) {
    setSettings((s) => ({ ...s, [key]: value }));
  }

  function applyPreset(name: string) {
    const preset = PAGE_PRESETS[name];
    if (preset) {
      setSettings((s) => ({ ...s, page_width_mm: preset.width, page_height_mm: preset.height }));
    }
  }

  /**
   * Ask where to save, render server-side while reporting progress, then
   * stream the finished PDF to that path.
   *
   * The render is a polled background job rather than one blocking POST:
   * it costs ~0.7s per unique card image, so a real deck spends tens of
   * seconds before any bytes exist and the UI would otherwise sit inert.
   * runDownload takes the save-location prompt first and calls this back
   * for the URL, so the user can choose up front and walk away instead of
   * being interrupted by a dialog once the render finally lands.
   */
  async function handleDownload() {
    if (projectTag == null || serverUnavailable) return;
    setDownloadError(null);
    setDownloading(true);
    try {
      await runDownload(`${projectName || "proxy-scaler"}.pdf`, async () => {
        const body = {
          project_tag: projectTag,
          entries,
          project_name: projectName,
          ...layout,
        };
        const started = await generationApi.startPdfJob(body);
        setDownloadPhase({ kind: "rendering", completed: 0, total: started.total });
        setDownloadCancel(() => {
          void generationApi.cancelPdfJob(started.job_id);
        });

        for (;;) {
          const status = await generationApi.pdfJobStatus(started.job_id);
          if (status.status === "done") break;
          if (status.status === "canceled") throw new DownloadCanceled();
          if (status.status === "failed") {
            throw new Error(status.error || "PDF render failed.");
          }
          setDownloadPhase({
            kind: "rendering",
            completed: status.completed,
            total: status.total,
          });
          await new Promise((resolve) => setTimeout(resolve, POLL_INTERVAL_MS));
        }
        // A plain GET, so Rust can stream it straight to disk.
        return { url: generationApi.pdfJobResultUrl(started.job_id) };
      });
    } catch (err) {
      // Cancelling is a normal outcome, not something to show as an error.
      if (err instanceof DownloadCanceled) return;
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
        <h3 style={{ marginBottom: 14 }}>Source images</h3>

        {/* Which already-generated variant to print for each card. These
            only select among existing images — they never trigger
            generation. Preferred DPI is a hard filter: a card with no
            image at it is left out and listed below, not substituted at
            another resolution. Within that DPI the preferred model wins,
            else the most recently generated image (see
            pdf_layout.py::_pick_dpi_variant). */}
        <div className="field-group">
          <label className="field">
            <span>Preferred model</span>
            <select
              value={layout.preferred_model ?? ""}
              disabled={modelsQuery.isLoading || modelsQuery.isError}
              onChange={(e) => updateLayout("preferred_model", e.target.value || null)}
            >
              <option value="">Any (highest DPI available)</option>
              {(modelsQuery.data ?? []).map((m) => (
                <option key={m.value} value={m.value}>
                  {m.label}
                </option>
              ))}
            </select>
          </label>

          <label className="field">
            <span>Preferred DPI</span>
            <select
              value={layout.preferred_dpi ?? ""}
              onChange={(e) =>
                updateLayout("preferred_dpi", e.target.value ? Number(e.target.value) : null)
              }
            >
              <option value="">Any (highest available)</option>
              {DPI_OPTIONS.map((dpi) => (
                <option key={dpi} value={dpi}>
                  {dpi}
                </option>
              ))}
            </select>
          </label>
        </div>

        {modelsQuery.isError && (
          <p className="error-text">
            Couldn&apos;t load the model list:{" "}
            {modelsQuery.error instanceof Error
              ? modelsQuery.error.message
              : String(modelsQuery.error)}
          </p>
        )}

        <h3 style={{ margin: "18px 0 14px" }}>Layout</h3>

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
              <NumberInput
                value={layout.page_width_mm}
                onChange={(v) => updateLayout("page_width_mm", v)}
              />
            </label>
            <label className="field">
              <span>Height (mm)</span>
              <NumberInput
                value={layout.page_height_mm}
                onChange={(v) => updateLayout("page_height_mm", v)}
              />
            </label>
          </div>

          <div style={{ display: "flex", gap: 10 }}>
            <label className="field">
              <span>Columns</span>
              <NumberInput
                min={1}
                value={layout.cols}
                onChange={(v) => updateLayout("cols", v)}
              />
            </label>
            <label className="field">
              <span>Rows</span>
              <NumberInput
                min={1}
                value={layout.rows}
                onChange={(v) => updateLayout("rows", v)}
              />
            </label>
          </div>

          {/* The preview approximates bleed by edge-extending a small
              cached thumbnail, while the real render extends the
              full-resolution art — close, but not identical. Flagged in
              the UI rather than chased, since keeping the two pipelines
              pixel-identical isn't worth the coupling. */}
          <label
            className="field"
            title="Not accurate to what you'll see in the final generated PDF currently."
          >
            <span>Bleed (mm)</span>
            <NumberInput
              step={0.1}
              value={layout.bleed_mm}
              onChange={(v) => updateLayout("bleed_mm", v)}
            />
          </label>

          <div style={{ display: "flex", gap: 10 }}>
            <label className="field">
              <span>Spacing X (mm)</span>
              <NumberInput
                step={0.1}
                value={layout.spacing_x_mm}
                onChange={(v) => updateLayout("spacing_x_mm", v)}
              />
            </label>
            <label className="field">
              <span>Spacing Y (mm)</span>
              <NumberInput
                step={0.1}
                value={layout.spacing_y_mm}
                onChange={(v) => updateLayout("spacing_y_mm", v)}
              />
            </label>
          </div>

          <div style={{ display: "flex", gap: 10 }}>
            <label className="field">
              <span>Offset X (mm)</span>
              <NumberInput
                step={0.1}
                value={layout.offset_x_mm}
                onChange={(v) => updateLayout("offset_x_mm", v)}
              />
            </label>
            <label className="field">
              <span>Offset Y (mm)</span>
              <NumberInput
                step={0.1}
                value={layout.offset_y_mm}
                onChange={(v) => updateLayout("offset_y_mm", v)}
              />
            </label>
          </div>

          <label className="field">
            <span>Export DPI</span>
            <NumberInput
              value={layout.export_dpi}
              onChange={(v) => updateLayout("export_dpi", v)}
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
                <NumberInput
                  step={0.05}
                  value={layout.guide_width_pt}
                  onChange={(v) => updateLayout("guide_width_pt", v)}
                />
              </label>
              <label className="field">
                <span>Guide length (mm)</span>
                <NumberInput
                  step={0.1}
                  value={layout.guide_length_mm}
                  onChange={(v) => updateLayout("guide_length_mm", v)}
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
            {leftoverSlots > 0 && (
              <p className="hint" style={{ marginTop: 6 }}>
                <strong>{leftoverSlots}</strong> open slot{leftoverSlots === 1 ? "" : "s"}{" "}
                left on your last page — add more cards to fill it out (a double-faced
                card uses 2 slots).
              </p>
            )}
            {previewQuery.data.missing.length > 0 && (
              <>
                <p className="error-text" style={{ marginTop: 10 }}>
                  <strong>
                    {previewQuery.data.missing.length} card(s) from your decklist have no
                    generated image yet
                  </strong>{" "}
                  and are left out of this PDF — generate them from the Decklist tab.
                </p>
                <ul style={{ margin: "4px 0 0", paddingLeft: 18 }}>
                  {previewQuery.data.missing.map((note, i) => (
                    <li key={i} className="error-text">
                      {note}
                    </li>
                  ))}
                </ul>
              </>
            )}
            {previewQuery.data.missing_at_dpi.length > 0 && (
              <>
                <p className="error-text" style={{ marginTop: 10 }}>
                  <strong>
                    {previewQuery.data.missing_at_dpi.length} card(s) have no image at{" "}
                    {layout.preferred_dpi} DPI
                  </strong>{" "}
                  and are left out of this PDF — generate them at that DPI, or set
                  Preferred DPI to &ldquo;Any&rdquo;.
                </p>
                <ul style={{ margin: "4px 0 0", paddingLeft: 18 }}>
                  {previewQuery.data.missing_at_dpi.map((name, i) => (
                    <li key={i} className="error-text">
                      {name}
                    </li>
                  ))}
                </ul>
              </>
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
            onClick={() => handleDownload()}
            disabled={downloading || entries.length === 0 || serverUnavailable}
            title={serverUnavailable ? "Generation server is unreachable" : undefined}
          >
            {downloading ? "Generating…" : "Generate & Download PDF"}
          </button>
          {downloadError && <span className="error-text">{downloadError}</span>}
        </div>
      </main>
    </div>
  );
}
