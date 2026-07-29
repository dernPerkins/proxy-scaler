"""PDF Generation tab: lay out generated card images onto print-ready sheets."""

from __future__ import annotations

import streamlit as st

from proxy_scaler.decklist import parse_decklist_text
from proxy_scaler.dpi import DEFAULT_DPI
from proxy_scaler.pdf_layout import (
    PAGE_SIZE_PRESETS_MM,
    build_pdf,
    expand_print_slots,
    match_quantities,
    paginate,
    resolve_page_layout,
)
from proxy_scaler.pipeline import FaceResult
from proxy_scaler.ui.projects import persist_pdf_widgets
from proxy_scaler.upscale import UpscaleModel

_EXPORT_DPI_OPTIONS = (800, 1200, 1401)


def _apply_page_size_preset() -> None:
    """Preset dropdown -> width/height fields (unless already Custom)."""
    preset = st.session_state.pdf_page_size_preset
    if preset == "Custom":
        return
    w, h = PAGE_SIZE_PRESETS_MM[preset.lower()]
    if st.session_state.pdf_orientation == "Landscape":
        w, h = h, w
    st.session_state.pdf_page_width_mm = w
    st.session_state.pdf_page_height_mm = h


def _mark_custom_page_size() -> None:
    """Typing a width/height directly means the preset no longer applies."""
    st.session_state.pdf_page_size_preset = "Custom"


def _apply_orientation_swap() -> None:
    """Swap width/height so they satisfy the newly-selected orientation."""
    w, h = st.session_state.pdf_page_width_mm, st.session_state.pdf_page_height_mm
    wants_portrait = st.session_state.pdf_orientation == "Portrait"
    if (w <= h) != wants_portrait:
        st.session_state.pdf_page_width_mm, st.session_state.pdf_page_height_mm = h, w


