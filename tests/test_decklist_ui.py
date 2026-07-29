"""AppTest coverage for decklist.py's regen-enqueue guards."""

from __future__ import annotations

from streamlit.testing.v1 import AppTest

from proxy_scaler import db


def _regen_script() -> None:
    from pathlib import Path

    import streamlit as st

    from proxy_scaler.pipeline import FaceResult
    from proxy_scaler.ui.decklist import _item_key, render_decklist_tab
    from proxy_scaler.ui.projects import ensure_session_defaults

    ensure_session_defaults()
    item = FaceResult(
        out_path=Path("/o/Sol_Ring-C21-263-swinir-800dpi.png"),
        original_path=Path("/c/Sol_Ring-C21-263.png"),
        scryfall_id="",
        face_index=None,
        face_name="Sol Ring",
        card_name="Sol Ring",
        set_code="c21",
        collector_number="263",
        png_url="",  # disk-recovered — no known Scryfall origin
        dpi=800,
        model="swinir",
    )
    st.session_state.gallery = [item.to_dict()]
    st.session_state.regen_key = _item_key(item)
    render_decklist_tab(draw_gallery=False)


def test_regen_with_empty_png_url_shows_error_and_does_not_enqueue(
    tmp_path, monkeypatch
) -> None:
    db_path = tmp_path / "test.db"
    db.init_db(db_path)
    monkeypatch.setattr(db, "DEFAULT_DB_PATH", db_path)

    enqueue_calls: list[tuple] = []
    monkeypatch.setattr(
        db, "enqueue_task", lambda *a, **kw: enqueue_calls.append((a, kw)) or 1
    )

    at = AppTest.from_function(_regen_script)
    at.run()

    assert not at.exception
    assert enqueue_calls == []
    assert any("No known source image" in e.value for e in at.error)


def _bulk_sync_script() -> None:
    from pathlib import Path

    from proxy_scaler.db import (
        ProjectSettings,
        TaskRow,
        add_cards_to_project,
        save_project,
        upsert_gallery_item_for_task,
    )
    from proxy_scaler.pipeline import FaceResult
    from proxy_scaler.ui.decklist import _sync_gallery_from_db
    from proxy_scaler.ui.projects import ensure_session_defaults
    import streamlit as st

    pid = save_project(
        "Bulk Sync Test",
        import_decklist_text="1 Sol Ring (c21) 263\n",
        settings=ProjectSettings(),
    )
    add_cards_to_project(
        pid,
        [
            {
                "scryfall_id": "sol-id",
                "card_name": "Sol Ring",
                "set_code": "c21",
                "collector_number": "263",
                "quantity": 1,
                "original_import_line": "1 Sol Ring (c21) 263",
            }
        ],
    )
    item = FaceResult(
        out_path=Path("/o/Sol_Ring-C21-263-swinir-800dpi.png"),
        original_path=Path("/c/Sol_Ring-C21-263.png"),
        scryfall_id="sol-id",
        face_index=None,
        face_name="Sol Ring",
        card_name="Sol Ring",
        set_code="c21",
        collector_number="263",
        png_url="https://example.com/sol.png",
        dpi=800,
        model="swinir",
    )
    task = TaskRow(
        id=1,
        project_id=pid,
        status="done",
        scryfall_id="sol-id",
        face_index=None,
        face_label=None,
        face_name="Sol Ring",
        card_name="Sol Ring",
        set_code="c21",
        collector_number="263",
        png_url="https://example.com/sol.png",
        dpi=800,
        model="swinir",
        tile_size=0,
        output_dir="/tmp/out",
        cache_dir="/tmp/cache",
        weights_dir="/tmp/weights",
        error=None,
        created_at="2026-01-01T00:00:00Z",
        started_at=None,
        completed_at=None,
    )
    upsert_gallery_item_for_task(task, item)

    ensure_session_defaults()
    st.session_state.project_id = pid
    # Simulate two fragment ticks — the regression was that this alone
    # (with no user interaction) silently marked every row "loaded".
    _sync_gallery_from_db()
    _sync_gallery_from_db()


def test_bulk_db_sync_never_marks_rows_loaded(tmp_path, monkeypatch) -> None:
    db_path = tmp_path / "test.db"
    from proxy_scaler import db

    db.init_db(db_path)
    monkeypatch.setattr(db, "DEFAULT_DB_PATH", db_path)

    at = AppTest.from_function(_bulk_sync_script)
    at.run()

    assert not at.exception
    assert at.session_state["gallery"]
    assert at.session_state["loaded_faces"] == set()


