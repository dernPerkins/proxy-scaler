// The Back Library: upload, pick, and manage the art printed on a card's
// Reverse.
//
// The library is app-global and lives on THIS machine (docs/adr/0003) —
// every project sees every back, and a project points at one by id. The
// upscales do not: those belong to whichever generation server is
// connected, which is why a back can read "original only" on Local and
// "1200 DPI" on a GPU box. That asymmetry is shown rather than hidden.
//
// Where back printing is configured — the toggle, flip edge, page order,
// offsets, guides — is the PDF tab, because all of those change the sheet
// rather than the image. This tab owns the image and nothing else.
import { useEffect, useMemo, useRef, useState } from "react";
import { useMutation, useQueries, useQuery, useQueryClient } from "@tanstack/react-query";
import { generationApi } from "../api/generation";
import { projectApi } from "../api/project";
import type { BackImage } from "../api/types";
import ConfirmDialog from "../components/ConfirmDialog";
import { DEFAULT_GEN_PATHS, DPI_OPTIONS } from "../constants";
import { useConnection } from "../connection";
import { getApiBaseUrl, useServerReadiness } from "../config";
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
  const bitmap = await createImageBitmap(new Blob([buffer], { type: file.type }));
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
      <div style={{ marginTop: 6, fontWeight: selected ? 600 : 400 }}>{back.label}</div>
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

  // Server-side state for every back in the library, annotated on top of
  // the local list — the same local-data-plus-live-status shape
  // mergeCardStatus.ts already uses on the Decklist tab. Absent while
  // disconnected rather than wrong: the library is still fully usable then.
  const statusQueries = useQueries({
    queries: backs.map((back) => ({
      queryKey: ["back-server-status", back.content_hash, getApiBaseUrl()],
      queryFn: () => generationApi.getBackImageStatus(back.content_hash),
      enabled: !serverUnavailable,
    })),
  });
  const selectedIndex = backs.findIndex((b) => b.id === settings.back_image_id);
  const selectedStatus = selectedIndex >= 0 ? statusQueries[selectedIndex]?.data : undefined;

  const addMutation = useMutation({
    mutationFn: async (file: File) => {
      if (file.size > MAX_UPLOAD_MB * 1024 * 1024) {
        throw new Error(`Back images are limited to ${MAX_UPLOAD_MB}MB.`);
      }
      const picked = await readPickedImage(file);
      return projectApi.addBackImage({
        ...picked,
        originalFilename: file.name,
        label: file.name.replace(/\.[^.]+$/, ""),
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

  const upscaleMutation = useMutation({
    mutationFn: (args: { hash: string; dpi: number }) =>
      generationApi.upscaleBackImage(args.hash, {
        model: settings.model,
        dpi_targets: [args.dpi],
        tile_size: settings.tile_size,
        weights_dir: DEFAULT_GEN_PATHS.weights_dir,
      }),
    onSuccess: () => {
      void queryClient.invalidateQueries({ queryKey: ["back-server-status"] });
      void queryClient.invalidateQueries({ queryKey: ["tasks"] });
    },
  });

  const clearUpscalesMutation = useMutation({
    mutationFn: (hash: string) => generationApi.clearBackImageUpscales(hash),
    onSuccess: () => queryClient.invalidateQueries({ queryKey: ["back-server-status"] }),
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
                This image works out to about {Math.round(selected.source_dpi)} DPI across
                a card. It will still print — upscaling it below, or using a larger
                source image, will look sharper.
              </p>
            )}

            <h3 style={{ margin: "18px 0 10px" }}>Upscaling</h3>
            {serverUnavailable ? (
              <p className="hint">
                Connect a generation server to upscale this back. Upscales live on the
                server that made them, not in your library.
              </p>
            ) : (
              <>
                <p className="hint" style={{ marginTop: 0 }}>
                  Using this project&apos;s model ({settings.model}). Upscales belong to
                  the connected server — switching servers means upscaling again there.
                </p>
                <div style={{ display: "flex", gap: 6, flexWrap: "wrap" }}>
                  {DPI_OPTIONS.map((dpi) => {
                    const have = selectedStatus?.variants.some((v) => v.dpi === dpi);
                    return (
                      <button
                        key={dpi}
                        className="btn-sm"
                        disabled={have || upscaleMutation.isPending}
                        onClick={() =>
                          upscaleMutation.mutate({ hash: selected.content_hash, dpi })
                        }
                        title={have ? "Already upscaled at this DPI" : undefined}
                      >
                        {have ? `${dpi} ✓` : `Upscale ${dpi}`}
                      </button>
                    );
                  })}
                </div>
                {upscaleMutation.isError && (
                  <p className="error-text">
                    {upscaleMutation.error instanceof Error
                      ? upscaleMutation.error.message
                      : String(upscaleMutation.error)}
                  </p>
                )}
                {(selectedStatus?.variants.length ?? 0) > 0 && (
                  <button
                    className="btn-sm"
                    style={{ marginTop: 8 }}
                    disabled={clearUpscalesMutation.isPending}
                    onClick={() => clearUpscalesMutation.mutate(selected.content_hash)}
                  >
                    Clear upscales on this server
                  </button>
                )}
              </>
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

      <main className="content">
        <h2>Backs</h2>
        <p className="hint" style={{ marginTop: 8 }}>
          Art printed on the reverse of your cards. Your library is shared across every
          project on this machine; turn back printing on from the PDF tab.
        </p>

        <div className="summary-row" style={{ marginTop: 12 }}>
          <button
            className="btn-primary"
            onClick={() => fileInput.current?.click()}
            disabled={addMutation.isPending}
          >
            {addMutation.isPending ? "Adding…" : "Add a back image"}
          </button>
          <span className="hint">PNG, JPEG or WebP, up to {MAX_UPLOAD_MB}MB.</span>
          <input
            ref={fileInput}
            type="file"
            accept="image/png,image/jpeg,image/webp"
            style={{ display: "none" }}
            onChange={(e) => {
              const file = e.target.files?.[0];
              // Reset first: picking the same file twice in a row fires no
              // change event otherwise, which reads as the button being
              // broken.
              e.target.value = "";
              if (file) addMutation.mutate(file);
            }}
          />
        </div>
        {uploadError && <p className="error-text">{uploadError}</p>}

        {backs.length === 0 ? (
          <p className="hint" style={{ marginTop: 16 }}>
            Nothing here yet. Add an image and it will be available to every project.
          </p>
        ) : (
          <div
            style={{
              marginTop: 16,
              display: "grid",
              gridTemplateColumns: "repeat(auto-fill, minmax(140px, 1fr))",
              gap: 12,
            }}
          >
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
