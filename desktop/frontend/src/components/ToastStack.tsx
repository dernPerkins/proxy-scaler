// The bottom-right stack of toasts (toasts.ts), newest at the bottom.
// Mounted once in App.tsx. Each non-sticky toast leaves after TOAST_MS;
// hovering it pauses the countdown, and leaving resumes it with what was
// left, so a toast someone is reading never vanishes under the pointer.
import { useEffect, useRef } from "react";
import { TOAST_MS, dismissToast, useToasts, type Toast } from "../toasts";

function ToastItem({ toast }: { toast: Toast }) {
  const remaining = useRef(TOAST_MS);
  const startedAt = useRef(0);
  const timer = useRef<ReturnType<typeof setTimeout> | null>(null);

  const start = () => {
    if (toast.sticky) return;
    startedAt.current = Date.now();
    timer.current = setTimeout(() => dismissToast(toast.id), remaining.current);
  };
  const pause = () => {
    if (timer.current == null) return;
    clearTimeout(timer.current);
    timer.current = null;
    remaining.current = Math.max(0, remaining.current - (Date.now() - startedAt.current));
  };

  useEffect(() => {
    start();
    return pause;
    // One countdown per toast, for its life.
    // eslint-disable-next-line react-hooks/exhaustive-deps
  }, [toast.id]);

  return (
    <div
      className={`toast toast-${toast.kind}`}
      role={toast.kind === "err" ? "alert" : undefined}
      onMouseEnter={pause}
      onMouseLeave={start}
    >
      <span className="dot" />
      <div className="toast-body">
        {toast.title && <strong>{toast.title}</strong>}
        <div style={toast.title ? { marginTop: 2 } : undefined}>{toast.message}</div>
      </div>
      <button
        type="button"
        className="toast-close"
        aria-label="Dismiss"
        title="Dismiss"
        onClick={() => dismissToast(toast.id)}
      >
        ×
      </button>
    </div>
  );
}

export default function ToastStack() {
  const toasts = useToasts();
  if (toasts.length === 0) return null;
  return (
    <div className="toast-stack" role="status">
      {toasts.map((t) => (
        <ToastItem key={t.id} toast={t} />
      ))}
    </div>
  );
}
