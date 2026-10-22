// The Back Library: upload, pick, and manage the art printed on a card's
// Reverse.
//
// The library is app-global and lives on THIS machine — every project
// sees every back, and a project points at one by id. The generation
// server only ever holds a synced copy of the bytes, pushed lazily when
// something actually needs to render with them, so this whole tab works
// with no server reachable at all.
//
// A back is upscaled from here, never by a project's Generate: the
// sidebar's upscale settings are the app-global library ones
// (libraryUpscale.ts), the server queues the work under its library tag,
// and whatever it makes is used by every project that prints with the
// back — the PDF/ZIP back lookup ranks its versions by the same preferred
// DPI/model as the cards.
//
// Where back printing is configured — the toggle, flip edge, page order,
// offsets, guides — is the PDF tab, because all of those change the sheet
// rather than the image. This tab owns the image and nothing else.
//
// The magnifier on a tile opens the shared full-image viewer
// (components/ImageViewer) with the trim line drawn from the back's bleed
// declaration, and the same settings as the sidebar beside it. Viewing a
// back does not select it: selecting changes what this project prints
// with, and looking should never do that.
import { useEffect, useMemo, useState } from "react";
import { useMutation, useQuery, useQueryClient } from "@tanstack/react-query";
import { generationApi } from "../api/generation";
import { projectApi } from "../api/project";
import type { BackImage, BackImageUsage } from "../api/types";
import ConfirmDialog from "../components/ConfirmDialog";
import SelectMenu from "../components/SelectMenu";
import ImageViewer, { MAX_BLEED_MM, MagnifierIcon } from "../components/ImageViewer";
import LibraryUpscaleControls from "../components/LibraryUpscaleControls";
import UpscaleSettingsFields from "../components/UpscaleSettingsFields";
import { useConnection } from "../connection";
import {
  MPC_FILL_MIN_SERVER_VERSION,
  serverSupportsLibraryUpscale,
  serverSupportsMpcFill,
  useServerReadiness,
  useServerVersion,
} from "../config";
import { MpcArtModal } from "../components/MpcArtPicker";
import { importMpcArt } from "../mpcFill";
import {
  serverSourceDpi,
  targetsFor,
  upscaleLibraryImage,
  useLibraryStatus,
  useLibraryUpscaleSettings,
  type LibraryUpscaleMode,
} from "../libraryUpscale";
import { UploadCanceled } from "../uploadProgress";
import { errorText, toast } from "../toasts";
import { blurOnEnter } from "../inputKeys";
import { readPickedImage } from "../imageUpload";
import { useProject } from "../context/ProjectContext";
import { useTourOnFirstView, useTourWhenPresent } from "../tutorial/tutorialStore";
import SidebarColumn from "../components/SidebarColumn";
import LibraryAddPanel from "../components/LibraryAddPanel";

// Matches proxy_scaler/backs.py's MIN_COMFORTABLE_DPI and the Rust
// source_dpi calculation — below this, a back is being asked to cover a
// 63×88mm card with less detail than a decent printer resolves.
const LOW_DPI = 300;

type BleedChange = { id: number; includesBleed: boolean; bleedMm: number };

/** The per-back settings, shared by the sidebar and the viewer so the two
 *  can never disagree about what a back can be told. `tourTargets` is set
 *  only in the sidebar: the viewer renders the same fields while the
 *  sidebar is still mounted, and a tour anchor must be unique. */
