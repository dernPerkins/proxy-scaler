import { useQuery, useQueryClient } from "@tanstack/react-query";
import { useState } from "react";
import { generationApi } from "../api/generation";
import { useConnection } from "../connection";
import { modelDisplayName } from "../constants";
import ModalOverlay from "./ModalOverlay";

// Raised the moment the worker's GPU runs out of memory and generation
// falls back to CPU (10-50x slower for the heavy models). Without this, a
// user who never opens the Tasks tab can leave a whole deck grinding away
// on CPU for hours without knowing why "it's taking so long" — the worker
// sets a flag in the shared DB the instant the fallback fires (see
// worker.py's on_cpu_fallback closure), this component notices it on the
// worker-status poll and asks whether to cancel the pending queue.
//
// Modeled on ResumeTasksPrompt: globally mounted, renders nothing until
// needed, two explicit buttons and no overlay/Esc dismiss — a quiet close
// would leave the "keep burning CPU vs stop" question unanswered while
// tasks pile up. Both buttons acknowledge the flag server-side, so the
// dialog never re-fires for the same fallback; a fresh fallback (or a
// worker restart that OOMs again) re-raises it.
//
// No device-probe gating on purpose: the flag can only be set by a worker
// that WAS on a GPU and lost it, so its presence is the whole signal —
// including for remote servers, where the local machine's own probe says
// nothing about the box doing the work.

interface FallbackNote {
  face_name?: string;
  model?: string;
}

function parseNote(raw: string): FallbackNote {
  try {
    const parsed: unknown = JSON.parse(raw);
    return typeof parsed === "object" && parsed !== null ? (parsed as FallbackNote) : {};
  } catch {
    return {};
  }
}

export default function CpuFallbackPrompt() {
  const { status } = useConnection();
  const queryClient = useQueryClient();
  const [working, setWorking] = useState(false);
  const [error, setError] = useState<string | null>(null);
  // Closes the dialog optimistically on ack, so it doesn't linger for the
  // one poll cycle until worker-status reads the cleared flag back.
  const [ackedNote, setAckedNote] = useState<string | null>(null);

  const connected = status.kind === "connected";
  const workerQuery = useQuery({
    queryKey: ["worker-status"],
    queryFn: generationApi.workerStatus,
    refetchInterval: 3000,
    enabled: connected,
  });
  const fallbackNote = workerQuery.data?.cpu_fallback ?? null;
  const open = connected && fallbackNote !== null && fallbackNote !== ackedNote;

  const tasksQuery = useQuery({
    queryKey: ["tasks"],
    queryFn: () => generationApi.listTasks(),
    enabled: open,
    refetchInterval: open ? 3000 : false,
  });

  if (!open) return null;

  const note = parseNote(fallbackNote);
  const pending = (tasksQuery.data ?? []).filter((t) => t.status === "pending").length;

  async function finish(action: () => Promise<unknown>) {
    setWorking(true);
    setError(null);
    try {
      await generationApi.ackCpuFallback();
      await action();
    } catch (err) {
      setError(err instanceof Error ? err.message : String(err));
      setWorking(false);
      return;
    }
    setAckedNote(fallbackNote);
    setWorking(false);
    void queryClient.invalidateQueries({ queryKey: ["tasks"] });
    void queryClient.invalidateQueries({ queryKey: ["worker-status"] });
    void queryClient.invalidateQueries({ queryKey: ["generation-status"] });
  }

  const cancelPending = () => finish(() => generationApi.cancelAllTasks(false));
  const continueOnCpu = () => finish(() => Promise.resolve());

  return (
    <ModalOverlay>
      <div className="modal modal-sm">
        <div className="modal-head">
          <span className="modal-title">Generation fell back to CPU</span>
        </div>

        <p style={{ marginBottom: 8 }}>
          The GPU ran out of memory
          {note.face_name ? (
            <>
              {" "}
              while generating <strong>{note.face_name}</strong>
              {note.model ? <> ({modelDisplayName(note.model)})</> : null}
            </>
          ) : null}
          , so generation switched to the CPU — typically 10–50× slower for
          the heavy models. The card currently generating will finish either
          way.
        </p>
        <p className="hint" style={{ marginBottom: 14 }}>
          Canceling clears the queue; canceled cards can be re-generated any
          time from the Decklist tab — a faster model (or a smaller tile
          size) may fit your GPU.
        </p>

        {error && (
          <p className="error-text" style={{ marginBottom: 12 }}>
            {error}
          </p>
        )}

        <div className="modal-actions">
          <button
            className="btn-danger"
            onClick={() => void cancelPending()}
            disabled={working}
          >
            {pending > 0
              ? `Cancel ${pending} pending ${pending === 1 ? "task" : "tasks"}`
              : "Cancel pending tasks"}
          </button>
          <button
            className="btn-primary"
            autoFocus
            onClick={() => void continueOnCpu()}
            disabled={working}
          >
            Continue on CPU
          </button>
        </div>
      </div>
    </ModalOverlay>
  );
}
