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
import { toast } from "../toasts";

/** "223 of 279 Sources" (public of all MPC Fill lists), or just the public
 *  count from a crawl that didn't record the total. */
function sourcesLabel(local: { source_count: number; source_total: number | null }): string {
  return local.source_total
    ? `${local.source_count} of ${local.source_total} Sources`
    : `${local.source_count} Sources`;
}

export default function MpcCatalogPanel(props: { serverUnavailable: boolean }) {
  const { serverUnavailable } = props;
  const queryClient = useQueryClient();
  const supported = serverSupportsMpcFill(useServerVersion());
  const jobId = useMpcCatalogJobId();
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
    onSuccess: (started) => {
      if (started) setMpcCatalogJobId(started.job_id);
      else toast("The MPC Fill Catalog is up to date.", "info", { key: "mpc-catalog" });
    },
    onError: (err: Error) => toast(err.message, "err", { key: "mpc-catalog" }),
  });

  const deleteMutation = useMutation({
    mutationFn: () => generationApi.deleteMpcCatalog(),
    onSuccess: () => refreshAfterCatalogUpdate(queryClient),
    onError: (err: Error) => toast(err.message, "err", { key: "mpc-catalog" }),
  });

  if (!supported || statusQuery.isError) return null;

  const local = statusQuery.data?.local ?? null;
  // Only uploaders who share their Google Drive folder publicly can be
  // indexed (the crawler never recovers hidden ones), so say how many that
  // is, and where to go for the rest.
  const coverageNote = [
    "Only Publicly Shared Google Drive Libraries Will Be Available.",
    local?.source_total
      ? `${local.source_count} of ${local.source_total} MPC Fill uploaders share theirs publicly.`
      : null,
    "If you don't see a user in the Catalog, please go directly to MPC Fill to get their artwork instead.",
  ]
    .filter(Boolean)
    .join(" ");
  const busy = serverUnavailable || running || updateMutation.isPending || deleteMutation.isPending;

  return (
    <div className="field divided">
      <span className="field-label-row">
        MPC Fill Catalog
        <span className="info-tip" tabIndex={0} role="img" aria-label={coverageNote} title={coverageNote}>
          ⓘ
        </span>
      </span>
      <p className="hint" style={{ margin: 0 }}>
        {running
          ? catalogPhaseLine(job)
          : local
            ? `${local.card_count.toLocaleString()} Images · ${sourcesLabel(local)} · Updated ${local.imported_at.slice(0, 10)}`
            : "Not Downloaded Yet: MPC Fill art search needs it."}
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
          This removes the downloaded art index from this server. MPC Fill art search stops
          working until it&apos;s downloaded again. Art already in your library and decks is
          untouched.
        </ConfirmDialog>
      )}
    </div>
  );
}