function BackSettingsFields({
  back,
  isDefault,
  onBleed,
  error,
  tourTargets,
}: {
  back: BackImage;
  isDefault: boolean;
  onBleed: (change: BleedChange) => void;
  error: string | null;
  tourTargets: boolean;
}) {
  const queryClient = useQueryClient();
  return (
    <div className="field-group">
      {back.mpc && (
        <p className="mpc-provenance">From MPC Fill · {back.mpc.source || "unknown uploader"}</p>
      )}
      <label className="field">
        <span>Name</span>
        <input
          defaultValue={back.label}
          key={back.id}
          onKeyDown={blurOnEnter}
          onBlur={(e) => {
            const next = e.target.value.trim();
            if (next && next !== back.label) {
              void projectApi
                .setBackImageLabel(back.id, next)
                .then(() => queryClient.invalidateQueries({ queryKey: ["back-images"] }));
            }
          }}
        />
      </label>

      {/* The user's declaration about their own file. Art that
          already carries bleed is not edge-extended a second time:
          with the amount, the server trims the file's own bleed to
          the sheet's (or tops it up), so the trim content stays
          exactly card sized whatever bleed the project uses. */}
      <label className="check" data-tour={tourTargets ? "back-bleed" : undefined}>
        <input
          type="checkbox"
          checked={back.includes_bleed}
          onChange={(e) =>
            onBleed({ id: back.id, includesBleed: e.target.checked, bleedMm: back.bleed_mm })
          }
        />
        This image already includes bleed
      </label>
      {back.includes_bleed && (
        <label className="field">
          <span>Bleed in the file (mm per side)</span>
          <input
            type="number"
            min={0}
            max={MAX_BLEED_MM}
            step={0.001}
            key={`bleed-${back.id}`}
            defaultValue={back.bleed_mm}
            onKeyDown={blurOnEnter}
            onBlur={(e) => {
              const next = Number(e.target.value);
              if (
                Number.isFinite(next) &&
                next >= 0 &&
                next <= MAX_BLEED_MM &&
                next !== back.bleed_mm
              ) {
                onBleed({ id: back.id, includesBleed: true, bleedMm: next });
              }
            }}
          />
        </label>
      )}
      {back.includes_bleed && (
        <p className="hint" style={{ marginTop: -4 }}>
          MakePlayingCards images carry 3.175 mm (1/8 in) per side. Printing trims
          this down to the project&apos;s bleed, or extends it if the project asks
          for more.
        </p>
      )}
      {error ? <p className="error-text">{error}</p> : null}

      <label className="check" data-tour={tourTargets ? "back-default" : undefined}>
        <input
          type="checkbox"
          checked={isDefault}
          onChange={(e) => {
            void projectApi
              .setDefaultBackImageId(e.target.checked ? back.id : null)
              .then(() => queryClient.invalidateQueries({ queryKey: ["back-image-default"] }));
          }}
        />
        Use for new projects
      </label>
      <p className="hint" style={{ marginTop: -4 }}>
        New projects start with this back. Projects you already have keep whatever
        they were set to.
      </p>

      {back.source_dpi < LOW_DPI && (
        <p className="hint">
          This image works out to about {Math.round(back.source_dpi)} DPI
          across a card, which will look soft in print. It will still print —
          upscale it below, or replace it with a larger source image.
        </p>
      )}
    </div>
  );
}

/** The delete confirm's body: who loses what. Projects are left with NO
 *  back (back printing then asks for one — recoverable); cards that chose
 *  this back for themselves fall back to their project's Selected Back. */
function deleteWarning(uses: BackImageUsage): string {
  const parts: string[] = [];
  if (uses.projects > 0) {
    parts.push(
      `${uses.projects} project${uses.projects === 1 ? "" : "s"} use${uses.projects === 1 ? "s" : ""} this back and will be left with no back image — back printing will ask you to pick one before it prints.`,
    );
  }
  if (uses.cards > 0) {
    const here = uses.cards_in_project > 0 ? ` (${uses.cards_in_project} in this project)` : "";
    parts.push(
      `${uses.cards} card${uses.cards === 1 ? "" : "s"}${here} chose it individually and will go back to their project's universal back.`,
    );
  }
  if (parts.length === 0) {
    return "This removes it from your library and from the connected generation server.";
  }
  return parts.join(" ");
}

