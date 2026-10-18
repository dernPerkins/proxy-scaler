// Sidebar panel for the generation server's MPC Fill art catalog
// (mpc_catalog.py): what's downloaded, an Update button that checks now
// (bypassing the launch prompt's once-a-day pace and the server's manifest
// cache), and Delete. Per-server, like CardDbPanel beside it. A started
// import is shown, and the app held, by MpcCatalogPrompt's modal.
import { useState } from "react";
import { useMutation, useQuery, useQueryClient } from "@tanstack/react-query";
import { generationApi } from "../api/generation";
import { serverSupportsMpcFill, useServerVersion } from "../config";
import { refreshAfterCatalogUpdate, setMpcCatalogJobId, useMpcCatalogJobId } from "../mpcCatalog";
import ConfirmDialog from "./ConfirmDialog";
import { catalogPhaseLine } from "./MpcCatalogPrompt";

export default function MpcCatalogPanel(props: { serverUnavailable: boolean }) {
  const { serverUnavailable } = props;
  const queryClient = useQueryClient();
  const supported = serverSupportsMpcFill(useServerVersion());
  const jobId = useMpcCatalogJobId();
  const [error, setError] = useState<string | null>(null);
  const [upToDate, setUpToDate] = useState(false);
  const [confirmDeleteOpen, setConfirmDeleteOpen] = useState(false);

  const statusQuery = useQuery({
    queryKey: ["mpc-catalog-status"],
    queryFn: () => generationApi.mpcCatalogStatus(),
    enabled: !serverUnavailable && supported,
    refetchInterval: 60_000,
    retry: false,
  });
  // Same key the watcher polls, so this only reads its cache.
  const jobQuery = useQuery({
    queryKey: ["mpc-catalog-job", jobId],
    queryFn: () => generationApi.mpcCatalogImportStatus(jobId as string),
    enabled: jobId != null,
    retry: false,
  });
  const job = jobId != null ? jobQuery.data : undefined;
  const running = job?.status === "running" || (jobId != null && job == null);

  const updateMutation = useMutation({
    mutationFn: async () => {
      const check = await generationApi.checkMpcCatalog(true);
      if (!check.update_available) return null;
      return generationApi.startMpcCatalogImport();
    },
    onMutate: () => {
      setError(null);
      setUpToDate(false);
    },
    onSuccess: (started) => {
      if (started) setMpcCatalogJobId(started.job_id);
      else setUpToDate(true);
    },
    onError: (err: Error) => setError(err.message),
  });

  const deleteMutation = useMutation({
    mutationFn: () => generationApi.deleteMpcCatalog(),
    onSuccess: () => {
      setError(null);
      setUpToDate(false);
      refreshAfterCatalogUpdate(queryClient);
    },
    onError: (err: Error) => setError(err.message),
  });

  if (!supported || statusQuery.isError) return null;

  const local = statusQuery.data?.local ?? null;
  const busy = serverUnavailable || running || updateMutation.isPending || deleteMutation.isPending;

  return (
    <div className="field divided">
      <span>MPC Fill Catalog</span>
      <p className="hint" style={{ margin: 0 }}>
        {running
          ? catalogPhaseLine(job)
          : local
            ? `${local.card_count.toLocaleString()} Images · ${local.source_count} Sources · Updated ${local.imported_at.slice(0, 10)}${upToDate ? " · Up to Date" : ""}`
            : "Not Downloaded Yet — art search asks mpcfill.com."}
      </p>
      {job?.status === "failed" && <p className="error-text">{job.error ?? "The update failed."}</p>}
      <button
        className="btn-sm"
        onClick={() => updateMutation.mutate()}
        disabled={busy}
        title={serverUnavailable ? "Generation server is unreachable" : undefined}
      >
        {updateMutation.isPending
          ? "Checking…"
          : local
            ? "Update MPC Fill Catalog"
            : "Download MPC Fill Catalog"}
      </button>
      {local && (
        <button
          className="btn-sm btn-danger"
          onClick={() => setConfirmDeleteOpen(true)}
          disabled={busy}
        >
          Delete MPC Fill Catalog
        </button>
      )}
      {confirmDeleteOpen && (
        <ConfirmDialog
          title="Delete the MPC Fill Catalog?"
          confirmLabel="Delete MPC Fill Catalog"
          onConfirm={() => {
            setConfirmDeleteOpen(false);
            deleteMutation.mutate();
          }}
          onCancel={() => setConfirmDeleteOpen(false)}
        >
          This removes the downloaded art index from this server. Art search goes back to
          asking mpcfill.com until it&apos;s downloaded again. Art already in your library and
          decks is untouched.
        </ConfirmDialog>
      )}
      {error && <p className="error-text">{error}</p>}
    </div>
  );
}
