import { useMutation, useQuery, useQueryClient } from "@tanstack/react-query";
import { api } from "../api/client";
import StatusBadge from "../components/StatusBadge";
import type { Task, TaskStatus } from "../api/types";

const STATUS_ORDER: TaskStatus[] = ["running", "pending", "done", "failed", "canceled"];

export default function TasksPage() {
  const queryClient = useQueryClient();

  // 2-3s refetchInterval replaces Streamlit's st.fragment(run_every=...)
  // autopolling — same cadence, no server push needed at this scale.
  const tasksQuery = useQuery({
    queryKey: ["tasks"],
    queryFn: () => api.listTasks(),
    refetchInterval: 2000,
  });

  const workerQuery = useQuery({
    queryKey: ["worker-status"],
    queryFn: () => api.workerStatus(),
    refetchInterval: 3000,
  });

  const cancelMutation = useMutation({
    mutationFn: (taskId: number) => api.cancelTask(taskId),
    onSuccess: () => {
      queryClient.invalidateQueries({ queryKey: ["tasks"] });
    },
  });

  const tasks: Task[] = tasksQuery.data ?? [];
  const counts = tasks.reduce<Partial<Record<TaskStatus, number>>>((acc, t) => {
    acc[t.status] = (acc[t.status] ?? 0) + 1;
    return acc;
  }, {});

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
                  <td>{task.card_name}</td>
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
