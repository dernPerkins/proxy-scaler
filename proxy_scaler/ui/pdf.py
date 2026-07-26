"""PDF Generation tab placeholder."""

from __future__ import annotations

import streamlit as st


def render_pdf_tab() -> None:
    st.subheader("PDF Generation")
    st.info("PDF generation is coming soon. Use the Decklist tab to prepare images for now.")
    st.markdown(
        """
        Planned later:
        - Layout / bleed / cut guides
        - Export print-ready PDF sheets from project gallery images
        """
    )
