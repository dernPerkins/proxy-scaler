"""Project save/load chrome and session_state helpers."""

from __future__ import annotations

from pathlib import Path

import streamlit as st

from proxy_scaler.db import (
    ProjectSettings,
    delete_project,
    get_last_project_id,
    init_db,
    list_projects,
    load_project,
    save_project,
    set_last_project_id,
)
from proxy_scaler.dpi import DEFAULT_DPI
from proxy_scaler.upscale import UpscaleModel

ROOT = Path(__file__).resolve().parents[2]
DEFAULT_OUTPUT = str(ROOT / "output")
DEFAULT_CACHE = str(ROOT / "imgcache")
DEFAULT_WEIGHTS = str(ROOT / "weights")
DEFAULT_PAGE_SIZE = 6

# Bound to Streamlit widgets in the Decklist tab. When that tab is unmounted
# (user switches to PDF), Streamlit drops these keys — keep mirrors in
# `_persist_*` so we can restore them.
_DECKLIST_WIDGET_KEYS = (
    "decklist_text",
    "model",
    "dpi",
    "all_dpis",
    "dpi_row_mode",
    "page_size",
    "skip_existing",
    "output_dir",
    "cache_dir",
    "weights_dir",
)


def _persist_key(key: str) -> str:
    return f"_persist_{key}"


def persist_decklist_widgets() -> None:
    """Snapshot widget values so they survive Decklist tab unmount."""
    for key in _DECKLIST_WIDGET_KEYS:
        if key in st.session_state:
            st.session_state[_persist_key(key)] = st.session_state[key]


def restore_decklist_widgets() -> None:
    """Rehydrate widget keys wiped when leaving the Decklist tab."""
    for key in _DECKLIST_WIDGET_KEYS:
        mirror = _persist_key(key)
        if key not in st.session_state and mirror in st.session_state:
            st.session_state[key] = st.session_state[mirror]


def _session_setting(key: str, default=None):
    if key in st.session_state:
        return st.session_state[key]
    return st.session_state.get(_persist_key(key), default)


def ensure_session_defaults() -> None:
    """Initialize all project-related session_state keys once."""
    defaults: dict = {
        "decklist_text": (
            "1 Sol Ring (c21) 263\n"
            "1 Dion, Bahamut's Dominant // Bahamut, Warden of Light (fin) 376\n"
            "1 Sol Ring\n"
        ),
        "gallery": [],
        "regen_key": None,
        "gallery_page": 0,
        "project_id": None,
        "project_name": "",
        "model": UpscaleModel.SWINIR.value,
        "dpi": DEFAULT_DPI,
        "all_dpis": False,
        "dpi_row_mode": False,
        "page_size": DEFAULT_PAGE_SIZE,
        "skip_existing": True,
        "output_dir": DEFAULT_OUTPUT,
        "cache_dir": DEFAULT_CACHE,
        "weights_dir": DEFAULT_WEIGHTS,
        "save_as_name": "",
        "confirm_delete_project": False,
        "_pending_load_id": None,
        "_pending_new": False,
        "_pending_project_name": None,
    }
    # Restore widget keys Streamlit cleared on the previous (PDF) run before
    # applying first-time defaults — otherwise defaults would mask the mirror.
    restore_decklist_widgets()
    for key, value in defaults.items():
        if key not in st.session_state:
            st.session_state[key] = value
    persist_decklist_widgets()


def maybe_load_last_project() -> None:
    """On a fresh session, auto-load the last-used project (else most recent)."""
    if st.session_state.get("_initial_project_load_done"):
        return
    st.session_state._initial_project_load_done = True
    if st.session_state.get("project_id"):
        return

    projects = list_projects()
    if not projects:
        return

    valid_ids = {p.id for p in projects}
    last_id = get_last_project_id()
    target_id = last_id if last_id in valid_ids else projects[0].id
    st.session_state._pending_load_id = target_id


def apply_pending_project_actions() -> None:
    """Apply load/new before any keyed widgets render (Streamlit constraint)."""
    pending_id = st.session_state.get("_pending_load_id")
    if pending_id is not None:
        loaded = load_project(int(pending_id))
        apply_loaded_project(loaded)
        set_last_project_id(int(pending_id))
        st.session_state._pending_load_id = None

    if st.session_state.get("_pending_new"):
        reset_to_new_project()
        st.session_state._pending_new = False

    pending_name = st.session_state.get("_pending_project_name")
    if pending_name is not None:
        st.session_state.project_name = pending_name
        st.session_state._pending_project_name = None


def settings_from_session() -> ProjectSettings:
    return ProjectSettings(
        model=_session_setting("model", UpscaleModel.SWINIR.value),
        dpi=int(_session_setting("dpi", DEFAULT_DPI)),
        all_dpis=bool(_session_setting("all_dpis", False)),
        dpi_row_mode=bool(_session_setting("dpi_row_mode", False)),
        page_size=int(_session_setting("page_size", DEFAULT_PAGE_SIZE)),
        skip_existing=bool(_session_setting("skip_existing", True)),
        output_dir=str(_session_setting("output_dir", DEFAULT_OUTPUT)),
        cache_dir=str(_session_setting("cache_dir", DEFAULT_CACHE)),
        weights_dir=str(_session_setting("weights_dir", DEFAULT_WEIGHTS)),
    )


