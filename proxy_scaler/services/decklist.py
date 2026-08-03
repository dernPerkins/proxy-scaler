"""Card/face identity matching and status-aggregation logic, extracted from
proxy_scaler/ui/decklist.py during the Streamlit -> FastAPI migration. Pure
functions only — no st.session_state, no rendering. This is real business
logic (not UI glue), preserved faithfully including its bug-fix reasoning.
"""

from __future__ import annotations

from collections import defaultdict

from proxy_scaler import db
from proxy_scaler.pipeline import FaceResult, group_by_face

_SORT_FIELDS = {
    "Name": lambda c: (c.card_name or "").casefold(),
    "Set": lambda c: (c.set_code or "").casefold(),
}
_SORT_OPTIONS = ["Name", "Set", "(none)"]


def card_identity(
    set_code: str | None, collector_number: str | None, scryfall_id: str | None
) -> str:
    """Physical-card identity (no face_index/label) shared by
    ProjectCardRow, FaceResult, and TaskRow — used to match a project_cards
    row to its faces' gallery items/tasks regardless of which one you start
    from."""
    if set_code and collector_number:
        return f"{set_code.lower()}/{collector_number}"
    return scryfall_id or "unknown"


def face_key_for_task(task: db.TaskRow) -> str:
    """Same identity scheme as pipeline.face_group_key(), read off a
    TaskRow instead of a FaceResult, so a face's in-flight task and its
    (once done) gallery item merge under the same key."""
    identity = card_identity(task.set_code, task.collector_number, task.scryfall_id)
    return f"{identity}:{task.face_index}:{task.face_label}"


def build_rows(
    items: list[FaceResult], tasks: list[db.TaskRow]
) -> list[tuple[str, list[FaceResult], list[db.TaskRow]]]:
    """Merge gallery items (done variants) and tasks (in-flight/failed/
    canceled) into one row per face. A face with a task but no done variant
    yet still gets a row — that's how "task exists, here's its status"
    shows up before anything has actually finished generating."""
    gallery_groups = dict(group_by_face(items))
    order = list(gallery_groups.keys())
    task_groups: dict[str, list[db.TaskRow]] = {}
    for task in tasks:
        key = face_key_for_task(task)
        if key not in gallery_groups and key not in task_groups:
            order.append(key)
        task_groups.setdefault(key, []).append(task)
    return [(key, gallery_groups.get(key, []), task_groups.get(key, [])) for key in order]


def group_by_card(
    items: list[FaceResult], tasks: list[db.TaskRow]
) -> tuple[dict[str, list[FaceResult]], dict[str, list[db.TaskRow]]]:
    """Coarser than build_rows: group by physical card (drop face_index),
    for matching a table row's card identity to all of its faces at once."""
    gallery_by_card: dict[str, list[FaceResult]] = defaultdict(list)
    for item in items:
        gallery_by_card[
            card_identity(item.set_code, item.collector_number, item.scryfall_id)
        ].append(item)
    tasks_by_card: dict[str, list[db.TaskRow]] = defaultdict(list)
    for task in tasks:
        tasks_by_card[
            card_identity(task.set_code, task.collector_number, task.scryfall_id)
        ].append(task)
    return gallery_by_card, tasks_by_card


def status_for_pairs(
    face_items: list[FaceResult], face_tasks: list[db.TaskRow]
) -> list[tuple[int, str, str, str | None]]:
    """One (dpi, model, status, error) entry per (dpi, model) pair this face
    has ever had a done variant or a task for. A done FaceResult always
    wins over any task history for the same pair — task records for a pair
    that's since succeeded are just history, not current state. Otherwise
    the newest task for that pair (face_tasks is already created_at DESC)
    determines the status."""
    done_pairs = {(item.dpi, item.model): item for item in face_items}
    task_pairs: dict[tuple[int, str], db.TaskRow] = {}
    for task in face_tasks:
        task_pairs.setdefault((task.dpi, task.model), task)
    all_pairs = set(done_pairs) | set(task_pairs)
    rows = []
    for dpi, model in sorted(all_pairs):
        if (dpi, model) in done_pairs:
            rows.append((dpi, model, "done", None))
        else:
            task = task_pairs[(dpi, model)]
            rows.append((dpi, model, task.status, task.error))
    return rows


def sort_cards(
    cards: list[db.ProjectCardRow], primary: str, secondary: str, descending: bool
) -> list[db.ProjectCardRow]:
    keys = [f for f in (primary, secondary) if f in _SORT_FIELDS]
    if not keys:
        return list(cards)
    return sorted(cards, key=lambda c: tuple(_SORT_FIELDS[k](c) for k in keys), reverse=descending)