def _task_only_row_script() -> None:
    import streamlit as st

    from proxy_scaler import db
    from proxy_scaler.ui.decklist import render_decklist_tab
    from proxy_scaler.ui.projects import ensure_session_defaults

    pid = db.save_project(
        "Task Only Row Test",
        import_decklist_text="1 Sol Ring (c21) 263\n",
        settings=db.ProjectSettings(),
    )
    db.add_cards_to_project(
        pid,
        [
            {
                "scryfall_id": "sol-id",
                "card_name": "Sol Ring",
                "set_code": "c21",
                "collector_number": "263",
                "quantity": 1,
                "original_import_line": "1 Sol Ring (c21) 263",
            }
        ],
    )
    # A pending task for this card — no gallery item exists yet, so this
    # is the only source of status for its row.
    db.enqueue_task(
        pid,
        scryfall_id="sol-id",
        face_index=None,
        face_label=None,
        face_name="Sol Ring",
        card_name="Sol Ring",
        set_code="c21",
        collector_number="263",
        png_url="https://example.com/sol.png",
        dpi=800,
        model="swinir",
        output_dir="/tmp/out",
        cache_dir="/tmp/cache",
        weights_dir="/tmp/weights",
    )
    ensure_session_defaults()
    st.session_state.project_id = pid
    render_decklist_tab(draw_gallery=True)


def test_task_only_row_shows_status_without_image(tmp_path, monkeypatch) -> None:
    db_path = tmp_path / "test.db"
    from proxy_scaler import db

    db.init_db(db_path)
    monkeypatch.setattr(db, "DEFAULT_DB_PATH", db_path)

    at = AppTest.from_function(_task_only_row_script)
    at.run()

    assert not at.exception
    captions = [c.value for c in at.caption]
    assert any("pending" in c for c in captions)
    assert not any("<img" in m.value for m in at.markdown)


def _import_flow_script() -> None:
    import streamlit as st

    from proxy_scaler.ui.decklist import render_decklist_tab
    from proxy_scaler.ui.projects import ensure_session_defaults

    ensure_session_defaults()
    st.session_state.project_name = "Import Flow Test"
    st.session_state.decklist_text = "1 Sol Ring (c21) 263\n"
    render_decklist_tab(draw_gallery=True)


def test_import_dedup_then_remove_flow(tmp_path, monkeypatch) -> None:
    """End-to-end through the real UI: Import creates a project_cards row,
    re-Import is a no-op (merge, not duplicate), Remove deletes it."""
    db_path = tmp_path / "test.db"
    db.init_db(db_path)
    monkeypatch.setattr(db, "DEFAULT_DB_PATH", db_path)

    sol_ring_card = {
        "id": "sol-id",
        "name": "Sol Ring",
        "set": "c21",
        "collector_number": "263",
        "image_status": "highres_scan",
        "image_uris": {"png": "https://example.com/sol.png"},
    }

    def fake_resolve_many(self, entries):
        return [(sol_ring_card, []) for _ in entries]

    monkeypatch.setattr(
        "proxy_scaler.scryfall.ScryfallClient.resolve_many", fake_resolve_many
    )

    at = AppTest.from_function(_import_flow_script)
    at.run()
    assert not at.exception

    import_btn = next(b for b in at.button if b.label == "Import cards")
    import_btn.click().run()
    assert not at.exception

    pid = at.session_state["project_id"]
    assert pid is not None
    cards = db.list_project_cards(pid, db_path=db_path)
    assert len(cards) == 1
    assert cards[0].card_name == "Sol Ring"

    # Re-importing the same text merges, doesn't duplicate.
    next(b for b in at.button if b.label == "Import cards").click().run()
    assert not at.exception
    assert len(db.list_project_cards(pid, db_path=db_path)) == 1

    # Remove itself uses st.rerun(scope="fragment") (see _render_card_row)
    # so a live click doesn't reset scroll position on a long list —
    # AppTest can't simulate a fragment-scoped rerun triggered by a click
    # at all (confirmed: even a single non-nested @st.fragment button
    # raises "scope='fragment' can only be specified ... during fragment
    # reruns" under AppTest's .click().run()), so the backend effect is
    # verified directly here instead of via a simulated click; the row's
    # own hide-on-remove render logic is covered by
    # test_removed_card_hidden_from_table below.
    [card] = db.list_project_cards(pid, db_path=db_path)
    db.remove_project_card(card.id, db_path=db_path)
    assert db.list_project_cards(pid, db_path=db_path) == []


def _bulk_generate_skips_active_script() -> None:
    import streamlit as st

    from proxy_scaler import db
    from proxy_scaler.ui.decklist import render_decklist_tab
    from proxy_scaler.ui.projects import ensure_session_defaults

    # A click on "Generate upscaled images" triggers an st.rerun(), which
    # re-executes this whole script function from the top — so one-time DB
    # setup (especially enqueue_task, which isn't idempotent) must only run
    # once, not on every rerun, or it'd create fresh duplicate tasks itself.
    if "test_setup_done" not in st.session_state:
        pid = db.save_project(
            "Active Task Skip Test",
            import_decklist_text="1 Sol Ring (c21) 263\n",
            settings=db.ProjectSettings(),
        )
        db.add_cards_to_project(
            pid,
            [
                {
                    "scryfall_id": "sol-id",
                    "card_name": "Sol Ring",
                    "set_code": "c21",
                    "collector_number": "263",
                    "quantity": 1,
                    "original_import_line": "1 Sol Ring (c21) 263",
                }
            ],
        )
        # A task already pending for this exact (face, dpi, model) — bulk
        # generate must not queue a duplicate for it.
        db.enqueue_task(
            pid,
            scryfall_id="sol-id",
            face_index=None,
            face_label=None,
            face_name="Sol Ring",
            card_name="Sol Ring",
            set_code="c21",
            collector_number="263",
            png_url="https://example.com/sol.png",
            dpi=800,
            model="swinir",
            output_dir="/tmp/out",
            cache_dir="/tmp/cache",
            weights_dir="/tmp/weights",
        )
        st.session_state.project_id = pid
        st.session_state.test_setup_done = True

    ensure_session_defaults()
    st.session_state.model = "swinir"
    st.session_state.dpi_800 = True
    st.session_state.dpi_600 = False
    st.session_state.dpi_1200 = False
    render_decklist_tab(draw_gallery=True)


