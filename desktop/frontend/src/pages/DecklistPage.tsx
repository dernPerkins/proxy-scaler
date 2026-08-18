import { useEffect, useState } from "react";
import { useMutation, useQuery, useQueryClient } from "@tanstack/react-query";
import { generationApi } from "../api/generation";
import type { CardRow } from "../api/project";
import type { DeckEntryIn, GalleryItem, ModelOption, Task } from "../api/types";
import CompareDialog from "../components/CompareDialog";
import NumberInput from "../components/NumberInput";
import ServerSwitcher from "../components/ServerSwitcher";
import StatusBadge from "../components/StatusBadge";
import { DPI_OPTIONS, modelDisplayName } from "../constants";
import { useConnection } from "../connection";
import { useServerReadiness } from "../config";
import { invokeOpenDirectory, invokeOpenRemoteTerminal, isTauri } from "../tauri";
import { useProject } from "../context/ProjectContext";
import { runDownload, useDownloadStatus } from "../download";
import {
  cardIdentity,
  groupByCard,
  groupByCardName,
  buildRows,
  statusForPairs,
  type VariantStatus,
} from "../mergeCardStatus";

// Purely presentational device hints appended to the "Upscale model"
// dropdown labels — the API's own flat list (proxy_scaler/upscale.py's
// UpscaleModel enum order) is unchanged, and so is whatever
// settings.model already holds.
const MODEL_LABEL_SUFFIXES: Record<string, string> = {
  ultrasharp_v2: " (best on GPU)",
  realesrgan_anime_fast: " (best on CPU)",
};

function annotateModelOptions(models: ModelOption[]): ModelOption[] {
  return models.map((m) => ({
    value: m.value,
    label: m.label + (MODEL_LABEL_SUFFIXES[m.value] ?? ""),
  }));
}

// Generation-machine-local directory names, fixed and not
// user-configurable: every generate/regenerate/clear request sends these
// same relative names, and the server resolves them against its own cwd.
// misc.py's /api/paths mirrors them and reports where they actually land
// (the sidebar shows that, read-only). Not in project.settings either —
// a path valid on one machine means nothing against a Remote host. See
// ARCHITECTURE.md.
const DEFAULT_GEN_PATHS = {
  output_dir: "output",
  cache_dir: "imgcache",
  weights_dir: "weights",
};

function cardToEntry(card: CardRow): DeckEntryIn {
  return {
    quantity: card.quantity ?? 1,
    name: card.name,
    set_code: card.set_code,
    collector_number: card.collector_number,
    raw_line: card.original_import_line,
  };
}

// A CardRow has no scryfall_id (the client never calls Scryfall — see
// ARCHITECTURE.md), so its identity falls back to its own name when
// there's no exact set/collector, same as the gallery/task side's
// fallback in mergeCardStatus.ts.
function localCardIdentity(card: CardRow): string {
  return cardIdentity(card.set_code, card.collector_number, null, card.name);
}

interface DisplayFace {
  faceLabel: string | null;
  variants: VariantStatus[];
}

