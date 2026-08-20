// Sidebar panel for the generation server's local Scryfall card corpus:
// shows what's imported (with a staleness nudge), offers the dataset choice
// (English-only vs all languages, sizes read live from Scryfall's catalog
// via the status endpoint), and drives the background import job with the
// same start-then-poll idiom the PDF render jobs use. Per-server on
// purpose: in Remote mode this reads and imports on the connected machine.
import { useEffect, useState } from "react";
import { useMutation, useQuery, useQueryClient } from "@tanstack/react-query";
import { generationApi } from "../api/generation";
import type { CardDataset } from "../api/types";

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
  const [jobId, setJobId] = useState<string | null>(null);
  const [jobError, setJobError] = useState<string | null>(null);

  const statusQuery = useQuery({
    queryKey: ["card-db-status"],
    queryFn: () => generationApi.cardDbStatus(),
    enabled: !serverUnavailable,
    // The remote half can change daily and an import may be running from
    // another window — a slow refresh keeps the hint honest without chatter.
    refetchInterval: 60_000,
  });
  const status = statusQuery.data;

  // Adopt an import some other window started, so this one shows progress
  // instead of a disabled button with no explanation.
  useEffect(() => {
    if (jobId == null && status?.active_job_id) setJobId(status.active_job_id);
  }, [jobId, status?.active_job_id]);

  // Default the dataset choice to what's already imported.
  useEffect(() => {
    if (status?.local) setDataset(status.local.dataset_type);
  }, [status?.local?.dataset_type]); // eslint-disable-line react-hooks/exhaustive-deps

  const jobQuery = useQuery({
    queryKey: ["card-import-job", jobId],
    queryFn: () => generationApi.cardImportStatus(jobId as string),
    enabled: jobId != null,
    refetchInterval: 1000,
  });
  const job = jobQuery.data;

  // Terminal job state: stop polling and refresh the status line (and the
  // import-language dropdown, whose options may just have changed).
  useEffect(() => {
    if (job && job.status !== "running") {
      if (job.status === "failed") setJobError(job.error ?? "Import failed.");
      setJobId(null);
      queryClient.invalidateQueries({ queryKey: ["card-db-status"] });
      queryClient.invalidateQueries({ queryKey: ["card-languages"] });
    }
  }, [job, queryClient]);

  const startMutation = useMutation({
    mutationFn: (chosen: CardDataset) => generationApi.startCardImport(chosen),
    onSuccess: ({ job_id }) => {
      setJobError(null);
      setJobId(job_id);
      queryClient.invalidateQueries({ queryKey: ["card-db-status"] });
    },
    onError: (err: Error) => setJobError(err.message),
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

  const running = jobId != null && (job == null || job.status === "running");
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
          Not imported yet.
        </p>
      )}
      {nudge && <p className="error-text">{nudge}</p>}

      {!running && (
        <>
          <select
            value={dataset}
            onChange={(e) => setDataset(e.target.value as CardDataset)}
            disabled={serverUnavailable}
          >
            <option value="default_cards">
              English only (~{formatMB(remote?.default_cards?.compressed_size ?? 80_000_000)} MB)
            </option>
            <option value="all_cards">
              All languages (~{formatMB(remote?.all_cards?.compressed_size ?? 400_000_000)} MB)
            </option>
          </select>
          {switching && (
            <p className="hint">
              Switching dataset replaces the current card database.
            </p>
          )}
          <button
            className="btn-sm"
            onClick={() => startMutation.mutate(dataset)}
            disabled={serverUnavailable || startMutation.isPending}
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
        </>
      )}

      {running && (
        <>
          <p className="hint" style={{ margin: 0 }}>
            {job == null || job.phase === "checking"
              ? "Checking Scryfall's catalog…"
              : job.phase === "downloading"
                ? `Downloading ${formatMB(job.bytes_downloaded)}/${formatMB(job.total_bytes)} MB…`
                : job.phase === "importing"
                  ? `Imported ${job.rows_imported.toLocaleString()} cards…`
                  : "Finishing up…"}
          </p>
          <button
            className="btn-sm"
            onClick={() => jobId && generationApi.cancelCardImport(jobId).catch(() => {})}
          >
            Cancel
          </button>
        </>
      )}

      {jobError && <p className="error-text">Import failed: {jobError}</p>}
    </div>
  );
}
