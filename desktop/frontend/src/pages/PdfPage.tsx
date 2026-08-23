import { useState } from "react";
import { useQuery } from "@tanstack/react-query";
import { generationApi, ApiError } from "../api/generation";
import { projectApi } from "../api/project";
import NumberInput from "../components/NumberInput";
import PdfPagePreview from "../components/PdfPagePreview";
import { DPI_OPTIONS } from "../constants";
import { useConnection } from "../connection";
import {
  BACK_PRINTING_MIN_SERVER_VERSION,
  getApiBaseUrl,
  serverSupportsBackPrinting,
  useServerReadiness,
  useServerVersion,
} from "../config";
import { useProject } from "../context/ProjectContext";
import { cardToEntry } from "../deckEntries";
import {
  DownloadCanceled,
  runDownload,
  setDownloadCancel,
  setDownloadPhase,
} from "../download";
import { pdfFilename } from "../pdfFilename";
import type {
  FlipEdge,
  PageOrder,
  PdfLayoutRequest,
  ReverseFill,
} from "../api/types";

// Render progress ticks about once a second per card image, so a
// sub-second poll keeps the bar responsive without hammering the server.
const POLL_INTERVAL_MS = 400;

// Orientation lives in the preset name rather than in a control of its
// own. It is only ever "which of these two numbers is bigger", and the
// two numbers are already editable below — a separate toggle would be a
// second control writing the same pair of fields. Both orientations are
// offered because paper grain makes a sheet noticeably more rigid one way
// than the other, and which way depends on the stock.
//
// A preset carries its grid too: picking a paper is really "set me up for
// this paper", and the grid that fits a portrait sheet does not fit the
// landscape one. 3×3 needs 195×270mm (fits both portrait presets, with
// Letter the tightest at 9.4mm of vertical slack); 4×2 needs 260×180mm
// and is the most cards that fit either landscape sheet. Both stay
// editable afterwards, and the overflow warning below covers whatever the
// user changes them to.
const PAGE_PRESETS: Record<
  string,
  { width: number; height: number; cols: number; rows: number }
> = {
  "A4 (Portrait)": { width: 210, height: 297, cols: 3, rows: 3 },
  "A4 (Landscape)": { width: 297, height: 210, cols: 4, rows: 2 },
  "Letter (Portrait)": { width: 215.9, height: 279.4, cols: 3, rows: 3 },
  "Letter (Landscape)": { width: 279.4, height: 215.9, cols: 4, rows: 2 },
};

// Shown when the width/height inputs hold something no preset matches.
// Not selectable — it describes the current state rather than being a
// thing you can choose.
const CUSTOM_PAGE_SIZE = "Custom";

/** Which preset the current dimensions correspond to, or CUSTOM_PAGE_SIZE.
 *
 *  The select used to be uncontrolled with a hardcoded default, so it read
 *  "A4" whatever the project actually held. Harmless with two presets and
 *  actively wrong now that the label claims an orientation: a box reading
 *  "A4 (Portrait)" above a landscape page is worse than no label at all.
 *
 *  Matched on the page size only, never the grid: a preset also sets
 *  cols/rows, but changing those afterwards is normal and shouldn't make
 *  the paper you picked read as "Custom".
 *
 *  Compared with a tolerance because these round-trip through the number
 *  inputs and a float stored as 279.40000000000003 is still Letter. */
function matchPagePreset(width: number, height: number): string {
  const match = Object.entries(PAGE_PRESETS).find(
    ([, size]) => Math.abs(size.width - width) < 0.05 && Math.abs(size.height - height) < 0.05,
  );
  return match ? match[0] : CUSTOM_PAGE_SIZE;
}

type LayoutSettings = Omit<PdfLayoutRequest, "project_tag" | "entries" | "project_name">;