export default function DecklistPage() {
  const queryClient = useQueryClient();
  const {
    projectId,
    projectTag,
    settings,
    setSettings,
    cards,
    importDecklistText,
    importingDecklistText,
    removeCard,
    setCardQuantity,
  } = useProject();
  const readiness = useServerReadiness();
  const connection = useConnection();
  // Whether the generation server is reachable right now — remote mode
  // has its own 30s heartbeat (connection.remoteHealthy); local mode's
  // equivalent is "has the sidecar finished starting". Generate/PDF
  // requests against an unreachable server used to just hang or fail
  // silently with no feedback; this both disables the buttons and (via
  // the mutation onError handlers below) surfaces a real error if one
  // slips through anyway.
  const serverUnavailable =
    connection.mode === "remote" ? !connection.remoteHealthy : readiness.status !== "ready";

  // Always read this from the API, never hardcode — see
  // api/generation.ts's listModels comment for the regression this
  // replaced.
  const modelsQuery = useQuery({ queryKey: ["models"], queryFn: () => generationApi.listModels() });

  // Purely an entry box — never seeded from the project's stored last
  // paste (the card rows below are the actual deck; decklistText lives on
  // only as Save As's copy source in ProjectContext). Cleared when a
  // different project loads so text typed against one deck can't be
  // imported into another, and cleared again after a successful import.
  const [decklistDraft, setDecklistDraft] = useState("");
  useEffect(() => setDecklistDraft(""), [projectId]);
  const [status, setStatus] = useState<string | null>(null);
  const [sortPrimary, setSortPrimary] = useState<"Name" | "Set" | "(none)">("Name");
  const [expandedFaces, setExpandedFaces] = useState<Set<string>>(new Set());

  // Where the fixed generation directories resolve on the connected
  // server's machine — display-only. Invalidated on server switch in
  // connection.tsx, since the answer is per-server.
  const pathsQuery = useQuery({
    queryKey: ["gen-paths"],
    queryFn: () => generationApi.getPaths(),
  });

  // Local card data (decklist text -> CardRow[]) is invoke-based and only
  // changes on an explicit mutation — no polling needed, it can't go
  // stale behind the user's back. Generation status (tasks + gallery) is
  // the half that's actually live, so it keeps the old 3s poll.
  const statusQuery = useQuery({
    queryKey: ["generation-status", projectTag],
    queryFn: async () => {
      const [tasks, gallery] = await Promise.all([
        generationApi.listTasks({ project_tag: projectTag as string }),
        generationApi.listGallery(projectTag as string),
      ]);
      return { tasks, gallery };
    },
    enabled: projectTag != null,
    refetchInterval: 3000,
  });

  function invalidateStatus() {
    queryClient.invalidateQueries({ queryKey: ["generation-status", projectTag] });
  }

  // Reconcile the gallery with the server's disk on import/load (and
  // again once the server comes up). Adoption: images generated under
  // another project exist on disk but this project has no rows, so the
  // deck would sit at "Not generated yet" until a Generate request
  // happens to skip_existing its way to them. Pruning: the reverse lie —
  // output files are shared and tag-less, so another project clearing
  // them leaves this project's rows asserting green badges for images
  // that 404. Keyed on the identity set, not `cards` itself, so quantity
  // edits don't re-fire it; best-effort because a failure only means the
  // status poll shows a stale view until the next reconcile.
  const cardIdentities = cards.map(localCardIdentity).sort().join("|");
  useEffect(() => {
    if (!projectTag || serverUnavailable || cards.length === 0) return;
    let cancelled = false;
    generationApi
      .adoptGallery(projectTag, cards.map(cardToEntry), DEFAULT_GEN_PATHS.output_dir)
      .then(({ adopted, pruned }) => {
        if (!cancelled && (adopted > 0 || pruned > 0)) {
          queryClient.invalidateQueries({ queryKey: ["generation-status", projectTag] });
        }
      })
      .catch(() => {});
    return () => {
      cancelled = true;
    };
    // eslint-disable-next-line react-hooks/exhaustive-deps
  }, [projectTag, cardIdentities, serverUnavailable]);

  const generateAllMutation = useMutation({
    mutationFn: () =>
      generationApi.generate({
        project_tag: projectTag as string,
        entries: cards.map(cardToEntry),
        model: settings.model,
        dpi_targets: settings.dpi_targets,
        skip_existing: settings.skip_existing,
        tile_size: settings.tile_size,
        output_dir: DEFAULT_GEN_PATHS.output_dir,
        cache_dir: DEFAULT_GEN_PATHS.cache_dir,
        weights_dir: DEFAULT_GEN_PATHS.weights_dir,
      }),
    onSuccess: (result) => {
      setStatus(
        result.queued
          ? `Queued ${result.queued} task(s) — see the Tasks tab to monitor progress.`
          : `Nothing to do — every requested image already exists.${result.notes.length ? " " + result.notes.join(" ") : ""}`,
      );
      invalidateStatus();
    },
    onError: (err: Error) => setStatus(`Generate failed: ${err.message}`),
  });

  const generateCardMutation = useMutation({
    mutationFn: (card: CardRow) =>
      generationApi.generate({
        project_tag: projectTag as string,
        entries: [cardToEntry(card)],
        model: settings.model,
        dpi_targets: settings.dpi_targets,
        skip_existing: settings.skip_existing,
        tile_size: settings.tile_size,
        output_dir: DEFAULT_GEN_PATHS.output_dir,
        cache_dir: DEFAULT_GEN_PATHS.cache_dir,
        weights_dir: DEFAULT_GEN_PATHS.weights_dir,
      }),
    onSuccess: invalidateStatus,
    onError: (err: Error) => setStatus(`Generate failed: ${err.message}`),
  });

  const regenerateMutation = useMutation({
    mutationFn: (galleryItemId: number) =>
      generationApi.regenerateGalleryItem(galleryItemId, {
        tile_size: settings.tile_size,
        output_dir: DEFAULT_GEN_PATHS.output_dir,
        cache_dir: DEFAULT_GEN_PATHS.cache_dir,
        weights_dir: DEFAULT_GEN_PATHS.weights_dir,
      }),
    onSuccess: invalidateStatus,
    onError: (err: Error) => setStatus(`Regenerate failed: ${err.message}`),
  });

  const [confirmClearGenerated, setConfirmClearGenerated] = useState(false);
  const clearGeneratedMutation = useMutation({
    mutationFn: () =>
      generationApi.clearGeneratedData(
        DEFAULT_GEN_PATHS.output_dir,
        DEFAULT_GEN_PATHS.cache_dir,
        projectTag ?? undefined,
      ),
    onSuccess: () => {
      setConfirmClearGenerated(false);
      invalidateStatus();
    },
    onError: (err: Error) => setStatus(`Clear failed: ${err.message}`),
  });

  function openDirectory(label: string, path: string) {
    // Local: OS file manager. Remote: a terminal window ssh'd into the
    // server and cd'd to the directory — deliberately not an sftp:// URL
    // handed to the OS, whose scheme handler is a lottery (VLC commonly
    // claims sftp:// on Linux and "opens" the directory as a playlist).
    const opening =
      connection.mode === "remote"
        ? invokeOpenRemoteTerminal(connection.host, path)
        : invokeOpenDirectory(path);
    opening.catch((err: unknown) => {
      const msg = err instanceof Error ? err.message : String(err);
      const hint =
        connection.mode === "remote"
          ? " — copy the path and ssh in manually instead."
          : "";
      setStatus(`Couldn't open ${label.toLowerCase()}: ${msg}${hint}`);
    });
  }

  // Button for one generation directory — opens it (file manager
  // locally, an SSH terminal for a remote server). The resolved absolute
  // path lives in the tooltip rather than the button face; disabled
  // until the server has reported it. Plain text in a browser dev tab,
  // where there's no Tauri command to invoke.
  function dirRow(label: string, path: string | undefined) {
    if (!isTauri()) {
      return (
        <div className="field">
          <span>{label}</span>
          <span className="path-text mono">{path ?? "…"}</span>
        </div>
      );
    }
    const title =
      path == null
        ? "Waiting for the server…"
        : connection.mode === "remote"
          ? `${path} — opens an SSH terminal on the server`
          : path;
    return (
      <button
        type="button"
        className="path-link"
        disabled={path == null}
        title={title}
        onClick={() => path != null && openDirectory(label, path)}
      >
        <span>{label}</span>
        <span className="path-open-icon" aria-hidden="true">
          ↗
        </span>
      </button>
    );
  }

  function toggleDpi(dpi: number) {
    setSettings((s) => ({
      ...s,
      dpi_targets: s.dpi_targets.includes(dpi)
        ? s.dpi_targets.filter((d) => d !== dpi)
        : [...s.dpi_targets, dpi].sort((a, b) => a - b),
    }));
  }

  function toggleExpanded(key: string) {
    setExpandedFaces((prev) => {
      const next = new Set(prev);
      if (next.has(key)) next.delete(key);
      else next.add(key);
      return next;
    });
  }

  const tasks: Task[] = statusQuery.data?.tasks ?? [];
  const gallery: GalleryItem[] = statusQuery.data?.gallery ?? [];
  const { galleryByCard, tasksByCard } = groupByCard(gallery, tasks);
  const { galleryByName, tasksByName } = groupByCardName(gallery, tasks);

  const sortedCards = sortCards(cards, sortPrimary);

  return (
    <div className="layout">
      <aside className="sidebar panel">
        <h3 style={{ marginBottom: 14 }}>Settings</h3>

        {/* Above the field group, and never behind a project check:
            switching to an empty server leaves projectId null, and if this
            were gated on a project there'd be no way to switch back. */}
        <ServerSwitcher />

        <div className="field-group">
          <label className="field">
            <span>Upscale model</span>
            <select
              value={settings.model}
              onChange={(e) => setSettings((s) => ({ ...s, model: e.target.value }))}
              disabled={modelsQuery.isLoading || modelsQuery.isError}
            >
              {annotateModelOptions(modelsQuery.data ?? []).map((m) => (
                <option key={m.value} value={m.value}>
                  {m.label}
                </option>
              ))}
            </select>
          </label>
          {/* Without this, a stuck/failed local-server start (or any other
              listModels() failure) rendered as a silently empty dropdown —
              indistinguishable from "there really are no models" — since
              the .map() above just produces zero <option>s either way. */}
          {modelsQuery.isLoading && (
            <p className="hint">
              {readiness.status === "starting"
                ? "Waiting for the local server to start…"
                : "Loading models…"}
            </p>
          )}
          {modelsQuery.isError && (
            <p className="error-text">
              Couldn't load models:{" "}
              {modelsQuery.error instanceof Error
                ? modelsQuery.error.message
                : String(modelsQuery.error)}
            </p>
          )}

          <div className="field">
            <span>Target DPI</span>
            <div className="check-row">
              {DPI_OPTIONS.map((dpi) => (
                <label key={dpi} className="check">
                  <input
                    type="checkbox"
                    checked={settings.dpi_targets.includes(dpi)}
                    onChange={() => toggleDpi(dpi)}
                  />
                  {dpi}
                </label>
              ))}
            </div>
          </div>

          <label className="check">
            <input
              type="checkbox"
              checked={settings.skip_existing}
              onChange={(e) => setSettings((s) => ({ ...s, skip_existing: e.target.checked }))}
            />
            Skip existing output files
          </label>

          <label className="field">
            <span>Tile size (0 = auto)</span>
            <input
              type="number"
              min={0}
              step={32}
              value={settings.tile_size}
              onChange={(e) => setSettings((s) => ({ ...s, tile_size: Number(e.target.value) }))}
            />
          </label>

          {dirRow("Output Directory", pathsQuery.data?.output_dir)}
          {dirRow("Cache Directory", pathsQuery.data?.cache_dir)}
          {dirRow("Weights Directory", pathsQuery.data?.weights_dir)}
        </div>

        <div className="danger-zone">
          <h3>Danger zone</h3>
          <p className="hint">
            Deletes the generated images and download cache (the output and cache directories
            above). Model weights are kept.
          </p>
          <label className="check">
            <input
              type="checkbox"
              checked={confirmClearGenerated}
              onChange={(e) => setConfirmClearGenerated(e.target.checked)}
            />
            Confirm delete generated data
          </label>
          <button
            className="btn-danger btn-block"
            onClick={() => clearGeneratedMutation.mutate()}
            disabled={!confirmClearGenerated || clearGeneratedMutation.isPending}
          >
            Delete all generated images &amp; cache
          </button>
          {clearGeneratedMutation.data && (
            <div className="hint">
              {clearGeneratedMutation.data.notes.length > 0 ? (
                clearGeneratedMutation.data.notes.map((note, i) => <div key={i}>{note}</div>)
              ) : (
                <div>Generated data cleared.</div>
              )}
            </div>
          )}
        </div>
      </aside>

      <main className="content">
        <h2>Decklist</h2>

        {/* The import box is unconditional: with no project yet, importing
            is what creates one (see ProjectContext's importDecklistText).
            Gating it on projectId would be circular — the row is born at
            first import, so the box that performs the import can't wait for
            the row. The readiness note that follows is about the local
            generation server, which importing doesn't touch — so it sits
            beside the box rather than in place of it. */}
        {readiness.status === "starting" && (
          <p className="hint" style={{ marginTop: 10 }}>
            Starting local server — your last project will load automatically.
          </p>
        )}

        <div className="import-box panel" style={{ marginTop: 10 }}>
          <p className="hint">
            One card per line — best format:{" "}
            <code>4 Card Name (set) 123</code>. Set and collector number are
            optional; <code>4x</code> works too.
          </p>
          <textarea
            value={decklistDraft}
            onChange={(e) => setDecklistDraft(e.target.value)}
            rows={6}
            style={{ width: "100%" }}
            placeholder={"1 Sol Ring (c21) 263\n4 Lightning Bolt"}
          />
          <button
            className="btn-primary"
            onClick={() => {
              setStatus(null);
              importDecklistText(decklistDraft)
                .then(() => setDecklistDraft(""))
                // A failed import keeps the text for a retry; the error
                // itself is surfaced via the context's error state.
                .catch(() => {});
            }}
            disabled={!decklistDraft.trim() || importingDecklistText}
          >
            {importingDecklistText ? "Importing…" : "Import cards"}
          </button>
          {status && <p className="hint">{status}</p>}
        </div>

        <div className="decklist-head">
          <h2>
            Cards <span style={{ color: "var(--text-faint)" }}>({cards.length})</span>
          </h2>
          <div className="decklist-actions">
            <select
              value={sortPrimary}
              onChange={(e) => setSortPrimary(e.target.value as typeof sortPrimary)}
            >
              <option value="Name">Sort: Name</option>
              <option value="Set">Sort: Set</option>
              <option value="(none)">Sort: (none)</option>
            </select>
            <button
              className="btn-primary"
              onClick={() => generateAllMutation.mutate()}
              disabled={!cards.length || generateAllMutation.isPending || serverUnavailable}
              title={serverUnavailable ? "Generation server is unreachable" : undefined}
            >
              Generate upscaled images
            </button>
          </div>
        </div>

        {serverUnavailable && (
          <p className="error-text" style={{ marginTop: 6 }}>
            Generation server is unreachable — reconnect before generating.
          </p>
        )}

        {cards.length === 0 && (
          <p className="empty-note">
            No cards yet — paste a decklist above and click Import cards.
          </p>
        )}

        {sortedCards.map((card) => {
          const identity = localCardIdentity(card);
          let cardGallery = galleryByCard.get(identity) ?? [];
          let cardTasks = tasksByCard.get(identity) ?? [];
          if (!card.set_code || !card.collector_number) {
            // Name-only local card: the generation server always
            // resolves it to one concrete printing (a real set +
            // collector_number), so it can never land in the
            // identity bucket above — match by name instead. See
            // mergeCardStatus.ts::groupByCardName.
            const nameKey = card.name.toLowerCase();
            cardGallery = [...cardGallery, ...(galleryByName.get(nameKey) ?? [])];
            cardTasks = [...cardTasks, ...(tasksByName.get(nameKey) ?? [])];
          }
          const faceGroups = buildRows(cardGallery, cardTasks);
          const faces: DisplayFace[] = faceGroups
            .map(({ items, tasks: faceTasks }) => {
              const source = items[0] ?? faceTasks[0];
              return {
                faceLabel: source?.face_label ?? null,
                // Canceled tasks are just queue history, not worth
                // surfacing here — the Tasks tab is where cancellation
                // state actually matters.
                variants: statusForPairs(items, faceTasks).filter(
                  (v) => v.status !== "canceled",
                ),
              };
            })
            .filter((face) => face.variants.length > 0);
          return (
            <CardRowView
              key={card.id}
              card={card}
              faces={faces}
              expandedFaces={expandedFaces}
              onToggleExpand={toggleExpanded}
              onRemove={() => removeCard(card.id)}
              onSetQuantity={(quantity) => setCardQuantity(card.id, quantity)}
              onGenerate={() => generateCardMutation.mutate(card)}
              onRegenerate={(galleryItemId) => regenerateMutation.mutate(galleryItemId)}
              disabled={serverUnavailable}
            />
          );
        })}
      </main>
    </div>
  );
}

