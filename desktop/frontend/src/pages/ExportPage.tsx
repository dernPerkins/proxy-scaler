import { useState } from "react";
import ModelSelect from "../components/ModelSelect";
import SelectMenu from "../components/SelectMenu";
import PresetBar from "../components/PresetBar";
import { useQuery } from "@tanstack/react-query";
import { generationApi, ApiError } from "../api/generation";
import { DPI_OPTIONS } from "../constants";
import { useConnection } from "../connection";
import {
  EXPORT_OPTIONS_MIN_SERVER_VERSION,
  EXPORT_ZIP_MIN_SERVER_VERSION,
  PER_CARD_BACKS_MIN_SERVER_VERSION,
  serverSupportsExportOptions,
  serverSupportsOriginals,
  serverSupportsPerCardBacks,
  serverSupportsZipExport,
  useServerReadiness,
  useServerVersion,
} from "../config";
import { useProject } from "../context/ProjectContext";
import { useTourOnFirstView } from "../tutorial/tutorialStore";
import { cardToEntry, sortCards } from "../deckEntries";
import { backImageRef, referencedBackIds, summarizeCardBacks, useBackImagesSync, useBackLibrary } from "../cardBacks";
import CardBackSummaryNote from "../components/CardBackSummaryNote";
import NumberInput from "../components/NumberInput";
import SortSelect from "../components/SortSelect";
import { registerCustomCards, waitForTasks } from "../syncCustoms";
import { UploadCanceled } from "../uploadProgress";
import {
  DownloadCanceled,
  runDownload,
  setDownloadCancel,
  setDownloadPhase,
} from "../download";
import { OriginalsFallbackNote } from "../components/OriginalsFallbackNote";
import { zipFilename } from "../zipFilename";
import type { ExportImageFormat, ExportZipFormat, ReverseFill } from "../api/types";
import tcgplaytestLogo from "../assets/tcgplaytest.webp";

// Same cadence as the PDF tab's job polling.
const POLL_INTERVAL_MS = 400;