def test_bulk_generate_skips_cards_with_active_task(tmp_path, monkeypatch) -> None:
    """The bulk "Generate upscaled images" button must not queue a
    duplicate task for a (face, dpi, model) that already has one
    pending/running — only genuinely missing images get queued."""
    db_path = tmp_path / "test.db"
    db.init_db(db_path)
    monkeypatch.setattr(db, "DEFAULT_DB_PATH", db_path)

    sol_ring_card = {
        "id": "sol-id",
        "name": "Sol Ring",
        "set": "c21",
        "collector_number": "263",
        "image_status": "highres_scan",
        "image_uris": {"png": "https://example.com/sol.png"},
    }

    def fake_resolve_many(self, entries):
        return [(sol_ring_card, []) for _ in entries]

    monkeypatch.setattr(
        "proxy_scaler.scryfall.ScryfallClient.resolve_many", fake_resolve_many
    )

    at = AppTest.from_function(_bulk_generate_skips_active_script)
    at.run()
    assert not at.exception

    next(b for b in at.button if b.label == "Generate upscaled images").click().run()
    assert not at.exception

    pid = at.session_state["project_id"]
    tasks = db.list_tasks(project_id=pid, db_path=db_path)
    # Still exactly the one pre-existing task — nothing duplicated.
    assert len(tasks) == 1
    assert any("Nothing to do" in i.value for i in at.info)


def _removed_card_hidden_script() -> None:
    import streamlit as st

    from proxy_scaler import db
    from proxy_scaler.ui.decklist import render_decklist_tab
    from proxy_scaler.ui.projects import ensure_session_defaults

    pid = db.save_project(
        "Removed Card Hidden Test",
        import_decklist_text="1 Sol Ring (c21) 263\n",
        settings=db.ProjectSettings(),
    )
    db.add_cards_to_project(
        pid,
        [
            {
                "scryfall_id": "sol-id",
                "card_name": "Sol Ring",
                "set_code": "c21",
                "collector_number": "263",
                "quantity": 1,
                "original_import_line": "1 Sol Ring (c21) 263",
            }
        ],
    )
    [card] = db.list_project_cards(pid)
    ensure_session_defaults()
    st.session_state.project_id = pid
    # Simulates the state right after a Remove click (see
    # _render_card_row): the id is hidden immediately, before
    # _draw_card_table's own next fetch confirms it's gone for good.
    st.session_state.removed_card_ids = {card.id}
    render_decklist_tab(draw_gallery=True)


def test_removed_card_hidden_from_table(tmp_path, monkeypatch) -> None:
    db_path = tmp_path / "test.db"
    db.init_db(db_path)
    monkeypatch.setattr(db, "DEFAULT_DB_PATH", db_path)

    at = AppTest.from_function(_removed_card_hidden_script)
    at.run()

    assert not at.exception
    assert not any("Sol Ring" in m.value for m in at.markdown)


def _card_shows_quantity_script() -> None:
    import streamlit as st

    from proxy_scaler import db
    from proxy_scaler.ui.decklist import render_decklist_tab
    from proxy_scaler.ui.projects import ensure_session_defaults

    pid = db.save_project(
        "Quantity Display Test",
        import_decklist_text="4 Lightning Bolt (lea) 161\n",
        settings=db.ProjectSettings(),
    )
    db.add_cards_to_project(
        pid,
        [
            {
                "scryfall_id": "bolt-id",
                "card_name": "Lightning Bolt",
                "set_code": "lea",
                "collector_number": "161",
                "quantity": 4,
                "original_import_line": "4 Lightning Bolt (lea) 161",
            }
        ],
    )
    ensure_session_defaults()
    st.session_state.project_id = pid
    render_decklist_tab(draw_gallery=True)


def test_card_row_shows_quantity(tmp_path, monkeypatch) -> None:
    db_path = tmp_path / "test.db"
    db.init_db(db_path)
    monkeypatch.setattr(db, "DEFAULT_DB_PATH", db_path)

    at = AppTest.from_function(_card_shows_quantity_script)
    at.run()

    assert not at.exception
    assert any("×4" in m.value for m in at.markdown)
