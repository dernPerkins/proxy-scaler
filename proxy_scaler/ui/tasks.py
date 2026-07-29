"""Tasks tab: live monitor for the background generation queue.

Generation no longer runs inline in the Decklist tab — it's enqueued as
rows in db.py's generation_tasks table and processed one at a time by the
background worker (worker.py, auto-spawned by app.py). This tab is the
window into that queue.
"""

from __future__ import annotations

import streamlit as st

from proxy_scaler import db

_STATUS_ICON = {
    "pending": "🕒",
    "running": "⚙️",
    "done": "✅",
    "failed": "❌",
    "canceled": "🚫",
}
_ACTIVE_STATUSES = ("pending", "running")
_HISTORY_STATUSES = ("done", "failed", "canceled")


def _task_label(task: db.TaskRow) -> str:
    face_bit = f" ({task.face_label})" if task.face_label else ""
    return (
        f"{task.face_name}{face_bit} — "
        f"{task.set_code.upper()}/{task.collector_number} · "
        f"{task.dpi} DPI · {task.model}"
    )


@st.fragment(run_every="2s")
def _render_task_monitor(project_id: int | None) -> None:
    if db.is_worker_running():
        st.caption("🟢 Background worker is running.")
    else:
        st.caption("🟡 Worker not detected — it should auto-start on your next click.")

    if project_id is not None:
        tasks = db.list_tasks(project_id=project_id)
    else:
        # No saved project yet — fall back to just this session's own
        # queued tasks (see decklist.py's _enqueue_face/pending_task_ids)
        # so there's still *some* visibility before the user bothers to
        # save. Tasks enqueued under project_id=None from an earlier,
        # now-gone session aren't listed here — save a project for a view
        # that survives across sessions.
        pending_ids = st.session_state.get("pending_task_ids") or []
        tasks = [t for t in (db.get_task(tid) for tid in pending_ids) if t is not None]
        if not tasks:
            st.info(
                "No project saved yet — tasks you queue this session will "
                "show up here. Save a project for a view that persists "
                "across sessions."
            )
            return

    if not tasks:
        st.info("No tasks yet. Queue some from the Decklist tab.")
        return

    counts: dict[str, int] = {}
    for t in tasks:
        counts[t.status] = counts.get(t.status, 0) + 1
    cols = st.columns(len(_STATUS_ICON))
    for col, (status, icon) in zip(cols, _STATUS_ICON.items()):
        with col:
            st.metric(f"{icon} {status.title()}", counts.get(status, 0))

    active = [t for t in tasks if t.status in _ACTIVE_STATUSES]
    if active:
        st.divider()
        st.markdown("**In progress**")
        for task in active:
            row_label, row_action = st.columns([5, 1])
            with row_label:
                st.write(f"{_STATUS_ICON[task.status]} {_task_label(task)}")
            with row_action:
                if task.status == "pending":
                    if st.button(
                        "Cancel",
                        key=f"cancel-task-{task.id}",
                        use_container_width=True,
                    ):
                        db.cancel_task(task.id)
                        st.rerun(scope="fragment")

    history = [t for t in tasks if t.status in _HISTORY_STATUSES][:50]
    if history:
        st.divider()
        st.markdown("**Recent history**")
        st.dataframe(
            [
                {
                    "Status": f"{_STATUS_ICON[t.status]} {t.status}",
                    "Card": _task_label(t),
                    "Completed": t.completed_at or "",
                    "Error": t.error or "",
                }
                for t in history
            ],
            use_container_width=True,
            hide_index=True,
        )


def render_tasks_tab(*, active: bool = True) -> None:
    """Background generation queue monitor. No settings/widgets of its
    own to persist across tab switches (unlike Decklist/PDF Generation),
    so a plain active check is enough — nothing needs the
    persist/restore-widget dance those tabs use."""
    if not active:
        return
    st.subheader("Generation Tasks")
    _render_task_monitor(st.session_state.get("project_id"))