// MakePlayingCards.com's spec: 63x88 mm trim, 69x94 mm with bleed.
const MPC_BLEED_MM = 3;

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
  // Output options are the same silent-drop shape (see config.ts): an
  // older server would ship unbled PNGs whatever the controls say, so
  // they're disabled and the request sends the inert defaults.
  const optionsSupported = serverSupportsExportOptions(serverVersion);
  const withBleed = settings.export_with_bleed && optionsSupported;
  const imageFormat: ExportImageFormat = optionsSupported ? settings.export_image_format : "png";

  // The project's Selected Back, resolved out of the app-global library —
  // same resolution + sync-to-connected-server dance as PdfPage (the
  // shared queryKeys mean React Query dedupes the two pages' copies).
  const backLibraryQuery = useBackLibrary();
  const selectedBack =
    settings.back_image_id != null
      ? backLibraryQuery.data?.find((b) => b.id === settings.back_image_id)
      : undefined;
  // The Selected Back plus every card's own back (see cardBacks.ts).
  // Unlike PdfPage there's no "backs off" mode here — every back is wanted
  // in every export, so sync whenever the server can take them.
  const referencedBacks = referencedBackIds(cards, settings)
    .map((id) => backLibraryQuery.data?.find((b) => b.id === id))
    .filter((b): b is NonNullable<typeof b> => b != null);
  const backSync = useBackImagesSync(
    referencedBacks.map((b) => b.id),
    !serverUnavailable && !serverTooOld,
  );
  const backReady = backSync.ready;
  const cardBacks = summarizeCardBacks(cards, backLibraryQuery.data);
  const perCardBacksSupported = serverSupportsPerCardBacks(serverVersion);

  const [downloading, setDownloading] = useState(false);
  const [downloadError, setDownloadError] = useState<string | null>(null);

  // Sorted before mapping: ZIP filenames are numbered in entries order,
  // so this is where the shared sort dropdown reaches the exported files.
  const entries = sortCards(cards, settings.sort_primary).map(cardToEntry);

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
      image_format: imageFormat,
      with_bleed: withBleed,
      bleed_mm: settings.export_bleed_mm,
      // The Back Library's own declaration about the file, same as the
      // PDF tab sends — the server cover-fits rather than double-bleeds.
      back_image_includes_bleed: selectedBack?.includes_bleed ?? false,
      back_image_bleed_mm: selectedBack?.includes_bleed ? selectedBack.bleed_mm : null,
      back_images: referencedBacks.map(backImageRef),
      // The ZIP's own back rules. Older servers ignore both and do what
      // they always did (pair, fill with the back image); the Back export
      // controls are disabled against them so the settings can't lie.
      reverse_fill: settings.export_reverse_fill,
      back_faces_as_reverse: settings.export_back_faces_as_reverse,
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
      // The counts don't depend on these, but a key that omits part of
      // the body it sends is a lie waiting to be believed.
      imageFormat,
      withBleed,
      settings.export_bleed_mm,
      // These two DO change the counts: pairing halves the vendor
      // format's slots, and the fill decides which backs are blank.
      settings.export_reverse_fill,
      settings.export_back_faces_as_reverse,
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
      // Same as the PDF tab: the archive is built from the server's
      // registry, so custom art it hasn't seen has to be synced AND
      // registered first — normally done at add time, re-run here to
      // self-heal customs added while no server was reachable
      // (registerCustomCards is idempotent; the wait covers the
      // file-copy task a never-cached upload needs).
      await waitForTasks(await registerCustomCards(cards, projectTag, serverVersion));
      const body = requestBody(format);
      if (!optionsSupported) {
        // A server without the job routes: the original synchronous
        // export — Rust POSTs the body and streams the archive to disk.
        // Only ever the verbatim PNG copy here (the options are inert),
        // which is disk-speed, so there's nothing to poll anyway.
        await runDownload(zipFilename(projectName), {
          url: generationApi.exportZipUrl(),
          body,
        });
        return;
      }
      // Otherwise the PDF tab's job loop: adding bleed or converting to
      // JPG re-renders every image (~1s each at 1200 DPI), so the server
      // reports per-image progress and the finished archive is fetched
      // from a plain GET Rust can stream. A plain PNG export is "done" on
      // the first poll — one code path regardless of the options.
      await runDownload(zipFilename(projectName), async () => {
        const started = await generationApi.startExportZipJob(body);
        setDownloadPhase({
          kind: "rendering",
          label: "Rendering images…",
          completed: 0,
          total: started.total,
        });
        setDownloadCancel(() => {
          void generationApi.cancelExportZipJob(started.job_id);
        });

        for (;;) {
          const status = await generationApi.exportZipJobStatus(started.job_id);
          if (status.status === "done") break;
          if (status.status === "canceled") throw new DownloadCanceled();
          if (status.status === "failed") {
            throw new Error(status.error || "Export failed.");
          }
          setDownloadPhase({
            kind: "rendering",
            label: "Rendering images…",
            completed: status.completed,
            total: status.total,
          });
          await new Promise((resolve) => setTimeout(resolve, POLL_INTERVAL_MS));
        }
        return { url: generationApi.exportZipJobResultUrl(started.job_id) };
      });
    } catch (err) {
      // Cancelling is a normal outcome, not something to show as an error.
      if (err instanceof DownloadCanceled || err instanceof UploadCanceled) return;
      setDownloadError(err instanceof ApiError ? err.message : String(err));
    } finally {
      setDownloading(false);
    }
  }

  // With no project the page is just a placeholder — nothing to tour yet.
  useTourOnFirstView("export", { present: projectId != null });

  if (projectId == null) {
    return (
      <div>
        <h2>ZIP</h2>
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
  // The vendor format pairs every front with a back, so a Selected Back is
  // needed exactly when some back would be it: not when every single-faced
  // card is blank or has its own, and not for an all-DFC deck with pairing
  // on. Until the preview has answered, assume it is needed — the server
  // refuses the export either way, this only decides the button's state.
  const tcgNeedsBack =
    settings.back_image_id == null &&
    (previewQuery.data == null || previewQuery.data.reverses_needing_back_image > 0);
  const tcgWaitingOnSync = !tcgNeedsBack && !backReady;

  const optionsTooOldTitle = "The connected generation server is too old for this — update it.";
  const backsTooOldTitle = `Back export settings need a generation server v${PER_CARD_BACKS_MIN_SERVER_VERSION} or newer — update it.`;
  const bledSizeNote =
    settings.export_bleed_mm === MPC_BLEED_MM
      ? " (69 × 94 mm — MakePlayingCards.com's size)"
      : ` (${(63 + 2 * settings.export_bleed_mm).toFixed(1)} × ${(88 + 2 * settings.export_bleed_mm).toFixed(1)} mm)`;

  return (
    <div className="layout">
      <aside className="sidebar panel">
        <PresetBar kind="zip" tourId="export-presets" />

        <h3 style={{ marginBottom: 14 }}>Source images</h3>

        {/* Which already-generated variant to export for each card — the
            PDF tab's selectors over the same persisted settings. They
            only select among existing images, never trigger generation;
            Preferred DPI is a hard filter among upscales, with the 300 DPI
            original as the fallback (see PdfPage / pdf_layout.py::
            _pick_scryfall_variant). */}
        <div className="field-group" data-tour="export-source">
          <label className="field">
            <span>Preferred model</span>
            <ModelSelect
              value={settings.preferred_model ?? ""}
              models={modelsQuery.data}
              disabled={modelsQuery.isLoading || modelsQuery.isError}
              onChange={(value) => setSettings((s) => ({ ...s, preferred_model: value || null }))}
              anyOption="Any (highest DPI available)"
            />
          </label>

          <label className="field">
            <span>Preferred DPI</span>
            <SelectMenu
              options={[
                { value: "", label: "Any (highest available)" },
                ...DPI_OPTIONS.map((dpi) => ({ value: String(dpi), label: String(dpi) })),
              ]}
              value={settings.preferred_dpi == null ? "" : String(settings.preferred_dpi)}
              onChange={(value) =>
                setSettings((s) => ({ ...s, preferred_dpi: value ? Number(value) : null }))
              }
            />
          </label>

          {/* Same override, same persisted setting as the PDF tab's
              checkbox — see PdfPage's Source images section. */}
          <label
            className="check"
            title={
              !originalsSupported
                ? "The connected generation server is too old for this — update it."
                : "Export Scryfall cards from their downloaded ~300 DPI originals only. Custom and MPC Fill images are unaffected and still follow the preferred model/DPI."
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
            Use Only 300 DPI Originals
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

        <h3 style={{ margin: "18px 0 14px" }}>Back export</h3>

        {/* The PDF tab's "Back printing" rules, for the ZIP. Separate
            settings on purpose (export_reverse_fill / export_back_faces_as_
            reverse): a vendor upload and a home print can want different
            answers, and each tab's presets carry their own. Both exports
            resolve a card's back the same way the PDF does — transform
            side, then the card's own choice, then these two. */}
        <div className="field-group" data-tour="export-backs">
          <label
            className="field"
            title={perCardBacksSupported || serverUnavailable ? undefined : backsTooOldTitle}
          >
            <span>Backs of single-faced cards</span>
            <SelectMenu
              options={[
                { value: "back_image", label: "Use the back image" },
                { value: "blank", label: "Leave blank" },
              ]}
              value={settings.export_reverse_fill}
              disabled={!perCardBacksSupported && !serverUnavailable}
              onChange={(value) =>
                setSettings((s) => ({ ...s, export_reverse_fill: value as ReverseFill }))
              }
            />
          </label>
          {settings.export_reverse_fill === "back_image" ? (
            <p className="hint" style={{ margin: "2px 0 8px" }}>
              {selectedBack ? (
                <>
                  Using <strong>{selectedBack.label}</strong> — change it on the Backs tab.
                </>
              ) : (
                <>No back selected. Pick one on the Backs tab.</>
              )}
            </p>
          ) : (
            <p className="hint" style={{ margin: "2px 0 8px" }}>
              Single-faced cards get a plain white back in the TCGPlaytest ZIP, and no{" "}
              <code>BACK/</code> file in the plain ZIP.
            </p>
          )}
          <CardBackSummaryNote
            summary={cardBacks}
            serverSupports={serverUnavailable || perCardBacksSupported}
            serverVersion={serverVersion}
          />

          <label
            className="check"
            title={perCardBacksSupported || serverUnavailable ? undefined : backsTooOldTitle}
          >
            <input
              type="checkbox"
              checked={settings.export_back_faces_as_reverse}
              disabled={!perCardBacksSupported && !serverUnavailable}
              onChange={(e) =>
                setSettings((s) => ({ ...s, export_back_faces_as_reverse: e.target.checked }))
              }
            />
            Pair a double-faced card&apos;s transform side as its back
          </label>
          <p className="hint" style={{ margin: "-2px 0 8px" }}>
            {settings.export_back_faces_as_reverse
              ? "A double-faced card is one front/back pair in the TCGPlaytest ZIP."
              : settings.export_reverse_fill === "blank"
                ? "Each face is its own card with a blank back (unless the card chose one)."
                : "Each face is its own card, and both get the back image."}{" "}
            The plain ZIP always ships every face once.
          </p>
        </div>

        <h3 style={{ margin: "18px 0 14px" }}>Output</h3>

        {/* Export-only, persisted per project like everything else on this
            page. PNG + no bleed is the original export (the stored files,
            untouched); anything else re-renders each image server-side. */}
        <div className="field-group" data-tour="export-output">
          <div
            className="field"
            title={
              !optionsSupported
                ? optionsTooOldTitle
                : "PNG ships the stored images exactly as they are. JPG re-encodes every image for much smaller files — recommended when ordering from a vendor."
            }
          >
            <span>Image format</span>
            <div className="segmented">
              <button
                className={imageFormat === "png" ? "active" : ""}
                disabled={!optionsSupported}
                onClick={() => setSettings((s) => ({ ...s, export_image_format: "png" }))}
              >
                PNG
              </button>
              <button
                className={imageFormat === "jpg" ? "active" : ""}
                disabled={!optionsSupported}
                onClick={() => setSettings((s) => ({ ...s, export_image_format: "jpg" }))}
              >
                JPG
              </button>
            </div>
            {/* Vendor uploads (TCGPlaytest, MakePlayingCards) are where the
                size difference matters most, and print quality is the same
                either way — so say so here, at the moment of choosing,
                rather than only in a hover tooltip. */}
            <p className="hint">
              JPG is recommended for vendor orders (TCGPlaytest, MakePlayingCards) —
              far smaller uploads with no visible difference in print.
            </p>
          </div>

          <label
            className="check"
            title={
              !optionsSupported
                ? optionsTooOldTitle
                : `Adds a bleed border around every image. Defaults to the ${MPC_BLEED_MM} mm bleed MakePlayingCards.com expects (69 × 94 mm cards).`
            }
          >
            <input
              type="checkbox"
              disabled={!optionsSupported}
              checked={withBleed}
              onChange={(e) =>
                setSettings((s) => ({ ...s, export_with_bleed: e.target.checked }))
              }
            />
            Export with bleed
          </label>

          {withBleed && (
            <label
              className="field"
              title={`Per side. MakePlayingCards.com expects ${MPC_BLEED_MM} mm.`}
            >
              <span>Bleed (mm)</span>
              <NumberInput
                step={0.1}
                min={0.1}
                value={settings.export_bleed_mm}
                onChange={(v) => setSettings((s) => ({ ...s, export_bleed_mm: v }))}
              />
            </label>
          )}
        </div>

        {/* A tooltip on a disabled control isn't discoverable — nothing
            invites hovering it — so the reason is also written out. */}
        {!serverUnavailable && !serverTooOld && !optionsSupported && (
          <p className="hint" style={{ marginTop: 10 }}>
            Image format and bleed need a generation server v
            {EXPORT_OPTIONS_MIN_SERVER_VERSION} or newer — update it to use them.
          </p>
        )}
      </aside>

      <main className="content">
        <h2>ZIP</h2>

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
              {selectedBack || cardBacks.ownBacks.length > 0 ? (
                <>
                  {" "}
                  plus {selectedBack && cardBacks.ownBacks.length === 0
                    ? <>your selected back image as the single <code>BACK/</code> entry</>
                    : <>each back the deck uses, once, in <code>BACK/</code></>}
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
              quantities included — with{" "}
              {settings.export_back_faces_as_reverse
                ? "double-faced cards backed by their own transform side"
                : "each face of a double-faced card as its own pair"}
              , cards with their own back by that back, and everything else by{" "}
              {settings.export_reverse_fill === "blank"
                ? "a plain white back"
                : "your selected back image"}
              .
            </p>
            {(previewQuery.data.blank_backs ?? 0) > 0 && (
              <p className="hint" style={{ marginTop: 6 }}>
                {previewQuery.data.blank_backs} back
                {previewQuery.data.blank_backs === 1 ? "" : "s"} will be a plain white card.
              </p>
            )}
            {/* What the files will be, so the Output settings' effect is
                visible before the export, not discovered after. */}
            <p className="hint" style={{ marginTop: 6 }}>
              {imageFormat === "jpg"
                ? "Every image is re-encoded as JPG"
                : withBleed
                  ? "Every image is re-encoded as PNG"
                  : "Images are exported exactly as stored (PNG)"}
              {withBleed
                ? `, with a ${settings.export_bleed_mm} mm bleed on each side${bledSizeNote}.`
                : "."}
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
                        no image yet
                      </strong>{" "}
                      and are left out of these ZIPs — download or generate them from the
                      Decklist tab.
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
            <OriginalsFallbackNote names={previewQuery.data.from_originals} />
          </div>
        ) : null}

        <div className="summary-row" style={{ marginTop: 14 }} data-tour="export-buttons" data-tour-fit>
          <SortSelect />
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
