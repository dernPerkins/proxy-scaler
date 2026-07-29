"""Decklist / upscale tab UI."""

from __future__ import annotations

import base64
import io
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


def _upsert_gallery(item: FaceResult) -> None:
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
    """Resolve decklist entries (one batched Scryfall call, not one per
    card) and queue one task per (face, dpi) not already satisfied on
    disk. Returns (queued_count, failed_count)."""
    client = ScryfallClient()
    resolved = client.resolve_many(entries)
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
        _upsert_gallery(FaceResult.from_dict(item))


def _ensure_project_id() -> int | None:
    """If a project name has been entered but not saved yet, save it now
    so newly-enqueued tasks can be attached to a real project_id — that's
    what lets the background worker persist their results straight to the
    DB (upsert_gallery_item_for_task), not just this session's own
    reconstruction (_sync_pending_tasks). Returns the current project_id,
    which may still be None if no name has been entered at all — tasks can
    still be enqueued with project_id=None, they just won't get a DB
    gallery row until/unless the project is saved later."""
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
            gallery=list(st.session_state.gallery or []),
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
        use_container_width=True,
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
        use_container_width=True,
    ):
        open_comparison_dialog(item)
    if st.button(
        f"Regen {target_dpi}",
        key=f"regen-{_item_key(item)}",
        use_container_width=True,
    ):
        st.session_state.regen_key = _item_key(item)
        st.session_state.regen_target_dpi = target_dpi
        st.rerun()


def _generate_card_button(template: FaceResult) -> None:
    """Generate this card face using the sidebar's current model + checked DPIs."""
    if st.button(
        "Generate",
        key=f"gen-card-{_item_key(template)}",
        use_container_width=True,
        type="primary",
    ):
        st.session_state.card_regen_key = _item_key(template)
        st.rerun()


@st.fragment
def _render_face_row(
    group_key: str, face_items: list[FaceResult], *, show_regen: bool
) -> None:
    """Header (name/printing + Generate) then one column per existing
    (dpi, model) variant — images stay behind a click-to-load placeholder
    until the user asks for this specific face, so opening a gallery full
    of previously-generated images doesn't block the page on decoding every
    one of them up front.

    Wrapped as a fragment so clicking "Load images" only reruns this one
    row instead of the whole app — a full-app st.rerun() resets the
    browser's scroll position, which on a long gallery makes it look like
    the click did nothing (the row that loaded is now off-screen). The
    Generate/Regen/Compare buttons below still call plain st.rerun(), which
    per Streamlit's fragment semantics still forces a full-app rerun (their
    handling lives in render_decklist_tab(), outside this fragment) — only
    the Load-images click needed to change behavior.
    """
    first = face_items[0]
    label = first.face_name
    if first.face_label:
        label = f"{label} ({first.face_label})"
    header_label, header_btn = st.columns([5, 1], vertical_alignment="center")
    with header_label:
        st.markdown(
            f"**{label}** — `{first.set_code.upper()}/{first.collector_number}`"
        )
    with header_btn:
        if show_regen:
            _generate_card_button(first)

    if group_key not in st.session_state.loaded_faces:
        variants = ", ".join(f"{item.dpi} DPI · {item.model}" for item in face_items)
        st.caption(f"Original + {len(face_items)} variant(s): {variants}")
        if show_regen:
            if st.button(
                "Load images",
                key=f"load-{group_key}",
                icon=":material/image:",
            ):
                st.session_state.loaded_faces.add(group_key)
                st.rerun(scope="fragment")
        return

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

    # Fixed 4-wide grid (Original counts as one slot) that wraps to a new
    # row of columns rather than stretching fewer images to fill the width —
    # unfilled trailing slots are simply left blank, so a sparse row (e.g.
    # Original + 1 variant) stays left-aligned with a gap on the right
    # instead of rendering two oversized images.
    slots: list[tuple] = [("original", None)] + [("variant", item) for item in face_items]
    for chunk_start in range(0, len(slots), _IMAGES_PER_ROW):
        chunk = slots[chunk_start : chunk_start + _IMAGES_PER_ROW]
        cols = st.columns(_IMAGES_PER_ROW)
        for col, (kind, item) in zip(cols, chunk):
            if kind == "original":
                _render_original(col)
            else:
                _render_variant(col, item)


