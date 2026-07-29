"""Decklist tab UI: import cards into a project's persistent card table,
generate images, review/manage them."""

from __future__ import annotations

import base64
import io
from collections import defaultdict
from pathlib import Path

import streamlit as st
from PIL import Image

from proxy_scaler import db
from proxy_scaler.decklist import parse_decklist_text
from proxy_scaler.db import save_project
from proxy_scaler.dpi import DPI_OPTIONS
from proxy_scaler.pipeline import (
    FaceResult,
    clear_generated_data,
    expected_face_result,
    face_group_key,
    group_by_face,
    output_filename,
)
from proxy_scaler.scryfall import ScryfallClient, ScryfallError, expand_faces
from proxy_scaler.ui.compare import open_comparison_dialog
from proxy_scaler.ui.projects import (
    DEFAULT_TILE_SIZE,
    selected_dpi_targets,
    settings_from_session,
    persist_decklist_widgets,
)
from proxy_scaler.ui.task_status import STATUS_ICON
from proxy_scaler.upscale import UpscaleModel

ROOT = Path(__file__).resolve().parents[2]
EXAMPLE_PATH = ROOT / "cards.example.txt"

_PREVIEW_MAX_EDGE = 600
# Fixed grid width per card face (Original + variants); rows wrap instead of
# stretching to fill the width, so a sparse row (e.g. Original + 1 variant)
# stays left-aligned at a consistent image size rather than rendering two
# oversized images to fill 2 wide columns.
_IMAGES_PER_ROW = 4

# Transformer/attention-heavy architectures that can OOM a ~12GB GPU on a
# full-image forward pass — the lighter CNN-based models don't need tiling.
_HEAVY_MODELS = frozenset(
    {UpscaleModel.ILLUSTRATIONJANAI, UpscaleModel.ULTRASHARP_V2, UpscaleModel.HAT}
)

_SORT_FIELDS = {
    "Name": lambda c: (c.card_name or "").casefold(),
    "Set": lambda c: (c.set_code or "").casefold(),
}
_SORT_OPTIONS = ["Name", "Set", "(none)"]


def _effective_tile_size(model_id: UpscaleModel, tile_size_setting: int) -> int:
    """0 (not manually set) auto-falls-back to DEFAULT_TILE_SIZE for heavy
    models only, leaving already-working lighter models untouched. An
    explicit non-zero setting always wins, regardless of model."""
    if tile_size_setting > 0:
        return tile_size_setting
    return DEFAULT_TILE_SIZE if model_id in _HEAVY_MODELS else 0


def _gallery_items() -> list[FaceResult]:
    return [FaceResult.from_dict(d) for d in st.session_state.gallery]


def _item_key(item: FaceResult) -> str:
    # Include printing + filename so disk-recovered items (empty scryfall_id)
    # don't collide on regenerate button keys.
    identity = item.scryfall_id or f"{item.set_code}/{item.collector_number}"
    return (
        f"{identity}:{item.face_index}:{item.face_label}:"
        f"{item.model}:{item.dpi}:{item.out_path.name}"
    )


def _upsert_gallery(item: FaceResult, *, mark_loaded: bool = True) -> None:
    items = st.session_state.gallery
    key = _item_key(item)
    for i, existing in enumerate(items):
        existing_item = FaceResult.from_dict(existing)
        if _item_key(existing_item) == key:
            items[i] = item.to_dict()
            break
    else:
        items.append(item.to_dict())
    # A freshly generated/regenerated face should appear immediately (live
    # progress feedback) rather than sitting behind the click-to-load gate
    # meant for images that were already on disk before this page opened.
    # mark_loaded=False is for bulk syncs (_sync_gallery_from_db) pulling in
    # possibly-many already-existing rows — those must NOT auto-expand, or
    # the click-to-load gate never actually gates anything.
    if mark_loaded:
        st.session_state.loaded_faces.add(face_group_key(item))


