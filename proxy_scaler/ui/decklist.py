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
from proxy_scaler.ui.projects import settings_from_session, persist_decklist_widgets
from proxy_scaler.upscale import UpscaleModel

ROOT = Path(__file__).resolve().parents[2]
EXAMPLE_PATH = ROOT / "cards.example.txt"

_PREVIEW_MAX_EDGE = 900


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


def _provenance_label(item: FaceResult) -> str:
    """Compact model / DPI / device line for gallery headers."""
    device = (item.device or "unknown").lower()
    device_bit = {"gpu": "GPU", "cpu": "CPU"}.get(device, "device?")
    return f"**{item.model}** · **{item.dpi} DPI** · **{device_bit}**"


def _lazy_image(path: Path, *, max_edge: int = _PREVIEW_MAX_EDGE) -> None:
    """Render a browser-lazy-loaded preview (downscaled for UI speed)."""
    if not path.is_file():
        st.warning("missing")
        return
    try:
        with Image.open(path) as im:
            full_w, full_h = im.size
            rgb = im.convert("RGB")
            if max(rgb.size) > max_edge:
                rgb.thumbnail((max_edge, max_edge), Image.Resampling.LANCZOS)
            buf = io.BytesIO()
            rgb.save(buf, format="JPEG", quality=82, optimize=True)
        b64 = base64.b64encode(buf.getvalue()).decode("ascii")
        st.markdown(
            f'<img src="data:image/jpeg;base64,{b64}" '
            f'loading="lazy" decoding="async" '
            f'style="width:100%;height:auto;display:block;" '
            f'alt="{path.name}" />',
            unsafe_allow_html=True,
        )
        st.caption(f"preview {rgb.size[0]}×{rgb.size[1]} (file {full_w}×{full_h})")
    except OSError as exc:
        st.warning(f"Could not load {path.name}: {exc}")


def _render_single_comparison(item: FaceResult) -> None:
    label = item.face_name
    if item.face_label:
        label = f"{label} ({item.face_label})"
    st.markdown(
        f"**{label}** — `{item.set_code.upper()}/{item.collector_number}` "
        f"· {_provenance_label(item)}"
    )
    left, right = st.columns(2)
    with left:
        st.caption("Original (Scryfall ~300 DPI)")
        _lazy_image(item.original_path)
    with right:
        st.caption(f"Upscaled ({item.dpi} DPI) · `{item.out_path.name}`")
        _lazy_image(item.out_path)


