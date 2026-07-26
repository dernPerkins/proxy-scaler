"""Round-trip tests for SQLite project persistence."""

from __future__ import annotations

from pathlib import Path

import pytest

from proxy_scaler.db import (
    ProjectSettings,
    delete_project,
    init_db,
    list_projects,
    load_project,
    parse_output_filename,
    save_project,
    scan_gallery_from_output,
)


@pytest.fixture()
def db_path(tmp_path: Path) -> Path:
    path = tmp_path / "test.db"
    init_db(path)
    return path


def test_save_load_round_trip(db_path: Path) -> None:
    deck = "1 Sol Ring (c21) 263\n1 Lightning Bolt\n"
    settings = ProjectSettings(
        model="realesrnet",
        dpi=1200,
        all_dpis=True,
        page_size=4,
        skip_existing=False,
        output_dir="/tmp/out",
        cache_dir="/tmp/cache",
        weights_dir="/tmp/weights",
    )
    gallery = [
        {
            "scryfall_id": "abc-123",
            "face_index": None,
            "face_name": "Sol Ring",
            "card_name": "Sol Ring",
            "set_code": "c21",
            "collector_number": "263",
            "face_label": None,
            "model": "realesrnet",
            "dpi": 1200,
            "native_scale": 4,
            "image_filename": "Sol_Ring-C21-263-realesrnet-1200dpi.png",
            "out_path": "/tmp/out/Sol_Ring-C21-263-realesrnet-1200dpi.png",
            "original_path": "/tmp/cache/orig.png",
            "png_url": "https://example.com/x.png",
        }
    ]

    pid = save_project(
        "Test Deck",
        import_decklist_text=deck,
        settings=settings,
        gallery=gallery,
        db_path=db_path,
    )
    assert pid > 0

    projects = list_projects(db_path)
    assert len(projects) == 1
    assert projects[0].name == "Test Deck"

    loaded = load_project(pid, db_path=db_path)
    assert loaded.name == "Test Deck"
    assert loaded.import_decklist_text == deck
    assert loaded.settings.model == "realesrnet"
    assert loaded.settings.dpi == 1200
    assert loaded.settings.all_dpis is True
    assert loaded.settings.page_size == 4
    assert loaded.settings.skip_existing is False
    assert loaded.settings.output_dir == "/tmp/out"
    assert len(loaded.gallery) == 1
    assert loaded.gallery[0]["scryfall_id"] == "abc-123"
    assert loaded.gallery[0]["set_code"] == "c21"
    assert loaded.gallery[0]["dpi"] == 1200


def test_upsert_replaces_cards_and_gallery(db_path: Path) -> None:
    settings = ProjectSettings()
    pid = save_project(
        "Upsert",
        import_decklist_text="1 Sol Ring (c21) 263\n",
        settings=settings,
        gallery=[
            {
                "scryfall_id": "a",
                "face_index": None,
                "face_name": "Sol Ring",
                "card_name": "Sol Ring",
                "set_code": "c21",
                "collector_number": "263",
                "model": "swinir",
                "dpi": 800,
                "out_path": "/o/a.png",
                "original_path": "/c/a.png",
                "png_url": "",
            }
        ],
        db_path=db_path,
    )
    pid2 = save_project(
        "Upsert",
        import_decklist_text="1 Lightning Bolt (lea) 161\n",
        settings=ProjectSettings(dpi=600),
        gallery=[
            {
                "scryfall_id": "b",
                "face_index": None,
                "face_name": "Lightning Bolt",
                "card_name": "Lightning Bolt",
                "set_code": "lea",
                "collector_number": "161",
                "model": "swinir",
                "dpi": 600,
                "out_path": "/o/b.png",
                "original_path": "/c/b.png",
                "png_url": "",
            }
        ],
        project_id=pid,
        db_path=db_path,
    )
    assert pid2 == pid
    loaded = load_project(pid, db_path=db_path)
    assert "Lightning Bolt" in loaded.import_decklist_text
    assert loaded.settings.dpi == 600
    assert len(loaded.gallery) == 1
    assert loaded.gallery[0]["scryfall_id"] == "b"