def _enqueue_face(
    *,
    scryfall_id: str,
    face_index: int | None,
    face_label: str | None,
    face_name: str,
    card_name: str,
    set_code: str,
    collector_number: str,
    png_url: str,
    dpi_targets: list[int],
    model: str,
    tile_size: int,
    output_dir: Path,
    cache_dir: Path,
    weights_dir: Path,
    project_id: int | None,
) -> list[int]:
    """Queue one task per requested DPI for an already-resolved face (no
    Scryfall call needed — caller already has scryfall_id/png_url/etc, from
    either a fresh batch resolve or an existing gallery FaceResult).
    Tracks the new task ids in session_state so this session's own gallery
    can pick up their results the moment they're done (see
    _sync_pending_tasks) — works even before a project is ever saved."""
    task_ids = [
        db.enqueue_task(
            project_id,
            scryfall_id=scryfall_id,
            face_index=face_index,
            face_label=face_label,
            face_name=face_name,
            card_name=card_name,
            set_code=set_code,
            collector_number=collector_number,
            png_url=png_url,
            dpi=dpi,
            model=model,
            tile_size=tile_size,
            output_dir=str(output_dir),
            cache_dir=str(cache_dir),
            weights_dir=str(weights_dir),
        )
        for dpi in dpi_targets
    ]
    st.session_state.pending_task_ids = [
        *st.session_state.get("pending_task_ids", []),
        *task_ids,
    ]
    return task_ids


def _active_task_keys(project_id: int | None) -> set[tuple[str, int | None, int, str]]:
    """(scryfall_id, face_index, dpi, model) keys with a pending/running
    task for this project. Enqueueing must skip these too, not just
    disk-existing files — otherwise clicking Generate again before a
    previous batch finishes (or the periodic Generate-all button) would
    queue duplicate work for an image that's already in flight."""
    if project_id is None:
        return set()
    tasks = db.list_tasks(project_id=project_id, statuses=["pending", "running"])
    return {(t.scryfall_id, t.face_index, t.dpi, t.model) for t in tasks}


def _enqueue_decklist_entries(
    entries: list,
    *,
    model: str,
    dpi_targets: list[int],
    skip_existing: bool,
    tile_size: int,
    output_dir: Path,
    cache_dir: Path,
    weights_dir: Path,
    project_id: int | None,
    on_note=None,
) -> tuple[int, int]:
    """Resolve entries (one batched Scryfall call, not one per card) and
    queue one task per (face, dpi) that's actually missing: not already
    satisfied on disk (when skip_existing) and not already pending/running
    for this project (always — regardless of skip_existing, duplicating
    in-flight work is never wanted). Returns (queued_count, failed_count).
    Used for both the bulk "Generate upscaled images" button (fed the
    whole project card list) and a single-card Generate click (fed a
    one-entry list)."""
    client = ScryfallClient()
    resolved = client.resolve_many(entries)
    active = _active_task_keys(project_id)
    queued = 0
    failed = 0
    seen_keys: set[str] = set()
    for entry, pre in zip(entries, resolved):
        try:
            if isinstance(pre, ScryfallError):
                raise pre
            card, warnings = pre
            for w in warnings:
                if on_note:
                    on_note(w)
            for face in expand_faces(card):
                face_key = f"{face.scryfall_id}:{face.face_index}"
                if face_key in seen_keys:
                    continue
                seen_keys.add(face_key)

                targets_needed = []
                for target_dpi in dpi_targets:
                    if (face.scryfall_id, face.face_index, target_dpi, model) in active:
                        continue
                    out_name = output_filename(
                        face.face_name,
                        face.set_code,
                        face.collector_number,
                        face.face_label,
                        model,
                        target_dpi,
                    )
                    if skip_existing and (output_dir / out_name).exists():
                        continue
                    targets_needed.append(target_dpi)
                if not targets_needed:
                    continue

                _enqueue_face(
                    scryfall_id=face.scryfall_id,
                    face_index=face.face_index,
                    face_label=face.face_label,
                    face_name=face.face_name,
                    card_name=face.card_name,
                    set_code=face.set_code,
                    collector_number=face.collector_number,
                    png_url=face.png_url,
                    dpi_targets=targets_needed,
                    model=model,
                    tile_size=tile_size,
                    output_dir=output_dir,
                    cache_dir=cache_dir,
                    weights_dir=weights_dir,
                    project_id=project_id,
                )
                queued += len(targets_needed)
        except ScryfallError as exc:
            failed += 1
            if on_note:
                on_note(f"FAIL [{entry.raw_line}]: {exc}")
    return queued, failed


