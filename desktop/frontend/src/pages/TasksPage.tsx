import { useMutation, useQuery, useQueryClient } from "@tanstack/react-query";
import { api } from "../api/client";
import type { Task, TaskStatus } from "../api/types";

const STATUS_ICON: Record<TaskStatus, string> = {
  pending: "⏳",
  running: "⚙️",
  done: "✅",
  failed: "❌",
  canceled: "🚫",
};

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
      <p>
        Worker:{" "}
        {workerQuery.isLoading
          ? "checking…"
          : workerQuery.data?.running
            ? "🟢 running"
            : "🟡 not running"}
      </p>
      <p>
        {Object.entries(counts)
          .map(([status, count]) => `${STATUS_ICON[status as TaskStatus]} ${status}: ${count}`)
          .join("   ") || "No tasks yet."}
      </p>
      {tasksQuery.isLoading ? (
        <p>Loading…</p>
      ) : (
        <table style={{ width: "100%", borderCollapse: "collapse" }}>
          <thead>
            <tr>
              <th style={{ textAlign: "left" }}>Card</th>
              <th style={{ textAlign: "left" }}>DPI</th>
              <th style={{ textAlign: "left" }}>Model</th>
              <th style={{ textAlign: "left" }}>Status</th>
              <th style={{ textAlign: "left" }}>Error</th>
              <th></th>
            </tr>
          </thead>
          <tbody>
            {tasks.map((task) => (
              <tr key={task.id}>
                <td>{task.card_name}</td>
                <td>{task.dpi}</td>
                <td>{task.model}</td>
                <td>
                  {STATUS_ICON[task.status]} {task.status}
                </td>
                <td>{task.error ? task.error.slice(0, 100) : ""}</td>
                <td>
                  {task.status === "pending" && (
                    <button
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
      )}
    </div>
  );
}