function BackTile({
  back,
  selected,
  isDefault,
  cardsHere,
  onSelect,
  onView,
}: {
  back: BackImage;
  selected: boolean;
  isDefault: boolean;
  /** Cards in the current project that chose this back for themselves
   *  (CardBackPicker) — the tile says so, since the Selected Back
   *  highlight alone would make such a back look unused. */
  cardsHere: number;
  onSelect: () => void;
  onView: () => void;
}) {
  const thumbQuery = useQuery({
    queryKey: ["back-thumb", back.id],
    queryFn: () => projectApi.backImageThumbnail(back.id),
    staleTime: Infinity,
  });
  return (
    // A div acting as a button rather than a <button>: the zoom control
    // inside it is itself a button, and buttons cannot nest.
    <div
      role="button"
      tabIndex={0}
      onClick={onSelect}
      onKeyDown={(e) => {
        if (e.key === "Enter" || e.key === " ") {
          e.preventDefault();
          onSelect();
        }
      }}
      data-tour={selected ? "back-tile" : undefined}
      className="panel"
      style={{
        padding: 8,
        borderColor: selected ? "var(--accent)" : "var(--border)",
        borderWidth: selected ? 2 : 1,
        borderStyle: "solid",
        textAlign: "left",
        cursor: "pointer",
        background: "transparent",
        // Grid items size to their content by default, so a long label
        // would stretch the tile rather than being clamped by the rule
        // above.
        minWidth: 0,
        overflow: "hidden",
      }}
    >
      <div
        style={{
          position: "relative",
          width: "100%",
          aspectRatio: "63 / 88",
          background: "var(--surface-2)",
          borderRadius: 4,
          overflow: "hidden",
        }}
      >
        {thumbQuery.data && (
          <img
            src={thumbQuery.data}
            alt={back.label}
            style={{ width: "100%", height: "100%", objectFit: "cover", display: "block" }}
          />
        )}
        <button
          type="button"
          className="thumb-zoom"
          data-tour={selected ? "back-zoom" : undefined}
          title="View full image"
          aria-label="View full image"
          onClick={(e) => {
            // Viewing must not also select: that would switch the
            // project's back as a side effect of looking at one.
            e.stopPropagation();
            onView();
          }}
        >
          <MagnifierIcon />
        </button>
      </div>
      {/* Uploaded filenames run long (and some are near-unreadable
          hashes), so the label is clamped to one ellipsised line with the
          full name in the tooltip — an unclamped name escaped the tile and
          overlapped its neighbours. */}
      <div
        title={back.label}
        style={{
          marginTop: 6,
          fontWeight: selected ? 600 : 400,
          overflow: "hidden",
          textOverflow: "ellipsis",
          whiteSpace: "nowrap",
        }}
      >
        {back.label}
      </div>
      <div className="hint" style={{ fontSize: 12 }}>
        {back.width}×{back.height}
        {back.source_dpi < LOW_DPI && " · low resolution"}
        {back.includes_bleed && " · bleed included"}
        {isDefault && " · default"}
        {cardsHere > 0 && ` · ${cardsHere} card${cardsHere === 1 ? "" : "s"} here`}
      </div>
    </div>
  );
}

