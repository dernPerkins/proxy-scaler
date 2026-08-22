// Sidebar panel for the generation server's local Scryfall card corpus:
// shows what's imported (with a staleness nudge), offers the dataset choice
// (English-only vs all languages), and starts imports / deletes the database.
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

// Scryfall publishes constantly — Secret Lair drops, convention and
// tournament exclusives — and a card the corpus has never heard of fails
// *silently* in the printing picker rather than announcing itself. So the
// nudge runs on the short side: it is one line of hint text next to an
// Import button, and being a week eager costs nothing.
const STALE_AFTER_DAYS = 7;

// Approximate compressed download sizes, for the choice made before any
// import exists to measure. Static on purpose: reading the real numbers
// meant a live Scryfall catalog fetch on every card-db status poll, and
// that poll sits on the app's launch path (see card_db_status()'s
// docstring in api/routers/cards.py). The import job fetches the true
// size the moment it starts, and the progress modal reports it.
const APPROX_DOWNLOAD_MB: Record<CardDataset, number> = {
  default_cards: 80,
  all_cards: 400,
};

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
    // An import may be running from another window, and one started here
    // finishes without telling us. Cheap now that this endpoint is a local
    // file check, but there is still nothing to gain from polling faster.
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

  // Age of the user's own import, which is the whole staleness signal now.
  // There used to be a second one — "Scryfall has newer card data" — but
  // answering that meant asking Scryfall on every poll, and the answer was
  // very nearly always yes (they republish daily), so it said little that
  // this day count doesn't.
  const staleDays = local
    ? Math.floor((Date.now() - Date.parse(local.imported_at)) / 86_400_000)
    : null;
  const nudge =
    local == null
      ? "No local card database — printings can't be browsed until one is imported."
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
          English only (~{APPROX_DOWNLOAD_MB.default_cards} MB)
        </option>
        <option value="all_cards">
          All languages (~{APPROX_DOWNLOAD_MB.all_cards} MB)
        </option>
      </select>
      {switching && !importRunning && (
        <p className="hint">Switching dataset replaces the current card database.</p>
      )}
      <button
        className="btn-sm"
        onClick={() => startMutation.mutate(dataset)}
        disabled={serverUnavailable || importRunning || startMutation.isPending}
        title={serverUnavailable ? "Generation server is unreachable" : undefined}
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
          (~{APPROX_DOWNLOAD_MB[dataset]} MB download). Your decks and
          generated images are untouched.
        </ConfirmDialog>
      )}

      {/* Start-refused (409) and delete errors; job failures surface in
          CardDbImportModal's error view instead. */}
      {error && <p className="error-text">{error}</p>}
    </div>
  );
}
