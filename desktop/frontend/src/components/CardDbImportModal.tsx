// The blocking progress view for a running card-database import. Mounted
// once in App.tsx: whenever an import job is running — started from the
// sidebar panel, the boot prompt, or another window — this overlay owns
// the screen until the job reaches a terminal state. Deliberately modal:
// mid-import the corpus is only partially populated, so leaving the app
// interactive would let the printing picker (and local-first resolution)
// answer from half a database. Cancel remains available throughout.
//
// This is also the single watcher of import jobs (the starters only push
// a job id — see cardDbImport.ts), which keeps the watch logic in one
// place: adopt only server-confirmed running jobs, never re-adopt one
// already watched to a terminal state, and treat an expired/unknown job
// (404) as over rather than polling it forever. The re-adopt loop this
// prevents froze the sidebar panel before the logic was centralized here.
import { useEffect, useRef, useState, useSyncExternalStore } from "react";
import { useQuery, useQueryClient } from "@tanstack/react-query";
import { generationApi } from "../api/generation";
import {
  getCardDbImportJobId,
  setCardDbImportJobId,
  subscribeCardDbImport,
} from "../cardDbImport";
import { useConnection } from "../connection";
import { useServerReadiness } from "../config";
import ModalOverlay from "./ModalOverlay";

function formatMB(bytes: number | null | undefined): string {
  if (bytes == null || bytes <= 0) return "?";
  return `${Math.round(bytes / 1_000_000)}`;
}

export default function CardDbImportModal() {
  const queryClient = useQueryClient();
  const connection = useConnection();
  const readiness = useServerReadiness();
  const serverUnavailable =
    connection.mode === "remote" ? !connection.remoteHealthy : readiness.status !== "ready";

  const jobId = useSyncExternalStore(subscribeCardDbImport, getCardDbImportJobId);
  // Failed imports keep the overlay up (with the error) until Close —
  // outliving the job id, which clears the moment the job goes terminal.
  const [error, setError] = useState<string | null>(null);
  const finishedJobs = useRef<Set<string>>(new Set());

  // Same query key the sidebar panel uses — shared cache, and this
  // instance's slow poll is what discovers imports started elsewhere.
  const statusQuery = useQuery({
    queryKey: ["card-db-status"],
    queryFn: () => generationApi.cardDbStatus(),
    enabled: !serverUnavailable,
    refetchInterval: 60_000,
  });
  const status = statusQuery.data;

  useEffect(() => {
    if (
      jobId == null &&
      status?.import_running &&
      status.active_job_id &&
      !finishedJobs.current.has(status.active_job_id)
    ) {
      setCardDbImportJobId(status.active_job_id);
    }
  }, [jobId, status?.import_running, status?.active_job_id]);

  const jobQuery = useQuery({
    queryKey: ["card-import-job", jobId],
    queryFn: () => generationApi.cardImportStatus(jobId as string),
    enabled: jobId != null,
    refetchInterval: 1000,
    retry: false,
  });
  const job = jobQuery.data;

  // Terminal state: remember the id, release the overlay (or swap it to
  // the error view), and refresh everything the finished import changed.
  useEffect(() => {
    if (jobId != null && job && job.status !== "running") {
      finishedJobs.current.add(jobId);
      if (job.status === "failed") {
        setError(job.error ?? "Import failed.");
      }
      setCardDbImportJobId(null);
      void queryClient.invalidateQueries({ queryKey: ["card-db-status"] });
      void queryClient.invalidateQueries({ queryKey: ["card-languages"] });
    }
  }, [jobId, job, queryClient]);

  // Unknown/expired job (or the server went away mid-import): nothing
  // left to watch — drop it and re-sync rather than polling a 404 forever.
  useEffect(() => {
    if (jobId != null && jobQuery.isError) {
      finishedJobs.current.add(jobId);
      setCardDbImportJobId(null);
      void queryClient.invalidateQueries({ queryKey: ["card-db-status"] });
    }
  }, [jobId, jobQuery.isError, queryClient]);

  if (jobId == null && error == null) return null;

  if (jobId == null) {
    // Error view — like ConfirmDialog, no overlay-dismiss shortcuts: the
    // single button is the whole choice.
    return (
      <ModalOverlay>
        <div className="modal modal-sm">
          <div className="modal-head">
            <span className="modal-title">Card database import failed</span>
          </div>
          <p className="error-text" style={{ marginBottom: 14 }}>
            {error}
          </p>
          <div className="modal-actions">
            <button className="btn-primary" autoFocus onClick={() => setError(null)}>
              Close
            </button>
          </div>
        </div>
      </ModalOverlay>
    );
  }

  const phaseLine =
    job == null || job.phase === "checking"
      ? "Checking Scryfall's catalog…"
      : job.phase === "downloading"
        ? `Downloading ${formatMB(job.bytes_downloaded)} / ${formatMB(job.total_bytes)} MB…`
        : job.phase === "importing"
          ? `Imported ${job.rows_imported.toLocaleString()} cards…`
          : "Finishing up…";

  return (
    <ModalOverlay>
      <div className="modal modal-sm">
        <div className="modal-head">
          <span className="modal-title">
            Importing card database
            {job ? ` (${job.dataset === "all_cards" ? "all languages" : "English only"})` : ""}
          </span>
        </div>
        <p style={{ marginBottom: 8 }}>{phaseLine}</p>
        <p className="hint" style={{ marginBottom: 14 }}>
          The app is paused while the card database updates — a half-imported
          database would answer printing lookups incompletely.
        </p>
        <div className="modal-actions">
          <button
            className="btn-danger"
            onClick={() => void generationApi.cancelCardImport(jobId).catch(() => {})}
          >
            Cancel import
          </button>
        </div>
      </div>
    </ModalOverlay>
  );
}
