"""Streamlit shell: projects bar + Decklist / PDF tabs."""

from __future__ import annotations

import streamlit as st

from proxy_scaler.db import init_db
from proxy_scaler.ui.decklist import render_decklist_tab
from proxy_scaler.ui.pdf import render_pdf_tab
from proxy_scaler.ui.projects import (
    apply_pending_project_actions,
    ensure_session_defaults,
    maybe_load_last_project,
    render_project_bar,
)

_TAB_DECKLIST = "decklist"
_TAB_PDF = "pdf"
_LABEL_DECKLIST = "Decklist"
_LABEL_PDF = "PDF Generation"
_PARAM_TO_LABEL = {
    _TAB_DECKLIST: _LABEL_DECKLIST,
    _TAB_PDF: _LABEL_PDF,
}
_LABEL_TO_PARAM = {
    _LABEL_DECKLIST: _TAB_DECKLIST,
    _LABEL_PDF: _TAB_PDF,
}


def _normalize_tab(raw: str | None) -> str:
    value = (raw or _TAB_DECKLIST).strip().lower()
    if value in _PARAM_TO_LABEL:
        return value
    return _TAB_DECKLIST


def _on_tab_change() -> None:
    """Keep ?tab= in sync when the user clicks a tab (no state fighting)."""
    label = st.session_state.get("ui_tab", _LABEL_DECKLIST)
    param = _LABEL_TO_PARAM.get(label, _TAB_DECKLIST)
    st.query_params["tab"] = param
    st.session_state["_url_tab"] = param


def main() -> None:
    st.set_page_config(page_title="proxy-scaler", layout="wide")
    init_db()
    ensure_session_defaults()
    maybe_load_last_project()
    apply_pending_project_actions()

    st.title("MTG Proxy Upscaler")
    st.caption(
        "Paste a decklist, pick model + DPI, review original vs upscaled, "
        "regenerate any you dislike. Save projects to SQLite for later."
    )

    render_project_bar()
    st.divider()

    url_tab = _normalize_tab(st.query_params.get("tab"))
    desired_label = _PARAM_TO_LABEL[url_tab]

    # Seed tab from URL once; only overwrite on external URL changes (back/forward).
    if "ui_tab" not in st.session_state:
        st.session_state.ui_tab = desired_label
        st.session_state["_url_tab"] = url_tab
    elif st.session_state.get("_url_tab") != url_tab:
        # Query string changed without a matching tab click (browser nav / deep link).
        st.session_state.ui_tab = desired_label
        st.session_state["_url_tab"] = url_tab

    tab_deck, tab_pdf = st.tabs(
        [_LABEL_DECKLIST, _LABEL_PDF],
        default=desired_label,
        key="ui_tab",
        on_change=_on_tab_change,
    )

    # Always run both tab bodies so Decklist widgets stay mounted when PDF is
    # selected (otherwise Streamlit drops their session_state keys).
    with tab_deck:
        render_decklist_tab(draw_gallery=bool(tab_deck.open))
    with tab_pdf:
        render_pdf_tab()


if __name__ == "__main__":
    main()
