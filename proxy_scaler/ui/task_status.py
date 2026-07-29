"""Status icon/label conventions shared between the Tasks tab and any other
view that surfaces generation_tasks status inline (see decklist.py's
per-face status badges)."""

from __future__ import annotations

from proxy_scaler import db

STATUS_ICON = {
    "pending": "🕒",
    "running": "⚙️",
    "done": "✅",
    "failed": "❌",
    "canceled": "🚫",
}


def task_label(task: db.TaskRow) -> str:
    face_bit = f" ({task.face_label})" if task.face_label else ""
    return (
        f"{task.face_name}{face_bit} — "
        f"{task.set_code.upper()}/{task.collector_number} · "
        f"{task.dpi} DPI · {task.model}"
    )
