"""Decklist / upscale tab UI."""

from __future__ import annotations

import base64
import io
from collections import defaultdict
from pathlib import Path

import streamlit as st
from PIL import Image

from proxy_scaler.decklist import parse_decklist_text
from proxy_scaler.db import save_project
from proxy_scaler.dpi import DPI_OPTIONS
from proxy_scaler.pipeline import (
    FaceResult,
    clear_generated_data,
    process_entries,
    regenerate_face,
)
from proxy_scaler.ui.compare import open_comparison_dialog
from proxy_scaler.ui.projects import (
    selected_dpi_targets,
    settings_from_session,
    persist_decklist_widgets,
)
from proxy_scaler.upscale import UpscaleModel

ROOT = Path(__file__).resolve().parents[2]
EXAMPLE_PATH = ROOT / "cards.example.txt"

_PREVIEW_MAX_EDGE = 600


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
            return
    items.append(item.to_dict())


def _group_by_face(items: list[FaceResult]) -> list[tuple[str, list[FaceResult]]]:
    """Group results by card face for multi-DPI rows."""
    groups: dict[str, list[FaceResult]] = defaultdict(list)
    order: list[str] = []
    for item in items:
        identity = item.scryfall_id or f"{item.set_code}/{item.collector_number}"
        key = f"{identity}:{item.face_index}:{item.face_label}:{item.model}"
        if key not in groups:
            order.append(key)
        groups[key].append(item)
    result: list[tuple[str, list[FaceResult]]] = []
    for key in order:
        face_items = sorted(groups[key], key=lambda x: x.dpi)
        result.append((key, face_items))
    return result


def _lazy_image(path: Path, *, max_edge: int = _PREVIEW_MAX_EDGE) -> None:
    """Render a browser-lazy-loaded preview (downscaled for UI speed)."""
    if not path.is_file():
        st.warning("missing")
        return
    try:
        with Image.open(path) as im:
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
        st.markdown(
            f'<img src="data:{mime};base64,{b64}" '
            f'loading="lazy" decoding="async" '
            f'style="width:100%;height:auto;display:block;" '
            f'alt="{path.name}" />',
            unsafe_allow_html=True,
        )
        st.caption(f"preview {preview.size[0]}×{preview.size[1]} (file {full_w}×{full_h})")
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


def _generate_button(template: FaceResult, target_dpi: int) -> None:
    """Generate X for a DPI that hasn't been produced yet."""
    if st.button(
        f"Generate {target_dpi}",
        key=f"gen-{_item_key(template)}-{target_dpi}",
        use_container_width=True,
    ):
        st.session_state.regen_key = _item_key(template)
        st.session_state.regen_target_dpi = target_dpi
        st.rerun()


def _render_dpi_row(face_items: list[FaceResult], *, show_regen: bool) -> None:
    """Original + one column per DPI target; buttons stack under each image."""
    first = face_items[0]
    by_dpi = {item.dpi: item for item in face_items}
    label = first.face_name
    if first.face_label:
        label = f"{label} ({first.face_label})"
    st.markdown(
        f"**{label}** — `{first.set_code.upper()}/{first.collector_number}` "
        f"· **{first.model}**"
    )
    cols = st.columns(1 + len(DPI_OPTIONS))
    with cols[0]:
        st.caption("Original ~300 DPI")
        _lazy_image(first.original_path)
        if show_regen:
            _download_button(
                first.original_path,
                label="Download original",
                key=f"dl-orig-{_item_key(first)}",
            )
    for col, target_dpi in zip(cols[1:], DPI_OPTIONS):
        with col:
            item = by_dpi.get(target_dpi)
            if item is not None:
                device = (item.device or "unknown").lower()
                device_bit = {"gpu": "GPU", "cpu": "CPU"}.get(device, "?")
                st.caption(f"{target_dpi} DPI · {device_bit}")
                _lazy_image(item.out_path)
                if show_regen:
                    _dpi_action_buttons(item, target_dpi)
            else:
                st.caption(f"{target_dpi} DPI · not generated")
                if show_regen:
                    _generate_button(first, target_dpi)


