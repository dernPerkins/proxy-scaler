// Sidebar panel for the generation server's local Scryfall card corpus:
// shows what's imported (with a staleness nudge), offers the dataset choice
// (English-only vs all languages, sizes read live from Scryfall's catalog
// via the status endpoint), and starts imports / deletes the database.
// Per-server on purpose: in Remote mode this reads and imports on the
// connected machine.
//
// This panel only STARTS imports. A running job is watched — and the whole
// app blocked — by CardDbImportModal (mounted in App.tsx): mid-import the
// corpus is only partially populated, so nothing should be querying it.
// The started job id is pushed through cardDbImport.ts so the modal
// appears immediately.
import { useEffect, useState } from "react";
import { useMutation, useQuery, useQueryClient } from "@tanstack/react-query";
import { generationApi } from "../api/generation";
import type { CardDataset } from "../api/types";
import { setCardDbImportJobId } from "../cardDbImport";
import ConfirmDialog from "./ConfirmDialog";

const STALE_AFTER_DAYS = 30;

function formatMB(bytes: number | null | undefined): string {
  if (bytes == null || bytes <= 0) return "?";
  return `${Math.round(bytes / 1_000_000)}`;
}

function formatDate(iso: string): string {
  return iso.slice(0, 10);
}

export default function CardDbPanel(props: { serverUnavailable: boolean }) {
  const { serverUnavailable } = props;
  const queryClient = useQueryClient();
  const [dataset, setDataset] = useState<CardDataset>("default_cards");
  const [error, setError] = useState<string | null>(null);

  const statusQuery = useQuery({
    queryKey: ["card-db-status"],
    queryFn: () => generationApi.cardDbStatus(),
    enabled: !serverUnavailable,
    // The remote half can change daily and an import may be running from
    // another window — a slow refresh keeps the hint honest without chatter.
    refetchInterval: 60_000,
  });
  const status = statusQuery.data;

  // Default the dataset choice to what's already imported.
  useEffect(() => {
    if (status?.local) setDataset(status.local.dataset_type);
  }, [status?.local?.dataset_type]); // eslint-disable-line react-hooks/exhaustive-deps

  const startMutation = useMutation({
    mutationFn: (chosen: CardDataset) => generationApi.startCardImport(chosen),
    onSuccess: ({ job_id }) => {
      setError(null);
      // Hands the job to CardDbImportModal, which takes the screen over.
      setCardDbImportJobId(job_id);
      void queryClient.invalidateQueries({ queryKey: ["card-db-status"] });
    },
    onError: (err: Error) => setError(err.message),
  });

  // ConfirmDialog rather than a checkbox-arm (see ConfirmDialog.tsx) —
  // the corpus is minutes of download/import to get back.
  const [confirmDeleteOpen, setConfirmDeleteOpen] = useState(false);
  const deleteMutation = useMutation({
    mutationFn: () => generationApi.deleteCardDb(),
    onSuccess: () => {
      setError(null);
      void queryClient.invalidateQueries({ queryKey: ["card-db-status"] });
      void queryClient.invalidateQueries({ queryKey: ["card-languages"] });
    },
    onError: (err: Error) => setError(err.message),
  });

  const local = status?.local ?? null;
  const remote = status?.remote ?? null;
  const remoteEntry = remote?.[dataset];

  const staleDays = local
    ? Math.floor((Date.now() - Date.parse(local.imported_at)) / 86_400_000)
    : null;
  const behindRemote =
    local != null &&
    remote?.[local.dataset_type] != null &&
    remote[local.dataset_type]!.updated_at > local.dataset_updated_at;
  const nudge =
    local == null
      ? "No local card database — printings can't be browsed until one is imported."
      : behindRemote
        ? "Scryfall has newer card data than the local copy."
        : staleDays != null && staleDays > STALE_AFTER_DAYS
          ? `Card data last imported ${staleDays} days ago.`
          : null;

  // While a job runs the blocking modal owns the screen, so this state is
  // mostly invisible — it exists so the controls aren't clickable in the
  // instant before the modal mounts.
  const importRunning = status?.import_running ?? false;
  const switching = local != null && local.dataset_type !== dataset;

  return (
    <div className="field divided">
      <span>Card database</span>
      {local ? (
        <p className="hint" style={{ margin: 0 }}>
          {local.dataset_type === "all_cards" ? "All languages" : "English only"} ·{" "}
          {local.card_count.toLocaleString()} cards · updated {formatDate(local.dataset_updated_at)}
        </p>
      ) : (
        <p className="hint" style={{ margin: 0 }}>
          {importRunning ? "Import in progress…" : "Not imported yet."}
        </p>
      )}
      {!importRunning && nudge && <p className="error-text">{nudge}</p>}

      <select
        value={dataset}
        onChange={(e) => setDataset(e.target.value as CardDataset)}
        disabled={serverUnavailable || importRunning}
      >
        <option value="default_cards">
          English only (~{formatMB(remote?.default_cards?.compressed_size ?? 80_000_000)} MB)
        </option>
        <option value="all_cards">
          All languages (~{formatMB(remote?.all_cards?.compressed_size ?? 400_000_000)} MB)
        </option>
      </select>
      {switching && !importRunning && (
        <p className="hint">Switching dataset replaces the current card database.</p>
      )}
      <button
        className="btn-sm"
        onClick={() => startMutation.mutate(dataset)}
        disabled={serverUnavailable || importRunning || startMutation.isPending}
        title={
          serverUnavailable
            ? "Generation server is unreachable"
            : remoteEntry == null
              ? "Scryfall's catalog is unreachable from the server right now — the import will re-check when started."
              : undefined
        }
      >
        {local ? "Update card database" : "Import card database"}
      </button>
      {local && (
        <button
          className="btn-sm btn-danger"
          onClick={() => setConfirmDeleteOpen(true)}
          disabled={serverUnavailable || importRunning || deleteMutation.isPending}
        >
          Delete card database
        </button>
      )}
      {confirmDeleteOpen && (
        <ConfirmDialog
          title="Delete the card database?"
          confirmLabel="Delete card database"
          onConfirm={() => {
            setConfirmDeleteOpen(false);
            deleteMutation.mutate();
          }}
          onCancel={() => setConfirmDeleteOpen(false)}
        >
          This removes the imported Scryfall card data from this server.
          The printing picker stops working until it's imported again
          (~{formatMB(remote?.[dataset]?.compressed_size ?? 80_000_000)} MB
          download). Your decks and generated images are untouched.
        </ConfirmDialog>
      )}

      {/* Start-refused (409) and delete errors; job failures surface in
          CardDbImportModal's error view instead. */}
      {error && <p className="error-text">{error}</p>}
    </div>
  );
}
