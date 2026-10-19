// The Custom Image library: upload and manage art used as card *fronts*.
// Two tabs show it: Customs (the user's own uploads) and MPC Fill (art
// downloaded from MPC Fill, the rows with MPC provenance). One library,
// one component (CustomLibraryPage), so tiles, settings, upscaling and
// delete behave the same on both; MpcFillPage.tsx is the second view.
//
// Structurally the Backs tab's twin (app-global, client-owned,
// content-addressed, works with no server reachable), with one difference
// that drives every decision here: a Custom Image is a card. It gets a row
// in the decklist, a quantity, and the upscale pipeline (when the project's
// "Custom images" setting on the Decklist tab asks for it) — so this tab
// manages the library, and "Add to project" is what turns an entry into an
// actual card.
//
// Deleting therefore removes cards, not just a preference. That asymmetry
// with the Backs tab (where deleting merely leaves a project with no back)
// is why the confirmation counts cards rather than projects.
//
// Like the Backs tab, per-image settings live in a sidebar for the
// selected tile: the name, the declaration that the file already carries
// bleed (and how much), and the Remove button. Selection is local to this
// page — unlike a back, a custom image is not something a project
// "selects", so there is no project setting to key it off.
//
// The "+" on a tile opens the full image with the trim line drawn over it
// from the same declaration, because a 168px thumbnail cannot show whether
// a file already carries bleed — and that is exactly the question the
// checkbox asks. The viewer carries the same settings so the answer can
// be given while looking at the evidence. It is components/ImageViewer,
// shared with the Backs tab.
import { useMemo, useRef, useState } from "react";
import { useMutation, useQuery, useQueryClient } from "@tanstack/react-query";
import { generationApi } from "../api/generation";
import { projectApi } from "../api/project";
import type { CustomImage } from "../api/types";
import ConfirmDialog from "../components/ConfirmDialog";
import SelectMenu from "../components/SelectMenu";
import ImageViewer, { MAX_BLEED_MM, MagnifierIcon } from "../components/ImageViewer";
import LibraryUpscaleControls from "../components/LibraryUpscaleControls";
import UpscaleSettingsFields from "../components/UpscaleSettingsFields";
import {
  MPC_FILL_MIN_SERVER_VERSION,
  serverSupportsLibraryUpscale,
  serverSupportsMpcFill,
  useServerReadiness,
  useServerVersion,
} from "../config";
import { MpcArtModal } from "../components/MpcArtPicker";
import { addMpcArtToDeck, importMpcArtInBackground, usePendingMpcImports } from "../mpcLibrary";
import StatusBadge from "../components/StatusBadge";
import { useConnection } from "../connection";
import { useProject } from "../context/ProjectContext";
import {
  targetsFor,
  upscaleLibraryImage,
  useLibraryStatus,
  useLibraryUpscaleSettings,
  type LibraryUpscaleMode,
} from "../libraryUpscale";
import { UploadCanceled } from "../uploadProgress";
import { errorText, toast } from "../toasts";
import { blurOnEnter } from "../inputKeys";
import { useTourOnFirstView, useTourWhenPresent } from "../tutorial/tutorialStore";
import { customImageRefs, getProjectSnapshot, registerCustomCards } from "../syncCustoms";
import {
  ACCEPTED_IMAGE_TYPES,
  MAX_UPLOAD_MB,
  addImagesSequentially,
} from "../imageUpload";

// The tile selected when this page was last on screen. Routes unmount
// their page on every tab switch, so plain component state would put the
// user back on the "Select an image" hint each time they came back from
// Tasks to check on an upscale — the Backs tab keeps its selection
// because there it is a project setting; here it is only ever UI state,
// and this is where it waits. Module-level rather than persisted: the
// library is app-global, so it is valid across projects, but a fresh
// launch starts with nothing selected like every other tab. An id whose
// image has since been deleted resolves to nothing selected through the
// same lookup as a stale live id.
const lastSelectedId: Record<LibraryView, number | null> = { uploads: null, mpc: null };

/** Which half of the library a page shows. */
export type LibraryView = "uploads" | "mpc";

// Matches proxy_scaler/customs.py's MIN_COMFORTABLE_DPI and the Rust
// source_dpi calculation.
const LOW_DPI = 300;

