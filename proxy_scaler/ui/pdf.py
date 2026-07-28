"""PDF Generation tab: lay out generated card images onto print-ready sheets."""

from __future__ import annotations

import streamlit as st

from proxy_scaler.decklist import parse_decklist_text
from proxy_scaler.dpi import DEFAULT_DPI
from proxy_scaler.pdf_layout import (
    build_pdf,
    expand_print_slots,
    match_quantities,
    paginate,
    resolve_page_layout,
)
from proxy_scaler.pipeline import FaceResult
from proxy_scaler.ui.projects import persist_pdf_widgets


def render_pdf_tab(*, active: bool = True) -> None:
    """PDF layout/export UI. Set active=False when this tab is hidden — its
    own sidebar settings won't render, but widget values are still persisted
    (see persist_pdf_widgets) so they survive the round trip back."""
    items = [FaceResult.from_dict(d) for d in st.session_state.gallery]

    if active:
        with st.sidebar:
            st.header("Settings")
            available_dpis = sorted({item.dpi for item in items}) or [DEFAULT_DPI]
            if st.session_state.get("pdf_image_dpi") not in available_dpis:
                st.session_state.pdf_image_dpi = (
                    1200 if 1200 in available_dpis else max(available_dpis)
                )
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
            st.radio(
                "Orientation",
                ["Landscape", "Portrait"],
                key="pdf_orientation",
                horizontal=True,
                help="Landscape = 4x2 cards/page, Portrait = 3x3 cards/page.",
            )
            st.radio("Paper size", ["Letter", "A4"], key="pdf_paper", horizontal=True)
            st.checkbox("Show cut guide lines", key="pdf_show_cut_lines")

    persist_pdf_widgets()
    if not active:
        return

    st.subheader("PDF Generation")
    if not items:
        st.info("No images yet — generate some in the Decklist tab first.")
        return

    entries = parse_decklist_text(st.session_state.decklist_text or "")
    units, unmatched = match_quantities(
        entries, items, preferred_dpi=int(st.session_state.pdf_image_dpi)
    )
    dpi_fallback_count = sum(1 for u in units if u.dpi_fallback)

    layout = resolve_page_layout(
        orientation=st.session_state.pdf_orientation,
        paper=st.session_state.pdf_paper,
    )
    slots = expand_print_slots(units)
    pages = paginate(slots, layout.cards_per_page)

    st.caption(
        f"{len(units)} unique card face(s) · {len(slots)} physical card(s) · "
        f"{len(pages)} page(s) at {layout.cols}×{layout.rows}/page "
        f"({layout.paper.upper()} {layout.orientation}, "
        f"margins {layout.margin_x_mm:.1f}mm / {layout.margin_y_mm:.1f}mm)"
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
                show_cut_lines=bool(st.session_state.pdf_show_cut_lines),
            )
        st.session_state._pdf_filename = (
            f"proxies-{layout.paper}-{layout.orientation}.pdf"
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
