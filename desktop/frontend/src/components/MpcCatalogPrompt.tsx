// The launch-time MPC Fill catalog offer, and the single watcher of
// catalog update jobs (mpcCatalog.ts holds the job id; MpcCatalogPanel
// starts jobs too and reads the same job query).
//
// Last link in the boot-dialog chain, after CardDbPrompt (see update.ts):
// renders nothing until that has settled with no dialog up. Then, at most
// once a day per server, it asks the server whether a newer catalog is
// published; when one is (and the user hasn't skipped that version), it
// asks Download or Skip. Download keeps the dialog up with progress until
// the update lands or the user sends it to the background — the update
// never blocks the app, since searches read the old catalog until the
// new one commits. Skip remembers the version, so only a newer one asks
// again. The sidebar panel's Update button is always there regardless.
import { useEffect, useRef, useState, useSyncExternalStore } from "react";
import { useMutation, useQuery, useQueryClient } from "@tanstack/react-query";
import { generationApi } from "../api/generation";
import type { MpcCatalogCheck } from "../api/types";
import { getCardDbImportJobId, subscribeCardDbImport } from "../cardDbImport";
import { serverSupportsMpcFill, useServerReadiness, useServerVersion } from "../config";
import { useConnection } from "../connection";
import {
  CHECK_INTERVAL_MS,
  formatMb,
  loadPromptState,
  refreshAfterCatalogUpdate,
  serverKey,
  setMpcCatalogJobId,
  updatePromptState,
  useMpcCatalogJobId,
} from "../mpcCatalog";
import { isTauri } from "../tauri";
import {
  getCardDbPromptOpen,
  getCardDbSettled,
  setMpcCatalogPromptOpen,
  setMpcCatalogSettled,
  subscribeUpdateStore,
} from "../update";
import ModalOverlay from "./ModalOverlay";

function formatMB(bytes: number | null | undefined, total: number | null | undefined): string {
  if (bytes == null || bytes < 0 || !total) return "?";
  // Catalog downloads are small, so a decimal place keeps the count moving.
  return total < 10_000_000 ? (bytes / 1_000_000).toFixed(1) : `${Math.round(bytes / 1_000_000)}`;
}

/** The progress line, worded like CardDbImportModal's. */
export function catalogPhaseLine(
  job: { phase: string; bytes_downloaded: number; total_bytes: number | null; rows_imported: number } | undefined,
): string {
  if (job == null || job.phase === "checking") return "Checking the catalog…";
  if (job.phase === "downloading") {
    return `Downloading ${formatMB(job.bytes_downloaded, job.total_bytes)} / ${formatMB(job.total_bytes, job.total_bytes)} MB…`;
  }
  return `Imported ${job.rows_imported.toLocaleString()} images…`;
}

/** Polls the watched job; finishes it (refresh, clear) exactly once. */
function useCatalogJobWatcher(serverUnavailable: boolean, supported: boolean) {
  const queryClient = useQueryClient();
  const jobId = useMpcCatalogJobId();
  const finished = useRef<Set<string>>(new Set());

  const statusQuery = useQuery({
    queryKey: ["mpc-catalog-status"],
    queryFn: () => generationApi.mpcCatalogStatus(),
    enabled: !serverUnavailable && supported,
    refetchInterval: 60_000,
    retry: false,
  });
  const status = statusQuery.data;

  // Adopt an update another window (or the panel before a remount) started.
  useEffect(() => {
    const active = status?.active_job_id;
    if (jobId == null && status?.import_running && active && !finished.current.has(active)) {
      setMpcCatalogJobId(active);
    }
  }, [jobId, status?.import_running, status?.active_job_id]);

  const jobQuery = useQuery({
    queryKey: ["mpc-catalog-job", jobId],
    queryFn: () => generationApi.mpcCatalogImportStatus(jobId as string),
    enabled: jobId != null,
    refetchInterval: (query) => (query.state.data?.status === "running" || !query.state.data ? 500 : false),
    retry: false,
  });
  const job = jobQuery.data;

  useEffect(() => {
    if (jobId == null || finished.current.has(jobId)) return;
    if (jobQuery.isError || (job && job.status !== "running")) {
      finished.current.add(jobId);
      refreshAfterCatalogUpdate(queryClient);
      // A failure stays watched, so the panel (and an open dialog) can say
      // what went wrong; a new update replaces it.
      if (jobQuery.isError || job?.status !== "failed") setMpcCatalogJobId(null);
    }
  }, [jobId, job, jobQuery.isError, queryClient]);

  return { status, statusQuery, jobId, job };
}