function UploadIcon() {
  return (
    <svg
      className="dropzone-icon"
      width="56"
      height="56"
      viewBox="0 0 24 24"
      fill="none"
      stroke="currentColor"
      strokeWidth="1.5"
      strokeLinecap="round"
      strokeLinejoin="round"
      aria-hidden="true"
    >
      <path d="M20.4 14.5A4 4 0 0 0 18 7.5h-1.3A6 6 0 1 0 6 12.9" />
      <path d="M12 12v9" />
      <path d="m8.5 15.5 3.5-3.5 3.5 3.5" />
    </svg>
  );
}

type BleedChange = { id: number; includesBleed: boolean; bleedMm: number };

/** The per-image settings, shared by the sidebar and the viewer so the two
 *  can never disagree about what a custom image can be told. */
function CustomSettingsFields({
  image,
  onBleed,
  error,
}: {
  image: CustomImage;
  onBleed: (change: BleedChange) => void;
  error: string | null;
}) {
  const queryClient = useQueryClient();
  return (
    <div className="field-group">
      {image.mpc && (
        <p className="mpc-provenance">
          From MPC Fill · {image.mpc.source || "unknown uploader"}
          {image.mpc.name && image.mpc.name !== image.label ? ` · ${image.mpc.name}` : ""}
        </p>
      )}
      <label className="field">
        <span>Name</span>
        <input
          defaultValue={image.label}
          key={image.id}
          onKeyDown={blurOnEnter}
          onBlur={(e) => {
            const next = e.target.value.trim();
            if (next && next !== image.label) {
              void projectApi
                .setCustomImageLabel(image.id, next)
                .then(() => queryClient.invalidateQueries({ queryKey: ["custom-images"] }));
            }
          }}
        />
      </label>

      {/* The user's declaration about their own file. An image that
          already carries bleed (an MPC Fill download, say) is cropped and
          upscaled to its bled size on the server, and the print trims
          that bleed to the project's — rather than cropping the file to
          card size and extending a second border around what was already
          bleed. */}
      <label className="check">
        <input
          type="checkbox"
          checked={image.includes_bleed}
          onChange={(e) =>
            onBleed({ id: image.id, includesBleed: e.target.checked, bleedMm: image.bleed_mm })
          }
        />
        This image already includes bleed
      </label>
      {image.includes_bleed && (
        <label className="field">
          <span>Bleed in the file (mm per side)</span>
          <input
            type="number"
            min={0}
            max={MAX_BLEED_MM}
            step={0.001}
            key={`bleed-${image.id}`}
            defaultValue={image.bleed_mm}
            onKeyDown={blurOnEnter}
            onBlur={(e) => {
              const next = Number(e.target.value);
              if (
                Number.isFinite(next) &&
                next >= 0 &&
                next <= MAX_BLEED_MM &&
                next !== image.bleed_mm
              ) {
                onBleed({ id: image.id, includesBleed: true, bleedMm: next });
              }
            }}
          />
        </label>
      )}
      <p className="hint" style={{ marginTop: -4 }}>
        MakePlayingCards images carry 3.175 mm (1/8 in) per side. Printing trims this down
        to the project&apos;s bleed, or extends it if the project asks for more. Changing it
        discards any upscales of this image on the server; generate again afterwards.
      </p>
      {error ? <p className="error-text">{error}</p> : null}

      {image.source_dpi < LOW_DPI && (
        <p className="hint">
          This image works out to about {Math.round(image.source_dpi)} DPI across a card,
          which will look soft in print. Upscale it below, or replace it with a larger
          source image.
        </p>
      )}
    </div>
  );
}

