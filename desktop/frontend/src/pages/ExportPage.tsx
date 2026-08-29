import { useState } from "react";
import { useQuery } from "@tanstack/react-query";
import { generationApi, ApiError } from "../api/generation";
import { projectApi } from "../api/project";
import { DPI_OPTIONS } from "../constants";
import { useConnection } from "../connection";
import {
  EXPORT_ZIP_MIN_SERVER_VERSION,
  getApiBaseUrl,
  serverSupportsOriginals,
  serverSupportsZipExport,
  useServerReadiness,
  useServerVersion,
} from "../config";
import { useProject } from "../context/ProjectContext";
import { cardToEntry } from "../deckEntries";
import { DownloadCanceled, runDownload } from "../download";
import { zipFilename } from "../zipFilename";
import type { ExportZipFormat } from "../api/types";
import tcgplaytestLogo from "../assets/tcgplaytest.webp";

export default function ExportPage() {
  const { projectId, projectTag, projectName, cards, settings, setSettings } = useProject();
  const readiness = useServerReadiness();
  const connection = useConnection();
  // Same unreachability check as PdfPage — without it the buttons just
  // silently fail.
  const serverUnavailable =
    connection.mode === "remote" ? !connection.remoteHealthy : readiness.status !== "ready";
  // The /api/export endpoints 404 on older servers — a loud failure, but
  // "server too old, update it" beats a bare "Not Found". See config.ts.
  const serverVersion = useServerVersion();
  const serverTooOld = !serverUnavailable && !serverSupportsZipExport(serverVersion);
  // Same guard as PdfPage: an older server drops the flag silently and
  // would export the preferred variants while the checkbox claims
  // originals.
  const originalsSupported = serverSupportsOriginals(serverVersion);
  const useOriginals = settings.use_originals && originalsSupported;

  // The project's Selected Back, resolved out of the app-global library —
  // same resolution + sync-to-connected-server dance as PdfPage (the
  // shared queryKeys mean React Query dedupes the two pages' copies).
  const backLibraryQuery = useQuery({
    queryKey: ["back-images"],
    queryFn: () => projectApi.listBackImages(),
  });
  const selectedBack =
    settings.back_image_id != null
      ? backLibraryQuery.data?.find((b) => b.id === settings.back_image_id)
      : undefined;
  const backSyncQuery = useQuery({
    queryKey: ["back-image-sync", selectedBack?.id, getApiBaseUrl()],
    queryFn: () => projectApi.syncBackImage(selectedBack!.id, getApiBaseUrl()),
    // Unlike PdfPage there's no "backs off" mode here — any selected back
    // is wanted in every export, so sync whenever one exists.
    enabled: selectedBack != null && !serverUnavailable && !serverTooOld,
  });
  const backReady = selectedBack == null || backSyncQuery.isSuccess;

  const [downloading, setDownloading] = useState(false);
  const [downloadError, setDownloadError] = useState<string | null>(null);

  const entries = cards.map(cardToEntry);

  // Same selectors, same persisted fields as the PDF tab (settings.
  // preferred_model / preferred_dpi) — deliberately shared, not
  // export-specific: they answer "which generated variant is the good
  // one", and that answer doesn't change with the output format.
  const modelsQuery = useQuery({
    queryKey: ["models"],
    queryFn: () => generationApi.listModels(),
  });

  function requestBody(format: ExportZipFormat) {
    return {
      project_tag: projectTag as string,
      entries,
      project_name: projectName ?? "",
      preferred_dpi: settings.preferred_dpi,
      preferred_model: settings.preferred_model,
      use_originals: useOriginals,
      format,
      back_image_hash: selectedBack?.content_hash ?? null,
    };
  }

  const previewQuery = useQuery({
    queryKey: [
      "export-zip-preview",
      projectTag,
      entries,
      settings.preferred_dpi,
      settings.preferred_model,
      useOriginals,
    ],
    queryFn: () => generationApi.exportZipPreview(requestBody("default")),
    enabled:
      projectTag != null &&
      entries.length > 0 &&
      !serverUnavailable &&
      !serverTooOld &&
      backReady,
  });

  async function handleExport(format: ExportZipFormat) {
    if (projectTag == null || serverUnavailable) return;
    setDownloadError(null);
    setDownloading(true);
    try {
      // Static source, no prepare callback: zipping is disk-speed file
      // copying server-side, so unlike the PDF there is no render phase
      // to poll — Rust POSTs the body and streams the archive to disk.
      await runDownload(zipFilename(projectName), {
        url: generationApi.exportZipUrl(),
        body: requestBody(format),
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
        <h2>Export</h2>
        <p className="hint" style={{ marginTop: 8 }}>
          Import a decklist on the Decklist tab to get started.
        </p>
      </div>
    );
  }

  const exportBlocked =
    downloading || entries.length === 0 || serverUnavailable || serverTooOld;
  const blockedTitle = serverUnavailable
    ? "Generation server is unreachable"
    : serverTooOld
      ? `This server needs to be v${EXPORT_ZIP_MIN_SERVER_VERSION} or newer`
      : undefined;
  // The vendor format pairs every front with a back, so it always needs a
  // Selected Back — even an all-DFC deck waits for one, by design.
  const tcgNeedsBack = settings.back_image_id == null;
  const tcgWaitingOnSync = !tcgNeedsBack && !backReady;

  return (
    <div className="layout">
      <aside className="sidebar panel">
        <h3 style={{ marginBottom: 14 }}>Source images</h3>

        {/* Which already-generated variant to export for each card — the
            PDF tab's selectors over the same persisted settings. They
            only select among existing images, never trigger generation;
            Preferred DPI is a hard filter (see PdfPage / pdf_layout.py::
            _pick_dpi_variant). */}
        <div className="field-group">
          <label className="field">
            <span>Preferred model</span>
            <select
              value={settings.preferred_model ?? ""}
              disabled={modelsQuery.isLoading || modelsQuery.isError || useOriginals}
              onChange={(e) =>
                setSettings((s) => ({ ...s, preferred_model: e.target.value || null }))
              }
            >
              <option value="">Any (highest DPI available)</option>
              {(modelsQuery.data ?? []).map((m) => (
                <option key={m.value} value={m.value}>
                  {m.label} — {m.speed}
                </option>
              ))}
            </select>
          </label>

          <label className="field">
            <span>Preferred DPI</span>
            <select
              value={settings.preferred_dpi ?? ""}
              disabled={useOriginals}
              onChange={(e) =>
                setSettings((s) => ({
                  ...s,
                  preferred_dpi: e.target.value ? Number(e.target.value) : null,
                }))
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

          {/* Same override, same persisted setting as the PDF tab's
              checkbox — see PdfPage's Source images section. */}
          <label
            className="check"
            title={
              !originalsSupported
                ? "The connected generation server is too old for this — update it."
                : "Export the downloaded ~300 DPI Scryfall originals; the preferred model/DPI don't apply."
            }
          >
            <input
              type="checkbox"
              disabled={!originalsSupported}
              checked={useOriginals}
              onChange={(e) =>
                setSettings((s) => ({ ...s, use_originals: e.target.checked }))
              }
            />
            Use 300 DPI originals
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

        <p className="hint" style={{ marginTop: 14 }}>
          Both exports share these with the PDF tab — they pick which
          already-generated image is used, and never trigger generation.
        </p>
      </aside>

      <main className="content">
        <h2>Export</h2>

        {serverUnavailable && (
          <p className="error-text" style={{ marginTop: 10 }}>
            Generation server is unreachable — reconnect before exporting.
          </p>
        )}

        {serverTooOld && (
          <p className="error-text" style={{ marginTop: 10 }}>
            <strong>This generation server is too old for this version of the app.</strong>{" "}
            ZIP export needs v{EXPORT_ZIP_MIN_SERVER_VERSION} or newer
            {serverVersion ? ` (it reports v${serverVersion})` : ""} — update the server
            and reconnect.
          </p>
        )}

        {entries.length === 0 ? (
          <p className="hint" style={{ marginTop: 10 }}>
            No cards in this project yet — add some from the Decklist tab first.
          </p>
        ) : serverUnavailable || serverTooOld ? null : previewQuery.isLoading ? (
          <p className="hint" style={{ marginTop: 10 }}>
            Checking images…
          </p>
        ) : previewQuery.isError ? (
          <p className="error-text" style={{ marginTop: 10 }}>
            Couldn&apos;t check images:{" "}
            {previewQuery.error instanceof Error
              ? previewQuery.error.message
              : String(previewQuery.error)}
          </p>
        ) : previewQuery.data ? (
          <div className="panel" style={{ padding: 14, marginTop: 10 }}>
            <p>
              <strong>Export ZIP</strong> packs <strong>{previewQuery.data.fronts}</strong>{" "}
              unique card image{previewQuery.data.fronts === 1 ? "" : "s"} into{" "}
              <code>FRONT/</code>
              {selectedBack ? (
                <>
                  {" "}
                  plus your selected back image as the single <code>BACK/</code> entry
                </>
              ) : (
                <> (no back image selected, so no {""}<code>BACK/</code> folder)</>
              )}
              .
            </p>
            <p style={{ marginTop: 6 }}>
              <strong>Export TCGPlaytest ZIP</strong> packs{" "}
              <strong>{previewQuery.data.paired_fronts}</strong> front/back pair
              {previewQuery.data.paired_fronts === 1 ? "" : "s"} — one per physical card,
              quantities included — with double-faced cards backed by their own transform
              side and everything else by your selected back image.
            </p>
            {previewQuery.data.missing.length > 0 && (
              <>
                <p className="error-text" style={{ marginTop: 10 }}>
                  {useOriginals ? (
                    <>
                      <strong>
                        {previewQuery.data.missing.length} card(s) from your decklist have
                        no downloaded original
                      </strong>{" "}
                      and are left out of these ZIPs — use Download images on the Decklist
                      tab.
                    </>
                  ) : (
                    <>
                      <strong>
                        {previewQuery.data.missing.length} card(s) from your decklist have
                        no generated image yet
                      </strong>{" "}
                      and are left out of these ZIPs — generate them from the Decklist tab.
                    </>
                  )}
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
                    {settings.preferred_dpi} DPI
                  </strong>{" "}
                  and are left out of these ZIPs — generate them at that DPI, or set
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

        <div className="summary-row" style={{ marginTop: 14 }}>
          <button
            className="btn-primary export-btn"
            onClick={() => handleExport("default")}
            disabled={exportBlocked}
            title={blockedTitle}
          >
            {downloading ? "Exporting…" : "Export ZIP"}
          </button>
          {/* Button plus its disabled-reason note. The tooltip alone
              isn't discoverable — nothing invites hovering a disabled
              button — and a note under the whole row read as a
              page-level aside and got missed, so the reason hangs
              directly off the button it's about (absolutely positioned,
              so the buttons themselves stay in line). */}
          <div className="export-btn-stack">
            <button
              className="export-btn export-vendor-btn"
              onClick={() => handleExport("tcgplaytest")}
              disabled={exportBlocked || tcgNeedsBack || tcgWaitingOnSync}
              title={
                blockedTitle ??
                (tcgNeedsBack
                  ? "Requires a selected back image — pick one on the Backs tab"
                  : tcgWaitingOnSync
                    ? "Syncing your back image to the server…"
                    : undefined)
              }
            >
              {downloading ? "Exporting…" : "Export"}
              <img src={tcgplaytestLogo} alt="TCGPlaytest" />
              {downloading ? "" : "ZIP"}
            </button>
            {!exportBlocked && tcgNeedsBack && (
              <p className="hint export-btn-note">
                Select a card back on the Backs tab to enable this export.
              </p>
            )}
            {!exportBlocked && tcgWaitingOnSync && (
              <p className="hint export-btn-note">
                Syncing your back image to the server — unlocks in a moment.
              </p>
            )}
          </div>
          {downloadError && <span className="error-text">{downloadError}</span>}
        </div>
      </main>
    </div>
  );
}
