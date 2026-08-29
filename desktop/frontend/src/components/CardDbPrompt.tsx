// The launch-time card-database offer: when the connected generation
// server has no imported Scryfall corpus, offer the download once —
// printings can't be browsed and non-English matching is live-API-only
// until it exists, and a fresh install would otherwise only discover the
// feature via the sidebar panel.
//
// Last link in the boot-dialog chain (see update.ts's sequencing section):
// renders nothing until the update check has settled with no update modal
// up AND ResumeTasksPrompt has settled with no resume modal up — launch
// dialogs never stack. "Not now" suppresses it for this launch; "Don't
// ask again" persists (app_settings — see project_store.rs); the sidebar
// panel remains the way in either way.
import { useRef, useState, useSyncExternalStore } from "react";
import { useMutation, useQuery, useQueryClient } from "@tanstack/react-query";
import { generationApi } from "../api/generation";
import { projectApi } from "../api/project";
import type { CardDataset } from "../api/types";
import { setCardDbImportJobId } from "../cardDbImport";
import { useConnection } from "../connection";
import { useServerReadiness } from "../config";
import ModalOverlay from "./ModalOverlay";
import { isTauri } from "../tauri";
import {
  getBootUpdateCheckSettled,
  getPatchNotesPromptOpen,
  getPatchNotesSettled,
  getResumeTasksPromptOpen,
  getResumeTasksSettled,
  getUpdatePromptOpen,
  subscribeUpdateStore,
} from "../update";

// Approximate compressed download sizes — see CardDbPanel's copy of this
// and card_db_status()'s docstring for why they aren't read live.
const APPROX_DOWNLOAD_MB: Record<CardDataset, number> = {
  default_cards: 80,
  all_cards: 400,
};

export default function CardDbPrompt() {
  const queryClient = useQueryClient();
  const connection = useConnection();
  const readiness = useServerReadiness();
  const [dataset, setDataset] = useState<CardDataset>("default_cards");
  const [dontAskAgain, setDontAskAgain] = useState(false);
  // "Not now" — this launch only. Also latched after a Download, so the
  // prompt can never reappear mid-session (e.g. after a delete).
  const [dismissedThisLaunch, setDismissedThisLaunch] = useState(false);
  const [error, setError] = useState<string | null>(null);

  const bootUpdateSettled = useSyncExternalStore(
    subscribeUpdateStore,
    getBootUpdateCheckSettled,
  );
  const updatePromptOpen = useSyncExternalStore(subscribeUpdateStore, getUpdatePromptOpen);
  const patchNotesSettled = useSyncExternalStore(subscribeUpdateStore, getPatchNotesSettled);
  const patchNotesOpen = useSyncExternalStore(subscribeUpdateStore, getPatchNotesPromptOpen);
  const resumeSettled = useSyncExternalStore(subscribeUpdateStore, getResumeTasksSettled);
  const resumePromptOpen = useSyncExternalStore(
    subscribeUpdateStore,
    getResumeTasksPromptOpen,
  );

  const serverUnavailable =
    connection.mode === "remote" ? !connection.remoteHealthy : readiness.status !== "ready";
  const bootChainClear =
    bootUpdateSettled &&
    !updatePromptOpen &&
    patchNotesSettled &&
    !patchNotesOpen &&
    resumeSettled &&
    !resumePromptOpen;

  const dismissedQuery = useQuery({
    queryKey: ["card-db-prompt-dismissed"],
    queryFn: () => projectApi.getCardDbPromptDismissed(),
    enabled: isTauri(),
    staleTime: Infinity,
  });

  const statusQuery = useQuery({
    queryKey: ["card-db-status"],
    queryFn: () => generationApi.cardDbStatus(),
    // Same key as CardDbPanel's poll — whichever mounts first fills it.
    enabled: !serverUnavailable,
    refetchInterval: 60_000,
  });

  const startMutation = useMutation({
    mutationFn: (chosen: CardDataset) => generationApi.startCardImport(chosen),
    onSuccess: ({ job_id }) => {
      // CardDbImportModal takes over with its blocking progress view;
      // this dialog's job is done.
      setDismissedThisLaunch(true);
      setCardDbImportJobId(job_id);
      void queryClient.invalidateQueries({ queryKey: ["card-db-status"] });
    },
    onError: (err: Error) => setError(err.message),
  });

  // Once visible, stay visible until answered — a status refetch (or the
  // server flickering) must not yank an open dialog off the screen. Only
  // an answer (Download / Not now) closes it, via dismissedThisLaunch.
  const shownRef = useRef(false);
  const status = statusQuery.data;
  const eligible =
    bootChainClear &&
    !serverUnavailable &&
    !dismissedThisLaunch &&
    dismissedQuery.data === false &&
    status != null &&
    status.local == null &&
    !status.import_running;
  if (!shownRef.current && eligible) shownRef.current = true;
  if (!shownRef.current || dismissedThisLaunch) return null;

  const dismiss = () => {
    setDismissedThisLaunch(true);
    shownRef.current = false;
    if (dontAskAgain) {
      projectApi.setCardDbPromptDismissed(true).catch(() => {});
      queryClient.setQueryData(["card-db-prompt-dismissed"], true);
    }
  };

  return (
    <ModalOverlay>
      <div className="modal modal-sm">
        <div className="modal-head">
          <span className="modal-title">Download the card database?</span>
        </div>

        <p style={{ marginBottom: 8 }}>
          A local copy of Scryfall's card data powers the printing picker
          and lets most imports and generations skip live API calls. It
          downloads once and updates on demand from the Decklist sidebar.
        </p>

        <select
          value={dataset}
          onChange={(e) => setDataset(e.target.value as CardDataset)}
          style={{ marginBottom: 10 }}
        >
          <option value="default_cards">
            English only (~{APPROX_DOWNLOAD_MB.default_cards} MB)
          </option>
          <option value="all_cards">
            All languages (~{APPROX_DOWNLOAD_MB.all_cards} MB)
          </option>
        </select>

        {error && (
          <p className="error-text" style={{ marginBottom: 10 }}>
            {error}
          </p>
        )}

        <label className="check" style={{ marginBottom: 12 }}>
          <input
            type="checkbox"
            checked={dontAskAgain}
            onChange={(e) => setDontAskAgain(e.target.checked)}
          />
          Don't ask again
        </label>

        <div className="modal-actions">
          <button className="btn-sm" onClick={dismiss} disabled={startMutation.isPending}>
            Not now
          </button>
          <button
            className="btn-primary"
            autoFocus
            onClick={() => startMutation.mutate(dataset)}
            disabled={startMutation.isPending}
          >
            {startMutation.isPending ? "Starting…" : "Download"}
          </button>
        </div>
      </div>
    </ModalOverlay>
  );
}
