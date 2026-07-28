"""Streamlit shell: projects bar + Decklist / PDF tabs."""

from __future__ import annotations

import streamlit as st

from proxy_scaler.db import init_db
from proxy_scaler.ui.decklist import render_decklist_tab, render_global_sidebar_actions
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
    """Keep ?tab= in sync when the user clicks a tab (URL bookmarking only —
    st.tabs()'s own `key` binding is the source of truth for which tab is
    selected; this just mirrors it into the URL for deep links)."""
    label = st.session_state.get("ui_tab", _LABEL_DECKLIST)
    param = _LABEL_TO_PARAM.get(label, _TAB_DECKLIST)
    st.query_params["tab"] = param


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

    # Seed the initial tab from the URL once (deep links like ?tab=pdf).
    # After that, st.tabs()'s own `key` binding is authoritative — clicking
    # a tab updates st.session_state.ui_tab synchronously with no round
    # trip. Re-deriving it from a fresh st.query_params read on every run
    # would race a just-registered click (query params sync through the
    # browser URL bar, session_state does not), which could silently snap
    # the tab back and require a second click to actually stick.
    if "ui_tab" not in st.session_state:
        st.session_state.ui_tab = _PARAM_TO_LABEL[_normalize_tab(st.query_params.get("tab"))]

    tab_deck, tab_pdf = st.tabs(
        [_LABEL_DECKLIST, _LABEL_PDF],
        key="ui_tab",
        on_change=_on_tab_change,
    )

    # Always run both tab bodies (their own active-tab checks gate whether
    # each renders its sidebar section / main body this run — see
    # persist_decklist_widgets/persist_pdf_widgets for how widget state
    # survives being unmounted while the other tab is active).
    with tab_deck:
        render_decklist_tab(draw_gallery=bool(tab_deck.open))
    with tab_pdf:
        render_pdf_tab(active=bool(tab_pdf.open))
    render_global_sidebar_actions()


if __name__ == "__main__":
    main()
