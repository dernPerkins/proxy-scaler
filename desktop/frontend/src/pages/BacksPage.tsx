// The Back Library: upload, pick, and manage the art printed on a card's
// Reverse.
//
// The library is app-global and lives on THIS machine (docs/adr/0003) —
// every project sees every back, and a project points at one by id. The
// generation server only ever holds a synced copy of the bytes, pushed
// lazily when something actually needs to render with them, so this whole
// tab works with no server reachable at all.
//
// Back Images are never upscaled, unlike card art. The low-resolution
// warning below is therefore the only quality signal there is, which is
// why it says what to do about it rather than just noting the number.
//
// Where back printing is configured — the toggle, flip edge, page order,
// offsets, guides — is the PDF tab, because all of those change the sheet
// rather than the image. This tab owns the image and nothing else.
import { useEffect, useMemo, useRef, useState } from "react";
import { useMutation, useQuery, useQueryClient } from "@tanstack/react-query";
import { generationApi } from "../api/generation";
import { projectApi } from "../api/project";
import type { BackImage } from "../api/types";
import ConfirmDialog from "../components/ConfirmDialog";
import { useConnection } from "../connection";
import { useServerReadiness } from "../config";
import { useProject } from "../context/ProjectContext";

// Matches proxy_scaler/backs.py's MIN_COMFORTABLE_DPI and the Rust
// source_dpi calculation — below this, a back is being asked to cover a
// 63×88mm card with less detail than a decent printer resolves.
const LOW_DPI = 300;
const THUMB_MAX_PX = 220;
const MAX_UPLOAD_MB = 50;

/**
 * Decode a picked file, measure it, and render the small preview the Rust
 * side stores alongside it.
 *
 * Done here rather than in Rust deliberately: generating a 220px JPEG
 * natively would mean adding an image-decoding crate to a build that ships
 * in six platform variants, and the webview already has to decode the file
 * to show the user what they picked.
 */
async function readPickedImage(file: File): Promise<{
  bytes: number[];
  thumbnail: number[];
  width: number;
  height: number;
}> {
  const buffer = await file.arrayBuffer();
  let bitmap: ImageBitmap;
  try {
    bitmap = await createImageBitmap(new Blob([buffer], { type: file.type }));
  } catch {
    // A file that claims an image type but isn't one (or is a format this
    // webview can't decode) — say so in the user's terms rather than
    // letting a decoder exception through.
    throw new Error(`${file.name} couldn't be read as an image.`);
  }
  const scale = Math.min(1, THUMB_MAX_PX / Math.max(bitmap.width, bitmap.height));
  const canvas = document.createElement("canvas");
  canvas.width = Math.max(1, Math.round(bitmap.width * scale));
  canvas.height = Math.max(1, Math.round(bitmap.height * scale));
  const ctx = canvas.getContext("2d");
  if (!ctx) throw new Error("Couldn't render a preview for that image.");
  ctx.drawImage(bitmap, 0, 0, canvas.width, canvas.height);
  const blob = await new Promise<Blob | null>((resolve) =>
    canvas.toBlob(resolve, "image/jpeg", 0.85),
  );
  if (!blob) throw new Error("Couldn't render a preview for that image.");
  const thumbBuffer = await blob.arrayBuffer();
  return {
    bytes: Array.from(new Uint8Array(buffer)),
    thumbnail: Array.from(new Uint8Array(thumbBuffer)),
    width: bitmap.width,
    height: bitmap.height,
  };
}

/* Inline rather than an asset or an icon package: it is one drawing used
   in one place, and `currentColor` lets it follow the dropzone's own
   hover/drag state in both themes without a second copy for dark mode. */
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

function BackTile({
  back,
  selected,
  isDefault,
  onSelect,
}: {
  back: BackImage;
  selected: boolean;
  isDefault: boolean;
  onSelect: () => void;
}) {
  const thumbQuery = useQuery({
    queryKey: ["back-thumb", back.id],
    queryFn: () => projectApi.backImageThumbnail(back.id),
    staleTime: Infinity,
  });
  return (
    <button
      type="button"
      onClick={onSelect}
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
        {isDefault && " · default"}
      </div>
    </button>
  );
}