function sortCards(cards: CardRow[], primary: "Name" | "Set" | "(none)"): CardRow[] {
  if (primary === "(none)") return cards;
  const key = (c: CardRow) =>
    (primary === "Name" ? c.name : (c.set_code ?? "")).toLowerCase();
  return [...cards].sort((a, b) => key(a).localeCompare(key(b)));
}

function slugify(name: string): string {
  return name.replace(/[^A-Za-z0-9._-]+/g, "_").replace(/^_+|_+$/g, "") || "card";
}

async function handleDownloadImage(url: string, filename: string): Promise<void> {
  await runDownload(filename, { url });
}

interface CompareTarget {
  originalUrl: string;
  upscaledUrl: string;
  label: string;
}

function CardRowView(props: {
  card: CardRow;
  faces: DisplayFace[];
  expandedFaces: Set<string>;
  onToggleExpand: (key: string) => void;
  onRemove: () => void;
  onSetQuantity: (quantity: number) => void;
  onGenerate: () => void;
  onRegenerate: (galleryItemId: number) => void;
  /** True when the generation server is unreachable — disables
   *  Generate/Regen (Remove/Show stay enabled since those are purely
   *  local). */
  disabled: boolean;
}) {
  const { card, faces, expandedFaces, onToggleExpand, onRemove, onSetQuantity, onGenerate, onRegenerate, disabled } = props;
  const quantity = card.quantity ?? 1;
  const rowKey = `card-${card.id}`;
  const expanded = expandedFaces.has(rowKey);
  const hasImages = faces.some((f) => f.variants.some((v) => v.status === "done"));
  const [compareTarget, setCompareTarget] = useState<CompareTarget | null>(null);
  const [downloadError, setDownloadError] = useState<string | null>(null);
  // Global single-flight lock (see download.ts::runDownload) -- disables
  // every Download button on the page the instant any one download
  // starts, not just once its file is ready. Without this, a slow fetch
  // gave zero feedback and a second click just launched another
  // unprotected download that looked like the first one "did nothing."
  const downloadStatus = useDownloadStatus();

  async function download(url: string, filename: string) {
    setDownloadError(null);
    try {
      await handleDownloadImage(url, filename);
    } catch (err) {
      setDownloadError(err instanceof Error ? err.message : String(err));
    }
  }

  return (
    <div className="card-row">
      <div className="card-main">
        <span className="card-name">{card.name}</span>
        <span className="card-meta mono">{(card.set_code ?? "—").toUpperCase()}</span>
        <span className="card-meta mono">{card.collector_number ?? "—"}</span>
        <span className="card-qty-stepper">
          <button
            className="btn-sm"
            aria-label="Decrease quantity"
            disabled={quantity <= 1}
            onClick={() => onSetQuantity(quantity - 1)}
          >
            −
          </button>
          <NumberInput
            value={quantity}
            min={1}
            onChange={(v) => onSetQuantity(Math.max(1, Math.round(v)))}
          />
          <button
            className="btn-sm"
            aria-label="Increase quantity"
            onClick={() => onSetQuantity(quantity + 1)}
          >
            +
          </button>
        </span>
        <span className="card-buttons">
          {hasImages && (
            <button className="btn-sm" onClick={() => onToggleExpand(rowKey)}>
              {expanded ? "Hide" : "Show"}
            </button>
          )}
          <button className="btn-sm" onClick={onGenerate} disabled={disabled}>
            Generate
          </button>
          <button className="btn-sm btn-danger" onClick={onRemove}>
            Remove
          </button>
        </span>
      </div>

      {faces.length === 0 ? (
        <div className="empty-note">Not generated yet.</div>
      ) : (
        faces.map((face, i) => (
          <div key={i} className="variants">
            {face.faceLabel && <span className="variant-face">{face.faceLabel}</span>}
            {face.variants.map((v) => (
              <StatusBadge key={`${v.dpi}-${v.model}`} status={v.status}>
                {v.dpi} · {modelDisplayName(v.model)}
              </StatusBadge>
            ))}
          </div>
        ))
      )}

      {expanded &&
        faces.map((face, i) => {
          const doneVariants = face.variants.filter(
            (v): v is VariantStatus & { galleryItemId: number } =>
              v.status === "done" && v.galleryItemId != null,
          );
          // The original (pre-upscale) source image is shared across
          // every DPI/model variant of this face — keyed server-side by
          // scryfall_id/face_index only (see original_cache_path in
          // upscale.py), not by model/dpi — so any one "done" variant's
          // gallery_item_id resolves to the same underlying file via
          // GET /api/gallery/{id}/original. Shown once per face, not
          // once per variant.
          const originalSource = doneVariants[0];
          return (
            <div key={i} className="thumbs">
              {originalSource && (
                <div className="thumb">
                  <div className="thumb-label">Original</div>
                  <img
                    src={generationApi.imageUrl(originalSource.galleryItemId, "original")}
                    alt={card.name}
                    loading="lazy"
                  />
                  <div className="thumb-buttons">
                    <button
                      className="btn-sm"
                      disabled={downloadStatus != null}
                      onClick={() =>
                        download(
                          generationApi.imageUrl(originalSource.galleryItemId, "original"),
                          `${slugify(card.name)}-original.png`,
                        )
                      }
                    >
                      {downloadStatus ? "Downloading…" : "Download"}
                    </button>
                  </div>
                </div>
              )}
              {doneVariants.map((v) => (
                <div key={`${v.dpi}-${v.model}`} className="thumb">
                  <div className="thumb-label" title={`${v.dpi} DPI · ${v.model}`}>
                    {v.dpi} DPI · {modelDisplayName(v.model)}
                  </div>
                  <img
                    src={generationApi.imageUrl(v.galleryItemId, "full")}
                    alt={card.name}
                    loading="lazy"
                  />
                  <div className="thumb-buttons">
                    <button
                      className="btn-sm"
                      disabled={downloadStatus != null}
                      onClick={() =>
                        download(
                          generationApi.imageUrl(v.galleryItemId, "full"),
                          `${slugify(card.name)}-${v.dpi}dpi-${v.model}.png`,
                        )
                      }
                    >
                      {downloadStatus ? "Downloading…" : "Download"}
                    </button>
                    <button
                      className="btn-sm"
                      onClick={() =>
                        setCompareTarget({
                          originalUrl: generationApi.imageUrl(v.galleryItemId, "original"),
                          upscaledUrl: generationApi.imageUrl(v.galleryItemId, "full"),
                          label: `${card.name} — ${v.dpi} DPI · ${modelDisplayName(v.model)}`,
                        })
                      }
                    >
                      Compare
                    </button>
                    <button
                      className="btn-sm"
                      onClick={() => onRegenerate(v.galleryItemId)}
                      disabled={disabled}
                    >
                      Regen
                    </button>
                  </div>
                </div>
              ))}
            </div>
          );
        })}

      {downloadError && (
        <p className="error-text">
          Download failed: {downloadError}
        </p>
      )}

      {compareTarget && (
        <CompareDialog
          originalUrl={compareTarget.originalUrl}
          upscaledUrl={compareTarget.upscaledUrl}
          label={compareTarget.label}
          onClose={() => setCompareTarget(null)}
        />
      )}
    </div>
  );
}
