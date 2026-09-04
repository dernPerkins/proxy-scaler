// The Custom Image library: upload and manage art used as card *fronts*.
//
// Structurally the Backs tab's twin (app-global, client-owned,
// content-addressed, works with no server reachable), with one difference
// that drives every decision here: a Custom Image is a card. It gets a row
// in the decklist, a quantity, and the full upscale pipeline — so this tab
// manages the library, and "Add to project" is what turns an entry into an
// actual card.
//
// Deleting therefore removes cards, not just a preference. That asymmetry
// with the Backs tab (where deleting merely leaves a project with no back)
// is why the confirmation counts cards rather than projects.
import { useMemo, useRef, useState } from "react";
import { useMutation, useQuery, useQueryClient } from "@tanstack/react-query";
import { projectApi } from "../api/project";
import type { CustomImage } from "../api/types";
import ConfirmDialog from "../components/ConfirmDialog";
import { useProject } from "../context/ProjectContext";
import {
  ACCEPTED_IMAGE_TYPES,
  MAX_UPLOAD_MB,
  addImagesSequentially,
} from "../imageUpload";

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

function CustomTile({
  image,
  inProject,
  onAdd,
  onDelete,
}: {
  image: CustomImage;
  inProject: boolean;
  onAdd: () => void;
  onDelete: () => void;
}) {
  const thumbQuery = useQuery({
    queryKey: ["custom-thumb", image.id],
    queryFn: () => projectApi.customImageThumbnail(image.id),
    staleTime: Infinity,
  });
  const lowRes = image.source_dpi < LOW_DPI;
  return (
    <div className="thumb">
      <div
        style={{
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
      </div>
      <div style={{ marginTop: 6, fontSize: 13, wordBreak: "break-word" }}>{image.label}</div>
      <div className="hint" style={{ fontSize: 12 }}>
        {Math.round(image.source_dpi)} DPI
        {/* A warning, never a block — and unlike a Back Image, there is a
            real remedy beyond finding a better file, so it says so. */}
        {lowRes ? " — low; upscaling will help" : null}
      </div>
      <div style={{ display: "flex", gap: 6, marginTop: 6 }}>
        <button type="button" onClick={onAdd} disabled={inProject}>
          {inProject ? "In project" : "Add to project"}
        </button>
        <button type="button" className="ghost" onClick={onDelete}>
          Remove
        </button>
      </div>
    </div>
  );
}

export default function CustomsPage() {
  const queryClient = useQueryClient();
  const { cards, addCustomCards, reloadCards } = useProject();

  const fileInput = useRef<HTMLInputElement>(null);
  const [uploadError, setUploadError] = useState<string | null>(null);
  const [progress, setProgress] = useState<{ done: number; total: number } | null>(null);
  const [pendingDelete, setPendingDelete] = useState<{
    image: CustomImage;
    uses: number;
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
  const images = useMemo(() => libraryQuery.data ?? [], [libraryQuery.data]);
  const idsInProject = useMemo(
    () => new Set(cards.map((c) => c.custom_image_id).filter((id): id is number => id != null)),
    [cards],
  );

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
      setUploadError(errors.length ? errors.join(" ") : null);
      setProgress(null);
    },
    onError: (err: unknown) => {
      setUploadError(err instanceof Error ? err.message : String(err));
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
    },
  });

  const handleFiles = (list: FileList | null) => {
    const files = Array.from(list ?? []);
    if (files.length) addMutation.mutate(files);
  };

  return (
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
        handleFiles(e.dataTransfer.files);
      }}
    >
      <h2>Custom cards</h2>
      <p className="hint" style={{ marginTop: 8 }}>
        Your own art, used as card fronts. The library is shared across every project on
        this machine; each image becomes a card named after its file. Images stay on this
        machine until something actually needs them — upscaling, or exporting.
      </p>

      {uploadError ? (
        <p className="error" style={{ marginTop: 12 }}>
          {uploadError}
        </p>
      ) : null}

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

        {images.map((image) => (
          <CustomTile
            key={image.id}
            image={image}
            inProject={idsInProject.has(image.id)}
            onAdd={() => void addCustomCards([image.id])}
            onDelete={async () => {
              const uses = await projectApi.countCardsUsingCustomImage(image.id);
              setPendingDelete({ image, uses });
            }}
          />
        ))}
      </div>

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
        </ConfirmDialog>
      ) : null}
    </main>
  );
}