def _import_entries(project_id: int, entries: list) -> tuple[int, int, int]:
    """Resolve entries and add new cards to the project's persistent card
    list — add_cards_to_project() skips any already present (by
    scryfall_id, then set+collector, then name), so re-importing the same
    text is a safe no-op rather than a duplicate. Returns
    (added, skipped, failed)."""
    client = ScryfallClient()
    resolved = client.resolve_many(entries)
    candidates: list[dict] = []
    failed = 0
    for entry, pre in zip(entries, resolved):
        if isinstance(pre, ScryfallError):
            failed += 1
            continue
        card, _warnings = pre
        candidates.append(
            {
                "scryfall_id": card.get("id"),
                "card_name": card.get("name"),
                "set_code": card.get("set"),
                "collector_number": card.get("collector_number"),
                "quantity": entry.quantity,
                "original_import_line": entry.raw_line,
            }
        )
    added = db.add_cards_to_project(project_id, candidates)
    skipped = len(candidates) - added
    return added, skipped, failed


def _sync_pending_tasks() -> None:
    """Pull results for tasks this session enqueued into
    st.session_state.gallery once they're done — reconstructed
    deterministically from the task's own fields (pipeline.
    expected_face_result), so this works even before a project is saved
    (no project_id yet for the worker to attach a DB gallery row to)."""
    pending = st.session_state.get("pending_task_ids") or []
    if not pending:
        return
    still_pending = []
    for task_id in pending:
        task = db.get_task(task_id)
        if task is None:
            continue
        if task.status in ("pending", "running"):
            still_pending.append(task_id)
        elif task.status == "done":
            _upsert_gallery(expected_face_result(task))
        # failed/canceled tasks are dropped here — visible in the Tasks tab.
    st.session_state.pending_task_ids = still_pending


def _sync_gallery_from_db() -> None:
    """Pull any project_gallery_items rows not yet reflected in
    st.session_state.gallery — how a background task's result (written
    directly to the DB by the worker) shows up without an explicit
    Save/reload, including tasks queued from an earlier session."""
    project_id = st.session_state.get("project_id")
    if project_id is None:
        return
    for item in db.list_gallery_items_for_project(project_id):
        _upsert_gallery(FaceResult.from_dict(item), mark_loaded=False)


def _card_identity(
    set_code: str | None, collector_number: str | None, scryfall_id: str | None
) -> str:
    """Physical-card identity (no face_index/label) shared by
    ProjectCardRow, FaceResult, and TaskRow — used to match a project_cards
    row to its faces' gallery items/tasks regardless of which one you start
    from."""
    if set_code and collector_number:
        return f"{set_code.lower()}/{collector_number}"
    return scryfall_id or "unknown"


def _face_key_for_task(task: db.TaskRow) -> str:
    """Same identity scheme as pipeline.face_group_key(), read off a
    TaskRow instead of a FaceResult, so a face's in-flight task and its
    (once done) gallery item merge under the same key."""
    identity = _card_identity(task.set_code, task.collector_number, task.scryfall_id)
    return f"{identity}:{task.face_index}:{task.face_label}"


def _project_tasks(project_id: int | None) -> list[db.TaskRow]:
    """Same dual lookup as tasks.py's monitor: this project's tasks once
    saved, else just this session's own queued tasks (pending_task_ids) so
    there's still some visibility before the user names/saves a project."""
    if project_id is not None:
        return db.list_tasks(project_id=project_id)
    pending_ids = st.session_state.get("pending_task_ids") or []
    return [t for t in (db.get_task(tid) for tid in pending_ids) if t is not None]