def _paginate(entries: list, page: int, page_size: int) -> tuple[list, int]:
    """Return (slice, total_pages)."""
    if page_size <= 0 or not entries:
        return entries, 1
    total_pages = max(1, (len(entries) + page_size - 1) // page_size)
    page = max(0, min(page, total_pages - 1))
    start = page * page_size
    return entries[start : start + page_size], total_pages


@st.fragment(run_every="3s")
def _draw_gallery(
    slot,
    *,
    show_regen: bool,
    page_size: int,
    jump_to_last: bool = False,
) -> None:
    """Wrapped as a fragment with a periodic tick so completed background
    tasks (see db.py's generation_tasks / worker.py) show up on their own
    — generation no longer blocks the script, so nothing else triggers a
    rerun once a task finishes. Syncing here (not just once at the top of
    render_decklist_tab()) is what makes the periodic tick actually pick
    up new results, not just redraw the same stale gallery every 3s."""
    _sync_pending_tasks()
    _sync_gallery_from_db()
    items = _gallery_items()
    with slot.container():
        st.subheader(f"Comparisons ({len(items)} image(s))")
        if not items:
            st.write("No images yet. Paste a list and click Generate.")
            return

        entries = group_by_face(items)
        unit = "card face(s)"

        total_pages = max(1, (len(entries) + page_size - 1) // page_size)
        if jump_to_last:
            st.session_state.gallery_page = total_pages - 1
        page = int(st.session_state.gallery_page)
        page = max(0, min(page, total_pages - 1))
        st.session_state.gallery_page = page

        # Live redraws during Generate call this many times in one run — only the
        # final pass (show_regen=True) may create keyed widgets, or Streamlit errors.
        if show_regen:
            nav_l, nav_label, nav_group = st.columns([1, 3, 2], gap="small")
            with nav_l:
                if st.button("← Prev", disabled=page <= 0, key="gallery_prev"):
                    st.session_state.gallery_page = page - 1
                    st.rerun()
            with nav_label:
                st.markdown(
                    f"<div style='text-align:right'>Page <b>{page + 1}</b> / {total_pages} "
                    f"· showing {page_size} {unit} per page</div>",
                    unsafe_allow_html=True,
                )
            with nav_group:
                nav_select, nav_r = st.columns([1, 1], gap="small")
                with nav_select:
                    page_options = list(range(total_pages))
                    selected_page = st.selectbox(
                        "Jump to page",
                        options=page_options,
                        format_func=lambda p: str(p + 1),
                        index=page,
                        key="gallery_page_select",
                        label_visibility="collapsed",
                    )
                    if selected_page != page:
                        st.session_state.gallery_page = selected_page
                        st.rerun()
                with nav_r:
                    if st.button(
                        "Next →",
                        disabled=page >= total_pages - 1,
                        key="gallery_next",
                        use_container_width=True,
                    ):
                        st.session_state.gallery_page = page + 1
                        st.rerun()
        else:
            st.caption(f"Page {page + 1} / {total_pages} · generating…")

        page_entries, _ = _paginate(entries, page, page_size)

        for group_key, face_items in page_entries:
            st.divider()
            _render_face_row(group_key, face_items, show_regen=show_regen)


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
            st.session_state.gallery_page = 0
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
    """Generate / gallery / regenerate UI (settings from session_state keys).

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
            st.slider(
                "Cards / comparisons per page",
                min_value=1,
                max_value=20,
                key="page_size",
                help=(
                    "Only this many entries are rendered at once (lazy gallery). "
                    "Images also use browser loading=lazy and a downscaled preview."
                ),
            )
            st.checkbox("Skip existing output files", key="skip_existing")
            st.text_input("Output directory", key="output_dir")
            st.text_input("Cache directory", key="cache_dir")
            st.text_input("Weights directory", key="weights_dir")

            st.divider()
            if st.button("Clear gallery view"):
                st.session_state.gallery = []
                st.session_state.gallery_page = 0
                st.rerun()

    model = st.session_state.model
    dpi_targets = selected_dpi_targets()
    page_size = int(st.session_state.page_size)
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
        "Decklist",
        height=220,
        key="decklist_text",
        placeholder=(
            "1 Sol Ring (c21) 263\n"
            "1 Dion, Bahamut's Dominant // Bahamut, Warden of Light (fin) 376\n"
            "4 Lightning Bolt"
        ),
    )

    run = st.button("Generate upscaled images", type="primary")
    status = st.empty()
    gallery_slot = st.empty()

    # When this tab is hidden, still mount controls/state but skip image work.
    if (
        not draw_gallery
        and not run
        and st.session_state.regen_key is None
        and st.session_state.get("card_regen_key") is None
    ):
        persist_decklist_widgets()
        return

    # All three generation actions below just enqueue work — actual
    # download/upscale runs in the background worker (see db.py's
    # generation_tasks / worker.py), not here, so none of this blocks the
    # script. Results appear automatically via _draw_gallery()'s periodic
    # sync (see its @st.fragment(run_every=...) decorator), and progress
    # is visible in the Tasks tab.

    regen_key = st.session_state.regen_key
    if regen_key is not None:
        items = _gallery_items()
        match = next((i for i in items if _item_key(i) == regen_key), None)
        target_dpi = st.session_state.get("regen_target_dpi")
        if match is not None:
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

    card_regen_key = st.session_state.get("card_regen_key")
    if card_regen_key is not None:
        items = _gallery_items()
        match = next((i for i in items if _item_key(i) == card_regen_key), None)
        if match is not None:
            if not dpi_targets:
                status.error("Select at least one target DPI in the sidebar first.")
            else:
                _enqueue_face(
                    scryfall_id=match.scryfall_id,
                    face_index=match.face_index,
                    face_label=match.face_label,
                    face_name=match.face_name,
                    card_name=match.card_name,
                    set_code=match.set_code,
                    collector_number=match.collector_number,
                    png_url=match.png_url,
                    dpi_targets=dpi_targets,
                    model=model,
                    tile_size=tile_size,
                    output_dir=output_dir,
                    cache_dir=cache_dir,
                    weights_dir=weights_dir,
                    project_id=_ensure_project_id(),
                )
                target_msg = "/".join(str(d) for d in dpi_targets) + " DPI"
                status.success(
                    f"Queued {match.face_name} with {model} @ {target_msg} — "
                    "see the Tasks tab."
                )
        st.session_state.card_regen_key = None

    if run:
        entries = parse_decklist_text(st.session_state.decklist_text)
        if not entries:
            status.error("No card entries found in the decklist.")
        elif not dpi_targets:
            status.error("Select at least one target DPI.")
        else:
            notes: list[str] = []
            queued, failed = _enqueue_decklist_entries(
                entries,
                model=model,
                dpi_targets=dpi_targets,
                skip_existing=skip_existing,
                tile_size=tile_size,
                output_dir=output_dir,
                cache_dir=cache_dir,
                weights_dir=weights_dir,
                project_id=_ensure_project_id(),
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

    _draw_gallery(
        gallery_slot,
        show_regen=True,
        page_size=page_size,
    )
    persist_decklist_widgets()