export default function MpcCatalogPrompt() {
  const queryClient = useQueryClient();
  const connection = useConnection();
  const readiness = useServerReadiness();
  const serverVersion = useServerVersion();
  const supported = serverSupportsMpcFill(serverVersion);
  const serverUnavailable =
    connection.mode === "remote" ? !connection.remoteHealthy : readiness.status !== "ready";
  const { status, statusQuery, jobId, job } = useCatalogJobWatcher(serverUnavailable, supported);

  const cardDbSettled = useSyncExternalStore(subscribeUpdateStore, getCardDbSettled);
  const cardDbOpen = useSyncExternalStore(subscribeUpdateStore, getCardDbPromptOpen);
  const cardDbImporting = useSyncExternalStore(subscribeCardDbImport, getCardDbImportJobId) != null;
  const chainClear = cardDbSettled && !cardDbOpen && !cardDbImporting;
  const key = serverKey(connection.mode, connection.host, connection.port);

  // "answered": Skip / background / closed this launch. "watching": the
  // user chose Download here, so the dialog shows the job's progress.
  const [answered, setAnswered] = useState(false);
  const [watching, setWatching] = useState(false);
  const [offer, setOffer] = useState<MpcCatalogCheck | null>(null);
  const [nothingToOffer, setNothingToOffer] = useState(false);
  const [error, setError] = useState<string | null>(null);

  const promptStateQuery = useQuery({
    queryKey: ["mpc-catalog-prompt-state"],
    queryFn: loadPromptState,
    enabled: isTauri(),
    staleTime: Infinity,
  });
  const remembered = promptStateQuery.data?.[key];
  const due =
    remembered?.checkedAt == null || Date.now() - remembered.checkedAt > CHECK_INTERVAL_MS;

  const canCheck =
    isTauri() &&
    supported &&
    !serverUnavailable &&
    chainClear &&
    promptStateQuery.isSuccess &&
    status != null &&
    !status.import_running &&
    jobId == null &&
    due &&
    !answered;

  const checkQuery = useQuery({
    queryKey: ["mpc-catalog-check", key],
    queryFn: () => generationApi.checkMpcCatalog(),
    enabled: canCheck && offer == null && !nothingToOffer,
    staleTime: Infinity,
    retry: false,
  });

  useEffect(() => {
    const check = checkQuery.data;
    if (!check || offer != null || nothingToOffer) return;
    if (check.update_available && check.remote_version !== remembered?.skippedVersion) {
      setOffer(check);
    } else {
      // Nothing new: don't ask the server again for a day.
      setNothingToOffer(true);
      void updatePromptState(key, { checkedAt: Date.now() }).then(() =>
        queryClient.invalidateQueries({ queryKey: ["mpc-catalog-prompt-state"] }),
      );
    }
  }, [checkQuery.data, offer, nothingToOffer, remembered?.skippedVersion, key, queryClient]);

  const startMutation = useMutation({
    mutationFn: () => generationApi.startMpcCatalogImport(),
    onSuccess: ({ job_id }) => {
      setError(null);
      setWatching(true);
      setMpcCatalogJobId(job_id);
    },
    onError: (err: Error) => setError(err.message),
  });

  const remember = (change: { skippedVersion?: string }) => {
    void updatePromptState(key, { checkedAt: Date.now(), ...change }).then(() =>
      queryClient.invalidateQueries({ queryKey: ["mpc-catalog-prompt-state"] }),
    );
  };

  // Watching ends by itself once the job is done (no success message).
  useEffect(() => {
    if (watching && jobId == null && !startMutation.isPending) {
      setWatching(false);
      setAnswered(true);
    }
  }, [watching, jobId, startMutation.isPending]);

  const open = !answered && (offer != null || watching);
  const settled =
    !isTauri() ||
    !supported ||
    answered ||
    nothingToOffer ||
    checkQuery.isError ||
    statusQuery.isError ||
    promptStateQuery.isError ||
    (promptStateQuery.isSuccess && !due) ||
    (status != null && status.import_running);
  useEffect(() => setMpcCatalogPromptOpen(open), [open]);
  useEffect(() => {
    if (settled && !open) setMpcCatalogSettled();
  }, [settled, open]);

  if (!open) return null;

  if (watching) {
    const failed = job?.status === "failed";
    if (failed) {
      // Error view, as CardDbImportModal's: the single button is the choice.
      return (
        <ModalOverlay>
          <div className="modal modal-sm">
            <div className="modal-head">
              <span className="modal-title">MPC Fill Catalog Import Failed</span>
            </div>
            <p className="error-text" style={{ marginBottom: 14 }}>
              {job?.error ?? "The import failed."}
            </p>
            <div className="modal-actions">
              <button
                className="btn-primary"
                autoFocus
                onClick={() => {
                  setMpcCatalogJobId(null);
                  setWatching(false);
                  setAnswered(true);
                }}
              >
                Close
              </button>
            </div>
          </div>
        </ModalOverlay>
      );
    }
    return (
      <ModalOverlay>
        <div className="modal modal-sm">
          <div className="modal-head">
            <span className="modal-title">Importing MPC Fill Catalog</span>
          </div>
          <p style={{ marginBottom: 8 }}>{catalogPhaseLine(job)}</p>
          <p className="hint" style={{ marginBottom: 14 }}>
            Art search keeps using the current catalog until this finishes, so you can
            carry on while it runs.
          </p>
          <div className="modal-actions">
            <button
              className="btn-sm"
              onClick={() => {
                setWatching(false);
                setAnswered(true);
              }}
            >
              Continue in Background
            </button>
            <button
              className="btn-danger"
              onClick={() => {
                if (jobId) void generationApi.cancelMpcCatalogImport(jobId).catch(() => {});
              }}
            >
              Cancel Import
            </button>
          </div>
        </div>
      </ModalOverlay>
    );
  }

  const firstDownload = offer?.local_version == null;
  const skip = () => {
    if (offer) remember({ skippedVersion: offer.remote_version });
    setAnswered(true);
  };

  return (
    <ModalOverlay>
      <div className="modal modal-sm">
        <div className="modal-head">
          <span className="modal-title">
            {firstDownload ? "Download the MPC Fill Catalog?" : "Update the MPC Fill Catalog?"}
          </span>
        </div>
        {firstDownload ? (
          <p style={{ marginBottom: 8 }}>
            A local index of MPC Fill&apos;s community art makes art search instant and
            lets it work offline, {formatMb(offer?.download_bytes ?? 0)} to download.
            Otherwise search pings mpcfill.com.
          </p>
        ) : (
          <p style={{ marginBottom: 8 }}>
            A newer MPC Fill catalog is available ({offer?.changed_sources} source
            {offer?.changed_sources === 1 ? "" : "s"} changed, {formatMb(offer?.download_bytes ?? 0)}).
          </p>
        )}
        <p className="hint" style={{ marginBottom: 12 }}>
          You can also update any time from the Decklist sidebar.
        </p>
        {error && (
          <p className="error-text" style={{ marginBottom: 10 }}>
            {error}
          </p>
        )}
        <div className="modal-actions">
          <button className="btn-sm" onClick={skip} disabled={startMutation.isPending}>
            Skip
          </button>
          <button
            className="btn-primary"
            autoFocus
            onClick={() => {
              remember({});
              startMutation.mutate();
            }}
            disabled={startMutation.isPending}
          >
            {startMutation.isPending ? "Starting…" : "Download"}
          </button>
        </div>
      </div>
    </ModalOverlay>
  );
}