def _paginate(entries: list, page: int, page_size: int) -> tuple[list, int]:
    """Return (slice, total_pages)."""
    if page_size <= 0 or not entries:
        return entries, 1
    total_pages = max(1, (len(entries) + page_size - 1) // page_size)
    page = max(0, min(page, total_pages - 1))
    start = page * page_size
    return entries[start : start + page_size], total_pages


def _draw_gallery(
    slot,
    *,
    show_regen: bool,
    page_size: int,
    jump_to_last: bool = False,
) -> None:
    items = _gallery_items()
    with slot.container():
        st.subheader(f"Comparisons ({len(items)} image(s))")
        if not items:
            st.write("No images yet. Paste a list and click Generate.")
            return

        entries = _group_by_face(items)
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

        for _group_key, face_items in page_entries:
            st.divider()
            _render_dpi_row(face_items, show_regen=show_regen)


def render_decklist_tab(*, draw_gallery: bool = True) -> None:
    """Generate / gallery / regenerate UI (settings from session_state keys).

    Always mount widgets (even when another tab is visible) so Streamlit does
    not wipe keyed settings. Set draw_gallery=False to skip heavy image
    previews while this tab is hidden.
    """
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

    model = st.session_state.model
    dpi_targets = selected_dpi_targets()
    page_size = int(st.session_state.page_size)
    skip_existing = bool(st.session_state.skip_existing)
    output_dir = Path(st.session_state.output_dir)
    cache_dir = Path(st.session_state.cache_dir)
    weights_dir = Path(st.session_state.weights_dir)

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
    log_box = st.empty()
    status = st.empty()
    gallery_slot = st.empty()

    # When this tab is hidden, still mount controls/state but skip image work.
    if not draw_gallery and not run and st.session_state.regen_key is None:
        persist_decklist_widgets()
        return

    regen_key = st.session_state.regen_key
    if regen_key is not None:
        items = _gallery_items()
        match = next((i for i in items if _item_key(i) == regen_key), None)
        target_dpi = st.session_state.get("regen_target_dpi")
        if match is not None:
            target_dpi = target_dpi if target_dpi is not None else match.dpi
            status.info(
                f"Regenerating {match.face_name} with {model} @ {target_dpi} DPI…"
            )
            lines: list[str] = []

            def on_progress(msg: str) -> None:
                lines.append(msg)
                log_box.code("\n".join(lines[-30:]), language="text")

            try:
                updated = regenerate_face(
                    match,
                    output_dir=output_dir,
                    cache_dir=cache_dir,
                    weights_dir=weights_dir,
                    dpi=target_dpi,
                    model=model,
                    on_progress=on_progress,
                )
                _upsert_gallery(updated)
                name = (st.session_state.get("project_name") or "").strip()
                if name and st.session_state.gallery:
                    try:
                        pid = save_project(
                            name,
                            import_decklist_text=st.session_state.decklist_text or "",
                            settings=settings_from_session(),
                            gallery=list(st.session_state.gallery),
                            project_id=st.session_state.get("project_id"),
                        )
                        st.session_state.project_id = pid
                    except ValueError:
                        pass
                status.success(
                    f"Regenerated {updated.face_name} "
                    f"({updated.model} {updated.dpi} DPI)"
                )
            except Exception as exc:  # noqa: BLE001
                status.error(f"Regenerate failed: {exc}")
        st.session_state.regen_key = None
        st.session_state.regen_target_dpi = None

    if run:
        entries = parse_decklist_text(st.session_state.decklist_text)
        if not entries:
            status.error("No card entries found in the decklist.")
        elif not dpi_targets:
            status.error("Select at least one target DPI.")
        else:
            st.session_state.gallery = []
            st.session_state.gallery_page = 0
            lines: list[str] = []
            target_msg = "/".join(str(d) for d in dpi_targets) + " DPI"
            status.info(
                f"Parsed {len(entries)} line(s) with {model} @ {target_msg}. "
                "Comparisons appear as each finishes…"
            )
            _draw_gallery(
                gallery_slot,
                show_regen=False,
                page_size=page_size,
                jump_to_last=True,
            )

            def on_progress_gen(msg: str) -> None:
                lines.append(msg)
                log_box.code("\n".join(lines[-50:]), language="text")

            def on_face_done(face: FaceResult) -> None:
                _upsert_gallery(face)
                _draw_gallery(
                    gallery_slot,
                    show_regen=False,
                    page_size=page_size,
                    jump_to_last=True,
                )

            try:
                result = process_entries(
                    entries,
                    output_dir=output_dir,
                    dpi_targets=dpi_targets,
                    model=model,
                    cache_dir=cache_dir,
                    weights_dir=weights_dir,
                    skip_existing=skip_existing,
                    on_progress=on_progress_gen,
                    on_face_done=on_face_done,
                )
            except ValueError as exc:
                status.error(str(exc))
                result = None

            if result is not None:
                if result.failed:
                    status.warning(f"Finished with {len(result.failed)} failure(s)")
                    for msg in result.failed:
                        st.text(msg)
                elif result.wrote:
                    status.success(
                        f"Done — {len(result.wrote)} image(s) in {output_dir}"
                    )
                else:
                    status.error("Nothing was written.")

                # Persist gallery into the open project so Load restores comparisons.
                name = (st.session_state.get("project_name") or "").strip()
                if name and st.session_state.gallery:
                    try:
                        pid = save_project(
                            name,
                            import_decklist_text=st.session_state.decklist_text or "",
                            settings=settings_from_session(),
                            gallery=list(st.session_state.gallery),
                            project_id=st.session_state.get("project_id"),
                        )
                        st.session_state.project_id = pid
                        status.caption(f"Project “{name}” updated with gallery.")
                    except ValueError:
                        pass

    _draw_gallery(
        gallery_slot,
        show_regen=True,
        page_size=page_size,
    )
    persist_decklist_widgets()