export default function BacksPage() {
  const { settings, setSettings } = useProject();
  const queryClient = useQueryClient();
  const connection = useConnection();
  const readiness = useServerReadiness();
  const serverUnavailable =
    connection.mode === "remote" ? !connection.remoteHealthy : readiness.status !== "ready";

  const fileInput = useRef<HTMLInputElement>(null);
  const [uploadError, setUploadError] = useState<string | null>(null);
  const [pendingDelete, setPendingDelete] = useState<{ back: BackImage; uses: number } | null>(
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

  const addMutation = useMutation({
    mutationFn: async (file: File) => {
      // The file picker filters by accept=""; a drop does not, so anything
      // at all can land here. Without this check a dropped PDF reaches
      // createImageBitmap and surfaces a raw DOMException.
      if (file.type && !file.type.startsWith("image/")) {
        throw new Error(`${file.name} isn't an image — use a PNG, JPEG or WebP.`);
      }
      if (file.size > MAX_UPLOAD_MB * 1024 * 1024) {
        throw new Error(`Back images are limited to ${MAX_UPLOAD_MB}MB.`);
      }
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
      setUploadError(null);
    },
    onError: (err) => setUploadError(err instanceof Error ? err.message : String(err)),
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
    },
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
    const uses = await projectApi.countProjectsUsingBackImage(back.id);
    setPendingDelete({ back, uses });
  }

  return (
    <div className="layout">
      <aside className="sidebar panel">
        <h3 style={{ marginBottom: 14 }}>Back image</h3>
        {selected == null ? (
          <p className="hint">
            No back selected for this project. Pick one from the library, or add a new
            one.
          </p>
        ) : (
          <div className="field-group">
            <label className="field">
              <span>Name</span>
              <input
                defaultValue={selected.label}
                key={selected.id}
                onBlur={(e) => {
                  const next = e.target.value.trim();
                  if (next && next !== selected.label) {
                    void projectApi
                      .setBackImageLabel(selected.id, next)
                      .then(() =>
                        queryClient.invalidateQueries({ queryKey: ["back-images"] }),
                      );
                  }
                }}
              />
            </label>

            {/* The user's declaration about their own file. Art that
                already carries bleed gets fitted to the bled size instead
                of edge-extended — extending it would add a duplicate ~1mm
                border and shrink the visible design. */}
            <label className="check">
              <input
                type="checkbox"
                checked={selected.includes_bleed}
                onChange={(e) => {
                  void projectApi
                    .setBackImageIncludesBleed(selected.id, e.target.checked)
                    .then(() =>
                      queryClient.invalidateQueries({ queryKey: ["back-images"] }),
                    );
                }}
              />
              This image already includes bleed
            </label>

            <label className="check">
              <input
                type="checkbox"
                checked={defaultQuery.data === selected.id}
                onChange={(e) => {
                  void projectApi
                    .setDefaultBackImageId(e.target.checked ? selected.id : null)
                    .then(() =>
                      queryClient.invalidateQueries({ queryKey: ["back-image-default"] }),
                    );
                }}
              />
              Use for new projects
            </label>
            <p className="hint" style={{ marginTop: -4 }}>
              New projects start with this back. Projects you already have keep whatever
              they were set to.
            </p>

            {selected.source_dpi < LOW_DPI && (
              <p className="hint">
                This image works out to about {Math.round(selected.source_dpi)} DPI
                across a card, which will look soft in print. It will still print —
                replace it with a larger source image if you want it sharp.
              </p>
            )}

            <button
              className="btn-sm"
              style={{ marginTop: 18 }}
              onClick={() => void confirmDelete(selected)}
            >
              Delete this back
            </button>
          </div>
        )}
      </aside>

      {/*
        Dropping a file works anywhere on this pane, not only on the
        dropzone — the dropzone is the affordance, the whole pane is the
        target, and dropping slightly wide of it still works. Clicking it
        opens the picker instead. All three land in one code path, because
        a drop hands us the same `File` the input does.

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

        {/* The dropzone is the grid's first tile rather than a control
            above it: card-shaped and card-sized, so the row reads as
            "your backs, and a slot to add another". It is also why the
            empty state needs no separate copy — an empty library is just
            the grid with one tile in it. */}
        {/* No alignItems override: grid items stretch to their row by
            default, which is what makes the dropzone exactly as tall as
            the tiles beside it. A BackTile is a card-shaped image box
            plus two lines of label, so its height can't be guessed from
            an aspect-ratio alone — letting the row decide is the only
            thing that stays right when a label wraps. The dropzone's own
            aspect-ratio still governs when it is the only tile there. */}
        <div
          style={{
            marginTop: 16,
            display: "grid",
            gridTemplateColumns: "repeat(auto-fill, minmax(168px, 1fr))",
            gap: 12,
          }}
        >
          <button
            type="button"
            className={`dropzone${dragging ? " is-dragging" : ""}`}
            onClick={() => fileInput.current?.click()}
            disabled={addMutation.isPending}
          >
            <UploadIcon />
            <span className="dropzone-title">
              {addMutation.isPending ? (
                "Adding…"
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
              up to {MAX_UPLOAD_MB}MB
            </span>
          </button>

          {/* Lives inside the grid so it can never be orphaned from the
              button that clicks it again — it was moved out once, which
              silently made the whole tile do nothing. */}
          <input
            ref={fileInput}
            type="file"
            accept="image/png,image/jpeg,image/webp"
            style={{ display: "none" }}
            onChange={(e) => {
              const file = e.target.files?.[0];
              // Reset first: picking the same file twice in a row fires
              // no change event otherwise, which reads as the control
              // being broken.
              e.target.value = "";
              if (file) addMutation.mutate(file);
            }}
          />

          {backs.map((back) => (
            <BackTile
              key={back.id}
              back={back}
              selected={back.id === settings.back_image_id}
              isDefault={defaultQuery.data === back.id}
              onSelect={() => setSettings((s) => ({ ...s, back_image_id: back.id }))}
            />
          ))}
        </div>

        {/* Rejections (wrong file type, over the size cap) surface here
            rather than inside the tile — the tile is small, and the
            message needs room to say which file and why. */}
        {uploadError && (
          <p className="error-text" style={{ marginTop: 12 }}>
            {uploadError}
          </p>
        )}

        {backs.length === 0 && (
          <p className="hint" style={{ marginTop: 12 }}>
            Whatever you add is shared with every project on this machine, not just
            this one.
          </p>
        )}
      </main>

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
          {pendingDelete.uses > 0
            ? `${pendingDelete.uses} project(s) use this back. They'll be left with no back image, and back printing will ask you to pick one before it prints.`
            : "This removes it from your library and from the connected generation server."}
        </ConfirmDialog>
      )}
    </div>
  );
}