export default function BacksPage() {
  const { projectId, cards, settings, setSettings } = useProject();
  const queryClient = useQueryClient();
  const connection = useConnection();
  const readiness = useServerReadiness();
  const serverUnavailable =
    connection.mode === "remote" ? !connection.remoteHealthy : readiness.status !== "ready";
  const serverVersion = useServerVersion();
  // Upscaling from this tab involves no project: the settings are the
  // app-global library ones (libraryUpscale.ts), shared with the Customs
  // tab, and the server files the work under its library tag.
  const libraryUpscaleSupported = serverSupportsLibraryUpscale(serverVersion);
  const modelsQuery = useQuery({ queryKey: ["models"], queryFn: () => generationApi.listModels() });
  const { settings: upscaleSettings, update: updateUpscaleSettings } =
    useLibraryUpscaleSettings(modelsQuery.data);

  const [settingsError, setSettingsError] = useState<string | null>(null);
  const [viewingId, setViewingId] = useState<number | null>(null);
  const [mpcOpen, setMpcOpen] = useState(false);
  const mpcSupported = serverSupportsMpcFill(serverVersion);
  const [pendingDelete, setPendingDelete] = useState<{ back: BackImage; uses: BackImageUsage } | null>(
    null,
  );
  // Drag events fire for every child element entered, so a plain boolean
  // flickers off the moment the pointer crosses a tile. Counting
  // enter/leave pairs is what keeps the highlight steady across the whole
  // drop zone.
  const [dragDepth, setDragDepth] = useState(0);
  const dragging = dragDepth > 0;

  const libraryQuery = useQuery({
    queryKey: ["back-images"],
    queryFn: () => projectApi.listBackImages(),
  });
  const defaultQuery = useQuery({
    queryKey: ["back-image-default"],
    queryFn: () => projectApi.getDefaultBackImageId(),
  });
  const backs = useMemo(() => libraryQuery.data ?? [], [libraryQuery.data]);
  const selected = backs.find((b) => b.id === settings.back_image_id) ?? null;
  const viewing = backs.find((b) => b.id === viewingId) ?? null;
  // Everything ever made for the selected back — polled only while one
  // is selected and the server can answer.
  const { variants } = useLibraryStatus(
    "back",
    selected?.content_hash ?? null,
    !serverUnavailable && libraryUpscaleSupported,
  );
  const upscaleMutation = useMutation({
    mutationFn: (back: BackImage) => upscaleLibraryImage("back", back, upscaleSettings, serverVersion),
    onSuccess: (result, back) => {
      if (result.queued) {
        toast(`Queued ${result.queued} task(s). The Tasks tab shows their progress.`, "ok", {
          key: "library-upscale",
        });
      } else {
        toast(`Nothing to do.${result.notes.length ? " " + result.notes.join(" ") : ""}`, "info", {
          key: "library-upscale",
        });
      }
      void queryClient.invalidateQueries({ queryKey: ["library-status", "back", back.content_hash] });
    },
    onError: (err: Error) => {
      if (err instanceof UploadCanceled) return;
      toast(`Upscale failed: ${err.message}`, "err", { key: "library-upscale" });
    },
  });
  useTourOnFirstView("backs");
  // Per-back settings only exist once this project has a back selected.
  useTourWhenPresent("backs-back", selected != null, "backs");

  const addMutation = useMutation({
    mutationFn: async (file: File) => {
      // readPickedImage owns the type and size checks — a drop bypasses
      // the picker's accept="", so anything at all can land here.
      const picked = await readPickedImage(file);
      return projectApi.addBackImage({
        ...picked,
        originalFilename: file.name,
        // The filename exactly as picked, extension included — it's what
        // the user recognises the file by, and stripping the extension
        // made two files that differ only by format indistinguishable.
        // Renameable afterwards from the sidebar.
        label: file.name,
      });
    },
    onSuccess: (added) => {
      void queryClient.invalidateQueries({ queryKey: ["back-images"] });
      // Selecting what you just added is the only useful next step, and
      // skipping it leaves a library with a new back nothing points at.
      setSettings((s) => ({ ...s, back_image_id: added.id }));
    },
    // Rejections (wrong file type, over the size cap) name the file and why.
    onError: (err) => toast(errorText(err), "err", { key: "back-upload" }),
  });

  const deleteMutation = useMutation({
    mutationFn: async (back: BackImage) => {
      // Best-effort on the server: the library copy is canonical, so a
      // server that's unreachable right now must not block the delete.
      if (!serverUnavailable) {
        try {
          await generationApi.deleteBackImageFromServer(back.content_hash);
        } catch {
          // Leaves a cached original on that host; harmless, and it is
          // reclaimable from this tab once it is reachable again.
        }
      }
      await projectApi.deleteBackImage(back.id);
    },
    onSuccess: () => {
      void queryClient.invalidateQueries({ queryKey: ["back-images"] });
      void queryClient.invalidateQueries({ queryKey: ["back-image-default"] });
      setPendingDelete(null);
      setViewingId(null);
    },
  });

  // The declaration about the file. Only recorded locally: unlike a custom
  // image, a back's bleed travels with every PDF/export request, so there
  // is nothing on the server to bring up to date.
  const bleedMutation = useMutation({
    mutationFn: async (args: BleedChange) => {
      await projectApi.setBackImageBleed(args.id, args.includesBleed, args.bleedMm);
      await queryClient.invalidateQueries({ queryKey: ["back-images"] });
    },
    onSuccess: () => setSettingsError(null),
    onError: (err: unknown) => setSettingsError(err instanceof Error ? err.message : String(err)),
  });

  // The Rust delete nulls this project's pointer in the database, but this
  // session's in-memory settings still hold the old id — clear it here so
  // the PDF tab doesn't keep reporting a back that no longer exists.
  useEffect(() => {
    if (settings.back_image_id != null && libraryQuery.isSuccess && selected == null) {
      setSettings((s) => ({ ...s, back_image_id: null }));
    }
  }, [settings.back_image_id, libraryQuery.isSuccess, selected, setSettings]);

  async function confirmDelete(back: BackImage) {
    const uses = await projectApi.backImageUsage(back.id, projectId);
    setPendingDelete({ back, uses });
  }

  const mpcDisabledReason = serverUnavailable
    ? "Generation server is unreachable"
    : !mpcSupported
      ? `Needs generation server v${MPC_FILL_MIN_SERVER_VERSION} or later`
      : null;

  return (
    <div className="layout">
      <SidebarColumn>
        <LibraryAddPanel
          title="Add Custom Backs"
          accept="image/png,image/jpeg,image/webp"
          dragging={dragging}
          busyLabel={addMutation.isPending ? "Adding…" : null}
          tour="back-dropzone"
          onFiles={(files) => addMutation.mutate(files[0])}
        >
          <button
            type="button"
            className="btn-sm btn-primary btn-block"
            disabled={mpcDisabledReason != null}
            title={mpcDisabledReason ?? "Browse community card backs on MPC Fill"}
            onClick={() => setMpcOpen(true)}
          >
            Search MPC Fill Backs
          </button>
        </LibraryAddPanel>
        <aside className="sidebar panel">
          <h3 style={{ marginBottom: 14 }}>Back image</h3>
          {selected == null ? (
            <p className="hint">
              No back selected for this project. Pick one from the library, or add a new
              one.
            </p>
          ) : (
            <div>
              <BackSettingsFields
                back={selected}
                isDefault={defaultQuery.data === selected.id}
                onBleed={(change) => bleedMutation.mutate(change)}
                error={settingsError}
                tourTargets
              />

              {/* The same app-global upscale settings the Customs tab shows
                  — not this project's, since upscaling a back involves no
                  project (see the header comment). */}
              <h3 style={{ marginTop: 22, marginBottom: 14 }}>Upscale settings</h3>
              <div className="field-group" data-tour="library-upscale-settings">
                <UpscaleSettingsFields
                  value={upscaleSettings}
                  onChange={updateUpscaleSettings}
                  tourPrefix="library-"
                />
                <label className="field">
                  <span>Result</span>
                  <SelectMenu
                    options={[
                      { value: "target", label: "Match target DPI" },
                      { value: "native", label: "Keep full 4× (up to 2400 DPI)" },
                    ]}
                    value={upscaleSettings.mode}
                    onChange={(value) => updateUpscaleSettings({ mode: value as LibraryUpscaleMode })}
                  />
                </label>
                <p className="hint">
                  Only a back below a ticked target is upscaled. Every project that prints
                  with it uses the result; the PDF and ZIP tabs&apos; preferred DPI picks
                  which version.
                </p>
              </div>
              <LibraryUpscaleControls
                sourceDpi={serverSourceDpi("back", selected)}
                variants={variants}
                targets={targetsFor("back", selected, upscaleSettings)}
                disabledReason={
                  !libraryUpscaleSupported
                    ? "Needs a newer generation server."
                    : serverUnavailable
                      ? "The generation server isn't reachable."
                      : null
                }
                pending={upscaleMutation.isPending}
                onUpscale={() => upscaleMutation.mutate(selected)}
              />
              <button
                className="btn-sm btn-danger btn-block"
                style={{ marginTop: 18 }}
                onClick={() => void confirmDelete(selected)}
              >
                Delete this back
              </button>
            </div>
          )}
        </aside>
      </SidebarColumn>

      {/*
        Dropping a file works anywhere on this pane, not only on the
        sidebar's dropzone — the dropzone is the affordance, the whole pane
        is the target. Clicking it opens the picker instead. Both land in
        one code path, because a drop hands us the same `File` the input
        does.

        This only receives events because the main window sets
        `dragDropEnabled: false` (tauri.conf.json). Tauri v2 defaults that
        to true, which makes the native layer consume OS file drops before
        the webview ever sees them — the drop silently does nothing, with
        no error to explain why.
      */}
      <main
        className="content"
        onDragEnter={(e) => {
          e.preventDefault();
          setDragDepth((d) => d + 1);
        }}
        onDragOver={(e) => {
          // Without preventDefault here the browser treats the drop as
          // navigation and opens the image instead.
          e.preventDefault();
        }}
        onDragLeave={() => setDragDepth((d) => Math.max(0, d - 1))}
        onDrop={(e) => {
          e.preventDefault();
          setDragDepth(0);
          const file = e.dataTransfer.files?.[0];
          if (file) addMutation.mutate(file);
        }}
      >
        <h2>Backs</h2>
        <p className="hint" style={{ marginTop: 8 }}>
          Art printed on the reverse of your cards. Your library is shared across every
          project on this machine; turn back printing on from the PDF tab.
        </p>

        {backs.length === 0 ? (
          <p className="hint" style={{ marginTop: 16 }}>
            No backs yet. Add one from the sidebar, or drop an image anywhere here.
          </p>
        ) : (
          <div
            className={dragging ? "library-drop-outline" : undefined}
            style={{
              marginTop: 16,
              display: "grid",
              gridTemplateColumns: "repeat(auto-fill, minmax(168px, 1fr))",
              gap: 12,
            }}
          >
            {backs.map((back) => (
              <BackTile
                key={back.id}
                back={back}
                selected={back.id === settings.back_image_id}
                isDefault={defaultQuery.data === back.id}
                cardsHere={
                  cards.filter((c) => c.back_mode === "image" && c.back_image_id === back.id).length
                }
                onSelect={() => setSettings((s) => ({ ...s, back_image_id: back.id }))}
                onView={() => setViewingId(back.id)}
              />
            ))}
          </div>
        )}
      </main>

      {mpcOpen && (
        <MpcArtModal
          title="MPC Fill — card backs"
          initial={{}}
          cardTypes={["CARDBACK"]}
          pickLabel="Use This Back"
          onClose={() => setMpcOpen(false)}
          onPick={async (card) => {
            const back = await importMpcArt("back", card);
            await queryClient.invalidateQueries({ queryKey: ["back-images"] });
            // Same as an upload: the new back becomes the project's.
            setSettings((s) => ({ ...s, back_image_id: back.id }));
            setMpcOpen(false);
          }}
        />
      )}

      {viewing && (
        <ImageViewer
          title={viewing.label}
          fullQueryKey={["back-full", viewing.id]}
          loadFull={() => projectApi.backImageFull(viewing.id)}
          includesBleed={viewing.includes_bleed}
          bleedMm={viewing.bleed_mm}
          width={viewing.width}
          height={viewing.height}
          sourceDpi={viewing.source_dpi}
          onClose={() => setViewingId(null)}
        >
          <BackSettingsFields
            back={viewing}
            isDefault={defaultQuery.data === viewing.id}
            onBleed={(change) => bleedMutation.mutate(change)}
            error={settingsError}
            tourTargets={false}
          />
        </ImageViewer>
      )}

      {pendingDelete && (
        <ConfirmDialog
          title={`Delete "${pendingDelete.back.label}"?`}
          // The child text says the project count out loud because those
          // projects end up with NO back rather than falling back to the
          // app default — back printing then blocks with a stated reason,
          // which is recoverable, instead of them quietly printing
          // something else.
          confirmLabel="Delete"
          onConfirm={() => deleteMutation.mutate(pendingDelete.back)}
          onCancel={() => setPendingDelete(null)}
        >
          {deleteWarning(pendingDelete.uses)}
        </ConfirmDialog>
      )}
    </div>
  );
}