def _render_dpi_row(items: list[FaceResult]) -> None:
    """Original + each DPI variant in one row."""
    first = items[0]
    label = first.face_name
    if first.face_label:
        label = f"{label} ({first.face_label})"
    st.markdown(
        f"**{label}** — `{first.set_code.upper()}/{first.collector_number}` "
        f"· **{first.model}**"
    )
    cols = st.columns(1 + len(items))
    with cols[0]:
        st.caption("Original ~300 DPI")
        _lazy_image(first.original_path)
    for col, item in zip(cols[1:], items):
        with col:
            device = (item.device or "unknown").lower()
            device_bit = {"gpu": "GPU", "cpu": "CPU"}.get(device, "?")
            st.caption(f"{item.dpi} DPI · {device_bit}")
            _lazy_image(item.out_path)


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
    dpi_row_mode: bool,
    page_size: int,
    jump_to_last: bool = False,
) -> None:
    items = _gallery_items()
    with slot.container():
        st.subheader(f"Comparisons ({len(items)} image(s))")
        if not items:
            st.write("No images yet. Paste a list and click Generate.")
            return

        if dpi_row_mode:
            entries: list = _group_by_face(items)
            unit = "card face(s)"
        else:
            entries = items
            unit = "comparison(s)"

        total_pages = max(1, (len(entries) + page_size - 1) // page_size)
        if jump_to_last:
            st.session_state.gallery_page = total_pages - 1
        page = int(st.session_state.gallery_page)
        page = max(0, min(page, total_pages - 1))
        st.session_state.gallery_page = page

        # Live redraws during Generate call this many times in one run — only the
        # final pass (show_regen=True) may create keyed widgets, or Streamlit errors.
        if show_regen:
            nav_l, nav_m, nav_r = st.columns([1, 2, 1])
            with nav_l:
                if st.button("← Prev", disabled=page <= 0, key="gallery_prev"):
                    st.session_state.gallery_page = page - 1
                    st.rerun()
            with nav_m:
                st.markdown(
                    f"<div style='text-align:center'>Page <b>{page + 1}</b> / {total_pages} "
                    f"· showing {page_size} {unit} per page</div>",
                    unsafe_allow_html=True,
                )
            with nav_r:
                if st.button(
                    "Next →",
                    disabled=page >= total_pages - 1,
                    key="gallery_next",
                ):
                    st.session_state.gallery_page = page + 1
                    st.rerun()
        else:
            st.caption(f"Page {page + 1} / {total_pages} · generating…")

        page_entries, _ = _paginate(entries, page, page_size)

        if dpi_row_mode:
            for _group_key, face_items in page_entries:
                st.divider()
                _render_dpi_row(face_items)
                if show_regen:
                    regen_cols = st.columns(len(face_items))
                    for col, item in zip(regen_cols, face_items):
                        with col:
                            c1, c2 = st.columns(2)
                            with c1:
                                if st.button(
                                    "Compare",
                                    key=f"cmp-{_item_key(item)}",
                                    use_container_width=True,
                                ):
                                    open_comparison_dialog(item)
                            with c2:
                                if st.button(
                                    f"Regen {item.dpi}",
                                    key=f"regen-{_item_key(item)}",
                                    use_container_width=True,
                                ):
                                    st.session_state.regen_key = _item_key(item)
                                    st.rerun()
            return

        for item in page_entries:
            st.divider()
            _render_single_comparison(item)
            if show_regen:
                c1, c2, _ = st.columns([1, 1, 4])
                with c1:
                    if st.button(
                        "Compare",
                        key=f"cmp-{_item_key(item)}",
                        use_container_width=True,
                    ):
                        open_comparison_dialog(item)
                with c2:
                    if st.button(
                        "Regenerate",
                        key=f"regen-{_item_key(item)}",
                        use_container_width=True,
                    ):
                        st.session_state.regen_key = _item_key(item)
                        st.rerun()


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
        st.checkbox(
            "Generate all DPIs (600 + 800 + 1200)",
            key="all_dpis",
            help="One upscale pass family per face, then resize to each target DPI.",
        )
        st.radio(
            "Target DPI",
            options=list(DPI_OPTIONS),
            format_func=lambda d: f"{d} DPI",
            horizontal=True,
            key="dpi",
            disabled=bool(st.session_state.all_dpis),
        )
        st.toggle(
            "Show all DPIs in one row",
            key="dpi_row_mode",
            help="Group variants of the same card face for side-by-side DPI comparison.",
        )
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
            for note in notes:
                st.write(note)
            st.success("Generated data cleared.")
            st.rerun()

    model = st.session_state.model
    all_dpis = bool(st.session_state.all_dpis)
    dpi = int(st.session_state.dpi)
    dpi_row_mode = bool(st.session_state.dpi_row_mode)
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
        if match is not None:
            status.info(
                f"Regenerating {match.face_name} with {model} @ {dpi} DPI…"
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
                    dpi=dpi if not all_dpis else match.dpi,
                    model=model,
                    on_progress=on_progress,
                )
                _upsert_gallery(updated)
                status.success(
                    f"Regenerated {updated.face_name} "
                    f"({updated.model} {updated.dpi} DPI)"
                )
            except Exception as exc:  # noqa: BLE001
                status.error(f"Regenerate failed: {exc}")
        st.session_state.regen_key = None

    if run:
        entries = parse_decklist_text(st.session_state.decklist_text)
        if not entries:
            status.error("No card entries found in the decklist.")
        else:
            st.session_state.gallery = []
            st.session_state.gallery_page = 0
            lines: list[str] = []
            target_msg = (
                "all DPIs (600/800/1200)"
                if all_dpis
                else f"{dpi} DPI"
            )
            status.info(
                f"Parsed {len(entries)} line(s) with {model} @ {target_msg}. "
                "Comparisons appear as each finishes…"
            )
            _draw_gallery(
                gallery_slot,
                show_regen=False,
                dpi_row_mode=dpi_row_mode,
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
                    dpi_row_mode=dpi_row_mode,
                    page_size=page_size,
                    jump_to_last=True,
                )

            try:
                result = process_entries(
                    entries,
                    output_dir=output_dir,
                    dpi=dpi,
                    all_dpis=all_dpis,
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
        dpi_row_mode=dpi_row_mode,
        page_size=page_size,
    )
    persist_decklist_widgets()