def _build_rows(
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
        key = _face_key_for_task(task)
        if key not in gallery_groups and key not in task_groups:
            order.append(key)
        task_groups.setdefault(key, []).append(task)
    return [(key, gallery_groups.get(key, []), task_groups.get(key, [])) for key in order]


def _group_by_card(
    items: list[FaceResult], tasks: list[db.TaskRow]
) -> tuple[dict[str, list[FaceResult]], dict[str, list[db.TaskRow]]]:
    """Coarser than _build_rows: group by physical card (drop face_index),
    for matching a table row's card identity to all of its faces at once."""
    gallery_by_card: dict[str, list[FaceResult]] = defaultdict(list)
    for item in items:
        gallery_by_card[_card_identity(item.set_code, item.collector_number, item.scryfall_id)].append(item)
    tasks_by_card: dict[str, list[db.TaskRow]] = defaultdict(list)
    for task in tasks:
        tasks_by_card[_card_identity(task.set_code, task.collector_number, task.scryfall_id)].append(task)
    return gallery_by_card, tasks_by_card


def _sort_cards(
    cards: list[db.ProjectCardRow], primary: str, secondary: str, descending: bool
) -> list[db.ProjectCardRow]:
    keys = [f for f in (primary, secondary) if f in _SORT_FIELDS]
    if not keys:
        return list(cards)
    return sorted(cards, key=lambda c: tuple(_SORT_FIELDS[k](c) for k in keys), reverse=descending)


def _ensure_project_id() -> int | None:
    """If a project name has been entered but not saved yet, save it now
    so newly-enqueued tasks (and Import) can be attached to a real
    project_id — that's what lets the background worker persist their
    results straight to the DB (upsert_gallery_item_for_task), not just
    this session's own reconstruction (_sync_pending_tasks). Returns the
    current project_id, which may still be None if no name has been
    entered at all."""
    name = (st.session_state.get("project_name") or "").strip()
    if not name:
        return st.session_state.get("project_id")
    if st.session_state.get("project_id") is not None:
        return st.session_state.project_id
    try:
        pid = save_project(
            name,
            import_decklist_text=st.session_state.decklist_text or "",
            settings=settings_from_session(),
            project_id=None,
        )
        st.session_state.project_id = pid
        return pid
    except ValueError:
        return None


@st.cache_data(show_spinner=False)
def _encode_preview(
    path_str: str, _mtime: float, max_edge: int
) -> tuple[str, str, int, int, int, int]:
    """Downscale + base64-encode one preview. Cached on (path, mtime,
    max_edge) so reruns triggered by unrelated widgets (e.g. changing the
    sidebar model) don't re-decode/re-encode every image in the gallery —
    that PIL work was the actual cost behind the visible full-gallery
    "refresh" on every settings change; identical output also lets
    Streamlit's frontend skip repainting the <img> tag entirely."""
    with Image.open(path_str) as im:
        full_w, full_h = im.size
        has_alpha = im.mode in ("RGBA", "LA")
        preview = im.convert("RGBA") if has_alpha else im.convert("RGB")
        if max(preview.size) > max_edge:
            preview.thumbnail((max_edge, max_edge), Image.Resampling.LANCZOS)
        buf = io.BytesIO()
        if has_alpha:
            preview.save(buf, format="PNG", optimize=True)
            mime = "image/png"
        else:
            preview.save(buf, format="JPEG", quality=82, optimize=True)
            mime = "image/jpeg"
    b64 = base64.b64encode(buf.getvalue()).decode("ascii")
    return b64, mime, preview.size[0], preview.size[1], full_w, full_h


def _lazy_image(path: Path, *, max_edge: int = _PREVIEW_MAX_EDGE) -> None:
    """Render a browser-lazy-loaded preview (downscaled for UI speed)."""
    if not path.is_file():
        st.warning("missing")
        return
    try:
        mtime = path.stat().st_mtime
        b64, mime, pw, ph, full_w, full_h = _encode_preview(str(path), mtime, max_edge)
        st.markdown(
            f'<img src="data:{mime};base64,{b64}" '
            f'loading="lazy" decoding="async" '
            f'style="width:100%;height:auto;display:block;" '
            f'alt="{path.name}" />',
            unsafe_allow_html=True,
        )
        st.caption(f"preview {pw}×{ph} (file {full_w}×{full_h})")
    except OSError as exc:
        st.warning(f"Could not load {path.name}: {exc}")


def _download_button(path: Path, *, label: str, key: str) -> None:
    """Download-as-is button for a PNG already on disk."""
    if not path.is_file():
        return
    st.download_button(
        label,
        data=path.read_bytes(),
        file_name=path.name,
        mime="image/png",
        key=key,
        width="stretch",
    )


def _dpi_action_buttons(item: FaceResult, target_dpi: int) -> None:
    """Download / Compare X / Regen X stacked under an existing image's column."""
    _download_button(
        item.out_path,
        label=f"Download {target_dpi}",
        key=f"dl-{_item_key(item)}",
    )
    if st.button(
        f"Compare {target_dpi}",
        key=f"cmp-{_item_key(item)}",
        width="stretch",
    ):
        open_comparison_dialog(item)
    if st.button(
        f"Regen {target_dpi}",
        key=f"regen-{_item_key(item)}",
        width="stretch",
    ):
        st.session_state.regen_key = _item_key(item)
        st.session_state.regen_target_dpi = target_dpi
        st.rerun()


def _status_for_pairs(
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


def _render_status_badges(face_items: list[FaceResult], face_tasks: list[db.TaskRow]) -> None:
    """No image decode at all — just an icon+text line per (dpi, model)."""
    for dpi, model, status, error in _status_for_pairs(face_items, face_tasks):
        icon = STATUS_ICON.get(status, "•")
        st.caption(f"{icon} {dpi} DPI · {model} · {status}")
        if status == "failed" and error:
            st.caption(f"　{error[:200]}")


def _render_face_images(face_items: list[FaceResult], *, show_regen: bool) -> None:
    """Original + one column per existing (dpi, model) variant, in a fixed
    4-wide grid that wraps to a new row of columns rather than stretching
    fewer images to fill the width — unfilled trailing slots are simply
    left blank, so a sparse row (e.g. Original + 1 variant) stays
    left-aligned with a gap on the right instead of rendering two
    oversized images. Only called once a row is expanded — see
    _render_card_row."""
    first = face_items[0]

    def _render_original(col) -> None:
        with col:
            st.caption("Original ~300 DPI")
            _lazy_image(first.original_path)
            if show_regen:
                _download_button(
                    first.original_path,
                    label="Download original",
                    key=f"dl-orig-{_item_key(first)}",
                )

    def _render_variant(col, item: FaceResult) -> None:
        with col:
            device = (item.device or "unknown").lower()
            device_bit = {"gpu": "GPU", "cpu": "CPU"}.get(device, "?")
            st.caption(f"{item.dpi} DPI · {item.model} · {device_bit}")
            _lazy_image(item.out_path)
            if show_regen:
                _dpi_action_buttons(item, item.dpi)

    slots: list[tuple] = [("original", None)] + [("variant", item) for item in face_items]
    for chunk_start in range(0, len(slots), _IMAGES_PER_ROW):
        chunk = slots[chunk_start : chunk_start + _IMAGES_PER_ROW]
        cols = st.columns(_IMAGES_PER_ROW)
        for col, (kind, item) in zip(cols, chunk):
            if kind == "original":
                _render_original(col)
            else:
                _render_variant(col, item)


@st.fragment
def _render_card_row(
    card: db.ProjectCardRow,
    gallery_for_card: list[FaceResult],
    tasks_for_card: list[db.TaskRow],
    *,
    show_regen: bool,
) -> None:
    """One table row: Name / Set / Collector / Qty / Show Images / Generate /
    Remove, then compact status badges for every face this card has (one
    or two — DFCs have front+back), and — once expanded — each face's full
    image grid. Wrapped as a fragment so Show/Hide images and Remove only
    rerun this one row (a full-app rerun would reset scroll position on a
    long list) — Remove hides the row immediately via removed_card_ids;
    _draw_card_table's own next refresh drops it from the underlying list
    and prunes the id back out.
    """
    if card.id in st.session_state.get("removed_card_ids", set()):
        return

    row_key = f"card-{card.id}"
    face_groups = _build_rows(gallery_for_card, tasks_for_card)
    has_images = any(face_items for _k, face_items, _t in face_groups)
    expanded = row_key in st.session_state.loaded_faces

    st.divider()
    name_col, set_col, coll_col, qty_col, show_col, gen_col, rm_col = st.columns(
        [3, 1, 1, 0.7, 1.4, 1, 1], vertical_alignment="center"
    )
    with name_col:
        st.write(f"**{card.card_name or card.original_import_line}**")
    with set_col:
        st.write((card.set_code or "—").upper())
    with coll_col:
        st.write(card.collector_number or "—")
    with qty_col:
        st.write(f"×{card.quantity or 1}")
    with show_col:
        if has_images:
            label = "Hide images" if expanded else "Show images"
            if st.button(label, key=f"show-{row_key}", width="stretch"):
                if expanded:
                    st.session_state.loaded_faces.discard(row_key)
                else:
                    st.session_state.loaded_faces.add(row_key)
                st.rerun(scope="fragment")
    with gen_col:
        if show_regen and st.button(
            "Generate", key=f"gen-{row_key}", width="stretch", type="primary"
        ):
            st.session_state.card_generate_id = card.id
            st.rerun()
    with rm_col:
        if show_regen and st.button("Remove", key=f"rm-{row_key}", width="stretch"):
            db.remove_project_card(card.id)
            st.session_state.removed_card_ids.add(card.id)
            st.rerun(scope="fragment")

    if not face_groups:
        st.caption("Not generated yet.")
    else:
        for _key, face_items, face_tasks in face_groups:
            _render_status_badges(face_items, face_tasks)

    if expanded:
        for _key, face_items, _face_tasks in face_groups:
            if not face_items:
                continue
            if face_items[0].face_label:
                st.caption(f"**{face_items[0].face_label}**")
            _render_face_images(face_items, show_regen=show_regen)


@st.fragment(run_every="3s")
def _draw_card_table(slot, project_id: int | None, *, show_regen: bool) -> None:
    """Wrapped as a fragment with a periodic tick so completed background
    tasks (see db.py's generation_tasks / worker.py) show up on their own
    — generation no longer blocks the script, so nothing else triggers a
    rerun once a task finishes."""
    _sync_pending_tasks()
    _sync_gallery_from_db()
    items = _gallery_items()
    tasks = _project_tasks(project_id)
    cards = db.list_project_cards(project_id) if project_id is not None else []
    # Prune removed_card_ids down to ids this fresh fetch still contains —
    # once a removal has actually propagated here the id no longer needs
    # hiding (it's simply absent from `cards`), so this keeps the set from
    # growing unbounded over a long session.
    st.session_state.removed_card_ids = st.session_state.get(
        "removed_card_ids", set()
    ) & {c.id for c in cards}

    with slot.container():
        header_col, gen_all_col = st.columns([4, 1.6], vertical_alignment="center")
        with header_col:
            st.subheader(f"Cards ({len(cards)})")
        with gen_all_col:
            if show_regen and st.button(
                "Generate upscaled images",
                key="generate-all",
                type="primary",
                width="stretch",
                disabled=not cards,
            ):
                st.session_state.generate_all_requested = True
                st.rerun()

        if not cards:
            st.write("No cards yet — import a list above.")
            return

        if show_regen:
            sort1_col, sort2_col, desc_col = st.columns([2, 2, 1])
            with sort1_col:
                st.selectbox("Sort by", options=_SORT_OPTIONS, key="card_sort_primary")
            with sort2_col:
                st.selectbox("Then by", options=_SORT_OPTIONS, key="card_sort_secondary")
            with desc_col:
                st.checkbox("Descending", key="card_sort_desc")

        sorted_cards = _sort_cards(
            cards,
            st.session_state.get("card_sort_primary", "Name"),
            st.session_state.get("card_sort_secondary", "(none)"),
            bool(st.session_state.get("card_sort_desc", False)),
        )

        gallery_by_card, tasks_by_card = _group_by_card(items, tasks)

        for card in sorted_cards:
            identity = _card_identity(card.set_code, card.collector_number, card.scryfall_id)
            _render_card_row(
                card,
                gallery_by_card.get(identity, []),
                tasks_by_card.get(identity, []),
                show_regen=show_regen,
            )


def render_global_sidebar_actions() -> None:
    """Destructive actions always shown at the bottom of the sidebar,
    regardless of which tab is active. Call this AFTER both tabs render so
    it lands below their tab-specific settings sections in the sidebar
    (Streamlit appends to st.sidebar in call order)."""
    with st.sidebar:
        delete_generated_notes = st.session_state.get("_delete_generated_notes")
        if delete_generated_notes is not None:
            for note in delete_generated_notes:
                st.write(note)
            st.success("Generated data cleared.")
            st.session_state._delete_generated_notes = None

        st.caption("Deletes output/ + imgcache/ on disk (keeps model weights).")
        confirm_delete = st.checkbox("Confirm delete generated data")
        if st.button(
            "Delete all generated images & cache",
            type="primary",
            disabled=not confirm_delete,
        ):
            notes = clear_generated_data(
                Path(st.session_state.output_dir),
                Path(st.session_state.cache_dir),
            )
            st.session_state.gallery = []
            # Stashed and shown on the next run — a message written right
            # before st.rerun() gets discarded before it's ever visible.
            st.session_state._delete_generated_notes = notes
            st.rerun()

        clear_all_notes = st.session_state.get("_clear_all_notes")
        if clear_all_notes is not None:
            for note in clear_all_notes:
                st.write(note)
            st.success("All projects cleared — app reset to a clean slate.")
            st.session_state._clear_all_notes = None

        st.caption(
            "Deletes every saved project and all generated images/cache — "
            "a full reset. Settings above are kept as-is."
        )
        confirm_clear_all = st.checkbox("Confirm clear all projects")
        if st.button(
            "Clear all projects",
            type="primary",
            disabled=not confirm_clear_all,
        ):
            # Deferred to apply_pending_project_actions(): project_name is a
            # widget-bound key already instantiated earlier in this run
            # (render_project_bar), so it can't be written to directly here.
            st.session_state._pending_clear_all = True
            st.rerun()


def render_decklist_tab(*, draw_gallery: bool = True) -> None:
    """Import / generate / card-table UI (settings from session_state keys).

    Always mount widgets (even when another tab is visible) so Streamlit does
    not wipe keyed settings. Set draw_gallery=False to skip heavy image
    previews while this tab is hidden.
    """
    if draw_gallery:
        with st.sidebar:
            st.header("Settings")
            model_values = [m.value for m in UpscaleModel]
            st.selectbox(
                "Upscale model",
                options=model_values,
                format_func=lambda v: UpscaleModel(v).label,
                key="model",
                help=(
                    "SwinIR is fidelity-first (default). "
                    "RealESRNet reduces hallucination; Real-ESRGAN is faster/sharper."
                ),
            )
            st.number_input(
                "Tile size (0 = auto)",
                min_value=0,
                max_value=2000,
                step=32,
                key="tile_size",
                help=(
                    "Processes each card in overlapping tiles instead of one "
                    "full-image pass, to avoid GPU out-of-memory errors. "
                    "0 = automatic: stays off for lighter models, and falls "
                    f"back to {DEFAULT_TILE_SIZE}px for memory-hungry ones "
                    "(IllustrationJaNai/UltraSharpV2/HAT). Set explicitly to "
                    "override — lower if you still hit OOM, higher for more "
                    "speed/quality if you have headroom."
                ),
            )
            _effective_preview = _effective_tile_size(
                UpscaleModel(st.session_state.model), int(st.session_state.tile_size)
            )
            if _effective_preview:
                st.caption(
                    f"Effective tile size: {_effective_preview}px"
                    + (" (auto)" if int(st.session_state.tile_size) == 0 else "")
                )
            st.write("Target DPI")
            dpi_cols = st.columns(len(DPI_OPTIONS))
            for col, d in zip(dpi_cols, DPI_OPTIONS):
                with col:
                    st.checkbox(f"{d} DPI", key=f"dpi_{d}")
            st.checkbox("Skip existing output files", key="skip_existing")
            st.text_input("Output directory", key="output_dir")
            st.text_input("Cache directory", key="cache_dir")
            st.text_input("Weights directory", key="weights_dir")

            st.divider()
            if st.button("Clear gallery view"):
                st.session_state.gallery = []
                st.rerun()

    model = st.session_state.model
    dpi_targets = selected_dpi_targets()
    skip_existing = bool(st.session_state.skip_existing)
    output_dir = Path(st.session_state.output_dir)
    cache_dir = Path(st.session_state.cache_dir)
    weights_dir = Path(st.session_state.weights_dir)
    tile_size = _effective_tile_size(
        UpscaleModel(model), int(st.session_state.tile_size)
    )

    if st.button("Load example deck (full)"):
        if EXAMPLE_PATH.is_file():
            st.session_state.decklist_text = EXAMPLE_PATH.read_text(encoding="utf-8")
        else:
            st.warning("cards.example.txt not found")

    st.text_area(
        "Import Card List",
        height=220,
        key="decklist_text",
        placeholder=(
            "1 Sol Ring (c21) 263\n"
            "1 Dion, Bahamut's Dominant // Bahamut, Warden of Light (fin) 376\n"
            "4 Lightning Bolt"
        ),
        help=(
            "Paste cards here and click Import to add them to this "
            "project's card list below — re-importing the same text is "
            "safe, cards already in the list are skipped, not duplicated."
        ),
    )
    import_clicked = st.button("Import cards", type="primary")

    status = st.empty()
    table_slot = st.empty()

    card_generate_id = st.session_state.get("card_generate_id")
    generate_all_requested = st.session_state.get("generate_all_requested", False)
    # When this tab is hidden, still mount controls/state but skip heavy work.
    if (
        not draw_gallery
        and not import_clicked
        and not generate_all_requested
        and st.session_state.regen_key is None
        and card_generate_id is None
    ):
        persist_decklist_widgets()
        return

    # All generation actions below just enqueue work — actual download/
    # upscale runs in the background worker (see db.py's generation_tasks /
    # worker.py), not here, so none of this blocks the script. Results
    # appear automatically via _draw_card_table()'s periodic sync, and
    # progress is visible in the Tasks tab.

    if import_clicked:
        pid = _ensure_project_id()
        if pid is None:
            status.error(
                "Enter a project name in the Project bar above before importing cards."
            )
        else:
            entries = parse_decklist_text(st.session_state.decklist_text)
            if not entries:
                status.error("No card entries found to import.")
            else:
                added, skipped, failed = _import_entries(pid, entries)
                msg = f"Imported {added} new card(s)"
                if skipped:
                    msg += f", {skipped} already in the list"
                if failed:
                    msg += f", {failed} failed to resolve"
                (status.warning if failed else status.success)(msg + ".")

    regen_key = st.session_state.regen_key
    if regen_key is not None:
        items = _gallery_items()
        match = next((i for i in items if _item_key(i) == regen_key), None)
        target_dpi = st.session_state.get("regen_target_dpi")
        if match is not None:
            if not match.png_url:
                status.error(
                    f"No known source image for {match.face_name} (recovered "
                    "from disk) — re-add it via Import to regenerate."
                )
            else:
                target_dpi = target_dpi if target_dpi is not None else match.dpi
                # Redo this exact variant unchanged — its own model/tile size,
                # not whatever the sidebar currently has selected (a separate
                # model may be picked there while comparing variants side by
                # side in the same row).
                regen_tile_size = _effective_tile_size(
                    UpscaleModel(match.model), int(st.session_state.tile_size)
                )
                _enqueue_face(
                    scryfall_id=match.scryfall_id,
                    face_index=match.face_index,
                    face_label=match.face_label,
                    face_name=match.face_name,
                    card_name=match.card_name,
                    set_code=match.set_code,
                    collector_number=match.collector_number,
                    png_url=match.png_url,
                    dpi_targets=[target_dpi],
                    model=match.model,
                    tile_size=regen_tile_size,
                    output_dir=output_dir,
                    cache_dir=cache_dir,
                    weights_dir=weights_dir,
                    project_id=_ensure_project_id(),
                )
                status.success(
                    f"Queued regenerate for {match.face_name} "
                    f"({match.model} {target_dpi} DPI) — see the Tasks tab."
                )
        st.session_state.regen_key = None
        st.session_state.regen_target_dpi = None

    if card_generate_id is not None:
        pid = st.session_state.get("project_id")
        cards = db.list_project_cards(pid) if pid is not None else []
        card = next((c for c in cards if c.id == card_generate_id), None)
        if card is None:
            status.error("That card is no longer in the project.")
        elif not dpi_targets:
            status.error("Select at least one target DPI in the sidebar first.")
        else:
            notes: list[str] = []
            queued, failed = _enqueue_decklist_entries(
                [card.to_deck_entry()],
                model=model,
                dpi_targets=dpi_targets,
                skip_existing=skip_existing,
                tile_size=tile_size,
                output_dir=output_dir,
                cache_dir=cache_dir,
                weights_dir=weights_dir,
                project_id=pid,
                on_note=notes.append,
            )
            if failed:
                status.error(f"Could not resolve {card.card_name}: {'; '.join(notes)}")
            elif queued:
                status.success(f"Queued {card.card_name} — see the Tasks tab.")
            else:
                status.info("Nothing to do — every requested image already exists.")
        st.session_state.card_generate_id = None

    if generate_all_requested:
        pid = st.session_state.get("project_id")
        cards = db.list_project_cards(pid) if pid is not None else []
        if not cards:
            status.error("No cards in the project yet — import some first.")
        elif not dpi_targets:
            status.error("Select at least one target DPI.")
        else:
            notes: list[str] = []
            queued, failed = _enqueue_decklist_entries(
                [c.to_deck_entry() for c in cards],
                model=model,
                dpi_targets=dpi_targets,
                skip_existing=skip_existing,
                tile_size=tile_size,
                output_dir=output_dir,
                cache_dir=cache_dir,
                weights_dir=weights_dir,
                project_id=pid,
                on_note=notes.append,
            )
            if failed:
                status.warning(
                    f"Queued {queued} task(s) — {failed} card(s) failed to resolve."
                )
                for msg in notes:
                    st.text(msg)
            elif queued:
                status.success(
                    f"Queued {queued} task(s) — see the Tasks tab to monitor progress."
                )
            else:
                status.info("Nothing to do — every requested image already exists.")
        st.session_state.generate_all_requested = False

    _draw_card_table(
        table_slot,
        st.session_state.get("project_id"),
        show_regen=True,
    )
    persist_decklist_widgets()