def render_pdf_tab(*, active: bool = True) -> None:
    """PDF layout/export UI. Settings live in the tab body itself (not the
    sidebar) — unlike proxxied (a comparable tool this mirrors), this app
    has no live preview to justify a sidebar. Set active=False when this tab
    is hidden — its widgets won't render, but values are still persisted
    (see persist_pdf_widgets) so they survive the round trip back."""
    items = [FaceResult.from_dict(d) for d in st.session_state.gallery]

    if active:
        st.subheader("PDF Generation")

        st.markdown("**Page size**")
        preset_col, w_col, h_col = st.columns(3)
        with preset_col:
            st.selectbox(
                "Page size",
                options=["Letter", "A4", "Custom"],
                key="pdf_page_size_preset",
                on_change=_apply_page_size_preset,
            )
        with w_col:
            st.number_input(
                "Page width (mm)",
                min_value=10.0,
                step=1.0,
                format="%.2f",
                key="pdf_page_width_mm",
                on_change=_mark_custom_page_size,
            )
        with h_col:
            st.number_input(
                "Page height (mm)",
                min_value=10.0,
                step=1.0,
                format="%.2f",
                key="pdf_page_height_mm",
                on_change=_mark_custom_page_size,
            )
        st.radio(
            "Orientation",
            ["Portrait", "Landscape"],
            key="pdf_orientation",
            horizontal=True,
            on_change=_apply_orientation_swap,
        )

        st.divider()
        st.markdown("**Grid**")
        cols_col, rows_col = st.columns(2)
        with cols_col:
            st.number_input(
                "Columns", min_value=1, step=1, key="pdf_cols",
                help="Cards per page, horizontally.",
            )
        with rows_col:
            st.number_input(
                "Rows", min_value=1, step=1, key="pdf_rows",
                help="Cards per page, vertically.",
            )

        st.divider()
        st.markdown("**Bleed & card spacing**")
        st.number_input(
            "Bleed width (mm)",
            min_value=0.0,
            step=0.5,
            format="%.2f",
            key="pdf_bleed_mm",
            help="Border extended past each card's trim edge (trimmed away after printing).",
        )
        spacing_x_col, spacing_y_col = st.columns(2)
        with spacing_x_col:
            st.number_input(
                "Card spacing horizontal (mm)",
                min_value=0.0,
                step=0.5,
                format="%.2f",
                key="pdf_spacing_x_mm",
                help="Extra gap between cards beyond their bleed, left-to-right.",
            )
        with spacing_y_col:
            st.number_input(
                "Card spacing vertical (mm)",
                min_value=0.0,
                step=0.5,
                format="%.2f",
                key="pdf_spacing_y_mm",
                help="Extra gap between cards beyond their bleed, top-to-bottom.",
            )

        st.divider()
        st.markdown("**Card position adjustment**")
        offset_x_col, offset_y_col = st.columns(2)
        with offset_x_col:
            st.number_input(
                "Horizontal offset (mm)",
                step=0.5,
                format="%.2f",
                key="pdf_offset_x_mm",
                help="Shifts the whole card grid left (negative) or right (positive) from centered.",
            )
        with offset_y_col:
            st.number_input(
                "Vertical offset (mm)",
                step=0.5,
                format="%.2f",
                key="pdf_offset_y_mm",
                help="Shifts the whole card grid up (negative) or down (positive) from centered.",
            )

        st.divider()
        st.markdown("**Guide lines**")
        guide_w_col, guide_l_col = st.columns(2)
        with guide_w_col:
            st.number_input(
                "Guide width (pt)",
                min_value=0.0,
                step=0.05,
                format="%.2f",
                key="pdf_guide_width_pt",
            )
        with guide_l_col:
            st.number_input(
                "Guide length (mm)",
                min_value=0.0,
                step=0.25,
                format="%.2f",
                key="pdf_guide_length_mm",
            )
        st.checkbox("Show cut guide lines", key="pdf_show_cut_lines")

        st.divider()
        st.markdown("**Source & export**")
        available_dpis = sorted({item.dpi for item in items}) or [DEFAULT_DPI]
        if st.session_state.get("pdf_image_dpi") not in available_dpis:
            st.session_state.pdf_image_dpi = (
                1200 if 1200 in available_dpis else max(available_dpis)
            )
        available_models = sorted({item.model for item in items})
        if st.session_state.get("pdf_preferred_model") not in available_models:
            st.session_state.pdf_preferred_model = (
                UpscaleModel.ULTRASHARP_V2.value
                if UpscaleModel.ULTRASHARP_V2.value in available_models
                else (available_models[0] if available_models else None)
            )
        dpi_col, model_col, export_col = st.columns(3)
        with dpi_col:
            st.selectbox(
                "Image DPI",
                options=available_dpis,
                format_func=lambda d: f"{d} DPI",
                key="pdf_image_dpi",
                help=(
                    "Which generated image resolution to source the PDF from. "
                    "A card missing this DPI falls back to its highest "
                    "available variant."
                ),
            )
        with model_col:
            st.selectbox(
                "Preferred model",
                options=available_models,
                format_func=lambda v: UpscaleModel(v).label,
                key="pdf_preferred_model",
                help=(
                    "Which model's output to use when a card has been "
                    "generated with more than one, at the same Image DPI."
                ),
            )
        with export_col:
            st.selectbox(
                "PDF export DPI",
                options=list(_EXPORT_DPI_OPTIONS),
                format_func=lambda d: f"{d} DPI",
                key="pdf_export_dpi",
                help=(
                    "Pixel density card art is embedded at, independent of "
                    "Image DPI — e.g. export a 1200 DPI source into an "
                    "800 DPI PDF for a smaller file."
                ),
            )

    persist_pdf_widgets()
    if not active:
        return

    if not items:
        st.info("No images yet — generate some in the Decklist tab first.")
        return

    entries = parse_decklist_text(st.session_state.decklist_text or "")
    units, unmatched = match_quantities(
        entries,
        items,
        preferred_dpi=int(st.session_state.pdf_image_dpi),
        preferred_model=st.session_state.get("pdf_preferred_model"),
    )
    dpi_fallback_count = sum(1 for u in units if u.dpi_fallback)

    layout = resolve_page_layout(
        page_w_mm=float(st.session_state.pdf_page_width_mm),
        page_h_mm=float(st.session_state.pdf_page_height_mm),
        cols=int(st.session_state.pdf_cols),
        rows=int(st.session_state.pdf_rows),
        bleed_mm=float(st.session_state.pdf_bleed_mm),
        spacing_x_mm=float(st.session_state.pdf_spacing_x_mm),
        spacing_y_mm=float(st.session_state.pdf_spacing_y_mm),
        offset_x_mm=float(st.session_state.pdf_offset_x_mm),
        offset_y_mm=float(st.session_state.pdf_offset_y_mm),
        guide_width_pt=float(st.session_state.pdf_guide_width_pt),
        guide_length_mm=float(st.session_state.pdf_guide_length_mm),
    )
    slots = expand_print_slots(units)
    pages = paginate(slots, layout.cards_per_page)

    st.divider()
    st.caption(
        f"{len(units)} unique card face(s) · {len(slots)} physical card(s) · "
        f"{len(pages)} page(s) at {layout.cols}×{layout.rows}/page "
        f"({layout.page_w_mm:.0f}×{layout.page_h_mm:.0f}mm {layout.orientation}, "
        f"margins {layout.margin_x_mm:.1f}mm / {layout.margin_y_mm:.1f}mm)"
    )
    if layout.grid_w_mm > layout.page_w_mm or layout.grid_h_mm > layout.page_h_mm:
        st.warning(
            f"The {layout.cols}×{layout.rows} grid "
            f"({layout.grid_w_mm:.1f}×{layout.grid_h_mm:.1f}mm) is larger than "
            f"the page ({layout.page_w_mm:.0f}×{layout.page_h_mm:.0f}mm) — "
            "cards near the edges may print off the page."
        )
    if unmatched:
        st.warning(
            f"{len(unmatched)} card face(s) had no quantity match in the "
            "current decklist text — defaulted to 1×."
        )
    if dpi_fallback_count:
        st.warning(
            f"{dpi_fallback_count} card face(s) don't have "
            f"{st.session_state.pdf_image_dpi} DPI generated — used their "
            "highest available DPI instead."
        )

    if st.button("Generate PDF", type="primary", disabled=not slots):
        with st.spinner(f"Rendering {len(pages)} page(s)…"):
            st.session_state._pdf_bytes = build_pdf(
                pages,
                layout=layout,
                export_dpi=int(st.session_state.pdf_export_dpi),
                show_cut_lines=bool(st.session_state.pdf_show_cut_lines),
            )
        st.session_state._pdf_filename = (
            f"proxies-{layout.page_w_mm:.0f}x{layout.page_h_mm:.0f}mm-"
            f"{layout.orientation}.pdf"
        )
        size_bytes = len(st.session_state._pdf_bytes)
        size_label = (
            f"{size_bytes / 1024 / 1024:.1f} MB"
            if size_bytes >= 1024 * 1024
            else f"{size_bytes / 1024:.0f} KB"
        )
        st.success(f"Generated {size_label} PDF.")

    pdf_bytes = st.session_state.get("_pdf_bytes")
    if pdf_bytes:
        st.download_button(
            "Download PDF",
            data=pdf_bytes,
            file_name=st.session_state._pdf_filename,
            mime="application/pdf",
            use_container_width=True,
        )