export default function PdfPage() {
  const { projectId, projectTag, projectName, cards, settings, setSettings } = useProject();
  const readiness = useServerReadiness();
  const connection = useConnection();
  // See DecklistPage's identical check — generation-server unreachability
  // used to leave this page's preview/download just silently failing with
  // no feedback.
  const serverUnavailable =
    connection.mode === "remote" ? !connection.remoteHealthy : readiness.status !== "ready";
  const layoutWantsBacks = settings.back_printing;
  // The one drift direction the server cannot reject for itself: an old
  // server ignores the guide flags silently and renders with its own
  // defaults, so this has to be caught before the request goes out. A hard
  // block on rendering, not a dismissible toast — the failure mode is a
  // wrong PDF, not a confusing one. See config.ts.
  const serverVersion = useServerVersion();
  const serverTooOld = !serverUnavailable && !serverSupportsBackPrinting(serverVersion);
  // The project's Selected Back, resolved out of the app-global library.
  // Library reads are local invokes — they work with no server reachable,
  // which is the whole point of the client owning it (docs/adr/0003).
  const backLibraryQuery = useQuery({
    queryKey: ["back-images"],
    queryFn: () => projectApi.listBackImages(),
  });
  const selectedBack =
    settings.back_image_id != null
      ? backLibraryQuery.data?.find((b) => b.id === settings.back_image_id)
      : undefined;

  // Make sure the CONNECTED server holds this back's bytes before anything
  // asks it to render with them. Keyed on the base URL as well as the
  // back, so switching to a server that has never seen this file re-syncs
  // rather than silently previewing a back that isn't there. Rust GETs
  // first and only uploads on a miss, so the steady-state cost is one
  // small request.
  const backSyncQuery = useQuery({
    queryKey: ["back-image-sync", selectedBack?.id, getApiBaseUrl()],
    queryFn: () => projectApi.syncBackImage(selectedBack!.id, getApiBaseUrl()),
    enabled: selectedBack != null && layoutWantsBacks && !serverUnavailable,
  });
  // Only gates the queries when a back is actually needed: an all-DFC
  // sheet, or back printing off, has nothing to wait for.
  const backReady = selectedBack == null || !layoutWantsBacks || backSyncQuery.isSuccess;

  // Which side of page 1 the visual preview shows. Local state, not a
  // persisted setting — it's a way of looking at the sheet, not a
  // property of it.
  //
  // Derived rather than reset, because the choice has to survive back
  // printing being switched off and on again while never being *acted*
  // on while it's off. Storing it alone left the preview showing a Back
  // Page after the toggle went off — and the front/back buttons are
  // hidden then, so there was no way to get back to the front.
  const [previewSideChoice, setPreviewSideChoice] = useState<"front" | "back">("front");
  const previewSide = settings.back_printing ? previewSideChoice : "front";

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
    preferred_dpi: settings.preferred_dpi,
    preferred_model: settings.preferred_model,
    hide_card_guides_front: settings.hide_card_guides_front,
    hide_page_guides_front: settings.hide_page_guides_front,
    hide_card_guides_back: settings.hide_card_guides_back,
    hide_page_guides_back: settings.hide_page_guides_back,
    back_printing: settings.back_printing,
    back_faces_as_reverse: settings.back_faces_as_reverse,
    reverse_fill: settings.reverse_fill,
    page_order: settings.page_order,
    flip_edge: settings.flip_edge,
    back_offset_x_mm: settings.back_offset_x_mm,
    back_offset_y_mm: settings.back_offset_y_mm,
    back_image_hash: selectedBack?.content_hash ?? null,
    back_image_includes_bleed: selectedBack?.includes_bleed ?? false,
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
    enabled:
      projectTag != null &&
      entries.length > 0 &&
      !serverUnavailable &&
      !serverTooOld &&
      backReady,
  });

  // Page-1-only visual layout preview — separate query/endpoint from the
  // numeric one above (see api/types.ts's PdfPagePreview comment for why
  // they're kept distinct). Same enabled-gating as previewQuery.
  const pagePreviewQuery = useQuery({
    queryKey: ["pdf-page-preview", projectTag, entries, layout, previewSide],
    queryFn: () =>
      generationApi.pdfPagePreview({
        project_tag: projectTag as string,
        entries,
        ...layout,
        preview_back_page: previewSide === "back",
      }),
    enabled:
      projectTag != null &&
      entries.length > 0 &&
      !serverUnavailable &&
      !serverTooOld &&
      backReady,
  });

  // cards_per_page is always cols*rows server-side (pdf_layout.resolve_page_
  // layout) and every page but the last is filled to exactly that (pdf_
  // layout.paginate) — so total capacity minus units is precisely how many
  // print slots are open on the last page.
  //
  // What a double-faced card COSTS in slots is now mode-dependent: two
  // when its faces print as separate cards, one when the transform side
  // goes on its own back. `units` already reflects whichever mode is on
  // (the server pairs before paginating), so the arithmetic is unchanged —
  // only the sentence explaining it below has to follow the mode.
  const cardsPerPage = layout.cols * layout.rows;
  // resolve_page_layout deliberately does not refuse a grid larger than
  // its page (a custom offset may push past an edge on purpose) and asks
  // callers to check instead — this is that check. Reachable most easily
  // by picking a landscape preset while keeping a tall grid: 3×3 is 270mm
  // of cards, which fits portrait A4 and runs off landscape A4.
  const preview = pagePreviewQuery.data;
  const gridOverflows =
    preview != null &&
    (preview.grid_w_mm > preview.page_w_mm || preview.grid_h_mm > preview.page_h_mm);

  const dfcSlotHint =
    layout.back_printing && layout.back_faces_as_reverse
      ? "a double-faced card uses 1 slot, with its transform side on the back"
      : "a double-faced card uses 2 slots";
  const leftoverSlots = previewQuery.data
    ? cardsPerPage * previewQuery.data.page_count - previewQuery.data.units
    : 0;

  function updateLayout<K extends keyof LayoutSettings>(key: K, value: LayoutSettings[K]) {
    setSettings((s) => ({ ...s, [key]: value }));
  }

  function applyPreset(name: string) {
    const preset = PAGE_PRESETS[name];
    if (preset) {
      setSettings((s) => ({
        ...s,
        page_width_mm: preset.width,
        page_height_mm: preset.height,
        // The grid comes with the paper — a portrait grid does not fit a
        // landscape sheet, so carrying the old one over would leave every
        // orientation change tripping the overflow warning.
        cols: preset.cols,
        rows: preset.rows,
      }));
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
      await runDownload(pdfFilename(projectName), async () => {
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

  // No row yet means nothing has been imported — a name is not what is
  // missing. The old copy here asked for one and for a Save button, and
  // both were still on screen after the gates came down: naming is
  // optional now (spec §5.1) and there is no Save button at all (§5.5).
  // The row is born at first import, so that is what to point at.
  if (projectId == null) {
    return (
      <div>
        <h2>PDF</h2>
        <p className="hint" style={{ marginTop: 8 }}>
          Import a decklist on the Decklist tab to get started.
        </p>
      </div>
    );
  }

  return (
    <div className="layout">
      <aside className="sidebar sidebar-split panel">
        {/* Two columns rather than one tall stack: four sections stacked
            pushed the Generate button below the fold on a laptop screen.
            The split lands at Guides so the halves come out roughly even.
            Collapses back to one column on a narrow window — see
            .sidebar-split in styles.css. */}
        <div className="sidebar-cols">
          <div>
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
                <select
                  value={matchPagePreset(layout.page_width_mm, layout.page_height_mm)}
                  onChange={(e) => applyPreset(e.target.value)}
                >
                  {Object.keys(PAGE_PRESETS).map((name) => (
                    <option key={name} value={name}>
                      {name}
                    </option>
                  ))}
                  {/* Only rendered while it applies, so it never looks
                      like a size you could pick. */}
                  {matchPagePreset(layout.page_width_mm, layout.page_height_mm) ===
                    CUSTOM_PAGE_SIZE && (
                    <option value={CUSTOM_PAGE_SIZE} disabled>
                      {CUSTOM_PAGE_SIZE}
                    </option>
                  )}
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
            </div>
          </div>

          <div>
            {/* Guides, split by kind and by page kind. Card Guides are the
                small marks at each card's own trim corners; Page Guides are
                the lines running from the page edge in to the grid. They used
                to share one switch, which couldn't express the setting most
                duplex printers actually want: guides on the fronts you cut
                against, none on the backs that show. */}
            <h3 style={{ margin: "18px 0 14px" }}>
              Guides{" "}
              <span
                className="hint"
                title="Front pages are the side you cut against, so guides there are the ones you use. Back pages default to none — that ink lands on the visible side of the card."
              >
                (?)
              </span>
            </h3>

            <div className="field-group">
              <label className="check">
                <input
                  type="checkbox"
                  checked={layout.hide_card_guides_front}
                  onChange={(e) => updateLayout("hide_card_guides_front", e.target.checked)}
                />
                Hide card guides on front pages
              </label>
              <label className="check">
                <input
                  type="checkbox"
                  checked={layout.hide_page_guides_front}
                  onChange={(e) => updateLayout("hide_page_guides_front", e.target.checked)}
                />
                Hide page guides on front pages
              </label>
              {/* Still shown (not hidden) while back printing is off, so the
                  settings stay comparable side by side — just inert, and said
                  so rather than silently ignored. */}
              <label className="check" style={{ opacity: layout.back_printing ? 1 : 0.5 }}>
                <input
                  type="checkbox"
                  disabled={!layout.back_printing}
                  checked={layout.hide_card_guides_back}
                  onChange={(e) => updateLayout("hide_card_guides_back", e.target.checked)}
                />
                Hide card guides on back pages
              </label>
              <label className="check" style={{ opacity: layout.back_printing ? 1 : 0.5 }}>
                <input
                  type="checkbox"
                  disabled={!layout.back_printing}
                  checked={layout.hide_page_guides_back}
                  onChange={(e) => updateLayout("hide_page_guides_back", e.target.checked)}
                />
                Hide page guides on back pages
              </label>
            </div>

            <h3 style={{ margin: "18px 0 14px" }}>Back printing</h3>

            <div className="field-group">
              <label className="check">
                <input
                  type="checkbox"
                  checked={layout.back_printing}
                  onChange={(e) => updateLayout("back_printing", e.target.checked)}
                />
                Print card backs
              </label>

              {layout.back_printing && (
                <>
                  {/* What goes on the back of a card that has no
                      transform side. "Leave blank" is the mode for
                      printing a deck purely so its double-faced cards get
                      their own backs — it needs no back image at all,
                      which is why the picker below it goes away entirely
                      in that mode. */}
                  <label className="field">
                    <span>Backs of single-faced cards</span>
                    <select
                      value={layout.reverse_fill}
                      onChange={(e) =>
                        updateLayout("reverse_fill", e.target.value as ReverseFill)
                      }
                    >
                      <option value="back_image">Use the back image</option>
                      <option value="blank">Leave blank</option>
                    </select>
                  </label>

                  {layout.reverse_fill === "back_image" ? (
                    <p className="hint" style={{ margin: "2px 0 8px" }}>
                      {selectedBack ? (
                        <>
                          Using <strong>{selectedBack.label}</strong> — change it on
                          the Backs tab.
                        </>
                      ) : (
                        <>No back selected. Pick one on the Backs tab.</>
                      )}
                    </p>
                  ) : (
                    <p className="hint" style={{ margin: "2px 0 8px" }}>
                      Only double-faced cards get anything on their back. No back
                      image needed.
                    </p>
                  )}

                  {/* Changes the print-slot count and therefore the page
                      count, which is why it lives here rather than with the
                      Back Image on the Backs tab. */}
                  <label className="check">
                    <input
                      type="checkbox"
                      checked={layout.back_faces_as_reverse}
                      onChange={(e) =>
                        updateLayout("back_faces_as_reverse", e.target.checked)
                      }
                    />
                    Print a double-faced card&apos;s transform side on its own back
                  </label>
                  <p className="hint" style={{ margin: "-2px 0 8px" }}>
                    {layout.back_faces_as_reverse
                      ? "A double-faced card is one printed card with both faces on it."
                      : layout.reverse_fill === "blank"
                        ? "Each face prints as its own card — so with blank backs, every back page comes out empty."
                        : "Each face prints as its own card, and both get the back image."}
                  </p>

                  <label className="field">
                    <span>
                      Flip edge{" "}
                      <span
                        className="hint"
                        title="Match this to your printer's duplex setting — 'Flip on long edge' or 'Flip on short edge' in the print dialog. If they disagree, every card gets someone else's back."
                      >
                        (?)
                      </span>
                    </span>
                    <select
                      value={layout.flip_edge}
                      onChange={(e) => updateLayout("flip_edge", e.target.value as FlipEdge)}
                    >
                      <option value="long">Long edge</option>
                      <option value="short">Short edge</option>
                    </select>
                  </label>

                  <label className="field">
                    <span>Page order</span>
                    <select
                      value={layout.page_order}
                      onChange={(e) => updateLayout("page_order", e.target.value as PageOrder)}
                    >
                      <option value="duplex">Duplex (front, back, front…)</option>
                      <option value="fronts_then_backs">All fronts, then all backs</option>
                    </select>
                  </label>

                  {/* Independent of the front offsets rather than added to
                      them: they calibrate two separate passes through the
                      printer, and duplex registration genuinely drifts. */}
                  <div style={{ display: "flex", gap: 10 }}>
                    <label className="field">
                      <span>Back offset X (mm)</span>
                      <NumberInput
                        step={0.1}
                        value={layout.back_offset_x_mm ?? 0}
                        onChange={(v) => updateLayout("back_offset_x_mm", v)}
                      />
                    </label>
                    <label className="field">
                      <span>Back offset Y (mm)</span>
                      <NumberInput
                        step={0.1}
                        value={layout.back_offset_y_mm ?? 0}
                        onChange={(v) => updateLayout("back_offset_y_mm", v)}
                      />
                    </label>
                  </div>
                </>
              )}
            </div>
          </div>
        </div>
      </aside>

      <main className="content">
        <h2>PDF</h2>

        {serverUnavailable && (
          <p className="error-text" style={{ marginTop: 10 }}>
            Generation server is unreachable — reconnect before generating.
          </p>
        )}

        {serverTooOld && (
          <p className="error-text" style={{ marginTop: 10 }}>
            <strong>This generation server is too old for this version of the app.</strong>{" "}
            It needs v{BACK_PRINTING_MIN_SERVER_VERSION} or newer
            {serverVersion ? ` (it reports v${serverVersion})` : ""}. Rendering is
            blocked rather than silently producing a PDF with the wrong guide settings
            — update the server and reconnect.
          </p>
        )}

        {entries.length === 0 ? (
          <p className="hint" style={{ marginTop: 10 }}>
            No cards in this project yet — add some from the Decklist tab first.
          </p>
        ) : serverUnavailable || serverTooOld ? null : previewQuery.isLoading ? (
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
              <strong>{previewQuery.data.page_count}</strong> sheet(s)
              {layout.back_printing && (
                <>
                  {" "}
                  — <strong>{previewQuery.data.total_page_count}</strong> pages once
                  backs are included
                </>
              )}
              .
            </p>
            {leftoverSlots > 0 && (
              <p className="hint" style={{ marginTop: 6 }}>
                <strong>{leftoverSlots}</strong> open slot{leftoverSlots === 1 ? "" : "s"}{" "}
                left on your last page — add more cards to fill it out ({dfcSlotHint}).
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
            {/* Only an error when a Reverse would ACTUALLY come up empty:
                an all-double-faced sheet fills every back with a transform
                side and needs no back image at all. */}
            {previewQuery.data.missing_back_image && (
              <p className="error-text" style={{ marginTop: 10 }}>
                <strong>
                  Back printing is on, but no back image is available for{" "}
                  {previewQuery.data.reverses_needing_back_image} card(s).
                </strong>{" "}
                Pick one on the Backs tab, or turn back printing off.
              </p>
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

        {!serverUnavailable && !serverTooOld && entries.length > 0 && (
          <div style={{ marginTop: 14 }}>
            {/* Checking the flip edge here costs nothing; checking it on
                the printer costs a sheet of cardstock. */}
            {layout.back_printing && (
              <div className="summary-row" style={{ marginBottom: 8 }}>
                <button
                  className={previewSide === "front" ? "btn-primary" : "btn-sm"}
                  onClick={() => setPreviewSideChoice("front")}
                >
                  Front of page 1
                </button>
                <button
                  className={previewSide === "back" ? "btn-primary" : "btn-sm"}
                  onClick={() => setPreviewSideChoice("back")}
                >
                  Back of page 1
                </button>
              </div>
            )}
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
              <>
                {/* A warning, never a block — see gridOverflows above. */}
                {gridOverflows && preview && (
                  <p className="error-text" style={{ marginBottom: 8 }}>
                    <strong>
                      Your {layout.cols}×{layout.rows} grid is{" "}
                      {preview.grid_w_mm.toFixed(0)}×{preview.grid_h_mm.toFixed(0)}mm,
                      larger than the {preview.page_w_mm.toFixed(0)}×
                      {preview.page_h_mm.toFixed(0)}mm page.
                    </strong>{" "}
                    Cards will run off the edge — use fewer rows or columns, or the
                    other orientation.
                  </p>
                )}
                <PdfPagePreview preview={pagePreviewQuery.data} />
              </>
            ) : null}
          </div>
        )}

        <div className="summary-row">
          <button
            className="btn-primary"
            onClick={() => handleDownload()}
            disabled={
              downloading ||
              entries.length === 0 ||
              serverUnavailable ||
              serverTooOld ||
              previewQuery.data?.missing_back_image === true
            }
            title={
              serverUnavailable
                ? "Generation server is unreachable"
                : serverTooOld
                  ? `This server needs to be v${BACK_PRINTING_MIN_SERVER_VERSION} or newer`
                  : previewQuery.data?.missing_back_image
                  ? "Pick a back image on the Backs tab, or turn back printing off"
                  : undefined
            }
          >
            {downloading ? "Generating…" : "Generate & Download PDF"}
          </button>
          {downloadError && <span className="error-text">{downloadError}</span>}
        </div>
      </main>
    </div>
  );
}