function CustomTile({
  image,
  inProject,
  selected,
  adding,
  onSelect,
  onAdd,
  onView,
}: {
  image: CustomImage;
  inProject: boolean;
  selected: boolean;
  adding: boolean;
  onSelect: () => void;
  onAdd: () => void;
  onView: () => void;
}) {
  const thumbQuery = useQuery({
    queryKey: ["custom-thumb", image.id],
    queryFn: () => projectApi.customImageThumbnail(image.id),
    staleTime: Infinity,
  });
  const lowRes = image.source_dpi < LOW_DPI;
  return (
    <div
      className="thumb"
      role="button"
      tabIndex={0}
      data-tour={selected ? "custom-tile" : undefined}
      onClick={onSelect}
      onKeyDown={(e) => {
        if (e.key === "Enter" || e.key === " ") {
          e.preventDefault();
          onSelect();
        }
      }}
      style={{
        padding: 6,
        borderRadius: 10,
        borderColor: selected ? "var(--accent)" : "transparent",
        borderWidth: 2,
        borderStyle: "solid",
        cursor: "pointer",
      }}
    >
      <div
        style={{
          position: "relative",
          aspectRatio: "63 / 88",
          borderRadius: 8,
          border: "1px solid var(--border)",
          background: "var(--surface-2)",
          overflow: "hidden",
        }}
      >
        {thumbQuery.data ? (
          <img
            src={thumbQuery.data}
            alt={image.label}
            style={{ width: "100%", height: "100%", objectFit: "cover", display: "block" }}
          />
        ) : null}
        <button
          type="button"
          className="thumb-zoom"
          data-tour={selected ? "custom-zoom" : undefined}
          title="View full image"
          aria-label="View full image"
          onClick={(e) => {
            e.stopPropagation();
            onView();
          }}
        >
          <MagnifierIcon />
        </button>
      </div>
      <div style={{ marginTop: 6, fontSize: 13, wordBreak: "break-word" }}>{image.label}</div>
      {image.mpc && (
        <div className="hint" style={{ fontSize: 12 }}>
          {image.mpc.source || "Unknown uploader"}
        </div>
      )}
      <div className="hint" style={{ fontSize: 12 }}>
        {Math.round(image.source_dpi)} DPI
        {/* A warning, never a block: a sharper file or an upscale are
            both real remedies. */}
        {lowRes ? " — low for print" : null}
        {image.includes_bleed ? " · bleed included" : null}
      </div>
      <div style={{ display: "flex", gap: 6, marginTop: 6 }}>
        <button
          type="button"
          data-tour={selected ? "custom-add" : undefined}
          onClick={(e) => {
            // The tile itself selects; the button must not also toggle
            // the sidebar to some other image mid-click.
            e.stopPropagation();
            onAdd();
          }}
          disabled={inProject || adding}
        >
          {inProject ? "In Project" : adding ? "Adding…" : "Add to Project"}
        </button>
      </div>
    </div>
  );
}

export default function CustomsPage() {
  return <CustomLibraryPage view="uploads" />;
}