def test_delete_cascades(db_path: Path) -> None:
    pid = save_project(
        "Gone",
        import_decklist_text="1 Sol Ring (c21) 263\n",
        settings=ProjectSettings(),
        gallery=[
            {
                "scryfall_id": "a",
                "face_index": None,
                "face_name": "Sol Ring",
                "card_name": "Sol Ring",
                "set_code": "c21",
                "collector_number": "263",
                "model": "swinir",
                "dpi": 800,
                "out_path": "/o/a.png",
                "original_path": "/c/a.png",
                "png_url": "",
            }
        ],
        db_path=db_path,
    )
    delete_project(pid, db_path=db_path)
    assert list_projects(db_path) == []
    with pytest.raises(ValueError, match="not found"):
        load_project(pid, db_path=db_path)


def test_save_requires_name(db_path: Path) -> None:
    with pytest.raises(ValueError, match="required"):
        save_project(
            "  ",
            import_decklist_text="",
            settings=ProjectSettings(),
            gallery=[],
            db_path=db_path,
        )


def test_rename_collision(db_path: Path) -> None:
    a = save_project(
        "Alpha",
        import_decklist_text="",
        settings=ProjectSettings(),
        gallery=[],
        db_path=db_path,
    )
    save_project(
        "Beta",
        import_decklist_text="",
        settings=ProjectSettings(),
        gallery=[],
        db_path=db_path,
    )
    with pytest.raises(ValueError, match="already exists"):
        save_project(
            "Beta",
            import_decklist_text="",
            settings=ProjectSettings(),
            gallery=[],
            project_id=a,
            db_path=db_path,
        )


def test_parse_output_filename() -> None:
    meta = parse_output_filename("Abandoned_Air_Temple-TLA-263-swinir-600dpi.png")
    assert meta is not None
    assert meta["set_code"] == "tla"
    assert meta["collector_number"] == "263"
    assert meta["model"] == "swinir"
    assert meta["dpi"] == 600
    assert meta["face_label"] is None

    dfc = parse_output_filename(
        "Dion_Bahamuts_Dominant-FIN-376-front-swinir-800dpi.png"
    )
    assert dfc is not None
    assert dfc["face_label"] == "front"
    assert dfc["face_index"] == 0

    hyphen_collector = parse_output_filename(
        "Knight_Exemplar-PLST-DDG-14-swinir-600dpi.png"
    )
    assert hyphen_collector is not None
    assert hyphen_collector["set_code"] == "plst"
    assert hyphen_collector["collector_number"] == "DDG-14"


def test_load_recovers_gallery_from_output(db_path: Path, tmp_path: Path) -> None:
    out = tmp_path / "output"
    out.mkdir()
    (out / "Sol_Ring-C21-263-swinir-800dpi.png").write_bytes(b"fake")
    (out / "unrelated.png").write_bytes(b"x")

    pid = save_project(
        "Recover",
        import_decklist_text="1 Sol Ring (c21) 263\n",
        settings=ProjectSettings(output_dir=str(out)),
        gallery=[],  # empty — simulates save-before-generate
        db_path=db_path,
    )
    loaded = load_project(pid, db_path=db_path)
    assert len(loaded.gallery) == 1
    assert loaded.gallery[0]["set_code"] == "c21"
    assert loaded.gallery[0]["dpi"] == 800
    assert "Sol_Ring" in loaded.gallery[0]["out_path"]


def test_scan_gallery_from_output(tmp_path: Path) -> None:
    from proxy_scaler.decklist import parse_decklist_text

    out = tmp_path / "output"
    out.mkdir()
    (out / "Sol_Ring-C21-263-swinir-600dpi.png").write_bytes(b"a")
    (out / "Sol_Ring-C21-263-swinir-800dpi.png").write_bytes(b"b")
    entries = parse_decklist_text("1 Sol Ring (c21) 263\n")
    gallery = scan_gallery_from_output(out, entries)
    assert len(gallery) == 2
    assert {g["dpi"] for g in gallery} == {600, 800}