def apply_loaded_project(loaded) -> None:
    st.session_state.project_id = loaded.id
    st.session_state.project_name = loaded.name
    st.session_state.decklist_text = loaded.import_decklist_text
    st.session_state.gallery = loaded.gallery
    st.session_state.gallery_page = 0
    st.session_state.regen_key = None
    s = loaded.settings
    st.session_state.model = s.model
    st.session_state.dpi = s.dpi
    st.session_state.all_dpis = s.all_dpis
    st.session_state.dpi_row_mode = s.dpi_row_mode
    st.session_state.page_size = s.page_size
    st.session_state.skip_existing = s.skip_existing
    if s.output_dir:
        st.session_state.output_dir = s.output_dir
    if s.cache_dir:
        st.session_state.cache_dir = s.cache_dir
    if s.weights_dir:
        st.session_state.weights_dir = s.weights_dir
    persist_decklist_widgets()


def reset_to_new_project() -> None:
    st.session_state.project_id = None
    st.session_state.project_name = ""
    st.session_state.decklist_text = ""
    st.session_state.gallery = []
    st.session_state.gallery_page = 0
    st.session_state.regen_key = None
    st.session_state.model = UpscaleModel.SWINIR.value
    st.session_state.dpi = DEFAULT_DPI
    st.session_state.all_dpis = False
    st.session_state.dpi_row_mode = False
    st.session_state.page_size = DEFAULT_PAGE_SIZE
    st.session_state.skip_existing = True
    st.session_state.output_dir = DEFAULT_OUTPUT
    st.session_state.cache_dir = DEFAULT_CACHE
    st.session_state.weights_dir = DEFAULT_WEIGHTS
    persist_decklist_widgets()


def _do_save(*, name: str, project_id: int | None) -> None:
    """Persist current session; raises ValueError on validation errors."""
    pid = save_project(
        name,
        import_decklist_text=_session_setting("decklist_text", "") or "",
        settings=settings_from_session(),
        gallery=list(st.session_state.gallery or []),
        project_id=project_id,
    )
    st.session_state.project_id = pid
    set_last_project_id(pid)


def render_project_bar() -> None:
    """Save / Load / New / Delete controls above tabs."""
    init_db()
    projects = list_projects()
    options = {f"{p.name} (#{p.id})": p.id for p in projects}

    # Shared column widths so Save/Load and Save As/Delete stack cleanly
    _W = [4.5, 1.0, 1.0, 1.3]

    status = (
        f"saved · #{st.session_state.project_id}"
        if st.session_state.project_id
        else "unsaved"
    )
    st.markdown(f"**Project**&nbsp;&nbsp;·&nbsp;&nbsp;{status}", unsafe_allow_html=True)

    name_col, save_col, new_col, as_col = st.columns(_W, vertical_alignment="bottom")
    with name_col:
        st.text_input(
            "Name",
            key="project_name",
            placeholder="Project name",
            label_visibility="collapsed",
            help="Name used when saving this project",
        )
    with save_col:
        if st.button("Save", use_container_width=True, type="primary"):
            name = (st.session_state.project_name or "").strip()
            if not name:
                st.error("Enter a project name before saving.")
            else:
                try:
                    _do_save(name=name, project_id=st.session_state.project_id)
                    st.toast(f"Saved “{name}”")
                except ValueError as exc:
                    st.error(str(exc))
    with new_col:
        if st.button("New", use_container_width=True):
            st.session_state._pending_new = True
            st.rerun()
    with as_col:
        with st.popover("Save As…", use_container_width=True):
            st.text_input("Save as", key="save_as_name", placeholder="New project name")
            if st.button("Save copy", use_container_width=True, type="primary"):
                name = (st.session_state.save_as_name or "").strip()
                if not name:
                    st.error("Name required.")
                else:
                    try:
                        _do_save(name=name, project_id=None)
                        st.session_state._pending_project_name = name
                        st.rerun()
                    except ValueError as exc:
                        st.error(str(exc))

    if options:
        load_col, go_col, _spacer, del_col = st.columns(
            _W, vertical_alignment="bottom"
        )
        with load_col:
            choice = st.selectbox(
                "Load project",
                options=["Select a project…"] + list(options.keys()),
                key="load_project_choice",
                label_visibility="collapsed",
                help="Load a previously saved project",
            )
        with go_col:
            if st.button("Load", use_container_width=True):
                if choice not in options:
                    st.warning("Pick a project to load.")
                else:
                    st.session_state._pending_load_id = options[choice]
                    st.rerun()
        with del_col:
            with st.popover("Delete…", use_container_width=True):
                if choice not in options:
                    st.caption("Select a project above first.")
                else:
                    st.caption(f"Permanently delete **{choice}** from the database?")
                    st.caption("Image files on disk are not removed.")
                    confirm = st.checkbox(
                        "I understand",
                        key="confirm_delete_project",
                    )
                    if st.button(
                        "Delete permanently",
                        use_container_width=True,
                        type="primary",
                        disabled=not confirm,
                    ):
                        deleted_id = options[choice]
                        delete_project(deleted_id)
                        if st.session_state.project_id == deleted_id:
                            st.session_state._pending_new = True
                        st.rerun()
    else:
        st.caption("No saved projects yet — enter a name and click Save.")