export function CustomLibraryPage({ view }: { view: LibraryView }) {
  const queryClient = useQueryClient();
  const { cards, settings: projectSettings, addCustomCards, addCardWithFaceArt, reloadCards } =
    useProject();
  const serverVersion = useServerVersion();
  const connection = useConnection();
  const readiness = useServerReadiness();
  const serverUnavailable =
    connection.mode === "remote" ? !connection.remoteHealthy : readiness.status !== "ready";
  // Upscaling from this tab involves no project: the settings are the
  // app-global library ones (libraryUpscale.ts), and the server files the
  // work under its library tag. Needs a server with /api/library.
  const libraryUpscaleSupported = serverSupportsLibraryUpscale(serverVersion);
  const modelsQuery = useQuery({ queryKey: ["models"], queryFn: () => generationApi.listModels() });
  const { settings: upscaleSettings, update: updateUpscaleSettings } =
    useLibraryUpscaleSettings(modelsQuery.data);

  const fileInput = useRef<HTMLInputElement>(null);
  const [settingsError, setSettingsError] = useState<string | null>(null);
  const [progress, setProgress] = useState<{ done: number; total: number } | null>(null);
  const [selectedId, setSelectedIdState] = useState<number | null>(lastSelectedId[view]);
  function setSelectedId(id: number | null) {
    lastSelectedId[view] = id;
    setSelectedIdState(id);
  }
  const [addingId, setAddingId] = useState<number | null>(null);
  // The full-size viewer deliberately does not survive a tab switch: it is
  // a modal over the page, not part of where the user was.
  const [viewingId, setViewingId] = useState<number | null>(null);
  const [mpcOpen, setMpcOpen] = useState(false);
  const mpcSupported = serverSupportsMpcFill(serverVersion);
  const [pendingDelete, setPendingDelete] = useState<{
    image: CustomImage;
    uses: number;
    /** Scryfall cards printing it on a face; they keep their card. */
    faceUses: number;
  } | null>(null);
  // A counter, not a boolean: drag events fire for every child element
  // entered, so a plain flag flickers off the moment the pointer crosses a
  // tile. See BacksPage, which learned this the same way.
  const [dragDepth, setDragDepth] = useState(0);
  const dragging = dragDepth > 0;

  const libraryQuery = useQuery({
    queryKey: ["custom-images"],
    queryFn: () => projectApi.listCustomImages(),
  });
  // The shared query holds the whole library (the Decklist links MPC picks
  // against it); each tab shows its half.
  const images = useMemo(
    () => (libraryQuery.data ?? []).filter((i) => (i.mpc != null) === (view === "mpc")),
    [libraryQuery.data, view],
  );
  // Picks still downloading, minus any whose entry has already landed.
  const pendingImports = usePendingMpcImports().filter(
    (p) => view === "mpc" && !images.some((i) => i.mpc?.drive_id === p.driveId),
  );
  const idsInProject = useMemo(
    () => new Set(cards.flatMap((c) => customImageRefs(c).map((ref) => ref.id))),
    [cards],
  );
  const selected = images.find((i) => i.id === selectedId) ?? null;
  const viewing = images.find((i) => i.id === viewingId) ?? null;
  // Everything ever made for the selected image, whichever project or
  // this tab made it — polled only while something is selected.
  const { variants } = useLibraryStatus(
    "custom",
    selected?.content_hash ?? null,
    !serverUnavailable && libraryUpscaleSupported,
  );
  const upscaleMutation = useMutation({
    mutationFn: (image: CustomImage) =>
      upscaleLibraryImage("custom", image, upscaleSettings, serverVersion),
    onSuccess: (result, image) => {
      if (result.queued) {
        toast(`Queued ${result.queued} task(s). The Tasks tab shows their progress.`, "ok", {
          key: "library-upscale",
        });
      } else {
        toast(`Nothing to do.${result.notes.length ? " " + result.notes.join(" ") : ""}`, "info", {
          key: "library-upscale",
        });
      }
      void queryClient.invalidateQueries({ queryKey: ["library-status", "custom", image.content_hash] });
    },
    onError: (err: Error) => {
      // Cancelling the upload is a normal outcome, not a failure.
      if (err instanceof UploadCanceled) return;
      toast(`Upscale failed: ${err.message}`, "err", { key: "library-upscale" });
    },
  });
  const baseTour = view === "mpc" ? "mpc" : "customs";
  useTourOnFirstView(baseTour);
  // Per-card settings only exist once a card is selected.
  useTourWhenPresent("customs-card", selected != null, baseTour);

  /** Into the current deck: an upload as a custom card; MPC art as its
   *  Scryfall card with the art on its face (see mpcLibrary.ts). */
  async function addToProject(image: CustomImage) {
    setAddingId(image.id);
    try {
      const added =
        view === "mpc"
          ? await addMpcArtToDeck(image, {
              lang: projectSettings.lang_any ? null : projectSettings.preferred_lang,
              addCardWithFaceArt,
              addCustomCards,
            })
          : { ...(await addCustomCards([image.id])), asCustom: true };
      if (view === "mpc" && added.asCustom) {
        toast(
          `No card is named "${image.mpc?.name || image.label}", so it was added as a custom card.`,
          "warn",
        );
      }
      // "Added it, it's ready to print" — same best-effort sync +
      // register as the Decklist dropzone. Quietly a no-op with no server
      // reachable; the PDF/ZIP export paths re-run it then.
      await registerCustomCards(added.cards, added.projectTag, serverVersion)
        .then(() =>
          queryClient.invalidateQueries({ queryKey: ["generation-status", added.projectTag] }),
        )
        .catch(() => {});
    } catch (err) {
      toast(`Couldn't add it to the project: ${errorText(err)}`, "err");
    } finally {
      setAddingId(null);
    }
  }

  const addMutation = useMutation({
    mutationFn: async (files: File[]) => {
      setProgress({ done: 0, total: files.length });
      return addImagesSequentially(
        files,
        (file, picked) =>
          projectApi.addCustomImage({ ...picked, originalFilename: file.name }),
        (done, total) => setProgress({ done, total }),
      );
    },
    onSuccess: ({ errors }) => {
      void queryClient.invalidateQueries({ queryKey: ["custom-images"] });
      if (errors.length) toast(errors.join(" "), "err", { key: "custom-upload" });
      setProgress(null);
    },
    onError: (err: unknown) => {
      toast(errorText(err), "err", { key: "custom-upload" });
      setProgress(null);
    },
  });

  const deleteMutation = useMutation({
    mutationFn: (id: number) => projectApi.deleteCustomImage(id),
    onSuccess: async () => {
      await queryClient.invalidateQueries({ queryKey: ["custom-images"] });
      // The delete also removed any project cards using it, so the open
      // project's card list is now stale.
      await reloadCards();
      setPendingDelete(null);
      setSelectedId(null);
      setViewingId(null);
    },
  });

  // The declaration about the file. Recorded locally, then pushed to the
  // connected server right away for any card already using the image:
  // the sync re-uploads under the new amount and the server discards the
  // upscales it made under the old one, so the next PDF is right rather
  // than the one after. Best-effort — with no server reachable the
  // PDF/ZIP paths re-run the same registration later.
  const bleedMutation = useMutation({
    mutationFn: async (args: BleedChange) => {
      await projectApi.setCustomImageBleed(args.id, args.includesBleed, args.bleedMm);
      await queryClient.invalidateQueries({ queryKey: ["custom-images"] });
      const { projectTag } = getProjectSnapshot();
      const using = cards.filter((c) =>
        customImageRefs(c).some((ref) => ref.id === args.id),
      );
      if (using.length && projectTag != null) {
        try {
          await registerCustomCards(using, projectTag, serverVersion);
          await queryClient.invalidateQueries({ queryKey: ["generation-status", projectTag] });
        } catch {
          // Deferred to the next generate or export, which runs the same
          // sync and reports any real problem then.
        }
      }
    },
    onSuccess: () => setSettingsError(null),
    onError: (err: unknown) => setSettingsError(err instanceof Error ? err.message : String(err)),
  });

  const handleFiles = (list: FileList | null) => {
    const files = Array.from(list ?? []);
    if (files.length) addMutation.mutate(files);
  };

  async function confirmDelete(image: CustomImage) {
    const [uses, faceUses] = await Promise.all([
      projectApi.countCardsUsingCustomImage(image.id),
      projectApi.countFaceArtUsingCustomImage(image.id),
    ]);
    setPendingDelete({ image, uses, faceUses });
  }

  return (
    <div className="layout">
      <aside className="sidebar panel">
        <h3 style={{ marginBottom: 14 }}>{view === "mpc" ? "MPC Fill Art" : "Custom card"}</h3>
        {selected == null ? (
          <p className="hint">
            Select an image to upscale it, rename it, say whether it already includes bleed,
            or remove it from the library. The + on a tile opens the full image with the
            trim line drawn on it.
          </p>
        ) : (
          <div data-tour="custom-settings">
            <CustomSettingsFields
              image={selected}
              onBleed={(change) => bleedMutation.mutate(change)}
              error={settingsError}
            />

            {/* App-global, not the project's Decklist settings: this tab
                upscales library images whether or not any project uses
                them, and the project's "Custom images" rule is about what
                its own bulk Generate does. */}
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
                Only an image below a ticked target is upscaled. One too large for this GPU
                is skipped with a message and prints as uploaded.
              </p>
            </div>
            <LibraryUpscaleControls
              sourceDpi={selected.source_dpi}
              variants={variants}
              targets={targetsFor("custom", selected, upscaleSettings)}
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
              Remove this image
            </button>
          </div>
        )}
      </aside>

      <main
        className="content"
        onDragEnter={(e) => {
          if (view !== "uploads") return;
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
          if (view === "uploads") handleFiles(e.dataTransfer.files);
        }}
      >
        {view === "mpc" ? (
          <>
            <h2>MPC Fill</h2>
            <p className="hint" style={{ marginTop: 8 }}>
              Community art downloaded from MPC Fill, shared across every project on this
              machine. Add to Project puts the art on its card in the current deck, the same
              as picking it in the change-printing picker.
            </p>
          </>
        ) : (
          <>
            <h2>Custom cards</h2>
            <p className="hint" style={{ marginTop: 8 }}>
              Your own art, used as card fronts. The library is shared across every project
              on this machine; each image becomes a card named after its file. Images stay on
              this machine until something actually needs them — upscaling, or exporting.
            </p>
          </>
        )}

        <div
          style={{
            marginTop: 16,
            display: "grid",
            gridTemplateColumns: "repeat(auto-fill, minmax(168px, 1fr))",
            gap: 12,
          }}
        >
          {view === "uploads" ? (
            <>
              <button
                type="button"
                className={`dropzone${dragging ? " is-dragging" : ""}`}
                data-tour="custom-dropzone"
                onClick={() => fileInput.current?.click()}
                disabled={addMutation.isPending}
              >
                <UploadIcon />
                <span className="dropzone-title">
                  {addMutation.isPending && progress ? (
                    `Adding ${progress.done + 1} of ${progress.total}…`
                  ) : (
                    <>
                      Drag and drop
                      <br />
                      or
                      <br />
                      click here
                    </>
                  )}
                </span>
                <span className="dropzone-hint">
                  PNG, JPEG or WebP
                  <br />
                  up to {MAX_UPLOAD_MB}MB each
                </span>
              </button>

              {/* Inside the grid so it can never be orphaned from the button
                  that clicks it — moving it out once silently made the whole
                  tile do nothing (see BacksPage). */}
              <input
                ref={fileInput}
                type="file"
                multiple
                accept={ACCEPTED_IMAGE_TYPES}
                style={{ display: "none" }}
                onChange={(e) => {
                  const files = e.target.files;
                  // Reset first: picking the same file twice in a row fires no
                  // change event otherwise, which reads as a broken control.
                  const copy = files ? Array.from(files) : [];
                  e.target.value = "";
                  if (copy.length) addMutation.mutate(copy);
                }}
              />
            </>
          ) : (
            <button
              type="button"
              className="dropzone mpc-library-tile"
              data-tour="mpc-search"
              disabled={serverUnavailable || !mpcSupported}
              title={
                serverUnavailable
                  ? "Generation server is unreachable"
                  : !mpcSupported
                    ? `Needs generation server v${MPC_FILL_MIN_SERVER_VERSION} or later`
                    : "Search community art on MPC Fill"
              }
              onClick={() => setMpcOpen(true)}
            >
              <span className="dropzone-title">Search MPC Fill</span>
              <span className="dropzone-hint">
                Search community art
                <br />
                cards and tokens
              </span>
            </button>
          )}

          {pendingImports.map((p) => (
            <div key={p.driveId} className="thumb" style={{ padding: 6 }} aria-busy="true">
              <div
                className="loading-shimmer"
                style={{ aspectRatio: "63 / 88", borderRadius: 8, border: "1px solid var(--border)" }}
              />
              <div style={{ marginTop: 6, fontSize: 13, wordBreak: "break-word" }}>{p.name}</div>
              <div className="hint" style={{ fontSize: 12 }}>
                {p.source || "Unknown uploader"}
              </div>
              <div style={{ marginTop: 6 }}>
                <StatusBadge status="running">Downloading · MPC</StatusBadge>
              </div>
            </div>
          ))}

          {images.map((image) => (
            <CustomTile
              key={image.id}
              image={image}
              inProject={idsInProject.has(image.id)}
              selected={image.id === selectedId}
              adding={addingId === image.id}
              onSelect={() => setSelectedId(image.id)}
              onView={() => {
                setSelectedId(image.id);
                setViewingId(image.id);
              }}
              onAdd={() => void addToProject(image)}
            />
          ))}
        </div>
      </main>

      {mpcOpen ? (
        <MpcArtModal
          title="MPC Fill — card fronts"
          initial={{}}
          cardTypes={["CARD", "TOKEN"]}
          pickLabel="Add to Library"
          onClose={() => setMpcOpen(false)}
          onPick={async (card) => {
            // Closes at once; the art downloads behind a placeholder tile.
            setMpcOpen(false);
            importMpcArtInBackground(card, queryClient, (image) => setSelectedId(image.id));
          }}
        />
      ) : null}

      {viewing ? (
        <ImageViewer
          title={viewing.label}
          fullQueryKey={["custom-full", viewing.id]}
          loadFull={() => projectApi.customImageFull(viewing.id)}
          includesBleed={viewing.includes_bleed}
          bleedMm={viewing.bleed_mm}
          width={viewing.width}
          height={viewing.height}
          sourceDpi={viewing.source_dpi}
          onClose={() => setViewingId(null)}
        >
          <CustomSettingsFields
            image={viewing}
            onBleed={(change) => bleedMutation.mutate(change)}
            error={settingsError}
          />
        </ImageViewer>
      ) : null}

      {pendingDelete ? (
        <ConfirmDialog
          title={`Remove "${pendingDelete.image.label}"?`}
          confirmLabel="Remove"
          onConfirm={() => deleteMutation.mutate(pendingDelete.image.id)}
          onCancel={() => setPendingDelete(null)}
        >
          {pendingDelete.uses > 0
            ? `This image is used by ${pendingDelete.uses} card(s), which will be removed too.`
            : "This removes the image from your library on this machine."}
          {pendingDelete.faceUses > 0
            ? ` ${pendingDelete.faceUses} card(s) printing it on a face go back to their own art.`
            : ""}
        </ConfirmDialog>
      ) : null}
    </div>
  );
}
