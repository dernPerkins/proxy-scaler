import { useMutation, useQuery, useQueryClient } from "@tanstack/react-query";
import { generationApi } from "../api/generation";
import StatusBadge from "../components/StatusBadge";
import { useProject } from "../context/ProjectContext";
import type { Task, TaskStatus } from "../api/types";

const STATUS_ORDER: TaskStatus[] = ["running", "pending", "done", "failed", "canceled"];

export default function TasksPage() {
  const queryClient = useQueryClient();
  const project = useProject();

  // 2-3s refetchInterval replaces Streamlit's st.fragment(run_every=...)
  // autopolling — same cadence, no server push needed at this scale.
  const tasksQuery = useQuery({
    queryKey: ["tasks"],
    queryFn: () => generationApi.listTasks(),
    refetchInterval: 2000,
  });

  const workerQuery = useQuery({
    queryKey: ["worker-status"],
    queryFn: () => generationApi.workerStatus(),
    refetchInterval: 3000,
  });

  const invalidateTasks = () => queryClient.invalidateQueries({ queryKey: ["tasks"] });

  const cancelMutation = useMutation({
    mutationFn: (taskId: number) => generationApi.cancelTask(taskId),
    onSuccess: invalidateTasks,
  });

  const cancelAllMutation = useMutation({
    mutationFn: () => generationApi.cancelAllTasks(),
    onSuccess: invalidateTasks,
  });

  const retryMutation = useMutation({
    mutationFn: (taskId: number) => generationApi.retryTask(taskId),
    onSuccess: invalidateTasks,
  });

  // Retries every failed task whose model/dpi match what the current
  // project is actually configured to generate, not the PDF tab's
  // separate preferred_dpi — dpi_targets is what enqueue_decklist_entries
  // used to produce each task's own dpi in the first place.
  const retryAllMutation = useMutation({
    mutationFn: () =>
      generationApi.retryAllTasks(
        project.projectTag ?? "",
        project.settings.model,
        project.settings.dpi_targets,
      ),
    onSuccess: invalidateTasks,
  });

  const tasks: Task[] = tasksQuery.data ?? [];
  const counts = tasks.reduce<Partial<Record<TaskStatus, number>>>((acc, t) => {
    acc[t.status] = (acc[t.status] ?? 0) + 1;
    return acc;
  }, {});

  const workerRunning = workerQuery.data?.running ?? false;
  const retryDisabledTitle = !workerRunning
    ? "Worker isn't running — restart the app before retrying"
    : undefined;

  return (
    <div>
      <h2>Tasks</h2>

      <div className="summary-row">
        <span className="chip">
          Worker:{" "}
          {workerQuery.isLoading
            ? "checking…"
            : workerQuery.data?.running
              ? "running"
              : "not running"}
        </span>
        {/* Fixed status order rather than object-key order — otherwise the
            badges reshuffle position between polls as counts change. */}
        {STATUS_ORDER.filter((s) => counts[s]).map((s) => (
          <StatusBadge key={s} status={s}>
            {s}: {counts[s]}
          </StatusBadge>
        ))}
        {tasks.length === 0 && !tasksQuery.isLoading && (
          <span className="hint">No tasks yet.</span>
        )}
        <button
          className="btn-danger"
          onClick={() => cancelAllMutation.mutate()}
          disabled={!counts.pending || cancelAllMutation.isPending}
        >
          Cancel All
        </button>
        <button
          className="btn-sm"
          onClick={() => retryAllMutation.mutate()}
          disabled={!counts.failed || !workerRunning || retryAllMutation.isPending}
          title={retryDisabledTitle}
        >
          Retry All
        </button>
      </div>

      {tasksQuery.isLoading ? (
        <p className="hint">Loading…</p>
      ) : tasks.length > 0 ? (
        <div className="table-wrap panel" style={{ padding: "12px 4px" }}>
          <table>
            <thead>
              <tr>
                <th>Card</th>
                <th>DPI</th>
                <th>Model</th>
                <th>Status</th>
                <th>Error</th>
                <th />
              </tr>
            </thead>
            <tbody>
              {tasks.map((task) => (
                <tr key={task.id}>
                  <td>
                    {task.card_name}
                    {task.face_label && (
                      <span className="variant-face" style={{ marginLeft: 6 }} title={task.face_name}>
                        {task.face_label}
                      </span>
                    )}
                  </td>
                  <td className="mono">{task.dpi}</td>
                  <td className="mono">{task.model}</td>
                  <td>
                    <StatusBadge status={task.status} />
                  </td>
                  <td className="error-text">{task.error ? task.error.slice(0, 100) : ""}</td>
                  <td>
                    {task.status === "pending" && (
                      <button
                        className="btn-sm"
                        onClick={() => cancelMutation.mutate(task.id)}
                        disabled={cancelMutation.isPending}
                      >
                        Cancel
                      </button>
                    )}
                    {task.status === "failed" && (
                      <button
                        className="btn-sm"
                        onClick={() => retryMutation.mutate(task.id)}
                        disabled={retryMutation.isPending || !workerRunning}
                        title={retryDisabledTitle}
                      >
                        Retry
                      </button>
                    )}
                  </td>
                </tr>
              ))}
            </tbody>
          </table>
        </div>
      ) : null}
    </div>
  );
}
